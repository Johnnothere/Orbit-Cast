#!/usr/bin/env python3
"""Copy every public table from one OrbitCast database to another.

Used to move production from the Tokyo Supabase project to the London one
(the app runs in europe-west4; every query to Tokyo was a ~250 ms round
trip, London is ~10 ms). The target must already have the schema - apply
supabase/migrations/*.sql to it first, or let the app's boot create the
cache tables.

    OLD_DATABASE_URL='postgresql://...' NEW_DATABASE_URL='postgresql://...' \
        python3 scripts/migrate_db.py            # copy
    ... python3 scripts/migrate_db.py --verify   # row counts on both sides only
    ... python3 scripts/migrate_db.py --overwrite  # source rows replace same-key target rows

Identity columns (GENERATED ALWAYS) keep their source ids (OVERRIDING
SYSTEM VALUE), so foreign keys stay valid. json/jsonb and array columns
round-trip intact.

Safe to re-run: rows are inserted with ON CONFLICT DO NOTHING, so an
interrupted copy picks up where it left off and nothing already in the
target is touched. Tables are copied in foreign-key order. Sequences are
reset to max(id) afterwards so new rows don't collide with copied ones.
Use the direct connection (db.<ref>.supabase.co:5432) or the SESSION pooler
for both sides, not the transaction pooler.
"""
import os
import sys
import time

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

BATCH = 500
OVERWRITE = "--overwrite" in sys.argv


def _adapt(row, json_cols):
    """psycopg2 decodes json/jsonb to dict/list on read; wrap those columns
    so they are written back as json rather than rejected ("can't adapt
    type"). Only json columns: a text[] column also reads as a list and
    must stay one."""
    return tuple(Json(v) if (j and v is not None) else v for v, j in zip(row, json_cols))


def connect(url):
    return psycopg2.connect(url, connect_timeout=15, application_name="orbitcast-migrate")


def tables_in_fk_order(cur):
    """Public tables, parents before children."""
    cur.execute("""
        select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r' order by c.relname""")
    names = [r[0] for r in cur.fetchall()]
    cur.execute("""
        select tc.table_name, ccu.table_name as parent
          from information_schema.table_constraints tc
          join information_schema.constraint_column_usage ccu on ccu.constraint_name = tc.constraint_name
         where tc.constraint_type = 'FOREIGN KEY' and tc.table_schema = 'public'""")
    parents = {}
    for child, parent in cur.fetchall():
        if child != parent:
            parents.setdefault(child, set()).add(parent)
    ordered, seen = [], set()

    def visit(t, stack=()):
        if t in seen or t in stack:
            return
        for p in parents.get(t, ()):
            if p in names:
                visit(p, stack + (t,))
        seen.add(t)
        ordered.append(t)

    for t in names:
        visit(t)
    return ordered


def columns(cur, table):
    cur.execute("""select column_name from information_schema.columns
                    where table_schema='public' and table_name=%s order by ordinal_position""", (table,))
    return [r[0] for r in cur.fetchall()]


def column_types(cur, table):
    cur.execute("""select column_name, data_type from information_schema.columns
                    where table_schema='public' and table_name=%s""", (table,))
    return dict(cur.fetchall())


def primary_key(conn, table):
    with conn.cursor() as cur:
        cur.execute("""select a.attname from pg_index i
                        join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
                       where i.indrelid = %s::regclass and i.indisprimary order by a.attnum""",
                    (f'public."{table}"',))
        return [r[0] for r in cur.fetchall()]


def count(cur, table):
    cur.execute(f'select count(*) from "{table}"')
    return cur.fetchone()[0]


def copy_table(src, dst, table):
    with src.cursor(name=f"copy_{table}") as rcur, dst.cursor() as wcur:
        with src.cursor() as c:
            cols = columns(c, table)
            types = column_types(c, table)
        with dst.cursor() as c:
            dst_cols = set(columns(c, table))
        cols = [c for c in cols if c in dst_cols]
        if not cols:
            return 0, "no shared columns"
        json_cols = [types.get(c) in ("json", "jsonb") for c in cols]
        col_list = ", ".join(f'"{c}"' for c in cols)
        rcur.itersize = BATCH
        rcur.execute(f'select {col_list} from "{table}"')
        # With --overwrite, a row that already exists in the target (same
        # primary key) is replaced by the source's version, so a stale
        # partial copy in the target cannot shadow current data. Without it,
        # existing target rows are left alone.
        pk = primary_key(dst, table)
        if OVERWRITE and pk and all(k in cols for k in pk):
            setters = ", ".join(f'"{c}" = excluded."{c}"' for c in cols if c not in pk)
            conflict = ", ".join(f'"{k}"' for k in pk)
            action = f"do update set {setters}" if setters else "do nothing"
            sql = f'insert into "{table}" ({col_list}) overriding system value values %s on conflict ({conflict}) {action}'
        else:
            sql = f'insert into "{table}" ({col_list}) overriding system value values %s on conflict do nothing'
        n = 0
        while True:
            rows = rcur.fetchmany(BATCH)
            if not rows:
                break
            psycopg2.extras.execute_values(wcur, sql, [_adapt(r, json_cols) for r in rows], page_size=BATCH)
            n += len(rows)
        dst.commit()
        return n, None


def reset_sequences(dst):
    # deptype 'a' is how a serial column owns its sequence; 'i' is how an
    # IDENTITY column does. Matching only 'a' skipped every identity column,
    # still printed "sequences reset", and left the London copy's counters
    # behind its rows - so new inserts collided with copied ids (duplicate
    # key on analyses and raw_files, 31 Oct 2026). See migration 0025.
    with dst.cursor() as cur:
        cur.execute("""
            select s.relname, t.relname, a.attname
              from pg_class s
              join pg_depend d on d.objid = s.oid and d.deptype in ('a', 'i')
              join pg_class t on t.oid = d.refobjid
              join pg_attribute a on a.attrelid = t.oid and a.attnum = d.refobjsubid
              join pg_namespace n on n.oid = s.relnamespace
             where s.relkind = 'S' and n.nspname = 'public'""")
        for seq, table, col in cur.fetchall():
            cur.execute(f'select coalesce(max("{col}"), 0) from "{table}"')
            mx = cur.fetchone()[0]
            cur.execute("select setval(%s, %s, %s)", (f'public."{seq}"', max(mx, 1), mx > 0))
    dst.commit()


def main():
    old, new = os.environ.get("OLD_DATABASE_URL"), os.environ.get("NEW_DATABASE_URL")
    if not old or not new:
        sys.exit("Set OLD_DATABASE_URL and NEW_DATABASE_URL.")
    if old == new:
        sys.exit("OLD and NEW are the same database.")
    verify_only = "--verify" in sys.argv
    src, dst = connect(old), connect(new)
    src.set_session(readonly=True, autocommit=False)
    with src.cursor() as c:
        order = tables_in_fk_order(c)
    print(f"{'table':24} {'old':>8} {'new':>8}   note")
    total, failures = 0, []
    for t in order:
        with src.cursor() as c:
            n_old = count(c, t)
        try:
            with dst.cursor() as c:
                n_new_before = count(c, t)
        except psycopg2.Error:
            dst.rollback()
            print(f"{t:24} {n_old:8} {'-':>8}   not in target (skipped)")
            continue
        note = ""
        if not verify_only and n_old:
            t0 = time.time()
            try:
                copied, err = copy_table(src, dst, t)
            except psycopg2.Error as exc:
                dst.rollback()
                copied, err = 0, f"FAILED: {str(exc).strip()[:120]}"
                failures.append(t)
            note = err or f"copied {copied} in {time.time() - t0:.1f}s"
            total += copied
        with dst.cursor() as c:
            n_new = count(c, t)
        flag = "" if n_new >= n_old else "   <-- fewer rows in target"
        print(f"{t:24} {n_old:8} {n_new:8}   {note}{flag}")
    if not verify_only:
        reset_sequences(dst)
        print(f"\nDone: {total} rows copied, sequences reset.")
        if failures:
            print(f"FAILED tables (fix and re-run; already-copied rows are kept): {', '.join(failures)}")
            sys.exit(1)
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
