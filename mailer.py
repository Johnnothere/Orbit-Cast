"""
ORBITCAST — outbound email.

Resend over plain HTTPS, no SDK: one POST with a JSON body is the whole
integration, and a dependency that exists to build one request is a
dependency that can break a deploy for no benefit.

DEGRADES, NEVER CRASHES. With no RESEND_API_KEY the app runs normally and
every email is logged instead of sent - which is also how you develop login
locally: the magic link appears in the console.

Env:
  RESEND_API_KEY   enables real sending
  MAIL_FROM        e.g. "OrbitCast <alerts@yourdomain.com>" - must be a
                   domain verified in Resend, with SPF and DKIM published,
                   or mail lands in spam whatever the code does
  PUBLIC_URL       absolute base for links in emails (no trailing slash)
"""

import html
import logging
import os

import requests

log = logging.getLogger("orbitcast.mail")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "OrbitCast <onboarding@resend.dev>")
PUBLIC_URL = (os.environ.get("PUBLIC_URL", "https://orbitcast.up.railway.app")).rstrip("/")

BRAND_BG = "#0b1220"
BRAND_CARD = "#111c31"
BRAND_TEXT = "#e8eefc"
BRAND_MUTED = "#93a3c0"
BRAND_ACCENT = "#6ea8fe"


def is_configured() -> bool:
    return bool(RESEND_API_KEY)


def send(to: str, subject: str, html_body: str, text_body: str,
         unsubscribe_url: str = None) -> bool:
    """True if the provider accepted it. A refusal is logged, never raised:
    a failed digest must not take down the scheduler for everyone else."""
    if not to:
        return False
    if not RESEND_API_KEY:
        log.warning("EMAIL NOT SENT (no RESEND_API_KEY). To: %s | Subject: %s\n%s",
                    to, subject, text_body[:1500])
        return False

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
            log.warning("Resend refused (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:
        log.warning("Email send failed: %s", exc)
        return False


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
