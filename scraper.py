#!/usr/bin/env python3
"""
ORBITCAST — Event Scraper
Scrapes 20+ sources across Tech, Defence, Intelligence, Business, Education & Hackathons.
"""

import os, json, re, time, hashlib, logging, requests, feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

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

    if re.search(r"\blondon\b", hay, re.IGNORECASE):
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
        return r.json() if json_mode else BeautifulSoup(r.text, "lxml")
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
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "RUSI"})
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

@source("London Defence Conference", "🎖️", "Defence & Geopolitics")
def scrape_ldc():
    events = []
    soup = fetch("https://londondefenceconference.com/")
    if not soup:
        return events
    for el in soup.select("article, [class*=card], section h2, section h3"):
        h     = el if el.name in ["h2","h3"] else el.select_one("h2, h3, h4")
        a     = el.select_one("a[href]") if el.name not in ["h2","h3"] else el.find_parent("a")
        title = el.get_text(strip=True) if el.name in ["h2","h3"] else (h.get_text(strip=True) if h else None)
        url   = fix_url(a["href"] if a else "", "https://londondefenceconference.com")
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": None, "url": url, "source": "London Defence Conference"})
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

def _scrape_eventbrite(category_slug, source_name):
    import re
    events = []
    soup = fetch(f"https://www.eventbrite.co.uk/d/united-kingdom--london/{category_slug}/")
    if not soup:
        return events
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
        if is_valid_event(title) and title not in seen_titles:
            seen_titles.add(title)
            events.append({"title": title, "date": date, "url": href, "source": source_name})
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
                if is_valid_event(title) and event_url and is_valid_url(event_url):
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
            slug  = ev.get("url") or ev.get("api_id","")
            event_url = f"https://lu.ma/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at","")
            date  = start[:10] if start else None
            seen.add(title)
            events.append({"title": title, "date": date, "url": event_url or "https://lu.ma", "source": "Luma London Discover"})
    except Exception as e:
        log.warning(f"Luma Discover failed: {e}")
    return events[:30]

# ─────────────────────────────────────────────
# EDUCATION & RESEARCH
# ─────────────────────────────────────────────

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
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "Imperial College"})
    return events[:15]

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
        if title and len(title) > 5 and title not in seen and is_valid_url(url):
            seen.add(title)
            events.append({"title": title, "date": date, "url": url, "source": "BrainStation London"})
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
]

_CURATED_EMOJI = {
    "Hackathons": "🛠️", "Cyber & Infosec": "🔐", "Intelligence & Security": "🧠",
    "Defence & Geopolitics": "🎖️", "Tech & AI": "🤖", "Business & Networking": "💼",
}

def _scrape_curated(category):
    return [
        {**ev, "source": f"Curated — {category}", "location": "London"}
        for ev in CURATED_LONDON_EVENTS
        if ev["category"] == category and _is_future(ev["date"])
    ]

for _cat in sorted({e["category"] for e in CURATED_LONDON_EVENTS}):
    def _make_curated_scraper(c):
        @source(f"Curated — {c}", _CURATED_EMOJI.get(c, "📌"), c)
        def _scraper():
            return _scrape_curated(c)
        return _scraper
    _make_curated_scraper(_cat)

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


def scrape_luma_calendar(name, cal_id, limit=20):
    """One Luma calendar -> its upcoming LONDON events.

    Geography matters here because most of these calendars are global.
    Luma gives two usable signals and both are checked:

      timezone          - "Europe/London" vs "Europe/Lisbon", "Asia/Kolkata",
                          "America/New_York"...  This is the reliable one:
                          it caught "SEV0 - The reliability conference (SF)",
                          which a city-name denylist misses entirely because
                          the title only says "(SF)".
      geo_address_info  - the city, passed through as `location` so the
                          catalog-level is_london() check has something to
                          work with. Previously no location was set at all,
                          so that filter was judging on the title alone.

    Events with no timezone at all are kept rather than dropped - same
    conservative principle as is_london(): silently losing a real London
    event is worse than letting an occasional stray through."""
    events = []
    try:
        url = (f"https://api.lu.ma/calendar/get-items"
               f"?calendar_api_id={cal_id}&pagination_limit={limit}")
        r   = requests.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}, timeout=12)
        if r.status_code != 200:
            log.warning(f"Luma {name}: HTTP {r.status_code}")
            return events
        for entry in r.json().get("entries",[]):
            ev    = entry.get("event",{})
            title = ev.get("name")
            if not title:
                continue
            tz = (ev.get("timezone") or "").strip()
            if tz and tz != "Europe/London":
                continue
            geo  = ev.get("geo_address_info") or {}
            city = (geo.get("city") or geo.get("city_state") or "").strip()
            slug  = ev.get("url") or ev.get("api_id","")
            event_url = f"https://lu.ma/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at","")
            date  = start[:10] if start else None
            time_str = start[11:16] if len(start) >= 16 else None
            events.append({"title": title, "date": date, "time": time_str,
                           "url": event_url or "https://lu.ma", "source": name,
                           "location": city or "London"})
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
        r = requests.get(f"https://luma.com/user/{username}", headers=HEADERS, timeout=12)
        if r.status_code != 200:
            log.warning(f"Luma user {username}: HTTP {r.status_code}")
            return None
        m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        return (data.get("props", {}).get("pageProps", {})
                    .get("initialData", {}).get("user", {}).get("api_id"))
    except Exception as e:
        log.warning(f"Luma user {username} lookup failed: {e}")
        return None


def scrape_luma_user(name, username, limit=20):
    """One Luma user profile -> their upcoming LONDON events.

    Same geography rules as scrape_luma_calendar: the event timezone is the
    reliable signal, geo_address_info supplies the city for the catalog-level
    is_london() check, and events with no timezone at all are kept."""
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
            ev    = entry.get("event", entry)
            title = ev.get("name")
            if not title:
                continue
            tz = (ev.get("timezone") or "").strip()
            if tz and tz != "Europe/London":
                continue
            iso = (ev.get("start_at") or "")[:10]
            if not _is_future(iso):
                continue
            geo  = ev.get("geo_address_info") or {}
            city = (geo.get("city") or geo.get("city_state") or "").strip()
            slug = ev.get("url") or ev.get("api_id", "")
            event_url = f"https://luma.com/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at", "")
            events.append({"title": title, "date": iso,
                           "time": start[11:16] if len(start) >= 16 else None,
                           "url": event_url or "https://luma.com", "source": name,
                           "location": city or "London"})
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
    past, which _is_future() would then silently drop."""
    month = _MONTH_LOOKUP.get((mon_word or "").lower()[:3])
    if not month:
        return None
    try:
        day = int(day)
    except (TypeError, ValueError):
        return None
    if year is None:
        today = datetime.now(timezone.utc).date()
        year = today.year
        # more than a month in the past reads as next year's edition
        if (month, day) < (today.month, today.day) and (today.month - month) > 1:
            year += 1
    try:
        return f"{int(year):04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return None


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


@source("Hackathon Atlas", "🗺️", "Hackathons")
def scrape_hackathon_atlas():
    """hackathonatlas.com - global hackathon directory, ~500 upcoming.

    Server-rendered, so the cards are in the HTML, but the city filter is
    client-side only - there is no ?city=London URL to request. The whole
    first page is fetched and filtered here instead. Cards print "Aug 16 ·
    London" with no year; _iso_from_month_day infers it."""
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
    return events[:25]


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


@source("Eventbrite Hackathons London", "🎫", "Hackathons")
def scrape_eventbrite_hackathons():
    """Eventbrite's London hackathon category - already London-scoped by URL,
    so it does not need the strict rule the global directories do."""
    return _scrape_eventbrite("hackathon", "Eventbrite Hackathons London")


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


def resolve_luma_host(slug, timeout=10):
    """The calendar hosting one Luma event, as a plain dict, or None.

    Uses api.luma.com/url, which answers with {"kind":"event","data":{...}}.
    The calendar sits at data.calendar - reading it off the top level instead
    is what made an earlier pass at this conclude, wrongly, that the endpoint
    exposed no host information."""
    try:
        r = requests.get("https://api.luma.com/url", params={"url": slug},
                         headers={"User-Agent": HEADERS["User-Agent"],
                                  "Accept": "application/json"},
                         timeout=timeout)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or {}
        cal  = data.get("calendar") or {}
        cal_id = cal.get("api_id")
        if not cal_id:
            return None
        return {
            "identifier": cal_id,
            "name": (cal.get("name") or "").strip(),
            "slug": cal.get("slug"),
            "city": (cal.get("geo_city") or "").strip(),
            "timezone": (cal.get("timezone") or "").strip(),
        }
    except Exception as e:
        log.debug(f"Luma host lookup failed for {slug}: {e}")
        return None


def known_luma_identifiers():
    """Calendar ids we already scrape, so harvesting only reports the gaps."""
    return {cal_id for cal_id, _, _ in LUMA_CALENDARS.values()}


def known_luma_slugs():
    """Vanity slugs we already scrape, as a second way to recognise our own.

    LUMA_USERS is keyed by username, not calendar id, so a profile we already
    track has no entry in known_luma_identifiers() - and its own events would
    otherwise come back as a "missing" source, sending whoever reviews the
    candidate list off to add something already there."""
    return {u.lower() for u in LUMA_USERS}


# Slugs resolved earlier in this process. Host calendars don't change, so
# re-resolving the same event every 30 minutes would be pure waste - and this
# is what keeps a recurring scrape from growing into a request storm.
_resolved_slugs = {}


def harvest_luma_hosts(events, known_ids=None, max_lookups=40):
    """Untracked host calendars behind the events we just ingested.

    Returns a list of candidate dicts, busiest first. `max_lookups` bounds the
    extra HTTP calls one scrape may make: unresolved slugs are worked through
    a batch at a time, so a catalog with hundreds of Luma events converges
    over several scrapes instead of hammering the API on one."""
    known = set(known_ids if known_ids is not None else known_luma_identifiers())
    known_slugs = known_luma_slugs()
    candidates, budget = {}, max_lookups

    for ev in events:
        slug = luma_slug(ev.get("url"))
        if not slug:
            continue
        if slug not in _resolved_slugs:
            if budget <= 0:
                continue                      # leave it for the next scrape
            budget -= 1
            host = resolve_luma_host(slug)
            time.sleep(0.15)                  # be a polite client
            # Only successes are remembered. Caching a None would let one
            # timeout hide that organiser for the entire life of the process,
            # and the lookup budget already bounds the cost of retrying.
            if host:
                _resolved_slugs[slug] = host
        else:
            host = _resolved_slugs[slug]
        if not host or host["identifier"] in known:
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

    ranked = sorted(candidates.values(), key=lambda c: -c["event_count"])
    if ranked:
        log.info(f"Source discovery: {len(ranked)} untracked Luma calendars behind "
                 f"ingested events (top: {ranked[0]['name']} x{ranked[0]['event_count']})")
    return ranked


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
