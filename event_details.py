"""Per-event details read from the event's own page.

The catalogue only carries what a listing shows: title, date, a rough place
and a link. The event drawer also wants to say what the event is about, who
it suits, where exactly it is and how to reach the organiser. All of that
lives on the event's own page, so this module reads it from there:

  * Luma events: Luma's public event API (description, full address, hosts
    and their social handles).
  * Everything else: the page's schema.org Event data (JSON-LD), its meta
    description, its visible text and its links.

A small model (MODEL_EXTRACT, Haiku) turns that into a short summary and a
few "good fit for" labels. It is never trusted with facts it could invent:
an email, website or social link is kept only if it appears verbatim on the
page, and a venue only if its words appear in the page text. Platform-owned
accounts (Meetup's own Twitter in the page footer, say) are filtered out
before the model sees the list.

Freshness: a record is re-read after FRESH_S. The model only runs again when
the page's descriptive content actually changed (content hash), so keeping
details current costs page fetches, not model calls.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

import ai_engine
import db
from scraper import HEADERS, fetch, _is_public_host

log = logging.getLogger("event_details")

FRESH_S = int(os.environ.get("OC_DETAILS_FRESH_S", str(6 * 3600)))   # re-read pages this often
FAIL_RETRY_S = 20 * 60                                               # a page that failed: try again after
WARM_DAYS = int(os.environ.get("OC_DETAILS_WARM_DAYS", "14"))        # pre-read events this far ahead
TEXT_CAP = 6000                                                      # page text handed to the model

_mem: dict = {}                    # id -> record (process cache in front of the database)
_mem_lock = threading.Lock()
_inflight: dict = {}               # id -> Lock, so one page is read once at a time
_inflight_lock = threading.Lock()
_warm_lock = threading.Lock()

# ── link and contact filtering ────────────────────────────────────────────────

SOCIAL_HOSTS = {
    "twitter.com": "X", "x.com": "X", "linkedin.com": "LinkedIn", "instagram.com": "Instagram",
    "facebook.com": "Facebook", "youtube.com": "YouTube", "tiktok.com": "TikTok",
    "bsky.app": "Bluesky", "threads.net": "Threads", "github.com": "GitHub", "discord.gg": "Discord",
    "discord.com": "Discord", "t.me": "Telegram", "mastodon.social": "Mastodon",
}
# Hosting platforms whose own brand accounts and emails sit in every page
# footer. Those are the platform, not the organiser.
PLATFORM_TOKENS = ("meetup", "eventbrite", "allevents", "devpost", "lumahq", "luma", "dorahacks",
                   "devfolio", "devevents", "dev.events", "hackathonhub", "tito", "ticketsource",
                   "ticketmaster", "humanitix", "skiddle", "fatsoma", "universe")
PLATFORM_HOSTS = ("eventbrite.", "meetup.com", "allevents.in", "lu.ma", "luma.com", "dev.events",
                  "devpost.com", "dorahacks.io", "devfolio.co", "hackathonhub", "ti.to", "tito.io")
JUNK_PATHS = ("/sharer", "/share", "intent/tweet", "/tr?", "/tr/", "/plugins", "/dialog/", "fbml",
              "/hashtag/", "/search", "/home?status", "/login", "/signup", "/policies", "/legal")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOREPLY_RE = re.compile(r"no-?reply|donotreply|privacy|abuse|webmaster|postmaster|example\.", re.I)


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _is_platform_url(url):
    h = _host(url)
    return any(p in h for p in PLATFORM_HOSTS)


def _social_kind(url):
    h = _host(url)
    for dom, kind in SOCIAL_HOSTS.items():
        if h == dom or h.endswith("." + dom):
            return kind
    return None


def _clean_social(url):
    """A social link worth showing: a real profile, not a share button or a
    tracking pixel, and not the hosting platform's own account."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    low = url.lower()
    if any(j in low for j in JUNK_PATHS):
        return None
    kind = _social_kind(url)
    if not kind:
        return None
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    handle = path.lower()
    if any(t in handle for t in PLATFORM_TOKENS):
        return None
    if kind == "LinkedIn" and not handle.startswith(("company/", "school/", "showcase/", "edu/", "groups/")):
        return None
    return url.split("#")[0].rstrip("/")


def _clean_email(e):
    e = (e or "").strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(e) or NOREPLY_RE.search(e):
        return None
    dom = e.split("@", 1)[1]
    if any(t in dom for t in PLATFORM_TOKENS):
        return None
    return e


# ── Luma ──────────────────────────────────────────────────────────────────────

def _prosemirror_text(node):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n.get("text", ""))
            for c in n.get("content", []) or []:
                walk(c)
            if n.get("type") in ("paragraph", "heading", "list_item", "listItem", "hard_break"):
                out.append("\n")
        elif isinstance(n, list):
            for c in n:
                walk(c)
    walk(node)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _luma_source(url):
    p = urlparse(url)
    slug = p.path.strip("/").split("/")[0]
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
        log.info(f"luma details failed {slug}: {e}")
        return None
    if d.get("kind") != "event":
        return None
    data = d.get("data") or {}
    ev = data.get("event") or {}
    text = _prosemirror_text(data.get("description_mirror") or {})
    venue = None
    geo = ev.get("geo_address_info") or {}
    if geo.get("mode", "shown") == "shown":
        venue = geo.get("full_address") or geo.get("address")
    links, emails = [], []
    org_name, website = None, None
    for h in (data.get("hosts") or [])[:3]:
        org_name = org_name or h.get("name")
        if h.get("website") and not website:
            website = h["website"]
            links.append(website)
        for key, base in (("twitter_handle", "https://x.com/"), ("instagram_handle", "https://www.instagram.com/"),
                          ("youtube_handle", "https://www.youtube.com/@"), ("tiktok_handle", "https://www.tiktok.com/@")):
            if h.get(key):
                links.append(base + h[key].lstrip("@/"))
        if h.get("linkedin_handle"):
            links.append("https://www.linkedin.com/" + h["linkedin_handle"].lstrip("/"))
    cal = data.get("calendar") or {}
    if cal.get("website"):
        links.append(cal["website"])
    for m in EMAIL_RE.findall(text):
        emails.append(m)
    return {"text": text, "desc": text[:600], "venue": venue, "org_name": org_name,
            "org_url": website, "links": links, "emails": emails, "structured_venue": bool(venue),
            "links_are_organiser": True}


# ── generic pages ─────────────────────────────────────────────────────────────

def _jsonld_events(soup):
    out = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
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
                if "@graph" in x:
                    stack.append(x["@graph"])
                t = x.get("@type")
                ts = t if isinstance(t, list) else [t]
                if any(isinstance(y, str) and y.endswith("Event") for y in ts):
                    out.append(x)
    return out


def _place_text(loc):
    """'Name, street, postcode' from a schema.org Place, or None when the
    place is only a city ('London, United Kingdom') or virtual."""
    if isinstance(loc, list):
        for l in loc:
            v = _place_text(l)
            if v:
                return v
        return None
    if isinstance(loc, str):
        loc = {"name": loc}
    if not isinstance(loc, dict) or "Virtual" in str(loc.get("@type", "")):
        return None
    name = re.sub(r"^\s*(location|venue)\s*:\s*", "", str(loc.get("name") or ""), flags=re.I).strip()
    addr = loc.get("address") or {}
    parts = []
    if isinstance(addr, str):
        parts = [addr]
    elif isinstance(addr, dict):
        parts = [str(addr.get(k) or "").strip() for k in ("streetAddress", "postalCode")]
    text = _tidy_address([name] + parts)
    generic = re.sub(r"[^a-z]", "", text.lower())
    if not text or generic in ("london", "londonunitedkingdom", "londonuk", "londonengland",
                               "unitedkingdom", "online", "tbc", "tba", "londonunitedkingdomlondonunitedkingdom"):
        return None
    return text[:200]


_DROP_PARTS = {"united kingdom", "uk", "gb", "england", "great britain", "greater london"}


def _tidy_address(chunks):
    """'X, X, 4 Street, London, London, United Kingdom, 17' -> 'X, 4 Street, London'."""
    bits = []
    for chunk in chunks:
        for p in str(chunk or "").split(","):
            p = p.strip(" .")
            low = p.lower()
            if not p or low in _DROP_PARTS or re.fullmatch(r"\d{1,3}", p):
                continue
            if any(low == b.lower() or low in b.lower() for b in bits):
                continue
            bits = [b for b in bits if b.lower() not in low]   # a longer part replaces a shorter one it contains
            bits.append(p)
    return ", ".join(bits)


def _page_source(url):
    soup = fetch(url)
    if soup is None:
        return None
    events = _jsonld_events(soup)
    ev = events[0] if events else {}
    desc = re.sub(r"\s+", " ", str(ev.get("description") or "")).strip()
    if not desc:
        for attr in ({"property": "og:description"}, {"name": "description"}, {"name": "twitter:description"}):
            m = soup.find("meta", attrs=attr)
            if m and m.get("content"):
                desc = m["content"].strip()
                break
    venue = _place_text(ev.get("location")) if ev else None
    org = ev.get("organizer")
    if isinstance(org, list):
        org = next((o for o in org if isinstance(o, dict)), None)
    org_name = org_url = org_email = None
    if isinstance(org, dict) and "Person" not in str(org.get("@type", "")):
        org_name = (org.get("name") or "").strip() or None
        org_url = org.get("url") if isinstance(org.get("url"), str) else None
        org_email = org.get("email") if isinstance(org.get("email"), str) else None
    if not org_name and not _is_platform_url(url):
        sn = soup.find("meta", attrs={"property": "og:site_name"})
        if sn and sn.get("content"):
            org_name = sn["content"].strip()
            org_url = org_url or f"{urlparse(url).scheme}://{urlparse(url).netloc}/"

    links, emails = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            emails.append(href[7:].split("?")[0])
            continue
        links.append(urljoin(url, href))
    if org_email:
        emails.append(org_email.removeprefix("mailto:"))
    if org_url:
        links.append(org_url)
    for o in ([ev.get("organizer")] if isinstance(ev.get("organizer"), dict) else (ev.get("organizer") or [])):
        if isinstance(o, dict):
            same = o.get("sameAs") or []
            links.extend([same] if isinstance(same, str) else [s for s in same if isinstance(s, str)])

    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"[ \t\r\f\v]+", " ", main.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    emails.extend(EMAIL_RE.findall(text))
    return {"text": text[:TEXT_CAP * 2], "desc": desc[:1500], "venue": venue, "org_name": org_name,
            "org_url": org_url if org_url and not _is_platform_url(org_url) else None,
            "links": links, "emails": emails, "structured_venue": bool(venue)}


# ── model pass ────────────────────────────────────────────────────────────────

SYSTEM = """You write the details panel for one professional event in London, from the event's own page.

Return JSON only:
{"about": "...", "fit_for": ["...", "..."], "organiser": "...", "email": "...", "website": "...", "socials": ["..."], "venue": "..."}

about: 2-3 plain sentences on what happens at the event and what someone gets from it. Facts from the page only. No hype, no marketing words, no "join us", no dates or prices.
fit_for: 2-4 short labels (2-4 words each) naming who it is genuinely right for, e.g. "Policy researchers", "Early-career security analysts", "Seed-stage founders". Specific to this event, not generic ("Professionals", "Anyone").
organiser: the organising group's name as the page gives it, or "".
email, website, socials: copy ONLY from the CANDIDATE LINKS list, exactly as written, and only ones that belong to the organiser or the event (not the ticketing platform, not a sponsor, not a speaker's personal page). "" or [] if none fit.
venue: the exact venue name and street address if the page states one, copied from the page; otherwise "". Never a city on its own."""


def _ai_pass(ev, src, emails, links):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    cand = "\n".join(["EMAIL " + e for e in emails[:10]] + ["LINK " + l for l in links[:30]]) or "(none)"
    user = (f"TITLE: {ev.get('title','')}\nCATEGORY: {ev.get('category','')}\nFORMAT: {ev.get('format','')}\n"
            f"DATE: {ev.get('date','')}\nLISTED PLACE: {ev.get('location','')}\n\n"
            f"PAGE SUMMARY: {src.get('desc','')}\n\nPAGE TEXT:\n{src.get('text','')[:TEXT_CAP]}\n\n"
            f"CANDIDATE LINKS:\n{cand}")
    try:
        return ai_engine._call(SYSTEM, user, max_tokens=600, model=ai_engine.MODEL_EXTRACT)
    except Exception as e:
        log.info(f"details model pass failed {ev.get('id')}: {e}")
        return None


def _words_on_page(s, text):
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())]
    if not words:
        return False
    low = text.lower()
    return sum(w in low for w in words) / len(words) >= 0.8


def _clip(s, n):
    s = re.sub(r"\[([^\]]+)\]\((?:https?://|mailto:)[^)]*\)", r"\1", str(s or ""))   # [text](url) -> text
    s = re.sub(r"(\*\*|__|`)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _build(ev, prev):
    url = ev.get("url") or ""
    if not url or not _is_public_host(url):
        return None
    src = _luma_source(url) if _host(url) in ("lu.ma", "luma.com") else None
    if src is None:
        src = _page_source(url)
    if src is None:
        return None

    emails = []
    for e in src["emails"]:
        c = _clean_email(e)
        if c and c not in emails:
            emails.append(c)
    socials, websites = [], []
    for l in src["links"]:
        s = _clean_social(l)
        if s and s.lower() not in (x.lower() for x in socials):
            socials.append(s)
    if src.get("org_url"):
        websites.append(src["org_url"])
    cand_links = socials + websites

    content_hash = hashlib.sha256(json.dumps(
        [ev.get("title"), src.get("desc"), src.get("text", "")[:2500], src.get("venue"), emails, cand_links],
        ensure_ascii=False).encode()).hexdigest()[:20]

    reuse = prev and prev.get("content_hash") == content_hash and prev.get("ai")
    out = dict(prev.get("ai") or {}) if reuse else (_ai_pass(ev, src, emails, cand_links) or {})
    ai_ok = bool(out)

    page_text = (src.get("text") or "") + " " + (src.get("desc") or "")
    about = _clip(out.get("about") or src.get("desc") or "", 600)
    fit = [_clip(x, 40) for x in (out.get("fit_for") or []) if isinstance(x, str) and x.strip()][:4]

    # Contacts: only what is verbatim in the candidate list.
    cand_low = {c.lower().rstrip("/"): c for c in cand_links}
    email = (out.get("email") or "").strip().lower()
    email = email if email in emails else (emails[0] if len(emails) == 1 else "")
    website = (out.get("website") or "").strip().rstrip("/")
    website = cand_low.get(website.lower(), "") or (src.get("org_url") or "")
    picked = [cand_low.get(str(s).strip().lower().rstrip("/")) for s in (out.get("socials") or [])]
    picked = [p for p in picked if p and _social_kind(p)]
    if not ai_ok:
        # No model: the organiser's own site shows its own accounts; a
        # platform page only ever shows the platform's, which are filtered.
        picked = socials if (src.get("links_are_organiser") or not _is_platform_url(url)) else []
    socials_out = [{"kind": _social_kind(p), "url": p} for p in picked[:6]]

    venue = src.get("venue") if src.get("structured_venue") else None
    if not venue:
        v = (out.get("venue") or "").strip()
        if v and _words_on_page(v, page_text) and re.sub(r"[^a-z]", "", v.lower()) not in ("london", "londonuk"):
            venue = _clip(v, 200)

    return {
        "id": ev.get("id"), "url": url, "ok": True,
        "fetched_at": time.time(),
        "content_hash": content_hash,
        "about": about or None,
        "fit_for": fit,
        "venue": venue,
        "organiser": {"name": _clip(out.get("organiser") or src.get("org_name") or "", 120) or None,
                      "email": email or None, "website": website or None, "socials": socials_out},
        "ai": {k: out.get(k) for k in ("about", "fit_for", "organiser", "email", "website", "socials", "venue")} if ai_ok else None,
    }


# ── cache and public entry points ─────────────────────────────────────────────

def _lock_for(eid):
    with _inflight_lock:
        lk = _inflight.get(eid)
        if lk is None:
            lk = _inflight[eid] = threading.Lock()
        return lk


def _cached(eid):
    with _mem_lock:
        rec = _mem.get(eid)
    if rec is None:
        rec = db.get_event_details(eid)
        if rec:
            with _mem_lock:
                _mem[eid] = rec
    return rec


def _fresh(rec, url):
    if not rec or rec.get("url") != url:
        return False
    age = time.time() - float(rec.get("fetched_at") or 0)
    return age < (FRESH_S if rec.get("ok") else FAIL_RETRY_S)


def get_details(ev, refresh=True):
    """The details record for a catalogue event, reading the page if the
    stored copy is missing or stale. Never raises."""
    eid = ev.get("id")
    if not eid:
        return None
    rec = _cached(eid)
    if _fresh(rec, ev.get("url")) or not refresh:
        return rec
    with _lock_for(eid):
        rec = _cached(eid)                       # another request may have just built it
        if _fresh(rec, ev.get("url")):
            return rec
        try:
            new = _build(ev, rec if rec and rec.get("url") == ev.get("url") else None)
        except Exception as e:
            log.warning(f"details build failed {eid}: {e}")
            new = None
        if new is None:
            # Keep serving the last good copy; just note when we tried.
            new = dict(rec) if rec and rec.get("ok") else {"id": eid, "url": ev.get("url"), "ok": False}
            new["fetched_at"] = time.time()
            if rec and rec.get("ok"):
                new["fetched_at"] = time.time() - FRESH_S + FAIL_RETRY_S
        with _mem_lock:
            if len(_mem) > 5000:
                _mem.clear()
            _mem[eid] = new
        db.save_event_details(eid, new.get("url") or "", new)
        return new


def public_view(rec):
    if not rec or not rec.get("ok"):
        return {"ok": False}
    return {"ok": True, "about": rec.get("about"), "fit_for": rec.get("fit_for") or [],
            "venue": rec.get("venue"), "organiser": rec.get("organiser") or {},
            "updated_at": datetime.fromtimestamp(rec.get("fetched_at") or time.time(), timezone.utc).isoformat()}


def warm(events, today=None):
    """Read details for upcoming in-person and online events in the background
    after a scrape, so the drawer opens with them already there. Two pages at a
    time; stale or missing records only."""
    if not _warm_lock.acquire(blocking=False):
        return
    try:
        from scraper import to_iso_date
        today = today or datetime.now(timezone.utc).date()
        todo = []
        for e in events:
            try:
                iso = to_iso_date(e.get("date") or "")
                if not iso:
                    continue
                d = datetime.fromisoformat(iso).date()
            except Exception:
                continue
            if 0 <= (d - today).days <= WARM_DAYS and not _fresh(_cached(e.get("id")), e.get("url")):
                todo.append(e)
        if not todo:
            return
        log.info(f"Warming event details for {len(todo)} events")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(get_details, todo))
    except Exception as e:
        log.warning(f"details warm failed: {e}")
    finally:
        _warm_lock.release()
