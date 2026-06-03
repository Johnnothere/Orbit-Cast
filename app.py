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
import requests as http_requests
from flask import Flask, jsonify, render_template, request
from security import init_security

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
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
        from scraper import SOURCES
        all_events, summary_data = [], []
        lock = threading.Lock()

        def scrape_one(src):
            try:
                events   = src["fn"]()
                enriched = [{**ev, "emoji": src["emoji"], "category": src["category"]} for ev in events]
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
# ORBITCAST AI  — Claude-powered recommendations
# ─────────────────────────────────────────────

@app.route("/api/ai/recommend", methods=["POST"])
@limiter.limit("20 per hour")
def ai_recommend():
    data    = request.get_json(silent=True) or {}
    profile = data.get("profile", "").strip()

    if not profile or len(profile) < 10:
        return jsonify({"error": "Please provide at least a short description of yourself."}), 400
    if len(profile) > 8000:
        return jsonify({"error": "Profile text too long (max 8000 chars)."}), 400

    cache  = load_events_cache()
    events = cache.get("events", [])
    if not events:
        return jsonify({"error": "No events available. Try refreshing first."}), 404

    # Try Anthropic Claude first
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            result = _ai_recommend_claude(profile, events, anthropic_key)
            return jsonify({**result, "method": "claude"})
        except Exception as e:
            log.warning(f"Claude recommendation failed: {e}, falling back to keywords")

    # Try OpenAI second
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        try:
            recs = _ai_recommend_openai(profile, events, openai_key)
            return jsonify({"recommendations": recs, "method": "openai"})
        except Exception as e:
            log.warning(f"OpenAI recommendation failed: {e}, falling back to keywords")

    # Keyword fallback
    recs = _ai_recommend_keyword(profile, events)
    return jsonify({"recommendations": recs, "method": "keyword"})


def _build_events_catalog(events):
    lines = []
    for i, ev in enumerate(events[:100]):
        line = f"{i+1}. {ev.get('title','')} | {ev.get('date','TBD')} | {ev.get('source','')} | {ev.get('category','')}"
        if ev.get("url"):
            line += f" | {ev['url']}"
        lines.append(line)
    return "\n".join(lines)


def _ai_recommend_claude(profile, events, api_key):
    """Use Claude claude-sonnet-4-20250514 for intelligent, structured recommendations."""
    events_text = _build_events_catalog(events)

    prompt = f"""You are OrbitCast AI — a sharp, strategic event recommendation engine for ambitious professionals in London's tech, defence, intelligence and business ecosystem.

UPCOMING EVENTS CATALOG:
{events_text}

USER PROFILE / CV:
{profile}

Analyse this person's background, skills, career stage, and aspirations. Then:
1. Give 3-4 strategic revelations — non-obvious insights about their positioning, trajectory, or opportunities
2. Recommend exactly 5 events from the catalog above that will genuinely move the needle for this person

Return ONLY valid JSON, no other text:
{{
  "profile_summary": "2-3 sentence sharp analysis of who this person is and where they're heading",
  "revelations": [
    {{
      "icon": "🎯",
      "title": "Short revelation title",
      "detail": "Specific strategic insight about this person — be concrete, not generic"
    }}
  ],
  "recommendations": [
    {{
      "event_index": 1,
      "match_score": 94,
      "why": "Specific reason this event is right for THIS person — reference their actual background",
      "prepare": "Concrete preparation advice tailored to their profile",
      "benefit": "Specific career/network/knowledge benefit for this person"
    }}
  ]
}}

Be sharp and specific. Reference actual details from their profile. No fluff."""

    r = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=45,
    )
    r.raise_for_status()
    content = r.json()["content"][0]["text"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(content)

    # Enrich recommendations with actual event data
    recs = []
    for rec in parsed.get("recommendations", [])[:5]:
        idx = rec.get("event_index", 1) - 1
        if 0 <= idx < len(events):
            ev = events[idx]
            recs.append({
                "title":       ev.get("title", ""),
                "date":        ev.get("date"),
                "url":         ev.get("url", ""),
                "source":      ev.get("source", ""),
                "category":    ev.get("category", ""),
                "emoji":       ev.get("emoji", ""),
                "match_score": rec.get("match_score", 80),
                "why":         rec.get("why", ""),
                "prepare":     rec.get("prepare", ""),
                "benefit":     rec.get("benefit", ""),
            })

    return {
        "profile_summary": parsed.get("profile_summary", ""),
        "revelations":     parsed.get("revelations", []),
        "recommendations": recs,
    }


def _ai_recommend_openai(profile, events, api_key):
    """OpenAI fallback."""
    events_text = _build_events_catalog(events)
    prompt = f"""You are OrbitCast AI. Recommend 5 events for this person.

EVENTS:
{events_text}

PROFILE: {profile}

Return JSON array only:
[{{"event_index":1,"match_score":90,"why":"...","prepare":"...","benefit":"..."}}]"""

    r = http_requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.7, "max_tokens": 1500},
        timeout=30,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    recs_raw = json.loads(content)
    recs = []
    for rec in recs_raw[:5]:
        idx = rec.get("event_index", 1) - 1
        if 0 <= idx < len(events):
            ev = events[idx]
            recs.append({**ev, "match_score": rec.get("match_score", 80),
                         "why": rec.get("why",""), "prepare": rec.get("prepare",""),
                         "benefit": rec.get("benefit","")})
    return recs


def _ai_recommend_keyword(profile, events):
    """Keyword fallback when no AI API key is set."""
    import re
    profile_lower = profile.lower()
    stop = {'the','a','an','and','or','but','in','on','at','to','for','of','with','is','am','are',
            'i','me','my','we','you','this','that','from','by','as','not','very','also','want',
            'like','looking','interested','experience','years','work','working','about','into','more'}
    words = set(re.findall(r'[a-z]{3,}', profile_lower)) - stop

    domain_map = {
        'ai':          ['artificial intelligence','machine learning','deep learning','llm','gpt','ai '],
        'tech':        ['software','engineering','developer','programming','coding','tech','cloud'],
        'cyber':       ['cybersecurity','security','infosec','hacking','threat','cyber'],
        'business':    ['business','startup','entrepreneur','management','product','finance'],
        'defence':     ['defence','defense','military','geopolitics','intelligence','national security'],
        'research':    ['research','academic','university','science','phd','student','education'],
        'blockchain':  ['blockchain','crypto','web3','defi','ethereum'],
        'hackathon':   ['hackathon','buildathon','hack','builder','build'],
    }
    user_domains = {d for d, kws in domain_map.items() if any(kw in profile_lower for kw in kws)}

    cat_boost = {}
    if 'ai' in user_domains or 'tech' in user_domains:
        cat_boost['Tech & AI'] = 15; cat_boost['Builder & Tech Community'] = 10
    if 'cyber' in user_domains:
        cat_boost['Cyber & Infosec'] = 15; cat_boost['Intelligence & Security'] = 10
    if 'business' in user_domains:
        cat_boost['Business & Networking'] = 15
    if 'defence' in user_domains:
        cat_boost['Defence & Geopolitics'] = 15; cat_boost['Intelligence & Security'] = 10
    if 'research' in user_domains:
        cat_boost['Education & Research'] = 15
    if 'hackathon' in user_domains:
        cat_boost['Hackathons'] = 20; cat_boost['Builder & Tech Community'] = 10

    scored = []
    for ev in events:
        text = f"{ev.get('title','')} {ev.get('category','')} {ev.get('source','')}".lower()
        matches = sum(1 for w in words if w in text)
        boost = cat_boost.get(ev.get('category',''), 0)
        if matches > 0 or boost > 0:
            scored.append((min(95, 30 + matches * 5 + boost), ev))

    scored.sort(key=lambda x: -x[0])
    recs = []
    for score, ev in scored[:5]:
        cat = ev.get('category','')
        why = f"Matches your {', '.join(list(user_domains)[:2]) or 'professional'} background."
        prep = "Review the agenda, register early, and prepare questions for speakers."
        benefit = "Expand your network and knowledge in areas directly relevant to your goals."
        recs.append({**ev, "match_score": score, "why": why, "prepare": prep, "benefit": benefit})

    if not recs:
        for ev in events[:5]:
            recs.append({**ev, "match_score": 50, "why": "Popular upcoming event.",
                         "prepare": "Check the event page for registration.", "benefit": "Networking and learning opportunity."})
    return recs


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
