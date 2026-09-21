"""
ORBITCAST — passwordless accounts.

WHY MAGIC LINKS AND NOT PASSWORDS
No password is ever stored, so none can leak, be reused across sites, or need
a reset flow. The email sender is required for the digests anyway, so the
login channel costs no extra infrastructure. Nothing new to configure beyond
what the digests already need.

WHY A SIGNED COOKIE AND NOT A SESSION TABLE
A session row per login is a table to grow, index and clean up. An HMAC-signed
cookie is self-verifying: the server trusts it only because the signature
matches a secret the browser never sees. Logout clears the cookie.

The trade-off, stated honestly: a signed cookie cannot be revoked server-side
before it expires. For a 30-day event-recommendation session that is an
acceptable exchange for having no session store. If this ever guards anything
worth stealing, add a session table and check it - don't stretch this.

TWO FRONT DOORS, ONE SESSION
Google Sign-In was added alongside the magic link, not instead of it. Both
paths end at the same place - db account row, set_session_cookie() - so
current_account(), login_required and the digest scheduler neither know nor
care which door someone came through. Magic links stay because the audience
includes defence, government and corporate addresses that are not Google
accounts, and because removing them would lock out every account already
created that way.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import jsonify, request

import db

log = logging.getLogger("orbitcast.auth")

SESSION_COOKIE = "oc_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30          # 30 days
TOKEN_TTL_MINUTES = 20                        # magic link lifetime

# A missing secret must not silently become a known one. A random secret per
# boot means sessions do not survive a restart - visibly annoying, which is
# the point: it gets noticed and fixed, whereas a hardcoded fallback would
# quietly let anyone who read this file forge a session for any account.
_SECRET = os.environ.get("SESSION_SECRET", "")
if not _SECRET:
    _SECRET = secrets.token_urlsafe(48)
    log.warning("SESSION_SECRET is not set - using a random per-boot secret, "
                "so every deploy signs everyone out. Set it in Railway.")
_SECRET_BYTES = _SECRET.encode("utf-8")


# ─────────────────────────────────────────────
# Magic-link tokens
# ─────────────────────────────────────────────

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_login_token(email: str):
    """Returns the raw token to put in the emailed link, or None if the
    database is unavailable. Only its hash is stored."""
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
    if not db.create_auth_token(email, _hash_token(raw), expires):
        return None
    return raw


def redeem_login_token(raw: str, current_oc_uid: str):
    """Spends the token and returns the account dict, or None.

    `current_oc_uid` is the browser's existing anonymous id - on a first-ever
    signup the account adopts it, so the analyses already stored under it
    become the new account's history instead of being orphaned."""
    if not raw:
        return None
    email = db.consume_auth_token(_hash_token(raw))
    if not email:
        return None
    return db.get_or_create_account(email, current_oc_uid)


# ─────────────────────────────────────────────
# Signed session cookie
# ─────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_SECRET_BYTES, payload, hashlib.sha256).digest())


def make_session(account) -> str:
    body = json.dumps({"aid": account["id"], "uid": account["oc_uid"],
                       "exp": int(time.time()) + SESSION_MAX_AGE},
                      separators=(",", ":")).encode("utf-8")
    return f"{_b64(body)}.{_sign(body)}"


def read_session(cookie_value):
    """Returns {"aid", "uid"} for a valid unexpired cookie, else None."""
    if not cookie_value or "." not in cookie_value:
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
    return {"aid": data.get("aid"), "uid": data.get("uid")}


def set_session_cookie(resp, account):
    resp.set_cookie(SESSION_COOKIE, make_session(account), max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="Lax", secure=_secure_cookies())
    # The account's oc_uid becomes this browser's oc_uid, which is what makes
    # the person's existing history follow them onto any device they log in on.
    resp.set_cookie("oc_uid", account["oc_uid"], max_age=60 * 60 * 24 * 365 * 2,
                    httponly=True, samesite="Lax", secure=_secure_cookies())
    return resp


def clear_session_cookie(resp):
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _secure_cookies() -> bool:
    """Secure in production, off on plain-HTTP localhost - otherwise the
    cookie is set and silently never sent back, which looks exactly like a
    broken login."""
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


def normalise_email(raw: str):
    """Light validation only. The magic link is the real check: an address
    that cannot receive mail never completes a login, whatever it looks like."""
    email = (raw or "").strip().lower()
    if len(email) < 5 or len(email) > 254:
        return None
    if email.count("@") != 1:
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    if any(c.isspace() for c in email):
        return None
    return email


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
# url_for(_external=True) would be the obvious choice and is a trap here:
# Railway terminates TLS at its proxy and forwards plain HTTP, so Flask - which
# does not trust X-Forwarded-Proto unless told to - would build
# "http://orbitcast.up.railway.app/auth/callback". Google compares the
# redirect_uri byte-for-byte against the registered value and rejects it with
# redirect_uri_mismatch, which reads like a credentials problem and is not one.
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "https://orbitcast.up.railway.app/auth/callback")

_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

_google = None


def google_enabled() -> bool:
    """False when the credentials are absent, so the frontend can hide the
    button instead of offering one that dead-ends in an error page."""
    return _google is not None


def init_google(app):
    """Wires Authlib onto the app. Safe to call with no credentials set - it
    logs and leaves Google disabled rather than raising at import time, which
    would take the whole site down over an optional login method."""
    global _google

    # Authlib keeps the OAuth state and nonce in Flask's own session cookie
    # between the redirect out and the callback back. Flask refuses to use
    # `session` at all without a secret key, and nothing else in this app had
    # ever needed one. Reusing SESSION_SECRET keeps it to a single secret to
    # configure; it is the same trust boundary either way.
    if not app.secret_key:
        app.secret_key = _SECRET

    if not _GOOGLE_CLIENT_ID or not _GOOGLE_CLIENT_SECRET:
        log.warning("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set - "
                    "Google Sign-In is disabled; magic links still work.")
        return None

    try:
        from authlib.integrations.flask_client import OAuth
    except ImportError:
        log.warning("Authlib is not installed - Google Sign-In is disabled.")
        return None

    oauth = OAuth(app)
    _google = oauth.register(
        name="google",
        client_id=_GOOGLE_CLIENT_ID,
        client_secret=_GOOGLE_CLIENT_SECRET,
        server_metadata_url=GOOGLE_DISCOVERY,
        # `openid email profile` and nothing more. Scope creep on a consent
        # screen costs conversions and, for this audience, trust - there is no
        # reason to ask for a calendar to sign somebody in.
        client_kwargs={"scope": "openid email profile"},
    )
    log.info("Google Sign-In enabled.")
    return _google


def google_redirect():
    """Sends the browser to Google's consent screen. Returns None if Google
    is not configured, so the caller can answer honestly rather than 500."""
    if not _google:
        return None
    from flask import session
    nonce = secrets.token_urlsafe(24)
    # Kept for the fallback path below. Authlib binds the nonce into its own
    # state record too and validates it on the way back; this copy exists only
    # so parse_id_token() can be called explicitly if a version of Authlib
    # does not hand back a pre-verified userinfo.
    session[NONCE_SESSION_KEY] = nonce
    return _google.authorize_redirect(GOOGLE_REDIRECT_URI, nonce=nonce)


def google_identity():
    """Completes the exchange and returns the verified claims as
    {"sub", "email", "email_verified", "name", "picture"}, or None.

    Every failure returns None rather than raising: a cancelled consent
    screen, an expired state, a burnt code and a genuine token-endpoint error
    all arrive here as exceptions, and all of them mean the same thing to the
    person - they are not signed in."""
    if not _google:
        return None
    from flask import session
    nonce = session.pop(NONCE_SESSION_KEY, None)
    try:
        token = _google.authorize_access_token()
    except Exception as exc:
        log.warning(f"Google token exchange failed: {exc}")
        return None

    claims = token.get("userinfo")
    if not claims:
        try:
            claims = _google.parse_id_token(token, nonce=nonce)
        except Exception as exc:
            log.warning(f"Google id_token could not be verified: {exc}")
            return None
    if not claims:
        return None

    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub or not email:
        # Both are required. An id_token without them is not something to
        # guess around - refuse the login.
        log.warning("Google returned an identity with no sub or no email.")
        return None

    return {
        "sub": sub,
        "email": email,
        # Passed through rather than assumed true. db.get_or_create_account_by_google
        # will only merge into an existing magic-link account when this is set.
        "email_verified": bool(claims.get("email_verified")),
        "name": (claims.get("name") or "").strip() or None,
        "picture": (claims.get("picture") or "").strip() or None,
    }
