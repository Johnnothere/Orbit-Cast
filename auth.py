"""
ORBITCAST — accounts. Google Sign-In only.

WHY GOOGLE ONLY
Magic-link email login was removed (Oct 2026). One front door means one
code path to audit, one identity provider doing the verification, and no
"request a link" endpoint to abuse. Google asserts the address is verified;
we refuse any identity where it does not.

The cost, stated plainly: there is no account recovery. Lose the Google
account, lose the OrbitCast account. The sign-in screen says so.

WHY A SIGNED COOKIE AND NOT A SESSION TABLE
A session row per login is a table to grow, index and clean up. An HMAC-signed
cookie is self-verifying: the server trusts it only because the signature
matches a secret the browser never sees. Logout clears the cookie.

The trade-off: a signed cookie cannot be revoked server-side before it
expires. Mitigations here: a 7-day lifetime (was 30), and the cookie is bound
to the browser's User-Agent so a copied cookie fails from a different browser.
If this ever guards anything worth stealing, add a session table - don't
stretch this.

SECRETS ARE REQUIRED
SESSION_SECRET must be set. A missing secret used to become a random per-boot
one; now the process refuses to start. Booting half-configured is how a
security control gets silently skipped in production.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from functools import wraps

from flask import jsonify, request

import db

log = logging.getLogger("orbitcast.auth")

SESSION_COOKIE = "oc_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7           # 7 days

_SECRET = os.environ.get("SESSION_SECRET", "")
if len(_SECRET) < 32:
    # Fail closed. A short or missing secret is a forgeable session for every
    # account; nothing in this app is worth running in that state.
    log.error("SESSION_SECRET is missing or shorter than 32 characters. Refusing to start. "
              "Generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"")
    sys.exit(1)
_SECRET_BYTES = _SECRET.encode("utf-8")


# ─────────────────────────────────────────────
# Signed session cookie
# ─────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes, purpose: bytes = b"session") -> str:
    # Domain-separated: a session signature can never be replayed as an
    # unsubscribe token or vice versa.
    return _b64(hmac.new(_SECRET_BYTES, purpose + b"\x00" + payload, hashlib.sha256).digest())


def _browser_fingerprint() -> str:
    """Coarse binding of the session to the browser that created it. A cookie
    copied to another browser (different User-Agent) stops working. It is
    deliberately coarse - a browser update changes the UA and signs the
    person out, which is an acceptable cost for a 7-day session."""
    ua = request.user_agent.string or ""
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]


def make_session(account) -> str:
    body = json.dumps({"aid": account["id"], "uid": account["oc_uid"],
                       "exp": int(time.time()) + SESSION_MAX_AGE,
                       "fp": _browser_fingerprint()},
                      separators=(",", ":")).encode("utf-8")
    return f"{_b64(body)}.{_sign(body)}"


def read_session(cookie_value):
    """Returns {"aid", "uid"} for a valid, unexpired cookie from the same
    browser, else None."""
    if not cookie_value or "." not in cookie_value or len(cookie_value) > 1024:
        return None
    encoded, sig = cookie_value.rsplit(".", 1)
    try:
        body = _unb64(encoded)
    except Exception:
        return None
    # compare_digest, not ==, so a forged signature cannot be discovered one
    # byte at a time by timing the response.
    if not hmac.compare_digest(_sign(body), sig):
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    if data.get("fp") != _browser_fingerprint():
        log.info("Session presented from a different browser; refused.")
        return None
    return {"aid": data.get("aid"), "uid": data.get("uid")}


def set_session_cookie(resp, account):
    resp.set_cookie(SESSION_COOKIE, make_session(account), max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="Lax", secure=secure_cookies(), path="/")
    # The account's oc_uid becomes this browser's oc_uid, which is what makes
    # the person's existing history follow them onto any device they log in on.
    # Same flags as the session - this id is itself a credential for the
    # anonymous-path routes.
    resp.set_cookie("oc_uid", account["oc_uid"], max_age=60 * 60 * 24 * 90,
                    httponly=True, samesite="Lax", secure=secure_cookies(), path="/")
    return resp


def clear_session_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def secure_cookies() -> bool:
    """Secure in production, off on plain-HTTP localhost - otherwise the
    cookie is set and silently never sent back, which looks exactly like a
    broken login. With ProxyFix installed (app.py) request.is_secure is
    already correct behind Railway's proxy."""
    return request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"


# ─────────────────────────────────────────────
# Request helpers
# ─────────────────────────────────────────────

def current_session():
    return read_session(request.cookies.get(SESSION_COOKIE))


def current_account():
    sess = current_session()
    if not sess:
        return None
    return db.get_account(sess.get("aid"))


def login_required(fn):
    """401 with a machine-readable reason, so the frontend can open the
    sign-in panel rather than showing a generic failure."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_session():
            return jsonify({"error": "Sign in to use this.",
                            "reason": "auth_required"}), 401
        return fn(*args, **kwargs)
    return wrapper


def client_ip() -> str:
    """Real client address. Correct only because app.py wraps the WSGI app in
    ProxyFix(x_for=1); without that this is Railway's proxy for everyone."""
    return request.remote_addr or ""


# ─────────────────────────────────────────────
# Unsubscribe tokens - derived, not stored
# ─────────────────────────────────────────────
# An unsubscribe link needs a secret a mail client can present without
# logging in. Storing a random one per account means a database dump hands
# out working links. Deriving it with HMAC from the account id means nothing
# is stored at all: the link is "<account_id>.<signature>", and verification
# recomputes the signature. Rotating SESSION_SECRET invalidates old links,
# which is the correct behaviour on a suspected leak.

def unsub_token(account_id) -> str:
    payload = str(int(account_id)).encode("ascii")
    return f"{payload.decode()}.{_sign(payload, b'unsub')}"


def verify_unsub_token(token: str):
    """Returns the account id the token was minted for, or None."""
    if not token or "." not in token or len(token) > 128:
        return None
    aid, _, sig = token.partition(".")
    if not aid.isdigit():
        return None
    if not hmac.compare_digest(_sign(aid.encode("ascii"), b"unsub"), sig):
        return None
    return int(aid)


# ─────────────────────────────────────────────
# Google Sign-In (OpenID Connect via Authlib)
# ─────────────────────────────────────────────
# Authlib is given the discovery document rather than hardcoded endpoint URLs,
# so Google rotating its signing keys or moving an endpoint needs no change
# here. It also means the id_token signature, issuer, audience, expiry and
# nonce are all verified by the library instead of by hand - the parts of OIDC
# that are easy to write and easier to write wrongly.

GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
NONCE_SESSION_KEY = "oc_oauth_nonce"

# The redirect URI is configuration, not something to derive from the request.
# url_for(_external=True) is a trap behind Railway's TLS-terminating proxy.
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "https://orbitcast.up.railway.app/auth/callback")

_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

_google = None


def google_enabled() -> bool:
    return _google is not None


def init_google(app):
    """Wires Authlib onto the app. Google is the ONLY way in, so missing
    credentials are a fatal misconfiguration, not a degraded mode."""
    global _google

    # Authlib keeps the OAuth state and nonce in Flask's own session cookie
    # between the redirect out and the callback back. Same trust boundary as
    # the session secret, so the same secret.
    if not app.secret_key:
        app.secret_key = _SECRET
    app.config.update(
        SESSION_COOKIE_NAME="oc_oauth",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Secure unless explicitly running on plain-HTTP localhost.
        SESSION_COOKIE_SECURE=os.environ.get("OC_INSECURE_DEV", "") != "1",
        PERMANENT_SESSION_LIFETIME=600,       # the OAuth round trip, not a login
    )

    if not _GOOGLE_CLIENT_ID or not _GOOGLE_CLIENT_SECRET:
        if os.environ.get("OC_ALLOW_NO_GOOGLE", "") == "1":
            # Local development and tests only. Never set this on Railway.
            log.warning("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET unset; running with sign-in "
                        "DISABLED because OC_ALLOW_NO_GOOGLE=1.")
            return None
        log.error("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET must be set - Google is the only "
                  "sign-in method. Refusing to start.")
        sys.exit(1)

    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    _google = oauth.register(
        name="google",
        client_id=_GOOGLE_CLIENT_ID,
        client_secret=_GOOGLE_CLIENT_SECRET,
        server_metadata_url=GOOGLE_DISCOVERY,
        # `openid email profile` and nothing more.
        client_kwargs={"scope": "openid email profile"},
    )
    log.info("Google Sign-In enabled.")
    return _google


def google_redirect():
    """Sends the browser to Google's consent screen, or None if unconfigured."""
    if not _google:
        return None
    from flask import session
    nonce = secrets.token_urlsafe(24)
    session[NONCE_SESSION_KEY] = nonce
    return _google.authorize_redirect(GOOGLE_REDIRECT_URI, nonce=nonce)


def google_identity():
    """Completes the exchange and returns the verified claims as
    {"sub", "email", "email_verified", "name", "picture"}, or None.

    Every failure returns None rather than raising: a cancelled consent
    screen, an expired state, a burnt code and a genuine token-endpoint error
    all mean the same thing to the person - they are not signed in."""
    if not _google:
        return None
    from flask import session
    nonce = session.pop(NONCE_SESSION_KEY, None)
    try:
        token = _google.authorize_access_token()
    except Exception as exc:
        log.warning(f"Google token exchange failed: {type(exc).__name__}")
        return None

    claims = token.get("userinfo")
    if not claims:
        try:
            claims = _google.parse_id_token(token, nonce=nonce)
        except Exception as exc:
            log.warning(f"Google id_token could not be verified: {type(exc).__name__}")
            return None
    if not claims:
        return None

    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub or not email:
        log.warning("Google returned an identity with no sub or no email.")
        return None

    return {
        "sub": sub,
        "email": email,
        "email_verified": bool(claims.get("email_verified")),
        "name": (claims.get("name") or "").strip()[:120] or None,
        "picture": (claims.get("picture") or "").strip()[:500] or None,
    }
