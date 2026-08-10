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


def is_london(ev, source_name: str = None) -> bool:
    """True unless the event positively identifies itself as somewhere else.

    Conservative by design: an event is only dropped when it names a city that
    isn't London. Anything with no location signal is kept, because these
    sources are London-scoped by default and silently dropping real London
    events is a worse failure than letting an occasional stray through."""
    hay = f"{ev.get('title','')} {ev.get('location','')}"

    if source_name in _CITY_PREFIXED_SOURCES:
        m = _CITY_PREFIX_RE.match(ev.get("title", "") or "")
        if m:
            return bool(re.search(r"\blondon\b", m.group(1), re.IGNORECASE))
        # no prefix at all - fall through to the generic check

    if re.search(r"\blondon\b", hay, re.IGNORECASE):
        return True                       # explicitly London - always keep
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

def is_valid_event(title):
    if not title or len(title) < 8:
        return False
    tl = title.lower().strip()
    if tl in JUNK_TITLES:
        return False
    if len(title) > 200:
        return False
    # Reject job postings
    if any(jw in tl for jw in JOB_TITLE_WORDS):
        return False
    # Reject pure-uppercase titles that are nav headings
    stripped = title.replace(" ", "").replace("&", "").replace("|", "")
    if stripped.isupper() and len(stripped) > 10:
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
    # RETIRED: "Jody Saunders" (cal-yzm8pBHRjoQCz1E) - the calendar returns
    # HTTP 404, it has been deleted upstream.
    # NOT ADDED, deliberately: "Future: UK" (cal-eP031AKL1RBuO3j) - its only
    # upcoming items are "Optimistic Picnic at Hyde Park" and "Dinners -
    # Interest", which are the same not-a-tech-event category we stripped
    # out of the Luma Discover feed. "Corgi London", "London AI" and
    # "KS Events" return zero upcoming items from the API, and one link
    # resolved to a private personal calendar.
}


def scrape_luma_calendar(name, cal_id):
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
        url = f"https://api.lu.ma/calendar/get-items?calendar_api_id={cal_id}&pagination_limit=20"
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
            return scrape_luma_calendar(n, c)
        return _scraper
    _make_scraper(_name, _cal_id, _emoji, _cat)

# RETIRED: GDG London. lu.ma/user/gdglondon now redirects to luma.com and the
# page is fully client-rendered - its __NEXT_DATA__ blob contains no events,
# no calendar id and no event ids, and Luma exposes no public user-events API
# (get-events / get-profile-items / get-hosting-events all return 404). The
# old selector also looked for a[href*='lu.ma'], which stopped matching after
# the luma.com rename. Scraping it would need a headless browser, which is a
# much heavier dependency than this source is worth.

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
