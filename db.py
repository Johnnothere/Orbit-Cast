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

import json
import logging
import os
from contextlib import contextmanager

log = logging.getLogger("orbitcast.db")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None


def _get_pool():
    global _pool
    if not DATABASE_URL:
        return None
    if _pool is None:
        from psycopg2.pool import SimpleConnectionPool
        _pool = SimpleConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pool


def is_configured() -> bool:
    return bool(DATABASE_URL)


@contextmanager
def _cursor():
    pool = _get_pool()
    if pool is None:
        yield None
        return
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        pool.putconn(conn)


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------
def get_consent(oc_uid: str):
    """Returns True/False if a decision is on record, None if never asked."""
    if not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute("select consented from consent where oc_uid = %s", (oc_uid,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"get_consent failed: {exc}")
        return None


def set_consent(oc_uid: str, consented: bool) -> bool:
    if not oc_uid:
        return False
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                """
                insert into consent (oc_uid, consented, consented_at, updated_at)
                values (%s, %s, now(), now())
                on conflict (oc_uid) do update
                  set consented = excluded.consented, updated_at = now()
                """,
                (oc_uid, consented),
            )
            return True
    except Exception as exc:
        log.warning(f"set_consent failed: {exc}")
        return False


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
def save_analysis(oc_uid: str, is_cv: bool, cv_text: str, profile: dict):
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                insert into analyses (oc_uid, is_cv, cv_text, profile)
                values (%s, %s, %s, %s)
                returning id
                """,
                (oc_uid, is_cv, cv_text, json.dumps(profile)),
            )
            return cur.fetchone()[0]
    except Exception as exc:
        log.warning(f"save_analysis failed: {exc}")
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
                      (analysis_id, event_id, title, category, fit_score, why, why_now, prepare, benefit)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (analysis_id, r.get("event_id"), r.get("title"), r.get("category"),
                     r.get("fit_score"), r.get("why"), r.get("why_now"), r.get("prepare"), r.get("benefit")),
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
                "select oc_uid, consented, consented_at, updated_at from consent where oc_uid = %s",
                (oc_uid,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            consent = dict(zip(["oc_uid", "consented", "consented_at", "updated_at"], row))
            consent["consented_at"] = _iso(consent["consented_at"])
            consent["updated_at"] = _iso(consent["updated_at"])

            cur.execute(
                """
                select id, created_at, is_cv, cv_text, profile
                from analyses where oc_uid = %s order by created_at desc
                """,
                (oc_uid,),
            )
            a_cols = ["id", "created_at", "is_cv", "cv_text", "profile"]
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
                           why, why_now, prepare, benefit, created_at
                    from recommendations
                    where analysis_id = any(%s)
                    order by fit_score desc nulls last
                    """,
                    (analysis_ids,),
                )
                r_cols = ["id", "analysis_id", "event_id", "title", "category", "fit_score",
                          "why", "why_now", "prepare", "benefit", "created_at"]
                for r in cur.fetchall():
                    rec = dict(zip(r_cols, r))
                    rec["created_at"] = _iso(rec["created_at"])
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

            return {"consent": consent, "analyses": analyses, "interactions": interactions}
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
    """Returns the stored value for `key`, or None if unset/unconfigured."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute("select value from ai_config where key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"get_config failed: {exc}")
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
            return True
    except Exception as exc:
        log.warning(f"delete_config failed: {exc}")
        return False
