#!/usr/bin/env python3
"""
ORBITCAST — Event Scraper
Scrapes 20+ sources across Tech, Defence, Intelligence, Business, Education & Hackathons.
"""

import os, json, time, hashlib, logging, requests, feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("event-radar")

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
# HACKATHONS  (hardcoded — reliable, curated)
# ─────────────────────────────────────────────

@source("London Hackathons", "⚡", "Hackathons")
def scrape_london_hackathons():
    # NOTE: this curated list is hand-maintained, not scraped - it goes stale.
    # Every entry below is now in the past (last checked 2026-07-31) and gets
    # filtered out of AI recommendations by ai_engine's date guard, but it
    # still clutters the Hackathons browse tab. Needs a real content refresh.
    raw = [
        {"title": "The Agent Economy Buildathon", "date": "2026-06-01", "url": "https://lnkd.in/enUFiCUU"},
        {"title": "Fullhouse — UK's first poker bot hackathon", "date": "2026-06-01", "url": "https://lnkd.in/eZKnwnGD"},
        {"title": "Wayflyer × Fin | Build the future of eCommerce", "date": "2026-06-03", "url": "https://lu.ma/v4bzvbka"},
        {"title": "Pop The Bubble Hackathon", "date": "2026-06-05", "url": "https://lu.ma/035ubxn3"},
        {"title": "NVIDIA Hack for Impact — London", "date": "2026-06-05", "url": "https://lnkd.in/ebmmXJ4E"},
        {"title": "AI Risk Content Hackathon — BlueDot Impact", "date": "2026-06-06", "url": "https://lnkd.in/eF5GSG7u"},
        {"title": "VibeHack London — £10k prize pool @ UCL", "date": "2026-06-06", "url": "https://lu.ma/9ef4463s"},
        {"title": "Shared Futures Buildathon", "date": "2026-06-07", "url": "https://lnkd.in/e7npuFaY"},
        {"title": "Move 37 | London Tech Week — AI × Finance", "date": "2026-06-09", "url": "https://lu.ma/v9ydosib"},
        {"title": "Deploy by Antler @ Google London", "date": "2026-06-10", "url": "https://lu.ma/j6wkjmkf"},
        {"title": "London Initiative for Safe AI Hackathon", "date": "2026-06-13", "url": "https://lnkd.in/erBNHS2b"},
        {"title": "Model to Market: The Quantitative Hack", "date": "2026-06-15", "url": "https://lu.ma/4j44j9l0"},
        {"title": "Localhost: On-Device Agent Hackathon", "date": "2026-06-20", "url": "https://lu.ma/8og1gx56"},
        {"title": "GTM Hackathon London", "date": "2026-06-20", "url": "https://lu.ma/hxsfxyvb"},
        {"title": "Hands Off Hackathon — AI-powered autonomous business", "date": "2026-06-25", "url": "https://lu.ma/wl7a90xe"},
        {"title": "London Agentic AI Hack Night", "date": "2026-06-25", "url": "https://lu.ma/dnoe595m"},
        {"title": "AI in Government Hackathon — ElevenLabs × IAI", "date": "2026-06-25", "url": "https://lu.ma/1d7szvu5"},
        {"title": "European Defense Tech Hackathon — 200+ hackers", "date": "2026-06-26", "url": "https://lnkd.in/e5sW5yaK"},
        {"title": "ARIA + MaterialHack — Biomaterials × Biomanufacturing", "date": "2026-06-26", "url": "https://lnkd.in/e2SVbwBs"},
    ]
    return [{**ev, "source": "London Hackathons"} for ev in raw]

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

@source("Critical Communications World", "📡", "Tech & AI")
def scrape_ccw():
    events = []
    soup = fetch("https://www.critical-communications-world.com/")
    if not soup:
        return events
    for art in soup.select("article, [class*=card]"):
        h     = art.select_one("h2, h3, h4")
        a     = art.select_one("a[href]")
        d     = art.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.critical-communications-world.com")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "Critical Communications World"})
    return events[:10]

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

@source("AI Expo Global", "🤖", "Tech & AI")
def scrape_ai_expo():
    events = []
    soup = fetch("https://www.ai-expo.net/global/")
    if not soup:
        return events
    for el in soup.select("article, [class*=session], [class*=speaker], [class*=card]"):
        h     = el.select_one("h2, h3, h4, .title")
        a     = el.select_one("a[href]")
        d     = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url   = fix_url(a["href"] if a else "", "https://www.ai-expo.net")
        date  = d.get_text(strip=True) if d else None
        if is_valid_event(title) and is_valid_url(url):
            events.append({"title": title, "date": date, "url": url, "source": "AI Expo Global"})
    return events[:10]

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
    "Jody Saunders":    "cal-yzm8pBHRjoQCz1E",
}
LUMA_EMOJIS = {
    "Plugged":"🔌","Encode Club":"⛓️","Claude Community":"🟠",
    "AI Native Dev":"⚡","SRV Frontier":"🚀","Vercel Events":"▲","Jody Saunders":"💡",
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

@source("GDG London", "🔷", "Builder & Tech Community")
def scrape_gdg_london():
    events = []
    soup = fetch("https://lu.ma/user/gdglondon")
    if not soup:
        return events
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return events
    try:
        for a in soup.select("a[href*='lu.ma']"):
            href = a.get("href","")
            txt  = a.get_text(strip=True)
            if href and txt and len(txt) > 5 and "/user/" not in href and is_valid_url(href):
                events.append({"title": txt, "date": None, "url": href, "source": "GDG London"})
    except Exception as e:
        log.warning(f"GDG London parse failed: {e}")
    return events[:10]

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
