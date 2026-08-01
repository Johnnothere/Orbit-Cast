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

@source("BISI", "🔍", "Intelligence & Security")
def scrape_bisi():
    events = []
    soup = fetch("https://bisi.org.uk/events")
    if not soup:
        return events
    for art in soup.select("article"):
        h = art.select_one("h2, h3, h4")
        a = art.select_one("a[href]")
        d = art.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://bisi.org.uk")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "BISI"})
    return events[:20]

@source("Intelligence Forums", "🧠", "Intelligence & Security")
def scrape_intelligence_forums():
    events = []
    soup = fetch("https://www.intelligence-forums.com/upcoming-forums")
    if not soup:
        return events
    for art in soup.select("article"):
        h = art.select_one("h2, h3, h4")
        a = art.select_one("a[href]")
        d = art.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.intelligence-forums.com")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "Intelligence Forums"})
    return events[:15]

@source("OSMOSIS", "🕵️", "Intelligence & Security")
def scrape_osmosis():
    events = []
    soup = fetch("https://osmosiscon.com/")
    if not soup:
        return events
    for el in soup.select("[class*=event], .session, article"):
        h = el.select_one("h2, h3, h4, .title")
        a = el.select_one("a[href]")
        d = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://osmosiscon.com")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "OSMOSIS"})
    return events[:10]

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
    events = []
    soup = fetch("https://www.infosecurityeurope.com/en-gb.html")
    if not soup:
        return events
    for el in soup.select("[class*=card], article, .session"):
        h     = el.select_one("h2, h3, h4, .title")
        a     = el.select_one("a[href]")
        d     = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.infosecurityeurope.com")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "Infosecurity Europe"})
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
            slug  = ev.get("url") or ev.get("api_id","")
            event_url = f"https://lu.ma/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at","")
            date  = start[:10] if start else None
            if title and title not in seen:
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
# LUMA CALENDARS
# ─────────────────────────────────────────────

LUMA_CALENDARS = {
    "Plugged":          "cal-FAtYQ9ilaLj34DO",
    "Encode Club":      "cal-8LJYo5N7QObN2DI",
    "Claude Community": "cal-TOpA5LAFfuDeFpu",
    "AI Native Dev":    "cal-uYzPjdxdCyDtuNO",
    "SRV Frontier":     "cal-LbyWro3ZdQSojJX",
    "Vercel Events":    "cal-hp9HP2UFTGNaMnY",
    # RETIRED: "Jody Saunders" (cal-yzm8pBHRjoQCz1E) - the calendar returns
    # HTTP 404, it has been deleted upstream.
}
LUMA_EMOJIS = {
    "Plugged":"🔌","Encode Club":"⛓️","Claude Community":"🟠",
    "AI Native Dev":"⚡","SRV Frontier":"🚀","Vercel Events":"▲",
}

def scrape_luma_calendar(name, cal_id):
    events = []
    try:
        url = f"https://api.lu.ma/calendar/get-items?calendar_api_id={cal_id}&pagination_limit=20"
        r   = requests.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}, timeout=12)
        if r.status_code != 200:
            return events
        for entry in r.json().get("entries",[]):
            ev    = entry.get("event",{})
            title = ev.get("name")
            slug  = ev.get("url") or ev.get("api_id","")
            event_url = f"https://lu.ma/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at","")
            date  = start[:10] if start else None
            time_str = start[11:16] if len(start) >= 16 else None
            if title:
                events.append({"title": title, "date": date, "time": time_str,
                               "url": event_url or "https://lu.ma", "source": name})
    except Exception as e:
        log.warning(f"Luma {name} failed: {e}")
    return events

for _name, _cal_id in LUMA_CALENDARS.items():
    _emoji = LUMA_EMOJIS.get(_name, "📅")
    def _make_scraper(n, c, e):
        @source(n, e, "Builder & Tech Community")
        def _scraper():
            return scrape_luma_calendar(n, c)
        return _scraper
    _make_scraper(_name, _cal_id, _emoji)

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
