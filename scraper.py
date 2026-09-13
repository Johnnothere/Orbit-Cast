#!/usr/bin/env python3
"""
ORBITCAST — Event Scraper
Scrapes 20+ sources across Tech, Defence, Intelligence, Business, Education & Hackathons.
"""

import os, json, re, time, hashlib, logging, requests, feedparser
from datetime import date, datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

# Events added through the admin link portal live in the database and are
# re-emitted into the catalog on every scrape (see _scrape_manual). db imports
# nothing from here, so there is no cycle, and it degrades to no-ops when
# DATABASE_URL is unset.
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("event-radar")

# Used to auto-tag any scraped event as "Hackathons" regardless of which
# source found it, instead of relying on a hand-maintained list that goes
# stale the moment nobody updates it.
HACKATHON_RE = re.compile(r"\b(hackathon|buildathon|hack\s?night|hack\s?day)\b", re.IGNORECASE)

# Several otherwise-good sources are global: the Claude Community calendar in
# particular publishes "Portland | ...", "Taipei | ...", "New York | ..." into
# what is meant to be a London-only catalog. Filtering is deliberately
# CONSERVATIVE - an event is only dropped when it positively names a different
# city. Anything with no city at all is kept, because these sources are
# London-scoped by default and silently dropping real London events would be
# a worse failure than letting an occasional stray through.
_NON_LONDON_CITIES = re.compile(
    r"\b(portland|san francisco|sf bay|silicon valley|taipei|chennai|new york|nyc|brooklyn"
    r"|atlanta|anchorage|adelaide|bhopal|san diego|seattle|boston|austin|denver|phoenix"
    r"|chicago|miami|los angeles|toronto|vancouver|montreal|sydney|melbourne|brisbane|perth"
    r"|berlin|munich|hamburg|paris|lyon|amsterdam|rotterdam|madrid|barcelona|lisbon|milan"
    r"|rome|zurich|geneva|vienna|prague|warsaw|stockholm|oslo|copenhagen|helsinki|dublin"
    r"|dubai|abu dhabi|riyadh|jeddah|doha|kuwait|cairo|tel aviv|istanbul|ankara"
    r"|bangkok|manila|jakarta|kuala lumpur|singapore|hong kong|shanghai|beijing|shenzhen"
    r"|tokyo|osaka|seoul|mumbai|new delhi|bangalore|bengaluru|hyderabad|pune|kolkata"
    r"|lagos|nairobi|accra|johannesburg|cape town|sao paulo|rio de janeiro|buenos aires"
    r"|mexico city|bogota|santiago|lima"
    r"|manchester|birmingham|leeds|glasgow|edinburgh|bristol|liverpool|cardiff|belfast"
    r"|newcastle|sheffield|nottingham|oxford|cambridge|brighton|philippines|qatar)\b",
    re.IGNORECASE,
)


# Some global calendars label every event with a "City | Title" prefix. For
# those, the prefix is authoritative and a denylist is the wrong tool - new
# cities appear faster than any list can be maintained (Durham, Nuremberg and
# Wellington all slipped past a 90-city list on the first run). Where the
# convention holds, require the city to BE London instead.
_CITY_PREFIXED_SOURCES = {"Claude Community"}
# Deliberately matches ANY characters before the pipe, not just ASCII letters:
# an [A-Za-z] class silently let "Medellín" through on the first run. Whatever
# label sits in that slot is the city for these sources.
_CITY_PREFIX_RE = re.compile(r"^\s*([^|]{2,28})\s*\|")

# An online event has no city, so the London question doesn't apply to it -
# it's reachable from London like anywhere else. Read off the LOCATION field
# only, never the title: "Building AI Agents Online" is a title that says
# nothing about the format, whereas a source that sets location="Online" is
# making a positive claim. Scrapers that know the format set it explicitly.
_ONLINE_RE = re.compile(r"^\s*(online|virtual|remote|livestream|webinar)\b", re.IGNORECASE)


def is_online(ev) -> bool:
    """True when the source positively labels the event online/virtual."""
    if ev.get("is_online"):
        return True
    return bool(_ONLINE_RE.match(ev.get("location", "") or ""))


# Sources that are NOT London-scoped: a global hackathon directory, a UK-wide
# student-hackathon charity, a worldwide Devpost search. For these the
# conservative "keep anything with no city" default is exactly wrong - it
# waves through every unrecognised city on earth. hackathons.org.uk alone
# offered up Bradford, Nottingham and Manchester, and only two of those three
# are on the denylist. Here London (or online) must be stated, not assumed.
_GLOBAL_SOURCES = {
    "Devpost London", "Devpost Online", "Hackathon Atlas",
    "Hackathons UK", "DoraHacks Virtual", "MLH",
}


# London, in the spellings that actually turn up in listings. The Cyrillic
# form is not decoration: "Лондон" was the stated venue of a Ukrainian-language
# hackathon sitting in the catalog, kept only because nothing disqualified it.
# A real London event should be kept because it says London, not by default.
_LONDON_NAME_RE = re.compile(r"\b(london)\b|Лондон|لندن", re.IGNORECASE)


def is_london(ev, source_name: str = None) -> bool:
    """True when the event belongs in a London catalog.

    Online events always qualify - they have no city to be wrong about.

    For London-scoped sources the check is deliberately CONSERVATIVE: an event
    is only dropped when it names a city that isn't London, because silently
    losing a real London event is worse than letting an occasional stray
    through. For the global sources in _GLOBAL_SOURCES that default inverts
    and London has to be named."""
    if is_online(ev):
        return True

    hay = f"{ev.get('title','')} {ev.get('location','')}"

    if source_name in _CITY_PREFIXED_SOURCES:
        m = _CITY_PREFIX_RE.match(ev.get("title", "") or "")
        if m:
            return bool(re.search(r"\blondon\b", m.group(1), re.IGNORECASE))
        # no prefix at all - fall through to the generic check

    if _LONDON_NAME_RE.search(hay):
        return True                       # explicitly London - always keep
    if source_name in _GLOBAL_SOURCES:
        return False                      # global source, London not stated
    return not _NON_LONDON_CITIES.search(hay)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEEN_FILE        = Path("seen_events.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────

def fetch(url, json_mode=False, timeout=15):
    try:
        h = dict(HEADERS)
        if json_mode:
            h["Accept"] = "application/json"
        r = requests.get(url, headers=h, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        if json_mode:
            return r.json()
        # r.content, not r.text, deliberately.
        #
        # requests only trusts the charset in the Content-Type header, and when
        # a server omits it for a text/* response it falls back to ISO-8859-1
        # (the old HTTP/1.1 default). Eventbrite serves exactly that: a bare
        # "Content-Type: text/html" on pages that are really UTF-8. Decoding
        # UTF-8 bytes as Latin-1 is what produced titles like
        # "London Tech ConnectorÂ®" and a Cyrillic hackathon rendered as
        # "Ð¨Ð¾ÑÑÐ¸Ð¹ ..." - visible mojibake in the live catalog.
        #
        # Handing the raw bytes to BeautifulSoup lets it read the document's
        # own <meta charset> / BOM, which is the declaration that actually
        # matters, and fall back to sniffing when there isn't one.
        return BeautifulSoup(r.content, "lxml")
    except Exception as e:
        log.warning(f"Fetch failed {url}: {e}")
        return None

def event_id(title, url):
    return hashlib.md5(f"{title.lower().strip()}{url}".encode()).hexdigest()[:12]

def load_seen():
    return json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10,
    )

# Titles to ignore
JUNK_TITLES = {
    "register free now", "register now", "skip to main content",
    "view accessibility support page", "sign up", "learn more",
    "find out more", "read more", "book now", "buy tickets",
    "the uk's leading public sector tech event",
}

# URL path segments that indicate non-event pages
JUNK_URL_PATHS = [
    "/cart", "/checkout", "/register", "/registration", "/login",
    "/job", "/jobs", "/careers", "/vacancy", "/vacancies",
    "/cookie", "/privacy", "/terms", "/sitemap", "/404",
    "/about", "/contact", "/search",
]

# Words that indicate a job listing title
JOB_TITLE_WORDS = [
    " manager,", " officer,", " director,", " executive,", " lead,",
    " analyst,", " engineer,", " consultant,", " specialist,",
    " head -", " deputy head", " chief ", " vp ",
]

# If a title names a kind of event, it is not a job advert - whatever
# job-ish words it also contains. Without this, " chief " threw out "The
# London Chief Product Officer Conference 2026", which is a conference about
# the role, not a vacancy for it.
EVENT_TYPE_RE = re.compile(
    r"\b(conference|summit|hackathon|buildathon|meetup|workshop|webinar|expo"
    r"|festival|symposium|forum|bootcamp|masterclass|panel|demo day|drinks"
    r"|party|breakfast|dinner|lunch|social|networking|talk|talks|showcase"
    r"|unconference|roundtable|briefing|seminar|congress|con)\b", re.IGNORECASE)

# Nav headings that scrapers pick up off listing pages. These are checked
# exactly rather than by shape, because "shape" heuristics kept eating real
# events - see the word-count rule in is_valid_event.
JUNK_TITLES |= {
    "home", "news", "blog", "events", "event", "menu", "more", "next", "back",
    "all events", "our events", "past events", "upcoming events", "view all",
    "our partners", "partners", "sponsors", "team", "faq", "faqs",
}


def is_valid_event(title):
    """True if this looks like a real event title.

    Every rule here has been tightened after it was caught discarding real
    events. The failure mode matters: a junk title that slips through is
    visible and fixable, whereas a real event rejected here vanishes with no
    trace in any log or count."""
    if not title:
        return False
    t = title.strip()
    # Was `< 8`, which threw out "44CON" and "Ctrl+W" - both real, recurring
    # London events. Genuinely empty labels are caught by JUNK_TITLES.
    if len(t) < 4 or len(t) > 200:
        return False
    tl = t.lower()
    if tl in JUNK_TITLES:
        return False
    # Reject job postings, unless the title names an event type.
    # Matched against a leading-space-padded copy: every entry in
    # JOB_TITLE_WORDS starts with a space, so without the pad none of them
    # could ever match a title that OPENS with the job word - "Head - Threat
    # Intelligence" walked straight through the job filter.
    padded = " " + tl
    if any(jw in padded for jw in JOB_TITLE_WORDS) and not EVENT_TYPE_RE.search(tl):
        return False
    # Reject pure-uppercase NAV HEADINGS - but a shouty title is how a great
    # many real conferences style themselves ("DEEP TECH LONDON SUMMIT",
    # "ONE AI HACKATHON", "NVIDIA GTC LONDON"), and the old rule dropped all
    # of them. Nav headings are short; three or more words is a name.
    stripped = t.replace(" ", "").replace("&", "").replace("|", "")
    if stripped.isupper() and len(stripped) > 10 and len(t.split()) < 3:
        return False
    return True

def is_valid_url(url):
    if not url:
        return False
    u = url.lower()
    return not any(p in u for p in JUNK_URL_PATHS)

def fix_url(url, base):
    if not url:
        return base
    url = url.strip()
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return base.rstrip("/") + "/" + url


# Trailing "#3", "Vol. 2", "(Sep 18)", "- London" and similar instance markers
# are stripped so that the weekly instances of one series collapse together.
_SERIES_NOISE_RE = re.compile(
    r"\s*(?:[-–—:|]\s*)?(?:#\s*\d+|vol\.?\s*\d+|part\s*\d+|no\.?\s*\d+|\d{4})\s*$",
    re.IGNORECASE,
)
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]{1,30}\)\s*$")


def norm_title(title: str) -> str:
    """Loose title key used for series collapsing and cross-source dedupe."""
    t = (title or "").strip().lower()
    t = _PAREN_TAIL_RE.sub("", t)
    t = _SERIES_NOISE_RE.sub("", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def to_iso_date(value):
    """Best-effort ISO date from whatever a source calls a date, else None.

    Sources disagree: Luma and the JSON APIs give "2026-08-14", Eventbrite
    gives "Fri, Sep 4, 12:30 PM", hackathons.org.uk gives "3 Oct 2026", and
    some Eventbrite rows give "Sunday at 1:00 PM + 39 more" with no date in
    them at all. Cross-source dedupe needs one comparable key, and None is an
    honest answer for the last case rather than a guess."""
    if not value:
        return None
    v = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    m = _LOOSE_DATE_RE.search(v)          # "3 Oct 2026"
    if m:
        return _iso_from_match(m)
    # "Aug26" / "Aug 26" - BrainStation prints the month and day with no
    # separator at all. The general pattern below requires whitespace, so this
    # form used to fall through and return None, which meant those events had
    # no dedupe key and could never be recognised as already past. Both the
    # frontend parser and ai_engine.parse_event_date already accept it;
    # this brings the third parser into line with them.
    m = re.match(r"^([A-Za-z]{3,9})\.?\s*(\d{1,2})$", v)
    if m and m.group(1).lower()[:3] in _MONTH_LOOKUP:
        return _iso_from_month_day(m.group(1), m.group(2))
    m = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\b", v)   # "Sep 4"
    if m and m.group(1).lower()[:3] in _MONTH_LOOKUP:
        return _iso_from_month_day(m.group(1), m.group(2))
    return None


def collapse_series(events, keep=2):
    """Keep only the next `keep` occurrences of any recurring series.

    Superteam UK's calendar is 16 copies of "Co-Working Fridays : London
    Chapter" stretching to December; Encode Club runs several weekly strands.
    Left alone a single series drowns out every other event in the catalog.
    Events are sorted by date first so the ones kept are the soonest.

    Called from the catalog build in app.py, AFTER the London filter, never
    from inside a scraper. Order matters: collapsing first would spend the
    quota on a series' two soonest instances wherever they happen to be, and
    a London instance further down the list would then be gone before the
    geography filter ever saw it."""
    out, counts = [], {}
    for ev in sorted(events, key=lambda e: e.get("date") or "9999"):
        key = norm_title(ev.get("title", ""))
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= keep:
            out.append(ev)
    return out

# ─────────────────────────────────────────────
# SOURCE REGISTRY
# ─────────────────────────────────────────────

SOURCES = []

def source(name, emoji, category):
    def decorator(fn):
        SOURCES.append({"name": name, "emoji": emoji, "category": category, "fn": fn})
        return fn
    return decorator

# ─────────────────────────────────────────────
# INTELLIGENCE & SECURITY
# ─────────────────────────────────────────────

@source("RUSI", "🛡️", "Intelligence & Security")
def scrape_rusi():
    events = []
    soup = fetch("https://rusi.org/events")
    if not soup:
        return events
    for art in soup.select("article"):
        h = art.select_one("h2, h3, h4")
        a = art.select_one("a[href]")
        d = art.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://rusi.org")
        date  = d.get_text(strip=True) if d else None
        # RUSI states both facts in the markup and we were reading neither, so
        # every RUSI event reached the catalog with no location and survived
        # only on is_london()'s "nothing disqualified it" default. .event-venue
        # carries "Location: London"; when it is absent the type badge says
        # "Members Event: Online" - which is the positive online claim
        # is_online() wants, rather than an inference from the title.
        v = art.select_one(".event-venue")
        venue = re.sub(r"^\s*location\s*:\s*", "",
                       v.get_text(" ", strip=True), flags=re.IGNORECASE) if v else ""
        badge = art.select_one(".event-type-badge")
        online = "online" in (badge.get_text(" ", strip=True).lower() if badge else "")
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "RUSI",
                           "location": "Online" if online else venue,
                           "is_online": online})
    return events

def _scrape_article_time_list(url, base, source_name, limit=20, keep=None):
    """Both BISI and Intelligence Forums publish events as <article> blocks
    with the title in an <h1> and a machine-readable <time datetime="...">.

    The previous scrapers looked for h2/h3/h4 and free-text dates, which is
    why both silently returned zero for months despite the sites being alive
    and full of relevant events. The <time datetime> attribute is an exact
    ISO date, so this needs no date guessing at all.

    `keep` is an optional predicate on the title, used to drop events these
    orgs run outside London."""
    events, seen = [], set()
    soup = fetch(url)
    if not soup:
        return events
    for art in soup.select("article"):
        h = art.select_one("h1, h2, h3")
        t = art.select_one("time[datetime]")
        a = art.select_one("a[href]")
        if not (h and t and a):
            continue
        title = h.get_text(" ", strip=True)
        iso = (t.get("datetime") or "")[:10]
        link = fix_url(a["href"], base)
        if not (is_valid_event(title) and is_valid_url(link)):
            continue
        if not _is_future(iso):
            continue
        if keep and not keep(title):
            continue
        if title.lower() in seen:
            continue
        seen.add(title.lower())
        events.append({"title": title, "date": iso, "url": link, "source": source_name})
    return events[:limit]


@source("BISI", "🔍", "Intelligence & Security")
def scrape_bisi():
    return _scrape_article_time_list(
        "https://bisi.org.uk/events", "https://bisi.org.uk", "BISI", limit=20)


# Intelligence Forums runs the same forum in several UK cities (IF London, IF
# Birmingham, IF Leeds, IF Glasgow...). Only London ones - and webinars, which
# anyone in London can attend - belong in a London aggregator.
_IF_KEEP = re.compile(r"\b(london|webinar|online|virtual)\b", re.IGNORECASE)


@source("Intelligence Forums", "🧠", "Intelligence & Security")
def scrape_intelligence_forums():
    return _scrape_article_time_list(
        "https://www.intelligence-forums.com/upcoming-forums",
        "https://www.intelligence-forums.com", "Intelligence Forums",
        limit=15, keep=lambda t: bool(_IF_KEEP.search(t)))


# RETIRED: OSMOSIS. osmosiscon.com now redirects to osmosisassociation.org and
# the events it lists are US-based ("OSMOSISCon Florida", "OSMOSIS Expo: DC").
# It's a genuine OSINT organisation, but it isn't running London events, so it
# has nothing to contribute to a London aggregator.

# ─────────────────────────────────────────────
# DEFENCE & GEOPOLITICS
# ─────────────────────────────────────────────

# The LDC site is a marketing homepage, not an events listing: its headings
# include the site's own navigation and its sponsor tiers. Walking every
# h2/h3 on it put "View our Linkedin Profile" into the Defence & Geopolitics
# category as an event, alongside "LDC Washington Forum" - an event in
# Washington, in a London catalog. Both are now filtered by name: a heading
# has to look like a conference programme item and must not name another
# city or a sponsor block.
_LDC_KEEP_RE = re.compile(r"\b(conference|forum|summit|symposium)\b", re.IGNORECASE)
_LDC_DROP_RE = re.compile(
    r"\b(washington|brussels|past conferences?|linkedin|sponsor\w*|supporter\w*"
    r"|partner\w*|network|committee|agency|media)\b", re.IGNORECASE)


@source("London Defence Conference", "🎖️", "Defence & Geopolitics")
def scrape_ldc():
    """London Defence Conference - one annual flagship conference plus a
    couple of satellite forums. Undated by design: the site announces the
    programme long before it publishes a date, and a guessed date would be
    worse than none."""
    events, seen = [], set()
    soup = fetch("https://londondefenceconference.com/")
    if not soup:
        return events
    for h in soup.select("h2, h3"):
        title = h.get_text(" ", strip=True)
        if not title or not _LDC_KEEP_RE.search(title) or _LDC_DROP_RE.search(title):
            continue
        a = h.find_parent("a") or h.select_one("a[href]")
        url = fix_url(a["href"] if a else "", "https://londondefenceconference.com") \
              or "https://londondefenceconference.com/"
        if not (is_valid_event(title) and is_valid_url(url)) or title.lower() in seen:
            continue
        seen.add(title.lower())
        events.append({"title": title, "date": None, "url": url,
                       "source": "London Defence Conference", "location": "London"})
    return events[:5]

# ─────────────────────────────────────────────
# CYBER & INFOSEC
# ─────────────────────────────────────────────

@source("Infosecurity Europe", "🔐", "Cyber & Infosec")
def scrape_infosec_europe():
    """Infosecurity Europe - the major annual infosec expo at ExCeL London.

    The old scraper walked card/article/session elements on an /en-gb.html URL
    and found nothing. The site publishes the event as a single schema.org
    JSON-LD block instead, which is both more reliable and gives an exact
    start date, so we read that."""
    events = []
    soup = fetch("https://www.infosecurityeurope.com/")
    if not soup:
        return events
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict) or "Event" not in str(item.get("@type", "")):
                continue
            # Title carries SEO branding ("Europe's Leading Cyber Security
            # Event | Infosecurity Europe") - keep the part that names the event.
            raw_title = (item.get("name") or "").strip()
            title = raw_title.split("|")[-1].strip() if "|" in raw_title else raw_title
            iso = str(item.get("startDate") or "")[:10]
            if not (title and _is_future(iso)):
                continue
            events.append({"title": title, "date": iso,
                           "url": "https://www.infosecurityeurope.com/",
                           "source": "Infosecurity Europe", "location": "ExCeL London"})
    return events[:10]

# ─────────────────────────────────────────────
# TECH & AI
# ─────────────────────────────────────────────

# RETIRED: Critical Communications World. The site is alive (HTTP 200) but the
# event it advertises is "11-13 May 2027, RAI Amsterdam, Netherlands" - it's a
# travelling conference not currently in London, so it's out of scope for a
# London aggregator. It returned 0 usable events on every run; keeping it only
# cost an HTTP request per scrape and showed a misleading "0" in the dashboard.

@source("Digital Government", "🏛️", "Tech & AI")
def scrape_digital_gov():
    """Scrape Digital Government events — with strict junk filtering."""
    events = []
    soup = fetch("https://www.digital-government.co.uk/")
    if not soup:
        return events
    for el in soup.select("[class*=event], article, [class*=card]"):
        h     = el.select_one("h2, h3, h4")
        a     = el.select_one("a[href]")
        d     = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.digital-government.co.uk")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "Digital Government"})
    return events[:10]

# RETIRED: AI Expo Global. The site is alive but it is a single-conference
# landing page ("AI & Big Data Expo Global, 3-4 February 2027, Olympia
# London"), not an event listing - it has no article/card/session elements for
# the scraper to walk, which is why it returned 0. That one conference is
# already picked up via AllEvents as "AI & Big Data Expo Global 2027", so
# scraping it separately would only add a duplicate.

# ─────────────────────────────────────────────
# EVENTBRITE
# ─────────────────────────────────────────────

def _eventbrite_venues(soup):
    """{event url or id -> venue string} from whatever the page embeds.

    The card markup gives a title, a link and a date line and nothing that can
    be safely read as a venue - which is why every Eventbrite event has been
    reaching the catalog with no location at all, surviving on is_london()'s
    "nothing disqualified it" default and showing no venue on the card.

    The structured data is where the venue actually lives, so it is read from
    there and matched back to the card by URL. Two shapes are tried because
    Eventbrite has shipped both; neither is guessed at from visible text -
    a wrong venue is worse than none, so anything unrecognised yields nothing
    and the event keeps today's behaviour exactly."""
    venues = {}

    def remember(url, venue):
        if url and venue:
            venues[url.split("?")[0]] = venue

    # 1. schema.org, if present.
    for node in _jsonld_events(soup):
        remember(node.get("url"), _jsonld_location(node))

    # 2. Eventbrite's own embedded search payload. The key has moved between
    #    __SERVER_DATA__ and __NEXT_DATA__, so the blob is walked rather than
    #    indexed by a path that will change again.
    for script in soup.find_all("script"):
        text = script.string or ""
        if "primary_venue" not in text:
            continue
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            continue
        try:
            blob = json.loads(m.group(0))
        except Exception:
            continue

        def walk(node):
            if isinstance(node, dict):
                venue = node.get("primary_venue")
                if isinstance(venue, dict):
                    addr = venue.get("address") or {}
                    label = (addr.get("localized_address_display")
                             or ", ".join(x for x in (venue.get("name"),
                                                      addr.get("city")) if x))
                    remember(node.get("url") or node.get("vanity_url"), label)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(blob)
    return venues


# ─────────────────────────────────────────────
# OFF-TOPIC LISTING FILTER
# ─────────────────────────────────────────────
# The listing-site CATEGORY feeds (Eventbrite tech/business/science, AllEvents
# tech/business/science/startup) are tag matches, not topic filters, and the
# tags are supplied by whoever posted the event. That is how a house/techno
# club night at Ministry Of Sound - "Audiowhore | Ministry Of Sound ... House /
# Tech" - entered the catalog tagged Tech & AI: the word "Tech" in a music
# genre. A singles night arrived the same way through the science feed.
#
# The keyword-aggregate sources (cyber, intel, defence) already defend against
# this with a positive title filter. The category feeds had no filter at all,
# which is why they are also the noisiest and largest part of the catalog.
#
# This is a NEGATIVE gate, deliberately: a positive one would also drop real
# events with vague titles ("Founders Breakfast", "October Meetup").
_OFFTOPIC_TITLE_RE = re.compile(
    r"\b(club ?night|nightclub|ministry of sound|fabric london|printworks"
    r"|\brave\b|dj ?set|\bdjs\b|house ?/ ?tech|techno|afrobeats?|amapiano"
    r"|reggaeton|bashment|garage night|after ?party|launch party|birthday bash"
    r"|bottomless brunch|boozy brunch|karaoke|open mic|comedy night|stand.?up comedy"
    r"|pub quiz|speed dating|singles (social|night|party|event)|matchmaking"
    r"|blind date|sound ?bath|breathwork|tantra|cacao ceremony|ecstatic dance"
    r"|yoga|pilates|reiki|life drawing|paint ?and ?sip|bingo|silent disco)\b",
    re.IGNORECASE,
)

# ...except when the same title also names professional substance. "FeelGood:
# The Health and Wellness Summit" and "Sports, Fitness & Wellness: Founders,
# Investment & Innovation" are real founder/investor events that a naive
# wellness filter would have eaten - which is exactly the over-correction this
# guard exists to prevent. Precision cuts both ways.
_PROFESSIONAL_MARKER_RE = re.compile(
    r"\b(summit|conference|symposium|founders?|investor\w*|investment|venture"
    r"|startup\w*|\bvc\b|\bb2b\b|keynote|panel|briefing|workshop|hackathon"
    r"|demo ?day|pitch|expo|forum|masterclass|bootcamp|seminar)\b",
    re.IGNORECASE,
)


# The venue catches what the title cannot. The club night above was listed
# twice: once with the full "House / Tech" title, and once as the bare artist
# name "Audiowhore", which names nothing filterable at all. Its venue field
# said "Ministry Of Sound" both times. Only dedicated nightclubs belong on
# this list - Printworks and Village Underground host real conferences, so
# they stay off it deliberately.
_OFFTOPIC_VENUE_RE = re.compile(
    r"\b(ministry of sound|fabric|egg london|corsica studios|xoyo|heaven"
    r"|electric brixton|o2 academy|koko|ministry|phonox|e1 london"
    r"|the cause|fold london|drumsheds)\b",
    re.IGNORECASE,
)


def _is_offtopic_listing(title, location=""):
    """True for a nightlife/social listing that a category tag dragged in.

    Checks the venue as well as the title: an event posted under the artist's
    name alone has nothing in its title to match, and the venue is the only
    field left that says what it actually is."""
    if not title:
        return False
    if _PROFESSIONAL_MARKER_RE.search(title):
        return False
    if _OFFTOPIC_TITLE_RE.search(title):
        return True
    return bool(location and _OFFTOPIC_VENUE_RE.search(location))


def _scrape_eventbrite(category_slug, source_name):
    events = []
    soup = fetch(f"https://www.eventbrite.co.uk/d/united-kingdom--london/{category_slug}/")
    if not soup:
        return events
    venues = _eventbrite_venues(soup)
    seen_titles = set()
    date_re = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun).+\d")
    for el in soup.select("a[data-event-id], [class*=search-event-card]"):
        a    = el if el.name == "a" else el.select_one("a[href]")
        href = a.get("href","") if a else ""
        if "/e/" not in href or not is_valid_url(href):
            continue
        parent = el.find_parent(["article","div","section","li"]) or el
        h      = parent.select_one("h2, h3, h4")
        title  = h.get_text(strip=True) if h else el.get_text(strip=True)
        date   = None
        for p in parent.select("p, span, time"):
            text = p.get_text(strip=True)
            if date_re.search(text):
                date = text; break
        venue = venues.get(href.split("?")[0], "")
        if (is_valid_event(title) and title not in seen_titles
                and not _is_offtopic_listing(title, venue)):
            seen_titles.add(title)
            events.append({"title": title, "date": date, "url": href,
                           "source": source_name,
                           "location": venue})
    got = sum(1 for e in events if e.get("location"))
    log.info(f"{source_name}: {len(events)} events, {got} with a venue "
             f"({len(venues)} venues found on the page)")
    return events[:20]

@source("Eventbrite Tech London",     "🎟️", "Tech & AI")
def scrape_eventbrite_tech():     return _scrape_eventbrite("tech",          "Eventbrite Tech London")

@source("Eventbrite Business London", "💼", "Business & Networking")
def scrape_eventbrite_business(): return _scrape_eventbrite("business",       "Eventbrite Business London")

@source("Eventbrite Science London",  "🔬", "Education & Research")
def scrape_eventbrite_science():  return _scrape_eventbrite("science-and-tech","Eventbrite Science London")

# ─────────────────────────────────────────────
# ALLEVENTS
# ─────────────────────────────────────────────

def _scrape_allevents(category, source_name):
    events = []
    url  = f"https://allevents.in/london/{category}" if category else "https://allevents.in/london"
    soup = fetch(url)
    if not soup:
        return events
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string)
            if not isinstance(data, list):
                continue
            for item in data:
                if item.get("@type") != "Event":
                    continue
                title     = item.get("name","").replace("&amp;","&")
                event_url = item.get("url","")
                date      = item.get("startDate")
                location  = ""
                loc = item.get("location",{})
                if isinstance(loc, dict):
                    location = loc.get("name","")
                if (is_valid_event(title) and event_url and is_valid_url(event_url)
                        and not _is_offtopic_listing(title, location)):
                    events.append({"title": title, "date": date, "url": event_url,
                                   "source": source_name, "location": location})
        except (json.JSONDecodeError, TypeError):
            continue
    return events[:25]

@source("AllEvents Tech",     "🌐", "Tech & AI")
def scrape_allevents_tech():     return _scrape_allevents("tech",    "AllEvents Tech")

@source("AllEvents Business",  "📊", "Business & Networking")
def scrape_allevents_business(): return _scrape_allevents("business","AllEvents Business")

@source("AllEvents Science",   "🧪", "Education & Research")
def scrape_allevents_science():  return _scrape_allevents("science", "AllEvents Science")

@source("AllEvents Startup",   "🚀", "Business & Networking")
def scrape_allevents_startup():  return _scrape_allevents("startup", "AllEvents Startup")

# ─────────────────────────────────────────────
# CYBER & INFOSEC  (keyword-filtered aggregate)
# ─────────────────────────────────────────────
# The dedicated infosec sources (Infosecurity Europe, BISI, Intelligence
# Forums, OSMOSIS) have all gone dead and return zero events, which left the
# Cyber & Infosec category completely empty - a security professional could
# upload a strong CV and legitimately get no relevant matches, because the
# catalog contained no technical security content at all.
#
# The generic listing-site searches DO still work, but their "cyber-security"
# category slugs are NOT real topical filters - they return property
# investment webinars and film fairs alongside the real thing. Labelling that
# raw feed as security content would be worse than having none, so we keep
# only titles that actually name security work. Precision over volume:
# a smaller, genuinely-security list beats a padded, mislabelled one.
_SECURITY_TITLE_RE = re.compile(
    r"\b(cyber ?security|cybersecurity|infosec|information security"
    r"|network security|application security|appsec|pen ?test\w*"
    r"|penetration test\w*|ethical hack\w*|red team\w*|blue team\w*"
    r"|purple team\w*|threat intel\w*|threat hunt\w*|malware|ransomware"
    r"|owasp|bsides|b-sides|ciso|soc analyst|vulnerabilit\w*|exploit\w*"
    r"|zero.?day|zero.?trust|digital forensic\w*|incident response|osint"
    r"|bug bounty|hack the box|capture the flag|ctf)\b",
    re.IGNORECASE,
)

_SECURITY_SEARCHES = [
    ("eventbrite", "cyber-security"),
    ("eventbrite", "information-security"),
    ("eventbrite", "hacking"),
    ("allevents",  "cyber-security"),
    ("allevents",  "it"),
]


# ─────────────────────────────────────────────
# TECHNICAL SECURITY COMMUNITIES
# ─────────────────────────────────────────────
# The listing-site searches above surface real security events, but they skew
# heavily toward conferences and business-networking breakfasts. The deep
# technical community - OWASP chapter meetups, BSides, DEF CON groups - only
# publishes on its own sites, so a senior offensive-security profile would
# otherwise never see anything hands-on. These are low-volume by nature (a
# chapter announces one meetup at a time), so expect small counts; the value
# is relevance, not quantity.

_DOW_DATE_RE = re.compile(
    r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})", re.IGNORECASE)

_LOOSE_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})\b", re.IGNORECASE)

_MONTH_LOOKUP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _iso_from_match(m):
    """Turn a (day, month-word, year) regex match into an ISO date string,
    or None if the month word isn't a real month."""
    try:
        day, mon_word, year = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        month = _MONTH_LOOKUP.get(mon_word)
        if not month:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, AttributeError):
        return None


def _is_future(iso_date):
    if not iso_date:
        return False
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").date() >= datetime.now(timezone.utc).date()
    except ValueError:
        return False


@source("OWASP London", "🛡️", "Cyber & Infosec")
def scrape_owasp_london():
    """OWASP London chapter meetups. The page lists meetups newest-first as
    <h4> date headings; we keep only ones still in the future, so this is
    legitimately empty between announcements rather than padded with past
    meetups."""
    events = []
    soup = fetch("https://owasp.org/www-chapter-london/")
    if not soup:
        return events
    for h in soup.select("h4"):
        text = h.get_text(" ", strip=True)
        m = _DOW_DATE_RE.search(text)
        if not m:
            continue
        iso = _iso_from_match(m)
        if not _is_future(iso):
            continue
        events.append({
            "title": "OWASP London Chapter Meetup",
            "date": iso,
            "url": "https://owasp.org/www-chapter-london/",
            "source": "OWASP London",
            "location": "London",
        })
    return events[:6]


@source("BSides London", "🔓", "Cyber & Infosec")
def scrape_bsides_london():
    """BSides London - community-run technical security conference."""
    events = []
    soup = fetch("https://bsides.london/")
    if not soup:
        return events
    text = soup.get_text(" ", strip=True)
    for m in _LOOSE_DATE_RE.finditer(text):
        iso = _iso_from_match(m)
        if not _is_future(iso):
            continue
        events.append({
            "title": "BSides London",
            "date": iso,
            "url": "https://bsides.london/",
            "source": "BSides London",
            "location": "London",
        })
        break  # single annual conference - first future date is the one
    return events


@source("DC4420", "💀", "Cyber & Infosec")
def scrape_dc4420():
    """DC4420 - the London DEF CON group, monthly hacker meetup."""
    events = []
    soup = fetch("https://dc4420.org/")
    if not soup:
        return events
    text = soup.get_text(" ", strip=True)
    for m in _LOOSE_DATE_RE.finditer(text):
        iso = _iso_from_match(m)
        if not _is_future(iso):
            continue
        events.append({
            "title": "DC4420 - London DEF CON Group Meetup",
            "date": iso,
            "url": "https://dc4420.org/",
            "source": "DC4420",
            "location": "London",
        })
        break
    return events


@source("Cyber & Infosec Search", "🔐", "Cyber & Infosec")
def scrape_cyber_infosec():
    events, seen = [], set()
    for kind, slug in _SECURITY_SEARCHES:
        try:
            found = (_scrape_eventbrite(slug, "Cyber & Infosec Search") if kind == "eventbrite"
                     else _scrape_allevents(slug, "Cyber & Infosec Search"))
        except Exception as e:
            log.warning(f"Cyber search {kind}/{slug} failed: {e}")
            continue
        for ev in found:
            title = (ev.get("title") or "").strip()
            if not _SECURITY_TITLE_RE.search(title) or title.lower() in seen:
                continue
            seen.add(title.lower())
            events.append(ev)
    return events[:25]


# ─────────────────────────────────────────────
# INTELLIGENCE & SECURITY / DEFENCE & GEOPOLITICS  (keyword-filtered aggregate)
# ─────────────────────────────────────────────
# The same problem as the cyber block above, one category over - and worse.
# The dedicated intel and defence sources are few and genuinely quiet: BISI
# and Intelligence Forums are both alive and parsing correctly but have
# published nothing dated in the future for months (checked live - every
# article on both pages carries a past date), and RUSI's events page is a
# single un-paginated screen of ~10. That left Intelligence & Security on 13
# events and Defence & Geopolitics on 14, against 128 for Business &
# Networking - the wrong shape for this catalog's actual audience, and the
# reason a security specialist could upload a strong CV and be matched
# mostly against startup networking.
#
# The listing-site searches DO carry real intel and defence events, they are
# just buried: "national-security" returns a Halloween party and a fashion
# afterparty next to a genuine emergency-briefing panel, and "defence" is
# overwhelmingly self-defence classes. So the cyber block's rule applies
# here too - keep only titles that actually name the work, and drop the
# martial-arts feed outright. Precision over volume.

_INTEL_TITLE_RE = re.compile(
    r"\b(osint|open.?source intelligence|socmint|humint|sigint|geoint|imint"
    r"|counter.?terror\w*|counterterror\w*|terroris\w*|counter.?extremis\w*|radicalis\w*"
    r"|espionage|spycraft|spymaster|tradecraft|covert action|clandestine"
    r"|intelligence (analy\w*|studies|communit\w*|agenc\w*|service\w*|officer\w*"
    r"|gathering|sharing|failure\w*|assessment\w*|cycle)"
    r"|(strategic|competitive|criminal|military|financial|threat) intelligence"
    r"|national security|homeland security|protective security|security clearance"
    r"|due diligence|sanctions|anti.?money.?laundering|\baml\b|\bkyc\b"
    r"|financial crime|illicit finance|money launder\w*|asset tracing"
    r"|fraud (investigat\w*|risk|prevention|conference|summit)"
    r"|investigative journalis\w*|open.?source investigat\w*"
    r"|insider threat|hostile state\w*|foreign interference|state threat\w*"
    r"|disinformation|misinformation|information operations|influence operations"
    r"|hybrid (threat|warfare)|counter.?intelligence|surveillance state)\b",
    re.IGNORECASE,
)

_GEOPOL_TITLE_RE = re.compile(
    r"\b(geopolitic\w*|geostrateg\w*|geoeconomic\w*|grand strategy|statecraft"
    r"|foreign polic\w*|international relations|international security"
    r"|international affairs|world order|global security"
    r"|defence (polic\w*|review|tech\w*|innovation|industr\w*|procurement"
    r"|conference|summit|studies|secretary|spending)"
    r"|defense (polic\w*|tech\w*|industr\w*|conference|summit|studies)"
    r"|\bnato\b|european defence|transatlantic"
    r"|arms control|arms race|nuclear (weapon\w*|deterren\w*|proliferation|posture)"
    r"|deterrence|armed forces|military (strategy|power|balance|aid|doctrine|history)"
    r"|warfare|war studies|warfight\w*|peacekeeping|insurgen\w*"
    r"|drone warfare|unmanned|autonomous weapon\w*|maritime security|naval power"
    r"|sanctions regime|export controls|economic statecraft"
    r"|diplomac\w*|diplomatic|multilateralis\w*)\b",
    re.IGNORECASE,
)

# A country name on its own is not a geopolitics signal - "China Mid-Autumn
# Festival" and "Learn Russian in Shoreditch" are not defence events. It
# counts only when the title also names a strategic frame, in either order,
# within a short window. This is what rescues the real ones the keyword list
# above misses by construction, e.g. "China's Global Strategy Under Xi
# Jinping" and "The Battle for the Arctic and the New World Order".
_GEO_COUNTRY_RE = (r"(ukraine|russia\w*|china|chinese|iran\w*|israel\w*|gaza|taiwan"
                   r"|north korea|indo.?pacific|middle east|the sahel|arctic|nato|europe)")
_GEO_FRAME_RE   = (r"(strateg\w*|polic\w*|security|militar\w*|\bwar\b|conflict|invasion"
                   r"|relations|order|power|threat|sanctions|regime|alliance|deterren\w*"
                   r"|geopolit\w*|foreign|defence|defense|intelligence|nuclear)")
_COUNTRY_CONTEXT_RE = re.compile(
    rf"\b{_GEO_COUNTRY_RE}\b.{{0,45}}\b{_GEO_FRAME_RE}\b"
    rf"|\b{_GEO_FRAME_RE}\b.{{0,45}}\b{_GEO_COUNTRY_RE}\b",
    re.IGNORECASE,
)

# "Defence" in a London listings feed is mostly self-defence classes, and no
# title regex below should ever be the only thing standing between a karate
# workshop and the Defence & Geopolitics category.
_NOT_SECURITY_RE = re.compile(
    r"\b(self.?defen[cs]e|krav maga|karate|kickbox\w*|jiu.?jitsu|taekwondo"
    r"|martial art\w*|boxing|stick.?boxing|womens? defence|personal safety class)\b",
    re.IGNORECASE,
)


def _keyword_aggregate(searches, title_re, source_name, extra_re=None, limit=25):
    """Run listing-site searches and keep only the titles that name the work.

    Generalised from scrape_cyber_infosec, which did exactly this inline for
    one category. A search that fails is skipped, never fatal: these feeds
    are third-party and a 403 on one slug must not empty the category."""
    events, seen = [], set()
    for kind, slug in searches:
        try:
            found = (_scrape_eventbrite(slug, source_name) if kind == "eventbrite"
                     else _scrape_allevents(slug, source_name))
        except Exception as e:
            log.warning(f"{source_name}: search {kind}/{slug} failed: {e}")
            continue
        for ev in found:
            title = (ev.get("title") or "").strip()
            if not title or title.lower() in seen:
                continue
            if _NOT_SECURITY_RE.search(title):
                continue
            if not (title_re.search(title) or (extra_re and extra_re.search(title))):
                continue
            seen.add(title.lower())
            events.append(ev)
    return events[:limit]


# AllEvents has no slug that maps to either category (its nearest is
# "workshops"), so both of these are Eventbrite-only by design rather than
# by omission.
_INTEL_SEARCHES = [("eventbrite", "intelligence"), ("eventbrite", "national-security"),
                   ("eventbrite", "counter-terrorism"), ("eventbrite", "security"),
                   ("eventbrite", "investigation")]

_GEOPOL_SEARCHES = [("eventbrite", "geopolitics"), ("eventbrite", "defence"),
                    ("eventbrite", "foreign-policy"),
                    ("eventbrite", "international-relations"), ("eventbrite", "war")]


@source("Intelligence & Security Search", "🔎", "Intelligence & Security")
def scrape_intel_search():
    return _keyword_aggregate(_INTEL_SEARCHES, _INTEL_TITLE_RE,
                              "Intelligence & Security Search")


@source("Defence & Geopolitics Search", "🧭", "Defence & Geopolitics")
def scrape_geopol_search():
    return _keyword_aggregate(_GEOPOL_SEARCHES, _GEOPOL_TITLE_RE,
                              "Defence & Geopolitics Search",
                              extra_re=_COUNTRY_CONTEXT_RE)


# ─────────────────────────────────────────────
# FOREIGN AFFAIRS / DEFENCE INSTITUTIONS
# ─────────────────────────────────────────────
# Named organisations that actually publish a dated upcoming programme. Each
# one below was checked live before being added; the ones that did not
# survive that check are recorded here so nobody re-adds them on a hunch:
#   IISS and Chatham House    - 403 to any scraping approach (curated only).
#   LSE and King's College    - calendars render client-side; one page of
#                               server-rendered HTML yields ~1 relevant event.
#   Bellingcat workshops      - no dated upcoming workshops published.
#   The Security Institute    - event links are not in the server HTML.

# "September 9, 2026" - month-first, which _LOOSE_DATE_RE (day-first) misses.
_MDY_DATE_RE = re.compile(
    r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def _iso_from_mdy(m):
    try:
        month = _MONTH_LOOKUP.get(m.group(1).lower()[:3])
        if not month:
            return None
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"
    except (ValueError, AttributeError):
        return None


@source("Frontline Club", "🎥", "Defence & Geopolitics")
def scrape_frontline_club():
    """Frontline Club - the foreign-correspondents' club in Paddington.

    Panel discussions, screenings and book talks on conflict, foreign policy
    and investigative journalism, several a week and open to non-members.
    Cards are .eaw-content-wrap: h3.eaw-title carries title and link, the
    <time> next to it carries a month-first date the generic day-first
    parser cannot read."""
    events = []
    soup = fetch("https://www.frontlineclub.com/events/")
    if not soup:
        return events
    for wrap in soup.select(".eaw-content-wrap"):
        a = wrap.select_one("h3.eaw-title a[href]")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url   = fix_url(a.get("href", ""), "https://www.frontlineclub.com")
        t     = wrap.select_one("time")
        m     = _MDY_DATE_RE.search(t.get_text(" ", strip=True)) if t else None
        iso   = _iso_from_mdy(m) if m else None
        if not (is_valid_event(title) and is_valid_url(url)):
            continue
        if not _is_future(iso):
            continue
        events.append({"title": title, "date": iso, "url": url,
                       "source": "Frontline Club", "location": "London"})
    return events[:15]


@source("Council on Geostrategy", "🗺️", "Defence & Geopolitics")
def scrape_geostrategy():
    """Council on Geostrategy - Geostrategy Forums, Whitehall Briefings and
    strategic-forum sessions, all in London. Event blocks are <article>s with
    the title in an h3 and a day-first date in the block text."""
    events, seen = [], set()
    soup = fetch("https://www.geostrategy.org.uk/events/")
    if not soup:
        return events
    for art in soup.select("article"):
        h = art.select_one("h3, h2")
        a = art.select_one('a[href*="/event/"]')
        if not (h and a):
            continue
        title = h.get_text(" ", strip=True)
        url   = fix_url(a.get("href", ""), "https://www.geostrategy.org.uk")
        m     = _LOOSE_DATE_RE.search(art.get_text(" ", strip=True))
        iso   = _iso_from_match(m) if m else None
        if not (is_valid_event(title) and is_valid_url(url)):
            continue
        if not _is_future(iso) or title.lower() in seen:
            continue
        seen.add(title.lower())
        events.append({"title": title, "date": iso, "url": url,
                       "source": "Council on Geostrategy", "location": "London"})
    return events[:15]


@source("SASIG", "🤝", "Cyber & Infosec")
def scrape_sasig():
    """SASIG - free security briefings and webinars for security leaders,
    several a week. No date parsing needed at all: every event link is
    /calendar/event/YYYY-MM-DD-slug/, so the date is in the URL and cannot
    drift out of sync with the title the way a scraped date string can."""
    events, seen = [], set()
    soup = fetch("https://www.thesasig.com/events/")
    if not soup:
        return events
    href_date = re.compile(r"/calendar/event/(\d{4})-(\d{2})-(\d{2})-")
    for a in soup.select('a[href*="/calendar/event/"]'):
        href = a.get("href", "")
        m = href_date.search(href)
        if not m:
            continue
        iso   = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        title = a.get_text(" ", strip=True)
        url   = fix_url(href, "https://www.thesasig.com")
        if not (is_valid_event(title) and is_valid_url(url)):
            continue
        if not _is_future(iso) or url in seen:
            continue
        seen.add(url)
        events.append({"title": title, "date": iso, "url": url, "source": "SASIG"})
    return events[:20]

# ─────────────────────────────────────────────
# LUMA DISCOVER
# ─────────────────────────────────────────────

# Luma's geo-discover endpoint returns EVERY public event within the radius
# regardless of topic - confirmed live: of ~43 results, the clear majority
# were hikes, brunches, padel, pottery, wakeboarding, canoeing, picnics.
# Genuinely relevant ones ("Monad Blitz London Hackathon", "HackWimbledon",
# "Introduction to Electronics and Computing") were a small minority mixed
# in. Tagging the whole feed "Builder & Tech Community" was wrong for most
# of it - same "precision over volume" call as _SECURITY_TITLE_RE above:
# the API gives us no category/description field to filter on, only the
# title, so title keywords are what we have. A smaller, genuinely-relevant
# list beats a padded, mislabelled one.
_BUILDER_TECH_TITLE_RE = re.compile(
    r"\b(hack\w*|build\w* ?(circle|night|day|week)"
    r"|startup\w*|founder\w*|venture capital|\bvc\b|demo ?day|pitch ?night|pitch ?event"
    r"|developer\w*|\bdevs?\b|software eng\w*|engineering team|programm\w*|coding\w*"
    r"|no.?code|open source|\bapi\b|saas"
    r"|artificial intelligence|\bai\b|machine learning|\bml\b|large language model|\bllm\b"
    r"|web3|blockchain|crypto\w*|\bdefi\b|\bnft\w*|ethereum|solidity|\bxrp\b|\bdao\b"
    r"|data scien\w*|data analyt\w*|electronics|computer scien\w*|computing"
    r"|product (manager|management|design)\w*|tech(nology)? (meetup|community|talk|conference|summit)"
    r"|cybersecurity|cyber ?security|infosec)\b",
    re.IGNORECASE,
)


@source("Luma London Discover", "✨", "Builder & Tech Community")
def scrape_luma_discover():
    """Luma's own London city feed, narrowed to builder/tech titles.

    Routed through _luma_event_record for the same reason the calendar and
    profile scrapers are - this is the third Luma scraper, and it had drifted
    furthest from the other two. It set no location at all (so every event
    reached the catalog with a blank venue and nothing for is_london() to
    judge), it never checked whether the date had passed, and it carried no
    Luma id, so a Discover copy of an event a tracked organiser already
    publishes could not be recognised as the same event.

    The geo radius is centred on London but it is a RADIUS - it reaches past
    the city - so the guardrail is doing real work here, not just tidying."""
    events = []
    try:
        url = "https://api.lu.ma/discover/get-paginated-events?geo_latitude=51.5074&geo_longitude=-0.1278&geo_type=circle"
        r   = requests.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}, timeout=12)
        if r.status_code != 200:
            return events
        seen = set()
        for entry in r.json().get("entries",[]):
            ev    = entry.get("event",{})
            title = ev.get("name")
            if not title or not _BUILDER_TECH_TITLE_RE.search(title) or title in seen:
                continue
            rec = _luma_event_record(ev, "Luma London Discover")
            if not rec:
                continue
            seen.add(title)
            events.append(rec)
    except Exception as e:
        log.warning(f"Luma Discover failed: {e}")
    return events[:30]

# ─────────────────────────────────────────────
# EDUCATION & RESEARCH
# ─────────────────────────────────────────────

# Imperial's London campuses, so a venue string can be turned into a positive
# London claim rather than left to the "nothing disqualified it" default.
# Deliberately an ALLOWLIST of campuses rather than a denylist of cities:
# Silwood Park (Ascot) is the one that must not be labelled London, and naming
# the London ones is the only way to be sure it is not.
_IMPERIAL_LONDON_CAMPUS_RE = re.compile(
    r"\b(south kensington|white city|hammersmith|charing cross|st mary'?s"
    r"|chelsea and westminster|royal brompton)\b", re.IGNORECASE)


@source("Imperial College", "🎓", "Education & Research")
def scrape_imperial():
    events = []
    soup = fetch("https://www.imperial.ac.uk/whats-on/")
    if not soup:
        return events
    for el in soup.select(".event"):
        h     = el.select_one("h2, h3, h4, .event-title, [class*=title]")
        a     = el.select_one("a[href]")
        d     = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.imperial.ac.uk")
        date  = d.get_text(strip=True) if d else None
        # .venue holds the real building and campus ("Huxley Building, South
        # Kensington Campus"). Worth reading for two reasons: it is what makes
        # the map and the distance figure work at all, and Imperial is not
        # entirely a London university - Silwood Park is in Ascot, and only a
        # campus name distinguishes it.
        v = el.select_one(".venue")
        venue = (v.get_text(" ", strip=True) if v else "").strip()
        online = bool(_ONLINE_RE.match(venue))
        if venue and not online:
            in_london = bool(_IMPERIAL_LONDON_CAMPUS_RE.search(venue)
                             or re.search(r"\blondon\b", venue, re.IGNORECASE))
            if not in_london:
                # Imperial's listing carries conferences its people are running
                # or attending abroad, and reading the venue is what finally
                # made them visible: "Xi'an Qujiang International Convention
                # Centre", "Science Congress Center, Walther-Von-Dyck" (Munich)
                # and "Belmeloro University Complex" (Bologna) were all live in
                # the catalog, kept because no location was set and the city
                # denylist cannot know every venue name on earth. With a venue
                # in hand the rule inverts safely for this source: a stated
                # venue that is not London means not London.
                continue
            if not re.search(r"\blondon\b", venue, re.IGNORECASE):
                venue = f"{venue}, London"
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url,
                           "source": "Imperial College", "location": venue,
                           "is_online": online})
    return events[:15]

# BrainStation runs campuses in several cities and tags each event with a
# campus code rather than a city name: "Demo Event TO", "Product Evenings TO",
# "Marketing Evenings LDN". Even on the /events/london page the other
# campuses' listings come through, and because the code is an abbreviation,
# is_london() cannot see it - "TO" is not the word "toronto", so the generic
# non-London city check passes it straight into a London-only catalog. Four
# Toronto events were live in the catalog because of this.
_BRAINSTATION_LONDON_CODES = {"LDN", "LON"}
_BRAINSTATION_OTHER_CAMPUSES = {"TO", "TOR", "NYC", "NY", "VAN", "MIA", "CHI", "BOS", "SF", "LA"}
# The code sits at the end of the headline part of the title, before any
# ":subtitle" - "Demo Event TO", "Demo Event TO:How Product Managers ...".
_BRAINSTATION_CAMPUS_RE = re.compile(r"\b([A-Z]{2,4})\s*$")


def _brainstation_is_london(title: str) -> bool:
    """False only when the title carries a campus code for another city.

    Deliberately a denylist, not an allowlist: an untagged title is kept,
    because this is the London page and silence means London. Plenty of real
    titles end in an unrelated acronym ("...for the NHS"), and dropping a real
    London event is the worse mistake."""
    m = _BRAINSTATION_CAMPUS_RE.search((title or "").split(":")[0].strip())
    if not m:
        return True
    code = m.group(1)
    if code in _BRAINSTATION_LONDON_CODES:
        return True
    return code not in _BRAINSTATION_OTHER_CAMPUSES


def _brainstation_date(raw):
    """ISO date for BrainStation's bare "May20" / "Jun02" strings.

    to_iso_date() would roll anything more than 60 days old forward to next
    year, which is the right call for a listing page that only advertises
    upcoming events - a bare "Jan 5" seen in December means next January. This
    page is not that: it carries past events alongside upcoming ones, so the
    roll-forward turned five finished May/June sessions into June 2027 and
    published them as upcoming. ("Design Evenings LDN" dated Jun02 was live in
    the catalog as a 2027 event.)

    So: read a bare month/day as THIS year, and only roll forward when that
    would put it absurdly far in the past - which is how the December-to-
    January wrap still resolves correctly, without resurrecting a session that
    finished in the spring."""
    iso = to_iso_date(raw)
    if not iso:
        return None
    m = re.match(r"^([A-Za-z]{3,9})\.?\s*(\d{1,2})$", str(raw).strip())
    if not m:
        return iso                      # already carried a real year
    month = _MONTH_LOOKUP.get(m.group(1).lower()[:3])
    if not month:
        return iso
    today = datetime.now(timezone.utc).date()
    try:
        candidate = date(today.year, month, int(m.group(2)))
    except ValueError:
        return iso
    if (today - candidate).days > 300:   # December seeing next January
        try:
            candidate = candidate.replace(year=today.year + 1)
        except ValueError:
            return iso
    return candidate.isoformat()


@source("BrainStation London", "📚", "Education & Research")
def scrape_brainstation():
    events = []
    soup = fetch("https://brainstation.io/events/london")
    if not soup:
        return events
    seen = set()
    for art in soup.select("article"):
        h     = art.select_one("h2, h3, h4, [class*=title]")
        a     = art.select_one("a[href]")
        d     = art.select_one("time, .date, [class*=date], [class*=time]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://brainstation.io")
        date  = d.get_text(strip=True) if d else None
        if not (title and len(title) > 5 and title not in seen and is_valid_url(url)):
            continue
        # The card states its venue outright - "482 Front St W, 2nd Floor,
        # Toronto, ON", "BrainStation Toronto" - as the second highlights
        # item, after the time range. Reading it replaces a guess with a fact:
        # _brainstation_is_london() was inferring the city from a campus
        # acronym at the end of the title, which is why four Toronto events
        # once reached the catalog. The acronym check stays as the fallback
        # for cards that carry no venue line.
        items = [li.get_text(" ", strip=True) for li in art.select("[class*=list-item]")]
        items = [i for i in items if i]
        venue = items[1] if len(items) > 1 else ""
        if venue:
            if not is_london({"title": title, "location": venue}):
                continue
        elif not (_brainstation_is_london(title) and is_london({"title": title})):
            continue
        # This listing carries past events as well as upcoming ones ("Jul08",
        # "Aug05" were both live in a mid-August catalog). Drop the ones we can
        # date and confirm have happened; keep anything undateable, same as
        # everywhere else. See _brainstation_date for why the shared parser is
        # not the right one here.
        iso = _brainstation_date(date)
        if iso and not _is_future(iso):
            continue
        seen.add(title)
        events.append({"title": title, "date": date, "url": url,
                       "source": "BrainStation London", "location": venue})
    return events[:15]

# ─────────────────────────────────────────────
# TECHUK EVENTS
# ─────────────────────────────────────────────

@source("techUK Events", "🏢", "Business & Networking")
def scrape_techuk():
    """techUK's own events listing, pre-filtered to London via the
    ?location=London query param. Static HTML with clean article/h4/date
    markup."""
    events = []
    soup = fetch("https://www.techuk.org/what-we-deliver/events.html?location=London")
    if not soup:
        return events
    for art in soup.select("article.eventfolio-calendar-event"):
        h = art.select_one("h4.article-title a")
        d = art.select_one(".article-date")
        if not (h and d):
            continue
        title = h.get_text(strip=True)
        url   = fix_url(h.get("href", ""), "https://www.techuk.org")
        m     = _LOOSE_DATE_RE.search(d.get_text(" ", strip=True))
        iso   = _iso_from_match(m) if m else None
        if not (is_valid_event(title) and is_valid_url(url) and _is_future(iso)):
            continue
        events.append({"title": title, "date": iso, "url": url,
                        "source": "techUK Events", "location": "London"})
    return events[:20]

# ─────────────────────────────────────────────
# SCHEMA.ORG JSON-LD SOURCES
# ─────────────────────────────────────────────
# Four of the sources below publish their events as schema.org JSON-LD in
# the page head. That's a far better contract than CSS selectors: it's a
# published standard the site maintains for Google, so it survives visual
# redesigns that would silently break class-name scraping. One shared
# parser serves all of them.
#
# Two shapes have to be handled, because sites use both:
#   - a bare Event / EducationEvent node (or several)
#   - an ItemList whose itemListElement[].item is the Event
# plus @graph wrapping, and @type arriving as either a string or a list.

def _jsonld_blocks(soup):
    """Every parseable application/ld+json payload on the page."""
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            out.append(json.loads(raw))
        except Exception:
            continue          # a malformed block is normal; skip it
    return out


def _jsonld_events(soup):
    """Flatten a page's JSON-LD down to just the event-shaped nodes.

    Matches any @type containing "Event" - dev.events publishes
    EducationEvent, not Event, which is exactly the kind of detail that
    makes a hardcoded == "Event" check quietly return nothing."""
    found, seen = [], set()

    def visit(node):
        if isinstance(node, list):
            for n in node:
                visit(n)
            return
        if not isinstance(node, dict):
            return
        for key in ("@graph", "itemListElement"):
            if key in node:
                visit(node[key])
        if "item" in node and isinstance(node["item"], dict):
            visit(node["item"])
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(isinstance(t, str) and "Event" in t for t in types):
            marker = id(node)
            if marker not in seen:
                seen.add(marker)
                found.append(node)

    for block in _jsonld_blocks(soup):
        visit(block)
    return found


def _jsonld_location(node) -> str:
    """schema.org location is a Place object, a bare string, or a list."""
    loc = node.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        name = (loc.get("name") or "").strip()
        addr = loc.get("address")
        if isinstance(addr, dict):
            city = (addr.get("addressLocality") or "").strip()
            if city and city.lower() not in name.lower():
                return f"{name}, {city}".strip(", ")
        elif isinstance(addr, str) and addr.strip() and not name:
            return addr.strip()
        return name
    return (loc or "").strip() if isinstance(loc, str) else ""


def _scrape_jsonld_source(urls, source_name, base=None, limit=25):
    """Generic: fetch a page, return its JSON-LD events as our event dicts.

    `urls` may be one URL or several tried in order until one yields
    events. Some of these sites sit behind a CDN that serves a different
    (JSON-LD free) variant to datacenter IPs than to a residential
    browser, and which paths that hits is inconsistent - so a second
    candidate URL is a cheap hedge rather than an outage."""
    if isinstance(urls, str):
        urls = [urls]
    for url in urls:
        events, seen = [], set()
        soup = fetch(url)
        if not soup:
            continue
        for node in _jsonld_events(soup):
            title = (node.get("name") or "").strip()
            iso = (node.get("startDate") or "")[:10]
            if not title or not _is_future(iso):
                continue
            link = (node.get("url") or "").strip()
            link = fix_url(link, base or url) if link else url
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            events.append({"title": title, "date": iso, "url": link,
                            "source": source_name,
                            "location": _jsonld_location(node)})
        if events:
            return events[:limit]
    return []


@source("dev.events London", "💻", "Tech & AI")
def scrape_dev_events():
    """dev.events' London tech listing. The visible cards are rendered
    client-side, but every conference is also emitted as a schema.org
    EducationEvent in the page head - so no headless browser needed, and
    the feed keeps flowing as they add conferences."""
    return _scrape_jsonld_source("https://dev.events/EU/GB/London/tech",
                                  "dev.events London", base="https://dev.events", limit=30)


@source("TechMeetups London", "🧩", "Builder & Tech Community")
def scrape_techmeetups():
    """techmeetups.io's London page carries an "Upcoming Tech Events in
    London" ItemList in JSON-LD. It does include the occasional non-London
    entry (a "Silicon Valley" gathering was in the live payload) - the
    catalog-level is_london() filter in app.py is what catches those."""
    return _scrape_jsonld_source("https://techmeetups.io/london",
                                  "TechMeetups London", limit=25)


@source("Event Tech Live", "🎪", "Business & Networking")
def scrape_event_tech_live():
    """Annual event-technology expo at ExCeL. Publishes the current
    edition as a JSON-LD Event, so next year's rolls in by itself.

    Both the homepage and the dated page carry the same Event node; both
    are tried because this host is one of the CDN-fronted ones that can
    serve a stripped variant to a datacenter IP."""
    return _scrape_jsonld_source(
        ["https://eventtechlive.com/", "https://eventtechlive.com/etl-london-2026/"],
        "Event Tech Live", limit=5)


@source("AI Summit London", "🤖", "Tech & AI")
def scrape_ai_summit():
    """The AI Summit London, from the JSON-LD Event on its homepage.

    Note this legitimately returns nothing between editions: at the time
    of writing the markup still advertised the finished June 2026 dates,
    which _is_future() drops. That's the correct behaviour - it starts
    feeding again the moment they publish the next edition, and until
    then the curated entry below covers it."""
    return _scrape_jsonld_source("https://london.theaisummit.com/",
                                  "AI Summit London", limit=5)


# ─────────────────────────────────────────────
# MEETUP GROUPS
# ─────────────────────────────────────────────
# Meetup is a React app - the events are not in the served DOM, but the
# full list IS in the __NEXT_DATA__ hydration blob as normalised Apollo
# nodes. One helper therefore covers ANY Meetup group, so adding a new
# community later is a single line in MEETUP_GROUPS rather than a new
# scraper.

def _scrape_meetup_group(slug: str, source_name: str, limit: int = 12):
    events = []
    soup = fetch(f"https://www.meetup.com/{slug}/events/")
    if not soup:
        return events
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        log.warning(f"Meetup {slug}: no __NEXT_DATA__ blob")
        return events
    try:
        data = json.loads(tag.string or tag.get_text() or "")
    except Exception as e:
        log.warning(f"Meetup {slug}: unparseable __NEXT_DATA__: {e}")
        return events

    nodes = []

    def visit(node):
        if isinstance(node, list):
            for n in node:
                visit(n)
        elif isinstance(node, dict):
            if node.get("__typename") == "Event":
                nodes.append(node)
            for v in node.values():
                visit(v)

    visit(data)

    seen = set()
    for ev in nodes:
        # status alone is NOT enough: recurring series carry ACTIVE
        # template entries dated months in the past (Silicon Roundabout
        # had ACTIVE events three months stale), so the real date is
        # checked too.
        if ev.get("status") in ("PAST", "CANCELLED"):
            continue
        iso = (ev.get("dateTime") or "")[:10]
        title = (ev.get("title") or "").strip()
        if not title or not _is_future(iso):
            continue
        eid = ev.get("id") or title.lower()
        if eid in seen:
            continue
        seen.add(eid)
        start = ev.get("dateTime") or ""
        events.append({
            "title": title,
            "date": iso,
            "time": start[11:16] if len(start) >= 16 else None,
            "url": ev.get("eventUrl") or f"https://www.meetup.com/{slug}/events/",
            "source": source_name,
            # ONLINE events from a London community are still relevant to a
            # London audience; label them rather than dropping them.
            "location": "Online" if ev.get("isOnline") else "London",
        })
    events.sort(key=lambda e: e["date"])
    return events[:limit]


MEETUP_GROUPS = {
    # slug: (display name, emoji, category)
    "siliconroundabout": ("Silicon Roundabout", "🔵", "Builder & Tech Community"),
    "llhs-ladies-of-london-hacking-society": (
        "Ladies of London Hacking Society", "🔐", "Cyber & Infosec"),
}

for _slug, (_name, _emoji, _cat) in MEETUP_GROUPS.items():
    def _make_meetup_scraper(slug, name):
        @source(name, _emoji, _cat)
        def _scraper():
            return _scrape_meetup_group(slug, name)
        return _scraper
    _make_meetup_scraper(_slug, _name)


# ─────────────────────────────────────────────
# LEADING DESIGN / CYBER GRIFFIN / 44CON
# ─────────────────────────────────────────────

@source("Leading Design", "🎨", "Business & Networking")
def scrape_leading_design():
    """Leading Design runs one conference per city per year. The index
    lists every edition as an <a class="promo"> carrying a machine
    readable <time datetime>, so next year's London edition appears here
    the day they publish it - no yearly edit needed. Non-London editions
    (New York) are dropped on the card's own text."""
    events = []
    soup = fetch("https://leadingdesign.com/conferences/")
    if not soup:
        return events
    for card in soup.select("a.promo"):
        t = card.select_one("time[datetime]")
        if not t:
            continue
        iso = (t.get("datetime") or "")[:10]
        text = card.get_text(" ", strip=True)
        if not _is_future(iso) or not re.search(r"\blondon\b", text, re.IGNORECASE):
            continue
        href = fix_url(card.get("href", ""), "https://leadingdesign.com")
        # card text reads "11 - 12 November London 2026"
        year = re.search(r"\b(20\d{2})\b", text)
        title = f"Leading Design London {year.group(1)}" if year else "Leading Design London"
        events.append({"title": title, "date": iso, "url": href,
                        "source": "Leading Design", "location": "London"})
    return events[:5]


@source("Cyber Griffin", "🛡️", "Cyber & Infosec")
def scrape_cyber_griffin():
    """City of London Police's Cyber Griffin briefings. Each briefing type
    (Baseline Part A/B, Case Study) is an .events-table whose rows are the
    individual sittings, each with its own Eventbrite link - so new dates
    appear automatically as the team schedules them."""
    events = []
    soup = fetch("https://cybergriffin.police.uk/events")
    if not soup:
        return events
    for table in soup.select(".events-table"):
        heading = table.find_previous(["h1", "h2", "h3", "h4"])
        label = heading.get_text(" ", strip=True) if heading else "Briefing"
        for row in table.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.select("td")]
            if not cells:
                continue                      # header row
            m = _LOOSE_DATE_RE.search(cells[0])
            iso = _iso_from_match(m) if m else None
            if not _is_future(iso):
                continue
            a = row.select_one("a[href]")
            events.append({
                "title": f"Cyber Griffin: {label}",
                "date": iso,
                "time": cells[1].split("-")[0].strip() if len(cells) > 1 else None,
                "url": a.get("href") if a else "https://cybergriffin.police.uk/events",
                "source": "Cyber Griffin",
                "location": "London",
            })
    events.sort(key=lambda e: e["date"])
    return events[:12]


@source("44CON", "🎩", "Cyber & Infosec")
def scrape_44con():
    """44CON - annual London infosec conference. Same approach as the
    BSides London and DC4420 scrapers above: the homepage states the
    conference date in prose rather than in markup, so take the first
    future full date on the page. (Their X account was the source
    originally suggested, but X requires authentication to read.)"""
    events = []
    soup = fetch("https://44con.com/")
    if not soup:
        return events
    text = soup.get_text(" ", strip=True)
    for m in _LOOSE_DATE_RE.finditer(text):
        iso = _iso_from_match(m)
        if not _is_future(iso):
            continue
        events.append({"title": "44CON", "date": iso,
                        "url": "https://44con.com/", "source": "44CON",
                        "location": "London"})
        break     # single annual conference - first future date is the one
    return events


# ─────────────────────────────────────────────
# CURATED ONE-OFF LONDON EVENTS
# ─────────────────────────────────────────────
# Each of these was individually checked live (title, date, London venue
# confirmed) from a page that can't be turned into a real scraper: some
# block scraping outright (Black Hat -> 403, BeyondTrust -> 403), some are
# client-rendered with no data in the raw HTML, some are single annual
# pages with no repeating structure to select on at all. Precision over
# volume, same call as _SECURITY_TITLE_RE / _BUILDER_TECH_TITLE_RE above.
#
# _is_future() still applies, so each entry drops off the catalog on its
# own once the date passes - but unlike a live scraper nothing regenerates
# next year's edition automatically. These need a yearly manual refresh.
CURATED_LONDON_EVENTS = [
    # Hackathons - HACKATHON_RE auto-tags these "Hackathons" from the title
    # regardless of the category set here, so it's a placeholder.
    # (Frontline London Hackathon and the Superlinked x Qwen Hackathon both
    #  moved to their hosts' Luma calendars - "Frontline London" and
    #  "Superlinked" in LUMA_CALENDARS - so they're no longer hardcoded
    #  here. Removing them also avoids listing each one twice under two
    #  slightly different titles.)
    {"title": "<vibes with kickstart/>", "date": "2026-08-21",
     "url": "https://luma.com/tmumetun", "category": "Builder & Tech Community"},

    # Cyber & Infosec
    # (44CON moved to a live scraper - scrape_44con)
    {"title": "SANS London September 2026", "date": "2026-09-07",
     "url": "https://www.sans.org/cyber-security-training-events/london-september-2026",
     "category": "Cyber & Infosec"},
    {"title": "Black Hat Europe 2026", "date": "2026-12-07",
     "url": "https://blackhat.com/europe/", "category": "Cyber & Infosec"},
    {"title": "BeyondTrust: Go Beyond London 2026", "date": "2026-09-10",
     "url": "https://www.beyondtrust.com/events/go-beyond-london", "category": "Cyber & Infosec"},
    {"title": "BeyondTrust Partner Summit London 2026", "date": "2026-09-09",
     "url": "https://www.beyondtrust.com/events/partner-summit-london", "category": "Cyber & Infosec"},

    # Intelligence & Security
    {"title": "The Global OSINT Conference 2026", "date": "2026-10-05",
     "url": "https://www.osint.uk/conference", "category": "Intelligence & Security"},
    {"title": "NextGen Intelligence Conference: Data, Cloud & AI", "date": "2026-11-16",
     "url": "https://www.luminik.io/events/nextgen-intelligence-conference-data-cloud-ai-london/",
     "category": "Intelligence & Security"},
    {"title": "Society for Intelligence History Annual Conference 2026", "date": "2026-10-11",
     "url": "https://www.intelligencehistory.org/2026conferencedetails", "category": "Intelligence & Security"},

    # Defence & Geopolitics
    {"title": "Defence in Space 2026", "date": "2026-10-27",
     "url": "https://defenceinspace.com/", "category": "Defence & Geopolitics"},
    {"title": "Counter UAS Homeland Security Europe 2026", "date": "2026-09-28",
     "url": "https://www.unmannedsystemstechnology.com/events/counter-uas-homeland-security-europe/",
     "category": "Defence & Geopolitics"},
    {"title": "DroneX Trade Show & Conference 2026", "date": "2026-09-29",
     "url": "https://dronexpo.co.uk/", "category": "Defence & Geopolitics"},
    {"title": "Defence Exports 2026", "date": "2026-09-28",
     "url": "https://www.defence-industries.com/events/defence-exports-2026", "category": "Defence & Geopolitics"},
    {"title": "Defence Aviation Safety 2026", "date": "2026-10-05",
     "url": "https://www.defenseadvancement.com/events/defence-aviation-safety/", "category": "Defence & Geopolitics"},

    # Tech & AI
    {"title": "56th European Microwave Conference (EuMW 2026)", "date": "2026-10-06",
     "url": "https://www.eumw.eu/", "category": "Tech & AI"},
    {"title": "The AI Summit London 2027", "date": "2027-06-09",
     "url": "https://london.theaisummit.com/", "category": "Tech & AI"},

    # Business & Networking
    # (Leading Design and Event Tech Live both moved to live scrapers -
    #  scrape_leading_design / scrape_event_tech_live - so they're no
    #  longer duplicated here.)

    # ── Chatham House ────────────────────────────────────────────────────
    # The Royal Institute of International Affairs, and the most obvious
    # omission in the Defence & Geopolitics category next to RUSI, which we
    # already scrape live.
    #
    # Curated rather than scraped because Cloudflare rejects the scraper at
    # the edge: /events, /events/upcoming, /rss.xml, /events/rss.xml,
    # /jsonapi/node/event and /feed all return 403, and a full set of browser
    # headers (UA, Accept, Sec-Fetch-*, encoding) does not change that - the
    # block is on the TLS handshake, so no amount of header dressing gets
    # `requests` through. The page has no JSON API behind it either.
    #
    # Their robots.txt does NOT disallow /events and sets Crawl-delay: 10,
    # so their stated policy permits crawling and the Cloudflare rule is
    # over-broad. Defeating it would still mean circumventing live bot
    # detection, which is not something to do unilaterally - the standing fix
    # is to ask Chatham House to allowlist the scraper, which their own
    # robots.txt suggests they would entertain.
    #
    # Recorded from the live public listing at /events/upcoming. Same yearly
    # manual-refresh caveat as everything else in this list, and more acute:
    # this organiser publishes continuously, so the entries below will lag.
    {"title": "Iceland's EU referendum: Implications for EU enlargement and Arctic security",
     "date": "2026-09-01", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/icelands-eu-referendum-implications-eu-enlargement-and-arctic-security",
     "category": "Defence & Geopolitics"},
    {"title": "Francis Fukuyama on his new political memoir: 'In the Realm of the Last Man'",
     "date": "2026-09-02",
     "url": "https://www.chathamhouse.org/events/all/members-event/francis-fukuyama-his-new-political-memoir-realm-last-man",
     "category": "Defence & Geopolitics"},
    {"title": "What would a ceasefire in Ukraine mean for Europe and the world?",
     "date": "2026-09-08",
     "url": "https://www.chathamhouse.org/events/all/standard-event/what-would-ceasefire-ukraine-mean-europe-and-world",
     "category": "Defence & Geopolitics"},
    {"title": "Launch of Chatham House Latin America Programme",
     "date": "2026-09-08",
     "url": "https://www.chathamhouse.org/events/all/standard-event/launch-latin-america-programme",
     "category": "Defence & Geopolitics"},
    {"title": "US at 250: Separation vs. concentration of power - America's enduring constitutional debate",
     "date": "2026-09-17",
     "url": "https://www.chathamhouse.org/events/all/standard-event/us-250-separation-vs-concentration-power-americas-enduring-constitutional",
     "category": "Defence & Geopolitics"},
    {"title": "Uzbekistan: assessing 10 years of rule under Mirziyoyev",
     "date": "2026-09-23", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/uzbekistan-assessing-10-years-rule-under-mirziyoyev",
     "category": "Defence & Geopolitics"},
    {"title": "US at 250: Interest vs. principle - the debate about America's values",
     "date": "2026-10-06",
     "url": "https://www.chathamhouse.org/events/all/standard-event/us-250-interest-vs-principle-debate-about-americas-values",
     "category": "Defence & Geopolitics"},
    {"title": "Film screening: Amerigo: The Search for the American Dream",
     "date": "2026-11-05",
     "url": "https://www.chathamhouse.org/events/all/standard-event/film-screening-amerigo-search-american-dream",
     "category": "Education & Research"},
    {"title": "Chatham House Competition policy conference 2026",
     "date": "2026-11-17",
     "url": "https://www.chathamhouse.org/events/all/conference/competition-policy-conference-2026",
     "category": "Business & Networking"},
    # Both of the following were left out on a first pass, reading only the
    # listing page. Opening the event pages themselves contradicted it:
    #
    # The AGM's venue line is "HYBRID - CHATHAM HOUSE AND ONLINE", so it is a
    # London event, not just institutional paperwork. It is genuinely
    # members-only though ("Guests will not be able to gain access"), which
    # the title says outright rather than letting someone find out at the door.
    {"title": "Chatham House Annual General Meeting (members only)",
     "date": "2026-09-08", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/members-event/annual-general-meeting-5",
     "category": "Business & Networking"},
    # The Berlin conference is "RITZ-CARLTON BERLIN AND ONLINE" - the listing
    # showed no format label at all, and the earlier note that is_london()
    # would drop it was right only about the in-person half. There is an
    # online option, so it is attendable from London; the location says both
    # so nobody books a flight on our say-so.
    {"title": "Chatham House Berlin conference 2026: Securing Europe's strategic autonomy",
     "date": "2026-11-25", "location": "Online (venue is Berlin)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/chatham-house-berlin-conference-2026",
     "category": "Defence & Geopolitics"},

    # ── Refreshed from the live Chatham House programme ────────────────────
    # chathamhouse.org 403s every scraping approach, which is why this list
    # exists at all - but a hand-kept list is only as good as its last read,
    # and the entries above it had already aged out. Each of the following
    # was taken from the current programme with its own format label, so
    # "Online" here means the event has no venue to travel to, not that the
    # format was unknown. Nothing needs deleting when these pass:
    # _scrape_curated() serves only future dates.
    {"title": "BRICS and the future of global power",
     "date": "2026-09-16", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/brics-and-future-global-power",
     "category": "Defence & Geopolitics"},
    {"title": "Botswana's future: Vice President Gaolathe on economic resilience amid global uncertainty",
     "date": "2026-09-17", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/botswanas-future-vice-president-gaolathe-economic-resilience-amid-global",
     "category": "Defence & Geopolitics"},
    {"title": "Is the Middle East entering a new era of regional security with the Mecca Pact?",
     "date": "2026-09-21", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/middle-east-entering-new-era-regional-security-mecca-pact",
     "category": "Defence & Geopolitics"},
    {"title": "Zambia's 2026 election: A test of democratic resilience",
     "date": "2026-09-22", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/zambias-2026-election-test-democratic-resilience",
     "category": "Defence & Geopolitics"},
    {"title": "How will young voters shape Morocco's political future?",
     "date": "2026-09-22", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/how-will-young-voters-shape-moroccos-political-future",
     "category": "Defence & Geopolitics"},
    {"title": "What to learn from Russia's State Duma elections",
     "date": "2026-09-22", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/what-learn-russias-state-duma-elections",
     "category": "Defence & Geopolitics"},
    {"title": "The Lake Chad Basin: Restoring regional security cooperation",
     "date": "2026-09-28", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/lake-chad-basin-restoring-regional-security-cooperation",
     "category": "Defence & Geopolitics"},
    {"title": "After the revolutions: The outlook for South Asia's Gen Z-inspired governments",
     "date": "2026-09-29", "location": "Online",
     "url": "https://www.chathamhouse.org/events/all/standard-event/after-revolutions-outlook-south-asias-gen-z-inspired-governments",
     "category": "Defence & Geopolitics"},
    {"title": "Reckoning or rhetoric: Is now the time for reparative justice on the global stage? (members only)",
     "date": "2026-10-15", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/members-event/reckoning-or-rhetoric-now-time-reparative-justice-global-stage",
     "category": "Defence & Geopolitics"},
    {"title": "US midterm elections 2026: what happens next?",
     "date": "2026-11-03", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/us-midterm-elections-2026-what-happens-next",
     "category": "Defence & Geopolitics"},
    {"title": "Iraq Initiative Conference 2026",
     "date": "2026-11-11", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/iraq-initiative-conference-2026",
     "category": "Defence & Geopolitics"},
    # The one genuinely intelligence-side event in the current programme -
    # a former National Security Advisor on future security challenges.
    # Everything else above is international affairs, and filing it under
    # Intelligence & Security to pad that category would be exactly the
    # inflation this product exists to refuse.
    {"title": "H.R. McMaster, President Trump's former National Security Advisor, discusses America's future security challenges",
     "date": "2026-12-10", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/standard-event/hr-mcmaster-president-trumps-former-national-security-advisor-discusses",
     "category": "Intelligence & Security"},
    {"title": "Chatham House Security and defence conference 2027",
     "date": "2027-03-03", "location": "London (hybrid)",
     "url": "https://www.chathamhouse.org/events/all/conference/security-and-defence-conference-2027",
     "category": "Defence & Geopolitics"},
]

_CURATED_EMOJI = {
    "Hackathons": "🛠️", "Cyber & Infosec": "🔐", "Intelligence & Security": "🧠",
    "Defence & Geopolitics": "🎖️", "Tech & AI": "🤖", "Business & Networking": "💼",
}

def _scrape_curated(category):
    """Curated entries for `category` that haven't happened yet.

    location defaults to London but an entry may override it - Chatham House
    runs webinars that are online-only, and stamping "London" on those would
    tell a reader to travel to something with no venue."""
    return [
        {**ev, "source": f"Curated — {category}",
         "location": ev.get("location", "London"),
         "is_online": (ev.get("location", "") or "").lower().startswith("online")}
        for ev in CURATED_LONDON_EVENTS
        if ev["category"] == category and _is_future(ev["date"])
    ]

def _scrape_manual(category):
    """Operator-added events for `category`, from the database.

    The catalog is rebuilt from nothing on every refresh, so an event added
    through the admin link portal only persists because this re-emits it each
    time. Same _is_future() rule as the hardcoded list above, so a manual entry
    also drops off on its own once its date passes - the row stays, for the
    record of what was added, but it stops being served.

    Deliberately tolerant: no database, or a database that errors, yields an
    empty list rather than an exception. A source that raises costs only itself
    (app.py catches per-source), but there is no reason to spend that."""
    try:
        rows = db.get_active_manual_events()
    except Exception as exc:                     # db import/connection problems
        log.warning(f"Manual events unavailable: {exc}")
        return []
    out = []
    for row in rows:
        if (row.get("category") or "") != category:
            continue
        if not _is_future(row.get("event_date")):
            continue
        out.append({
            "title": row.get("title"),
            "date": row.get("event_date"),
            "time": row.get("event_time") or "",
            "url": row.get("url"),
            "description": row.get("description") or "",
            "source": row.get("source_label") or f"Added — {category}",
            "location": row.get("location") or ("Online" if row.get("is_online") else "London"),
            "is_online": bool(row.get("is_online")),
        })
    return out


# One source per category, for both the hardcoded list and the database-backed
# one. Registration happens at import, so the set of categories has to be known
# up front - it is the fixed eight, not whatever happens to be in the table
# right now. That matters: a category with no rows today must still have a
# source registered, or the first event added to it would not appear until the
# next redeploy.
_ALL_CATEGORIES = [
    "Defence & Geopolitics", "Intelligence & Security", "Tech & AI",
    "Cyber & Infosec", "Education & Research", "Builder & Tech Community",
    "Business & Networking", "Hackathons",
]

for _cat in sorted({e["category"] for e in CURATED_LONDON_EVENTS}):
    def _make_curated_scraper(c):
        @source(f"Curated — {c}", _CURATED_EMOJI.get(c, "📌"), c)
        def _scraper():
            return _scrape_curated(c)
        return _scraper
    _make_curated_scraper(_cat)

for _cat in _ALL_CATEGORIES:
    def _make_manual_scraper(c):
        @source(f"Added — {c}", _CURATED_EMOJI.get(c, "📌"), c)
        def _scraper():
            return _scrape_manual(c)
        return _scraper
    _make_manual_scraper(_cat)

# ─────────────────────────────────────────────
# LUMA CALENDARS
# ─────────────────────────────────────────────

# name: (calendar id, emoji, category)
LUMA_CALENDARS = {
    "Plugged":          ("cal-FAtYQ9ilaLj34DO", "🔌", "Builder & Tech Community"),
    "Encode Club":      ("cal-8LJYo5N7QObN2DI", "⛓️", "Builder & Tech Community"),
    "Claude Community": ("cal-TOpA5LAFfuDeFpu", "🟠", "Builder & Tech Community"),
    "AI Native Dev":    ("cal-uYzPjdxdCyDtuNO", "⚡", "Builder & Tech Community"),
    "SRV Frontier":     ("cal-LbyWro3ZdQSojJX", "🚀", "Builder & Tech Community"),
    "Vercel Events":    ("cal-hp9HP2UFTGNaMnY", "▲", "Builder & Tech Community"),
    # Added from event links supplied by the operator. Each one is the
    # HOST's calendar rather than the single event that was sent, so the
    # organiser keeps feeding us as they schedule new things.
    # Several are global (Raycast runs Raycafés in Porto, Chennai,
    # Chattanooga, Köln; incident.io runs Austin/NYC/SF) - the London
    # filter in scrape_luma_calendar is what keeps this to London.
    "Raycast Community": ("cal-KwZeQ0HC9LFQ3Fk", "🔦", "Builder & Tech Community"),
    "Tech: Europe":      ("cal-qyEpCltsspbMoJR", "🇪🇺", "Builder & Tech Community"),
    "incident.io":       ("cal-0jikzPhENNNJBGU", "🚨", "Tech & AI"),
    "Dex Live":          ("cal-40Ym3ZLnIXQCmUd", "🎙️", "Tech & AI"),
    "Superlinked":       ("cal-N7kd4GStFtvqpHn", "🔗", "Builder & Tech Community"),
    "Frontline London":  ("cal-TfyKmqJMRKifRSw", "🛰️", "Defence & Geopolitics"),
    "Launch London":     ("cal-Msjwg9quXv2wERr", "🎈", "Business & Networking"),
    "Halkin Offices":    ("cal-sTpcuYmvvYXvEtd", "🏛️", "Business & Networking"),
    # The Hack Collective is a London hackathon AGGREGATOR calendar, not a
    # single host - it carries other organisers' events, so it gets a higher
    # page limit than the host calendars above. luma.com/thehackcollective and
    # the api.luma.com/ics/get?entity=calendar&id=cal-Qk1P4msjA8eRxCs feed are
    # the same calendar; the JSON endpoint is used because the ICS one gives
    # coordinates but no city name.
    "The Hack Collective": ("cal-Qk1P4msjA8eRxCs", "🛠️", "Hackathons"),
    # Re-tested and revived: this was previously dismissed for returning zero
    # upcoming items. That was true on the day and wrong as a policy - a quiet
    # calendar is not a dead one. Sources are now kept and allowed to return
    # zero rather than rejected on a single empty read.
    "Corgi London": ("cal-v8PuFjastlj2pZp", "🐕", "Business & Networking"),
    # RETIRED: "Jody Saunders" (cal-yzm8pBHRjoQCz1E) - the calendar returns
    # HTTP 404, it has been deleted upstream.
    # NOT ADDED, deliberately: "Future: UK" (cal-eP031AKL1RBuO3j) - its only
    # upcoming items are "Optimistic Picnic at Hyde Park" and "Dinners -
    # Interest", which are the same not-a-tech-event category we stripped
    # out of the Luma Discover feed.
    # RE-TESTED, still empty: luma.com/londonai (cal-zUWmkxeBGlQQenp, "Air
    # Street events") and luma.com/london-ai (cal-OErAx8480sqDAtW, "AI Startup
    # Events - London") both answer 200 with no upcoming items.
    # UNRESOLVED: "KS Events" - the original link was never recorded here and
    # the obvious slugs 404, so it could not be re-tested.
}

# Aggregator calendars carry many organisers, so they page deeper than the
# single-host calendars.
_LUMA_DEEP_CALENDARS = {"The Hack Collective": 50}


# ─────────────────────────────────────────────
# LUMA GEOGRAPHY GUARDRAIL
# ─────────────────────────────────────────────
#
# The rule used to be "drop it only if the timezone names somewhere else",
# followed by `location = city or "London"`. Both halves leaked, in opposite
# directions:
#
#   * An event with NO timezone was KEPT and then STAMPED "London". So a San
#     Francisco meetup with a missing tz did not merely survive the scraper -
#     it arrived carrying a London label, which is_london() then waved through
#     as an explicit London claim. That is the whole mechanism behind "why is a
#     Singapore event in my catalog".
#   * A genuinely ONLINE event in America/Los_Angeles was dropped, even though
#     virtual events are explicitly in scope.
#
# Geography is decided positively now: keep it when Luma says it is online, or
# when it names a city we recognise as UK, or when it names no city at all but
# sits in Europe/London. Everything else - a named foreign city, or no signal
# whatsoever - is refused. And a city is never invented: "London" is written
# only when Luma actually said London, or gave nothing but a UK timezone.

# The country field settles it whenever Luma supplies one, which is on every
# geocoded event. Matching city names against a UK list is only the fallback,
# and it is a fallback for a reason: on the first run "New York" was accepted
# as a London event, because "York" is an English city and \byork\b matches
# inside it. A stated country cannot be talked around that way.
_UK_COUNTRY_RE = re.compile(
    r"^\s*(uk|u\.k\.|gb|gbr|united kingdom|great britain|england|scotland|wales"
    r"|northern ireland|britain)\s*$", re.IGNORECASE)

# Used only when there is no country at all. "york" carries a negative
# lookbehind so it cannot be reached through "New York"; the rest are
# unambiguous enough as a city value.
_UK_PLACE_RE = re.compile(
    r"\b(london|uk|u\.k\.|gb|gbr|united kingdom|great britain|england|scotland"
    r"|wales|northern ireland"
    r"|manchester|birmingham|leeds|glasgow|edinburgh|bristol|liverpool|cardiff"
    r"|belfast|newcastle|sheffield|nottingham|oxford|cambridge|brighton|reading"
    r"|milton keynes|coventry|leicester|southampton|portsmouth|bath"
    r"|aberdeen|dundee|norwich|exeter|swansea|derby|hull|stoke|luton|slough"
    r"|croydon|watford|guildford|cheltenham|kingston upon thames"
    r"|(?<!new )york)\b",
    re.IGNORECASE,
)


def luma_geo(ev):
    """(keep, location, is_online) for one Luma API event object.

    `keep` is the strict guardrail: London or genuinely virtual, per the
    catalog rule. `location` is what Luma actually said - never a guess - so
    the catalog-level is_london() check downstream has a true value to judge,
    and a UK-but-not-London city stays visible to it rather than disguised."""
    # Online is a positive claim from Luma itself, never inferred from a title.
    #
    # location_type is the field that actually answers this - "offline" for a
    # venue, "online"/"virtual" for a stream. The presence of virtual_info is
    # NOT the answer, however much it looks like one: Luma attaches
    # {"has_access": false} to every event it returns, physical ones included,
    # so treating it as a signal marks the entire catalog online. It is only
    # worth consulting when location_type is missing AND there is no address.
    geo = ev.get("geo_address_info") or {}
    loc_type = (ev.get("location_type") or "").strip().lower()
    if loc_type in ("online", "virtual", "zoom", "remote"):
        return True, "Online", True
    if not loc_type and not geo:
        virtual = ev.get("virtual_info") or {}
        if ev.get("zoom_meeting_url") or ev.get("meeting_url") or virtual.get("url"):
            return True, "Online", True

    city = (geo.get("city") or geo.get("city_state") or "").strip()
    country = (geo.get("country") or "").strip()
    code = (geo.get("country_code") or "").strip()
    region = (geo.get("region") or "").strip()
    tz = (ev.get("timezone") or "").strip()

    # A stated country is the strongest signal there is - believe it, in both
    # directions. This is what refuses San Francisco and Singapore even on an
    # otherwise London-scoped calendar, and it is also what keeps a UK town
    # nobody thought to list ("Slough, United Kingdom") instead of discarding
    # it for being unrecognised.
    if country or code:
        uk = bool(_UK_COUNTRY_RE.match(country) or _UK_COUNTRY_RE.match(code))
        return uk, (city or region or country or "United Kingdom"), False

    if city or region:
        return bool(_UK_PLACE_RE.search(f"{city} {region}")), (city or region), False

    # No address at all. A Europe/London timezone is a UK claim and the only
    # thing left to go on; anything else - including no timezone whatsoever -
    # is refused rather than assumed, because "assume London" is exactly how
    # the strays got in.
    if tz == "Europe/London":
        return True, "London", False
    return False, tz or "", False


def _luma_event_url(ev):
    """One canonical URL per Luma event, so co-hosted listings collide.

    The same event reached through a calendar and through a host profile used
    to come back as lu.ma/<slug> from one and luma.com/<slug> from the other.
    event_id() is a hash of title+url, so those two hashed differently and both
    survived the id dedupe - the very duplicate the id pass exists to kill.
    One host, always."""
    slug = ev.get("url") or ev.get("api_id") or ""
    if not slug:
        return "https://lu.ma"
    if slug.startswith("http"):
        return re.sub(r"^https?://(?:www\.)?luma\.com/", "https://lu.ma/", slug)
    return f"https://lu.ma/{slug}"


def _luma_event_record(ev, source_name):
    """Shared Luma entry -> catalog event, or None when the guardrail refuses.

    Both the calendar scraper and the profile scraper funnel through here, so
    the London/virtual rule, the upcoming rule and the dedupe key cannot drift
    apart between them - which they had."""
    title = ev.get("title") or ev.get("name")
    if not title:
        return None
    iso = (ev.get("start_at") or "")[:10]
    if not _is_future(iso):
        return None                       # past events are never a recommendation
    keep, location, online = luma_geo(ev)
    if not keep:
        return None
    start = ev.get("start_at", "")
    return {
        "title": title,
        "date": iso,
        "time": start[11:16] if len(start) >= 16 else None,
        "url": _luma_event_url(ev),
        "source": source_name,
        "location": location or "London",
        "is_online": online,
        # Luma's own id for the event. One co-hosted event has exactly one of
        # these however many organisers list it, which makes it a stronger
        # dedupe key than title+date will ever be.
        "luma_id": ev.get("api_id") or None,
    }


def luma_calendar_status(cal_id):
    """HTTP status for a calendar's items endpoint, or None if it could not be
    asked. Used only to tell "nothing scheduled" apart from "we cannot see it".

    A private or deleted calendar answers 401/404 and yields zero events, which
    is indistinguishable at the catalog level from a real organiser having a
    quiet fortnight - and the dashboard was reporting both as "a quiet
    organiser is not a dead one, worth retrying later". One of those is worth
    retrying and the other never will be."""
    try:
        r = requests.get("https://api.lu.ma/calendar/get-items",
                         params={"calendar_api_id": cal_id, "pagination_limit": 1},
                         headers={"User-Agent": HEADERS["User-Agent"],
                                  "Accept": "application/json"}, timeout=10)
        return r.status_code
    except Exception as exc:
        log.warning(f"Luma status probe failed for {cal_id}: {exc}")
        return None


def scrape_luma_calendar(name, cal_id, limit=20):
    """One Luma calendar -> its upcoming London-or-online events.

    Geography and recency are both handled by _luma_event_record, which the
    profile scraper below shares, so the two cannot drift apart. Two things
    that were wrong here specifically:

      * There was no upcoming filter at all. get-items answers with whatever
        the calendar holds, so past events were being ingested and only fell
        out later, if at all - the profile scraper had an _is_future() guard
        and this one simply did not.
      * `location = city or "London"` invented a city for every event Luma had
        no geo for. See the guardrail note above luma_geo() for why that was
        the leak rather than a convenience.

    `period=future` is asked for as well as filtered: if Luma honours it the
    page budget is spent entirely on events that can still be attended, and if
    it ignores the parameter the local filter is what actually decides."""
    events = []
    try:
        url = (f"https://api.lu.ma/calendar/get-items"
               f"?calendar_api_id={cal_id}&period=future&pagination_limit={limit}")
        r   = requests.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}, timeout=12)
        if r.status_code != 200:
            log.warning(f"Luma {name}: HTTP {r.status_code}")
            return events
        for entry in r.json().get("entries",[]):
            rec = _luma_event_record(entry.get("event") or {}, name)
            if rec:
                events.append(rec)
    except Exception as e:
        log.warning(f"Luma {name} failed: {e}")
    return events

for _name, (_cal_id, _emoji, _cat) in LUMA_CALENDARS.items():
    def _make_scraper(n, c, e, cat):
        @source(n, e, cat)
        def _scraper():
            return scrape_luma_calendar(n, c, limit=_LUMA_DEEP_CALENDARS.get(n, 20))
        return _scraper
    _make_scraper(_name, _cal_id, _emoji, _cat)


# ─────────────────────────────────────────────
# LUMA USER PROFILES
# ─────────────────────────────────────────────
#
# CORRECTION. An earlier version of this file retired GDG London with the note
# that "Luma exposes no public user-events API (get-events /
# get-profile-items / get-hosting-events all return 404)" and that a headless
# browser would be needed. That was wrong. Those 404s came from probing
# api.LU.MA with guessed path names; the endpoint lives on api.LUMA.COM after
# the rename - the same rename the note itself had already spotted two lines
# down, applied to a CSS selector but not to the API host:
#
#   GET https://api.luma.com/user/profile/events-hosting
#       ?user_api_id=usr-XXXX&period=future&pagination_limit=N
#
# It returns the identical entries[].event shape the calendar endpoint does,
# so this is a near-copy of scrape_luma_calendar rather than a new dependency.
# GDG London itself is re-tested and genuinely has no upcoming events today
# (HTTP 200, zero entries) - right outcome, wrong reason.

def _luma_user_api_id(username):
    """Resolve a luma.com/user/<handle> to its usr- id.

    The handle in that URL is EITHER a vanity username or a raw usr- id -
    Luma only mints the vanity form for accounts that choose one, and a
    profile with username: null is reachable only by its id. An id needs no
    lookup, so it short-circuits; anything else is resolved off the page."""
    if (username or "").startswith("usr-"):
        return username
    try:
        text = _luma_html(f"https://luma.com/user/{username}")
        if not text:
            return None
        m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        return (data.get("props", {}).get("pageProps", {})
                    .get("initialData", {}).get("user", {}).get("api_id"))
    except Exception as e:
        log.warning(f"Luma user {username} lookup failed: {e}")
        return None


# ─────────────────────────────────────────────
# RESOLVING WHATEVER THE OPERATOR PASTED
# ─────────────────────────────────────────────
#
# The dashboard used to accept a cal- id, a usr- id, or a /u/ URL, and nothing
# else. That is not what Luma links look like in the wild, and it is why real
# organisers were being missed rather than refused:
#
#   luma.com/user/usr-XXXX      the actual profile URL format - the /u/ pattern
#                               the old parser looked for is not what Luma
#                               emits, so every profile link failed to parse
#   luma.com/user/some-handle   vanity profile
#   luma.com/londonai           vanity CALENDAR url, no cal- id anywhere in it
#   lu.ma/cal-XXXX              already fine
#
# Everything is resolved to a canonical cal-/usr- api id here, which is also
# what stops the same organiser being tracked twice under two different names:
# a vanity URL and its cal- id are one identifier after this, not two.

_LUMA_EVENT_HINT = ("that is a link to a single EVENT, not to an organiser. "
                    "Add it from the Add-by-Link tab instead.")


def _luma_html(url, attempts=3):
    """Fetch a Luma HTML page, backing off when Luma throttles us.

    Luma rate-limits its own website far harder than its API, and it counts
    per source IP - which on Railway means one shared address doing a full
    scrape every REFRESH_MINUTES. Locally this path answered 200 every time;
    in production the first vanity-URL lookup came back HTTP 429 and the
    dashboard reported "Could not read that Luma page". Same code, different
    IP reputation, which is exactly the class of bug that only shows up live.

    The API resolver above is preferred for everything it can answer. This is
    for user handles, which have no API equivalent."""
    delay = 1.5
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 503) and attempt < attempts - 1:
                wait = float(r.headers.get("Retry-After") or delay)
                log.info(f"Luma throttled {url} ({r.status_code}); retrying in {wait:.1f}s")
                time.sleep(min(wait, 8))
                delay *= 2
                continue
            log.warning(f"Luma page {url}: HTTP {r.status_code}")
            return None
        except Exception as exc:
            log.warning(f"Luma page {url} lookup failed: {exc}")
            return None
    return None


def _luma_next_data(url):
    """The __NEXT_DATA__ blob off a Luma page, parsed, or None."""
    text = _luma_html(url)
    if not text:
        return None
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except Exception as exc:
        log.warning(f"Luma page {url}: __NEXT_DATA__ did not parse: {exc}")
        return None


def _luma_resolve_slug(slug):
    """(identifier, kind, name, error) for a Luma vanity slug, via the API.

    api.lu.ma/url?url=<slug> answers {"kind": "calendar"|"event", "data": ...}
    for any vanity path, without touching the website - so it is not subject to
    the page throttling that broke the HTML route in production. It does NOT
    resolve user handles (404), which is why the page fallback still exists."""
    try:
        r = requests.get("https://api.lu.ma/url", params={"url": slug},
                         headers={"User-Agent": HEADERS["User-Agent"],
                                  "Accept": "application/json"}, timeout=12)
        if r.status_code != 200:
            return "", "", "", ""          # empty error = "try the page instead"
        payload = r.json()
    except Exception as exc:
        log.warning(f"Luma url resolver failed for {slug}: {exc}")
        return "", "", "", ""

    kind = (payload.get("kind") or "").strip()
    data = payload.get("data") or {}
    node = data.get(kind) if isinstance(data.get(kind), dict) else data

    if kind == "event":
        # An event page also carries the calendar hosting it, which is the
        # thing the operator almost certainly wanted. Naming it turns a dead
        # end into one copy-paste.
        ev_name = ((node or {}).get("name") or "").strip()
        host = data.get("calendar") if isinstance(data.get("calendar"), dict) else {}
        hint = (f"\"{ev_name}\" is a single EVENT, not an organiser. " if ev_name
                else "That is a link to a single EVENT, not an organiser. ")
        hint += "Add it from the Add-by-Link tab instead."
        if host.get("api_id"):
            hint += (f" To track the organiser instead, paste {host['api_id']}"
                     + (f" ({host.get('name')})" if host.get("name") else "") + ".")
        return "", "", "", hint

    if kind in ("calendar", "user") and isinstance(node, dict) and node.get("api_id"):
        return node["api_id"], kind, (node.get("name") or "").strip(), ""
    return "", "", "", ""


def _luma_entity_from_page(url):
    """(identifier, kind, name, error) for a Luma page that carries no id.

    Walks the page's own data for the calendar or user it describes. An event
    page is recognised and refused explicitly rather than being mistaken for an
    organiser - pasting an event link into the organiser box is an easy slip
    and deserves a real answer."""
    data = _luma_next_data(url)
    if not data:
        return "", "", "", ""      # empty: the caller still has routes to try
    initial = (data.get("props", {}).get("pageProps", {}).get("initialData", {}) or {})
    inner = initial.get("data") or initial

    if isinstance(inner, dict) and isinstance(inner.get("event"), dict):
        # An event page also carries the calendar hosting it, which is the
        # thing the operator almost certainly wanted. Naming it turns a dead
        # end into one copy-paste - that is the whole point of tracking the
        # organiser rather than the single event they happened to send.
        ev_name = (inner["event"].get("name") or "").strip()
        host = inner.get("calendar") if isinstance(inner.get("calendar"), dict) else {}
        host_id = host.get("api_id") or ""
        hint = _LUMA_EVENT_HINT
        if ev_name:
            hint = f"\"{ev_name}\" is a single EVENT, not an organiser. " + hint.split("not to an organiser. ")[-1]
        if host_id:
            hint += (f" To track the organiser instead, paste {host_id}"
                     + (f" ({host.get('name')})" if host.get("name") else "") + ".")
        return "", "", "", hint

    for key, kind in (("calendar", "calendar"), ("user", "user")):
        node = inner.get(key) if isinstance(inner, dict) else None
        if isinstance(node, dict) and node.get("api_id"):
            return (node["api_id"], kind,
                    (node.get("name") or "").strip(), "")

    # Fall back to a walk: Luma has moved this blob around more than once, and
    # a shifted key should not read as "no such organiser".
    found = {}

    def walk(node):
        if isinstance(node, dict):
            api_id = node.get("api_id")
            if isinstance(api_id, str):
                if api_id.startswith("cal-") and "calendar" not in found:
                    found["calendar"] = (api_id, (node.get("name") or "").strip())
                elif api_id.startswith("usr-") and "user" not in found:
                    found["user"] = (api_id, (node.get("name") or "").strip())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    for kind in ("calendar", "user"):
        if kind in found:
            ident, nm = found[kind]
            return ident, kind, nm, ""
    return "", "", "", "That Luma page names no calendar or profile we can track."


def resolve_luma_identifier(raw):
    """Anything an operator can paste -> (identifier, kind, name, error).

    Never guesses: a slug that cannot be resolved comes back with an error
    string rather than being stored as-is, because storing an unresolvable
    handle creates a source that silently returns nothing forever."""
    raw = (raw or "").strip()
    if not raw:
        return "", "", "", "Paste a Luma organiser link or id."

    m = re.search(r"\b(cal-[A-Za-z0-9]+)", raw)
    if m:
        return m.group(1), "calendar", "", ""
    m = re.search(r"\b(usr-[A-Za-z0-9]+)", raw)
    if m:
        return m.group(1), "user", "", ""

    m = re.search(r"(?:lu\.ma|luma\.com)/(?:user|u)/([A-Za-z0-9_.\-]+)", raw, re.IGNORECASE)
    if m:
        handle = m.group(1)
        uid = _luma_user_api_id(handle)
        if uid:
            return uid, "user", "", ""
        return "", "", "", f"No Luma profile found at /user/{handle}."

    m = re.search(r"(?:lu\.ma|luma\.com)/([A-Za-z0-9_.\-]+)", raw, re.IGNORECASE)
    slug = m.group(1) if m else (raw if re.match(r"^[A-Za-z0-9_.\-]+$", raw) else "")
    if not slug:
        return "", "", "", "That is not a Luma link or id."

    # API first. It answers for every vanity calendar and event slug without
    # touching the website, which is what Luma throttles.
    ident, kind, nm, err = _luma_resolve_slug(slug)
    if ident or err:
        return ident, kind, nm, err

    # Not something the resolver knows, so it is either a user handle (no API
    # equivalent exists) or nothing at all. Both need the page.
    #
    # The user lookup is tried BEFORE the page error is surfaced, deliberately.
    # A bare handle like "SuperteamUK" 404s at luma.com/SuperteamUK and
    # resolves fine at luma.com/user/SuperteamUK; returning the first error
    # would refuse a profile that is right there.
    ident, kind, nm, page_err = _luma_entity_from_page(f"https://luma.com/{slug}")
    if ident:
        return ident, kind, nm, ""
    uid = _luma_user_api_id(slug)
    if uid:
        return uid, "user", "", ""
    return "", "", "", (page_err or
                        f"Could not resolve \"{slug}\" to a Luma calendar or profile. "
                        "Check the link opens in a browser.")


def scrape_luma_user(name, username, limit=20):
    """One Luma user profile -> their upcoming London-or-online events.

    This is the path that matters most in practice: plenty of real London
    organisers never create a calendar at all, so the only handle that exists
    for them is their profile URL. Same guardrail and the same canonical event
    URL as the calendar scraper, both via _luma_event_record - which is also
    what makes an event co-hosted by three of these profiles collapse to one
    row instead of three."""
    events = []
    uid = _luma_user_api_id(username)
    if not uid:
        return events
    try:
        r = requests.get(
            "https://api.luma.com/user/profile/events-hosting",
            params={"user_api_id": uid, "period": "future", "pagination_limit": limit},
            headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
            timeout=12,
        )
        if r.status_code != 200:
            log.warning(f"Luma user {name}: HTTP {r.status_code}")
            return events
        for entry in r.json().get("entries", []):
            rec = _luma_event_record(entry.get("event") or entry, name)
            if rec:
                events.append(rec)
    except Exception as e:
        log.warning(f"Luma user {name} failed: {e}")
    return events


LUMA_USERS = {
    # username-or-usr-id: (display name, emoji, category)
    "SuperteamUK": ("Superteam UK", "🟣", "Builder & Tech Community"),
    # Dormant today - 14 events hosted, none upcoming - and kept anyway, under
    # the same rule that revived Corgi London: a quiet organiser is not a dead
    # one, and a source is allowed to return zero. Their past events are all
    # Europe/London (including "Fundraising Fundamentals with the London
    # Founders' Network"), so they clear the timezone filter when they resume.
    # Addressed by usr- id because this profile has no vanity username.
    "usr-netxIsUXILxiHEt": ("Early-Stage Startup Workshops", "🌱", "Business & Networking"),
    # NOT ADDED: gdglondon. The API works (HTTP 200) but the profile has no
    # upcoming events. Left here as a live note rather than a deletion - if it
    # starts scheduling again, uncomment it.
    # "gdglondon": ("GDG London", "🔴", "Builder & Tech Community"),
}

# NOT ADDED as a calendar: cal-1PTimjWlm590x39 (luma.com/product-coach, "Early-
# Stage Startup Workshops"). It is the SAME organiser as the usr- entry above,
# and the profile is the superset - the calendar's ICS carries 10 events while
# the profile has 14, including the London Founders' Network one. Adding both
# would just feed the deduper.

for _username, (_name, _emoji, _cat) in LUMA_USERS.items():
    def _make_user_scraper(u, n, e, cat):
        @source(n, e, cat)
        def _scraper():
            return scrape_luma_user(n, u)
        return _scraper
    _make_user_scraper(_username, _name, _emoji, _cat)


# ─────────────────────────────────────────────
# LUMA ORGANISERS APPROVED FROM THE DASHBOARD
# ─────────────────────────────────────────────
#
# The two loops above register one @source per organiser, at import. That is
# fine for a hardcoded dict and useless for anything approved at runtime: a new
# row could not register a source until the next redeploy, which is exactly why
# source_candidates could collect leads but never act on them.
#
# So instead of one source per organiser, this is one source per CATEGORY, each
# reading the organisers assigned to it at scrape time. New organisers are
# picked up on the very next refresh with no deploy, and the category stays
# correct because app.py takes an event's category from its source.
#
# Identifiers already covered by the hardcoded dicts are skipped rather than
# scraped twice - dedupe in app.py would collapse the duplicate events anyway,
# but fetching the same calendar twice per refresh is pure waste.
_HARDCODED_LUMA_IDS = ({cid for cid, _, _ in LUMA_CALENDARS.values()}
                       | set(LUMA_USERS.keys()))

# LUMA_USERS is keyed by whatever handle was convenient when the entry was
# written - "SuperteamUK" for one, "usr-netxIsUXILxiHEt" for another. The
# dashboard now canonicalises everything an operator pastes to a usr- id, so a
# comparison against the raw keys alone would answer "not tracked yet" for
# Superteam UK and happily start a SECOND feed for a calendar we already
# scrape. Resolving the username keys once, and caching, closes that.
_RESOLVED_LUMA_IDS = None


def tracked_luma_identifiers():
    """Every identifier already covered by the hardcoded dicts, lowercased,
    in both the form it is written in and its canonical usr- form.

    The resolution costs one HTTP call per username-keyed entry, on first use
    only. A lookup that fails is not retried in this process and is not fatal -
    the raw key is still in the set, so the worst case is the pre-existing
    behaviour rather than a crash."""
    global _RESOLVED_LUMA_IDS
    if _RESOLVED_LUMA_IDS is None:
        resolved = {i.lower() for i in _HARDCODED_LUMA_IDS}
        for handle in LUMA_USERS:
            if handle.startswith("usr-"):
                continue
            try:
                uid = _luma_user_api_id(handle)
            except Exception as exc:
                log.warning(f"Could not canonicalise Luma user {handle}: {exc}")
                uid = None
            if uid:
                resolved.add(uid.lower())
        _RESOLVED_LUMA_IDS = resolved
    return _RESOLVED_LUMA_IDS


def _scrape_luma_db(category):
    """Every dashboard-approved Luma organiser feeding `category`.

    One organiser failing (deleted calendar, Luma hiccup) must not cost the
    others in the same category, so each is wrapped individually."""
    try:
        sources = db.get_active_luma_sources(category)
    except Exception as exc:
        log.warning(f"DB Luma sources unavailable: {exc}")
        return []
    out = []
    for src in sources:
        ident = src.get("identifier") or ""
        if ident.lower() in tracked_luma_identifiers():
            continue
        name = src.get("name") or ident
        try:
            if src.get("kind") == "user":
                out.extend(scrape_luma_user(name, ident))
            else:
                out.extend(scrape_luma_calendar(name, ident))
        except Exception as exc:
            log.warning(f"DB Luma source {name} ({ident}) failed: {exc}")
    return out


for _cat in _ALL_CATEGORIES:
    def _make_luma_db_scraper(c):
        @source(f"Luma — {c}", "🟣", c)
        def _scraper():
            return _scrape_luma_db(c)
        return _scraper
    _make_luma_db_scraper(_cat)

# GDG London is now handled by the LUMA_USERS block above - see the
# CORRECTION note there for why the old "no public user-events API"
# retirement was wrong.


# ─────────────────────────────────────────────
# UNICORN MAFIA
# ─────────────────────────────────────────────

@source("Unicorn Mafia", "🦄", "Builder & Tech Community")
def scrape_unicorn_mafia():
    """unicrnmafia.com - a curated London builder-scene calendar.

    The /e page is a client-rendered Next.js app whose HTML contains the word
    "EVENTS" and nothing else, so the usual fetch()+soup approach returns
    zero. The page's own data call is a clean JSON endpoint, which is what is
    used here. Every entry links out to Luma, and the feed is already
    London-scoped, so it does in one request what a dozen hand-added host
    calendars were assembled to approximate."""
    events = []
    data = fetch("https://www.unicrnmafia.com/api/calendar", json_mode=True)
    if not data:
        return events
    for e in data.get("events", []):
        title = (e.get("summary") or "").strip()
        iso   = (e.get("start", {}).get("dateTime")
                 or e.get("start", {}).get("date") or "")[:10]
        if not title or not _is_future(iso):
            continue
        url = e.get("externalUrl") or e.get("htmlLink") or "https://www.unicrnmafia.com/e"
        start = e.get("start", {}).get("dateTime", "")
        events.append({
            "title": title,
            "date": iso,
            "time": start[11:16] if len(start) >= 16 else None,
            "url": url,
            "source": "Unicorn Mafia",
            "location": e.get("location") or "London",
        })
    return events


# ─────────────────────────────────────────────
# HACKATHONS
# ─────────────────────────────────────────────
#
# Two audiences here, and they need different geography rules:
#   in-person - must be in London, so these sources sit in _GLOBAL_SOURCES
#               and have to NAME London rather than merely not name a
#               different city (hackathons.org.uk offers Bradford, and
#               Bradford is not on any denylist).
#   online    - no city to be wrong about, so location="Online" passes
#               is_london() by way of is_online().
#
# The same hackathon shows up on Devpost, Eventbrite, Hackathon Atlas and a
# Luma calendar simultaneously. Cross-source dedupe in app.py is what stops
# the catalog listing it four times.

_MONTH_DAY_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2})")


def _iso_from_month_day(mon_word, day, year=None):
    """ISO date from a month word + day, inferring the year when absent.

    Listing pages routinely print "Aug 16" with no year. A bare month that has
    already passed is read as next year rather than as a date months in the
    past, which _is_future() would then silently drop.

    The inference compares actual DATES, not month numbers. An earlier version
    tested `(today.month - month) > 1`, which left a dead zone one to two
    months wide: on 15 February a listing showing "Jan 5" resolved to January
    of the current year, 41 days in the past, and the event was dropped - when
    an upcoming-events page showing "Jan 5" in mid-February plainly means next
    January. A day count has no such seam and wraps across December correctly."""
    month = _MONTH_LOOKUP.get((mon_word or "").lower()[:3])
    if not month:
        return None
    try:
        day = int(day)
    except (TypeError, ValueError):
        return None
    if year is not None:
        try:
            return f"{int(year):04d}-{month:02d}-{day:02d}"
        except (TypeError, ValueError):
            return None

    today = datetime.now(timezone.utc).date()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None                      # e.g. "Feb 30", or Feb 29 in a common year
    # A little slack before rolling forward: an event that finished a fortnight
    # ago is a stale listing, not next year's edition. Beyond the window, the
    # only sensible reading of a bare month/day on an upcoming page is the
    # next one.
    #
    # 60 days, matching ai_engine._infer_year() and _inferYear() in
    # templates/index.html. This was 31, and the disagreement was real: a
    # listing printed "Jul08" was read here as NEXT July (so it counted as
    # upcoming and kept its dedupe key in the future) while the frontend and
    # the scoring engine both read it as this July and correctly treated it as
    # past. Same string, three parsers, two answers. Rolling forward later is
    # also the more honest default - it leaves a stale listing looking stale
    # instead of resurrecting it as next year's edition.
    if (today - candidate).days > 60:
        try:
            candidate = candidate.replace(year=today.year + 1)
        except ValueError:               # 29 Feb rolling into a common year
            return None
    return candidate.isoformat()


def _devpost_dates(period):
    """(start, end) out of a Devpost range string.

    Four shapes occur in the wild and all four appear on page one of a London
    search:
        "Jul 31 - Oct 01, 2026"   cross-month
        "Aug 05 - 16, 2026"       same month, end day only
        "Jul 30, 2026"            single day
        "Oct 03 - Nov 30, 2025"   cross-month, past year
    The year is printed once, at the end, and applies to both halves."""
    if not period:
        return None, None
    ym = re.search(r"(\d{4})\s*$", period)
    year = int(ym.group(1)) if ym else None
    pairs = _MONTH_DAY_RE.findall(period)
    if not pairs:
        return None, None
    start = _iso_from_month_day(pairs[0][0], pairs[0][1], year)
    if len(pairs) > 1:
        return start, _iso_from_month_day(pairs[1][0], pairs[1][1], year)
    # same-month range: the closing day stands alone after the dash
    m = re.search(r"-\s*(\d{1,2})\s*,", period)
    if m:
        return start, _iso_from_month_day(pairs[0][0], m.group(1), year)
    return start, start


def _window_date(start_iso, end_iso):
    """The date worth showing for an event with a submission window.

    Online hackathons routinely open weeks before you find them - "Jun 04 -
    Aug 14" is live today and listing it under June puts it in the catalog's
    past. Once the start has gone by, the deadline is the date the reader can
    still act on."""
    if start_iso and _is_future(start_iso):
        return start_iso
    return end_iso or start_iso


_DEVPOST_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json",
    # Devpost's API answers 403 to a plain request; it wants to look like the
    # site's own XHR.
    "X-Requested-With": "XMLHttpRequest",
}


def _scrape_devpost(source_name, params, limit=20):
    events = []
    try:
        r = requests.get("https://devpost.com/api/hackathons",
                         params=params, headers=_DEVPOST_HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning(f"Devpost {source_name}: HTTP {r.status_code}")
            return events
        for h in r.json().get("hackathons", []):
            # "ended" entries dominate a relevance-sorted search - the London
            # query returns hackathons back to 2014 - so open state is checked
            # rather than trusted from the query params.
            if h.get("open_state") not in ("open", "upcoming"):
                continue
            title = (h.get("title") or "").strip()
            start, end = _devpost_dates(h.get("submission_period_dates"))
            iso   = _window_date(start, end)
            loc   = (h.get("displayed_location") or {}).get("location") or ""
            if not is_valid_event(title):
                continue
            # a submission window that opened last month is still live, so the
            # deadline rather than the start decides whether it is worth
            # showing; keep anything whose window has not closed.
            if not _is_future(iso):
                continue
            events.append({
                "title": title,
                "date": iso,
                "url": h.get("url") or "https://devpost.com/hackathons",
                "source": source_name,
                "location": loc,
                "is_online": loc.strip().lower() in ("online", "virtual"),
            })
    except Exception as e:
        log.warning(f"Devpost {source_name} failed: {e}")
    return events[:limit]


@source("Devpost London", "🏁", "Hackathons")
def scrape_devpost_london():
    """In-person London hackathons on Devpost.

    Note for whoever reads the summary line: this legitimately returns very
    few - Devpost's entire London corpus is 246 hackathons of which only a
    handful are ever open at once, and today none of the open ones are
    in-person London. Empty here is a real signal, not a broken selector."""
    return _scrape_devpost("Devpost London",
                           {"search": "London", "status[]": ["open", "upcoming"],
                            "order_by": "deadline"})


@source("Devpost Online", "🌍", "Hackathons")
def scrape_devpost_online():
    """Open online hackathons - no city, so reachable from London.

    Capped well below the ~80 open at any time: this is a London catalog with
    an online section, not a Devpost mirror."""
    return _scrape_devpost("Devpost Online",
                           {"challenge_type[]": "online",
                            "status[]": ["open", "upcoming"],
                            "order_by": "deadline"}, limit=12)


@source("Hackathons UK", "🎓", "Hackathons")
def scrape_hackathons_uk():
    """hackathons.org.uk - UK student-run hackathons, listed by the charity.

    UK-wide rather than London-scoped, hence its place in _GLOBAL_SOURCES:
    its current upcoming list is Bradford, Nottingham and Manchester, and
    without the strict rule Bradford would have been waved through as London
    purely for not appearing on a denylist."""
    events = []
    soup = fetch("https://www.hackathons.org.uk/events/")
    if not soup:
        return events
    # Past events live under their own heading on the same page; everything
    # before it is upcoming. _is_future() is the real guard, this just avoids
    # walking hundreds of dead entries.
    for a in soup.select("a[href]"):
        block = a.find_parent(["article", "li", "div"]) or a
        text  = block.get_text(" ", strip=True)
        # multi-day events print both ends ("24 Oct 2026 - 25 Oct 2026"); the
        # first is the start, and the venue follows the last.
        dates = list(_LOOSE_DATE_RE.finditer(text))
        if not dates:
            continue
        m   = dates[-1]
        iso = _iso_from_match(dates[0])
        if not _is_future(iso):
            continue
        h = block.select_one("h2, h3, h4")
        title = h.get_text(strip=True) if h else None
        if not is_valid_event(title):
            continue
        url = fix_url(a.get("href", ""), "https://www.hackathons.org.uk")
        # cards read "Physical <title> <date> <venue> Learn More" - the venue
        # is what sits between the date and the call to action.
        venue = text[m.end():].replace("Learn More", "").strip(" -–—·|")
        events.append({"title": title, "date": iso, "url": url,
                       "source": "Hackathons UK",
                       "location": venue or text,
                       "is_online": bool(re.search(r"\bvirtual\b", text, re.I))})
    # the same event is reachable through several links in one card
    seen, out = set(), []
    for ev in events:
        k = (norm_title(ev["title"]), ev["date"])
        if k in seen:
            continue
        seen.add(k)
        out.append(ev)
    return out[:15]


# Links that appear on EVERY Hackathon Atlas page - the site's own chrome and
# its author's socials. They are the reason "first external link" is not a
# usable rule for finding an event's real home.
_ATLAS_CHROME_HOSTS = ("hackathonatlas.com", "github.com", "linkedin.com",
                       "x.com", "twitter.com", "boujaddi.com")

# atlas detail url -> resolved source url. Detail pages don't change their
# outbound link, so this is resolved once per event for the life of the
# process instead of on every 30-minute scrape.
_atlas_source_cache = {}


def _atlas_source_url(detail_url, timeout=15):
    """The event's OWN page behind a Hackathon Atlas listing, or None.

    Atlas is an aggregator: hackathonatlas.com/hackathons/<uuid> is its index
    card, not the event. Linking there sends a reader to a directory entry and
    makes them find the real thing themselves, and it hides the event's actual
    host from source discovery - an Atlas URL tells harvest_luma_hosts()
    nothing, even when the event underneath it is a Luma event we could have
    learned a whole calendar from.

    The detail page labels the outbound link "Register" and "Official page",
    both pointing at the same place; that label is the signal, with a
    chrome-host exclusion as the fallback."""
    if detail_url in _atlas_source_cache:
        return _atlas_source_cache[detail_url]
    result = None
    try:
        soup = fetch(detail_url, timeout=timeout)
        if soup:
            externals = []
            for a in soup.select('a[href^="http"]'):
                href = a.get("href", "")
                if any(host in href for host in _ATLAS_CHROME_HOSTS):
                    continue
                externals.append((a.get_text(" ", strip=True), href))
            for text, href in externals:
                if re.search(r"register|official page", text or "", re.IGNORECASE):
                    result = href
                    break
            if not result and externals:
                result = externals[0][1]
    except Exception as e:
        log.debug(f"Atlas source lookup failed for {detail_url}: {e}")
    # Only successes are remembered. Caching a None would let one timeout pin
    # that event to the aggregator link for the life of the process, and the
    # per-scrape budget already bounds the cost of trying again.
    if result:
        _atlas_source_cache[detail_url] = result
    return result


@source("Hackathon Atlas", "🗺️", "Hackathons")
def scrape_hackathon_atlas():
    """hackathonatlas.com - global hackathon directory, ~500 upcoming.

    Server-rendered, so the cards are in the HTML, but the city filter is
    client-side only - there is no ?city=London URL to request. The whole
    first page is fetched and filtered here instead. Cards print "Aug 16 ·
    London" with no year; _iso_from_month_day infers it.

    Atlas is an AGGREGATOR, so each surviving event is resolved through to the
    page the organiser actually runs - lu.ma, devpost, the event's own site -
    and only falls back to the Atlas card if that lookup fails. See
    _atlas_source_url() for why the aggregator URL is the wrong thing to keep.
    Resolution costs one request per event, capped and cached, and only
    happens for events that already passed the date and London/online
    filters - never for the ~480 we are about to discard."""
    events = []
    soup = fetch("https://hackathonatlas.com")
    if not soup:
        return events
    for a in soup.select('a[href^="/hackathons/"]'):
        spans = [s.get_text(strip=True) for s in a.select("span")]
        spans = [s for s in spans if s]
        if len(spans) < 2:
            continue
        # last span is the "Aug 16 · London" meta line, the one before it the title
        meta  = spans[-1]
        title = spans[-2]
        if "·" not in meta:
            continue
        date_part, _, place = meta.partition("·")
        place = place.strip()
        m = _MONTH_DAY_RE.search(date_part)
        if not m:
            continue
        iso = _iso_from_month_day(m.group(1), m.group(2))
        if not _is_future(iso) or not is_valid_event(title):
            continue
        online = place.lower() in ("online", "virtual", "remote")
        if not online and not re.search(r"\blondon\b", place, re.IGNORECASE):
            continue
        events.append({"title": title, "date": iso,
                       "url": fix_url(a.get("href", ""), "https://hackathonatlas.com"),
                       "source": "Hackathon Atlas",
                       "location": "Online" if online else place,
                       "is_online": online})

    events = events[:25]
    # Resolve the aggregator cards to the organisers' own pages. Budgeted so a
    # slow run can't stall the whole scrape; anything unresolved keeps the
    # Atlas link, which is a worse link but still a working one.
    budget = 25
    for ev in events:
        if budget <= 0:
            break
        if "hackathonatlas.com/hackathons/" not in ev["url"]:
            continue
        if ev["url"] not in _atlas_source_cache:
            budget -= 1
            time.sleep(0.15)
        real = _atlas_source_url(ev["url"])
        if real:
            ev["url"] = real
    return events


@source("MLH", "🏆", "Hackathons")
def scrape_mlh():
    """Major League Hacking - the global student hackathon league.

    The season page is an Inertia app: the whole event list is already in the
    page as JSON inside <script data-page="app">, so there is no HTML card
    parsing and no second request. formatType is authoritative for online vs
    in-person, which is better than guessing from a location string that says
    "Everywhere, Worldwide".

    Global, so it sits in _GLOBAL_SOURCES and has to name London. Today it
    carries no UK events at all - MLH's London dates are seasonal, and this
    returning only its digital events is correct, not broken."""
    events = []
    season = datetime.now(timezone.utc).year + 1        # MLH seasons run ahead
    soup = fetch(f"https://mlh.io/seasons/{season}/events")
    if not soup:
        return events
    tag = soup.find("script", attrs={"data-page": "app"})
    if not tag:
        log.warning("MLH: no data-page blob")
        return events
    try:
        data = json.loads(tag.string or tag.get_text() or "")
    except (json.JSONDecodeError, TypeError) as e:
        log.warning(f"MLH: unparseable blob: {e}")
        return events
    for e in data.get("props", {}).get("upcomingEvents", []):
        title = (e.get("name") or "").strip()
        iso   = (e.get("startsAt") or "")[:10]
        if not title or not _is_future(iso):
            continue
        online = e.get("formatType") != "physical"
        url = e.get("url") or ""
        events.append({
            "title": title,
            "date": iso,
            "url": fix_url(url, "https://mlh.io"),
            "source": "MLH",
            "location": "Online" if online else (e.get("location") or ""),
            "is_online": online,
        })
    return events[:20]


# Eventbrite's /hackathon/ search is a fuzzy keyword match, not a category:
# it returns anything it thinks is adjacent. In the live catalog that meant
# "Networking Hacks for Introverts", "Black Girls Hike: Cockfosters to Enfield
# Lock", "PULL UP! - THE JUNGLE DANCE LAB" and a run of cybersecurity
# breakfast-networking listings, all filed under Hackathons - which is a
# top-level tab, so the noise is the first thing anyone browsing it sees.
#
# HACKATHON_RE alone is too tight here: it wants the literal word, and misses
# the jam/datathon family that genuinely belongs. This adds those, and nothing
# looser - a title that says none of these is not a hackathon.
# "хакатон" is here because London's Ukrainian tech community advertises in
# Ukrainian on Eventbrite ("Шостий Жіночий Хакатон від EduHub" - a real
# women's hackathon). Worth noting this only became matchable once fetch()
# stopped mangling UTF-8: before that the title arrived as "Ð¥Ð°ÐºÐ°ÑÐ¾Ð½"
# and no rule of any kind could have recognised it.
_HACKATHON_ADJACENT_RE = re.compile(
    r"(\b(hack[\s-]?athon|game\s?jam|code\s?jam|hack\s?jam|datathon|codeathon|makeathon"
    r"|build[\s-]?athon|ctf|capture\s+the\s+flag)\b|хакатон)", re.IGNORECASE)


def _looks_like_hackathon(title: str) -> bool:
    t = title or ""
    return bool(HACKATHON_RE.search(t) or _HACKATHON_ADJACENT_RE.search(t))


@source("Eventbrite Hackathons London", "🎫", "Hackathons")
def scrape_eventbrite_hackathons():
    """Eventbrite's London hackathon search - already London-scoped by URL,
    so it does not need the strict rule the global directories do.

    It DOES need a title check: the search is fuzzy and everything it returns
    is filed under Hackathons by this source's category, so an unfiltered feed
    fills the Hackathons tab with dance nights and hikes. See the note above."""
    return [e for e in _scrape_eventbrite("hackathon", "Eventbrite Hackathons London")
            if _looks_like_hackathon(e.get("title", ""))]


@source("DoraHacks Virtual", "⚡", "Hackathons")
def scrape_dorahacks_virtual():
    """dorahacks.io virtual hackathons - online, so no city to filter on.

    The API answers 405 without a Referer header; with one it returns clean
    JSON with unix timestamps. 572 are live at any time, so this is capped."""
    events = []
    try:
        r = requests.get(
            "https://dorahacks.io/api/v1/hub/hackathons",
            params={"page": 1, "page_size": 30, "venue_form": "Virtual"},
            headers={"User-Agent": HEADERS["User-Agent"],
                     "Accept": "application/json, text/plain, */*",
                     "Referer": "https://dorahacks.io/hackathon"},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"DoraHacks: HTTP {r.status_code}")
            return events
        for h in r.json().get("results", []):
            title = (h.get("title") or "").strip()
            ts    = h.get("timeline_start")
            end   = h.get("timeline_end")
            if not title or not ts:
                continue
            start_iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            # a hackathon that opened weeks ago but is still accepting
            # submissions is live and worth listing, so the END of the window
            # decides, not the start.
            end_iso = (datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d")
                       if end else start_iso)
            if not _is_future(end_iso):
                continue
            iso = _window_date(start_iso, end_iso)
            uname = h.get("uname") or ""
            events.append({
                "title": title,
                "date": iso,
                "url": f"https://dorahacks.io/hackathon/{uname}/detail" if uname
                       else "https://dorahacks.io/hackathon",
                "source": "DoraHacks Virtual",
                "location": "Online",
                "is_online": True,
            })
    except Exception as e:
        log.warning(f"DoraHacks failed: {e}")
    return events[:15]


# ─────────────────────────────────────────────
# SOURCE DISCOVERY
# ─────────────────────────────────────────────
#
# Every source in this file arrived the same way: somebody noticed an event
# that wasn't in the catalog and sent a link. That makes coverage a function
# of how much event-hunting the operator happens to do, and it means a whole
# organiser can go missing indefinitely without anything looking wrong - the
# run summary counts what we found, never what we didn't.
#
# The evidence to fix that is already in hand. Aggregator calendars like The
# Hack Collective and Unicorn Mafia carry OTHER people's events, and every one
# of those events names the calendar that hosts it. So each scrape already
# tells us about organisers we don't track; we just used to throw that away.
#
# This resolves ingested Luma events back to their host calendar and reports
# the ones missing from LUMA_CALENDARS / LUMA_USERS. It proposes, it does not
# auto-add: a candidate is a lead for a human to accept or reject, because
# "hosted a London event once" is not the same as "worth a permanent source".

_LUMA_SLUG_RE = re.compile(r"^https?://(?:lu\.ma|luma\.com)/([A-Za-z0-9\-_]+)/?$", re.IGNORECASE)

# Calendar names that are never worth proposing. Luma gives every personal
# account a calendar called "Personal" with no slug; those are individuals
# posting one event, not organisers with a schedule to follow.
_SKIP_CALENDAR_NAMES = {"personal"}


def luma_slug(url):
    """The vanity slug out of a Luma event URL, or None if it isn't one.

    Both hosts appear in the catalog - lu.ma from the older calendar API and
    luma.com from the newer user API - and they are the same site."""
    m = _LUMA_SLUG_RE.match((url or "").strip())
    if not m:
        return None
    slug = m.group(1)
    # /user/<name> and /event/<id> are not event slugs
    if slug.lower() in ("user", "event", "discover", "signin"):
        return None
    return slug


# slug -> {"host": {...}|None, "title": str|None}. One lookup answers two
# different questions - who hosts this, and what does the organiser call it -
# so both callers share a cache instead of each paying for the same request.
_luma_event_cache = {}


def resolve_luma_event(slug, timeout=10):
    """Host calendar and canonical title for one Luma event.

    Uses api.luma.com/url, which answers {"kind":"event","data":{...}}. The
    two fields live at DIFFERENT depths, which has now cost this project three
    separate mistakes:
        data.calendar        - the host calendar
        data.event.name      - the organiser's own title
    Reading the title off data.name (where the calendar sits) returns None
    silently, which reads exactly like "the titles agree" and hid every
    mismatch behind a clean-looking audit."""
    if slug in _luma_event_cache:
        return _luma_event_cache[slug]
    out = {"host": None, "title": None}
    try:
        r = requests.get("https://api.luma.com/url", params={"url": slug},
                         headers={"User-Agent": HEADERS["User-Agent"],
                                  "Accept": "application/json"},
                         timeout=timeout)
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            cal  = data.get("calendar") or {}
            if cal.get("api_id"):
                out["host"] = {
                    "identifier": cal["api_id"],
                    "name": (cal.get("name") or "").strip(),
                    "slug": cal.get("slug"),
                    "city": (cal.get("geo_city") or "").strip(),
                    "timezone": (cal.get("timezone") or "").strip(),
                }
            out["title"] = ((data.get("event") or {}).get("name") or "").strip() or None
    except Exception as e:
        log.debug(f"Luma event lookup failed for {slug}: {e}")
    # successes only - a cached failure would hide this event for the life of
    # the process, and the callers' budgets already bound retrying
    if out["host"] or out["title"]:
        _luma_event_cache[slug] = out
    return out


def resolve_luma_host(slug, timeout=10):
    """The calendar hosting one Luma event, as a plain dict, or None."""
    return resolve_luma_event(slug, timeout=timeout)["host"]


def add_source_titles(events, max_lookups=150):
    """Attach the organiser's own title to aggregator-sourced events.

    Aggregators rewrite titles. Hackathon Atlas lists the Softr hackathon by
    its subtitle, "From Spreadsheet Chaos to One Source of Truth", while the
    organiser calls it "AI Hackathon for EventProfs & Event Operators in
    London with Softr". Same event, two names.

    The stored title is left ALONE - it is what the catalog shows, and the
    aggregator's phrasing is not wrong. The organiser's title is added to
    `aliases` instead, so both names find the event in search and both are
    available to cross-source dedupe. Nothing is renamed and nothing is lost.

    Budgeted and cached. The budget is set high enough to cover the whole
    Luma-linked catalog in ONE pass rather than converging over several: a
    lower ceiling meant the aliases attached in some later scrape and a search
    could miss an event in the meantime, which is the exact failure this
    exists to prevent. The cost is paid once - resolved slugs are cached for
    the life of the process and shared with harvest_luma_hosts(), so a steady
    state costs only the handful of events that are genuinely new."""
    budget = max_lookups
    tagged = 0
    for ev in events:
        slug = luma_slug(ev.get("url"))
        if not slug:
            continue
        if slug not in _luma_event_cache:
            if budget <= 0:
                continue
            budget -= 1
            resolve_luma_event(slug)
            time.sleep(0.1)
        canonical = (_luma_event_cache.get(slug) or {}).get("title")
        if not canonical or norm_title(canonical) == norm_title(ev.get("title", "")):
            continue
        aliases = ev.setdefault("aliases", [])
        if canonical not in aliases:
            aliases.append(canonical)
            tagged += 1
    if tagged:
        log.info(f"Title aliases: {tagged} events also known by another name")
    return events


def known_luma_identifiers():
    """Everything we already scrape, lowercased, so harvesting reports only gaps.

    This used to answer with the hardcoded LUMA_CALENDARS ids alone. Organisers
    approved from the dashboard were invisible to it, so every scrape
    rediscovered them and re-proposed them as untracked leads - the candidates
    list kept re-offering organisers that were already feeding the catalog.
    Delegating to tracked_luma_identifiers() covers the hardcoded calendars,
    the hardcoded profiles, and the database-approved rows in one answer."""
    try:
        known = set(tracked_luma_identifiers())
    except Exception as exc:
        log.warning(f"Could not resolve tracked Luma ids: {exc}")
        known = {i.lower() for i in _HARDCODED_LUMA_IDS}
    try:
        known |= db.get_luma_source_identifiers()
    except Exception as exc:
        log.warning(f"Could not read approved Luma sources: {exc}")
    return known


def known_luma_slugs():
    """Vanity slugs we already scrape, as a second way to recognise our own.

    LUMA_USERS is keyed by username, not calendar id, so a profile we already
    track has no entry in known_luma_identifiers() - and its own events would
    otherwise come back as a "missing" source, sending whoever reviews the
    candidate list off to add something already there."""
    return {u.lower() for u in LUMA_USERS}


def harvest_luma_hosts(events, known_ids=None, max_lookups=40):
    """Untracked host calendars behind the events we just ingested.

    Returns a list of candidate dicts, busiest first. `max_lookups` bounds the
    extra HTTP calls one scrape may make: unresolved slugs are worked through
    a batch at a time, so a catalog with hundreds of Luma events converges
    over several scrapes instead of hammering the API on one.

    Shares _luma_event_cache with add_source_titles(), so whichever of the two
    runs first pays for the lookup and the other gets it free - the same
    request carries both the host and the canonical title."""
    known = {str(i).lower() for i in
             (known_ids if known_ids is not None else known_luma_identifiers())}
    known_slugs = known_luma_slugs()
    candidates, budget = {}, max_lookups

    for ev in events:
        slug = luma_slug(ev.get("url"))
        if not slug:
            continue
        if slug not in _luma_event_cache:
            if budget <= 0:
                continue                      # leave it for the next scrape
            budget -= 1
            resolve_luma_event(slug)
            time.sleep(0.15)                  # be a polite client
        host = (_luma_event_cache.get(slug) or {}).get("host")
        if not host or (host["identifier"] or "").lower() in known:
            continue
        if (host["slug"] or "").lower() in known_slugs:
            continue
        if (host["name"] or "").strip().lower() in _SKIP_CALENDAR_NAMES or not host["slug"]:
            continue

        c = candidates.setdefault(host["identifier"], {
            **host, "kind": "luma_calendar",
            "url": f"https://luma.com/{host['slug']}",
            "event_count": 0, "sample_title": ev.get("title", ""),
        })
        c["event_count"] += 1

    for c in candidates.values():
        c.setdefault("discovered_via", "catalog_host")
    ranked = sorted(candidates.values(), key=lambda c: -c["event_count"])
    if ranked:
        log.info(f"Source discovery: {len(ranked)} untracked Luma calendars behind "
                 f"ingested events (top: {ranked[0]['name']} x{ranked[0]['event_count']})")
    return ranked


# ── Wider discovery ──────────────────────────────────────────────────────
#
# harvest_luma_hosts() can only see organisers our EXISTING sources already
# surface, which makes it good at filling in a neighbourhood and useless at
# finding a new one. Two blind spots followed, and both had to be fixed by a
# human sending a link:
#
#   events we never ingest  - anything our sources don't carry, or that the
#                             category filters drop, has no host to harvest.
#   dormant organisers      - a calendar with nothing scheduled publishes
#                             nothing, so there is no event to walk back from.
#
# The city feed answers the first: it is every London event Luma knows about,
# not just ours, and each entry embeds its calendar - a page of ~46 events
# costs one request instead of 46.
#
# Host history answers the second. People outlive their calendars: the host of
# an event visible today has a profile listing everything they have ever run,
# and `period=past` on it reaches calendars that have been quiet for months.
# This is exactly how "Early-Stage Startup Workshops" is reachable - dormant
# since August 2025, invisible to every other route.

_LUMA_LONDON_GEO = {"geo_latitude": "51.5074", "geo_longitude": "-0.1278",
                    "geo_type": "circle"}


def _is_relevant_calendar(cal_name, event_titles):
    """Whether a lead is worth a human's attention.

    London's Luma is not a tech directory - one page of the city feed offered
    Panthers Basketball, a Brazilian dance school and a sports-club booking
    page. Proposing all of them would bury the real finds, so a lead has to
    look like tech/builder territory in either its calendar name or something
    it has actually published."""
    if HACKATHON_RE.search(cal_name or "") or _BUILDER_TECH_TITLE_RE.search(cal_name or ""):
        return True
    return any(HACKATHON_RE.search(t or "") or _BUILDER_TECH_TITLE_RE.search(t or "")
               for t in event_titles)


def _luma_city_feed(pages=3):
    """Entries from Luma's London discover feed, following the cursor."""
    entries, cursor = [], None
    for _ in range(max(1, pages)):
        params = dict(_LUMA_LONDON_GEO)
        if cursor:
            params["pagination_cursor"] = cursor
        try:
            r = requests.get("https://api.lu.ma/discover/get-paginated-events",
                             params=params,
                             headers={"User-Agent": HEADERS["User-Agent"],
                                      "Accept": "application/json"},
                             timeout=15)
            if r.status_code != 200:
                log.warning(f"Luma city feed: HTTP {r.status_code}")
                break
            data = r.json()
        except Exception as e:
            log.warning(f"Luma city feed failed: {e}")
            break
        batch = data.get("entries") or []
        entries.extend(batch)
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
        time.sleep(0.2)
    return entries


def _host_past_calendars(user_api_id, limit=15):
    """Calendars a host has run in the PAST - dormant ones included."""
    try:
        r = requests.get("https://api.luma.com/user/profile/events-hosting",
                         params={"user_api_id": user_api_id, "period": "past",
                                 "pagination_limit": limit},
                         headers={"User-Agent": HEADERS["User-Agent"],
                                  "Accept": "application/json"},
                         timeout=12)
        if r.status_code != 200:
            return []
        out = []
        for entry in r.json().get("entries", []):
            cal = entry.get("calendar") or {}
            if cal.get("api_id"):
                out.append((cal, (entry.get("event") or {}).get("name") or ""))
        return out
    except Exception as e:
        log.debug(f"Host history lookup failed for {user_api_id}: {e}")
        return []


def _as_candidate(cal, sample_title, via):
    return {
        "identifier": cal.get("api_id"),
        "kind": "luma_calendar",
        "name": (cal.get("name") or "").strip(),
        "slug": cal.get("slug"),
        "url": f"https://luma.com/{cal.get('slug')}" if cal.get("slug") else None,
        "city": (cal.get("geo_city") or "").strip(),
        "timezone": (cal.get("timezone") or "").strip(),
        "event_count": 1,
        "sample_title": sample_title,
        "discovered_via": via,
    }


def discover_luma_sources(known_ids=None, pages=3, host_lookups=8):
    """Untracked London calendars from the city feed and from host history.

    Returns candidates in the same shape as harvest_luma_hosts(). Both budgets
    are deliberately small: this runs on every scrape, and the point is to
    widen coverage steadily rather than to crawl Luma."""
    known = {str(i).lower() for i in
             (known_ids if known_ids is not None else known_luma_identifiers())}
    known_slugs = known_luma_slugs()
    found, titles_by_cal, hosts = {}, {}, {}

    def consider(cal, title, via):
        cid = cal.get("api_id")
        if not cid or cid.lower() in known:
            return
        if (cal.get("slug") or "").lower() in known_slugs or not cal.get("slug"):
            return                                  # personal calendars have no slug
        if (cal.get("name") or "").strip().lower() in _SKIP_CALENDAR_NAMES:
            return
        # Host history follows PEOPLE, and people run calendars in more than
        # one city - one London host led to Manila, Berlin and Amsterdam
        # chapters of the same running club. Drop a calendar that names a city
        # which isn't London; keep the ones that name none, on the same
        # conservative principle as is_london().
        city = (cal.get("geo_city") or "").strip()
        if city and not re.search(r"\blondon\b", city, re.IGNORECASE) \
                and _NON_LONDON_CITIES.search(city):
            return
        titles_by_cal.setdefault(cid, []).append(title)
        if cid in found:
            found[cid]["event_count"] += 1
        else:
            found[cid] = _as_candidate(cal, title, via)

    # 1. the city feed - wide, and calendars come embedded
    for entry in _luma_city_feed(pages=pages):
        title = (entry.get("event") or {}).get("name") or ""
        consider(entry.get("calendar") or {}, title, "city_feed")
        for h in (entry.get("hosts") or []):
            if h.get("api_id"):
                hosts.setdefault(h["api_id"], title)

    # 2. host history - the only route to an organiser with nothing scheduled.
    # Hosts of relevant-looking events are tried first, so a small budget is
    # spent where a real lead is likeliest.
    ranked_hosts = sorted(hosts.items(),
                          key=lambda kv: not _is_relevant_calendar("", [kv[1]]))
    for user_id, _seen_title in ranked_hosts[:max(0, host_lookups)]:
        for cal, title in _host_past_calendars(user_id):
            consider(cal, title, "host_history")
        time.sleep(0.15)

    relevant = [c for c in found.values()
                if _is_relevant_calendar(c["name"], titles_by_cal.get(c["identifier"], []))]
    relevant.sort(key=lambda c: -c["event_count"])
    if relevant:
        by_route = {}
        for c in relevant:
            by_route[c["discovered_via"]] = by_route.get(c["discovered_via"], 0) + 1
        log.info(f"Wider discovery: {len(relevant)} untracked London calendars "
                 f"({dict(by_route)}), {len(found) - len(relevant)} filtered as off-topic")
    return relevant


# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run(dry_run=False):
    seen = load_seen()
    all_new, results_summary = [], []

    for src in SOURCES:
        name, emoji, category = src["name"], src["emoji"], src["category"]
        log.info(f"Scraping {name}...")
        try:
            events = src["fn"]()
        except Exception as e:
            log.error(f"{name} scraper crashed: {e}")
            events = []

        new_events = []
        for ev in events:
            eid = event_id(ev["title"], ev["url"])
            if eid not in seen:
                seen[eid] = {"title": ev["title"], "source": name,
                             "seen_at": datetime.now(timezone.utc).isoformat()}
                new_events.append(ev)
                all_new.append({**ev, "emoji": emoji, "category": category})

        results_summary.append({"source": name, "emoji": emoji, "category": category,
                                 "total": len(events), "new": len(new_events)})
        log.info(f"  {name}: {len(events)} events, {len(new_events)} new")
        time.sleep(0.5)

    if all_new and not dry_run:
        by_cat = {}
        for ev in all_new:
            by_cat.setdefault(ev["category"],[]).append(ev)
        for cat, evs in by_cat.items():
            lines = [f"<b>📡 New Events — {cat}</b>\n"]
            for ev in evs[:10]:
                date_str = f" · {ev['date']}" if ev.get("date") else ""
                lines.append(f"{ev['emoji']} <a href=\"{ev['url']}\">{ev['title']}</a>{date_str}\n   <i>{ev['source']}</i>")
            send_telegram("\n".join(lines))
            time.sleep(0.3)

    save_seen(seen)
    log.info(f"Done. {len(all_new)} new events across {len(SOURCES)} sources.")
    return results_summary, all_new

if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    summary, new_events = run(dry_run=dry)
    print(f"\n{'='*50}\nSUMMARY\n{'='*50}")
    for s in summary:
        print(f"{s['emoji']} {s['source']}: {s['total']} total, {s['new']} new")
    print(f"\nTotal new events: {sum(s['new'] for s in summary)}")
