#!/usr/bin/env python3
"""
ORBITCAST — Web Dashboard + API
Run with: python app.py
"""

import os
import json
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from security import init_security

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
limiter = init_security(app)

SEEN_FILE = Path("seen_events.json")
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
        "events": events,
        "summary": summary,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    EVENTS_FILE.write_text(json.dumps(data, indent=2))
    LAST_RUN_FILE.write_text(json.dumps({"last_run": data["last_run"]}))

# ─────────────────────────────────────────────
# BACKGROUND SCRAPE RUNNER
# ─────────────────────────────────────────────

_scrape_lock = threading.Lock()
_scraping = False

def run_scrape_background():
    global _scraping
    with _scrape_lock:
        if _scraping:
            return
        _scraping = True
    try:
        from scraper import SOURCES

        all_events = []
        summary_data = []
        results_lock = threading.Lock()

        def scrape_one(src):
            try:
                events = src["fn"]()
                enriched = [{**ev, "emoji": src["emoji"], "category": src["category"]} for ev in events]
                summary = {"source": src["name"], "emoji": src["emoji"], "category": src["category"], "count": len(events)}
                return enriched, summary
            except Exception as e:
                log.error(f"Scraper {src['name']} failed: {e}")
                return [], {"source": src["name"], "emoji": src["emoji"], "category": src["category"], "count": 0}

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(scrape_one, src): src for src in SOURCES}
            for future in as_completed(futures):
                events, summary = future.result()
                with results_lock:
                    all_events.extend(events)
                    summary_data.append(summary)

        summary_data.sort(key=lambda x: x["source"])
        save_events_cache(all_events, summary_data)
        log.info(f"Background scrape done: {len(all_events)} events")
    except Exception as e:
        log.error(f"Background scrape failed: {e}")
    finally:
        _scraping = False

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/api/events")
def api_events():
    cache = load_events_cache()
    category = request.args.get("category", "").strip()
    source = request.args.get("source", "").strip()
    events = cache.get("events", [])
    if category:
        events = [e for e in events if e.get("category", "").lower() == category.lower()]
    if source:
        events = [e for e in events if e.get("source", "").lower() == source.lower()]
    return jsonify({
        "events": events,
        "total": len(events),
        "last_run": cache.get("last_run"),
        "scraping": _scraping,
    })

@app.route("/api/summary")
def api_summary():
    cache = load_events_cache()
    return jsonify({
        "summary": cache.get("summary", []),
        "last_run": cache.get("last_run"),
        "scraping": _scraping,
    })

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
# STARTUP
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Run initial scrape on boot in background
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
