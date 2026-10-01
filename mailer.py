"""
ORBITCAST — outbound email.

THREE WAYS TO SEND, because the right one depends on what you own and where
you host, and neither should be a code change:

  1. Resend (HTTPS API) - set RESEND_API_KEY. Needs a domain you control and
     have verified. Best deliverability; what you want once you have one.
  2. Mailjet (HTTPS API) - set MAILJET_API_KEY/MAILJET_SECRET_KEY. Mailjet
     validates a SINGLE SENDER ADDRESS by emailing it a link, so this one
     works with no domain at all.
  3. Any SMTP server - set SMTP_HOST/SMTP_USER/SMTP_PASS.

WHY TWO HTTPS OPTIONS AND NOT JUST SMTP: Railway disables outbound SMTP
(ports 25/465/587/2525) on its Free, Trial and Hobby plans, deliberately, to
stop spam. On those plans no SMTP credentials will ever work - the packets
do not leave the box. An HTTPS API is port 443 like any other request, so it
is unaffected. That is the whole reason path 2 exists.

First configured wins, in the order above. With none, the app still runs and
every email is logged instead - which is also how you develop login locally:
the magic link appears in the console.

Env:
  RESEND_API_KEY   enables the Resend path
  MAILJET_API_KEY  } enables the Mailjet path - both are required, they are
  MAILJET_SECRET_KEY } the public/private pair from the Mailjet dashboard
  SMTP_HOST        enables the SMTP path (e.g. smtp.gmail.com,
                   smtp-relay.brevo.com)
  SMTP_PORT        default 587 (STARTTLS). 465 switches to implicit TLS.
  SMTP_USER        username - usually the full email address
  SMTP_PASS        password or app-specific password. NEVER your normal
                   account password on a shared host: Gmail and Brevo both
                   issue a separate credential you can revoke on its own.
  MAIL_FROM        e.g. "OrbitCast <alerts@yourdomain.com>". Whatever domain
                   this names has to be one the provider will vouch for -
                   verified in Resend, or a verified sender in Brevo, or the
                   account's own address over Gmail SMTP. Naming a domain
                   nobody authorised is how mail lands in spam no matter how
                   correct the code is.
  PUBLIC_URL       absolute base for links in emails (no trailing slash)
"""

import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import requests

log = logging.getLogger("orbitcast.mail")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAILJET_API_KEY = os.environ.get("MAILJET_API_KEY", "")
MAILJET_SECRET_KEY = os.environ.get("MAILJET_SECRET_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# resend.dev is Resend's testing sender: it only delivers to the address on
# your own Resend account, so it proves the flow works and reaches nobody
# else. Fine as a default, wrong the moment a second person signs up.
MAIL_FROM = os.environ.get("MAIL_FROM", "OrbitCast <onboarding@resend.dev>")
PUBLIC_URL = (os.environ.get("PUBLIC_URL", "https://orbitcast.up.railway.app")).rstrip("/")

BRAND_BG = "#0b1220"
BRAND_CARD = "#111c31"
BRAND_TEXT = "#e8eefc"
BRAND_MUTED = "#93a3c0"
BRAND_ACCENT = "#6ea8fe"


# The last failure, kept so the admin dashboard can show WHY mail is not
# arriving. Without this the only record is a Railway log line, which means
# "check the logs" every time - and a silent mail failure is the one bug
# where the user sees nothing at all, so it needs to be visible somewhere
# you can actually look.
_LAST_ERROR = ""


def _fail(msg: str) -> bool:
    global _LAST_ERROR
    _LAST_ERROR = msg
    log.warning("MAIL: %s", msg)
    return False


def last_error() -> str:
    return _LAST_ERROR


def active_provider() -> str:
    if RESEND_API_KEY:
        return "resend"
    if MAILJET_API_KEY and MAILJET_SECRET_KEY:
        return "mailjet"
    if SMTP_HOST:
        return "smtp"
    return "none"


def status() -> dict:
    """Which provider is live and whether each credential is PRESENT - never
    the values themselves. Presence is what diagnoses a misnamed env var,
    and the value would be a secret in a dashboard."""
    return {
        "provider": active_provider(),
        "resend_key": bool(RESEND_API_KEY),
        "mailjet_key": bool(MAILJET_API_KEY),
        "mailjet_secret": bool(MAILJET_SECRET_KEY),
        "smtp_host": SMTP_HOST or "",
        "mail_from": MAIL_FROM,
        "public_url": PUBLIC_URL,
        "last_error": _LAST_ERROR,
    }


def is_configured() -> bool:
    return bool(RESEND_API_KEY or (MAILJET_API_KEY and MAILJET_SECRET_KEY) or SMTP_HOST)


def send(to: str, subject: str, html_body: str, text_body: str,
         unsubscribe_url: str = None) -> bool:
    """True if the provider accepted it. A refusal is logged, never raised:
    a failed digest must not take down the scheduler for everyone else."""
    if not to:
        return False
    if not RESEND_API_KEY:
        if MAILJET_API_KEY and MAILJET_SECRET_KEY:
            return _send_mailjet(to, subject, html_body, text_body, unsubscribe_url)
        if SMTP_HOST:
            return _send_smtp(to, subject, html_body, text_body, unsubscribe_url)
        log.warning("EMAIL NOT SENT — no provider. To: %s | Subject: %s\n%s",
                    to, subject, text_body[:1500])
        return _fail("No mail provider configured. Set RESEND_API_KEY, or "
                     "MAILJET_API_KEY + MAILJET_SECRET_KEY, or SMTP_HOST.")

    payload = {"from": MAIL_FROM, "to": [to], "subject": subject,
               "html": html_body, "text": text_body}
    if unsubscribe_url:
        # Gmail and Outlook surface a native unsubscribe control from these
        # two headers. Honouring them is both a deliverability signal and,
        # for a marketing-shaped email, a legal requirement.
        payload["headers"] = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    try:
        r = requests.post("https://api.resend.com/emails", json=payload, timeout=20,
                          headers={"Authorization": f"Bearer {RESEND_API_KEY}"})
        if r.status_code >= 300:
            return _fail(f"Resend refused with HTTP {r.status_code}: {r.text[:200]}")
        return True
    except Exception as exc:
        return _fail(f"Resend request failed: {exc}")


def _send_mailjet(to: str, subject: str, html_body: str, text_body: str,
                  unsubscribe_url: str = None) -> bool:
    """Mailjet Send API v3.1. HTTPS on 443, so it works on hosts that block
    SMTP - which is every Railway plan below Pro.

    The sender address has to be one you have validated in Mailjet, but
    validation there is per-ADDRESS: they email it a link, you click it. No
    DNS, no domain. That is what makes this the option when you do not own a
    domain yet."""
    name, addr = parseaddr(MAIL_FROM)
    message = {
        "From": {"Email": addr or MAIL_FROM, "Name": name or "OrbitCast"},
        "To": [{"Email": to}],
        "Subject": subject,
        "TextPart": text_body,
        "HTMLPart": html_body,
    }
    if unsubscribe_url:
        message["Headers"] = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    try:
        r = requests.post("https://api.mailjet.com/v3.1/send",
                          json={"Messages": [message]}, timeout=20,
                          auth=(MAILJET_API_KEY, MAILJET_SECRET_KEY))
        if r.status_code >= 300:
            hint = ""
            if r.status_code in (401, 403):
                hint = (" — that is an authentication failure: check "
                        "MAILJET_API_KEY and MAILJET_SECRET_KEY are the "
                        "public/private pair, not swapped or truncated.")
            return _fail(f"Mailjet refused with HTTP {r.status_code}: "
                         f"{r.text[:200]}{hint}")
        # A 200 is not automatically a success here: Mailjet reports
        # per-message status inside the body, so a single bad recipient comes
        # back 200 with Status "error". Reading only the HTTP code would
        # record a silent failure as a delivered digest.
        try:
            results = (r.json() or {}).get("Messages") or []
            if results and results[0].get("Status") != "success":
                errs = results[0].get("Errors") or []
                detail = "; ".join(str(e.get("ErrorMessage") or e) for e in errs) or str(results[0])
                return _fail(f"Mailjet accepted the request but rejected the "
                             f"message: {detail[:250]} — the usual cause is "
                             f"MAIL_FROM ({MAIL_FROM}) not matching a sender "
                             f"address validated in Mailjet.")
        except ValueError:
            pass
        return True
    except Exception as exc:
        return _fail(f"Mailjet request failed: {exc}")


def _send_smtp(to: str, subject: str, html_body: str, text_body: str,
               unsubscribe_url: str = None) -> bool:
    """The same message over SMTP. Multipart alternative - plain text first,
    HTML second - because a mail client picks the last part it understands,
    and a message with no text part is itself a spam signal."""
    msg = EmailMessage()
    name, addr = parseaddr(MAIL_FROM)
    msg["From"] = formataddr((name, addr)) if addr else MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        with server:
            if SMTP_PORT != 465:
                # Upgrade to TLS before authenticating. Without this the
                # username and password cross the network in the clear.
                try:
                    server.starttls(context=ssl.create_default_context())
                except smtplib.SMTPNotSupportedError:
                    log.warning("SMTP server %s does not support STARTTLS", SMTP_HOST)
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as exc:
        # Logged, never raised: a mail outage must not take the scheduler
        # down for everyone else, and digest.py reads this False to avoid
        # marking the events as delivered.
        return _fail(f"SMTP send via {SMTP_HOST}:{SMTP_PORT} failed: {exc}")


# ─────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────

def _shell(inner: str, footer: str = "") -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:{BRAND_BG};
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="100%" style="max-width:560px;" cellpadding="0" cellspacing="0">
<tr><td style="padding:0 0 20px 0;">
  <span style="color:{BRAND_TEXT};font-size:15px;font-weight:700;letter-spacing:.14em;">ORBITCAST</span>
</td></tr>
<tr><td style="background:{BRAND_CARD};border-radius:14px;padding:28px;color:{BRAND_TEXT};
  font-size:15px;line-height:1.6;">{inner}</td></tr>
<tr><td style="padding:18px 4px;color:{BRAND_MUTED};font-size:12px;line-height:1.6;">{footer}</td></tr>
</table></td></tr></table></body></html>"""


def send_login_link(to: str, raw_token: str, is_known: bool = True) -> bool:
    link = f"{PUBLIC_URL}/auth/verify?token={raw_token}"
    inner = f"""
      <p style="margin:0 0 16px 0;">Here is your sign-in link for OrbitCast.</p>
      <p style="margin:0 0 24px 0;">
        <a href="{link}" style="display:inline-block;background:{BRAND_ACCENT};color:#06101f;
          text-decoration:none;font-weight:600;padding:12px 22px;border-radius:9px;">Sign in</a>
      </p>
      <p style="margin:0;color:{BRAND_MUTED};font-size:13px;">
        The link works once and expires in 20 minutes. If you did not ask for it,
        ignore this email - nobody can sign in without it.</p>"""
    text = (f"Sign in to OrbitCast:\n{link}\n\n"
            "Works once, expires in 20 minutes. If you didn't request it, ignore this email.")
    return send(to, "Your OrbitCast sign-in link", _shell(inner), text)


def _event_row(ev: dict) -> str:
    title = html.escape(ev.get("title") or "Untitled")
    url = html.escape(ev.get("url") or "#")
    date = html.escape(ev.get("date") or "Date TBC")
    cat = html.escape(ev.get("category") or "")
    why = html.escape((ev.get("why") or "").strip())
    score = ev.get("fit_score")
    score_html = (f'<span style="color:{BRAND_ACCENT};font-weight:600;">{int(score)}% fit</span>'
                  if isinstance(score, (int, float)) else "")
    return f"""
      <tr><td style="padding:14px 0;border-top:1px solid rgba(255,255,255,.08);">
        <a href="{url}" style="color:{BRAND_TEXT};font-size:16px;font-weight:600;
           text-decoration:none;">{title}</a>
        <div style="color:{BRAND_MUTED};font-size:13px;margin:4px 0 6px 0;">
          {date} &nbsp;·&nbsp; {cat} &nbsp;·&nbsp; {score_html}</div>
        <div style="color:{BRAND_TEXT};font-size:14px;opacity:.9;">{why}</div>
      </td></tr>"""


def send_digest(to: str, recommendations: list, unsub_url: str,
                settings_url: str = None) -> bool:
    settings_url = settings_url or f"{PUBLIC_URL}/"
    n = len(recommendations)
    subject = f"{n} event{'s' if n != 1 else ''} worth your time"
    rows = "".join(_event_row(r) for r in recommendations)
    inner = f"""
      <p style="margin:0 0 4px 0;font-size:17px;font-weight:600;">Your shortlist</p>
      <p style="margin:0 0 10px 0;color:{BRAND_MUTED};font-size:13px;">
        Matched against your profile. Nothing below scored under 65% - anything weaker
        was left out rather than padded in.</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>"""
    footer = (f'<a href="{settings_url}" style="color:{BRAND_MUTED};">Change how often you get this</a>'
              f' &nbsp;·&nbsp; <a href="{unsub_url}" style="color:{BRAND_MUTED};">Unsubscribe</a>')
    lines = [f"- {r.get('title')} ({r.get('date') or 'date TBC'}) {r.get('url')}"
             for r in recommendations]
    text = ("Your OrbitCast shortlist:\n\n" + "\n".join(lines) +
            f"\n\nChange frequency: {settings_url}\nUnsubscribe: {unsub_url}")
    return send(to, subject, _shell(inner, footer), text, unsubscribe_url=unsub_url)


def send_empty_digest(to: str, unsub_url: str, settings_url: str = None) -> bool:
    """The honest empty email.

    This exists on purpose. The scorer drops anything under 65%, so some weeks
    genuinely have nothing. Filling the gap with weak matches is the one thing
    that would teach people to stop opening these - and it is the same
    inflation the recommendation engine refuses to do on the site."""
    settings_url = settings_url or f"{PUBLIC_URL}/"
    inner = f"""
      <p style="margin:0 0 12px 0;font-size:17px;font-weight:600;">Nothing this time</p>
      <p style="margin:0 0 12px 0;">No new event scored high enough against your profile
        to be worth your evening. Rather than pad the list, here is an empty one.</p>
      <p style="margin:0;color:{BRAND_MUTED};font-size:13px;">
        You will hear from us again as soon as something fits.</p>"""
    footer = (f'<a href="{settings_url}" style="color:{BRAND_MUTED};">Change how often you get this</a>'
              f' &nbsp;·&nbsp; <a href="{unsub_url}" style="color:{BRAND_MUTED};">Unsubscribe</a>')
    text = ("No new event scored high enough against your profile this time, so this "
            "digest is deliberately empty.\n\n"
            f"Change frequency: {settings_url}\nUnsubscribe: {unsub_url}")
    return send(to, "Nothing worth your time this week", _shell(inner, footer), text,
                unsubscribe_url=unsub_url)
