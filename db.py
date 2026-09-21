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
        with _cursor() as cur:
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
        with _cursor() as cur:
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
        with _cursor() as cur:
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
        with _cursor() as cur:
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

            # ---- activity over time ------------------------------------
            # Generated date spine so quiet days plot as zero instead of
            # silently collapsing the x-axis.
            safe("daily", """
                with spine as (
                  select generate_series(
                    (current_date - (%s || ' days')::interval)::date,
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
            """, (days,))

            # ---- the honesty curve -------------------------------------
            safe("fit_hist", """
                select width_bucket(fit_score, 60, 100, 8) as bucket,
                       60 + (width_bucket(fit_score, 60, 100, 8) - 1) * 5 as lo,
                       count(*) as n
                from recommendations where fit_score is not null
                group by 1,2 order by 1
            """)
            safe("matches_dist", """
                select n_matches, count(*) as analyses from (
                  select a.id, count(r.id) as n_matches
                  from analyses a
                  left join recommendations r on r.analysis_id = a.id
                  where a.is_cv group by a.id
                ) t group by n_matches order by n_matches
            """)

            # ---- what the engine actually recommends -------------------
            safe("categories", """
                select coalesce(nullif(category,''),'Uncategorised') as category,
                       count(*) as n, round(avg(fit_score)::numeric,1) as avg_fit
                from recommendations group by 1 order by n desc
            """)
            safe("top_events", """
                select title, coalesce(nullif(category,''),'—') as category,
                       count(*) as times_recommended,
                       round(avg(fit_score)::numeric,1) as avg_fit
                from recommendations where title is not null and title <> ''
                group by title, category order by times_recommended desc, avg_fit desc limit 12
            """)
            safe("clicked_events", """
                select r.title, count(*) as clicks
                from interactions i join recommendations r on r.id = i.recommendation_id
                where r.title is not null and r.title <> ''
                group by r.title order by clicks desc limit 12
            """)

            # ---- who the people are ------------------------------------
            safe("evidence", """
                select coalesce(nullif(evidence_level,''),'unknown') as evidence_level,
                       count(*) as n
                from analyses group by 1 order by n desc
            """)
            safe("input_kinds", """
                select case when is_cv then 'usable (CV / self-description)'
                            else 'unusable input' end as kind, count(*) as n
                from analyses group by 1 order by n desc
            """)
            safe("seniority", """
                select coalesce(nullif(profile->>'seniority',''),'unstated') as seniority,
                       count(*) as n
                from analyses where profile is not null group by 1 order by n desc
            """)
            safe("fields", """
                select coalesce(nullif(field,''),'unstated') as field, count(*) as n
                from analyses group by 1 order by n desc limit 12
            """)

            # ---- rhythm -------------------------------------------------
            safe("hours", """
                select extract(hour from created_at)::int as hour, count(*) as n
                from analyses group by 1 order by 1
            """)
            safe("weekdays", """
                select extract(isodow from created_at)::int as dow, count(*) as n
                from analyses group by 1 order by 1
            """)
            safe("repeat", """
                select n_analyses, count(*) as people from (
                  select oc_uid, count(*) as n_analyses from analyses group by oc_uid
                ) t group by n_analyses order by n_analyses
            """)

            # ---- uploads ------------------------------------------------
            safe("files", """
                select count(*) as n,
                       coalesce(sum(size_bytes),0) as total_bytes,
                       coalesce(round(avg(size_bytes))::bigint,0) as avg_bytes,
                       coalesce(max(size_bytes),0) as max_bytes
                from raw_files
            """, single=True)
            safe("file_types", """
                select coalesce(nullif(lower(regexp_replace(filename,'^.*\\.','')),''),'unknown') as ext,
                       count(*) as n, coalesce(round(avg(size_bytes))::bigint,0) as avg_bytes
                from raw_files group by 1 order by n desc
            """)

            # ---- location ------------------------------------------------
            safe("location_consent", """
                select
                  count(*) filter (where location_consented is true)  as granted,
                  count(*) filter (where location_consented is false) as refused,
                  count(*) filter (where location_consented is null)  as never_asked
                from consent
            """, single=True)
            safe("locations", """
                select round(lat::numeric,4) as lat, round(lng::numeric,4) as lng,
                       round(coalesce(accuracy_m,0)::numeric,0) as accuracy_m,
                       to_char(created_at,'YYYY-MM-DD') as day
                from user_locations order by created_at desc limit 500
            """)

            # ---- training data + traffic detail --------------------------
            safe("labeled", """
                select judgment, count(*) as n from labeled_examples
                group by 1 order by n desc
            """)
            safe("traffic_detail", """
                select
                  coalesce(nullif(utm_source,''),'—')   as utm_source,
                  coalesce(nullif(utm_medium,''),'—')   as utm_medium,
                  coalesce(nullif(utm_campaign,''),'—') as utm_campaign,
                  coalesce(nullif(regexp_replace(referrer,'^https?://([^/]+).*$','\\1'),''),'—') as referrer,
                  count(*) as people
                from consent where consented
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
        with _cursor() as cur:
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
# Accounts + passwordless login
# --------------------------------------------------------------------------
# An account OWNS an oc_uid rather than replacing it - see the design note at
# the top of supabase/migrations/009_accounts_and_digests.sql. Everything in
# this file already keys on oc_uid, so signing up keeps the person's existing
# analyses instead of orphaning them.

def create_auth_token(email: str, token_hash: str, expires_at) -> bool:
    """Stores the SHA-256 of a magic-link token. The raw token exists only in
    the email that was just sent."""
    try:
        with _cursor() as cur:
            if cur is None:
                return False
            cur.execute(
                "insert into auth_tokens (token_hash, email, expires_at) values (%s, %s, %s)",
                (token_hash, email.strip().lower(), expires_at),
            )
            return True
    except Exception as exc:
        log.warning(f"create_auth_token failed: {exc}")
        return False


def consume_auth_token(token_hash: str):
    """Single use: marks the token used and returns its email, or None if it
    is unknown, expired, or already spent. The update's own where-clause is
    what makes it single-use - checking then updating would race."""
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                """
                update auth_tokens set used_at = now()
                where token_hash = %s and used_at is null and expires_at > now()
                returning email
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.warning(f"consume_auth_token failed: {exc}")
        return None


def purge_expired_auth_tokens() -> int:
    try:
        with _cursor() as cur:
            if cur is None:
                return 0
            cur.execute("delete from auth_tokens where expires_at < now() - interval '1 day'")
            return cur.rowcount or 0
    except Exception as exc:
        log.warning(f"purge_expired_auth_tokens failed: {exc}")
        return 0


def get_or_create_account(email: str, oc_uid: str):
    """Returns the account for `email`, creating it against the CALLER'S
    current oc_uid if it does not exist.

    That oc_uid argument is the whole point: a first-time signup adopts the
    browser's existing anonymous id, so the analyses already sitting under it
    become the account's history. A returning login ignores it and returns the
    stored one, which the caller then re-issues as the cookie."""
    email = (email or "").strip().lower()
    if not email or not oc_uid:
        return None
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "select id, email, oc_uid, created_at from accounts where lower(email) = %s",
                (email,),
            )
            row = cur.fetchone()
            if row:
                cur.execute("update accounts set last_login_at = now() where id = %s", (row[0],))
                return {"id": row[0], "email": row[1], "oc_uid": row[2], "is_new": False}
            cur.execute(
                """
                insert into accounts (email, oc_uid, last_login_at)
                values (%s, %s, now())
                on conflict (oc_uid) do update set email = excluded.email,
                                                   last_login_at = now()
                returning id, email, oc_uid
                """,
                (email, oc_uid),
            )
            new = cur.fetchone()
            return {"id": new[0], "email": new[1], "oc_uid": new[2], "is_new": True}
    except Exception as exc:
        log.warning(f"get_or_create_account failed: {exc}")
        return None


def get_or_create_account_by_google(google_sub: str, email: str, name: str,
                                    avatar_url: str, oc_uid: str,
                                    email_verified: bool = False):
    """Resolves a Google identity to an account. Returns the account dict, or
    None if the database is unavailable.

    Three cases, in this order, and the order is the security-relevant part:

    1. google_sub already known - the normal returning login. Refresh the
       profile fields (people change their Google name and photo) and return
       the STORED oc_uid, which the caller re-issues as the cookie so history
       follows them onto this device.

    2. google_sub unknown but the email matches an existing account - someone
       who signed up by magic link and is now using the Google button. Link
       the two rather than creating a second account, which the unique index
       on lower(email) would reject anyway.

       This is only safe when Google says the address is verified. Without
       that check, anyone able to create a Google identity asserting an
       address they do not own could claim the existing OrbitCast account for
       it - a straightforward account takeover. Unverified addresses fall
       through to case 3 and get their own separate account instead.

    3. Neither matches - a genuinely new person. The account adopts the
       browser's current oc_uid, exactly as a magic-link signup does, so the
       analyses already stored anonymously become this account's history.
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

            # 2. existing magic-link account for a Google-verified address
            if email_verified:
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

            # 3. new account, adopting this browser's anonymous id
            cur.execute(
                """
                insert into accounts (email, oc_uid, google_sub, name, avatar_url, last_login_at)
                values (%s, %s, %s, %s, %s, now())
                on conflict (oc_uid) do update
                        set email         = excluded.email,
                            google_sub    = excluded.google_sub,
                            name          = excluded.name,
                            avatar_url    = excluded.avatar_url,
                            last_login_at = now()
                returning id, email, oc_uid
                """,
                (email, oc_uid, google_sub, name or None, avatar_url or None),
            )
            new = cur.fetchone()
            return {"id": new[0], "email": new[1], "oc_uid": new[2],
                    "name": name, "avatar_url": avatar_url, "is_new": True}
    except Exception as exc:
        log.warning(f"get_or_create_account_by_google failed: {exc}")
        return None


def get_account(account_id):
    try:
        with _cursor() as cur:
            if cur is None:
                return None
            cur.execute(
                "select id, email, oc_uid, created_at, name, avatar_url "
                "from accounts where id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "email": row[1], "oc_uid": row[2], "created_at": _iso(row[3]),
                    "name": row[4], "avatar_url": row[5]}
    except Exception as exc:
        log.warning(f"get_account failed: {exc}")
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
        with _cursor() as cur:
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


def pause_digest_by_token(unsub_token: str):
    """One-click unsubscribe. Returns the email it paused, or None."""
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
