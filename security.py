"""
ORBITCAST — Security hardening module
Add two lines to your app.py:

    from security import init_security
    limiter = init_security(app)

Then decorate your /api/refresh route:

    @app.route('/api/refresh', methods=['POST'])
    @limiter.limit("6 per hour")
    def refresh():
        ...
"""

import hmac
import logging
import os
import secrets

from flask import request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

log = logging.getLogger("orbitcast.security")

# ── CSRF ──────────────────────────────────────────────────────────────────────
# Double-submit cookie. The token is readable by our own JavaScript (so it can
# be echoed in a header) but not by another origin, because the same-origin
# policy stops a third-party page reading document.cookie here. An attacker can
# still make the browser *send* our session cookie cross-site; what they cannot
# do is read the token to put it in the header, which is the whole point.
#
# Stateless on purpose: nothing to store, nothing to migrate when rate-limit
# storage moves to Redis and the app eventually runs more than one worker.
CSRF_COOKIE = "oc_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS", "TRACE"))

# Paths under /api/ that are deliberately not CSRF-checked.
#   /api/admin/ is authenticated by the X-Admin-Secret header, not by a cookie.
#   A cross-site page cannot set that header and does not know the secret, so
#   these routes are not CSRF-reachable in the first place.
CSRF_EXEMPT_PREFIXES = ("/api/admin/",)

# Enforcement is on by default. Set OC_CSRF_ENFORCE=0 to log violations without
# rejecting them - useful for one deploy if a call site turns out to have been
# missed, since a blocked request looks to the user like a dead button.
CSRF_ENFORCE = os.environ.get("OC_CSRF_ENFORCE", "1") != "0"


def _csrf_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES)


# ── Bot protection ────────────────────────────────────────────────────────────
# Cloudflare Turnstile guards the two endpoints that cost real money per
# request: /api/analyze spends model tokens, /api/auth/email/request sends real
# email. Both are rate-limited per IP already, but an IP is cheap to rotate and
# neither limit stops a determined script from running up a bill.
#
# Ships inert: with no keys configured verify_turnstile() returns True, so this
# deploys as a no-op and is switched on by setting the two env vars.
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_S = 5


def turnstile_enabled() -> bool:
    return bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)


def verify_turnstile(token: str, remote_ip: str = None, *, fail_open: bool = False) -> bool:
    """True if the challenge passed, or if Turnstile is not configured.

    fail_open says what to do when Cloudflare itself is unreachable. For
    /api/analyze the answer is no - an outage there must not open the spend
    tap. For the email endpoint it is yes: the per-IP limiter still caps the
    damage, and silently breaking sign-in for everyone is the worse failure.
    """
    if not turnstile_enabled():
        return True
    if not token:
        return False
    try:
        import requests
        r = requests.post(
            TURNSTILE_VERIFY_URL,
            data={"secret": TURNSTILE_SECRET_KEY, "response": token,
                  **({"remoteip": remote_ip} if remote_ip else {})},
            timeout=TURNSTILE_TIMEOUT_S,
        )
        return bool(r.json().get("success"))
    except Exception as exc:
        log.warning("Turnstile verification unreachable (%s); %s.",
                    type(exc).__name__, "allowing" if fail_open else "refusing")
        return fail_open


def init_security(app):
    """
    Attach security headers, rate limiter, and hardened error handlers to app.
    Returns the Limiter instance so you can use @limiter.limit() on routes.
    """

    # ── Rate limiter ──────────────────────────────────────────────────────────
    def _client_key():
        """Signed-in visitors are counted per account, everyone else per
        address. A university, office or event Wi-Fi puts hundreds of people
        behind one public IP; keyed on IP alone they shared one allowance and
        a busy room got 429s on ordinary page loads. The account id comes from
        the signed session cookie (an HMAC check, no database)."""
        try:
            import auth
            aid = auth.current_session_id()
            if aid:
                return f"acct:{aid}"
        except Exception:
            pass
        return f"ip:{get_remote_address()}"

    _storage_uri = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    limiter = Limiter(
        app=app,
        key_func=_client_key,
        # A ceiling on every route that has no limit of its own. Per-route
        # limits are stricter where it matters. Generous on purpose: for
        # anonymous visitors this is per network address, which can be a
        # whole campus. Real client address: app.py wraps the WSGI app in
        # ProxyFix before this runs.
        default_limits=["3000 per hour", "300 per minute"],
        # In-process counters: correct for the single Gunicorn worker. With
        # more than one worker set RATELIMIT_STORAGE_URI to a redis:// URL.
        storage_uri=_storage_uri,
        # Do not swallow storage errors. Two of these limits - the shared
        # anon-daily and ai-daily scopes in app.py - are spend ceilings, not
        # politeness limits: if the counter store is unreachable we want the
        # request to fail, not to sail through and bill a model call. The
        # trade-off is that a Redis outage takes the site down rather than
        # making it expensive, which is the right way round.
        swallow_errors=False,
    )

    # The counters are only global if the store is. On memory:// every worker
    # keeps its own, so each limit is silently multiplied by the worker count
    # and the daily spend ceilings stop being ceilings at all.
    if _storage_uri.startswith("memory://"):
        log.warning(
            "Rate limiting is using in-process storage. Counters reset on every "
            "deploy and are per-worker. Set RATELIMIT_STORAGE_URI to a redis:// "
            "URL before raising the Gunicorn worker count above 1."
        )

    # ── CSRF ──────────────────────────────────────────────────────────────────
    @app.before_request
    def check_csrf():
        if request.method in CSRF_SAFE_METHODS:
            return None
        if not request.path.startswith("/api/") or _csrf_exempt(request.path):
            return None
        sent = request.headers.get(CSRF_HEADER, "")
        have = request.cookies.get(CSRF_COOKIE, "")
        if sent and have and hmac.compare_digest(sent, have):
            return None
        log.warning("CSRF check failed: %s %s (header=%s cookie=%s, enforcing=%s)",
                    request.method, request.path, bool(sent), bool(have), CSRF_ENFORCE)
        if not CSRF_ENFORCE:
            return None
        # 403, not 401: the session may be perfectly valid; it is this
        # particular request that cannot be trusted to have come from our page.
        return jsonify(error="Your session needs refreshing. Reload the page and try again.",
                       reason="csrf_failed"), 403

    # ── Security response headers ─────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
        # Issue the CSRF token on first contact so it is already in place
        # before anything is submitted. Not httponly - our own script has to
        # read it to echo it back in the header.
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(
                CSRF_COOKIE, secrets.token_urlsafe(32),
                max_age=60 * 60 * 24 * 30, httponly=False, samesite="Lax",
                secure=(request.is_secure
                        or request.headers.get("X-Forwarded-Proto") == "https"),
                path="/",
            )
        # Never let a browser serve a stale page or a stale catalog.
        #
        # Nothing here sent Cache-Control at all, and with no Cache-Control,
        # no Expires and no ETag, browsers fall back to HEURISTIC caching -
        # they invent their own freshness lifetime and reuse the response
        # without asking. That is why a deploy could go out, be confirmed live
        # by curl, and still show the previous version in a browser that had
        # visited before - including in a private window, which keeps its own
        # in-session cache.
        #
        # "no-cache" does not mean "do not store"; it means "revalidate before
        # reusing", which is exactly right for a page whose content changes on
        # deploy and a catalog that re-scrapes every 30 minutes. Applied only
        # to HTML and JSON - the two things this app actually serves and the
        # two that go stale. Anything else (a downloaded raw file) keeps
        # whatever it set for itself.
        if response.mimetype in ("text/html", "application/json"):
            response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        # Browsers remember to use HTTPS for a year, so a stripped first
        # request cannot downgrade the session. Only sent over HTTPS, as the
        # spec requires; harmless on localhost.
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Control referrer information
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy — lock down browser features.
        # geolocation=(self): the opt-in location feature (map + distance
        # to events) needs navigator.geolocation from this origin. It was
        # geolocation=() before, which blocks the API at the browser level
        # regardless of any in-page consent UI - that had to change first,
        # or the location consent banner would prompt for something the
        # browser silently refuses to ever grant.
        response.headers["Permissions-Policy"] = (
            "geolocation=(self), camera=(), microphone=(), payment=()"
        )
        # Content Security Policy
        # 'unsafe-inline' is needed for the inline <script> and <style> blocks.
        # To remove it: move all JS/CSS to external files and add a nonce.
        # unpkg.com is whitelisted for Leaflet (the map library) - map tiles
        # themselves are plain <img> requests, already covered by the
        # existing broad img-src https: allowance.
        # base-uri stops an injected <base> redirecting every relative URL on
        # the page; form-action stops an injected form posting elsewhere;
        # object-src kills the <object>/<embed> legacy plugin surface. All
        # three are cheap because nothing here uses any of them.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            # challenges.cloudflare.com is Turnstile: script-src for its
            # loader, frame-src for the iframe it mounts the challenge in.
            # Listed unconditionally even though the widget only renders when
            # TURNSTILE_SITE_KEY is set - a policy that varies by config is a
            # policy nobody can test.
            "script-src 'self' 'unsafe-inline' https://unpkg.com "
            "https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-src https://challenges.cloudflare.com; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none';"
        )
        # Remove server fingerprinting (hides "Werkzeug/x.x.x Python/x.x.x")
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers.pop("Server", None)
        response.headers.pop("X-Powered-By", None)
        return response

    # ── Hardened error handlers (no framework leakage) ───────────────────────
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        # Serve the app itself for non-API 404s, so a mistyped or stale URL
        # lands on something usable instead of a dead end.
        #
        # This used to call send_static_file("index.html"). There is no
        # static/ directory in this project - the page is a Jinja template -
        # but app.static_folder is a non-empty path string regardless of
        # whether that directory exists, so the ternary ALWAYS took the
        # send_static_file branch, which raised NotFound from inside the 404
        # handler. Flask does not re-enter error handling for that, so every
        # unknown non-API path returned 500 with a full traceback in the logs:
        # /events, /home, any typo. Render the real template instead, and keep
        # an honest 404 status rather than pretending the URL exists.
        try:
            return render_template("index.html"), 404
        except Exception:          # template missing/unrenderable - never 500 here
            return jsonify(error="not found"), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify(error="method not allowed"), 405

    @app.errorhandler(429)
    def rate_limited(e):
        # Routes can carry a plain-English message (e.g. the analysis limits);
        # everything else gets the generic one.
        limit = getattr(e, "limit", None)
        msg = getattr(limit, "error_message", None) if limit is not None else None
        if callable(msg):
            msg = msg()
        resp = jsonify(error=msg or "Too many requests. Please slow down and try again shortly.",
                       reason="rate_limited")
        resp.status_code = 429
        return resp

    @app.errorhandler(413)
    def too_large(e):
        # Read the real limit rather than restating it: this message said 5MB
        # while MAX_CONTENT_LENGTH was 12MB, so anyone with an 8MB CV was told
        # their file was too big by a number that had not been true for a while.
        limit = app.config.get("MAX_CONTENT_LENGTH") or 0
        mb = max(1, limit // (1024 * 1024))
        return jsonify(error=f"File too large — max {mb}MB."), 413

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Internal error")
        return jsonify(error="internal server error"), 500

    return limiter
