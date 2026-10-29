"""Start and end times for catalogue events, read from the event's own page.

Why this exists. Most sources list an event with a date and a link, and keep
the time on the event's page. The scrapers read listings, so until now an
event's time only reached the catalogue when the listing happened to carry
it - roughly half the catalogue said "TBC" against events whose page stated
a time in the first line. The event drawer read the page later (event_details)
but only for events in the next two weeks, two at a time, after the model had
run, and only the drawer saw the answer; the cards, the calendar, the digest
and the .ics feed never did.

What it does. After every scrape, each event without a time has its page read
once and the time taken from it, in this order, and only from what the page
actually states:

  1. schema.org Event JSON-LD startDate / endDate (most organisers, Eventbrite,
     Meetup, RUSI, HJS, Geostrategy, AllEvents event pages)
  2. event:start_time / og:start_time meta tags (Eventbrite)
  3. <time datetime="..."> and itemprop="startDate" microdata
  4. the Luma API for lu.ma / luma.com links
  5. the visible text: a range ("11am - 12pm", "18:00–20:00", "7 PM to 8:30 PM")
     or a labelled start ("Doors 6pm", "Starts at 18:30", "Time: 09:00")

A page that gives a midnight start and no end is treated as "no time given":
calendar exports and date-only listings all use 00:00 for "unknown", and a
catalogue that says 00:00 is worse than one that says TBC.

Results are cached per URL (database when available, process memory always)
so each page is read about once a day; a page with no time is retried every
few hours in case the organiser adds one. The read is bounded per scrape so
it never delays or blocks the catalogue: cached times are applied before the
catalogue is saved, and newly read ones are patched in as they arrive.

Everything here is deterministic. No model is involved, so a time can only
come from the page - it cannot be invented.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

import db
from scraper import HEADERS, _is_public_host, fetch, to_iso_date

log = logging.getLogger("event_times")

LONDON = ZoneInfo("Europe/London")

FRESH_S = int(os.environ.get("OC_TIMES_FRESH_S", str(24 * 3600)))     # re-read a page with a time
RETRY_S = int(os.environ.get("OC_TIMES_RETRY_S", str(6 * 3600)))      # re-read a page without one
FAIL_RETRY_S = 60 * 60                                                # a page that could not be read
BUDGET = int(os.environ.get("OC_TIMES_BUDGET", "160"))                # pages per scrape
WORKERS = int(os.environ.get("OC_TIMES_WORKERS", "4"))

_mem: dict = {}                    # url -> record
_mem_lock = threading.Lock()
_run_lock = threading.Lock()       # one backfill at a time

# ── small helpers ─────────────────────────────────────────────────────────────

_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*"
    r"(Z|z|[+-]\d{2}:?\d{2}|UTC|GMT)?)?\s*$"
)


def parse_iso(value):
    """(aware London datetime, has_clock) from a timestamp, or None.

    Accepts the forms sources actually emit: "2026-11-03T19:00:00Z",
    "2026-11-10T08:15:00+00:00", "2026-12-01T13:00:00" (no zone: taken as
    London), "2026-10-29T17:30:00.000+00:00", and a bare date, which comes
    back with has_clock False so callers can tell "no time" from "midnight"."""
    if value is None:
        return None
    m = _ISO_RE.match(str(value))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if m.group(4) is None:
            return datetime(y, mo, d, tzinfo=LONDON), False
        h, mi = int(m.group(4)), int(m.group(5))
        s = int(m.group(6) or 0)
        z = m.group(7)
        if h > 23 or mi > 59 or s > 59:
            return None
        if z and z.upper() not in ("Z", "UTC", "GMT"):
            sign = 1 if z[0] == "+" else -1
            zz = z[1:].replace(":", "")
            off = timedelta(hours=int(zz[:2]), minutes=int(zz[2:4])) * sign
            dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone(off)).astimezone(LONDON)
        elif z:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).astimezone(LONDON)
        else:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=LONDON)
    except (ValueError, OverflowError):
        return None
    return dt, True


def when_from_iso(start, end=None):
    """(date_iso, "HH:MM" | None, "HH:MM" | None) from ISO timestamps.

    Midnight with no usable end is "no time": every date-only feed pads to
    00:00. An end on another day is dropped (a multi-day event's end time is
    not that day's end time); an end at or before the start is dropped."""
    pa = parse_iso(start)
    if pa is None:
        return None, None, None
    a, a_clock = pa
    date_iso = a.date().isoformat()
    if not a_clock:
        return date_iso, None, None
    pb = parse_iso(end) if end else None
    end_hm = None
    if pb is not None and pb[1] and pb[0].date() == a.date() and pb[0] > a:
        end_hm = pb[0].strftime("%H:%M")
    start_hm = a.strftime("%H:%M")
    if start_hm == "00:00" and not end_hm:
        return date_iso, None, None
    return date_iso, start_hm, end_hm


def label(start_hm, end_hm=None):
    """The catalogue's `time` string: "18:30" or "18:30–20:30"."""
    if not start_hm:
        return None
    return f"{start_hm}–{end_hm}" if end_hm else start_hm


_CLOCK_IN_LABEL = re.compile(r"(\d{1,2})[:.](\d{2})")


def usable_time(value) -> bool:
    """True when an event's `time` field says something: not empty, and not
    the midnight that date-only feeds pad to."""
    if not value:
        return False
    m = _CLOCK_IN_LABEL.search(str(value))
    if not m:
        return False
    return not (int(m.group(1)) == 0 and m.group(2) == "00")


# ── text patterns ─────────────────────────────────────────────────────────────
# A time in prose is "7pm", "7.30pm", "19:30", "7:30 p.m." - a bare number is
# never a time (too many of those are prices, ages, dates and room numbers).

def _t(prefix):
    return (rf"(?P<{prefix}h>\d{{1,2}})(?:[:.](?P<{prefix}m>[0-5]\d))?"
            rf"\s*(?P<{prefix}mer>[ap]\.?\s?m\.?(?![a-z]))?")


_DASH = r"(?:-|–|—|−|to|until|till|’til|'til)"
_RANGE_RE = re.compile(
    r"(?<![£$€\d:.])" + _t("a") + rf"\s*{_DASH}\s*" + _t("b") + r"(?![\d:.])",
    re.IGNORECASE,
)
# A single time must be a time: minutes or a meridian, never a bare number.
_STRICT = r"(?=\d{1,2}(?:[:.][0-5]\d|\s*[ap]\.?\s?m))"
_LABEL_RE = re.compile(
    r"(?:\b(?:doors?(?:\s+open)?|start(?:s|ing)?(?:\s+time)?|time|from|at|kick-?off|begins?|"
    r"registration|arrivals?|arrive|check-?in)\b|@)\s*[:\-–]?\s*(?!£|\$|€)"
    + _STRICT + _t("a") + r"(?![\d:.])",
    re.IGNORECASE,
)
_DATE_THEN_TIME_RE = re.compile(
    r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b[^\n]{0,40}?"
    r"(?<![£$€\d:.])" + _STRICT + _t("a") + r"(?![\d:.])",
    re.IGNORECASE,
)
# "priced at 10.50", "tickets from 7.50", "aged 18-25": not times.
_MONEY_CTX = ("£", "$", "€", "cost", "price", "fee", "ticket", "aged", "ages", "age ", "pay", "rate")


def _to24(h, m, mer):
    """24h (hour, minute) from a prose time, or None when it is not one."""
    h = int(h)
    m = int(m) if m else 0
    mer = (mer or "").lower().replace(".", "").replace(" ", "")
    if mer:
        if not 1 <= h <= 12:
            return None
        if mer.startswith("p") and h < 12:
            h += 12
        if mer.startswith("a") and h == 12:
            h = 0
    else:
        if h > 23:
            return None
    return h, m


def _hm(t):
    return f"{t[0]:02d}:{t[1]:02d}"


def _money_near(text, m):
    before = text[max(0, m.start() - 16):m.start()].lower()
    after = text[m.end():m.end() + 4]
    return any(w in before for w in _MONEY_CTX) or any(c in after for c in "£$€%")


def times_from_text(text):
    """(start "HH:MM", end "HH:MM" | None) stated in prose, or (None, None).

    A range wins over a single labelled start. A single time has to be
    labelled ("doors 6pm", "starts at 18:30", "time: 09:00") or follow a day
    name ("Thursday, Oct 29 · 5:30 PM"); an unlabelled "7pm" somewhere in a
    paragraph is not evidence of when the event begins."""
    text = re.sub(r"[   ]", " ", text or "")
    for m in _RANGE_RE.finditer(text):
        a_h, a_min, a_mer = m.group("ah"), m.group("am"), m.group("amer")
        b_h, b_min, b_mer = m.group("bh"), m.group("bm"), m.group("bmer")
        if not (a_mer or b_mer) and not (a_min and b_min):
            continue                                  # "9 - 5", "18-25", "3-4", "2026-10-29"
        if _money_near(text, m):
            continue
        # "1-2pm" / "10am-2": a missing meridian is inherited from the other side.
        a_eff = a_mer or (b_mer if int(a_h) <= 12 else None)
        b_eff = b_mer or (a_mer if int(b_h) <= 12 else None)
        a, b = _to24(a_h, a_min, a_eff), _to24(b_h, b_min, b_eff)
        if a is None or b is None:
            continue
        if b <= a and not a_mer and b_mer:            # "11-1pm": the start was am
            a2 = _to24(a_h, a_min, "am")
            if a2 is not None and b > a2:
                a = a2
        if b <= a and not b_mer and a_mer:            # "10am-2": the end was pm
            b2 = _to24(b_h, b_min, "pm")
            if b2 is not None and b2 > a:
                b = b2
        if b <= a and not a_mer and not b_mer and int(b_h) <= 12:   # "9:00 - 5:00"
            b2 = (int(b_h) + 12, b[1])
            if b2 > a:
                b = b2
        if b <= a or a == (0, 0):
            continue
        return _hm(a), _hm(b)
    for rx in (_LABEL_RE, _DATE_THEN_TIME_RE):
        for m in rx.finditer(text):
            if not (m.group("amer") or m.group("am")):
                continue
            if _money_near(text, m):
                continue
            a = _to24(m.group("ah"), m.group("am"), m.group("amer"))
            if a is None or a == (0, 0):
                continue
            return _hm(a), None
    return None, None


# ── structured data on the page ───────────────────────────────────────────────

def _jsonld_event_nodes(soup):
    out = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            d = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        stack = [d]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                for k in ("@graph", "itemListElement", "subEvent"):
                    if k in x:
                        stack.append(x[k])
                if isinstance(x.get("item"), dict):
                    stack.append(x["item"])
                t = x.get("@type")
                ts = t if isinstance(t, list) else [t]
                if any(isinstance(y, str) and "Event" in y for y in ts) and x.get("startDate"):
                    out.append(x)
    return out


def _pick(cands, event_date):
    """cands: [(date_iso, start, end)]. The one for the catalogue's date when
    there is one; the only one otherwise; nothing when the page lists several
    dates and none is ours (a listing page, or a series shown on its next
    instance)."""
    cands = [c for c in cands if c and c[0]]
    if not cands:
        return None
    if event_date:
        same = [c for c in cands if c[0] == event_date]
        if same:
            return max(same, key=lambda c: (bool(c[1]), bool(c[2])))
    dates = {c[0] for c in cands}
    if len(dates) == 1:
        return max(cands, key=lambda c: (bool(c[1]), bool(c[2])))
    return None


def extract(soup, event_date=None, url=""):
    """{"date", "start", "end", "method"} from a fetched page, or None.

    `event_date` is the catalogue's date for the event, used to choose
    between several structured events on one page and to refuse a time that
    belongs to a different date."""
    event_date = (event_date or "")[:10] or None

    # 1. JSON-LD
    cands = []
    for node in _jsonld_event_nodes(soup):
        d, s, e = when_from_iso(node.get("startDate"), node.get("endDate"))
        if d and not s and node.get("doorTime"):
            d2, s2, _ = when_from_iso(node.get("doorTime"))
            if d2 == d:
                s = s2
        cands.append((d, s, e))
    hit = _pick(cands, event_date)
    if hit and hit[1]:
        return {"date": hit[0], "start": hit[1], "end": hit[2], "method": "jsonld"}
    jsonld_dates = {c[0] for c in cands if c and c[0]}

    # 2. meta tags
    def meta(*keys):
        for k in keys:
            for attr in ("property", "name", "itemprop"):
                m = soup.find("meta", attrs={attr: k})
                if m and m.get("content"):
                    return m["content"]
        return None
    ms, me = meta("event:start_time", "og:start_time", "startDate"), meta("event:end_time", "og:end_time", "endDate")
    if ms:
        d, s, e = when_from_iso(ms, me)
        if s and (not event_date or d == event_date):
            return {"date": d, "start": s, "end": e, "method": "meta"}

    # 3. <time datetime> and microdata
    cands = []
    for t in soup.find_all("time"):
        dt = t.get("datetime")
        if dt:
            d, s, e = when_from_iso(dt)
            if d:
                cands.append((d, s, e))
    for t in soup.find_all(attrs={"itemprop": "startDate"}):
        v = t.get("content") or t.get("datetime")
        if v:
            d, s, e = when_from_iso(v)
            if d:
                cands.append((d, s, e))
    hit = _pick(cands, event_date)
    if hit and hit[1]:
        # pair with an endDate microdata/time on the same date when present
        end = None
        for t in soup.find_all(attrs={"itemprop": "endDate"}):
            d, s, _ = when_from_iso(t.get("content") or t.get("datetime"))
            if d == hit[0] and s and s > hit[1]:
                end = s
                break
        return {"date": hit[0], "start": hit[1], "end": hit[2] or end, "method": "time-tag"}

    # 4. visible text. Not on a homepage or a page that lists several dated
    #    events: the first time on those belongs to something else.
    path = urlparse(url).path.strip("/") if url else "x"
    if not path or len(jsonld_dates) > 1:
        return None
    for t in soup(["script", "style", "noscript", "svg", "nav", "form", "iframe"]):
        t.decompose()
    # Narrowest first: an element the organiser labelled as the time or
    # date, then the main content, then the whole page minus its footer.
    # RUSI, for one, keeps the time in a page banner outside <article>.
    chunks = []
    for el in soup.find_all(attrs={"class": _TIME_CLASS_RE})[:12]:
        chunks.append(el.get_text(" ", strip=True)[:400])
    for el in soup.find_all(attrs={"id": _TIME_CLASS_RE})[:6]:
        chunks.append(el.get_text(" ", strip=True)[:400])
    main = soup.find("main") or soup.find("article")
    if main is not None:
        chunks.append(re.sub(r"[ \t\r\f\v]+", " ", main.get_text("\n"))[:40000])
    for t in soup(["footer"]):
        t.decompose()
    body = soup.body or soup
    chunks.append(re.sub(r"[ \t\r\f\v]+", " ", body.get_text("\n"))[:40000])
    for text in chunks:
        s, e = times_from_text(text)
        if s:
            return {"date": event_date, "start": s, "end": e, "method": "text"}
    return None


_TIME_CLASS_RE = re.compile(r"(^|[-_ ])(time|date|datetime|when|schedule|meta)([-_ ]|$)", re.I)


# ── Luma ──────────────────────────────────────────────────────────────────────

def _luma_times(url, event_date=None):
    slug = urlparse(url).path.strip("/").split("/")[0]
    if not slug:
        return None
    try:
        r = requests.get("https://api.lu.ma/url", params={"url": slug},
                         headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
                         timeout=(6, 15))
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception as e:
        log.info(f"luma time lookup failed {slug}: {e}")
        return None
    if d.get("kind") != "event":
        return None
    ev = (d.get("data") or {}).get("event") or {}
    date_iso, s, e = when_from_iso(ev.get("start_at"), ev.get("end_at"))
    if not s:
        return None
    if event_date and date_iso != event_date:
        return None
    return {"date": date_iso, "start": s, "end": e, "method": "luma"}


# ── read one event ────────────────────────────────────────────────────────────

def read_event_time(url, event_date=None):
    """Read the page and return a result record (always a dict, never raises).
    found=True carries start/end; found=False with ok=True means the page was
    read and states no time; ok=False means it could not be read."""
    now = time.time()
    rec = {"url": url, "fetched_at": now, "ok": False, "found": False,
           "start": None, "end": None, "date": None, "method": None}
    event_date = to_iso_date(event_date) if event_date else None
    if not url or not _is_public_host(url):
        rec["method"] = "refused"
        return rec
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        hit = None
        if host in ("lu.ma", "luma.com"):
            hit = _luma_times(url, event_date)
            rec["ok"] = True
        else:
            soup = fetch(url)
            if soup is None:
                return rec
            rec["ok"] = True
            hit = extract(soup, event_date, url)
        if hit:
            rec.update({"found": True, "start": hit["start"], "end": hit["end"],
                        "date": hit.get("date") or event_date, "method": hit["method"]})
    except Exception as e:
        log.info(f"time read failed {url[:100]}: {e}")
    return rec


# ── cache ─────────────────────────────────────────────────────────────────────

def _cached(url):
    with _mem_lock:
        return _mem.get(url)


def _store(rec):
    with _mem_lock:
        if len(_mem) > 20000:
            _mem.clear()
        _mem[rec["url"]] = rec
    db.save_event_time(rec)


def remember(url, event_date, start, end=None, method="details"):
    """Record a time another reader (event_details) took from the page, so
    the next scrape applies it from the cache instead of reading again."""
    if not url or not start:
        return
    _store({"url": url, "fetched_at": time.time(), "ok": True, "found": True,
            "start": start, "end": end, "date": to_iso_date(event_date) if event_date else None,
            "method": method})


def _load_cached(urls):
    """Pull records for these URLs into memory from the database (one query)."""
    missing = [u for u in urls if u and _cached(u) is None]
    if not missing:
        return
    rows = db.get_event_times(missing)
    if rows:
        with _mem_lock:
            for u, rec in rows.items():
                _mem.setdefault(u, rec)


def _stale(rec):
    if not rec:
        return True
    age = time.time() - float(rec.get("fetched_at") or 0)
    if not rec.get("ok"):
        return age > FAIL_RETRY_S
    return age > (FRESH_S if rec.get("found") else RETRY_S)


# ── catalogue entry points ────────────────────────────────────────────────────

def normalise(ev):
    """Move a time a source buried in its `date` string into `time`, in
    London time, so every consumer (cards, calendar, digest, .ics) reads one
    field. AllEvents gives "2026-11-05T13:00:00+00:00", Eventbrite "Fri, Sep
    4, 12:30 PM", Imperial "Fri 30 October 2026, 14.00–15.00GMT". A `time`
    the scraper set is left alone unless it is the midnight placeholder.
    Returns the event."""
    t = ev.get("time")
    if t and not usable_time(t):
        ev["time"] = None
    if usable_time(ev.get("time")):
        return ev
    raw = ev.get("date")
    if not raw or not isinstance(raw, str):
        return ev
    d, s, e = when_from_iso(raw)
    if d and s:
        ev["time"] = label(s, e)
        return ev
    if d:
        return ev                                   # ISO date, no clock
    s, e = times_from_text(raw)
    if s:
        ev["time"] = label(s, e)
    return ev


def needs_time(ev) -> bool:
    """An event the catalogue cannot put a time against."""
    return not usable_time(ev.get("time")) and bool(ev.get("url"))


def _apply(ev, rec, today=None):
    """Patch one event from a found record. Returns True when it changed."""
    if not rec or not rec.get("found") or not rec.get("start"):
        return False
    have = to_iso_date(ev.get("date")) if ev.get("date") else None
    page = rec.get("date")
    # Never move an event to another date on the strength of its page: a
    # series page shows its next instance, a hackathon page its submission
    # window. A time on another date is that date's time, not ours.
    if have and page and have != page:
        return False
    if not have and page:
        # Fill a date the listing did not give, if it is a plausible one -
        # not a series placeholder years out.
        today = today or datetime.now(timezone.utc).date().isoformat()
        if not (today <= page <= (datetime.fromisoformat(today) + timedelta(days=400)).date().isoformat()):
            return False
    new = label(rec["start"], rec.get("end"))
    changed = ev.get("time") != new
    ev["time"] = new
    if not have and page:
        ev["date"] = page
        changed = True
    return changed


def apply_cached(events) -> int:
    """Fill times from the cache, no network. Call before the catalogue is
    saved so the saved copy already carries every time we have."""
    todo = [e for e in events if needs_time(e)]
    if not todo:
        return 0
    _load_cached([e["url"] for e in todo])
    n = 0
    for e in todo:
        if _apply(e, _cached(e["url"])):
            n += 1
    return n


def backfill(events, after=None, budget=None, workers=None, today=None):
    """Read the pages of events still without a time, soonest first, up to
    `budget` pages, `workers` at a time. Events are patched in place and
    `after(patches)` is called once with {event_id: {"time", "date"}} for
    the ones that changed. Returns the number patched. Never raises."""
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        budget = BUDGET if budget is None else budget
        workers = WORKERS if workers is None else workers
        today = today or datetime.now(timezone.utc).date().isoformat()
        todo = [e for e in events if needs_time(e)]
        _load_cached([e["url"] for e in todo])
        # Apply anything cached but not yet applied (a record written by a
        # previous run that the catalogue was saved before), then queue the
        # rest.
        patches = {}
        queue = []
        for e in todo:
            rec = _cached(e["url"])
            if rec and not _stale(rec):
                if _apply(e, rec):
                    patches[e.get("id")] = {"time": e.get("time"), "date": e.get("date")}
                continue
            queue.append(e)
        # Soonest first; undated last. A past date has no business here.
        def iso(e):
            return (to_iso_date(e.get("date")) if e.get("date") else None) or "9999"
        queue = [e for e in queue if iso(e) == "9999" or iso(e) >= today]
        queue.sort(key=iso)
        seen_urls, dedup = set(), []
        for e in queue:
            if e["url"] in seen_urls:
                continue
            seen_urls.add(e["url"])
            dedup.append(e)
        by_url = {}
        for e in queue:
            by_url.setdefault(e["url"], []).append(e)
        batch = dedup[:budget]
        if batch:
            log.info(f"Reading times for {len(batch)} event pages ({len(dedup)} without a time)")

            def one(e):
                rec = read_event_time(e["url"], e.get("date") or None)
                _store(rec)
                return e["url"], rec

            found = 0
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for url, rec in pool.map(one, batch):
                    if rec.get("found"):
                        found += 1
                    for e in by_url.get(url, []):
                        if _apply(e, rec):
                            patches[e.get("id")] = {"time": e.get("time"), "date": e.get("date")}
            log.info(f"Times read: {found} of {len(batch)} pages state a time")
        if patches and after:
            try:
                after(patches)
            except Exception as e:
                log.warning(f"time patch callback failed: {e}")
        return len(patches)
    except Exception as e:
        log.warning(f"time backfill failed: {e}")
        return 0
    finally:
        _run_lock.release()


def coverage(events):
    """{"with": n, "without": n, "pct": 0-100} for the admin coverage view."""
    total = len(events)
    with_t = sum(1 for e in events if usable_time(e.get("time")))
    return {"with": with_t, "without": total - with_t,
            "pct": round(100.0 * with_t / total, 1) if total else 0.0}
