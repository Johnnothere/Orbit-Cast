#!/usr/bin/env python3
"""
EVENT RADAR — Intelligence, Tech & Defence Event Monitor
Scrapes 18+ sources. Sends Telegram alerts on new events.
"""

import os
import json
import time
import hashlib
import logging
import requests
import feedparser
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("event-radar")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEEN_FILE = Path("seen_events.json")

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
    raw = f"{title.lower().strip()}{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TELEGRAM SKIP] {message[:80]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)


JUNK_TITLES = {
    "register free now", "register now", "skip to main content",
    "view accessibility support page", "sign up", "learn more",
    "find out more", "read more", "book now", "buy tickets",
}

def is_valid_event(title):
    if not title or len(title) < 8:
        return False
    if title.lower().strip() in JUNK_TITLES:
        return False
    if len(title) > 200:
        return False
    return True


def fix_url(url, base):
    if not url:
        return base
    url = url.strip()
    if url.startswith('http'):
        return url
    if url.startswith('/'):
        return base.rstrip('/') + url
    return base.rstrip('/') + '/' + url

# ─────────────────────────────────────────────
# SCRAPERS
# ─────────────────────────────────────────────

SOURCES = []

def source(name, emoji, category):
    def decorator(fn):
        SOURCES.append({"name": name, "emoji": emoji, "category": category, "fn": fn})
        return fn
    return decorator


# ── INTELLIGENCE & SECURITY ──────────────────

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
        url = a["href"] if a else "https://rusi.org/events"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title) and url:
            url = fix_url(url, "https://rusi.org")
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
        url = a["href"] if a else "https://bisi.org.uk/events"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title) and url:
            url = fix_url(url, "https://bisi.org.uk")
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
        url = a["href"] if a else "https://www.intelligence-forums.com"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title) and url:
            url = fix_url(url, "https://www.intelligence-forums.com")
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
        url = (a["href"] if a else "https://osmosiscon.com/")
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://osmosiscon.com")
            events.append({"title": title, "date": date, "url": url, "source": "OSMOSIS"})
    return events[:10]


# ── DEFENCE & GEOPOLITICS ────────────────────

@source("London Defence Conference", "🎖️", "Defence & Geopolitics")
def scrape_ldc():
    events = []
    soup = fetch("https://londondefenceconference.com/")
    if not soup:
        return events
    for el in soup.select("article, [class*=card], section h2, section h3"):
        h = el if el.name in ["h2", "h3"] else el.select_one("h2, h3, h4")
        a = el.select_one("a[href]") if el.name not in ["h2", "h3"] else el.find_parent("a")
        title = (el.get_text(strip=True) if el.name in ["h2", "h3"] else (h.get_text(strip=True) if h else None))
        url = a["href"] if a else "https://londondefenceconference.com/"
        if is_valid_event(title):
            url = fix_url(url, "https://londondefenceconference.com")
            events.append({"title": title, "date": None, "url": url, "source": "London Defence Conference"})
    return events[:5]


# ── CYBER & INFOSEC ──────────────────────────

@source("Infosecurity Europe", "🔐", "Cyber & Infosec")
def scrape_infosec_europe():
    events = []
    soup = fetch("https://www.infosecurityeurope.com/en-gb.html")
    if not soup:
        return events
    for el in soup.select("[class*=card], article, .session"):
        h = el.select_one("h2, h3, h4, .title")
        a = el.select_one("a[href]")
        d = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://www.infosecurityeurope.com"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://www.infosecurityeurope.com")
            events.append({"title": title, "date": date, "url": url, "source": "Infosecurity Europe"})
    return events[:10]


# ── TECH & AI ────────────────────────────────

@source("Critical Communications World", "📡", "Tech & AI")
def scrape_ccw():
    events = []
    soup = fetch("https://www.critical-communications-world.com/")
    if not soup:
        return events
    for art in soup.select("article, [class*=card]"):
        h = art.select_one("h2, h3, h4")
        a = art.select_one("a[href]")
        d = art.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://www.critical-communications-world.com/"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://www.critical-communications-world.com")
            events.append({"title": title, "date": date, "url": url, "source": "Critical Communications World"})
    return events[:10]


@source("Digital Government", "🏛️", "Tech & AI")
def scrape_digital_gov():
    events = []
    soup = fetch("https://www.digital-government.co.uk/")
    if not soup:
        return events
    for el in soup.select("[class*=event], article, [class*=card]"):
        h = el.select_one("h2, h3, h4")
        a = el.select_one("a[href]")
        d = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://www.digital-government.co.uk/"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://www.digital-government.co.uk")
            events.append({"title": title, "date": date, "url": url, "source": "Digital Government"})
    return events[:10]


@source("AI Expo Global", "🤖", "Tech & AI")
def scrape_ai_expo():
    events = []
    soup = fetch("https://www.ai-expo.net/global/")
    if not soup:
        return events
    for el in soup.select("article, [class*=session], [class*=speaker], [class*=card]"):
        h = el.select_one("h2, h3, h4, .title")
        a = el.select_one("a[href]")
        d = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://www.ai-expo.net/global/"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://www.ai-expo.net")
            events.append({"title": title, "date": date, "url": url, "source": "AI Expo Global"})
    return events[:10]


# ── EDUCATION & RESEARCH ─────────────────────

@source("Imperial College", "🎓", "Education & Research")
def scrape_imperial():
    events = []
    soup = fetch("https://www.imperial.ac.uk/whats-on/")
    if not soup:
        return events
    for el in soup.select(".event"):
        h = el.select_one("h2, h3, h4, .event-title, [class*=title]")
        a = el.select_one("a[href]")
        d = el.select_one("time, .date, [class*=date]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://www.imperial.ac.uk/whats-on/"
        date = d.get_text(strip=True) if d else None
        if is_valid_event(title):
            url = fix_url(url, "https://www.imperial.ac.uk")
            events.append({"title": title, "date": date, "url": url, "source": "Imperial College"})
    return events[:15]


@source("BrainStation London", "📚", "Education & Research")
def scrape_brainstation():
    events = []
    soup = fetch("https://brainstation.io/events/london")
    if not soup:
        return events
    seen_titles = set()
    for art in soup.select("article"):
        h = art.select_one("h2, h3, h4, [class*=title]")
        a = art.select_one("a[href]")
        d = art.select_one("time, .date, [class*=date], [class*=time]")
        title = h.get_text(strip=True) if h else None
        url = a["href"] if a else "https://brainstation.io/events/london"
        date = d.get_text(strip=True) if d else None
        if title and len(title) > 5 and title not in seen_titles:
            seen_titles.add(title)
            url = fix_url(url, "https://brainstation.io")
            events.append({"title": title, "date": date, "url": url, "source": "BrainStation London"})
    return events[:15]


# ── LUMA CALENDARS (API-based) ───────────────

LUMA_CALENDARS = {
    "Plugged":           "cal-FAtYQ9ilaLj34DO",
    "Encode Club":       "cal-8LJYo5N7QObN2DI",
    "Claude Community":  "cal-TOpA5LAFfuDeFpu",
    "AI Native Dev":     "cal-uYzPjdxdCyDtuNO",
    "SRV Frontier":      "cal-LbyWro3ZdQSojJX",
    "Vercel Events":     "cal-hp9HP2UFTGNaMnY",
    "Jody Saunders":     "cal-yzm8pBHRjoQCz1E",
}

LUMA_EMOJIS = {
    "Plugged": "🔌",
    "Encode Club": "⛓️",
    "Claude Community": "🟠",
    "AI Native Dev": "⚡",
    "SRV Frontier": "🚀",
    "Vercel Events": "▲",
    "Jody Saunders": "💡",
}


def scrape_luma_calendar(name, cal_id):
    events = []
    try:
        url = f"https://api.lu.ma/calendar/get-items?calendar_api_id={cal_id}&pagination_limit=20"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=12)
        if r.status_code != 200:
            return events
        entries = r.json().get("entries", [])
        for entry in entries:
            ev = entry.get("event", {})
            title = ev.get("name")
            slug = ev.get("url") or ev.get("api_id", "")
            event_url = f"https://lu.ma/{slug}" if slug and not slug.startswith("http") else slug
            start = ev.get("start_at", "")
            date = start[:10] if start else None
            if title:
                events.append({"title": title, "date": date, "url": event_url or f"https://lu.ma", "source": name})
    except Exception as e:
        log.warning(f"Luma {name} failed: {e}")
    return events


# Register all Luma calendars as sources
for _name, _cal_id in LUMA_CALENDARS.items():
    _emoji = LUMA_EMOJIS.get(_name, "📅")
    # Closure trick to capture loop variables
    def _make_scraper(n, c):
        @source(n, _emoji, "Builder & Tech Community")
        def _scraper():
            return scrape_luma_calendar(n, c)
        return _scraper
    _make_scraper(_name, _cal_id)


# ── GDG LONDON (HTML fallback) ───────────────

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
        data = json.loads(script.string)
        props = data.get("props", {}).get("pageProps", {}).get("initialData", {})
        # Try to find event links from page
        for a in soup.select("a[href*='lu.ma']"):
            href = a.get("href", "")
            txt = a.get_text(strip=True)
            if href and txt and len(txt) > 5 and "/user/" not in href:
                events.append({"title": txt, "date": None, "url": href, "source": "GDG London"})
    except Exception as e:
        log.warning(f"GDG London parse failed: {e}")
    return events[:10]


# ─────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────

def run(dry_run=False):
    seen = load_seen()
    all_new = []
    results_summary = []

    for src in SOURCES:
        name = src["name"]
        emoji = src["emoji"]
        category = src["category"]
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
                seen[eid] = {
                    "title": ev["title"],
                    "source": name,
                    "seen_at": datetime.now(timezone.utc).isoformat(),
                }
                new_events.append(ev)
                all_new.append({**ev, "emoji": emoji, "category": category})

        results_summary.append({
            "source": name,
            "emoji": emoji,
            "category": category,
            "total": len(events),
            "new": len(new_events),
        })
        log.info(f"  {name}: {len(events)} events, {len(new_events)} new")
        time.sleep(0.5)

    # Send Telegram for new events
    if all_new and not dry_run:
        # Group by category
        by_cat = {}
        for ev in all_new:
            by_cat.setdefault(ev["category"], []).append(ev)

        for cat, evs in by_cat.items():
            lines = [f"<b>📡 New Events — {cat}</b>\n"]
            for ev in evs[:10]:
                date_str = f" · {ev['date']}" if ev.get("date") else ""
                lines.append(f"{ev['emoji']} <a href=\"{ev['url']}\">{ev['title']}</a>{date_str}\n   <i>{ev['source']}</i>")
            msg = "\n".join(lines)
            send_telegram(msg)
            time.sleep(0.3)

    save_seen(seen)
    log.info(f"Done. {len(all_new)} new events across {len(SOURCES)} sources.")
    return results_summary, all_new


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    summary, new_events = run(dry_run=dry)
    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    for s in summary:
        print(f"{s['emoji']} {s['source']}: {s['total']} total, {s['new']} new")
    print(f"\nTotal new events: {sum(s['new'] for s in summary)}")
