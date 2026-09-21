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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, make_response, redirect, render_template, request
from markupsafe import escape
from security import init_security
import ai_engine
import auth
import db
import digest
import mailer
import rag

log = logging.getLogger("orbitcast.web")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # headroom for admin example-doc uploads
limiter = init_security(app)

# Registers the Google OIDC client and gives Flask a secret_key, which Authlib
# needs to keep the OAuth state and nonce across the redirect. No-ops with a
# warning when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset, so a deploy
# without them still serves the site and still signs people in by magic link.
auth.init_google(app)

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

# Three tiers, most-live first:
#
#   1. the database  - survives deploys, shared by every process, always the
#                      last real scrape
#   2. the JSON file - same box, same boot; written by every scrape, and the
#                      whole story when no DATABASE_URL is configured
#   3. empty         - nothing to serve yet
#
# Tier 3 was the entire cold-boot experience before the database tier existed.
# events_cache.json is gitignored, so it is NOT shipped with the deploy, and
# Railway's filesystem is ephemeral - which means a fresh container genuinely
# has no catalog at all, and every visitor for the first minute of a deploy
# got an empty list. The database tier is what removes that window.

# The live catalog, held in memory. load_events_cache() is called on the hot
# path of several routes, so it must not hit the database on every request -
# it resolves the tiers once and then serves from here. The refresh loop
# replaces this wholesale on each scrape, so "cached" never means "stale":
# it's the same object the last scrape produced.
_catalog = None
_catalog_lock = threading.Lock()


def load_events_cache():
    global _catalog
    if _catalog is not None:
        return _catalog
    with _catalog_lock:
        if _catalog is not None:                    # filled while we waited
            return _catalog
        cached = db.get_events_cache()
        if cached and cached.get("events"):
            _catalog = cached
        elif EVENTS_FILE.exists():
            _catalog = json.loads(EVENTS_FILE.read_text())
        else:
            _catalog = {"events": [], "summary": [], "last_run": None}
        return _catalog

def load_last_run():
    return {"last_run": load_events_cache().get("last_run")}

def save_events_cache(events, summary, last_run=None):
    global _catalog
    # last_run means "when the catalog was last SCRAPED". Editing the catalog
    # in place - adding or removing a single event from the admin portal - is
    # not a scrape, and stamping it as one would tell the dashboard the sources
    # were checked when they weren't. Those callers pass the existing value
    # through; a real scrape leaves it None and gets a fresh stamp.
    data = {
        "events":   events,
        "summary":  summary,
        "last_run": last_run or datetime.now(timezone.utc).isoformat(),
    }
    # Serve the new catalog immediately - a single assignment, so a request
    # arriving mid-write gets the old catalog or the new one, never a
    # half-built list.
    _catalog = data
    # Database first - it's the only copy that outlives this container. The
    # local file is still written either way: it costs nothing and it is the
    # whole catalog for a deployment with no DATABASE_URL configured.
    db.save_events_cache(data)
    try:
        EVENTS_FILE.write_text(json.dumps(data, indent=2))
        LAST_RUN_FILE.write_text(json.dumps({"last_run": data["last_run"]}))
    except OSError as e:
        # a read-only or full filesystem must not lose a scrape that already
        # landed in the database
        log.warning(f"Local cache write failed (database copy stands): {e}")

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
        from scraper import (SOURCES, event_id, HACKATHON_RE, is_london,
                             norm_title, to_iso_date, harvest_luma_hosts,
                             collapse_series, discover_luma_sources,
                             add_source_titles)
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
                # Trim recurring series to their next couple of dates - one
                # calendar's weekly co-working slot is otherwise 16 of the
                # rows in this catalog. Done HERE, after the London filter,
                # so the instances kept are London ones.
                enriched = collapse_series(enriched, keep=2)
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

        # The same event legitimately arrives from more than one source - a
        # Luma hackathon is listed on both the host's calendar and the
        # community's, and aggregator feeds overlap constantly. event_id is
        # a hash of title+url, so identical events collide by design; keep
        # the first and drop the rest. ai_engine.build_compact_events()
        # already did this for the AI path, so only the browse list and the
        # calendar were ever showing the duplicates.
        deduped, seen_ids = [], set()
        for ev in all_events:
            if ev["id"] in seen_ids:
                continue
            seen_ids.add(ev["id"])
            deduped.append(ev)

        # Co-hosted Luma events, before anything title-shaped is attempted.
        #
        # One Luma event listed by three co-hosts is one event with ONE api_id,
        # however many organiser feeds surface it. That id is a far stronger
        # key than title+date - it survives a host renaming their copy, and it
        # does not need a resolvable date, so it also catches the co-hosted
        # events that the title+date pass below has to skip. The richest copy
        # wins for the same reason it does there: as_completed order is not
        # stable between runs, so "first one seen" is not a decision.
        def _luma_richness(ev):
            return sum(1 for k in ("date", "time", "location", "url", "description")
                       if ev.get(k))

        by_luma, luma_free = {}, []
        for ev in sorted(deduped, key=_luma_richness, reverse=True):
            lid = ev.get("luma_id")
            if not lid:
                luma_free.append(ev)
                continue
            if lid not in by_luma:
                by_luma[lid] = ev
        deduped = list(by_luma.values()) + luma_free

        # That id only catches BYTE-identical listings, which is the easy half.
        # The hard half is the same hackathon on Devpost, Eventbrite, Hackathon
        # Atlas and a Luma calendar at once, under four different URLs - the
        # hash differs every time, so all four used to survive. Match on
        # normalised title + resolved date instead.
        #
        # Requiring a date is deliberate. Without one, two genuinely different
        # instances of a recurring series would collapse into one, which loses
        # real events; a duplicate that slips through is the cheaper mistake.
        # Records are ranked so the survivor is the richest one available
        # rather than whichever thread happened to finish first - as_completed
        # order is not stable between runs.
        def richness(ev):
            return sum(1 for k in ("date", "time", "location", "url") if ev.get(k))

        # Aggregators rewrite titles, so the same event can arrive under two
        # different names and match on neither. Attach the organiser's own
        # title as an alias FIRST, then dedupe against every name an event is
        # known by - otherwise a title-based key cannot see the collision.
        add_source_titles(deduped)

        by_key, keyless, kept = {}, [], []
        for ev in sorted(deduped, key=richness, reverse=True):
            iso = to_iso_date(ev.get("date"))
            if not iso:
                keyless.append(ev)
                continue
            names = [ev.get("title", "")] + list(ev.get("aliases") or [])
            keys = {(norm_title(n), iso) for n in names if n}
            if any(k in by_key for k in keys):
                continue                       # already have it under some name
            kept.append(ev)
            for k in keys:
                by_key[k] = ev
        deduped = kept + keyless

        dupes = len(all_events) - len(deduped)
        all_events = deduped

        summary_data.sort(key=lambda x: x["source"])
        save_events_cache(all_events, summary_data)
        log.info(f"Scrape done: {len(all_events)} events ({dupes} cross-source duplicates removed)")

        # Save the catalog FIRST, then go looking for what's missing from it.
        # Discovery makes extra HTTP calls and is the newest code here, so it
        # runs after the thing everyone depends on is already stored, inside
        # its own try - a discovery failure must never cost a good scrape.
        try:
            # Two passes, because they reach different things. The first walks
            # events we already ingest, so it can only ever find organisers our
            # existing sources surface. The second goes outside the catalog
            # entirely - London's whole city feed, plus the past events of
            # hosts we can see, which is the only route that reaches an
            # organiser with nothing scheduled right now.
            candidates = harvest_luma_hosts(all_events)
            seen = {c["identifier"] for c in candidates}
            candidates += [c for c in discover_luma_sources()
                           if c["identifier"] not in seen]
            recorded = db.record_source_candidates(candidates)
            if candidates:
                log.info(f"Source discovery: {len(candidates)} untracked host "
                         f"calendars ({recorded} recorded)")
        except Exception as e:
            log.warning(f"Source discovery failed (catalog is unaffected): {e}")
    except Exception as e:
        log.error(f"Background scrape failed: {e}")
    finally:
        _scraping = False

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# PUBLIC PAYLOAD HYGIENE
# ─────────────────────────────────────────────
# Which ~45 places this catalog is assembled from is the one part of it that
# took real work and that nobody can re-derive by reading the app. Events
# keep their `source` everywhere inside the process - the scraper dedups on
# it, the AI scorer reads it, the admin dashboard reports on it, the DB
# stores it - but nothing served to an anonymous browser carries it, and the
# per-source breakdown is an admin view now, not a public endpoint.
#
# Note what this does NOT do: every event card still links to the event's own
# page, so the destination domain is visible on click. This closes the front
# door - it hands nobody a ready-made source list - it does not make the
# catalog unscrapeable, and it should not be mistaken for that.
# emoji goes with it: emoji is assigned per source, not per category, so a
# card showing 🛡️ against one event and 🎥 against another hands back a
# stable per-source token - a source list with the names filed off.
_PRIVATE_EVENT_FIELDS = ("source", "emoji")


def _public_event(ev):
    return {k: v for k, v in ev.items() if k not in _PRIVATE_EVENT_FIELDS}


def _public_result(result):
    """An /api/analyze result with the source stripped off each match.

    Copies rather than mutates: the caller still persists the full rows, and
    save_recommendations() writes the source the admin analytics reads."""
    recs = result.get("recommendations")
    if not isinstance(recs, list):
        return result
    return {**result, "recommendations": [_public_event(r) for r in recs]}


@app.route("/api/events")
def api_events():
    cache    = load_events_cache()
    category = request.args.get("category", "").strip()
    events   = cache.get("events", [])
    if category:
        events = [e for e in events if e.get("category","").lower() == category.lower()]
    # The ?source= filter is gone on purpose: a public filter on a hidden
    # field is an oracle - you can enumerate the source list by guessing at
    # it, which defeats stripping the field in the first place.
    return jsonify({"events": [_public_event(e) for e in events], "total": len(events),
                    "last_run": cache.get("last_run"), "scraping": _scraping})

@app.route("/api/summary")
def api_summary():
    """Per-source counts - the coverage view. Admin-only, same X-Admin-Secret
    as every other /api/admin route, and fails closed the same way: with no
    ADMIN_SECRET set on the server, nobody gets this, including you."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
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

# ─────────────────────────────────────────────
# ACCOUNTS
# ─────────────────────────────────────────────
# The AI is behind a login from here on. The trade-off is real and worth
# naming: every visitor now has to sign up before seeing what the engine
# does, which costs conversions. What it buys is a durable profile per
# person, an email address to send digests to, and one identity across
# devices instead of a cookie that dies with the browser.


@app.route("/api/auth/request", methods=["POST"])
@limiter.limit("10 per hour")
def api_auth_request():
    """Emails a magic link. The response is deliberately identical whether or
    not the address has an account - anything else turns this endpoint into a
    way to test who has signed up."""
    email = auth.normalise_email((request.get_json(silent=True) or {}).get("email"))
    generic = {"ok": True, "message": "Check your email for a sign-in link."}
    if not email:
        return jsonify({"error": "That does not look like an email address."}), 400
    if not db.is_configured():
        return jsonify({"error": "Accounts are not available right now."}), 503

    raw = auth.issue_login_token(email)
    if raw:
        mailer.send_login_link(email, raw)
        if not mailer.is_configured():
            # Local development: with no mail provider the link is logged, so
            # say so rather than letting someone wait for mail that will
            # never arrive.
            log.warning("Mail is not configured - the sign-in link was logged, not sent.")
    return jsonify(generic)


@app.route("/auth/verify", methods=["GET", "POST"])
@limiter.limit("30 per hour")
def auth_verify():
    """The magic-link target. GET confirms, POST signs in.

    The interstitial is not ceremony. Enterprise mail security rewrites and
    PRE-FETCHES every link in an incoming email (Outlook Safe Links, Proofpoint
    URL Defense, Mimecast). The token here is single-use by design, so a
    scanner that fetches it first BURNS it - and the person clicks their own
    sign-in link and is told it has expired. That failure is silent, blames
    the user, and is close to impossible to diagnose from support emails.

    This matters more than usual for this audience: government, defence and
    security organisations are exactly where aggressive link scanning is
    standard. A scanner cannot submit the form, so the token survives until a
    human clicks.

    The token is never rendered into readable page text or a link - only into
    the form action - so it does not leak by referrer or over someone's
    shoulder in a screenshot."""
    token = request.args.get("token", "") or (request.form.get("token", "") if request.form else "")
    if not token:
        return redirect("/?signin=expired")

    if request.method == "GET":
        return (f"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<body style=\"background:#0b1220;color:#e8eefc;font-family:system-ui,sans-serif;"
                f"padding:48px;line-height:1.6;\">"
                f"<h1 style='font-size:20px;margin:0 0 .5rem;'>Sign in to OrbitCast</h1>"
                f"<p style='color:#93a3c0;margin:0 0 1.25rem;'>One more click and you are in.</p>"
                f"<form method='post' action='/auth/verify'>"
                f"<input type='hidden' name='token' value='{escape(token)}'>"
                f"<button style=\"background:#e8eefc;color:#06101f;border:none;border-radius:999px;"
                f"padding:12px 26px;font-size:15px;font-weight:600;cursor:pointer;\">"
                f"Sign in</button></form></body>", 200)

    oc_uid = request.cookies.get("oc_uid") or uuid.uuid4().hex
    account = auth.redeem_login_token(token, oc_uid)
    if not account:
        return redirect("/?signin=expired")
    resp = make_response(redirect("/?signin=ok"))
    auth.set_session_cookie(resp, account)
    return resp


@app.route("/auth/login")
@limiter.limit("30 per hour")
def auth_google_login():
    """Hands off to Google's consent screen.

    A plain redirect, not JSON: OAuth is a browser navigation, and doing it
    from fetch() would be blocked as a cross-origin redirect. So the button in
    the UI is a real link to this path."""
    if not auth.google_enabled():
        # Honest about which thing is missing. Silently falling back to the
        # email form would leave someone clicking a Google button that appears
        # to do nothing.
        return redirect("/?signin=google_off")
    redirected = auth.google_redirect()
    if redirected is None:
        return redirect("/?signin=google_off")
    return redirected


@app.route("/auth/callback")
@limiter.limit("30 per hour")
def auth_google_callback():
    """Where Google sends the browser back. Exchanges the code, resolves the
    identity to an account, and sets the same signed session cookie the magic
    link sets - from here on the two paths are indistinguishable.

    Note it reads oc_uid from the cookie rather than calling ensure_oc_uid():
    this response is a redirect whose cookies are about to be overwritten by
    set_session_cookie() anyway, and a first-ever visitor who arrives straight
    at a Google login has no oc_uid yet, so one is minted here for the new
    account to adopt."""
    if not auth.google_enabled():
        return redirect("/?signin=google_off")

    identity = auth.google_identity()
    if not identity:
        # Covers a cancelled consent screen, an expired state and a real token
        # error alike. All of them mean "not signed in", and none of them are
        # worth a different page.
        return redirect("/?signin=failed")

    if not db.is_configured():
        return redirect("/?signin=unavailable")

    oc_uid = request.cookies.get("oc_uid") or uuid.uuid4().hex
    account = db.get_or_create_account_by_google(
        identity["sub"], identity["email"], identity["name"],
        identity["picture"], oc_uid, identity["email_verified"])
    if not account:
        return redirect("/?signin=unavailable")

    resp = make_response(redirect("/?signin=ok"))
    auth.set_session_cookie(resp, account)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    resp = make_response(jsonify({"ok": True}))
    auth.clear_session_cookie(resp)
    return resp


@app.route("/api/me")
def api_me():
    """One call for the whole signed-in state: who, and what alerts they get."""
    account = auth.current_account()
    if not account:
        # google_enabled is reported signed-out as well as signed-in: it is
        # what decides whether the sign-in panel renders the Google button,
        # and that decision is needed precisely when nobody is signed in.
        return jsonify({"signed_in": False, "google_enabled": auth.google_enabled()})
    prefs = db.get_digest_prefs(account["id"]) or {}
    has_profile = bool(db.get_latest_analysis(account["oc_uid"]))
    return jsonify({
        "signed_in": True,
        "google_enabled": auth.google_enabled(),
        "email": account["email"],
        "name": account.get("name"),
        "avatar_url": account.get("avatar_url"),
        "has_profile": has_profile,
        "alerts": {
            "configured": bool(prefs),
            "frequency": prefs.get("frequency", "weekly"),
            "interval_days": prefs.get("interval_days", 3),
            "weekday": prefs.get("weekday", 0),
            "send_hour": prefs.get("send_hour", 8),
            "timezone": prefs.get("timezone", digest.DEFAULT_TZ),
            "paused": prefs.get("paused", True),
            "next_send_at": prefs["next_send_at"].isoformat() if prefs.get("next_send_at") else None,
        },
    })


@app.route("/api/digest/prefs", methods=["POST"])
@auth.login_required
@limiter.limit("60 per hour")
def api_digest_prefs():
    """Saves the alert schedule and computes the next send.

    Storing the profile is what makes matching possible, so enabling alerts
    records consent for it - the UI says this in as many words next to the
    control. Without that, a subscriber's digest would silently never have a
    profile to match against."""
    account = auth.current_account()
    if not account:
        return jsonify({"error": "Sign in first."}), 401

    clean, err = digest.validate_prefs(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400

    existing = db.get_digest_prefs(account["id"])
    unsub = existing["unsub_token"] if existing else uuid.uuid4().hex
    next_at = None if clean["paused"] else digest.compute_next_send(clean)

    if not clean["paused"] and db.get_consent(account["oc_uid"]) is not True:
        db.set_consent(account["oc_uid"], True)

    saved = db.upsert_digest_prefs(account["id"], clean["frequency"], clean["interval_days"],
                                    clean["weekday"], clean["send_hour"], clean["timezone"],
                                    clean["paused"], next_at, unsub)
    if not saved:
        return jsonify({"error": "Could not save that."}), 503
    return jsonify({"ok": True,
                     "next_send_at": next_at.isoformat() if next_at else None,
                     "paused": clean["paused"]})


def _plain_page(heading: str, sub: str = "", extra: str = "") -> str:
    return (f"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<body style=\"background:#0b1220;color:#e8eefc;font-family:system-ui,sans-serif;"
            f"padding:48px;line-height:1.6;\"><h1 style='font-size:20px;margin:0 0 .5rem;'>{heading}</h1>"
            f"<p style='color:#93a3c0;margin:0 0 1.25rem;'>{sub}</p>{extra}"
            f"<p><a style='color:#6ea8fe;' href='/'>Back to OrbitCast</a></p></body>")


@app.route("/unsubscribe", methods=["GET", "POST"])
@limiter.limit("60 per hour")
def unsubscribe():
    """No login required - an unsubscribe that demands a sign-in is an
    unsubscribe that does not work.

    POST must be accepted, not just GET: the List-Unsubscribe-Post header this
    app sends tells Gmail and Outlook to POST here when someone uses the mail
    client's own unsubscribe button. A GET-only route answers that with 405,
    and the native button silently fails.

    And GET must NOT act on its own. Corporate mail security (Safe Links,
    Proofpoint, Mimecast) fetches every URL in an incoming email to check it.
    A GET that unsubscribes would be triggered by the scanner before the
    person ever saw the message - so GET asks, POST acts."""
    token = request.args.get("t", "") or (request.form.get("t", "") if request.form else "")
    if request.method == "GET":
        if not token:
            return _plain_page("That unsubscribe link is not valid."), 200
        form = (f"<form method='post' action='/unsubscribe?t={token}'>"
                f"<button style=\"background:#e8eefc;color:#06101f;border:none;border-radius:999px;"
                f"padding:12px 22px;font-size:15px;font-weight:600;cursor:pointer;\">"
                f"Turn my alerts off</button></form>")
        return _plain_page("Turn off OrbitCast alerts?",
                           "One click and we stop emailing you. You can turn them back on "
                           "any time from your account.", form), 200

    email = db.pause_digest_by_token(token)
    if not email:
        return _plain_page("That unsubscribe link is not valid.",
                           "It may already have been used."), 200
    return _plain_page("Your OrbitCast alerts are off.",
                       "Nothing else changes - your account and profile stay as they are."), 200


@app.route("/api/analyze", methods=["POST"])
@auth.login_required
@limiter.limit("20 per hour")
def api_analyze():
    file = request.files.get("file")
    # NUL (0x00) can't be stored in a Postgres text column at all - the
    # insert raises, not just renders oddly. extract_text() already strips
    # it from file-derived text; pasted text skips that function entirely
    # (it comes straight from the form field), so it needs its own strip
    # here or a stray NUL in a paste silently breaks persistence exactly
    # the same way a bad PDF extraction did.
    pasted_text = (request.form.get("text") or "").replace("\x00", "").strip()

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

    resp = jsonify(_public_result(result))
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
            resp = jsonify(_public_result(result))  # rebuild - recs now carry recommendation_id for tracking
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
@auth.login_required
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

    return jsonify(_public_result(result))


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
    """GDPR delete. Now also removes the account, which cascades to the alert
    schedule and the send log - otherwise someone who asked to be forgotten
    would keep receiving email, which is the version of this bug that gets
    reported to a regulator rather than to us."""
    oc_uid = request.cookies.get("oc_uid")
    if oc_uid:
        db.delete_account(oc_uid)
        db.forget(oc_uid)
    resp = jsonify({"ok": True})
    resp.set_cookie("oc_uid", "", expires=0)
    auth.clear_session_cookie(resp)
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


@app.route("/api/admin/source-candidates", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_source_candidates():
    """Organisers the scraper found hosting events we ingest but that aren't
    sources yet - i.e. the coverage gaps, which nothing used to report.

    ?status=new (default) | added | ignored | all"""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    status = request.args.get("status", "new")
    return jsonify({"candidates": db.list_source_candidates(
        status=None if status == "all" else status)})


@app.route("/api/admin/source-candidates/<identifier>", methods=["POST"])
@limiter.limit("120 per hour")
def api_admin_set_source_candidate(identifier):
    """Accept ('added') or reject ('ignored') a harvested lead. 'ignored'
    persists across re-harvests, so a rejected calendar stays rejected."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    status = (request.get_json(silent=True) or {}).get("status", "")
    if status not in ("new", "added", "ignored"):
        return jsonify({"error": "status must be new, added or ignored"}), 400
    if not db.set_source_candidate_status(identifier, status):
        return jsonify({"error": "Unknown candidate"}), 404
    return jsonify({"ok": True, "identifier": identifier, "status": status})


def _manual_event_urls():
    """Normalised urls of every event the operator has already added by link.

    The duplicate rule needs to tell "this clashes with a scraped event" apart
    from "you added this yourself" - the second is an update, not a refusal.
    Empty on any database problem, which degrades to the old behaviour rather
    than blocking an add."""
    import link_intake
    try:
        return {link_intake._norm_url(r.get("url") or "")
                for r in db.get_active_manual_events() if r.get("url")}
    except Exception as e:
        log.warning(f"Could not read added-by-link urls: {e}")
        return set()


@app.route("/api/admin/links/check", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_check_link():
    """Dry run: what would happen if this link were added.

    Fetches the page, resolves it through an aggregator if needed, extracts the
    event, and runs every catalog rule - without writing anything. This is what
    replaces asking a human to eyeball each link."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste an event link first."}), 400
    import link_intake
    try:
        verdict = link_intake.verify_link(url, load_events_cache().get("events", []),
                                          manual_urls=_manual_event_urls())
    except Exception as e:
        log.warning(f"Link check failed for {url}: {e}")
        return jsonify({"error": f"Could not check that link: {e}"}), 500
    return jsonify(verdict)


@app.route("/api/admin/links", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_add_link():
    """Verify a link and, if it passes, add it to the catalog.

    `force` adds despite failed checks - deliberately explicit, and recorded in
    the stored verdict, so a forced entry is distinguishable later from one
    that genuinely passed. `overrides` lets the operator supply what the page
    didn't state (usually a date) rather than the system inventing it."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste an event link first."}), 400
    import link_intake
    manual_urls = _manual_event_urls()
    try:
        verdict = link_intake.verify_link(url, load_events_cache().get("events", []),
                                          manual_urls=manual_urls)
    except Exception as e:
        log.warning(f"Link check failed for {url}: {e}")
        return jsonify({"error": f"Could not check that link: {e}"}), 500

    event = verdict.get("event")
    if not event:
        return jsonify({"ok": False, "added": False, "verdict": verdict,
                         "error": "Nothing usable could be read from that page."}), 422

    # Operator-supplied corrections are applied BEFORE the pass/fail decision is
    # re-made, so filling in a missing date turns a failing link into a passing
    # one rather than needing a force.
    overrides = data.get("overrides") or {}
    for field in ("title", "date", "time", "location", "category", "description"):
        val = (overrides.get(field) or "").strip() if isinstance(overrides.get(field), str) else None
        if val:
            event[field] = val
    if overrides.get("category") in link_intake.CATEGORIES:
        event["emoji"] = link_intake.CATEGORY_EMOJI.get(overrides["category"], "📌")
    # Re-decide on ANY field a correction can move, not just the date. Fixing a
    # wrong location used to leave the original "London or online" failure in
    # place, so the operator had to force past a check they had just satisfied.
    if any((overrides.get(f) or "").strip()
           for f in ("date", "location", "title") if isinstance(overrides.get(f), str)):
        verdict = link_intake.reverify(verdict, event,
                                       load_events_cache().get("events", []),
                                       manual_urls=manual_urls)

    forced = bool(data.get("force"))
    if not verdict.get("ok") and not forced:
        failed = [c["check"] for c in verdict.get("checks", []) if not c["ok"]]
        return jsonify({"ok": False, "added": False, "verdict": verdict,
                         "error": "Did not pass: " + ", ".join(failed)}), 422

    verdict["forced"] = forced
    verdict["added_at"] = datetime.now(timezone.utc).isoformat()
    event["source_label"] = (data.get("source_label") or "").strip() or None
    if not db.upsert_manual_event(event, verdict=verdict,
                                   added_by=request.headers.get("X-Admin-User")):
        return jsonify({"ok": False, "added": False, "verdict": verdict,
                         "error": "Could not save it - is DATABASE_URL configured?"}), 503

    # Serve it immediately rather than making the operator wait up to
    # REFRESH_MINUTES to see whether it worked. The next scrape re-emits it
    # from the database anyway; this only closes the gap until then.
    _inject_manual_event(event)
    # Forcing past the geography check adds the event, and the next scrape then
    # drops it again: _scrape_manual re-emits every active row, and the catalog
    # build runs is_london() over the lot. Saying so is the difference between
    # a documented override and an event that quietly disappears in half an
    # hour with no explanation.
    warning = None
    if forced and any(c["check"] == "London or online" and not c["ok"]
                      for c in verdict.get("checks", [])):
        warning = ("Added, but it is not London or online. The catalog rebuild "
                   "applies that rule to every source, so this will drop back "
                   "out at the next refresh. Correct the location instead if it "
                   "really is a London event.")
    return jsonify({"ok": True, "added": True, "forced": forced,
                     "updated": bool(verdict.get("already_added")),
                     "warning": warning,
                     "verdict": verdict, "event": event})


def _inject_manual_event(event):
    """Put a just-added event into the in-memory catalog straight away.

    Mirrors what the scrape does: same id scheme, same category/emoji stamping.

    It REPLACES any existing entry for the same url rather than skipping when
    the id already exists. The id is a hash of title+url, so resubmitting a
    link with a corrected title produced a different id, sailed past the
    "already present" guard, and left the catalog holding the event twice -
    under the old title and the new one - until the next scrape rebuilt it.
    The database row was always keyed on the url alone; this now matches it."""
    try:
        import link_intake
        from scraper import event_id
        cache = load_events_cache()
        url = link_intake._norm_url(event.get("url", ""))
        events = [e for e in cache.get("events", [])
                  if link_intake._norm_url(e.get("url") or "") != url]
        new = {**event,
               "id": event_id(event.get("title", ""), event.get("url", "")),
               "source": event.get("source_label") or f"Added — {event.get('category')}",
               "emoji": event.get("emoji", "📌")}
        save_events_cache(events + [new], cache.get("summary", []),
                          last_run=cache.get("last_run"))
    except Exception as e:
        log.warning(f"Could not inject manual event into live catalog: {e}")


@app.route("/api/admin/links", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_list_links():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    include_removed = request.args.get("include_removed") == "1"
    return jsonify({"events": db.list_manual_events(include_removed=include_removed)})


@app.route("/api/admin/links/remove", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_remove_link():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    import link_intake
    raw = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    # Rows are keyed on the NORMALISED url, because that is what the add path
    # stores - trailing slash and tracking params stripped. Removing by the
    # link as originally pasted therefore missed the row and answered "Unknown
    # event". Normalise the same way here, and still accept the raw string so
    # a row written before this existed is also reachable.
    url = link_intake._norm_url(raw)
    removed = db.set_manual_event_status(url, "removed")
    if not removed and raw and raw != url:
        removed, url = db.set_manual_event_status(raw, "removed"), raw
    if not removed:
        return jsonify({"error": "Unknown event"}), 404
    # Drop it from the in-memory catalog too, so it disappears immediately
    # rather than lingering until the next scrape rebuilds without it. Compare
    # normalised on both sides - the catalog copy came from the scrape, whose
    # url may differ from the stored key by exactly that trailing slash.
    try:
        cache = load_events_cache()
        remaining = [e for e in cache.get("events", [])
                     if link_intake._norm_url(e.get("url") or "") != url]
        save_events_cache(remaining, cache.get("summary", []),
                          last_run=cache.get("last_run"))
    except Exception as e:
        log.warning(f"Could not drop removed event from live catalog: {e}")
    return jsonify({"ok": True, "url": url})


@app.route("/api/admin/sources", methods=["GET"])
@limiter.limit("120 per hour")
def api_admin_list_sources():
    """Luma organisers approved from the dashboard. The hardcoded ones in
    scraper.py are not listed - they are not editable from here."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"sources": db.list_luma_sources()})


@app.route("/api/admin/sources/verify", methods=["POST"])
@limiter.limit("120 per hour")
def api_admin_verify_source():
    """Dry run for an organiser: already tracked? real upcoming London events?
    a category we host? Writes nothing."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    identifier, name, kind, parse_error = _parse_organiser_input(data)
    if not identifier:
        return jsonify({"error": parse_error or "Give a Luma organiser link or id."}), 400
    import link_intake
    try:
        return jsonify(link_intake.verify_organiser(identifier, name, kind))
    except Exception as e:
        log.warning(f"Organiser verify failed for {identifier}: {e}")
        return jsonify({"error": f"Could not check that organiser: {e}"}), 500


@app.route("/api/admin/sources", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_add_source():
    """Verify an organiser and, if it passes, start scraping it every refresh.

    No deploy involved: the DB-backed Luma sources read this table at scrape
    time, so a source approved here feeds from the next refresh onward."""
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    identifier, name, kind, parse_error = _parse_organiser_input(data)
    if not identifier:
        return jsonify({"error": parse_error or "Give a Luma organiser link or id."}), 400
    import link_intake
    try:
        verdict = link_intake.verify_organiser(identifier, name, kind)
    except Exception as e:
        log.warning(f"Organiser verify failed for {identifier}: {e}")
        return jsonify({"error": f"Could not check that organiser: {e}"}), 500

    src = verdict.get("source")
    forced = bool(data.get("force"))
    if not verdict.get("ok") and not forced:
        failed = [c["check"] for c in verdict.get("checks", []) if not c["ok"]]
        return jsonify({"ok": False, "added": False, "verdict": verdict,
                         "error": "Did not pass: " + ", ".join(failed)}), 422
    if not src:
        # Forced past a failure that ran before a category was established -
        # there is nothing to store, so a category has to be supplied.
        category = (data.get("category") or "").strip()
        if category not in link_intake.CATEGORIES:
            return jsonify({"ok": False, "added": False, "verdict": verdict,
                             "error": "Forcing this one needs an explicit category."}), 422
        src = {"identifier": identifier, "kind": kind, "name": name or identifier,
               "category": category,
               "emoji": link_intake.CATEGORY_EMOJI.get(category, "🟣")}

    verdict["forced"] = forced
    if not db.upsert_luma_source(src["identifier"], src["kind"], src["name"],
                                  src["category"], emoji=src.get("emoji"),
                                  verdict=verdict,
                                  added_by=request.headers.get("X-Admin-User")):
        return jsonify({"ok": False, "added": False, "verdict": verdict,
                         "error": "Could not save it - is DATABASE_URL configured?"}), 503
    # Mark the matching lead reviewed, so an organiser accepted here stops
    # reappearing at the top of the candidates list on the next harvest.
    try:
        db.set_source_candidate_status(src["identifier"], "added")
    except Exception:
        pass
    return jsonify({"ok": True, "added": True, "forced": forced,
                     "verdict": verdict, "source": src})


@app.route("/api/admin/sources/status", methods=["POST"])
@limiter.limit("60 per hour")
def api_admin_set_source_status():
    if not _admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    status = (data.get("status") or "").strip()
    if not db.set_luma_source_status(identifier, status):
        return jsonify({"error": "Unknown source, or bad status "
                                  "(use active or paused)."}), 404
    return jsonify({"ok": True, "identifier": identifier, "status": status})


def _parse_organiser_input(data):
    """Whatever the operator pasted -> (identifier, name, kind, error).

    This used to be a handful of regexes that between them covered cal- ids,
    usr- ids and a /u/ URL pattern Luma does not actually emit. Everything else
    fell through to a bare-token match that a full URL can never satisfy, so a
    real profile link (luma.com/user/usr-XXXX) or a vanity calendar link
    (luma.com/londonai) came back empty and was reported as "give me a cal-
    id" - which is how organisers with no calendar id ended up simply not being
    addable, and their events missing from the catalog.

    scraper.resolve_luma_identifier does the real work, including reading the
    page when the URL carries no id. It always returns a canonical cal-/usr-
    id, which is also what makes the duplicate check meaningful: a vanity URL
    and its calendar id are now one identifier rather than two."""
    raw = (data.get("identifier") or data.get("url") or "").strip()
    name = (data.get("name") or "").strip()
    if not raw:
        return "", name, "calendar", "Paste a Luma organiser link or id."
    from scraper import resolve_luma_identifier
    identifier, kind, resolved_name, error = resolve_luma_identifier(raw)
    if not identifier:
        return "", name, kind or "calendar", error
    # Luma's own name for the calendar beats a blank box, and loses to anything
    # the operator typed - they are naming the source, not the calendar.
    return identifier, (name or resolved_name), kind, ""


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
    meta = db.get_config_meta(ai_engine.CONFIG_KEY_SCORING_RULES)
    custom = meta["value"] if meta else None
    return jsonify({
        "rules": custom if custom else ai_engine.DEFAULT_SCORING_RULES,
        "is_custom": bool(custom),
        "default_rules": ai_engine.DEFAULT_SCORING_RULES,
        "updated_at": meta["updated_at"] if meta else None,
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
    days = request.args.get("days", type=int) or 30
    days = max(7, min(days, 180))
    payload = {"funnel": db.get_funnel_analytics()}
    payload.update(db.get_analytics_overview(days=days))
    payload["days"] = days
    return jsonify(payload)


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

# How often the catalog re-scrapes itself, in minutes. Events are announced
# and filled up during the day, so a catalog that only refreshes when someone
# redeploys is stale by definition. 30 minutes is well inside the rate limits
# of every source and still catches same-day announcements.
REFRESH_MINUTES = int(os.getenv("REFRESH_MINUTES", "30"))


def _refresh_loop():
    """Scrape at boot, then keep scraping on a timer for the process's life.

    Previously this was a one-shot thread: the catalog was only ever as fresh
    as the last deploy or the last time someone pressed Refresh by hand. A
    long-lived container could serve week-old events without anything looking
    wrong.

    Failures are swallowed by run_scrape_background itself, so a source going
    down delays nothing - the loop just tries again next interval, and the
    previous catalog stays served in the meantime."""
    while True:
        try:
            run_scrape_background()
        except Exception as e:                      # belt and braces
            log.error(f"Refresh loop iteration failed: {e}")
        if REFRESH_MINUTES <= 0:                    # opt out for local runs
            return
        time.sleep(REFRESH_MINUTES * 60)


# Runs on import, not just under `python app.py` - gunicorn imports this
# module directly and never hits the __main__ guard below. Without this,
# every deploy boots with an empty cache (Railway's filesystem is
# ephemeral) and nothing repopulates it until someone manually hits
# Refresh. Safe to run unconditionally: single gunicorn worker process,
# so this fires exactly once.
logging.basicConfig(level=logging.INFO)
threading.Thread(target=_refresh_loop, daemon=True).start()

# Email digests. Same single-worker reasoning as the refresh loop above, plus
# a database claim per send so a restart mid-run cannot double-email anyone.
digest.start_scheduler(
    catalog_fn=lambda: load_events_cache().get("events", []),
    score_fn=lambda profile, level, field, events: ai_engine.score_and_enrich(
        profile, level, field, events),
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
