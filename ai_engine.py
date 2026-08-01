"""
OrbitCast AI - upload classification + honest CV analysis + event matching.

The whole design goal here is HONESTY: read the real person, recommend only
genuine matches, ground every claim in actual CV evidence, and be willing to
say "nothing here strongly fits you."

Three-pass pipeline, each pass one focused Claude call:
  1. extract_profile   - classify the doc, read the person (incl. trajectory)
  2. score_events       - match profile to events, honestly, with why-now
  3. critique_recommendations - drop any why-statement that isn't evidence-specific

Deps used on demand:  anthropic, pdfplumber, python-docx
"""

import io
import json
import logging
import os
import re
from datetime import date

from anthropic import Anthropic

log = logging.getLogger("orbitcast.ai")

try:
    import rag
except Exception as _exc:  # pragma: no cover - never let RAG availability break core analysis
    log.warning(f"rag module unavailable, continuing without retrieval: {_exc}")
    rag = None

MODEL = "claude-sonnet-5"             # current Sonnet model id
FIT_THRESHOLD = 65                   # only surface events scoring this or higher
MAX_RECOMMENDATIONS = 4              # honesty rule: 2-4 strong matches, not a list
MAX_CATALOG_SIZE = 100                # cap events sent to scoring - the live catalog
                                       # has grown past 200; a huge input made responses
                                       # more likely to truncate before finishing valid
                                       # JSON. Soonest-first is a reasonable priority cut.

_client = None


def _get_client():
    """Lazy client so the module imports even with no key set (route returns a
    clean 'not configured' result instead of crashing)."""
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


def _extract_json_object(raw: str) -> str:
    """Models are told to return bare JSON, but sometimes wrap it in a code
    fence or add a stray line of commentary anyway. Strip a fence if present,
    then fall back to slicing from the first '{' to the last '}' so a wrapper
    sentence doesn't sink an otherwise-valid response."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw[3:]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start:end + 1]
    return raw


def _call(system, user_content, max_tokens=1500):
    # NOTE: do NOT pass `temperature` here. It is deprecated on this model and
    # the API rejects the request outright with a 400, which takes down every
    # analysis (extract_profile included, so uploads come back as "not a CV").
    # Sampling variance near the 65 threshold has to be handled some other way.
    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    candidate = _extract_json_object(raw)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        log.warning(
            "Non-JSON model response (stop_reason=%s, %d chars): %r",
            getattr(resp, "stop_reason", None), len(raw), raw[:800],
        )
        raise


# --------------------------------------------------------------------------
# 1. Text extraction - CVs arrive as PDF / DOCX / TXT
# --------------------------------------------------------------------------
# Below this many non-whitespace characters, treat a PDF extraction as having
# failed rather than pass near-empty text to the classifier - which just
# produces a confusing "doesn't look like a CV" for what's actually an
# extraction problem (scanned image, or a font encoding pdfplumber can't map).
MIN_PDF_TEXT_CHARS = 40


def _extract_pdf_pdfplumber(file_bytes: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _extract_pdf_pypdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(file_bytes: bytes, filename: str) -> str:
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        # Two extraction libraries parse PDF internals differently (font
        # encoding, ToUnicode maps, layout reconstruction) - some CVs that
        # export cleanly from one tool extract as empty/garbled from the
        # other. Try both, keep whichever got more actual text.
        texts = []
        for extractor in (_extract_pdf_pdfplumber, _extract_pdf_pypdf):
            try:
                texts.append(extractor(file_bytes))
            except Exception as exc:
                log.warning(f"{extractor.__name__} failed: {exc}")
        best = max(texts, key=lambda t: len(t.strip())) if texts else ""
        if len(best.strip()) < MIN_PDF_TEXT_CHARS:
            raise ValueError(
                "No readable text found in this PDF - it may be a scanned image "
                "with no text layer. Try a DOCX/TXT version, or paste your CV as text."
            )
        return best

    if name.endswith(".docx"):
        import docx
        document = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in document.paragraphs)

    return file_bytes.decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------
# 2. Event format inference - the scraped sources give us title/date/category/
#    source only, nothing like speaker seniority or audience level. Rather
#    than inflate the schema with fields we'd have to invent, we derive a
#    "format" tag from the title itself - that's real signal, not fabrication.
# --------------------------------------------------------------------------
_FORMAT_PATTERNS = [
    ("hackathon",       r"\b(hackathon|buildathon|hack\s?night|hack\s?day)\b"),
    ("workshop",        r"\b(workshop|masterclass|clinic|bootcamp|training)\b"),
    ("networking",      r"\b(mixer|drinks|social|meetup|networking|happy hour)\b"),
    ("panel/summit",    r"\b(summit|panel|forum|conference|congress|expo)\b"),
    ("talk",            r"\b(talk|keynote|lecture|briefing|seminar|webinar)\b"),
]


def _infer_format(title: str) -> str:
    t = (title or "").lower()
    for label, pattern in _FORMAT_PATTERNS:
        if re.search(pattern, t):
            return label
    return "event"


# Sources scrape wildly inconsistent date strings - the live catalog carries
# ISO ("2026-08-01"), UK long ("19 August 2026"), US short ("Wed, Sep 9, 6:00
# PM"), compact ("Aug17"), ranges ("Mon 7 September 2026 - Thu 10 September
# 2026") and recurring-with-no-date ("Monday at 6:00 PM") all at once. These
# CANNOT be compared as raw strings: "20 October 2026" sorts before
# "2026-08-01" lexicographically, so a plain string sort is not a date sort at
# all. Everything date-related below goes through parse_event_date().
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _month_num(token: str):
    t = token.lower().rstrip(".")
    return _MONTHS.get(t[:4]) or _MONTHS.get(t[:3])


def _infer_year(month: int, day: int, today: date):
    """No year in the string: pick the nearest sensible one. A date that
    already passed more than ~60 days ago almost certainly means next year."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (today - candidate).days <= 60:
            return candidate
    return None


def parse_event_date(raw, today: date = None):
    """Best-effort real date from a scraped date string. Returns None for
    genuinely dateless/recurring listings ("Monday at 6:00 PM") rather than
    guessing a date that would then drive sorting and past-event filtering."""
    if not raw:
        return None
    today = today or date.today()
    s = str(raw).strip()
    if not s:
        return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)                       # 2026-08-01
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b", s)
    if m and _month_num(m.group(2)):                                       # 19 August 2026
        try:
            return date(int(m.group(3)), _month_num(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    m = re.match(r"^([A-Za-z]{3,9})\.?\s*(\d{1,2})$", s)                    # Aug17
    if m and _month_num(m.group(1)):
        return _infer_year(_month_num(m.group(1)), int(m.group(2)), today)

    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{4}))?", s)
    if m and _month_num(m.group(1)):                                       # Wed, Sep 9, 6:00 PM
        month, day = _month_num(m.group(1)), int(m.group(2))
        if m.group(3):
            try:
                return date(int(m.group(3)), month, day)
            except ValueError:
                return None
        return _infer_year(month, day, today)

    return None


def _is_past(date_str) -> bool:
    """Only exclude an event when we can confidently confirm it's already
    happened - an unparseable/recurring date passes through rather than risk
    dropping a real future event."""
    parsed = parse_event_date(date_str)
    return parsed is not None and parsed < date.today()


# Scraper artifacts that are not events at all. Kept deliberately tight -
# these pollute the catalog and waste scoring slots, but an over-broad filter
# would silently drop real events, which is the worse failure.
_JUNK_TITLE_RE = re.compile(
    r"\b(view our|follow us|subscribe|newsletter|headline sponsor|our sponsors"
    r"|sponsors \d{4}|linkedin profile|privacy policy|terms of use|cookie)\b",
    re.I,
)


def _is_junk(title: str) -> bool:
    t = (title or "").strip()
    return len(t) < 5 or bool(_JUNK_TITLE_RE.search(t))


def build_compact_events(events: list) -> list:
    """Shared shape passed to both scoring and (eventually) the frontend.

    Drops events we can confirm already happened - recommending a past event
    is the kind of thing the honesty rules exist to prevent - plus obvious
    scraper artifacts that aren't events, plus duplicates: the same event is
    routinely scraped from several sources/pages, and every duplicate both
    wastes a scoring slot and risks the same event being recommended twice."""
    compact, seen = [], set()
    for e in events:
        if _is_past(e.get("date")) or _is_junk(e.get("title", "")):
            continue
        event_id = str(e.get("id") or e.get("url") or e.get("title", ""))
        if event_id in seen:
            continue
        seen.add(event_id)
        compact.append({
            "id": event_id,
            "title": e.get("title", ""),
            "category": e.get("category", ""),
            "date": e.get("date", ""),
            "source": e.get("source", ""),
            "url": e.get("url", ""),
            "emoji": e.get("emoji", ""),
            "format": _infer_format(e.get("title", "")),
            "description": (e.get("description", "") or "")[:400],
        })
    return compact


def select_catalog(compact_events: list, cap: int = None) -> list:
    """Pick which events the scorer actually gets to see.

    Soonest-first alone starves niche categories: the live feed is dominated
    by high-volume community listings, so a straight date cut handed the model
    ~57 co-working meetups and 2 security events - which is why a security
    specialist could get zero matches out of a catalog that genuinely had
    relevant events in it, just slightly further out.

    So: sort each category by real date, then round-robin across categories.
    Every category keeps its soonest-first bias, but no single high-volume
    category can crowd the others out of the prompt."""
    cap = cap or MAX_CATALOG_SIZE
    if len(compact_events) <= cap:
        return compact_events

    by_cat = {}
    for e in compact_events:
        by_cat.setdefault(e.get("category") or "Uncategorised", []).append(e)

    for events in by_cat.values():
        # Undated events sort last - they're real, but a confirmed date is a
        # better bet for "worth your time in the near future".
        events.sort(key=lambda e: (parse_event_date(e.get("date")) is None,
                                    parse_event_date(e.get("date")) or date.max))

    selected, queues = [], list(by_cat.values())
    while len(selected) < cap and any(queues):
        for q in queues:
            if not q:
                continue
            selected.append(q.pop(0))
            if len(selected) >= cap:
                break
    return selected


# --------------------------------------------------------------------------
# 3. PASS 1 - classify + extract a structured, evidence-grounded profile
# --------------------------------------------------------------------------
PROFILE_SYSTEM_PROMPT = """You are OrbitCast's profile-reading engine. You do ONE job: \
read an uploaded document and, if it's a CV, produce an honest, evidence-grounded \
profile of the person. You do not see events or make recommendations - that's a \
separate step done by someone else.

STEP 1 - CLASSIFY the document. Decide if it is a CV / resume or something else \
(cover letter, report, random PDF, image-of-text, etc.). Be strict: a CV has a \
person's work/education history, skills, and contact-style details. If it is not \
clearly a CV, set is_cv=false and STOP - do not invent a profile.

STEP 2 - If and only if it IS a CV, read the actual person:
- Ground EVERY statement in concrete evidence from the CV. Reference the specific \
  role, project, skill, or detail you are inferring from. No generic flattery like \
  "passionate innovator." If you cannot point to evidence, do not say it.
- Give an honest read INCLUDING gaps, not just strengths. If the CV is strong on X \
  but thin on Y, say it plainly.
- Infer seniority from actual signals (years, titles, scope of responsibility) - \
  student, junior, mid, senior, or exec. Do not guess beyond what the CV supports.
- Read their TRAJECTORY, not just their history: is there a recent pivot, a newly \
  acquired skill, a stated career gap they're closing, a change in the kind of work \
  they're taking on? This should point at where they seem to be headed next, cited \
  against a specific, concrete detail in the CV (e.g. "moved from pure backend work \
  to two recent ML-adjacent projects" - not "seems ambitious"). If there's no clear \
  trajectory signal, leave it empty rather than inventing one.

Keep strengths/gaps/interests items and the trajectory read to ONE tight sentence \
each. Specific beats long.

Return ONLY valid JSON - no markdown, no backticks, no preamble - in EXACTLY this schema:
{
  "is_cv": boolean,
  "confidence": number,            // 0-1, how sure you are it is a CV
  "document_type": string,         // "cv" | "cover_letter" | "report" | "other"
  "message_if_not_cv": string,     // short friendly note shown to user if is_cv=false, else ""
  "profile": {
     "summary": string,            // honest 2-3 sentence read, evidence-grounded
     "strengths": [string],        // each tied to specific CV evidence
     "gaps": [string],             // honest and specific
     "interests": [string],        // inferred professional interests, evidence-based
     "seniority": string,          // "student" | "junior" | "mid" | "senior" | "exec"
     "trajectory": string          // where they're headed next, grounded in a specific
                                    // CV detail; empty string if no clear signal
  }
}
If is_cv is false, profile fields are empty strings/lists."""


def _empty_result(message: str) -> dict:
    return {
        "is_cv": False,
        "confidence": 0.0,
        "document_type": "unknown",
        "message_if_not_cv": message,
        "profile": {"summary": "", "strengths": [], "gaps": [], "interests": [],
                    "seniority": "", "trajectory": ""},
        "recommendations": [],
    }


def extract_profile(file_text: str) -> dict:
    user_content = f"DOCUMENT TEXT:\n{file_text[:12000]}"
    data = _call(PROFILE_SYSTEM_PROMPT, user_content, max_tokens=2000)
    data.setdefault("recommendations", [])
    return data


# --------------------------------------------------------------------------
# 4. PASS 2 - match the profile to events, honestly, with why-now reasoning
# --------------------------------------------------------------------------
# The scoring prompt is split in three so the admin dashboard can override the
# middle section (the actual judgment guidance) without touching the parts
# that keep the app itself working: the role framing and the JSON schema.
# Swapping in bad "rules" text can make recommendations worse, but it can't
# break parsing - build_scoring_prompt() always re-wraps whatever rules text
# is active in the same fixed header/footer.
SCORING_HEADER = """You are OrbitCast's event-matching engine. You receive an \
already-built profile of a person (from a separate reading step) and a catalog of \
real events. Your ONE job: score honest fit and explain it.

"""

DEFAULT_SCORING_RULES = """HONESTY RULES - this is the entire point of the product:
- Be selective. Most events will NOT be a strong fit. Return AT MOST 4 \
  recommendations - 2-4 strong ones is correct and expected, not 15. If nothing fits \
  well, return an empty list.
- Score fit honestly on 0-100. Do not inflate. 60 is a real "maybe", 90 means this \
  person should clearly go. Only include events scoring 65 or above.
- Never recommend an event that is not in the provided catalog. Use the exact \
  event_id from the catalog.
- You are told today's date below. Never recommend an event whose date has \
  already passed - if a date looks past or ambiguous, treat it with suspicion \
  rather than recommending it anyway.
- Reason about TRAJECTORY, not just topic overlap. The profile includes where this \
  person seems to be headed (a pivot, a new skill, a gap they're closing). An event \
  that matches their trajectory forward is worth more than one that just echoes their \
  existing strengths. Say so explicitly in "why_now" - what about their current \
  moment (not their whole history) makes this event timely.

Here is what separates a good "why" from a bad one - study these:

GOOD (specific, evidence-tied): "Their CV shows two recent projects fine-tuning \
open-source LLMs after four years of plain backend work - this workshop on LLM \
deployment ops is a direct extension of that pivot, not a stretch."

BAD (generic, could apply to anyone): "This event is great for someone interested \
in AI and looking to grow their network and career."

A "maybe, not a yes": if the fit is real but thin, score it in the low 65-72 range \
and say so honestly, e.g. "Their background is mostly non-technical, but they've \
started a data analytics course - this is a reasonable stretch event, not a clear \
fit. Worth attending, but not central to where they're headed."

Keep every text field to ONE tight sentence, two at most. Specific and evidence-tied \
beats long - a sharp one-sentence "why" is worth more than a paragraph."""

SCORING_FOOTER = """

Return ONLY valid JSON - no markdown, no backticks, no preamble - in EXACTLY this schema:
{
  "recommendations": [
     {
       "event_id": string,         // MUST match an id from the provided catalog
       "title": string,
       "fit_score": number,        // 0-100, honest, 65+ only
       "why": string,              // why THIS person, citing their profile evidence
       "why_now": string,          // why this event fits their trajectory right now,
                                    // not just their general topic history
       "prepare": string,          // concrete prep given their actual background
       "benefit": string           // concrete, specific payoff for them
     }
  ]
}"""

CONFIG_KEY_SCORING_RULES = "scoring_rules"


def get_scoring_rules() -> str:
    """The active rules text: an admin override from ai_config if one exists,
    otherwise the built-in default. Never raises - a DB hiccup just means the
    default rules apply, same as before this override existed."""
    try:
        import db
        custom = db.get_config(CONFIG_KEY_SCORING_RULES)
        if custom and custom.strip():
            return custom
    except Exception as exc:
        log.warning(f"get_scoring_rules: falling back to default ({exc})")
    return DEFAULT_SCORING_RULES


def build_scoring_prompt() -> str:
    return SCORING_HEADER + get_scoring_rules() + SCORING_FOOTER


def score_events(profile: dict, compact_events: list) -> list:
    # Capped candidate pool - a smaller set is both more useful to recommend
    # from and a smaller prompt, which made truncation less likely in practice
    # (confirmed via stop_reason=max_tokens on the full catalog). select_catalog
    # keeps it soonest-first *per category* so niche categories aren't starved.
    catalog = select_catalog(compact_events)

    # RAG: pull in past human-reviewed judgments for people similar to this
    # profile, if the retrieval layer is configured. Never lets a retrieval
    # failure sink the analysis - falls straight back to the static few-shot
    # examples already baked into the active scoring rules block.
    rag_block = ""
    if rag is not None:
        try:
            rag_block = rag.format_examples_for_prompt(rag.retrieve_examples(profile))
        except Exception as exc:
            log.warning(f"RAG retrieval failed, continuing without it: {exc}")

    user_content = (
        f"TODAY'S DATE: {date.today().isoformat()}\n\n"
        "PERSON PROFILE (JSON):\n"
        f"{json.dumps(profile, ensure_ascii=False)}\n\n"
        "EVENT CATALOG (JSON array):\n"
        f"{json.dumps(catalog, ensure_ascii=False)}"
        + (f"\n\n{rag_block}" if rag_block else "")
    )
    data = _call(build_scoring_prompt(), user_content, max_tokens=4096)
    valid_ids = {e["id"] for e in catalog}
    recs = [
        r for r in data.get("recommendations", [])
        if isinstance(r, dict)
        and r.get("event_id") in valid_ids
        and r.get("fit_score", 0) >= FIT_THRESHOLD
    ]
    return sorted(recs, key=lambda r: r.get("fit_score", 0), reverse=True)


# --------------------------------------------------------------------------
# 5. PASS 3 - self-critique: catch drift back toward generic flattery
# --------------------------------------------------------------------------
CRITIQUE_SYSTEM_PROMPT = """You are OrbitCast's honesty auditor. You receive a \
person's profile and a list of event recommendations someone else produced for \
them. Your ONE job: check each recommendation's "why" and "why_now" text against \
one question - does it cite specific evidence from the profile, or could it apply \
to almost anyone with a passing interest in the topic?

Examples of what to REJECT (too generic, keep_it=false):
- "Great for expanding your network and knowledge in this space."
- "This aligns with your interests and will help your career."
- Anything that doesn't name a specific detail from the profile (a role, project,
  skill, or the stated trajectory).

Examples of what to KEEP (keep_it=true):
- Text that names a specific project, role, skill, or trajectory detail from the
  profile and ties it to why this specific event matters now.

Do not rewrite good text. Do not soften scores. Only decide keep/reject per item.

Return ONLY valid JSON - no markdown, no backticks, no preamble - in EXACTLY this schema:
{
  "reviewed": [
    { "event_id": string, "keep_it": boolean }
  ]
}"""


def critique_recommendations(profile: dict, recommendations: list) -> list:
    if not recommendations:
        return recommendations
    user_content = (
        "PERSON PROFILE (JSON):\n"
        f"{json.dumps(profile, ensure_ascii=False)}\n\n"
        "RECOMMENDATIONS TO AUDIT (JSON array):\n"
        f"{json.dumps(recommendations, ensure_ascii=False)}"
    )
    try:
        data = _call(CRITIQUE_SYSTEM_PROMPT, user_content, max_tokens=1000)
    except Exception as exc:
        # Critique failing shouldn't sink the whole analysis - fall back to
        # the unaudited (but already threshold-enforced) recommendations.
        log.warning(f"Critique pass failed, keeping unaudited recommendations: {exc}")
        return recommendations

    keep_ids = {
        r["event_id"] for r in data.get("reviewed", [])
        if isinstance(r, dict) and r.get("keep_it")
    }
    return [r for r in recommendations if r.get("event_id") in keep_ids]


# --------------------------------------------------------------------------
# 6. Orchestration - the single entry point the route calls
# --------------------------------------------------------------------------
def analyze_upload(file_text: str, events: list) -> dict:
    if not file_text or not file_text.strip():
        return _empty_result("That file looks empty - try a PDF, DOCX, or TXT CV.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _empty_result("AI analysis isn't configured yet (no API key set).")

    try:
        data = extract_profile(file_text)
    except json.JSONDecodeError:
        return _empty_result("I couldn't read that file clearly. Try a PDF or DOCX CV.")
    except Exception as exc:  # network / API failure - never 500 the route
        log.warning(f"extract_profile failed: {exc}")
        return _empty_result(f"Analysis is temporarily unavailable. ({exc})")

    if not data.get("is_cv"):
        data["recommendations"] = []
        return data

    compact_events = build_compact_events(events)

    # Scoring is a model call and can fail transiently (truncated/invalid JSON,
    # API hiccup). Retry once before giving up - measured ~1 in 4 analyses
    # returning zero purely from a transient failure, on a profile whose top
    # match otherwise scored 87-90 consistently.
    #
    # And critically: DO NOT let a failure masquerade as "no matches". The
    # frontend tells the user "that's an honest result, not a failure", which
    # is a lie if scoring never actually completed. scoring_failed makes the
    # difference explicit so the UI can say "try again" instead.
    recs, scoring_failed = [], False
    for attempt in (1, 2):
        try:
            recs = score_events(data["profile"], compact_events)
            recs = critique_recommendations(data["profile"], recs)
            scoring_failed = False
            break
        except Exception as exc:
            scoring_failed = True
            log.warning(f"score_events failed (attempt {attempt}/2): {exc}")
    if scoring_failed:
        log.warning("scoring failed twice - reporting as failure, not as 'no matches'")
        recs = []
    data["scoring_failed"] = scoring_failed

    # Enrich with the real event object and cap at the honesty-rule max.
    by_id = {e["id"]: e for e in compact_events}
    enriched = []
    for r in recs[:MAX_RECOMMENDATIONS]:
        ev = by_id.get(r["event_id"], {})
        enriched.append({**r, "category": ev.get("category", ""),
                          "date": ev.get("date", ""), "source": ev.get("source", ""),
                          "url": ev.get("url", ""), "emoji": ev.get("emoji", ""),
                          "format": ev.get("format", "")})

    data["recommendations"] = enriched
    return data
