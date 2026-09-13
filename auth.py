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
