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
import requests as http_requests
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
# ORBITCAST AI — Personalised event recommendations
# ─────────────────────────────────────────────

@app.route("/api/ai/recommend", methods=["POST"])
@limiter.limit("10 per hour")
def ai_recommend():
    """Accept user profile/CV text and return personalised event recommendations."""
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "").strip()
    if not profile or len(profile) < 10:
        return jsonify({"error": "Please provide at least a short description of yourself."}), 400
    if len(profile) > 5000:
        return jsonify({"error": "Profile text too long (max 5000 chars)."}), 400

    cache = load_events_cache()
    events = cache.get("events", [])
    if not events:
        return jsonify({"error": "No events available. Try refreshing first."}), 404

    # Try LLM-powered recommendations first
    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        try:
            recs = _ai_recommend_llm(profile, events, api_key)
            return jsonify({"recommendations": recs, "method": "ai"})
        except Exception as e:
            log.warning(f"LLM recommendation failed: {e}, falling back to keyword matching")

    # Fallback: keyword-based matching
    recs = _ai_recommend_keyword(profile, events)
    return jsonify({"recommendations": recs, "method": "keyword"})


def _ai_recommend_llm(profile, events, api_key):
    """Use OpenAI to generate personalised event recommendations."""
    # Build compact event list for the prompt
    event_list = []
    for i, ev in enumerate(events[:80]):  # limit to 80 events for token budget
        entry = f"{i+1}. {ev.get('title','')} | {ev.get('date','TBD')} | {ev.get('source','')}"
        if ev.get('category'):
            entry += f" | {ev['category']}"
        event_list.append(entry)
    events_text = "\n".join(event_list)

    prompt = f"""You are OrbitCast AI, a personalised event recommendation engine.

EVENTS CATALOG:
{events_text}

USER PROFILE:
{profile}

Based on the user's background, skills, aspirations, and interests, recommend the TOP 5 most relevant events. For each recommendation provide:
1. The event number from the catalog
2. A brief explanation of WHY this event is perfect for them
3. What they should PREPARE before attending
4. How attending will BENEFIT their career/goals

Respond in JSON format:
[
  {{
    "event_index": 1,
    "match_score": 95,
    "why": "...",
    "prepare": "...",
    "benefit": "..."
  }}
]

Only return the JSON array, no other text."""

    r = http_requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 1500,
        },
        timeout=30,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    # Parse the JSON from the response
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    recs_raw = json.loads(content)

    # Enrich with actual event data
    recs = []
    for rec in recs_raw[:5]:
        idx = rec.get("event_index", 1) - 1
        if 0 <= idx < len(events):
            ev = events[idx]
            recs.append({
                "title": ev.get("title", ""),
                "date": ev.get("date"),
                "url": ev.get("url", ""),
                "source": ev.get("source", ""),
                "category": ev.get("category", ""),
                "emoji": ev.get("emoji", ""),
                "match_score": rec.get("match_score", 80),
                "why": rec.get("why", ""),
                "prepare": rec.get("prepare", ""),
                "benefit": rec.get("benefit", ""),
            })
    return recs


def _ai_recommend_keyword(profile, events):
    """Fallback keyword-based event matching when no LLM API key is available."""
    import re
    profile_lower = profile.lower()

    stop_words = {'the','a','an','and','or','but','in','on','at','to','for','of','with',
                  'is','am','are','was','were','be','been','being','have','has','had',
                  'do','does','did','will','would','could','should','may','might',
                  'i','me','my','we','our','you','your','he','she','it','they','them',
                  'this','that','these','those','from','by','as','not','very','also',
                  'want','like','looking','interested','experience','years','work',
                  'working','about','into','more','new','good','make','get'}
    words = set(re.findall(r'[a-z]{3,}', profile_lower)) - stop_words

    # Domain keyword mapping for smarter matching
    domain_map = {
        'ai': ['artificial intelligence', 'machine learning', 'deep learning', 'neural', 'ai', 'llm', 'gpt', 'data science'],
        'tech': ['technology', 'software', 'engineering', 'developer', 'programming', 'coding', 'tech', 'cloud', 'devops'],
        'cyber': ['cybersecurity', 'security', 'infosec', 'hacking', 'penetration', 'threat', 'cyber'],
        'business': ['business', 'startup', 'entrepreneur', 'management', 'product', 'marketing', 'finance', 'consulting'],
        'defence': ['defence', 'defense', 'military', 'geopolitics', 'intelligence', 'national security'],
        'research': ['research', 'academic', 'university', 'science', 'phd', 'student', 'education'],
        'networking': ['networking', 'community', 'meetup', 'connect', 'career', 'professional'],
        'blockchain': ['blockchain', 'crypto', 'web3', 'defi', 'ethereum', 'bitcoin'],
    }

    # Detect user domains
    user_domains = set()
    for domain, keywords in domain_map.items():
        if any(kw in profile_lower for kw in keywords):
            user_domains.add(domain)

    # Category relevance based on domains
    cat_boost = {}
    if 'ai' in user_domains or 'tech' in user_domains:
        cat_boost['Tech & AI'] = 15
        cat_boost['Builder & Tech Community'] = 10
    if 'cyber' in user_domains:
        cat_boost['Cyber & Infosec'] = 15
        cat_boost['Intelligence & Security'] = 10
    if 'business' in user_domains or 'networking' in user_domains:
        cat_boost['Business & Networking'] = 15
    if 'defence' in user_domains:
        cat_boost['Defence & Geopolitics'] = 15
        cat_boost['Intelligence & Security'] = 10
    if 'research' in user_domains:
        cat_boost['Education & Research'] = 15
    if 'blockchain' in user_domains:
        cat_boost['Builder & Tech Community'] = 15

    scored = []
    for ev in events:
        text = f"{ev.get('title','')} {ev.get('category','')} {ev.get('source','')}".lower()
        matches = sum(1 for w in words if w in text)
        # Also check for multi-word matches
        for domain, keywords in domain_map.items():
            if domain in user_domains:
                matches += sum(2 for kw in keywords if kw in text)

        boost = cat_boost.get(ev.get('category', ''), 0)
        if matches > 0 or boost > 0:
            score = min(95, 30 + matches * 5 + boost)
            scored.append((score, ev))

    scored.sort(key=lambda x: -x[0])

    # Generate contextual explanations
    def make_why(ev, domains):
        cat = ev.get('category', '')
        parts = []
        if cat == 'Tech & AI' and ('ai' in domains or 'tech' in domains):
            parts.append("Directly relevant to your technology and AI background")
        elif cat == 'Business & Networking' and 'business' in domains:
            parts.append("Matches your business and professional development interests")
        elif cat == 'Cyber & Infosec' and 'cyber' in domains:
            parts.append("Aligns with your cybersecurity expertise")
        elif cat == 'Education & Research' and 'research' in domains:
            parts.append("Relevant to your academic and research interests")
        elif cat == 'Builder & Tech Community':
            parts.append("Great community event for tech professionals like you")
        else:
            parts.append("This event covers topics related to your profile")
        return ". ".join(parts) + "."

    def make_prep(ev):
        cat = ev.get('category', '')
        if 'Business' in cat:
            return "Bring business cards, prepare your elevator pitch, and research attendees on LinkedIn."
        if 'Tech' in cat or 'Builder' in cat:
            return "Review the event topics, bring a laptop for demos, and prepare questions for speakers."
        if 'Cyber' in cat or 'Security' in cat:
            return "Review recent security news, prepare technical questions, and bring your portfolio."
        return "Check the event agenda, register early, and prepare questions for the speakers."

    def make_benefit(ev, domains):
        cat = ev.get('category', '')
        if 'ai' in domains and 'Tech' in cat:
            return "Expand your AI knowledge, discover new tools, and connect with practitioners in the field."
        if 'business' in domains:
            return "Build your professional network, discover business opportunities, and gain industry insights."
        if 'cyber' in domains:
            return "Stay current with security trends, learn new techniques, and connect with industry peers."
        return "Gain valuable insights, expand your network, and discover new opportunities in your field."

    recs = []
    for score, ev in scored[:5]:
        recs.append({
            "title": ev.get("title", ""),
            "date": ev.get("date"),
            "url": ev.get("url", ""),
            "source": ev.get("source", ""),
            "category": ev.get("category", ""),
            "emoji": ev.get("emoji", ""),
            "match_score": score,
            "why": make_why(ev, user_domains),
            "prepare": make_prep(ev),
            "benefit": make_benefit(ev, user_domains),
        })

    if not recs:
        for ev in events[:5]:
            recs.append({
                "title": ev.get("title", ""),
                "date": ev.get("date"),
                "url": ev.get("url", ""),
                "source": ev.get("source", ""),
                "category": ev.get("category", ""),
                "emoji": ev.get("emoji", ""),
                "match_score": 50,
                "why": "A popular upcoming event in your area.",
                "prepare": "Check the event page for registration details.",
                "benefit": "Great opportunity for learning and networking.",
            })

    return recs


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
