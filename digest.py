"""
ORBITCAST — scheduled email digests.

Cadence works like a reminders app: daily, every N days, or weekly on a chosen
weekday, at a chosen hour, in the subscriber's own timezone.

THREE THINGS THIS FILE IS CAREFUL ABOUT, because each one has an obvious
wrong version that looks correct until it is live:

1. Idempotency. The slot is claimed in the database BEFORE the email is sent,
   keyed on the local date it was due. Railway restarts the process on every
   deploy; a loop that sent first and recorded after would re-send to everyone
   it had already reached when it came back up.

2. Novelty. Each digest scores only events this account has not already been
   emailed. Scoring the whole catalog every time sends the same shortlist over
   and over, which is how a digest earns an unsubscribe by week three.

3. Daylight saving. Next-send times are computed on NAIVE LOCAL wall-clock
   dates and only then attached to the timezone. Adding 7 days to an aware UTC
   timestamp drifts an 08:00 send to 07:00 or 09:00 the moment the clocks
   change.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

import auth
import db
import mailer

log = logging.getLogger("orbitcast.digest")

FREQUENCIES = ("daily", "every_n_days", "weekly")
DEFAULT_TZ = "Europe/London"
SCHEDULER_TICK_SECONDS = 300          # 5 minutes; sends are hour-granular
EMPTY_DIGEST_COOLDOWN_DAYS = 7        # don't send "nothing this week" daily
MAX_PER_TICK = 50                     # bound the work one tick can do
MAX_SNOOZE_DAYS = 90

# The catalogue's categories. A digest can be narrowed to some of them; an
# empty selection means all, which is also what every subscriber who has
# never touched the setting gets.
CATEGORIES = ("Intelligence & Security", "Defence & Geopolitics", "Cyber & Infosec",
              "Tech & AI", "Education & Research", "Builder & Tech Community",
              "Business & Networking", "Hackathons")


def _tz(name):
    """Never fail over a timezone name. A bad or unavailable zone falls back
    to London rather than killing this subscriber's digest forever."""
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            return timezone.utc


def compute_next_send(prefs: dict, after: datetime = None) -> datetime:
    """The next send time in UTC, strictly after `after` (default: now).

    Works in local wall-clock terms throughout - see note 3 in the module
    docstring. '08:00 on Mondays' must stay 08:00 through a clock change."""
    tz = _tz(prefs.get("timezone"))
    after = after or datetime.now(timezone.utc)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    local_now = after.astimezone(tz)
    hour = int(prefs.get("send_hour", 8) or 0)
    freq = prefs.get("frequency", "weekly")

    def at(day_date):
        return datetime(day_date.year, day_date.month, day_date.day, hour, 0, 0, tzinfo=tz)

    if freq == "weekly":
        target = int(prefs.get("weekday", 0) or 0)          # 0 = Monday
        days_ahead = (target - local_now.weekday()) % 7
        candidate = at(local_now.date() + timedelta(days=days_ahead))
        if candidate <= local_now:
            candidate = at(local_now.date() + timedelta(days=days_ahead + 7))
    else:
        step = 1 if freq == "daily" else max(1, int(prefs.get("interval_days", 3) or 3))
        candidate = at(local_now.date())
        if candidate <= local_now:
            candidate = at(local_now.date() + timedelta(days=step))
    return candidate.astimezone(timezone.utc)


def period_key(prefs: dict, when: datetime = None) -> str:
    """The idempotency key: the local date this send belongs to. Two sends on
    one local day are the same send, however many times the loop wakes up."""
    tz = _tz(prefs.get("timezone"))
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(tz).date().isoformat()


def validate_prefs(data: dict):
    """(clean_prefs, error). Everything here arrives from a browser, so each
    field is clamped rather than trusted."""
    freq = (data.get("frequency") or "weekly").strip()
    if freq not in FREQUENCIES:
        return None, "frequency must be daily, every_n_days or weekly"
    try:
        interval = int(data.get("interval_days", 3) or 3)
        weekday = int(data.get("weekday", 0) or 0)
        hour = int(data.get("send_hour", 8) or 0)
    except (TypeError, ValueError):
        return None, "interval_days, weekday and send_hour must be numbers"
    if not 1 <= interval <= 30:
        return None, "interval_days must be between 1 and 30"
    if not 0 <= weekday <= 6:
        return None, "weekday must be 0 (Monday) to 6 (Sunday)"
    if not 0 <= hour <= 23:
        return None, "send_hour must be between 0 and 23"
    tzname = (data.get("timezone") or DEFAULT_TZ).strip() or DEFAULT_TZ
    if ZoneInfo is not None:
        try:
            ZoneInfo(tzname)
        except Exception:
            return None, f"unknown timezone: {tzname}"
    return {"frequency": freq, "interval_days": interval, "weekday": weekday,
            "send_hour": hour, "timezone": tzname,
            "paused": bool(data.get("paused", False))}, None


def clean_categories(raw):
    """A browser-supplied category list reduced to the ones that exist. All
    of them selected is stored as none selected: both mean 'everything', and
    storing the explicit list would silently exclude a category added later."""
    if not isinstance(raw, (list, tuple)):
        return []
    picked = [c for c in CATEGORIES if c in set(raw)]
    return [] if len(picked) == len(CATEGORIES) else picked


def snooze_until_from(days, now: datetime = None):
    """None for 'not snoozed'. Clamped: a snooze is a break, not a way to
    park a subscription indefinitely - that is what the off switch is for."""
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return (now or datetime.now(timezone.utc)) + timedelta(days=min(days, MAX_SNOOZE_DAYS))


def next_send_for(prefs: dict, snooze_until=None) -> datetime:
    """compute_next_send(), but never before a snooze ends."""
    now = datetime.now(timezone.utc)
    if snooze_until and snooze_until.tzinfo is None:
        snooze_until = snooze_until.replace(tzinfo=timezone.utc)
    after = snooze_until if (snooze_until and snooze_until > now) else now
    return compute_next_send(prefs, after=after)


# ─────────────────────────────────────────────
# Building and sending one digest
# ─────────────────────────────────────────────

def build_digest(account, prefs, catalog_events, score_fn):
    """Returns (recommendations, event_ids_considered).

    `score_fn` is injected rather than imported so this can be tested without
    an Anthropic key, and so app.py stays the only place that knows how the
    catalog is loaded."""
    stored = db.get_latest_analysis(account["oc_uid"])
    if not stored:
        return None, []                      # no profile yet - nothing to match against

    already = db.recently_sent_event_ids(account["id"], days=45)
    fresh = [e for e in (catalog_events or []) if e.get("id") not in already]
    fresh = _apply_account_filters(account["id"], fresh)
    if not fresh:
        return [], []

    scored = score_fn(stored["profile"], stored.get("evidence_level") or "rich",
                      stored.get("field") or "", fresh)
    recs = (scored or {}).get("recommendations") or []
    return recs, [r.get("event_id") for r in recs if r.get("event_id")]


def _apply_account_filters(account_id, events):
    """The person's own narrowing: categories they chose, events they muted.

    Applied BEFORE scoring, not after. Filtering afterwards would spend the
    model call on events that can never be sent, and would let a muted event
    take one of the few slots a real match could have had."""
    cats = set((db.get_digest_extras(account_id) or {}).get("categories") or [])
    if cats:
        events = [e for e in events if e.get("category") in cats]
    muted = {d["id"] for d in db.list_dismissed(account_id)}
    if muted:
        events = [e for e in events if e.get("id") not in muted]
    return events


def send_preview(account, prefs, catalog_events, score_fn) -> str:
    """'Send me one now'. The same email the schedule would send, right away.

    Deliberately records nothing: no period is claimed and no event ids are
    written, so a preview neither uses up the next scheduled send nor retires
    the events in it. The price is that the next real digest may repeat what
    the preview showed, which is the right way round for a test."""
    unsub = f"{mailer.PUBLIC_URL}/unsubscribe?t={auth.unsub_token(account['id'])}"
    settings = f"{mailer.PUBLIC_URL}/?settings=alerts"
    stored = db.get_latest_analysis(account["oc_uid"])
    if not stored:
        return "no_profile"
    events = _apply_account_filters(account["id"], list(catalog_events or []))
    recs = []
    if events:
        scored = score_fn(stored["profile"], stored.get("evidence_level") or "rich",
                          stored.get("field") or "", events)
        recs = (scored or {}).get("recommendations") or []
    if recs:
        ok = mailer.send_digest(account["email"], recs, unsub, settings)
    else:
        ok = mailer.send_empty_digest(account["email"], unsub, settings)
    return ("sent" if recs else "sent_empty") if ok else "send_failed"


def send_one(account, prefs, catalog_events, score_fn) -> str:
    """Sends this account's due digest. Returns a status string for the log.

    Claims the period first: if another run already has it, this returns
    immediately rather than emailing twice."""
    key = period_key(prefs)
    if not db.claim_digest_period(account["id"], key):
        return "already_claimed"

    unsub = f"{mailer.PUBLIC_URL}/unsubscribe?t={auth.unsub_token(account['id'])}"
    settings = f"{mailer.PUBLIC_URL}/?settings=alerts"

    try:
        recs, ids = build_digest(account, prefs, catalog_events, score_fn)
    except Exception as exc:
        log.warning("digest build failed for account %s: %s", account["id"], exc)
        db.finish_digest_send(account["id"], key, "error", [], 0)
        return "error"

    if recs is None:
        db.finish_digest_send(account["id"], key, "no_profile", [], 0)
        return "no_profile"

    if not recs:
        # An empty digest is honest, but an empty digest every day is noise.
        last_empty = db.last_empty_digest_at(account["id"])
        if last_empty and (datetime.now(timezone.utc) - last_empty
                           < timedelta(days=EMPTY_DIGEST_COOLDOWN_DAYS)):
            db.finish_digest_send(account["id"], key, "skipped_empty", [], 0)
            return "skipped_empty"
        ok = mailer.send_empty_digest(account["email"], unsub, settings)
        db.finish_digest_send(account["id"], key, "sent_empty" if ok else "send_failed", [], 0)
        return "sent_empty" if ok else "send_failed"

    # The send result decides what gets recorded, and that matters more than it
    # looks: event_ids is what marks an event as already-delivered. Writing the
    # ids after a FAILED send would retire those events for this subscriber
    # permanently - the digest that never arrived would also be the reason they
    # never hear about those events again. On failure the ids stay unwritten,
    # so the same events are back in the running at the next scheduled send.
    ok = mailer.send_digest(account["email"], recs, unsub, settings)
    if not ok:
        log.warning("digest email was not accepted for account %s", account["id"])
        db.finish_digest_send(account["id"], key, "send_failed", [], 0)
        return "send_failed"
    db.finish_digest_send(account["id"], key, "sent", ids, len(recs))
    return "sent"


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def run_due(catalog_fn, score_fn, limit: int = MAX_PER_TICK) -> dict:
    """One pass over everything that is due. Safe to call by hand."""
    stats = {}
    due = db.due_digests(limit=limit)
    if not due:
        return stats
    events = catalog_fn() or []
    for prefs in due:
        account = {"id": prefs["account_id"], "email": prefs["email"],
                   "oc_uid": prefs["oc_uid"]}
        try:
            status = send_one(account, prefs, events, score_fn)
        except Exception as exc:
            log.warning("digest failed for account %s: %s", prefs["account_id"], exc)
            status = "error"
        stats[status] = stats.get(status, 0) + 1
        # Always roll the clock forward, even on failure. A subscriber whose
        # send errored must not stay permanently due, re-running every tick
        # and burning the API budget on the same broken row.
        db.set_next_send_at(prefs["account_id"], compute_next_send(prefs))
        # A snooze that has run its course is cleared, so the panel stops
        # saying "snoozed until" about a date that is already behind us.
        extras = db.get_digest_extras(prefs["account_id"]) or {}
        if extras.get("snooze_until"):
            db.set_digest_extras(prefs["account_id"], extras.get("categories"), None)
    if stats:
        log.info("digest run: %s", stats)
    return stats


def start_scheduler(catalog_fn, score_fn):
    """Starts the background loop. One worker only - app.py runs a single
    gunicorn worker on purpose (see Procfile); with more, each would run its
    own loop and the claim in claim_digest_period() becomes the only thing
    standing between subscribers and duplicate email."""
    if not db.is_configured():
        log.info("Digest scheduler not started: no DATABASE_URL.")
        return None

    def loop():
        # Let the app finish booting and the first catalog land before the
        # first pass, so an early digest is not scored against an empty list.
        time.sleep(60)
        while True:
            try:
                run_due(catalog_fn, score_fn)
                db.purge_expired_auth_tokens()
            except Exception as exc:
                log.warning("digest scheduler tick failed: %s", exc)
            time.sleep(SCHEDULER_TICK_SECONDS)

    t = threading.Thread(target=loop, daemon=True, name="digest-scheduler")
    t.start()
    log.info("Digest scheduler started (tick %ss).", SCHEDULER_TICK_SECONDS)
    return t
