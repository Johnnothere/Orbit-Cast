#!/usr/bin/env python3
"""
ORBITCAST — Web Dashboard + API
Run with: python app.py
"""

import os
import json
import threading
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, make_response, render_template, request
from security import init_security
import ai_engine
import db
import rag

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # headroom for admin example-doc uploads
limiter = init_security(app)

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
OC_UID_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 years


def ensure_oc_uid(resp):
    """Every write to the database is keyed on this anonymous per-browser id.
    Mints one on first contact and persists it via cookie on `resp` - never
    tied to any real identity, just enough to know "same browser asked
    before" for consent and, later, for the forget-me delete."""
    oc_uid = request.cookies.get("oc_uid")
    if not oc_uid:
        oc_uid = uuid.uuid4().hex
        resp.set_cookie("oc_uid", oc_uid, max_age=OC_UID_MAX_AGE, httponly=True, samesite="Lax")
    return oc_uid

SEEN_FILE   = Path("seen_events.json")
EVENTS_FILE = Path("events_cache.json")
LAST_RUN_FILE = Path("last_run.json")

# ─────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────

def load_events_cache():
    if EVENTS_FILE.exists():
        return json.loads(EVENTS_FILE.read_text())
    return {"events": [], "summary": [], "last_run": None}

def load_last_run():
    if LAST_RUN_FILE.exists():
        return json.loads(LAST_RUN_FILE.read_text())
    return {}

def save_events_cache(events, summary):
    data = {
        "events":   events,
        "summary":  summary,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    EVENTS_FILE.write_text(json.dumps(data, indent=2))
    LAST_RUN_FILE.write_text(json.dumps({"last_run": data["last_run"]}))

# ─────────────────────────────────────────────
# BACKGROUND SCRAPE
# ─────────────────────────────────────────────

_scrape_lock = threading.Lock()
_scraping    = False

def run_scrape_background():
    global _scraping
    with _scrape_lock:
        if _scraping:
            return
        _scraping = True
    try:
        from scraper import SOURCES, event_id, HACKATHON_RE, is_london
        all_events, summary_data = [], []
        lock = threading.Lock()

        def scrape_one(src):
            try:
                events = src["fn"]()
                enriched = []
                for ev in events:
                    # This is a London catalog. Some sources (notably the
                    # Claude Community calendar) are global and publish
                    # "Portland | ...", "Taipei | ..." events; drop anything
                    # that names a different city.
                    if not is_london(ev, src["name"]):
                        continue
                    # Auto-tag hackathons from title, regardless of source -
                    # keeps the category live instead of relying on a
                    # hand-maintained list that goes stale.
                    category = "Hackathons" if HACKATHON_RE.search(ev.get("title", "")) else src["category"]
                    enriched.append({**ev, "id": event_id(ev.get("title", ""), ev.get("url", "")),
                                      "emoji": src["emoji"], "category": category})
                # count reflects what actually made it into the catalog, so the
                # dashboard doesn't claim events that were filtered out
                summary  = {"source": src["name"], "emoji": src["emoji"],
                            "category": src["category"], "count": len(enriched)}
                return enriched, summary
            except Exception as e:
                log.error(f"Scraper {src['name']} failed: {e}")
                return [], {"source": src["name"], "emoji": src["emoji"],
                            "category": src["category"], "count": 0}

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(scrape_one, s): s for s in SOURCES}
            for f in as_completed(futures):
                evs, summ = f.result()
                with lock:
                    all_events.extend(evs)
                    summary_data.append(summ)

        summary_data.sort(key=lambda x: x["source"])
        save_events_cache(all_events, summary_data)
        log.info(f"Scrape done: {len(all_events)} events")
    except Exception as e:
        log.error(f"Background scrape failed: {e}")
    finally:
        _scraping = False

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/api/events")
def api_events():
    cache    = load_events_cache()
    category = request.args.get("category", "").strip()
    source   = request.args.get("source",   "").strip()
    events   = cache.get("events", [])
    if category:
        events = [e for e in events if e.get("category","").lower() == category.lower()]
    if source:
        events = [e for e in events if e.get("source","").lower() == source.lower()]
    return jsonify({"events": events, "total": len(events),
                    "last_run": cache.get("last_run"), "scraping": _scraping})

@app.route("/api/summary")
def api_summary():
    cache = load_events_cache()
    return jsonify({"summary": cache.get("summary",[]),
                    "last_run": cache.get("last_run"), "scraping": _scraping})

@app.route("/api/refresh", methods=["POST"])
@limiter.limit("6 per hour")
def api_refresh():
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "Scraping in background..."})

@app.route("/api/status")
def api_status():
    return jsonify({"scraping": _scraping, "last_run": load_last_run().get("last_run")})

@app.route("/")
def dashboard():
    resp = make_response(render_template("index.html"))
    ensure_oc_uid(resp)
    return resp

@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")

# ─────────────────────────────────────────────
# ORBITCAST AI  — Claude-powered CV analysis
# ─────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
@limiter.limit("20 per hour")
def api_analyze():
    file = request.files.get("file")
    pasted_text = (request.form.get("text") or "").strip()

    if file and file.filename:
        file_bytes = file.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            return jsonify({"error": "File too large (max 5MB)."}), 400
        try:
            file_text = ai_engine.extract_text(file_bytes, file.filename)
        except ValueError as e:
            # Raised with an already user-facing message (e.g. no extractable
            # text found) - surface it instead of a generic one.
            log.warning(f"CV extraction failed: {e}")
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            log.warning(f"CV extraction failed: {e}")
            return jsonify({"error": "Could not read that file. Try a PDF, DOCX, or TXT CV."}), 400
        if pasted_text:
            file_text = f"{file_text}\n\n[ADDITIONAL CONTEXT FROM USER]\n{pasted_text}"
    else:
        file_text = pasted_text

    if not file_text or len(file_text.strip()) < 10:
        return jsonify({"error": "Upload a CV, or tell us a bit about yourself."}), 400
    file_text = file_text[:20000]

    cache  = load_events_cache()
    events = cache.get("events", [])
    if not events:
        return jsonify({"error": "No events available. Try refreshing first."}), 404

    # Read-only cookie peek (does not mint one) - only used to look up a
    # location this browser already granted, so recommendations can carry a
    # distance + directions link. None on any first visit or decline; the
    # analysis runs identically either way.
    _existing_oc_uid = request.cookies.get("oc_uid")
    user_location = None
    if _existing_oc_uid and db.get_location_consent(_existing_oc_uid):
        user_location = db.get_latest_location(_existing_oc_uid)

    result = ai_engine.analyze_upload(file_text, events, user_location=user_location)

    resp = jsonify(result)
    oc_uid = ensure_oc_uid(resp)
    # Consent is the hinge: no recorded "yes" for this browser means nothing
    # gets written, and the response the user sees is identical either way.
    if db.get_consent(oc_uid):
        try:
            analysis_id = db.save_analysis(oc_uid, result.get("is_cv", False), file_text, result.get("profile"),
                                            evidence_level=result.get("evidence_level"), field=result.get("field"))
            # The original file, byte-for-byte, stored alongside the
            # extracted-text row above - same consent gate, additive only.
            # The extraction pipeline above is untouched by this.
            if file and file.filename and analysis_id is not None:
                db.save_raw_file(analysis_id, file.filename, file.mimetype, file_bytes)
            recs = result.get("recommendations", [])
            rec_ids = db.save_recommendations(analysis_id, recs)
            for rec, rec_id in zip(recs, rec_ids):
                rec["recommendation_id"] = rec_id
            resp = jsonify(result)  # rebuild - recs now carry recommendation_id for tracking
        except Exception as e:
            log.warning(f"Persisting analysis failed (non-fatal): {e}")
    return resp


@app.route("/api/my-history", methods=["GET"])
def api_my_history():
    """Powers the returning-visitor 'reuse my last profile' prompt. Reads
    the oc_uid from THIS request's own cookie only - there is no way to
    pass a different oc_uid in, so this can only ever return the calling
    browser's own data, never anyone else's."""
    oc_uid = request.cookies.get("oc_uid")
    if not oc_uid or not db.get_consent(oc_uid):
        return jsonify({"has_history": False})
    last = db.get_latest_analysis(oc_uid)
    if not last:
        return jsonify({"has_history": False})
    return jsonify({"has_history": True, "summary": (last["profile"] or {}).get("summary", "")})


@app.route("/api/analyze/reuse", methods=["POST"])
@limiter.limit("30 per hour")
def api_analyze_reuse():
    """Re-scores the caller's own last stored profile against the current
    catalog, without re-running extraction or asking them to resubmit
    anything - the 'don't make me upload my CV every time' feature. Same
    consent gate and same own-cookie-only access as /api/my-history."""
    oc_uid = request.cookies.get("oc_uid")
    if not oc_uid or not db.get_consent(oc_uid):
        return jsonify({"error": "No saved profile for this browser."}), 404
    last = db.get_latest_analysis(oc_uid)
    if not last:
        return jsonify({"error": "No saved profile for this browser."}), 404

    cache  = load_events_cache()
    events = cache.get("events", [])
    if not events:
        return jsonify({"error": "No events available. Try refreshing first."}), 404

    user_location = db.get_latest_location(oc_uid) if db.get_location_consent(oc_uid) else None
    scored = ai_engine.score_and_enrich(last["profile"], last["evidence_level"], last["field"],
                                         events, user_location=user_location)
    result = {"is_cv": True, "evidence_level": last["evidence_level"], "field": last["field"],
              "profile": last["profile"], "clarifying_questions": [], "analysis_failed": False,
              "message_if_not_cv": "", **scored}

    try:
        analysis_id = db.save_analysis(oc_uid, True, "[reused previous profile]", last["profile"],
                                        evidence_level=last["evidence_level"], field=last["field"])
        recs = result.get("recommendations", [])
        rec_ids = db.save_recommendations(analysis_id, recs)
        for rec, rec_id in zip(recs, rec_ids):
            rec["recommendation_id"] = rec_id
    except Exception as e:
        log.warning(f"Persisting reused analysis failed (non-fatal): {e}")

    return jsonify(result)


# ─────────────────────────────────────────────
# CONSENT + TRACKING + GDPR
# ─────────────────────────────────────────────

@app.route("/api/consent", methods=["GET", "POST"])
def api_consent():
    if request.method == "GET":
        oc_uid = request.cookies.get("oc_uid")
        decision = db.get_consent(oc_uid) if oc_uid else None
        return jsonify({"decision": decision})

    data = request.get_json(silent=True) or {}
    accepted = bool(data.get("accepted"))
    resp = jsonify({"ok": True, "decision": accepted})
    oc_uid = ensure_oc_uid(resp)
    # Traffic source can only come from the client - document.referrer and
    # any ?utm_* params are properties of the ORIGINAL page load, not of
    # this fetch() call (whose own Referer header would just be this same
    # page). Capped length as basic hygiene against an oversized payload;
    # nothing here is validated against a known list of sources.
    def _cap(s, n=300):
        return (s or "").strip()[:n] or None
    db.set_consent(oc_uid, accepted,
                    referrer=_cap(data.get("referrer")),
                    utm_source=_cap(data.get("utm_source"), 100),
                    utm_medium=_cap(data.get("utm_medium"), 100),
                    utm_campaign=_cap(data.get("utm_campaign"), 100))
    return resp

@app.route("/api/location", methods=["GET", "POST"])
def api_location():
    if request.method == "GET":
        oc_uid = request.cookies.get("oc_uid")
        decision = db.get_location_consent(oc_uid) if oc_uid else None
        # On a returning visit where location was already granted, hand back
        # the last known fix so the page doesn't have to re-prompt the OS
        # geolocation dialog on every load just to draw the map/distance.
        loc = db.get_latest_location(oc_uid) if (oc_uid and decision) else None
        return jsonify({"decision": decision, "lat": loc["lat"] if loc else None,
                         "lng": loc["lng"] if loc else None})

    data = request.get_json(silent=True) or {}
    accepted = bool(data.get("accepted"))
    resp = jsonify({"ok": True, "decision": accepted})
    oc_uid = ensure_oc_uid(resp)
    # location_consented lives on the consent row, so that row must exist
    # first - it does by the time this can fire, since the location prompt
    # only ever appears after the general consent banner has been answered.
    if db.get_consent(oc_uid) is None:
        db.set_consent(oc_uid, False)
    db.set_location_consent(oc_uid, accepted)
    if accepted:
        lat, lng = data.get("lat"), data.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            db.save_user_location(oc_uid, lat, lng, data.get("accuracy"))
    return resp


@app.route("/api/track", methods=["POST"])
@limiter.limit("200 per hour")
def api_track():
    oc_uid = request.cookies.get("oc_uid")
    data = request.get_json(silent=True) or {}
    if oc_uid and db.get_consent(oc_uid):
        db.log_interaction(oc_uid, data.get("recommendation_id"), data.get("event_type", "view_event"))
    return jsonify({"ok": True})

@app.route("/api/forget", methods=["POST"])
@limiter.limit("10 per hour")
def api_forget():
    oc_uid = request.cookies.get("oc_uid")
    if oc_uid:
        db.forget(oc_uid)
    resp = jsonify({"ok": True})
    resp.set_cookie("oc_uid", "", expires=0)
    return resp

# ─────────────────────────────────────────────
# ADMIN — RAG example ingestion (mirrors "upload a doc to train it")
# ─────────────────────────────────────────────

def _admin_authorized() -> bool:
    return bool(ADMIN_SECRET) and request.headers.get("X-Admin-Secret") == ADMIN_SECRET


@app.route("/admin")
def admin_dashboard():
    # The page itself carries no secret data - it just prompts for the admin
    # secret client-side and attaches it as a header on every API call below.
    # Every route that actually reads/writes anything re-checks that header.
    return render_template("admin.html")


@app.route("/api/admin/ingest", methods=["POST"])
@limiter.limit("30 per hour")
def api_admin_ingest():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Upload a document (PDF, DOCX, or TXT) of example judgments."}), 400
    file_bytes = file.read()
    try:
        text = ai_engine.extract_text(file_bytes, file.filename)
    except Exception as e:
        return jsonify({"error": f"Could not read that file: {e}"}), 400
    if not text or len(text.strip()) < 20:
        return jsonify({"error": "That file looks empty."}), 400
    result = rag.ingest_document(text, file.filename, reviewed_by=request.headers.get("X-Admin-User"))
    return jsonify(result)


@app.route("/api/admin/examples", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_list_examples():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"examples": db.list_labeled_examples()})


@app.route("/api/admin/examples", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_add_example():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    profile_summary = (data.get("profile_summary") or "").strip()
    event_context = (data.get("event_context") or "").strip()
    judgment = (data.get("judgment") or "").strip()
    ideal_why = (data.get("ideal_why") or "").strip()
    if not profile_summary or judgment not in ("good", "bad", "borderline"):
        return jsonify({"error": "profile_summary and a valid judgment (good/bad/borderline) are required."}), 400
    embedding = rag.embed_text(f"{profile_summary}\n{event_context}")
    if embedding is None:
        return jsonify({"error": "Could not embed this example - is VOYAGE_API_KEY configured?"}), 503
    example_id = db.insert_labeled_example(
        source="manual", profile_summary=profile_summary, profile_json=None,
        event_context=event_context, judgment=judgment, ideal_why=ideal_why,
        embedding=embedding, reviewed_by=request.headers.get("X-Admin-User"),
    )
    if example_id is None:
        return jsonify({"error": "Could not save that example."}), 500
    return jsonify({"id": example_id})


@app.route("/api/admin/examples/<int:example_id>", methods=["DELETE"])
@limiter.limit("60 per hour")
def api_admin_delete_example(example_id):
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    ok = db.delete_labeled_example(example_id)
    return jsonify({"ok": ok}), (200 if ok else 404)


@app.route("/api/admin/config", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_get_config():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    custom = db.get_config(ai_engine.CONFIG_KEY_SCORING_RULES)
    return jsonify({
        "rules": custom if custom else ai_engine.DEFAULT_SCORING_RULES,
        "is_custom": bool(custom),
        "default_rules": ai_engine.DEFAULT_SCORING_RULES,
    })


@app.route("/api/admin/config", methods=["POST"])
@limiter.limit("30 per hour")
def api_admin_set_config():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        db.delete_config(ai_engine.CONFIG_KEY_SCORING_RULES)
        return jsonify({"ok": True, "rules": ai_engine.DEFAULT_SCORING_RULES, "is_custom": False})
    rules = (data.get("rules") or "").strip()
    if not rules:
        return jsonify({"error": "rules text cannot be empty - use reset instead to clear an override."}), 400
    db.set_config(ai_engine.CONFIG_KEY_SCORING_RULES, rules)
    return jsonify({"ok": True, "rules": rules, "is_custom": True})


@app.route("/api/admin/analyses/<int:analysis_id>/raw", methods=["GET"])
@limiter.limit("60 per hour")
def api_admin_download_raw(analysis_id):
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    raw = db.get_raw_file(analysis_id)
    if raw is None:
        return jsonify({"error": "No original file stored for this analysis - it may have been a "
                                  "pasted-text submission, or predates this feature."}), 404
    resp = make_response(raw["file_bytes"])
    resp.headers["Content-Type"] = raw["content_type"] or "application/octet-stream"
    safe_name = (raw["filename"] or "resume").replace('"', "")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@app.route("/api/admin/analytics", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_analytics():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"funnel": db.get_funnel_analytics()})


@app.route("/api/admin/users", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_list_users():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"users": db.list_users()})


@app.route("/api/admin/users/<oc_uid>", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_get_user(oc_uid):
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    activity = db.get_user_activity(oc_uid)
    if activity is None:
        return jsonify({
            "error": "No record for that ID - either it's never been issued, "
                     "or that browser was never asked for consent."
        }), 404
    return jsonify(activity)


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

# Runs on import, not just under `python app.py` - gunicorn imports this
# module directly and never hits the __main__ guard below. Without this,
# every deploy boots with an empty cache (Railway's filesystem is
# ephemeral) and nothing repopulates it until someone manually hits
# Refresh. Safe to run unconditionally: single gunicorn worker process,
# so this fires exactly once.
logging.basicConfig(level=logging.INFO)
threading.Thread(target=run_scrape_background, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
