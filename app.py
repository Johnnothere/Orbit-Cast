#!/usr/bin/env python3
"""
ORBITCAST — Web Dashboard + API
Run with: python app.py
"""

import os
import json
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from security import init_security
import ai_engine

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # 6MB hard cap on request body
limiter = init_security(app)

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
        from scraper import SOURCES, event_id
        all_events, summary_data = [], []
        lock = threading.Lock()

        def scrape_one(src):
            try:
                events   = src["fn"]()
                enriched = [{**ev, "id": event_id(ev.get("title", ""), ev.get("url", "")),
                             "emoji": src["emoji"], "category": src["category"]} for ev in events]
                summary  = {"source": src["name"], "emoji": src["emoji"],
                            "category": src["category"], "count": len(events)}
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
    return render_template("index.html")

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

    result = ai_engine.analyze_upload(file_text, events)
    return jsonify(result)


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
