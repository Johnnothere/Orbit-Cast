"""
OrbitCast AI - Postgres (Supabase) persistence layer.

Consent is the hinge: every write function here (except set_consent itself)
is only ever called by app.py after confirming consent=true for that oc_uid.
This module doesn't re-check consent - that's app.py's job - it just does
the actual reads/writes and never crashes the request if the DB is
unreachable or unconfigured.

Everything degrades gracefully when DATABASE_URL isn't set: every function
returns None/[]/False instead of raising, so the app runs exactly as it did
before this module existed if no database is wired up.
"""

import hashlib
import json
import logging
import os
import queue
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("orbitcast.db")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Connection pool, sized for one process serving many threads at once.
#
# This used to be psycopg2's SimpleConnectionPool(1, 10). Three problems, the
# first of which mattered most:
#  - psycopg2's pools keep only `minconn` idle connections (1 here) and CLOSE
#    every other connection when it is handed back. As soon as two requests
#    overlapped, nearly every query opened a brand-new connection - TCP, TLS,
#    pooler auth - to a database on the other side of the world, while holding
#    the pool's lock, so every other thread queued behind that handshake.
#  - SimpleConnectionPool is documented as not thread-safe; this process runs
#    dozens of threads.
#  - When all 10 were in use, getconn() raised at once instead of waiting.
#
# This pool keeps every connection it opens (up to DB_POOL_MAX) and reuses it,
# opens new ones outside any lock, and makes a thread that finds them all busy
# wait up to DB_POOL_WAIT_S. Connections idle longer than DB_IDLE_MAX_S are
# replaced rather than reused, so one the pooler quietly dropped is not handed
# to a request.
#
# Keep DB_POOL_MAX inside what the Supabase connection string allows. The app
# connects through Supavisor: in SESSION mode (pooler port 5432) every client
# connection holds a database connection and the plan's pool size (15 on the
# smallest plans) is the hard ceiling for the whole project - so the default
# stays at the old 10. On the TRANSACTION pooler (port 6543) it can safely go
# to 20-30.
DB_POOL_MAX = max(2, int(os.environ.get("DB_POOL_MAX", "10")))
DB_POOL_WAIT_S = float(os.environ.get("DB_POOL_WAIT_S", "15"))
DB_IDLE_MAX_S = float(os.environ.get("DB_IDLE_MAX_S", "240"))


class _Pool:
    def __init__(self, dsn, maxconn):
        self.dsn = dsn
        self.maxconn = maxconn
        self._slots = threading.BoundedSemaphore(maxconn)
        self._idle = []                     # [(conn, last_used_monotonic)] - LIFO
        self._lock = threading.Lock()
        self.in_use = 0
        self.opened = 0

    def _connect(self):
        import psycopg2
        conn = psycopg2.connect(
            self.dsn, connect_timeout=10,
            # TCP keepalives, so a connection the network or the pooler
            # silently dropped is noticed instead of hanging a query.
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
            application_name="orbitcast-web")
        with self._lock:
            self.opened += 1
        return conn

    def acquire(self):
        if not self._slots.acquire(timeout=DB_POOL_WAIT_S):
            raise RuntimeError(f"database pool busy: no connection free after {DB_POOL_WAIT_S:.0f}s")
        try:
            now = time.monotonic()
            stale = []
            conn = None
            with self._lock:
                while self._idle:
                    c, used = self._idle.pop()
                    if c.closed or now - used > DB_IDLE_MAX_S:
                        stale.append(c)
                        continue
                    conn = c
                    break
                self.in_use += 1
            for c in stale:
                try:
                    c.close()
                except Exception:
                    pass
            if conn is None:
                conn = self._connect()      # outside the lock
            return conn
        except Exception:
            with self._lock:
                self.in_use -= 1
            self._slots.release()
            raise

    def release(self, conn, broken=False):
        try:
            if conn is not None and not broken and not conn.closed:
                import psycopg2.extensions as ext
                status = conn.info.transaction_status
                if status == ext.TRANSACTION_STATUS_UNKNOWN:
                    broken = True
                elif status != ext.TRANSACTION_STATUS_IDLE:
                    conn.rollback()
            if conn is not None:
                if broken or conn.closed:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    with self._lock:
                        self._idle.append((conn, time.monotonic()))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        finally:
            with self._lock:
                self.in_use -= 1
            self._slots.release()


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if not DATABASE_URL:
        return None
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _Pool(DATABASE_URL, DB_POOL_MAX)
    return _pool


def is_configured() -> bool:
    return bool(DATABASE_URL)


def pool_stats() -> dict:
    """For /api/health: connections checked out now, and opened since boot
    (a number that keeps climbing means connections are being dropped)."""
    if _pool is None:
        return {"max": DB_POOL_MAX, "in_use": 0, "opened": 0}
    return {"max": DB_POOL_MAX, "in_use": _pool.in_use, "opened": _pool.opened,
            "idle": len(_pool._idle)}


@contextmanager
def _cursor():
    pool = _get_pool()
    if pool is None:
        yield None
        return
    import psycopg2
    conn = pool.acquire()
    broken = False
    try:
        with conn:                          # commit on success, rollback on error
            with conn.cursor() as cur:
                yield cur
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    finally:
        pool.release(conn, broken)


# ── Small in-process caches for the hottest, rarely-changing reads ──────────
# get_account runs on every signed-in request and get_config on every scoring
# call. Both rows change rarely and only through functions in this module,
# which clear the cache when they do. Single process (see Procfile), so a
# clear here is seen by every later request at once.
_TTL_ACCOUNT_S = 60
_TTL_CONFIG_S = 60
_account_cache: dict = {}
_config_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_get(cache, key, ttl):
    with _cache_lock:
        hit = cache.get(key)
    if hit and (time.monotonic() - hit[0]) < ttl:
        return True, hit[1]
    return False, None


def _cache_put(cache, key, value):
    with _cache_lock:
        if len(cache) > 5000:
            cache.clear()
        cache[key] = (time.monotonic(), value)


def _clear_account_cache():
    with _cache_lock:
        _account_cache.clear()


def _clear_config_cache():
    with _cache_lock:
        _config_cache.clear()


@contextmanager
def _read_cursor():
    """For a single SELECT. Runs in autocommit, so it costs one network round
    trip instead of three (psycopg2 otherwise sends BEGIN, the query, and
    COMMIT separately). The database is a long way from the web server, so
    each round trip is ~100 ms: on /api/me, which makes several of these reads
    per signed-in page load, that was the bulk of the response time.

    Only for functions that run exactly one SELECT - anything that writes, or
    reads twice and needs both reads consistent, keeps using _cursor()."""
    pool = _get_pool()
    if pool is None:
        yield None
        return
    import psycopg2
    conn = pool.acquire()
    broken = False
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            yield cur
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    finally:
        try:
            if not conn.closed:
                conn.autocommit = False      # back to normal for the next borrower
        except Exception:
            broken = True
        pool.release(conn, broken)


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------
def get_consent(oc_uid: str):
    """Returns True/False if a decision is on record, None if never asked."""
    if not oc_uid:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select consented from consent where oc_uid = %s", (oc_uid,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"get_consent failed: {exc}")
        return None


def set_consent(oc_uid: str, consented: bool, referrer: str = None, utm_source: str = None,
                 utm_medium: str = None, utm_campaign: str = None) -> bool:
    """Traffic-source fields are only ever populated on the FIRST accept for
    a given oc_uid (coalesce keeps whatever was captured then) - a later
    re-answer (e.g. from a re-triggered banner) never overwrites the
    original attribution with nulls."""
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into consent (oc_uid, consented, consented_at, updated_at,
                                      referrer, utm_source, utm_medium, utm_campaign)
                values (%s, %s, now(), now(), %s, %s, %s, %s)
                on conflict (oc_uid) do update
                  set consented = excluded.consented, updated_at = now(),
                      referrer = coalesce(consent.referrer, excluded.referrer),
                      utm_source = coalesce(consent.utm_source, excluded.utm_source),
                      utm_medium = coalesce(consent.utm_medium, excluded.utm_medium),
                      utm_campaign = coalesce(consent.utm_campaign, excluded.utm_campaign)
                """,
                (oc_uid, consented, referrer, utm_source, utm_medium, utm_campaign),
            )
            return True
    except Exception as exc:
        log.warning(f"set_consent failed: {exc}")
        return False


def get_location_consent(oc_uid: str):
    """Same True/False/None contract as get_consent, but for the separate
    location permission - granting GPS access is a different decision from
    letting us save a CV, so it gets its own yes/no rather than piggybacking
    on the general consent flag."""
    if not oc_uid:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select location_consented from consent where oc_uid = %s", (oc_uid,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"get_location_consent failed: {exc}")
        return None


def set_location_consent(oc_uid: str, consented: bool) -> bool:
    """Requires a consent row to already exist (i.e. the general consent
    banner must have been shown first) since location_consented lives on
    that same table - the app's flow enforces this ordering."""
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                update consent
                set location_consented = %s, location_consented_at = now()
                where oc_uid = %s
                """,
                (consented, oc_uid),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"set_location_consent failed: {exc}")
        return False


def save_user_location(oc_uid: str, lat: float, lng: float, accuracy_m: float = None) -> bool:
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "insert into user_locations (oc_uid, lat, lng, accuracy_m) values (%s, %s, %s, %s)",
                (oc_uid, lat, lng, accuracy_m),
            )
            return True
    except Exception as exc:
        log.warning(f"save_user_location failed: {exc}")
        return False


def get_latest_location(oc_uid: str):
    """The most recent fix for this oc_uid, or None if they never granted
    location or a lookup failed. Used to compute distance-to-event; never
    raises, so a DB hiccup just means recommendations render without
    distances rather than breaking the analysis."""
    if not oc_uid:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "select lat, lng from user_locations where oc_uid = %s order by created_at desc limit 1",
                (oc_uid,),
            )
            row = cur.fetchone()
            return {"lat": row[0], "lng": row[1]} if row else None
    except Exception as exc:
        log.warning(f"get_latest_location failed: {exc}")
        return None


def forget(oc_uid: str) -> bool:
    """GDPR delete: wipes every row tied to this oc_uid, consent record included."""
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            # cascades take care of analyses/recommendations/interactions
            cur.execute("delete from consent where oc_uid = %s", (oc_uid,))
            return True
    except Exception as exc:
        log.warning(f"forget failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Analyses + recommendations
# --------------------------------------------------------------------------
def save_analysis(oc_uid: str, is_cv: bool, cv_text: str, profile: dict,
                   evidence_level: str = None, field: str = None):
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                insert into analyses (oc_uid, is_cv, cv_text, profile, evidence_level, field)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                (oc_uid, is_cv, cv_text, json.dumps(profile), evidence_level, field),
            )
            return cur.fetchone()[0]
    except Exception as exc:
        log.warning(f"save_analysis failed: {exc}")
        return None


def get_latest_analysis(oc_uid: str):
    """Powers the returning-visitor 'reuse my last profile' prompt - only
    ever called for the caller's OWN oc_uid (enforced in app.py by reading
    it from the request cookie, never a client-supplied id), so this never
    exposes one person's profile to another."""
    if not oc_uid:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                select profile, evidence_level, field
                from analyses
                where oc_uid = %s and is_cv = true
                order by created_at desc limit 1
                """,
                (oc_uid,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            profile, evidence_level, field = row
            if isinstance(profile, str):
                try:
                    profile = json.loads(profile)
                except Exception:
                    profile = None
            if not profile:
                return None
            return {"profile": profile, "evidence_level": evidence_level or "rich", "field": field or ""}
    except Exception as exc:
        log.warning(f"get_latest_analysis failed: {exc}")
        return None


def save_raw_file(analysis_id, filename: str, content_type: str, file_bytes: bytes):
    """Stores the ORIGINAL uploaded file byte-for-byte, alongside the
    extracted-text analysis row it belongs to. Only ever called after the
    same consent check that gates save_analysis - this is additive storage
    for the same purpose, not a separate consent surface."""
    if analysis_id is None or not file_bytes:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                insert into raw_files (analysis_id, filename, content_type, file_bytes, size_bytes)
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                # psycopg2 auto-adapts a plain `bytes` value to bytea - no
                # explicit Binary() wrapper needed (registered by default on
                # import, matching the lazy-import style the rest of this
                # module uses for the psycopg2 dependency).
                (analysis_id, filename, content_type, file_bytes, len(file_bytes)),
            )
            return cur.fetchone()[0]
    except Exception as exc:
        log.warning(f"save_raw_file failed: {exc}")
        return None


def get_raw_file(analysis_id):
    """Returns {filename, content_type, file_bytes} for the admin download
    route, or None if this analysis has no stored original file (text-only
    submission, consent declined, or it predates this feature)."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                select filename, content_type, file_bytes
                from raw_files where analysis_id = %s
                order by created_at desc limit 1
                """,
                (analysis_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {"filename": row[0], "content_type": row[1], "file_bytes": bytes(row[2])}
    except Exception as exc:
        log.warning(f"get_raw_file failed: {exc}")
        return None


def get_cv_status(oc_uid: str):
    """What this person currently has on file, for the account modal.

    Reports the two things separately because they are deleted separately and
    have different retention meaning: the ORIGINAL uploaded file (bytes, in
    raw_files) and the EXTRACTED TEXT (cv_text, on analyses). A text-only
    submission - someone who typed a self-description instead of uploading -
    has text and no file, and must not be reported as "no CV on file".

    Counts rather than a boolean so the UI can say how many uploads are held,
    which is the honest number: every analysis stores its own row, so someone
    who ran five analyses has five stored files, not one."""
    if not oc_uid:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                select
                  count(rf.id)                                   as file_count,
                  coalesce(sum(rf.size_bytes), 0)                as total_bytes,
                  max(rf.created_at)                             as last_file_at,
                  (select max(a2.created_at) from analyses a2
                    where a2.oc_uid = %s and a2.cv_text is not null
                      and a2.cv_text <> '')                      as last_text_at,
                  (select rf2.filename from raw_files rf2
                     join analyses a3 on a3.id = rf2.analysis_id
                    where a3.oc_uid = %s
                    order by rf2.created_at desc limit 1)        as last_filename
                from raw_files rf
                join analyses a on a.id = rf.analysis_id
                where a.oc_uid = %s
                """,
                (oc_uid, oc_uid, oc_uid),
            )
            row = cur.fetchone()
            if row is None:
                return {"file_count": 0, "total_bytes": 0, "last_file_at": None,
                        "last_text_at": None, "last_filename": None}
            return {"file_count": row[0] or 0,
                    "total_bytes": int(row[1] or 0),
                    "last_file_at": _iso(row[2]),
                    "last_text_at": _iso(row[3]),
                    "last_filename": row[4]}
    except Exception as exc:
        log.warning(f"get_cv_status failed: {exc}")
        return None


def delete_stored_cv(oc_uid: str):
    """Deletes this person's stored CV material without touching their account.

    Removes the raw uploaded files outright and blanks cv_text, but KEEPS the
    derived `profile` jsonb. That split is deliberate: profile is what
    /api/analyze/reuse and every scheduled digest match against, so deleting
    it would silently stop someone's alerts as a side effect of a button that
    said "delete my CV". /api/forget remains the delete-everything path, and
    the UI says which is which.

    Returns {"files_deleted", "texts_cleared"} so the route can report what
    actually happened rather than a bare ok - "deleted" when there was nothing
    to delete is the kind of reassurance that hides a bug."""
    if not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            # Scoped by a join back to analyses.oc_uid, never by an id handed
            # in by the caller - the same own-data-only rule as get_raw_file's
            # callers, enforced here in SQL so a future route cannot get it
            # wrong.
            cur.execute(
                """
                delete from raw_files rf
                using analyses a
                where rf.analysis_id = a.id and a.oc_uid = %s
                """,
                (oc_uid,),
            )
            files_deleted = cur.rowcount or 0
            cur.execute(
                """
                update analyses set cv_text = null
                where oc_uid = %s and cv_text is not null and cv_text <> ''
                """,
                (oc_uid,),
            )
            texts_cleared = cur.rowcount or 0
            return {"files_deleted": files_deleted, "texts_cleared": texts_cleared}
    except Exception as exc:
        log.warning(f"delete_stored_cv failed: {exc}")
        return None


def save_recommendations(analysis_id, recommendations: list):
    """Inserts each recommendation, returns a list of DB ids in the same
    order as the input list (None for any that failed to insert)."""
    if analysis_id is None or not recommendations:
        return [None] * len(recommendations)
    ids = []
    try:
        with _cursor() as cur:
            if cur is None:
                return [None] * len(recommendations)
            for r in recommendations:
                cur.execute(
                    """
                    insert into recommendations
                      (analysis_id, event_id, title, category, fit_score,
                       why, why_now, prepare, benefit, weakness)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (analysis_id, r.get("event_id"), r.get("title"), r.get("category"),
                     r.get("fit_score"), r.get("why"), r.get("why_now"),
                     r.get("prepare"), r.get("benefit"), r.get("weakness")),
                )
                ids.append(cur.fetchone()[0])
            return ids
    except Exception as exc:
        log.warning(f"save_recommendations failed: {exc}")
        return [None] * len(recommendations)


def _iso(dt):
    """timestamptz columns come back as datetime objects - stringify to ISO
    8601 explicitly rather than relying on Flask's jsonify default encoder,
    so the admin frontend always gets something JS's `new Date()` parses."""
    return dt.isoformat() if dt else None


def get_funnel_analytics():
    """Admin dashboard: conversion funnel (accepted -> submitted an
    analysis -> clicked a recommendation), broken down by traffic source.
    Every stage counts DISTINCT oc_uid so someone who submitted 5 CVs still
    counts once toward 'submitted' - this is a conversion funnel, not a
    raw event tally. Source resolution: UTM param first, then the
    referrer's bare hostname, then 'direct / unknown' for a typed URL or a
    browser that withheld the referrer."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select
                  coalesce(
                    nullif(c.utm_source, ''),
                    nullif(regexp_replace(c.referrer, '^https?://([^/]+).*$', '\\1'), ''),
                    'direct / unknown'
                  ) as source,
                  count(distinct c.oc_uid) as accepted,
                  count(distinct a.oc_uid) as submitted_analysis,
                  count(distinct i.oc_uid) as clicked_recommendation
                from consent c
                left join analyses a on a.oc_uid = c.oc_uid
                left join interactions i on i.oc_uid = c.oc_uid
                where c.consented = true
                group by source
                order by accepted desc
                """
            )
            cols = ["source", "accepted", "submitted_analysis", "clicked_recommendation"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning(f"get_funnel_analytics failed: {exc}")
        return []


def _rows(cur, sql, params=None):
    """Run a query and return a list of dicts keyed by the cursor's own
    column names, so a SELECT can be edited without also editing a
    hand-maintained column list next to it."""
    cur.execute(sql, params or ())
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_analytics_overview(days: int = 30):
    """Everything the admin analytics page renders, in one round trip.

    Deliberately exhaustive: every table and column we persist is
    represented somewhere below, because a metric that exists in the
    database but nowhere in the UI is a metric nobody acts on. Each block
    is independent - one failing query returns an empty section rather
    than taking down the whole page.

    Note the honesty metrics (zero_match_rate, fit score distribution,
    matches-per-analysis): those exist to make the product's central claim
    - "two to four strong matches, frequently none" - measurable rather
    than just asserted."""
    out = {
        "totals": {}, "daily": [], "fit_hist": [], "matches_dist": [],
        "categories": [], "evidence": [], "input_kinds": [], "seniority": [],
        "fields": [], "top_events": [], "clicked_events": [], "hours": [],
        "weekdays": [], "repeat": [], "files": {}, "file_types": [],
        "locations": [], "location_consent": {}, "labeled": [],
        "traffic_detail": [], "recent": [],
    }
    try:
        with _cursor() as cur:
            if cur is None:
                return out

            # The 7/30/90 buttons used to re-run all 21 queries and change
            # exactly ONE chart: only `daily` read the parameter, everything
            # else was all-time. `win` is spliced into every block that has a
            # timestamp. days=0 means all time. int() is the injection guard —
            # nothing user-supplied reaches the string.
            d = int(days or 0)
            win = "" if d <= 0 else f" and {{}} >= now() - interval '{d} days'"

            def W(col):
                return win.format(col) if win else ""

            def safe(key, sql, params=None, single=False):
                """One bad query must not blank the whole dashboard."""
                try:
                    rows = _rows(cur, sql, params)
                    out[key] = (rows[0] if rows else {}) if single else rows
                except Exception as exc:
                    log.warning(f"analytics[{key}] failed: {exc}")

            # ---- headline counters -------------------------------------
            safe("totals", """
                select
                  (select count(*) from consent)                          as visitors,
                  (select count(*) from consent where consented)          as accepted,
                  (select count(*) from consent where consented = false)  as declined,
                  (select count(*) from analyses)                         as analyses,
                  (select count(distinct oc_uid) from analyses)           as people_analysed,
                  (select count(*) from analyses where is_cv)             as usable_inputs,
                  (select count(*) from recommendations)                  as recommendations,
                  (select count(*) from interactions)                     as clicks,
                  (select count(*) from raw_files)                        as files,
                  (select count(*) from user_locations)                   as location_pings,
                  (select count(*) from labeled_examples)                 as labeled_examples,
                  (select coalesce(round(avg(fit_score)::numeric,1),0) from recommendations) as avg_fit,
                  (select coalesce(max(fit_score),0) from recommendations)                   as max_fit,
                  (select coalesce(min(fit_score),0) from recommendations)                   as min_fit
            """, single=True)

            # the same counters, inside the window, so a KPI can show both
            safe("totals_window", f"""
                select
                  (select count(*) from consent
                    where consented {W('consented_at') or ''})                  as accepted,
                  (select count(*) from analyses where true {W('created_at')})  as analyses,
                  (select count(distinct oc_uid) from analyses
                    where true {W('created_at')})                               as people_analysed,
                  (select count(*) from recommendations
                    where true {W('created_at')})                               as recommendations,
                  (select count(*) from interactions
                    where true {W('created_at')})                               as clicks
            """, single=True)

            # ---- activity over time ------------------------------------
            # Generated date spine so quiet days plot as zero instead of
            # silently collapsing the x-axis.
            safe("daily", """
                with bounds as (
                  select coalesce(least((select min(consented_at) from consent),
                                        (select min(created_at)   from analyses)),
                                  now())::date as start_d
                ), spine as (
                  select generate_series(
                    case when %s::int = 0 then (select start_d from bounds)
                         else (current_date - (%s || ' days')::interval)::date end,
                    current_date, '1 day')::date as d
                )
                select to_char(s.d,'YYYY-MM-DD') as day,
                  (select count(*) from consent c
                     where c.consented and c.consented_at::date = s.d)      as accepted,
                  (select count(*) from analyses a
                     where a.created_at::date = s.d)                        as analyses,
                  (select count(*) from interactions i
                     where i.created_at::date = s.d)                        as clicks
                from spine s order by s.d
            """, (d, d))

            # ---- the honesty curve -------------------------------------
            safe("fit_hist", f"""
                select width_bucket(fit_score, 60, 100, 8) as bucket,
                       60 + (width_bucket(fit_score, 60, 100, 8) - 1) * 5 as lo,
                       count(*) as n
                from recommendations where fit_score is not null {W('created_at')}
                group by 1,2 order by 1
            """)
            safe("matches_dist", f"""
                select n_matches, count(*) as analyses from (
                  select a.id, count(r.id) as n_matches
                  from analyses a
                  left join recommendations r on r.analysis_id = a.id
                  where a.is_cv {W('a.created_at')} group by a.id
                ) t group by n_matches order by n_matches
            """)

            # ---- what the engine actually recommends -------------------
            safe("categories", f"""
                select coalesce(nullif(category,''),'Uncategorised') as category,
                       count(*) as n, round(avg(fit_score)::numeric,1) as avg_fit
                from recommendations where true {W('created_at')} group by 1 order by n desc
            """)
            safe("top_events", f"""
                select title, coalesce(nullif(category,''),'—') as category,
                       count(*) as times_recommended,
                       round(avg(fit_score)::numeric,1) as avg_fit
                from recommendations where title is not null and title <> '' {W('created_at')}
                group by title, category order by times_recommended desc, avg_fit desc limit 12
            """)
            safe("clicked_events", f"""
                select r.title, count(*) as clicks
                from interactions i join recommendations r on r.id = i.recommendation_id
                where r.title is not null and r.title <> '' {W('i.created_at')}
                group by r.title order by clicks desc limit 12
            """)

            # ---- who the people are ------------------------------------
            safe("evidence", f"""
                select coalesce(nullif(evidence_level,''),'unknown') as evidence_level,
                       count(*) as n
                from analyses where true {W('created_at')} group by 1 order by n desc
            """)
            safe("input_kinds", f"""
                select case when is_cv then 'usable (CV / self-description)'
                            else 'unusable input' end as kind, count(*) as n
                from analyses where true {W('created_at')} group by 1 order by n desc
            """)
            safe("seniority", f"""
                select coalesce(nullif(profile->>'seniority',''),'unstated') as seniority,
                       count(*) as n
                from analyses where profile is not null {W('created_at')} group by 1 order by n desc
            """)
            safe("fields", f"""
                select coalesce(nullif(field,''),'unstated') as field, count(*) as n
                from analyses where true {W('created_at')} group by 1 order by n desc limit 12
            """)

            # ---- rhythm -------------------------------------------------
            safe("hours", f"""
                select extract(hour from created_at)::int as hour, count(*) as n
                from analyses where true {W('created_at')} group by 1 order by 1
            """)
            safe("weekdays", f"""
                select extract(isodow from created_at)::int as dow, count(*) as n
                from analyses where true {W('created_at')} group by 1 order by 1
            """)
            safe("repeat", f"""
                select n_analyses, count(*) as people from (
                  select oc_uid, count(*) as n_analyses from analyses where true {W('created_at')} group by oc_uid
                ) t group by n_analyses order by n_analyses
            """)

            # ---- uploads ------------------------------------------------
            safe("files", f"""
                select count(*) as n,
                       coalesce(sum(size_bytes),0) as total_bytes,
                       coalesce(round(avg(size_bytes))::bigint,0) as avg_bytes,
                       coalesce(max(size_bytes),0) as max_bytes
                from raw_files where true {W('created_at')}
            """, single=True)
            safe("file_types", f"""
                select coalesce(nullif(lower(regexp_replace(filename,'^.*\\.','')),''),'unknown') as ext,
                       count(*) as n, coalesce(round(avg(size_bytes))::bigint,0) as avg_bytes
                from raw_files where true {W('created_at')} group by 1 order by n desc
            """)

            # ---- location ------------------------------------------------
            safe("location_consent", """
                select
                  count(*) filter (where location_consented is true)  as granted,
                  count(*) filter (where location_consented is false) as refused,
                  count(*) filter (where location_consented is null)  as never_asked
                from consent
            """, single=True)
            safe("locations", f"""
                select round(lat::numeric,4) as lat, round(lng::numeric,4) as lng,
                       round(coalesce(accuracy_m,0)::numeric,0) as accuracy_m,
                       to_char(created_at,'YYYY-MM-DD') as day
                from user_locations where true {W('created_at')} order by created_at desc limit 500
            """)

            # ---- training data + traffic detail --------------------------
            safe("labeled", f"""
                select judgment, count(*) as n from labeled_examples
                where true {W('created_at')} group by 1 order by n desc
            """)
            safe("traffic_detail", f"""
                select
                  coalesce(nullif(utm_source,''),'—')   as utm_source,
                  coalesce(nullif(utm_medium,''),'—')   as utm_medium,
                  coalesce(nullif(utm_campaign,''),'—') as utm_campaign,
                  coalesce(nullif(regexp_replace(referrer,'^https?://([^/]+).*$','\\1'),''),'—') as referrer,
                  count(*) as people
                from consent where consented {W('consented_at')}
                group by 1,2,3,4 order by people desc limit 20
            """)
            safe("recent", """
                select to_char(a.created_at,'YYYY-MM-DD HH24:MI') as at,
                       a.is_cv, coalesce(nullif(a.evidence_level,''),'—') as evidence_level,
                       coalesce(nullif(a.field,''),'—') as field,
                       coalesce(nullif(a.profile->>'seniority',''),'—') as seniority,
                       (select count(*) from recommendations r where r.analysis_id = a.id) as matches,
                       left(a.oc_uid, 8) as uid
                from analyses a order by a.created_at desc limit 15
            """)
        out["window_days"] = int(days or 0)
        # location_consent and `recent` are deliberately NOT windowed: the
        # first is a standing state rather than an event, the second is "the
        # latest 15" by definition. The UI labels both as all-time.
        out["all_time_blocks"] = ["totals", "location_consent", "recent"]
    except Exception as exc:
        log.warning(f"get_analytics_overview failed: {exc}")
    return out


def list_users(limit: int = 200):
    """Admin dashboard: one row per oc_uid ever asked for consent (whether
    they said yes or no), with activity counts, most recently active first.
    A user who declined consent shows up with consented=false and zero
    counts - everything downstream of that decision was never written."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select c.oc_uid, c.consented, c.consented_at,
                       count(distinct a.id) as analyses_count,
                       count(distinct i.id) as interactions_count,
                       greatest(c.consented_at, max(a.created_at), max(i.created_at)) as last_active
                from consent c
                left join analyses a on a.oc_uid = c.oc_uid
                left join interactions i on i.oc_uid = c.oc_uid
                group by c.oc_uid, c.consented, c.consented_at
                order by last_active desc
                limit %s
                """,
                (limit,),
            )
            cols = ["oc_uid", "consented", "consented_at", "analyses_count",
                    "interactions_count", "last_active"]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["consented_at"] = _iso(d["consented_at"])
                d["last_active"] = _iso(d["last_active"])
                rows.append(d)
            return rows
    except Exception as exc:
        log.warning(f"list_users failed: {exc}")
        return []


def get_user_activity(oc_uid: str):
    """The full timeline for one oc_uid: consent decision, every analysis
    they ran (each with its recommendations nested), and every interaction
    (event clicks). Returns None if this oc_uid has no consent record at
    all - i.e. it was never issued a cookie that reached /api/consent, so
    there is nothing to show, distinct from a real user with zero activity."""
    if not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None

            cur.execute(
                """
                select oc_uid, consented, consented_at, updated_at,
                       location_consented, location_consented_at,
                       referrer, utm_source, utm_medium, utm_campaign
                from consent where oc_uid = %s
                """,
                (oc_uid,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            consent = dict(zip(["oc_uid", "consented", "consented_at", "updated_at",
                                 "location_consented", "location_consented_at",
                                 "referrer", "utm_source", "utm_medium", "utm_campaign"], row))
            consent["consented_at"] = _iso(consent["consented_at"])
            consent["updated_at"] = _iso(consent["updated_at"])
            consent["location_consented_at"] = _iso(consent["location_consented_at"])

            cur.execute(
                "select lat, lng, accuracy_m, created_at from user_locations "
                "where oc_uid = %s order by created_at desc limit 10",
                (oc_uid,),
            )
            locations = [dict(zip(["lat", "lng", "accuracy_m", "created_at"], r)) for r in cur.fetchall()]
            for loc in locations:
                loc["created_at"] = _iso(loc["created_at"])

            cur.execute(
                """
                select a.id, a.created_at, a.is_cv, a.cv_text, a.profile,
                       a.evidence_level, a.field,
                       (select rf.size_bytes from raw_files rf
                        where rf.analysis_id = a.id order by rf.created_at desc limit 1) as raw_file_size_bytes
                from analyses a
                where a.oc_uid = %s order by a.created_at desc
                """,
                (oc_uid,),
            )
            a_cols = ["id", "created_at", "is_cv", "cv_text", "profile",
                      "evidence_level", "field", "raw_file_size_bytes"]
            analyses = [dict(zip(a_cols, r)) for r in cur.fetchall()]
            for a in analyses:
                a["created_at"] = _iso(a["created_at"])
                # profile is jsonb - psycopg2-binary auto-decodes it to a
                # dict, but guard the off chance it comes back as a raw
                # string rather than crash the whole admin view over it.
                if isinstance(a["profile"], str):
                    try:
                        a["profile"] = json.loads(a["profile"])
                    except Exception:
                        pass

            analysis_ids = [a["id"] for a in analyses]
            recs_by_analysis = {aid: [] for aid in analysis_ids}
            if analysis_ids:
                cur.execute(
                    """
                    select id, analysis_id, event_id, title, category, fit_score,
                           why, why_now, prepare, benefit, weakness, created_at
                    from recommendations
                    where analysis_id = any(%s)
                    order by fit_score desc nulls last
                    """,
                    (analysis_ids,),
                )
                r_cols = ["id", "analysis_id", "event_id", "title", "category", "fit_score",
                          "why", "why_now", "prepare", "benefit", "weakness", "created_at"]
                for r in cur.fetchall():
                    rec = dict(zip(r_cols, r))
                    rec["created_at"] = _iso(rec["created_at"])
                    # band is derived, not stored - stamping here keeps the
                    # server as the single source of truth for how a score
                    # reads (ai_engine.band_label).
                    try:
                        import ai_engine
                        rec["band"] = ai_engine.band_label(rec.get("fit_score"))
                    except Exception:
                        pass
                    recs_by_analysis.setdefault(rec["analysis_id"], []).append(rec)
            for a in analyses:
                a["recommendations"] = recs_by_analysis.get(a["id"], [])

            cur.execute(
                """
                select i.id, i.recommendation_id, i.event_type, i.created_at, r.title
                from interactions i
                left join recommendations r on r.id = i.recommendation_id
                where i.oc_uid = %s
                order by i.created_at desc
                """,
                (oc_uid,),
            )
            i_cols = ["id", "recommendation_id", "event_type", "created_at", "event_title"]
            interactions = [dict(zip(i_cols, r)) for r in cur.fetchall()]
            for i in interactions:
                i["created_at"] = _iso(i["created_at"])

            return {"consent": consent, "analyses": analyses, "interactions": interactions, "locations": locations}
    except Exception as exc:
        log.warning(f"get_user_activity failed: {exc}")
        return None


# --------------------------------------------------------------------------
# Interactions (implicit signal)
# --------------------------------------------------------------------------
def log_interaction(oc_uid: str, recommendation_id, event_type: str) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "insert into interactions (oc_uid, recommendation_id, event_type) values (%s, %s, %s)",
                (oc_uid, recommendation_id, event_type),
            )
            return True
    except Exception as exc:
        log.warning(f"log_interaction failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Labeled examples (RAG knowledge base)
# --------------------------------------------------------------------------
def _vec_literal(embedding: list) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def insert_labeled_example(source, profile_summary, profile_json, event_context,
                            judgment, ideal_why, embedding, reviewed_by=None):
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                insert into labeled_examples
                  (source, profile_summary, profile_json, event_context, judgment, ideal_why, embedding, reviewed_by)
                values (%s, %s, %s, %s, %s, %s, %s::vector, %s)
                returning id
                """,
                (source, profile_summary, json.dumps(profile_json) if profile_json else None,
                 event_context, judgment, ideal_why, _vec_literal(embedding), reviewed_by),
            )
            return cur.fetchone()[0]
    except Exception as exc:
        log.warning(f"insert_labeled_example failed: {exc}")
        return None


def search_labeled_examples(embedding: list, k: int = 3, judgment: str = None):
    """Nearest neighbours by cosine distance. Optionally restrict to a
    judgment type (e.g. only 'good' examples for positive few-shot)."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            if judgment:
                cur.execute(
                    """
                    select profile_summary, event_context, judgment, ideal_why,
                           1 - (embedding <=> %s::vector) as similarity
                    from labeled_examples
                    where judgment = %s
                    order by embedding <=> %s::vector
                    limit %s
                    """,
                    (_vec_literal(embedding), judgment, _vec_literal(embedding), k),
                )
            else:
                cur.execute(
                    """
                    select profile_summary, event_context, judgment, ideal_why,
                           1 - (embedding <=> %s::vector) as similarity
                    from labeled_examples
                    order by embedding <=> %s::vector
                    limit %s
                    """,
                    (_vec_literal(embedding), _vec_literal(embedding), k),
                )
            cols = ["profile_summary", "event_context", "judgment", "ideal_why", "similarity"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning(f"search_labeled_examples failed: {exc}")
        return []


def list_labeled_examples(limit: int = 200):
    """Admin dashboard listing - newest first, embedding vector omitted
    (it's 1024 floats and useless to render)."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select id, source, profile_summary, event_context, judgment,
                       ideal_why, reviewed_by, created_at
                from labeled_examples
                order by created_at desc
                limit %s
                """,
                (limit,),
            )
            cols = ["id", "source", "profile_summary", "event_context", "judgment",
                    "ideal_why", "reviewed_by", "created_at"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning(f"list_labeled_examples failed: {exc}")
        return []


def delete_labeled_example(example_id: int) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("delete from labeled_examples where id = %s", (example_id,))
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"delete_labeled_example failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Admin config (key/value overrides, e.g. custom scoring rules)
# --------------------------------------------------------------------------
def get_config(key: str):
    """Returns the stored value for `key`, or None if unset/unconfigured.
    Cached for a minute (cleared on set/delete): read on every scoring call."""
    found, value = _cache_get(_config_cache, key, _TTL_CONFIG_S)
    if found:
        return value
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select value from ai_config where key = %s", (key,))
            row = cur.fetchone()
            value = row[0] if row else None
            _cache_put(_config_cache, key, value)
            return value
    except Exception as exc:
        log.warning(f"get_config failed: {exc}")
        return None


def get_config_meta(key: str):
    """Admin-only variant of get_config - also returns when it was last
    changed, so the Scoring Rules tab can show "custom, edited 3 days ago"
    instead of just "custom". get_config() itself stays a bare string
    return since ai_engine.get_scoring_rules() (the production read path)
    depends on that exact shape."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute("select value, updated_at from ai_config where key = %s", (key,))
            row = cur.fetchone()
            return {"value": row[0], "updated_at": _iso(row[1])} if row else None
    except Exception as exc:
        log.warning(f"get_config_meta failed: {exc}")
        return None


def set_config(key: str, value: str) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into ai_config (key, value, updated_at)
                values (%s, %s, now())
                on conflict (key) do update
                  set value = excluded.value, updated_at = now()
                """,
                (key, value),
            )
        _clear_config_cache()
        return True
    except Exception as exc:
        log.warning(f"set_config failed: {exc}")
        return False


def delete_config(key: str) -> bool:
    """Removes an override so the caller's hardcoded default takes over again."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("delete from ai_config where key = %s", (key,))
        _clear_config_cache()
        return True
    except Exception as exc:
        log.warning(f"delete_config failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Event catalog
# --------------------------------------------------------------------------
# The scraped catalog is cached to disk in events_cache.json, but Railway's
# filesystem is ephemeral: every deploy wipes it and the app falls back to
# whatever snapshot is committed to git. Mirroring the catalog here makes it
# survive deploys, so a cold boot serves the last real scrape instead of a
# hand-committed file that ages the moment it lands.
#
# Same graceful-degradation contract as the rest of this module: with no
# DATABASE_URL these return None/False and the caller falls back to the JSON
# file exactly as before.

def get_events_cache():
    """The stored catalog snapshot, or None if unavailable/never written."""
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select payload from events_cache where id = 1")
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            payload = row[0]
            # psycopg2 decodes jsonb to dict already; tolerate text columns too
            return json.loads(payload) if isinstance(payload, str) else payload
    except Exception as exc:
        log.warning(f"get_events_cache failed: {exc}")
        return None


# --------------------------------------------------------------------------
# Page-read caches: created at boot if missing
# --------------------------------------------------------------------------
# event_details and event_times are caches of what public event pages say,
# rebuilt from the pages whenever they are empty. Both migrations (0020,
# 0021) are additive "create table if not exists" with no data in them, so
# the app creates them itself at boot when they are missing: a deploy that
# runs ahead of the migration files otherwise silently re-reads every page
# after every restart, and nobody sees why. The migration files stay the
# record of the schema; this is the same DDL, idempotent, best-effort.

_CACHE_TABLES_DDL = """
create table if not exists event_details (
  id          text primary key,
  url         text not null,
  payload     jsonb not null,
  fetched_at  timestamptz not null default now()
);
alter table event_details enable row level security;
create table if not exists event_times (
  url         text primary key,
  found       boolean not null default false,
  start_time  text,
  end_time    text,
  payload     jsonb not null,
  fetched_at  timestamptz not null default now()
);
alter table event_times enable row level security;
"""


def ensure_cache_tables() -> bool:
    """Create the page-read cache tables if they do not exist. Safe to run
    on every boot; returns False (and logs) when there is no database or
    the role may not create tables, in which case the caches live in
    process memory as before."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(_CACHE_TABLES_DDL)
            return True
    except Exception as exc:
        log.warning(f"ensure_cache_tables skipped: {exc}")
        return False


# --------------------------------------------------------------------------
# Event details (event_details.py): what each event page says about itself
# --------------------------------------------------------------------------
# Missing table (migration 0020 not applied) or no database: reads return
# None and writes return False, and event_details keeps the record in memory.

def get_event_details(event_id: str):
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select payload from event_details where id = %s", (event_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception as exc:
        log.debug(f"get_event_details failed: {exc}")
        return None


def save_event_details(event_id: str, url: str, payload: dict) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into event_details (id, url, payload, fetched_at)
                values (%s, %s, %s, now())
                on conflict (id) do update
                  set url = excluded.url, payload = excluded.payload, fetched_at = now()
                """,
                (event_id, url, json.dumps(payload)),
            )
            return True
    except Exception as exc:
        log.debug(f"save_event_details failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Event times (event_times.py): the start/end time each event page states
# --------------------------------------------------------------------------
# One row per event URL. Missing table (migration 0021 not applied) or no
# database: reads return {} and writes return False, and event_times keeps
# its records in process memory, re-reading pages after a deploy.

def get_event_times(urls: list) -> dict:
    """{url: record} for the URLs that have a row."""
    if not urls:
        return {}
    try:
        with _read_cursor() as cur:
            if cur is None:
                return {}
            cur.execute("select url, payload from event_times where url = any(%s)", (list(urls),))
            out = {}
            for url, payload in cur.fetchall():
                rec = json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(rec, dict):
                    out[url] = rec
            return out
    except Exception as exc:
        log.debug(f"get_event_times failed: {exc}")
        return {}


def save_event_time(rec: dict) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into event_times (url, found, start_time, end_time, payload, fetched_at)
                values (%s, %s, %s, %s, %s, now())
                on conflict (url) do update
                  set found = excluded.found, start_time = excluded.start_time,
                      end_time = excluded.end_time, payload = excluded.payload,
                      fetched_at = now()
                """,
                (rec.get("url"), bool(rec.get("found")), rec.get("start"), rec.get("end"),
                 json.dumps(rec)),
            )
            return True
    except Exception as exc:
        log.debug(f"save_event_time failed: {exc}")
        return False


def save_events_cache(payload: dict) -> bool:
    """Replace the stored catalog in one atomic swap.

    Written as a single upsert so readers always see a complete catalog -
    either the previous one or the new one, never a half-applied rewrite."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into events_cache (id, payload, event_count, source_count, last_run)
                values (1, %s, %s, %s, now())
                on conflict (id) do update
                  set payload      = excluded.payload,
                      event_count  = excluded.event_count,
                      source_count = excluded.source_count,
                      last_run     = now()
                """,
                (json.dumps(payload),
                 len(payload.get("events") or []),
                 len(payload.get("summary") or [])),
            )
            return True
    except Exception as exc:
        log.warning(f"save_events_cache failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Source discovery
# --------------------------------------------------------------------------
# Leads harvested from ingested events - host calendars that clearly run
# London events but aren't tracked as sources yet. See
# scraper.harvest_luma_hosts() for where they come from and
# 0007_source_candidates.sql for why they are proposed rather than auto-added.

def record_source_candidates(candidates: list) -> int:
    """Upsert harvested candidates. Returns how many rows were written.

    A human's decision outlives the harvester: `status` is deliberately NOT in
    the update list, so a candidate marked 'ignored' stays ignored however
    many times it is re-harvested. Everything else refreshes, because a
    calendar can be renamed or move city between scrapes."""
    if not candidates:
        return 0
    written = 0
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            for c in candidates:
                if not c.get("identifier"):
                    continue
                cur.execute(
                    """
                    insert into source_candidates
                      (identifier, kind, name, url, city, timezone,
                       event_count, sample_title, discovered_via,
                       times_seen, first_seen, last_seen)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now(), now())
                    on conflict (identifier) do update
                      set name         = excluded.name,
                          url          = excluded.url,
                          city         = excluded.city,
                          timezone     = excluded.timezone,
                          event_count  = excluded.event_count,
                          sample_title = excluded.sample_title,
                          -- keep the FIRST route that found it: the point of
                          -- this column is which pass widened coverage, and
                          -- later passes re-find the same lead every scrape
                          discovered_via = coalesce(source_candidates.discovered_via,
                                                    excluded.discovered_via),
                          times_seen   = source_candidates.times_seen + 1,
                          last_seen    = now()
                    """,
                    (c["identifier"], c.get("kind", "luma_calendar"), c.get("name"),
                     c.get("url"), c.get("city"), c.get("timezone"),
                     int(c.get("event_count") or 0), c.get("sample_title"),
                     c.get("discovered_via")),
                )
                written += 1
            return written
    except Exception as exc:
        log.warning(f"record_source_candidates failed: {exc}")
        return written


def list_source_candidates(status: str = "new", limit: int = 100):
    """Harvested leads, busiest first. status=None returns every state."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            sql = """
                select identifier, kind, name, url, city, timezone,
                       event_count, times_seen, sample_title, status,
                       discovered_via, first_seen, last_seen
                  from source_candidates
            """
            params = []
            if status:
                sql += " where status = %s"
                params.append(status)
            sql += " order by event_count desc, times_seen desc limit %s"
            params.append(limit)
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                rec["first_seen"] = _iso(rec["first_seen"])
                rec["last_seen"] = _iso(rec["last_seen"])
                out.append(rec)
            return out
    except Exception as exc:
        log.warning(f"list_source_candidates failed: {exc}")
        return []


def set_source_candidate_status(identifier: str, status: str) -> bool:
    """Mark a lead as 'added' (now a real source) or 'ignored' (rejected)."""
    if status not in ("new", "added", "ignored"):
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "update source_candidates set status = %s where identifier = %s",
                (status, identifier),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"set_source_candidate_status failed: {exc}")
        return False


# ─────────────────────────────────────────────
# MANUAL EVENTS  (admin link portal)
# ─────────────────────────────────────────────
# Curated events used to live in CURATED_LONDON_EVENTS in scraper.py, so
# adding one was a code change and a deploy. These rows are the same thing
# held in the database, read back by the DB-backed curated sources on every
# scrape - which is what makes an added event survive the next refresh
# instead of vanishing when the catalog is rebuilt.


def upsert_manual_event(event: dict, verdict: dict = None, added_by: str = None) -> bool:
    """Add an operator-supplied event, or update it if the link is resubmitted.

    Keyed on url, so the same event submitted twice is one row rather than two
    near-identical catalog entries under slightly different titles. A row that
    was previously removed comes back as active on resubmission - re-adding a
    link is an explicit act."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into manual_events
                    (url, title, event_date, event_time, location, description,
                     category, emoji, is_online, source_label, verdict, added_by,
                     status, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', now())
                on conflict (url) do update set
                    title        = excluded.title,
                    event_date   = excluded.event_date,
                    event_time   = excluded.event_time,
                    location     = excluded.location,
                    description  = excluded.description,
                    category     = excluded.category,
                    emoji        = excluded.emoji,
                    is_online    = excluded.is_online,
                    source_label = excluded.source_label,
                    verdict      = excluded.verdict,
                    added_by     = excluded.added_by,
                    status       = 'active',
                    updated_at   = now()
                """,
                (event.get("url"), event.get("title"), event.get("date") or None,
                 event.get("time") or None, event.get("location") or None,
                 event.get("description") or None, event.get("category"),
                 event.get("emoji"), bool(event.get("is_online")),
                 event.get("source_label") or None,
                 json.dumps(verdict) if verdict else None, added_by),
            )
            return True
    except Exception as exc:
        log.warning(f"upsert_manual_event failed: {exc}")
        return False


def list_manual_events(include_removed: bool = False, limit: int = 300):
    """Every operator-added event, newest first, for the admin list."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            sql = """
                select url, title, event_date, event_time, location, description,
                       category, emoji, is_online, source_label, added_by, status,
                       created_at, updated_at
                  from manual_events
            """
            params = []
            if not include_removed:
                sql += " where status = 'active'"
            sql += " order by created_at desc limit %s"
            params.append(limit)
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                rec["created_at"] = _iso(rec["created_at"])
                rec["updated_at"] = _iso(rec["updated_at"])
                out.append(rec)
            return out
    except Exception as exc:
        log.warning(f"list_manual_events failed: {exc}")
        return []


def get_active_manual_events():
    """The scrape-time read. Deliberately thin and exception-safe: this runs
    inside a scraper source, and a database hiccup must cost the manual events
    only - never the whole scrape."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select url, title, event_date, event_time, location,
                       description, category, emoji, is_online, source_label
                  from manual_events
                 where status = 'active'
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning(f"get_active_manual_events failed: {exc}")
        return []


def set_manual_event_status(url: str, status: str) -> bool:
    """'removed' takes an event out of the catalog without losing the record of
    it having been added, and by whom."""
    if status not in ("active", "removed"):
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "update manual_events set status = %s, updated_at = now() where url = %s",
                (status, url),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"set_manual_event_status failed: {exc}")
        return False


# ─────────────────────────────────────────────
# LUMA SOURCES  (organisers approved from the dashboard)
# ─────────────────────────────────────────────
# LUMA_CALENDARS / LUMA_USERS in scraper.py are still the hardcoded set. These
# rows are the additive, no-deploy half: read at scrape time by the DB-backed
# Luma sources, so approving an organiser makes it feed on the next refresh.


def upsert_luma_source(identifier: str, kind: str, name: str, category: str,
                       emoji: str = None, verdict: dict = None, added_by: str = None) -> bool:
    """Track a Luma organiser as a recurring source.

    Keyed on identifier, so re-approving one that is already tracked updates it
    rather than creating a second feed for the same calendar."""
    if kind not in ("calendar", "user"):
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into luma_sources
                    (identifier, kind, name, emoji, category, verdict, added_by,
                     status, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, 'active', now())
                on conflict (identifier) do update set
                    kind       = excluded.kind,
                    name       = excluded.name,
                    emoji      = excluded.emoji,
                    category   = excluded.category,
                    verdict    = excluded.verdict,
                    added_by   = excluded.added_by,
                    status     = 'active',
                    updated_at = now()
                """,
                (identifier, kind, name, emoji, category,
                 json.dumps(verdict) if verdict else None, added_by),
            )
            return True
    except Exception as exc:
        log.warning(f"upsert_luma_source failed: {exc}")
        return False


def get_active_luma_sources(category: str = None):
    """The scrape-time read. Exception-safe: this runs inside a scraper source,
    and a database hiccup must cost these organisers only, never the scrape."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            sql = ("select identifier, kind, name, emoji, category "
                   "from luma_sources where status = 'active'")
            params = []
            if category:
                sql += " and category = %s"
                params.append(category)
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        log.warning(f"get_active_luma_sources failed: {exc}")
        return []


def get_luma_source_identifiers():
    """Every tracked organiser identifier, lowercased, regardless of status.

    The duplicate check deliberately ignores status. A PAUSED organiser is
    still tracked - re-adding it should reactivate the existing row, not read
    as "not tracked yet" and invite a second one under a different name.
    get_active_luma_sources() is the scrape-time read and is not that."""
    try:
        with _cursor() as cur:
            if cur is None:
                return set()
            cur.execute("select identifier from luma_sources")
            return {(r[0] or "").lower() for r in cur.fetchall() if r[0]}
    except Exception as exc:
        log.warning(f"get_luma_source_identifiers failed: {exc}")
        return set()


def list_luma_sources(limit: int = 500):
    """Every tracked organiser, for the admin list."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select identifier, kind, name, emoji, category, status,
                       verdict, added_by, created_at, updated_at
                  from luma_sources
                 order by created_at desc
                 limit %s
                """, (limit,))
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                rec["created_at"] = _iso(rec["created_at"])
                rec["updated_at"] = _iso(rec["updated_at"])
                out.append(rec)
            return out
    except Exception as exc:
        log.warning(f"list_luma_sources failed: {exc}")
        return []


def set_luma_source_status(identifier: str, status: str) -> bool:
    """'paused' stops an organiser being scraped without deleting the record of
    it - a quiet organiser is not a dead one, so removal is deliberately not
    the way to silence one."""
    if status not in ("active", "paused"):
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "update luma_sources set status = %s, updated_at = now() "
                "where identifier = %s", (status, identifier))
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"set_luma_source_status failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Accounts (Google Sign-In only)
# --------------------------------------------------------------------------
# An account OWNS an oc_uid rather than replacing it - see the design note at
# the top of supabase/migrations/0011_accounts_and_digests.sql. Everything in
# this file already keys on oc_uid, so signing up keeps the person's existing
# anonymous analyses instead of orphaning them.
#
# Magic-link email login was removed (migration 0014 drops auth_tokens). The
# only way in is a verified Google identity.

import uuid as _uuid

# Who may CREATE an account. Both empty = anyone with a verified Google
# address. Set either in Railway to run invite-only: a comma-separated list of
# full addresses, and/or of domains ("reichman.ac.il,gre.ac.uk"). Existing
# accounts are never affected - this gates creation, not login.
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
ALLOWED_EMAIL_DOMAINS = {d.strip().lower().lstrip("@") for d in os.environ.get("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()}


def signup_allowed(email: str) -> bool:
    """True when this address may create a NEW account under the allow-list.
    With no allow-list configured, everyone may."""
    email = (email or "").strip().lower()
    if not ALLOWED_EMAILS and not ALLOWED_EMAIL_DOMAINS:
        return True
    if email in ALLOWED_EMAILS:
        return True
    domain = email.rpartition("@")[2]
    return bool(domain) and domain in ALLOWED_EMAIL_DOMAINS


def signup_gated() -> bool:
    return bool(ALLOWED_EMAILS or ALLOWED_EMAIL_DOMAINS)


def get_or_create_account_by_google(google_sub: str, email: str, name: str,
                                    avatar_url: str, oc_uid: str,
                                    email_verified: bool = False):
    """Resolves a Google identity to an account. Returns the account dict,
    {"refused": "..."} when the allow-list or verification refuses a new
    signup, or None if the database is unavailable.

    Three cases, in this order, and the order is the security-relevant part:

    1. google_sub already known - the normal returning login. Refresh the
       profile fields and return the STORED oc_uid, which the caller
       re-issues as the cookie so history follows them onto this device.

    2. google_sub unknown but the email matches an existing account (created
       before Google was the only door, or whose Google sub changed). Link the
       two. Only when Google says the address is verified - otherwise anyone
       able to mint a Google identity asserting an address could claim the
       account for it.

    3. Neither matches - a genuinely new person. Requires a verified address
       and, if an allow-list is configured, a listed one.

       The account adopts the browser's anonymous oc_uid ONLY IF NO ACCOUNT
       OWNS IT. The previous version did
       `on conflict (oc_uid) do update set email = ..., google_sub = ...`,
       which meant that signing in with a never-seen Google account while
       holding somebody else's oc_uid cookie (a shared laptop where they had
       not signed out, or a stolen cookie) OVERWROTE their account row with
       the new identity: their profile, CV files, history, saved events and
       calendar feed changed hands and they were locked out. Now an owned
       oc_uid is never adopted - the new account gets a fresh id, and the
       other account is untouched.
    """
    google_sub = (google_sub or "").strip()
    email = (email or "").strip().lower()
    if not google_sub or not email or not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None

            # 1. known Google identity
            cur.execute(
                "select id, email, oc_uid from accounts where google_sub = %s",
                (google_sub,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    update accounts
                       set last_login_at = now(),
                           email      = %s,
                           name       = coalesce(%s, name),
                           avatar_url = coalesce(%s, avatar_url)
                     where id = %s
                    """,
                    (email, name or None, avatar_url or None, row[0]),
                )
                return {"id": row[0], "email": email, "oc_uid": row[2],
                        "name": name, "avatar_url": avatar_url, "is_new": False}

            # Everything below creates or links an account: a verified address
            # is non-negotiable for both.
            if not email_verified:
                log.warning("Google identity with unverified email refused.")
                return {"refused": "email_not_verified"}

            # 2. existing account for this verified address
            cur.execute(
                "select id, oc_uid from accounts where lower(email) = %s",
                (email,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    update accounts
                       set google_sub    = %s,
                           name          = coalesce(%s, name),
                           avatar_url    = coalesce(%s, avatar_url),
                           last_login_at = now()
                     where id = %s
                    """,
                    (google_sub, name or None, avatar_url or None, row[0]),
                )
                return {"id": row[0], "email": email, "oc_uid": row[1],
                        "name": name, "avatar_url": avatar_url, "is_new": False}

            # 3. new account
            if not signup_allowed(email):
                log.warning("Signup refused by allow-list.")
                return {"refused": "not_invited"}

            # Never adopt an oc_uid that already belongs to an account.
            cur.execute("select 1 from accounts where oc_uid = %s", (oc_uid,))
            if cur.fetchone():
                oc_uid = _uuid.uuid4().hex

            cur.execute(
                """
                insert into accounts (email, oc_uid, google_sub, name, avatar_url, last_login_at)
                values (%s, %s, %s, %s, %s, now())
                on conflict (oc_uid) do nothing
                returning id, email, oc_uid
                """,
                (email, oc_uid, google_sub, name or None, avatar_url or None),
            )
            new = cur.fetchone()
            if not new:
                # Lost a race for that oc_uid between the check and the insert.
                # Retry once with an id nobody can already hold.
                cur.execute(
                    """
                    insert into accounts (email, oc_uid, google_sub, name, avatar_url, last_login_at)
                    values (%s, %s, %s, %s, %s, now())
                    returning id, email, oc_uid
                    """,
                    (email, _uuid.uuid4().hex, google_sub, name or None, avatar_url or None),
                )
                new = cur.fetchone()
            return {"id": new[0], "email": new[1], "oc_uid": new[2],
                    "name": name, "avatar_url": avatar_url, "is_new": True}
    except Exception as exc:
        log.warning(f"get_or_create_account_by_google failed: {exc}")
        return None


# ─────────────────────────────────────────────
# Email sign-in tokens (hashed, single-use, 15 min)
# ─────────────────────────────────────────────
# Second door into the same accounts table, built on auth_tokens (0017).
# Token plaintext leaves this process exactly once - in the mail body - and
# is never stored. Everything in the database is the SHA-256 hex of it,
# which is useless on its own.

EMAIL_SIGNIN_TTL_S = 15 * 60


def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def recent_email_signin_requests(email: str, within_minutes: int = 15) -> int:
    """How many signin tokens have been issued to this address in the last
    `within_minutes`. A per-address cap on top of the per-IP Flask-Limiter
    bucket: a burst from varying IPs still cannot flood one person's inbox."""
    email = (email or "").strip().lower()
    if not email:
        return 0
    try:
        with _read_cursor() as cur:
            if cur is None:
                return 0
            cur.execute(
                "select count(*) from auth_tokens "
                "where email = %s and purpose = 'signin' "
                "and created_at > now() - make_interval(mins => %s)",
                (email, int(within_minutes)),
            )
            return int(cur.fetchone()[0])
    except Exception as exc:
        log.warning(f"recent_email_signin_requests failed: {exc}")
        return 0


def create_email_signin_token(email: str, oc_uid_hint: str,
                              requested_ip: str = None,
                              requested_ua: str = None) -> str:
    """Mints a signin token for this address and returns the PLAINTEXT.
    Only the hash is stored. Returns None if the database is unavailable.

    The caller is responsible for rate-limiting - this function will mint
    whatever it's asked to."""
    import secrets
    email = (email or "").strip().lower()
    if not email:
        return None
    plain = secrets.token_urlsafe(32)         # 256 bits of entropy
    token_hash = _hash_token(plain)
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                insert into auth_tokens
                  (email, token_hash, purpose, expires_at,
                   requested_ip, requested_ua, oc_uid_hint)
                values (%s, %s, 'signin',
                        now() + make_interval(secs => %s),
                        %s, %s, %s)
                """,
                (email, token_hash, EMAIL_SIGNIN_TTL_S,
                 requested_ip or None,
                 (requested_ua or None) and requested_ua[:400],
                 oc_uid_hint or None),
            )
        return plain
    except Exception as exc:
        log.warning(f"create_email_signin_token failed: {exc}")
        return None


def consume_email_signin_token(plain_token: str):
    """Marks a valid, unexpired, unused token as used and returns
    {email, oc_uid_hint}. Returns None for ANY failure: expired, missing,
    already used, wrong purpose, or database down. The caller cannot tell
    them apart - same philosophy as a signed cookie that fails to verify.

    The UPDATE ... RETURNING is atomic, so a token can be consumed exactly
    once even if the user double-clicks the mail link or a scanner races
    them to it."""
    if not plain_token or len(plain_token) < 32 or len(plain_token) > 128:
        return None
    token_hash = _hash_token(plain_token)
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                update auth_tokens
                   set used_at = now()
                 where token_hash = %s
                   and purpose = 'signin'
                   and used_at is null
                   and expires_at > now()
                returning email, oc_uid_hint
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"email": row[0], "oc_uid_hint": row[1]}
    except Exception as exc:
        log.warning(f"consume_email_signin_token failed: {exc}")
        return None


def cleanup_expired_email_signin_tokens(older_than_hours: int = 24) -> int:
    """Housekeeping. Not required for correctness - expires_at already
    prevents reuse - but keeps the table from growing. Any scheduled sweep
    can call this; nothing currently must."""
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            cur.execute(
                "delete from auth_tokens "
                "where expires_at < now() - make_interval(hours => %s)",
                (int(older_than_hours),),
            )
            return cur.rowcount or 0
    except Exception as exc:
        log.warning(f"cleanup_expired_email_signin_tokens failed: {exc}")
        return 0


def get_or_create_account_by_email(email: str, oc_uid: str):
    """Mirror of get_or_create_account_by_google for the email-link path.
    Called ONLY after consume_email_signin_token has succeeded, which is
    proof the caller controls the mailbox.

    Three cases, mirroring the Google version:

      1. Account already exists for this address - log the person in,
         return the stored oc_uid. Nothing else about the account changes.
         A Google-linked account signs in by email just as naturally as by
         Google: the two doors converge on the same row.

      2. New account - signup must be allowed by the configured allow-list,
         the same gate the Google path uses. If it refuses, we say so
         explicitly rather than silently dropping the login.

      3. Database unavailable - None.

    The account-takeover rule from get_or_create_account_by_google carries
    unchanged: a new account NEVER adopts an oc_uid that another account
    already owns. The verify endpoint hands us oc_uid_hint from the token
    row - the browser that REQUESTED the link - and we only adopt it when
    nobody else holds it."""
    email = (email or "").strip().lower()
    if not email or not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None

            # 1. existing account for this address
            cur.execute(
                "select id, email, oc_uid, name, avatar_url "
                "from accounts where lower(email) = %s",
                (email,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "update accounts set last_login_at = now() where id = %s",
                    (row[0],),
                )
                return {"id": row[0], "email": row[1], "oc_uid": row[2],
                        "name": row[3], "avatar_url": row[4],
                        "is_new": False}

            # 2. new account
            if not signup_allowed(email):
                log.warning("Email signup refused by allow-list.")
                return {"refused": "not_invited"}

            cur.execute("select 1 from accounts where oc_uid = %s", (oc_uid,))
            if cur.fetchone():
                oc_uid = _uuid.uuid4().hex

            cur.execute(
                """
                insert into accounts (email, oc_uid, last_login_at)
                values (%s, %s, now())
                on conflict (oc_uid) do nothing
                returning id, email, oc_uid
                """,
                (email, oc_uid),
            )
            new = cur.fetchone()
            if not new:
                # Lost a race for that oc_uid; retry with a fresh one.
                cur.execute(
                    """
                    insert into accounts (email, oc_uid, last_login_at)
                    values (%s, %s, now())
                    returning id, email, oc_uid
                    """,
                    (email, _uuid.uuid4().hex),
                )
                new = cur.fetchone()
            return {"id": new[0], "email": new[1], "oc_uid": new[2],
                    "name": None, "avatar_url": None, "is_new": True}
    except Exception as exc:
        log.warning(f"get_or_create_account_by_email failed: {exc}")
        return None


# ─────────────────────────────────────────────
# Home postcode - durable per-account alternative to HTML5 geolocation
# ─────────────────────────────────────────────
# Stored on accounts (0019). The geocode is done by the caller before this
# function sees anything - a bad coordinate is worse than none, so a failed
# geocode never writes.

def set_postcode(account_id, postcode: str, lat: float, lng: float) -> bool:
    """Replaces (or sets) this account's home postcode and its lat/lng.
    Returns True on success. All three fields move together: a half-written
    row (postcode text present, lat/lng missing) is the one state the
    downstream code can't degrade from gracefully."""
    if account_id is None:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                update accounts
                   set postcode        = %s,
                       postcode_lat    = %s,
                       postcode_lng    = %s,
                       postcode_set_at = now()
                 where id = %s
                """,
                (postcode, float(lat), float(lng), account_id),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"set_postcode failed: {exc}")
        return False


def clear_postcode(account_id) -> bool:
    if account_id is None:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "update accounts set postcode = null, postcode_lat = null, "
                "postcode_lng = null, postcode_set_at = null where id = %s",
                (account_id,),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"clear_postcode failed: {exc}")
        return False


def get_postcode(account_id):
    """Returns {"postcode", "lat", "lng", "set_at"} or None when the account
    hasn't set one. Reads from the pooled read-cursor, no caching here -
    only the analysis path reads this and it's one query per analysis."""
    if account_id is None:
        return None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "select postcode, postcode_lat, postcode_lng, postcode_set_at "
                "from accounts where id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            if not row or not row[0] or row[1] is None or row[2] is None:
                return None
            return {"postcode": row[0], "lat": float(row[1]), "lng": float(row[2]),
                    "set_at": _iso(row[3])}
    except Exception as exc:
        log.warning(f"get_postcode failed: {exc}")
        return None


def account_exists_for_oc_uid(oc_uid: str) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("select 1 from accounts where oc_uid = %s", (oc_uid,))
            return cur.fetchone() is not None
    except Exception as exc:
        log.warning(f"account_exists_for_oc_uid failed: {exc}")
        return False


def recommendation_belongs_to(recommendation_id, oc_uid: str) -> bool:
    """True when this recommendation came out of one of oc_uid's own analyses."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                select 1 from recommendations r
                join analyses a on a.id = r.analysis_id
                where r.id = %s and a.oc_uid = %s
                """,
                (recommendation_id, oc_uid),
            )
            return cur.fetchone() is not None
    except Exception as exc:
        log.warning(f"recommendation_belongs_to failed: {exc}")
        return False


def get_account(account_id):
    """Cached for a minute per id; every function here that changes or deletes
    an account clears the cache."""
    found, acct = _cache_get(_account_cache, account_id, _TTL_ACCOUNT_S)
    if found:
        return dict(acct) if acct else None
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "select id, email, oc_uid, created_at, name, avatar_url "
                "from accounts where id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            acct = None if not row else {
                "id": row[0], "email": row[1], "oc_uid": row[2], "created_at": _iso(row[3]),
                "name": row[4], "avatar_url": row[5]}
        _cache_put(_account_cache, account_id, acct)
        return dict(acct) if acct else None
    except Exception as exc:
        log.warning(f"get_account failed: {exc}")
        return None


def get_cv_identity(account_id):
    """The name this account's analyses are locked to, or None."""
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute("select cv_identity from accounts where id = %s", (account_id,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"get_cv_identity failed: {exc}")
        return None


def set_cv_identity_if_absent(account_id, name: str):
    """First named document wins. Atomic: if two uploads race, both get back
    whichever name actually landed, and the gate judges against that. Returns
    the locked name, or None when the database is unavailable (the gate then
    judges against the submitted name alone for this one run)."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                update accounts
                   set cv_identity = %s, cv_identity_set_at = now()
                 where id = %s and cv_identity is null
                """,
                ((name or "").strip()[:120], account_id),
            )
        with _read_cursor() as cur:
            cur.execute("select cv_identity from accounts where id = %s", (account_id,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"set_cv_identity_if_absent failed: {exc}")
        return None


def delete_account(oc_uid: str) -> bool:
    """Part of the GDPR delete. Cascades to digest_prefs and digest_sends."""
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("delete from accounts where oc_uid = %s", (oc_uid,))
        _clear_account_cache()
        return True
    except Exception as exc:
        log.warning(f"delete_account failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Digest preferences + send log
# --------------------------------------------------------------------------
_DIGEST_COLS = ("account_id, frequency, interval_days, weekday, send_hour, "
                "timezone, paused, next_send_at, unsub_token")


def _digest_row(row):
    if not row:
        return None
    return {"account_id": row[0], "frequency": row[1], "interval_days": row[2],
            "weekday": row[3], "send_hour": row[4], "timezone": row[5],
            "paused": row[6], "next_send_at": row[7], "unsub_token": row[8]}


def get_digest_prefs(account_id):
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(f"select {_DIGEST_COLS} from digest_prefs where account_id = %s",
                        (account_id,))
            return _digest_row(cur.fetchone())
    except Exception as exc:
        log.warning(f"get_digest_prefs failed: {exc}")
        return None


def upsert_digest_prefs(account_id, frequency, interval_days, weekday, send_hour,
                         tz, paused, next_send_at, unsub_token):
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                f"""
                insert into digest_prefs (account_id, frequency, interval_days, weekday,
                                          send_hour, timezone, paused, next_send_at, unsub_token)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (account_id) do update set
                    frequency = excluded.frequency,
                    interval_days = excluded.interval_days,
                    weekday = excluded.weekday,
                    send_hour = excluded.send_hour,
                    timezone = excluded.timezone,
                    paused = excluded.paused,
                    next_send_at = excluded.next_send_at,
                    updated_at = now()
                returning {_DIGEST_COLS}
                """,
                (account_id, frequency, interval_days, weekday, send_hour, tz,
                 paused, next_send_at, unsub_token),
            )
            return _digest_row(cur.fetchone())
    except Exception as exc:
        log.warning(f"upsert_digest_prefs failed: {exc}")
        return None


def set_next_send_at(account_id, next_send_at) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("update digest_prefs set next_send_at = %s where account_id = %s",
                        (next_send_at, account_id))
            return True
    except Exception as exc:
        log.warning(f"set_next_send_at failed: {exc}")
        return False


def due_digests(limit: int = 200):
    """Subscriptions whose send time has passed. Joined to the account so the
    scheduler has the email and oc_uid without a second round trip."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                f"""
                select {_DIGEST_COLS}, a.email, a.oc_uid
                from digest_prefs d join accounts a on a.id = d.account_id
                where d.paused = false
                  and d.next_send_at is not null
                  and d.next_send_at <= now()
                order by d.next_send_at asc
                limit %s
                """,
                (limit,),
            )
            out = []
            for row in cur.fetchall():
                prefs = _digest_row(row[:9])
                prefs["email"] = row[9]
                prefs["oc_uid"] = row[10]
                out.append(prefs)
            return out
    except Exception as exc:
        log.warning(f"due_digests failed: {exc}")
        return []


def pause_digest_by_account(account_id):
    """One-click unsubscribe for an HMAC-derived token (auth.unsub_token).
    Returns the email it paused, or None."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                update digest_prefs d set paused = true, updated_at = now()
                from accounts a
                where a.id = d.account_id and d.account_id = %s
                returning a.email
                """,
                (account_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"pause_digest_by_account failed: {exc}")
        return None


def pause_digest_by_token(unsub_token: str):
    """Legacy path for links in emails sent before tokens were HMAC-derived.
    The stored column is a random id with no other power than this."""
    if not unsub_token or len(unsub_token) != 32:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                update digest_prefs d set paused = true, updated_at = now()
                from accounts a
                where a.id = d.account_id and d.unsub_token = %s
                returning a.email
                """,
                (unsub_token,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"pause_digest_by_token failed: {exc}")
        return None


def claim_digest_period(account_id, period_key: str) -> bool:
    """Reserves this account's slot for this period BEFORE the email is sent.

    The unique index on (account_id, period_key) means the second caller loses
    - which is exactly what stops a restart mid-run, or two workers, from
    emailing the same person twice. Sending first and recording after would
    invert that guarantee into a double-send."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into digest_sends (account_id, period_key, status)
                values (%s, %s, 'claimed')
                on conflict (account_id, period_key) do nothing
                returning id
                """,
                (account_id, period_key),
            )
            return cur.fetchone() is not None
    except Exception as exc:
        log.warning(f"claim_digest_period failed: {exc}")
        return False


def finish_digest_send(account_id, period_key: str, status: str,
                        event_ids: list = None, match_count: int = 0) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                update digest_sends
                set status = %s, event_ids = %s, match_count = %s, sent_at = now()
                where account_id = %s and period_key = %s
                """,
                (status, list(event_ids or []), match_count, account_id, period_key),
            )
            return True
    except Exception as exc:
        log.warning(f"finish_digest_send failed: {exc}")
        return False


def recently_sent_event_ids(account_id, days: int = 45):
    """Event ids already emailed to this account. The digest excludes these,
    which is what stops every send being a re-run of the last one."""
    try:
        with _cursor() as cur:
            if cur is None:
                return set()
            cur.execute(
                """
                select event_ids from digest_sends
                where account_id = %s and sent_at > now() - (%s || ' days')::interval
                """,
                (account_id, int(days)),
            )
            out = set()
            for (ids,) in cur.fetchall():
                out.update(ids or [])
            return out
    except Exception as exc:
        log.warning(f"recently_sent_event_ids failed: {exc}")
        return set()


def last_empty_digest_at(account_id):
    """When this account was last told 'nothing worth your time'. Used to stop
    a daily subscriber getting an empty email every single day."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                select sent_at from digest_sends
                where account_id = %s and status = 'sent_empty'
                order by sent_at desc limit 1
                """,
                (account_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"last_empty_digest_at failed: {exc}")
        return None


# --------------------------------------------------------------------------
# Account panel: saved events, dismissed events, digest extras, history,
# calendar feed, export
# --------------------------------------------------------------------------
# Every function below reads or writes something added by migration 0013.
# Each one swallows its own failure and returns an empty value, so the app
# behaves exactly as it did before if the migration has not been applied
# yet - the panel simply shows less.

_SAVED_COLS = ("event_id, title, url, category, event_date, event_time, "
               "location, is_online, saved_at")


def _saved_row(row):
    return {"id": row[0], "title": row[1], "url": row[2], "category": row[3],
            "date": row[4], "time": row[5], "location": row[6],
            "is_online": row[7], "saved_at": _iso(row[8])}


def list_saved_events(account_id):
    """None means 'could not read' (no database, or 0013 not applied) - the
    caller keeps the browser's own list rather than treating it as empty."""
    try:
        with _read_cursor() as cur:
            if cur is None:
                return None
            cur.execute(f"select {_SAVED_COLS} from saved_events "
                        "where account_id = %s order by saved_at desc", (account_id,))
            return [_saved_row(r) for r in cur.fetchall()]
    except Exception as exc:
        log.warning(f"list_saved_events failed: {exc}")
        return None


def upsert_saved_events(account_id, events: list) -> int:
    """Saves a snapshot of each event. Re-saving refreshes the snapshot (a
    changed time or venue) but keeps the original saved_at."""
    if not events:
        return 0
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            n = 0
            for e in events:
                cur.execute(
                    """
                    insert into saved_events (account_id, event_id, title, url, category,
                                              event_date, event_time, location, is_online)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (account_id, event_id) do update set
                        title = excluded.title, url = excluded.url,
                        category = excluded.category, event_date = excluded.event_date,
                        event_time = excluded.event_time, location = excluded.location,
                        is_online = excluded.is_online
                    """,
                    (account_id, e["id"], e["title"], e.get("url"), e.get("category"),
                     e.get("date"), e.get("time"), e.get("location"), e.get("is_online")),
                )
                n += 1
            return n
    except Exception as exc:
        log.warning(f"upsert_saved_events failed: {exc}")
        return 0


def delete_saved_event(account_id, event_id: str) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("delete from saved_events where account_id = %s and event_id = %s",
                        (account_id, event_id))
            return True
    except Exception as exc:
        log.warning(f"delete_saved_event failed: {exc}")
        return False


def list_dismissed(account_id):
    try:
        with _read_cursor() as cur:
            if cur is None:
                return []
            cur.execute("select event_id, title, created_at from dismissed_events "
                        "where account_id = %s order by created_at desc", (account_id,))
            return [{"id": r[0], "title": r[1], "at": _iso(r[2])} for r in cur.fetchall()]
    except Exception as exc:
        log.warning(f"list_dismissed failed: {exc}")
        return []


def set_dismissed(account_id, event_id: str, title: str, dismissed: bool) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            if dismissed:
                cur.execute(
                    "insert into dismissed_events (account_id, event_id, title) values (%s, %s, %s) "
                    "on conflict (account_id, event_id) do nothing",
                    (account_id, event_id, title))
            else:
                cur.execute("delete from dismissed_events where account_id = %s and event_id = %s",
                            (account_id, event_id))
            return True
    except Exception as exc:
        log.warning(f"set_dismissed failed: {exc}")
        return False


def get_digest_extras(account_id) -> dict:
    """categories + snooze_until. Read separately from get_digest_prefs() on
    purpose: that query is on the scheduler's critical path and must keep
    working on a database where 0013 has not been applied."""
    try:
        with _read_cursor() as cur:
            if cur is None:
                return {}
            cur.execute("select categories, snooze_until from digest_prefs where account_id = %s",
                        (account_id,))
            row = cur.fetchone()
            if not row:
                return {}
            return {"categories": list(row[0] or []), "snooze_until": row[1]}
    except Exception as exc:
        log.warning(f"get_digest_extras failed: {exc}")
        return {}


def set_digest_extras(account_id, categories, snooze_until) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("update digest_prefs set categories = %s, snooze_until = %s, "
                        "updated_at = now() where account_id = %s",
                        (list(categories) if categories else None, snooze_until, account_id))
            return True
    except Exception as exc:
        log.warning(f"set_digest_extras failed: {exc}")
        return False


def recent_digest_sends(account_id, limit: int = 6):
    """What was actually sent, most recent first. 'claimed' rows are sends
    in flight and are left out."""
    try:
        with _read_cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select sent_at, status, match_count from digest_sends
                where account_id = %s and status <> 'claimed'
                order by sent_at desc limit %s
                """,
                (account_id, int(limit)),
            )
            return [{"at": _iso(r[0]), "status": r[1], "matches": r[2]} for r in cur.fetchall()]
    except Exception as exc:
        log.warning(f"recent_digest_sends failed: {exc}")
        return []


def get_history(oc_uid: str, limit: int = 25):
    """The caller's own past analyses, newest first, each with its matches.
    cv_text is deliberately not selected: the panel never needs it, and not
    reading it is cheaper and safer than reading it and dropping it."""
    if not oc_uid:
        return []
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            cur.execute(
                """
                select a.id, a.created_at, a.is_cv, a.profile, a.evidence_level, a.field,
                       (a.cv_text = '[reused previous profile]') as reused,
                       (select rf.filename from raw_files rf where rf.analysis_id = a.id
                        order by rf.created_at desc limit 1) as filename
                from analyses a
                where a.oc_uid = %s
                order by a.created_at desc limit %s
                """,
                (oc_uid, int(limit)),
            )
            runs = []
            for r in cur.fetchall():
                profile = r[3]
                if isinstance(profile, str):
                    try:
                        profile = json.loads(profile)
                    except Exception:
                        profile = None
                runs.append({"id": r[0], "at": _iso(r[1]), "is_cv": r[2], "profile": profile,
                             "evidence_level": r[4], "field": r[5], "reused": bool(r[6]),
                             "filename": r[7], "matches": []})
            ids = [a["id"] for a in runs]
            if ids:
                cur.execute(
                    """
                    select id, analysis_id, event_id, title, category, fit_score,
                           why, why_now, prepare, benefit, weakness
                    from recommendations where analysis_id = any(%s)
                    order by fit_score desc nulls last
                    """,
                    (ids,),
                )
                by_id = {a["id"]: a for a in runs}
                try:
                    import ai_engine
                    _band = ai_engine.band_label
                except Exception:
                    _band = lambda s: None   # noqa: E731
                for r in cur.fetchall():
                    by_id[r[1]]["matches"].append(
                        {"recommendation_id": r[0], "event_id": r[2], "title": r[3],
                         "category": r[4], "fit_score": r[5], "band": _band(r[5]),
                         "why": r[6], "why_now": r[7], "prepare": r[8],
                         "benefit": r[9], "weakness": r[10]})
            return runs
    except Exception as exc:
        log.warning(f"get_history failed: {exc}")
        return []


def clear_history(oc_uid: str):
    """Clears the match history without touching the profile or the stored CV.

    Three things hang off an analysis row - its matches, the profile the
    digest scores against, and (by cascade) the uploaded CV file - and only
    the first is "history". So: every match goes; a run is deleted outright
    only if it is neither the newest profile nor the owner of a stored file.
    Deleting the rows wholesale would silently take the CV and stop the
    person's email, which is what 'Delete my data' is for, said out loud.
    Returns the number of matches removed, or None on failure."""
    if not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "delete from recommendations where analysis_id in "
                "(select id from analyses where oc_uid = %s)", (oc_uid,))
            removed = cur.rowcount
            cur.execute(
                """
                delete from analyses a
                where a.oc_uid = %s
                  and a.id is distinct from (
                    select id from analyses where oc_uid = %s and is_cv = true
                    order by created_at desc limit 1)
                  and not exists (select 1 from raw_files rf where rf.analysis_id = a.id)
                """,
                (oc_uid, oc_uid),
            )
            return removed
    except Exception as exc:
        log.warning(f"clear_history failed: {exc}")
        return None


def get_account_meta(account_id) -> dict:
    """How this account signs in, and whether it has a calendar feed. The
    feed token itself is never returned: only its hash is stored (migration
    0014), so the URL can be shown exactly once, when it is created or
    rotated - the same model as an API key."""
    out = {"google_linked": False, "last_login_at": None, "cal_feed_active": False}
    try:
        with _read_cursor() as cur:
            if cur is None:
                return out
            cur.execute("select google_sub is not null, last_login_at, "
                        "cal_token_hash is not null from accounts where id = %s",
                        (account_id,))
            row = cur.fetchone()
            if row:
                out["google_linked"], out["last_login_at"] = bool(row[0]), _iso(row[1])
                out["cal_feed_active"] = bool(row[2])
    except Exception as exc:
        log.warning(f"get_account_meta failed: {exc}")
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def set_cal_token(account_id, token) -> bool:
    """Stores the SHA-256 of the feed token (or clears it). A database dump
    therefore does not hand anyone a working feed URL."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("update accounts set cal_token_hash = %s, cal_token = null where id = %s",
                        (_sha256(token) if token else None, account_id))
            return cur.rowcount == 1
    except Exception as exc:
        log.warning(f"set_cal_token failed: {exc}")
        return False


def account_id_by_cal_token(token: str):
    if not token or len(token) < 32 or len(token) > 128:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute("select id from accounts where cal_token_hash = %s", (_sha256(token),))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"account_id_by_cal_token failed: {exc}")
        return None


# --------------------------------------------------------------------------
# Audit log (migration 0014)
# --------------------------------------------------------------------------
# Audit rows are written by one background thread, not inside the request:
# each insert is a full transaction (three round trips to a database ~100 ms
# away), and under load the request was waiting on it for no benefit to the
# person. Bounded queue; if it ever fills, rows are written inline instead of
# being dropped.
_audit_q: "queue.Queue" = queue.Queue(maxsize=2000)
_audit_thread = None
_audit_thread_lock = threading.Lock()


def _audit_writer():
    while True:
        args = _audit_q.get()
        try:
            _audit_write(*args)
        finally:
            _audit_q.task_done()


def audit(account_id, action: str, resource: str = None, detail: dict = None,
          ip: str = None, user_agent: str = None) -> bool:
    """Records who did what to sensitive data. Never raises; a failed audit
    write is logged but must not block the action it describes."""
    global _audit_thread
    if not DATABASE_URL:
        return False
    if _audit_thread is None:
        with _audit_thread_lock:
            if _audit_thread is None:
                _audit_thread = threading.Thread(target=_audit_writer, daemon=True,
                                                 name="audit-writer")
                _audit_thread.start()
    try:
        _audit_q.put_nowait((account_id, action, resource, detail, ip, user_agent))
        return True
    except queue.Full:
        return _audit_write(account_id, action, resource, detail, ip, user_agent)


def flush_audit(timeout: float = 5.0):
    """Wait (briefly) for queued audit rows to be written - used at shutdown."""
    end = time.monotonic() + timeout
    while _audit_q.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.05)


def _audit_write(account_id, action, resource, detail, ip, user_agent) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into audit_log (account_id, action, resource, detail, ip_address, user_agent)
                values (%s, %s, %s, %s::jsonb, %s, %s)
                """,
                (account_id, (action or "")[:64], (resource or None),
                 json.dumps(detail or {}, default=str)[:4000],
                 (ip or None), (user_agent or "")[:300] or None),
            )
            return True
    except Exception as exc:
        log.warning(f"audit failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Source health  (migration 0015_admin_operability)
# --------------------------------------------------------------------------
# The coverage view used to render a per-source count and nothing else, and
# its own hint admitted the problem: "a source on 0 is either dormant or
# broken; the two look identical from here." They are not identical any more.
# Every scrape writes one row per source with what it returned, when it last
# returned anything, and the error it raised if it crashed.

def record_source_health(rows: list) -> int:
    """Upsert one row per source from a finished scrape.

    last_success_at only moves when a source actually returned something, so
    it answers "when did this last work" rather than "when did we last try".
    consecutive_zero is the streak length, which is what turns one quiet run
    into a visible problem after a few of them."""
    if not rows:
        return 0
    written = 0
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            for r in rows:
                name = (r.get("source") or "").strip()
                if not name:
                    continue
                count = int(r.get("count") or 0)
                cur.execute(
                    """
                    insert into source_health
                      (source, kind, emoji, category, last_count, consecutive_zero,
                       last_run, last_success_at, last_error, updated_at)
                    values (%s, %s, %s, %s, %s, %s, now(), %s, %s, now())
                    on conflict (source) do update set
                      kind             = excluded.kind,
                      emoji            = coalesce(excluded.emoji, source_health.emoji),
                      category         = coalesce(excluded.category, source_health.category),
                      last_count       = excluded.last_count,
                      consecutive_zero = case when excluded.last_count > 0 then 0
                                              else source_health.consecutive_zero + 1 end,
                      last_run         = now(),
                      last_success_at  = case when excluded.last_count > 0 then now()
                                              else source_health.last_success_at end,
                      last_error       = excluded.last_error,
                      updated_at       = now()
                    """,
                    (name, r.get("kind") or "builtin", r.get("emoji"), r.get("category"),
                     count, 0 if count else 1,
                     _now_if(count), (r.get("error") or None)),
                )
                written += 1
            return written
    except Exception as exc:
        log.warning(f"record_source_health failed: {exc}")
        return written


def _now_if(count):
    """last_success_at on INSERT: a first run that found nothing has never
    succeeded, and must not be stamped as if it had."""
    from datetime import datetime, timezone as _tz
    return datetime.now(_tz.utc) if count else None


def list_source_health():
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            rows = _rows(cur, """
                select source, kind, emoji, category, last_count, consecutive_zero,
                       last_run, last_success_at, last_error, paused
                  from source_health
            """)
            for r in rows:
                r["last_run"] = _iso(r["last_run"])
                r["last_success_at"] = _iso(r["last_success_at"])
            return rows
    except Exception as exc:
        log.warning(f"list_source_health failed: {exc}")
        return []


def paused_sources() -> set:
    """Names of sources the operator has paused, of EITHER kind. Read once per
    scrape. An empty set on any database problem, so a database blip never
    silently stops the catalogue refreshing."""
    try:
        with _cursor() as cur:
            if cur is None:
                return set()
            cur.execute("select source from source_health where paused")
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:
        log.warning(f"paused_sources failed: {exc}")
        return set()


def set_source_paused(source: str, paused: bool, kind: str = "builtin") -> bool:
    """Pause or resume any source by name. Creates the health row if this
    source has not been through a scrape yet."""
    if not source:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into source_health (source, kind, paused, updated_at)
                values (%s, %s, %s, now())
                on conflict (source) do update
                  set paused = excluded.paused, updated_at = now()
                """, (source, kind, bool(paused)))
            return True
    except Exception as exc:
        log.warning(f"set_source_paused failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Hidden events  (migration 0015)
# --------------------------------------------------------------------------
def hidden_event_ids() -> set:
    try:
        with _cursor() as cur:
            if cur is None:
                return set()
            cur.execute("select event_id from hidden_events")
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:
        log.warning(f"hidden_event_ids failed: {exc}")
        return set()


def list_hidden_events(limit: int = 200):
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            rows = _rows(cur, """
                select event_id, title, reason, hidden_by, created_at
                  from hidden_events order by created_at desc limit %s
            """, (limit,))
            for r in rows:
                r["created_at"] = _iso(r["created_at"])
            return rows
    except Exception as exc:
        log.warning(f"list_hidden_events failed: {exc}")
        return []


def hide_event(event_id: str, title: str = None, reason: str = None,
               hidden_by: str = None) -> bool:
    if not event_id:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into hidden_events (event_id, title, reason, hidden_by)
                values (%s, %s, %s, %s)
                on conflict (event_id) do update
                  set title = coalesce(excluded.title, hidden_events.title),
                      reason = coalesce(excluded.reason, hidden_events.reason)
                """, (event_id, (title or None), (reason or None), (hidden_by or None)))
            return True
    except Exception as exc:
        log.warning(f"hide_event failed: {exc}")
        return False


def unhide_event(event_id: str) -> bool:
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute("delete from hidden_events where event_id = %s", (event_id,))
            return cur.rowcount > 0
    except Exception as exc:
        log.warning(f"unhide_event failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Source candidates: a queue you can actually work through
# --------------------------------------------------------------------------
# The old list_source_candidates() hard-capped at 100 with no offset, no total,
# no filter and no sort, against 806 unreviewed rows. The panel showed the top
# hundred and looked like it was showing all of them.

_CAND_SORTS = {
    "events": "event_count desc, times_seen desc, last_seen desc",
    "recent": "last_seen desc, event_count desc",
    "oldest": "first_seen asc, event_count desc",
    "name":   "lower(coalesce(name, identifier)) asc",
}


def search_source_candidates(status="new", limit=25, offset=0, q=None, via=None,
                             min_events=0, fresh=None, sort="events"):
    """One page of the queue plus the true total under the same filters."""
    limit = max(1, min(int(limit or 25), 200))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if status:
        where.append("status = %s"); params.append(status)
    if q:
        where.append("(name ilike %s or identifier ilike %s or sample_title ilike %s)")
        params += [f"%{q}%"] * 3
    if via:
        where.append("discovered_via = %s"); params.append(via)
    if min_events:
        where.append("event_count >= %s"); params.append(int(min_events))
    if fresh == "stale":
        where.append("last_seen < now() - interval '14 days'")
    elif fresh and str(fresh).isdigit() and int(fresh) > 0:
        where.append("last_seen >= now() - (%s || ' days')::interval"); params.append(str(int(fresh)))
    clause = (" where " + " and ".join(where)) if where else ""
    order = _CAND_SORTS.get(sort or "events", _CAND_SORTS["events"])
    try:
        with _cursor() as cur:
            if cur is None:
                return [], 0
            cur.execute("select count(*) from source_candidates" + clause, params)
            total = cur.fetchone()[0]
            rows = _rows(cur, f"""
                select identifier, kind, name, url, city, timezone, event_count,
                       times_seen, sample_title, status, discovered_via,
                       first_seen, last_seen
                  from source_candidates{clause}
                 order by {order}
                 limit %s offset %s
            """, params + [limit, offset])
            for r in rows:
                r["first_seen"] = _iso(r["first_seen"])
                r["last_seen"] = _iso(r["last_seen"])
            return rows, total
    except Exception as exc:
        log.warning(f"search_source_candidates failed: {exc}")
        return [], 0


def source_candidate_counts() -> dict:
    try:
        with _cursor() as cur:
            if cur is None:
                return {}
            cur.execute("select status, count(*) from source_candidates group by status")
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception as exc:
        log.warning(f"source_candidate_counts failed: {exc}")
        return {}


def source_candidate_tidy_preview() -> dict:
    """How many unreviewed leads need no human judgment at all:
      - already tracked as a source (6 of them were sitting in the queue), and
      - one event or fewer AND not seen in a scrape for a fortnight."""
    try:
        with _cursor() as cur:
            if cur is None:
                return {"already_tracked": 0, "thin_stale": 0}
            cur.execute("""
                select
                  count(*) filter (where identifier in (select identifier from luma_sources)),
                  count(*) filter (where event_count <= 1
                                     and last_seen < now() - interval '14 days'
                                     and identifier not in (select identifier from luma_sources))
                  from source_candidates where status = 'new'
            """)
            a, b = cur.fetchone()
            return {"already_tracked": a or 0, "thin_stale": b or 0}
    except Exception as exc:
        log.warning(f"source_candidate_tidy_preview failed: {exc}")
        return {"already_tracked": 0, "thin_stale": 0}


def tidy_source_candidates() -> int:
    """Apply the two no-judgment rules above. Reversible: nothing is deleted,
    the rows just move out of 'new'."""
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            cur.execute("""
                update source_candidates set status = 'added'
                 where status = 'new'
                   and identifier in (select identifier from luma_sources)
            """)
            n = cur.rowcount
            cur.execute("""
                update source_candidates set status = 'ignored'
                 where status = 'new'
                   and event_count <= 1
                   and last_seen < now() - interval '14 days'
            """)
            return n + cur.rowcount
    except Exception as exc:
        log.warning(f"tidy_source_candidates failed: {exc}")
        return 0


def bulk_set_source_candidate_status(identifiers: list, status: str) -> int:
    if status not in ("new", "added", "ignored") or not identifiers:
        return 0
    ids = [i for i in identifiers if i][:500]
    if not ids:
        return 0
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            cur.execute(
                "update source_candidates set status = %s where identifier = any(%s)",
                (status, ids))
            return cur.rowcount
    except Exception as exc:
        log.warning(f"bulk_set_source_candidate_status failed: {exc}")
        return 0


def auto_ignore_stale_candidates() -> int:
    """Run at the END of discovery, every scrape. Without it the queue only
    ever grows: 806 unreviewed leads had accumulated since 14 August, 530 of
    them with a single event. A lead that has shown one event and has not
    been seen for a fortnight is noise, not a decision."""
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            cur.execute("""
                update source_candidates set status = 'ignored'
                 where status = 'new'
                   and event_count <= 1
                   and last_seen < now() - interval '21 days'
            """)
            n = cur.rowcount
            cur.execute("""
                update source_candidates set status = 'added'
                 where status = 'new'
                   and identifier in (select identifier from luma_sources)
            """)
            return n + cur.rowcount
    except Exception as exc:
        log.warning(f"auto_ignore_stale_candidates failed: {exc}")
        return 0


# --------------------------------------------------------------------------
# Accounts and audit, for the admin panel
# --------------------------------------------------------------------------
def list_accounts(limit: int = 500):
    """Google sign-in made the account the real identity and nothing in the
    panel could show you one. Pre-aggregated per side rather than joined all
    at once: joining analyses AND saved_events onto accounts in one query
    multiplies rows per person before the distinct counts collapse them."""
    try:
        with _cursor() as cur:
            if cur is None:
                return []
            rows = _rows(cur, """
                with an as (
                  select oc_uid, count(*) n, max(created_at) last_at
                    from analyses group by oc_uid
                ), sv as (
                  select account_id, count(*) n from saved_events group by account_id
                )
                select a.id, a.email, a.name, a.oc_uid, a.created_at, a.last_login_at,
                       (a.cal_token_hash is not null) as cal_feed,
                       (d.account_id is not null and not coalesce(d.paused, false)
                        and coalesce(d.frequency,'off') <> 'off') as digest_enabled,
                       d.frequency                    as digest_cadence,
                       d.next_send_at                 as digest_next,
                       coalesce(an.n, 0)              as analyses_count,
                       coalesce(sv.n, 0)              as saved_count,
                       greatest(a.last_login_at, an.last_at, a.created_at) as last_active
                  from accounts a
                  left join digest_prefs d on d.account_id = a.id
                  left join an on an.oc_uid = a.oc_uid
                  left join sv on sv.account_id = a.id
                 order by last_active desc nulls last
                 limit %s
            """, (limit,))
            for r in rows:
                for k in ("created_at", "last_login_at", "last_active", "digest_next"):
                    r[k] = _iso(r.get(k))
            return rows
    except Exception as exc:
        log.warning(f"list_accounts failed: {exc}")
        return []


def list_audit(limit: int = 50, offset: int = 0, action: str = None):
    """The audit_log table has existed since migration 0014 with nothing able
    to read it. Returns one page plus the true total."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if action:
        where.append("l.action = %s"); params.append(action)
    clause = (" where " + " and ".join(where)) if where else ""
    try:
        with _cursor() as cur:
            if cur is None:
                return [], 0, []
            cur.execute("select count(*) from audit_log l" + clause, params)
            total = cur.fetchone()[0]
            rows = _rows(cur, f"""
                select l.id, l.account_id, a.email as account_email, l.action,
                       l.resource, l.detail, l.ip_address, l.created_at
                  from audit_log l
                  left join accounts a on a.id = l.account_id{clause}
                 order by l.created_at desc
                 limit %s offset %s
            """, params + [limit, offset])
            for r in rows:
                r["created_at"] = _iso(r["created_at"])
            cur.execute("select distinct action from audit_log order by action")
            actions = [r[0] for r in cur.fetchall()]
            return rows, total, actions
    except Exception as exc:
        log.warning(f"list_audit failed: {exc}")
        return [], 0, []


# ── Account-cache invalidation for the functions that change accounts ───────
# Wrapped here rather than edited inline so every return path is covered.
def _clears_account_cache(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            _clear_account_cache()
    return wrapper


get_or_create_account_by_google = _clears_account_cache(get_or_create_account_by_google)
get_or_create_account_by_email  = _clears_account_cache(get_or_create_account_by_email)
set_postcode   = _clears_account_cache(set_postcode)
clear_postcode = _clears_account_cache(clear_postcode)
forget = _clears_account_cache(forget)
