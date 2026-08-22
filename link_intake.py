"""
ORBITCAST — link intake

Paste an event URL, get back a verdict against OrbitCast's own catalog rules
and, when it passes, a catalog-ready event record.

This exists because adding one event used to mean editing CURATED_LONDON_EVENTS
in scraper.py and pushing. The rules being checked here are not new - they are
the ones already enforced across the scrapers and repeated constantly in
review:

  1. It has to be a real event page, with a title and a date.
  2. London, or genuinely online. Online is allowed (hackathons, some Chatham
     House webinars); a different CITY is not.
  3. It has to be upcoming. A past event is never worth a recommendation.
  4. An aggregator is not a source. hackathonatlas.com/hackathons/<uuid> is an
     index card - resolve through to the organiser's own page and keep the
     ORGANISER's title, not the aggregator's rewrite.
  5. It must not already be in the catalog.
  6. A calendar/organiser page is not a one-off event. If the link is a Luma
     calendar or user profile, the honest answer is "this should be a recurring
     source", not "here is one event" - so that is what gets reported.

Extraction is deliberately layered cheapest-first: schema.org JSON-LD (a
published contract the site maintains for Google), then OpenGraph/meta tags,
then Claude reading the visible text. Most real event pages never reach the
model.
"""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import scraper

log = logging.getLogger("orbitcast.link_intake")

CATEGORIES = [
    "Defence & Geopolitics", "Intelligence & Security", "Tech & AI",
    "Cyber & Infosec", "Education & Research", "Builder & Tech Community",
    "Business & Networking", "Hackathons",
]

# Mirrors _CURATED_EMOJI in scraper.py, extended to cover all eight so an
# operator-added event never lands without one.
CATEGORY_EMOJI = {
    "Defence & Geopolitics": "🎖️", "Intelligence & Security": "🧠",
    "Tech & AI": "🤖", "Cyber & Infosec": "🔐",
    "Education & Research": "🎓", "Builder & Tech Community": "🛠️",
    "Business & Networking": "💼", "Hackathons": "⚡",
}

# Pages that list OTHER people's events. A link to one of these is a request to
# add a feed, not an event, and is reported as such.
_CALENDAR_URL_RE = re.compile(
    r"(luma\.com/(?:cal-|u/)|lu\.ma/(?:cal-|u/)|meetup\.com/[^/]+/?$"
    r"|eventbrite\.[a-z.]+/o/|/events/?$|/calendar/?$)", re.IGNORECASE)

# Aggregators: the event exists, but this URL is the directory card for it.
_AGGREGATOR_HOSTS = ("hackathonatlas.com",)


def _norm_url(url: str) -> str:
    """Trim tracking noise and trailing slashes so the same event submitted
    from two places is recognised as one row."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    p = urlparse(url)
    query = "&".join(
        q for q in (p.query or "").split("&")
        if q and not q.lower().split("=")[0].startswith(("utm_", "fbclid", "gclid", "mc_"))
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def _first_text(*values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _from_jsonld(soup):
    """schema.org Event node, if the page publishes one."""
    nodes = scraper._jsonld_events(soup)
    if not nodes:
        return None
    node = nodes[0]
    start = _first_text(node.get("startDate"))
    return {
        "title": _first_text(node.get("name")),
        "date": scraper.to_iso_date(start) or "",
        "time": (start[11:16] if len(start) >= 16 and "T" in start else ""),
        "location": scraper._jsonld_location(node),
        "description": _first_text(node.get("description"))[:600],
        "via": "schema.org JSON-LD",
    }


def _from_meta(soup):
    """OpenGraph / <meta> fallback, deliberately narrow.

    Only claims a page is an event when the page SAYS it is - og:type=event, or
    a real event:start_time. An earlier version accepted any page with a title
    and then scraped a date out of the body text, which read rusi.org's
    homepage as an event called "Homepage" dated 9 July (a date that happened
    to appear somewhere in the page furniture). A homepage, a blog index and a
    ticket checkout all have og:title, so a title alone proves nothing. When
    the signals are not here, returning None hands the page to Claude, which is
    asked the is-this-an-event question explicitly."""
    def meta(*names):
        for n in names:
            tag = (soup.find("meta", attrs={"property": n})
                   or soup.find("meta", attrs={"name": n}))
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = _first_text(meta("og:title", "twitter:title"),
                        soup.title.get_text(strip=True) if soup.title else "")
    if not title:
        return None
    raw_date = meta("event:start_time")
    is_event_type = "event" in (meta("og:type") or "").lower()
    if not raw_date and not is_event_type:
        return None
    return {
        "title": title,
        "date": scraper.to_iso_date(raw_date) or "",
        "time": (raw_date[11:16] if len(raw_date) >= 16 and "T" in raw_date else ""),
        "location": meta("og:locality", "event:location") or "",
        "description": meta("og:description", "description")[:600],
        "via": "OpenGraph / meta tags",
    }


def _from_claude(soup, url):
    """Last resort: hand the visible text to Claude.

    Only reached when a page publishes neither JSON-LD nor a usable OG title,
    which in practice means a hand-built conference page. Told explicitly to
    return nulls rather than guess - an invented date here would put a wrong
    date in the catalog, which is worse than refusing the link."""
    import ai_engine
    text = soup.get_text("\n", strip=True)[:6000]
    if len(text) < 80:
        return None
    system = (
        "You read one event page and report only what it actually says. "
        "Return bare JSON: {\"title\": str|null, \"date\": \"YYYY-MM-DD\"|null, "
        "\"time\": \"HH:MM\"|null, \"location\": str|null, \"description\": str|null, "
        "\"is_event_page\": bool, \"is_listing_page\": bool}. "
        "is_event_page is false for a homepage, a blog post, a ticket checkout "
        "or anything that is not one specific event. is_listing_page is true if "
        "it lists MANY events rather than being one. "
        "Never infer or invent a date - if the page does not state one, return "
        "null. Never guess a city; report the venue exactly as written."
    )
    try:
        out = ai_engine._call(system, f"URL: {url}\n\n{text}",
                              max_tokens=700, model=ai_engine.MODEL_EXTRACT)
    except Exception as e:
        log.warning(f"Claude extraction failed for {url}: {e}")
        return None
    if not out.get("is_event_page"):
        return {"title": _first_text(out.get("title")), "date": "", "time": "",
                "location": "", "description": "", "via": "Claude",
                "not_an_event": True,
                "is_listing_page": bool(out.get("is_listing_page"))}
    return {
        "title": _first_text(out.get("title")),
        "date": scraper.to_iso_date(out.get("date")) or "",
        "time": _first_text(out.get("time")),
        "location": _first_text(out.get("location")),
        "description": _first_text(out.get("description"))[:600],
        "via": "Claude (page had no structured data)",
        "is_listing_page": bool(out.get("is_listing_page")),
    }


def _classify(title, description, location):
    """Pick one of the eight catalog categories.

    HACKATHON_RE wins outright before the model is asked, because that is
    exactly how app.py categorises every scraped event - an operator-added
    hackathon must not land in a different category than the same event would
    have if a scraper had found it."""
    if scraper.HACKATHON_RE.search(title or ""):
        return "Hackathons", "title matched the hackathon rule"
    import ai_engine
    system = (
        "Classify one London event into exactly one category. "
        f"Categories: {', '.join(CATEGORIES)}. "
        "Return bare JSON: {\"category\": str, \"why\": str}. "
        "Pick the single best fit; do not invent a category outside the list."
    )
    try:
        out = ai_engine._call(
            system,
            f"Title: {title}\nLocation: {location}\nDescription: {description}",
            max_tokens=300, model=ai_engine.MODEL_EXTRACT)
        cat = out.get("category")
        if cat in CATEGORIES:
            return cat, _first_text(out.get("why"))
    except Exception as e:
        log.warning(f"Category classification failed: {e}")
    return "Business & Networking", "could not classify - defaulted, please correct"


def _check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


def _geo_check(title, location, is_online=None):
    """Rule 2 as one function, so the check is identical whether it runs on
    what the page said or on what the operator corrected it to.

    It used not to be. The London/online verdict was computed once, during the
    first read, and reverify() carried it forward untouched - so an operator
    who fixed a wrong location still had to force the event through on a check
    that was judging the ORIGINAL value. Correcting the input has to be able to
    change the answer, or the correction box is decorative."""
    probe = {"title": title or "", "location": location or ""}
    # The is_online hint only applies when there is no location to read. A
    # location the operator typed is a statement about the format as well as
    # the place: changing "Online" to "London" has to be able to turn the flag
    # off, or a corrected event stays permanently marked virtual.
    if is_online is not None and not (location or "").strip():
        probe["is_online"] = bool(is_online)
    online = scraper.is_online(probe)
    if online:
        return True, _check("London or online", True,
                            "Online event - no city to be wrong about, so it qualifies.")
    if scraper.is_london(probe):
        return False, _check("London or online", True,
                             location or "No city named; source is London-scoped.")
    return False, _check(
        "London or online", False,
        f"Names a city that is not London ({location or 'see title'}). "
        "This is a London-only catalog.")


def _dupe_check(url, title, iso_date, catalog_events, manual_urls):
    """Rule 5, with one distinction the original did not draw.

    A link already in the catalog BECAUSE THE OPERATOR ADDED IT is not the same
    thing as a link that clashes with a scraped event, and reporting them
    identically is what produced the "it says not added when it IS added" bug:
    add a link, then check the same link, and the portal failed it against its
    own successful write. The row is keyed on the url and re-adding it updates
    that row, so this is a pass with an explanation, not a refusal."""
    if url and url in (manual_urls or set()):
        return True, _check(
            "not already listed", True,
            "You already added this link - adding it again updates that entry "
            "rather than creating a second one.")
    for existing in (catalog_events or []):
        if url and _norm_url(existing.get("url", "")) == url:
            return False, _check(
                "not already listed", False,
                f"Already in the catalog from \"{existing.get('source')}\" as "
                f"\"{existing.get('title')}\".")
        if (iso_date and title
                and scraper.norm_title(existing.get("title", "")) == scraper.norm_title(title)
                and scraper.to_iso_date(existing.get("date")) == iso_date):
            return False, _check(
                "not already listed", False,
                f"Already in the catalog from \"{existing.get('source')}\" as "
                f"\"{existing.get('title')}\".")
    return False, _check("not already listed", True, "Not in the catalog yet.")


def verify_link(url: str, catalog_events=None, manual_urls=None):
    """Run a URL through the catalog rules.

    Returns a dict with `ok` (whether it may be added without an override),
    `checks` (every rule, passed or failed, with the reason), and `event` (the
    catalog-ready record). Nothing is written here - the caller decides."""
    norm = _norm_url(url)
    result = {"ok": False, "url": norm, "submitted_url": (url or "").strip(),
              "checks": [], "event": None, "resolved_from": None,
              "suggest_source": None, "already_added": False,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    if not norm or "." not in urlparse(norm).netloc:
        result["checks"].append(_check("valid url", False, "That is not a URL."))
        return result

    host = urlparse(norm).netloc

    # Rule 6 - a calendar page is a feed request, not an event. Reported before
    # anything is fetched, because the right answer is a different action.
    if _CALENDAR_URL_RE.search(norm):
        result["suggest_source"] = (
            "This looks like an organiser's calendar rather than one event. "
            "OrbitCast's rule is that a source must keep feeding, so this is "
            "better added as a recurring source than as a single entry."
        )

    # Rule 4 - resolve an aggregator card through to the organiser's own page.
    fetch_url = norm
    if any(h in host for h in _AGGREGATOR_HOSTS):
        real = scraper._atlas_source_url(norm)
        if real:
            fetch_url = _norm_url(real)
            result["resolved_from"] = norm
            result["url"] = fetch_url
            result["checks"].append(_check(
                "aggregator resolved", True,
                f"{host} is an aggregator; followed through to {urlparse(fetch_url).netloc}."))
        else:
            result["checks"].append(_check(
                "aggregator resolved", False,
                f"{host} is an aggregator and the organiser's own link could not "
                "be found on it. Submit the organiser's page directly."))
            return result

    soup = scraper.fetch(fetch_url)
    if soup is None:
        result["checks"].append(_check(
            "page reachable", False,
            "Could not fetch that page (it may block scrapers, or be down)."))
        return result
    result["checks"].append(_check("page reachable", True, "Fetched the page."))

    data = _from_jsonld(soup) or _from_meta(soup) or _from_claude(soup, fetch_url)
    if not data or not data.get("title"):
        result["checks"].append(_check(
            "is an event page", False,
            "No event details found on that page - no structured data, no title."))
        return result
    if data.get("not_an_event"):
        result["checks"].append(_check(
            "is an event page", False,
            "That page does not describe one specific event."))
        return result
    result["checks"].append(_check(
        "is an event page", True, f"Read the details via {data['via']}."))

    if data.get("is_listing_page") and not result["suggest_source"]:
        result["suggest_source"] = (
            "That page lists several events. Adding it as one entry would "
            "capture only the first - it is better wired as a recurring source.")

    title = data["title"]
    location = data.get("location") or ""
    ev = {"title": title, "date": data.get("date") or "", "time": data.get("time") or "",
          "location": location, "description": data.get("description") or "",
          "url": fetch_url}

    # Rule 1 - a date is mandatory. "Some Thursday" is not a catalog entry.
    if not ev["date"]:
        result["checks"].append(_check(
            "has a date", False,
            "No event date could be read from that page. OrbitCast will not "
            "guess one - add it by hand below if you know it."))
    else:
        result["checks"].append(_check("has a date", True, ev["date"]))

    # Rule 3 - upcoming.
    if ev["date"]:
        if scraper._is_future(ev["date"]):
            result["checks"].append(_check("upcoming", True, f"Runs on {ev['date']}."))
        else:
            result["checks"].append(_check(
                "upcoming", False, f"{ev['date']} has already passed."))

    # Rule 2 - London, or genuinely online.
    online, geo = _geo_check(title, location)
    result["checks"].append(geo)
    ev["is_online"] = bool(online)

    # Rule 5 - not already in the catalog.
    already, dupe = _dupe_check(fetch_url, title, ev["date"], catalog_events, manual_urls)
    result["already_added"] = already
    result["checks"].append(dupe)

    category, why = _classify(title, ev["description"], location)
    ev["category"] = category
    ev["emoji"] = CATEGORY_EMOJI.get(category, "📌")
    result["category_reason"] = why

    result["event"] = ev
    result["ok"] = all(c["ok"] for c in result["checks"])
    return result


def reverify(verdict: dict, event: dict, catalog_events=None,
             manual_urls=None) -> dict:
    """Re-run every check whose answer an operator correction can change.

    Only the page-reading checks are carried forward - the page was already
    fetched and read, and re-fetching it would say the same thing. Everything
    downstream of an editable field is recomputed, which now includes the
    London/online verdict: correcting a wrong location used to leave the
    original geography failure standing, forcing the operator to override a
    check they had just fixed."""
    date_str = (event.get("date") or "").strip()
    iso = scraper.to_iso_date(date_str) or ""
    if iso:
        event["date"] = iso

    recomputed = ("has a date", "upcoming", "not already listed", "London or online")
    keep = [c for c in verdict.get("checks", []) if c["check"] not in recomputed]

    if iso:
        keep.append(_check("has a date", True, f"{iso} (supplied by operator)."))
        keep.append(_check("upcoming", scraper._is_future(iso),
                           f"Runs on {iso}." if scraper._is_future(iso)
                           else f"{iso} has already passed."))
    else:
        keep.append(_check("has a date", False,
                           f"\"{date_str}\" is not a date OrbitCast can read - "
                           "use YYYY-MM-DD."))

    online, geo = _geo_check(event.get("title"), event.get("location"),
                             event.get("is_online"))
    event["is_online"] = bool(online)
    keep.append(geo)

    already, dupe = _dupe_check(_norm_url(event.get("url") or ""),
                                event.get("title"), iso, catalog_events, manual_urls)
    verdict["already_added"] = already
    keep.append(dupe)

    verdict["checks"] = keep
    verdict["ok"] = all(c["ok"] for c in keep)
    return verdict


# ──────────────────────────────────────────────────────────────────────────
# ORGANISER (Luma calendar) intake
# ──────────────────────────────────────────────────────────────────────────
#
# Adding an ORGANISER is a bigger commitment than adding an event: it is scraped
# every refresh from then on. So it is verified against the organiser's REAL
# upcoming events rather than against whatever a spreadsheet claims, which also
# sidesteps the fact that an export's timezone field is not a city -
# "Europe/London" is equally true of Manchester, Edinburgh and Cardiff.
#
# Three questions, in the order that makes the cheapest one first:
#   1. Do we already track it? Then stop - a second feed for one calendar is
#      pure duplicate work.
#   2. Does it actually have upcoming LONDON events? scrape_luma_calendar
#      already applies the timezone + city checks, so this is the real answer,
#      not a guess from the export.
#   3. Do those events belong to a category OrbitCast hosts? An organiser
#      running run clubs and brunches is a real organiser and still not ours.


def _known_luma_identifiers():
    """Everything already tracked, hardcoded or approved, lowercased.

    Two changes over the original, both aimed at the same failure - the same
    organiser tracked twice under two names:

      * scraper.tracked_luma_identifiers() resolves the username-keyed hardcoded
        entries to their usr- ids, so "SuperteamUK" and the usr- id the
        dashboard now canonicalises to are recognised as one organiser.
      * the database side reads EVERY row rather than only the active ones. A
        paused organiser is still tracked; re-adding it must reactivate that
        row, not read as "not tracked yet"."""
    try:
        known = set(scraper.tracked_luma_identifiers())
    except Exception as exc:
        log.warning(f"Could not resolve hardcoded Luma ids: {exc}")
        known = {i.lower() for i in scraper._HARDCODED_LUMA_IDS}
    try:
        import db
        known |= db.get_luma_source_identifiers()
    except Exception as exc:
        log.warning(f"Could not read tracked Luma sources: {exc}")
    return known


def _classify_organiser(name, titles):
    """Which OrbitCast category this organiser feeds, from its real event
    titles - or None when it does not belong in the catalog at all.

    The None answer matters as much as the category. Luma's London feed is full
    of genuine, well-run organisers doing run clubs, supper clubs and yoga, and
    a recommendation engine for "the right room" is not improved by any of
    them."""
    listed = "\n".join(f"- {t}" for t in titles[:10])
    if scraper.HACKATHON_RE.search(" ".join(titles)):
        return "Hackathons", "at least one event is explicitly a hackathon"
    import ai_engine
    system = (
        "You decide whether an events organiser belongs in a London event "
        "intelligence catalog, and if so under which category.\n"
        f"Categories: {', '.join(CATEGORIES)}.\n"
        "The catalog is for professional/industry rooms: defence, intelligence, "
        "security, technology, AI, research, startups, business networking, "
        "hackathons.\n"
        "It is NOT for social, fitness, wellness, hobby, nightlife, dating, "
        "religious, arts or purely consumer events - those must be refused even "
        "when the organiser is legitimate and well run.\n"
        "Return bare JSON: {\"belongs\": bool, \"category\": str|null, "
        "\"why\": str}. Judge the organiser as a whole from its events; if it is "
        "mixed, go with what most of its events are."
    )
    try:
        out = ai_engine._call(system, f"Organiser: {name}\nUpcoming events:\n{listed}",
                              max_tokens=400, model=ai_engine.MODEL_EXTRACT)
    except Exception as exc:
        log.warning(f"Organiser classification failed for {name}: {exc}")
        return None, "could not classify - refused rather than guessed"
    if not out.get("belongs"):
        return None, _first_text(out.get("why")) or "not a category OrbitCast hosts"
    cat = out.get("category")
    if cat not in CATEGORIES:
        return None, f"classified outside the catalog's categories ({cat!r})"
    return cat, _first_text(out.get("why"))


def verify_organiser(identifier: str, name: str = "", kind: str = "calendar",
                     min_events: int = 1):
    """Check one Luma organiser and say whether it should become a source.

    Returns the same shape as verify_link: `ok`, `checks`, and on success the
    row to store. Writes nothing."""
    identifier = (identifier or "").strip()
    result = {"ok": False, "identifier": identifier, "name": name, "kind": kind,
              "checks": [], "source": None, "sample_titles": [],
              "geo_summary": None,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    if not identifier:
        result["checks"].append(_check("valid identifier", False, "No calendar id given."))
        return result

    if identifier.lower() in _known_luma_identifiers():
        result["checks"].append(_check(
            "not already tracked", False,
            "Already a source - adding it again would scrape the same calendar twice."))
        return result
    result["checks"].append(_check("not already tracked", True, "Not tracked yet."))

    # The real events, London-filtered by the existing scraper.
    try:
        if kind == "user":
            events = scraper.scrape_luma_user(name or identifier, identifier)
        else:
            events = scraper.scrape_luma_calendar(name or identifier, identifier)
    except Exception as exc:
        result["checks"].append(_check("reachable", False, f"Could not read that calendar: {exc}"))
        return result

    # scrape_luma_* already applied luma_geo() and the upcoming filter, so this
    # list IS the London-or-online, still-to-happen set. The _is_future pass is
    # kept as a belt-and-braces guard rather than a second opinion.
    upcoming = [e for e in events if scraper._is_future(scraper.to_iso_date(e.get("date")))]
    titles = [e.get("title", "") for e in upcoming if e.get("title")]
    result["sample_titles"] = titles[:6]
    # Counted with the same is_london() the catalog build uses, not by
    # subtracting the online ones. The Luma guardrail admits UK venues, and the
    # London filter downstream then drops the ones that are not London - so a
    # naive "everything left is London" would promise events that never appear.
    online_n = sum(1 for e in upcoming if e.get("is_online"))
    london_n = sum(1 for e in upcoming
                   if not e.get("is_online") and scraper.is_london(e))
    elsewhere = len(upcoming) - online_n - london_n
    result["geo_summary"] = (
        f"{london_n} in London, {online_n} online"
        + (f", {elsewhere} elsewhere in the UK (those are dropped too)" if elsewhere else "")
        + ". Events this organiser runs outside London are refused at scrape "
          "time, every refresh, with nothing for you to review.")

    if len(upcoming) < min_events:
        result["checks"].append(_check(
            "has upcoming London events", False,
            f"{len(upcoming)} upcoming London or online events right now - nothing to "
            "verify it on. A quiet organiser is not a dead one, so this is worth "
            "retrying later rather than a permanent no."))
        return result
    result["checks"].append(_check(
        "has upcoming London events", True,
        f"{len(upcoming)} upcoming, already filtered to London or online by the scraper."))

    category, why = _classify_organiser(name or identifier, titles)
    if not category:
        result["checks"].append(_check(
            "category OrbitCast hosts", False, why))
        return result
    result["checks"].append(_check("category OrbitCast hosts", True, f"{category} — {why}"))

    result["source"] = {"identifier": identifier, "kind": kind,
                        "name": name or identifier, "category": category,
                        "emoji": CATEGORY_EMOJI.get(category, "🟣")}
    result["ok"] = all(c["ok"] for c in result["checks"])
    return result
