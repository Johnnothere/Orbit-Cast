#!/usr/bin/env python3
"""
ORBITCAST — Web Dashboard + API
Run with: python app.py
"""

import os
import io
import re
import json
import secrets
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import requests as http_requests
from flask import Flask, jsonify, render_template, request, make_response
from security import init_security

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
limiter = init_security(app)

SEEN_FILE   = Path("seen_events.json")
EVENTS_FILE = Path("events_cache.json")
LAST_RUN_FILE = Path("last_run.json")

# ─────────────────────────────────────────────
# AI / STORAGE / BILLING CONFIG  (all optional, set via Railway → Variables)
# ─────────────────────────────────────────────
FREE_ANALYSES         = int(os.getenv("FREE_ANALYSES", "3"))
STORE_RAW_FILES       = os.getenv("STORE_RAW_FILES", "false").lower() == "true"
DATABASE_URL          = os.getenv("DATABASE_URL", "")
STRIPE_SECRET         = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID       = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_URL            = os.getenv("PUBLIC_URL", "").rstrip("/")
BILLING_ON            = bool(STRIPE_SECRET and STRIPE_PRICE_ID)

UPLOADS_FILE = Path("uploads_store.jsonl")   # file fallback when no DATABASE_URL
PREMIUM_FILE = Path("premium_store.json")
_mem_counts  = {}                            # uid -> CV-analysis count (in-memory aid)


# ── CV text extraction (server-side — far better than client-side regex) ──
def extract_cv_text(file_bytes, filename):
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in d.paragraphs)
    return file_bytes.decode("utf-8", errors="ignore")


# ── Storage: Postgres if DATABASE_URL is set, else local JSON files ──
def _db():
    if not DATABASE_URL:
        return None
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_storage():
    conn = _db()
    if not conn:
        log.info("No DATABASE_URL — storing uploads in local files (not durable across redeploys).")
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS analyses (
                id BIGSERIAL PRIMARY KEY, uid TEXT, filename TEXT, document_type TEXT,
                is_cv BOOLEAN, extracted_text TEXT, raw_file BYTEA, result JSONB,
                created_at TIMESTAMPTZ DEFAULT now());""")
            cur.execute("""CREATE TABLE IF NOT EXISTS premium (
                email TEXT PRIMARY KEY, active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now());""")
    finally:
        conn.close()


def count_analyses(uid):
    if not uid:
        return 0
    conn = _db()
    if conn:
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM analyses WHERE uid=%s AND is_cv=TRUE", (uid,))
                return cur.fetchone()[0]
        finally:
            conn.close()
    if uid in _mem_counts:
        return _mem_counts[uid]
    n = 0
    if UPLOADS_FILE.exists():
        for line in UPLOADS_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("uid") == uid and r.get("is_cv"):
                    n += 1
            except Exception:
                pass
    _mem_counts[uid] = n
    return n


def save_analysis(uid, filename, text, raw_bytes, result):
    is_cv = bool(result.get("is_cv"))
    conn = _db()
    if conn:
        try:
            import psycopg2
            with conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO analyses
                    (uid, filename, document_type, is_cv, extracted_text, raw_file, result)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (uid, filename, result.get("document_type"), is_cv, text,
                     (psycopg2.Binary(raw_bytes) if (STORE_RAW_FILES and raw_bytes) else None),
                     json.dumps(result)))
        finally:
            conn.close()
        return
    rec = {"uid": uid, "filename": filename, "document_type": result.get("document_type"),
           "is_cv": is_cv, "text": text, "result": result,
           "ts": datetime.now(timezone.utc).isoformat()}
    with UPLOADS_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    if is_cv:
        _mem_counts[uid] = _mem_counts.get(uid, 0) + 1


def is_premium(email):
    if not email:
        return False
    email = email.lower().strip()
    conn = _db()
    if conn:
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT active FROM premium WHERE email=%s", (email,))
                row = cur.fetchone()
                return bool(row and row[0])
        finally:
            conn.close()
    if PREMIUM_FILE.exists():
        try:
            return bool(json.loads(PREMIUM_FILE.read_text()).get(email))
        except Exception:
            return False
    return False


def set_premium(email):
    email = email.lower().strip()
    conn = _db()
    if conn:
        try:
            with conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO premium (email, active) VALUES (%s, TRUE)
                    ON CONFLICT (email) DO UPDATE SET active=TRUE""", (email,))
        finally:
            conn.close()
        return
    data = {}
    if PREMIUM_FILE.exists():
        try:
            data = json.loads(PREMIUM_FILE.read_text())
        except Exception:
            data = {}
    data[email] = True
    PREMIUM_FILE.write_text(json.dumps(data))


def get_or_make_uid():
    return request.cookies.get("oc_uid") or secrets.token_hex(16)


def _with_uid(resp, uid):
    resp.set_cookie("oc_uid", uid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax")
    return resp

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
    # Accept either a multipart upload (CV file + optional text) OR JSON {profile}.
    uploaded  = request.files.get("cv")
    raw_bytes = None
    cv_text   = ""
    filename  = "pasted-text.txt"
    if uploaded and uploaded.filename:
        filename  = uploaded.filename
        raw_bytes = uploaded.read()
        if len(raw_bytes) > 5 * 1024 * 1024:
            return jsonify({"error": "File too large (max 5MB)."}), 400
        try:
            cv_text = extract_cv_text(raw_bytes, filename)
        except Exception as e:
            log.warning(f"CV extraction failed: {e}")
            cv_text = ""

    body  = request.get_json(silent=True) or {}
    typed = (request.form.get("profile") or body.get("profile", "")).strip()
    email = (request.form.get("email") or body.get("email", "")).strip().lower()

    profile = ((cv_text + "\n\n" + typed) if cv_text else typed).strip()
    if not profile or len(profile) < 10:
        return jsonify({"error": "Upload a CV or tell us a bit about yourself first."}), 400
    if len(profile) > 12000:
        profile = profile[:12000]

    # Free-tier gate (the revenue lever).
    uid     = get_or_make_uid()
    premium = is_premium(email)
    if not premium and count_analyses(uid) >= FREE_ANALYSES:
        resp = make_response(jsonify({
            "error": "limit_reached",
            "message": f"You've used your {FREE_ANALYSES} free analyses.",
            "billing_enabled": BILLING_ON}), 402)
        return _with_uid(resp, uid)

    cache  = load_events_cache()
    events = cache.get("events", [])
    if not events:
        return jsonify({"error": "No events available yet. Hit Refresh, then analyse."}), 404

    result = {}
    method = "keyword"
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            result = _ai_recommend_claude(profile, events, anthropic_key)
            method = "claude"
        except Exception as e:
            log.warning(f"Claude recommendation failed: {e}, trying fallback")
    if not result:
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            try:
                result = {"is_cv": True, "recommendations": _ai_recommend_openai(profile, events, openai_key)}
                method = "openai"
            except Exception as e:
                log.warning(f"OpenAI recommendation failed: {e}, using keywords")
    if not result:
        result = {"is_cv": True, "recommendations": _ai_recommend_keyword(profile, events)}
        method = "keyword"

    save_analysis(uid, filename, profile, raw_bytes, result)
    remaining = None if premium else max(0, FREE_ANALYSES - count_analyses(uid))
    result["method"] = method
    result["_meta"]  = {"premium": premium, "remaining_free": remaining}
    return _with_uid(make_response(jsonify(result)), uid)


def _build_events_catalog(events):
    lines = []
    for i, ev in enumerate(events[:100]):
        line = f"{i+1}. {ev.get('title','')} | {ev.get('date','TBD')} | {ev.get('source','')} | {ev.get('category','')}"
        if ev.get("url"):
            line += f" | {ev['url']}"
        lines.append(line)
    return "\n".join(lines)


def _ai_recommend_claude(profile, events, api_key):
    """Claude — classifies CV vs not, then returns an HONEST, selective set of matches.

    The value of OrbitCast is that it does NOT flatter and does NOT over-recommend:
    selectivity and gap-honesty are instructed in the prompt AND enforced in code.
    """
    events_text = _build_events_catalog(events)

    prompt = f"""You are OrbitCast AI — an honest, selective event matcher for London's tech, defence, intelligence, security, research and builder scene. Your value is that you DON'T flatter and DON'T over-recommend.

UPCOMING EVENTS CATALOG (numbered):
{events_text}

UPLOADED TEXT (may be a CV/resume, or may be something else entirely):
{profile}

STEP 1 — CLASSIFY. Decide whether the uploaded text is a CV/resume. A CV has a person's work/education history, skills, and contact-style details. If it is NOT clearly a CV (a cover letter, a report, a random document, or just a vague one-line note), set is_cv=false and return empty revelations and recommendations with a short friendly message telling them to paste more about their background or upload an actual CV.

STEP 2 — If it IS a CV (or a substantive self-description), read the real person and match them to events. Be honest:
- Ground EVERY statement in concrete evidence from the text. Reference the actual role, project, or skill. No generic flattery ("passionate innovator", "acceleration window", "differentiation edge").
- Be SELECTIVE. Most events will not be a strong fit. Recommend only genuine matches — typically 2 to 4, never padding to a fixed number. If nothing is a strong fit, return an empty recommendations list and say so plainly in the message.
- Score each match honestly 0-100. Do not inflate. Only include events scoring 65 or higher.
- In the revelations, give honest insight INCLUDING at least one real gap or weakness in their profile — not only strengths.

Return ONLY valid JSON, no other text:
{{
  "is_cv": true,
  "document_type": "cv",
  "message": "",
  "profile_summary": "2-3 sentence honest, evidence-grounded read of who this person is and where they're heading",
  "revelations": [
    {{"icon": "🎯", "title": "Short title", "detail": "Specific, evidence-based insight — at least one should name a real gap"}}
  ],
  "recommendations": [
    {{"event_index": 1, "match_score": 88, "why": "Why THIS person — cite their actual background", "prepare": "Concrete prep given their background", "benefit": "Specific, concrete payoff"}}
  ]
}}

Be sharp, specific, and honest. An empty recommendations list is a valid, correct answer when nothing fits."""

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

    # Not a CV -> stop, return a clean signal for the frontend.
    if not parsed.get("is_cv", True):
        return {
            "is_cv": False,
            "document_type": parsed.get("document_type", "other"),
            "message": parsed.get("message",
                "That doesn't look like a CV. Paste a few lines about your background, or upload an actual CV."),
            "profile_summary": "", "revelations": [], "recommendations": [],
        }

    # Enrich + enforce the honesty threshold in code, not just in the prompt.
    recs = []
    for rec in parsed.get("recommendations", []):
        if rec.get("match_score", 0) < 65:
            continue
        idx = rec.get("event_index", 0) - 1
        if 0 <= idx < len(events):
            ev = events[idx]
            recs.append({
                "title":       ev.get("title", ""),
                "date":        ev.get("date"),
                "url":         ev.get("url", ""),
                "source":      ev.get("source", ""),
                "category":    ev.get("category", ""),
                "emoji":       ev.get("emoji", ""),
                "match_score": rec.get("match_score", 70),
                "why":         rec.get("why", ""),
                "prepare":     rec.get("prepare", ""),
                "benefit":     rec.get("benefit", ""),
            })
    recs.sort(key=lambda r: r.get("match_score", 0), reverse=True)

    msg = parsed.get("message", "")
    if not recs and not msg:
        msg = ("No event currently tracked is a strong match for your background — "
               "that's an honest read, not a bug. Check back as new events are scraped.")

    return {
        "is_cv": True,
        "document_type": parsed.get("document_type", "cv"),
        "message": msg,
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
# BILLING + PRIVACY ROUTES
# ─────────────────────────────────────────────

@app.route("/api/upgrade", methods=["POST"])
def api_upgrade():
    if not BILLING_ON:
        return jsonify({"error": "Billing isn't set up yet."}), 503
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Enter your email to upgrade."}), 400
    import stripe
    stripe.api_key = STRIPE_SECRET
    base = PUBLIC_URL or request.host_url.rstrip("/")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        customer_email=email,
        success_url=f"{base}/?upgraded=1",
        cancel_url=f"{base}/?upgraded=0")
    return jsonify({"url": session.url})


@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_SECRET:
        return "", 503
    import stripe
    stripe.api_key = STRIPE_SECRET
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception as e:
        return f"bad webhook: {e}", 400
    if event.get("type") == "checkout.session.completed":
        sess  = event["data"]["object"]
        email = sess.get("customer_email") or (sess.get("customer_details") or {}).get("email")
        if email:
            set_premium(email)
    return "", 200


@app.route("/api/forget", methods=["POST"])
def api_forget():
    """GDPR: delete everything tied to this browser (and optional email)."""
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    uid   = request.cookies.get("oc_uid")
    conn  = _db()
    if conn:
        try:
            with conn, conn.cursor() as cur:
                if uid:
                    cur.execute("DELETE FROM analyses WHERE uid=%s", (uid,))
                if email:
                    cur.execute("DELETE FROM premium WHERE email=%s", (email,))
        finally:
            conn.close()
        return jsonify({"ok": True})
    if uid and UPLOADS_FILE.exists():
        kept = [l for l in UPLOADS_FILE.read_text().splitlines()
                if l.strip() and json.loads(l).get("uid") != uid]
        UPLOADS_FILE.write_text(("\n".join(kept) + "\n") if kept else "")
    _mem_counts.pop(uid, None)
    if email and PREMIUM_FILE.exists():
        try:
            d = json.loads(PREMIUM_FILE.read_text())
            d.pop(email, None)
            PREMIUM_FILE.write_text(json.dumps(d))
        except Exception:
            pass
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

# Runs under gunicorn too (the __main__ block below does not).
try:
    init_storage()
except Exception as e:
    log.warning(f"init_storage skipped: {e}")

# Warm start: if there's no event cache yet, kick off a scrape so the AI
# landing page has data on a fresh deploy (gunicorn skips __main__).
if not EVENTS_FILE.exists():
    threading.Thread(target=run_scrape_background, daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t = threading.Thread(target=run_scrape_background, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
