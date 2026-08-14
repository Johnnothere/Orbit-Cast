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

from flask import request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def init_security(app):
    """
    Attach security headers, rate limiter, and hardened error handlers to app.
    Returns the Limiter instance so you can use @limiter.limit() on routes.
    """

    # ── Rate limiter ──────────────────────────────────────────────────────────
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=[],          # no global limit; apply per-route
        storage_uri="memory://",    # swap for "redis://..." in production
    )

    # ── Security response headers ─────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
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
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        # Remove server fingerprinting (hides "Werkzeug/x.x.x Python/x.x.x")
        response.headers.pop("Server", None)
        response.headers.pop("X-Powered-By", None)
        return response

    # ── Hardened error handlers (no framework leakage) ───────────────────────
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        # Serve the SPA for non-API 404s so client-side routing works
        return app.send_static_file("index.html") if app.static_folder else (
            jsonify(error="not found"), 404
        )

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify(error="method not allowed"), 405

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify(error="too many requests — slow down"), 429

    @app.errorhandler(413)
    def too_large(e):
        return jsonify(error="file too large — max 5MB"), 413

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Internal error")
        return jsonify(error="internal server error"), 500

    return limiter
