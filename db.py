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
