"""
OrbitCast AI - input classification + honest profile reading + event matching.

The whole design goal here is HONESTY: read the real person, recommend only
genuine matches, ground every claim in what they actually gave us, and be
willing to say "nothing here strongly fits you."

Input is a CV OR a self-description in their own words, however short. Both are
first-class - "I'm a 21 year old psychology student who wants to get into
intelligence" is the single most common thing a real user types, and it is a
question, not a malformed CV. Only genuinely person-free input stops at the gate.

Pipeline, each pass one focused Claude call:
  1. extract_profile          - classify the input, read the person, and (when the
                                input is thin) draft the questions worth asking back
  2. build_orientation        - ONLY for thin inputs: what the field is, what's
                                honestly true about breaking in right now (grounded
                                with web search), and what events do and don't do
  3. score_events             - match profile to events, honestly, with why-now
  4. critique_recommendations - drop any why-statement that isn't evidence-specific

Deps used on demand:  anthropic, pdfplumber, python-docx
"""

import io
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from anthropic import Anthropic

log = logging.getLogger("orbitcast.ai")

try:
    import rag
except Exception as _exc:  # pragma: no cover - never let RAG availability break core analysis
    log.warning(f"rag module unavailable, continuing without retrieval: {_exc}")
    rag = None

# Models are env-switchable so cost experiments never need a code change.
# Scoring and critique are the two passes that decide what "honest" means -
# if you move them to a cheaper model, watch the admin honesty stats and the
# critique rejection rate for a few days before trusting it.
MODEL = os.environ.get("OC_MODEL_SCORE", "claude-sonnet-5")
# Response-time fix: a normal analysis is 3 sequential Claude calls -
# extract_profile -> score_events -> critique_recommendations - each a full
# round trip plus real generation time, because scoring needs the extracted
# profile and critique needs the scored recs. They can't run in parallel.
# extract_profile is the one call in that chain that's closer to structured
# extraction than open-ended judgment (classify + pull out fields against an
# explicit, fully-specified schema) - claude-haiku-4-5 handles that reliably
# and is meaningfully faster, so it's the one call moved off Sonnet. The two
# calls that actually decide what's "honest" - scoring and the self-critique
# that catches drift back toward flattery - stay on Sonnet untouched. If
# extraction quality ever looks off, move MODEL_EXTRACT back to MODEL; it's
# a one-line revert.
MODEL_EXTRACT = os.environ.get("OC_MODEL_EXTRACT", "claude-haiku-4-5-20251001")
MODEL_CRITIQUE = os.environ.get("OC_MODEL_CRITIQUE", "") or None   # default: MODEL
FIT_THRESHOLD = 65                   # only surface events scoring this or higher
MAX_RECOMMENDATIONS = 5              # honesty rule: a few strong matches, never a padded list
MAX_CATALOG_SIZE = 100                # cap events sent to scoring - the live catalog
                                       # has grown past 200; a huge input made responses
                                       # more likely to truncate before finishing valid
                                       # JSON. Soonest-first is a reasonable priority cut.

_client = None
_client_lock = threading.Lock()

# Rough running usage counters, surfaced on /api/health for the admin - the
# fastest way to see whether prompt caching is actually hitting (cache_read
# should dwarf uncached input once more than one analysis has run per scrape).
USAGE = {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
_usage_lock = threading.Lock()


def _note_usage(resp):
    try:
        u = resp.usage
        with _usage_lock:
            USAGE["calls"] += 1
            USAGE["input"] += getattr(u, "input_tokens", 0) or 0
            USAGE["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            USAGE["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            USAGE["output"] += getattr(u, "output_tokens", 0) or 0
    except Exception:
        pass

# ── Load limits for the Claude API ───────────────────────────────────────────
# Every model call in the app goes through _call() or _call_with_search(), and
# both pass through this gate: at most AI_MAX_INFLIGHT requests to Anthropic at
# once from this process - analyses, re-runs, the digest scheduler and admin
# ingest all share it. Past that, callers queue here instead of piling onto the
# API and getting 429s back. Raise it if your Anthropic rate-limit tier allows.
AI_MAX_INFLIGHT = max(1, int(os.environ.get("AI_MAX_INFLIGHT", "32")))
# Per-request ceiling. The SDK default is 10 minutes, which let one stuck call
# hold a worker that long. Web-search orientation is the slowest call (60-90s).
AI_TIMEOUT_S = float(os.environ.get("AI_TIMEOUT_S", "120"))
# The SDK retries 429 / 529 / connection errors with backoff and honours the
# API's retry-after. Four tries rides out a short rate-limit spike.
AI_MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "4"))
_ai_slots = threading.BoundedSemaphore(AI_MAX_INFLIGHT)


def _get_client():
    """Lazy client so the module imports even with no key set (route returns a
    clean 'not configured' result instead of crashing)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"),
                                    timeout=AI_TIMEOUT_S, max_retries=AI_MAX_RETRIES)
    return _client


# ── Outage breaker ───────────────────────────────────────────────────────────
# When the API refuses every call for an account-level reason - no credit, a
# revoked key, a permission problem - nothing in this process can fix it, and
# every analysis, digest and details pass was still making the call (twice,
# with retries) and failing the same way. The first such refusal now opens
# the breaker: for OUTAGE_PAUSE_S every call fails at once, in-process, with
# the real reason, and /api/health reports it so somebody can act on it.
# Transient errors (429, 5xx, timeouts) are not account problems and leave
# the breaker alone - the SDK's own retries handle those.
OUTAGE_PAUSE_S = float(os.environ.get("AI_OUTAGE_PAUSE_S", "600"))
_outage = {"until": 0.0, "reason": None, "since": None, "count": 0}
_outage_lock = threading.Lock()
_ACCOUNT_ERROR_MARKERS = ("credit balance", "billing", "invalid x-api-key",
                          "authentication_error", "permission_error", "api key")


class AIUnavailable(RuntimeError):
    """Raised instead of calling the API while the breaker is open."""


def outage():
    """{"reason", "since", "until"} while the breaker is open, else None."""
    with _outage_lock:
        if _outage["until"] > time.time():
            return {"reason": _outage["reason"], "since": _outage["since"],
                    "until": _outage["until"], "count": _outage["count"]}
    return None


def _note_account_error(exc):
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if not (status in (400, 401, 403) and any(m in msg for m in _ACCOUNT_ERROR_MARKERS)):
        return
    with _outage_lock:
        first = _outage["until"] <= time.time()
        _outage["until"] = time.time() + OUTAGE_PAUSE_S
        _outage["reason"] = str(exc)[:300]
        _outage["since"] = _outage["since"] if not first and _outage["since"] else time.time()
        _outage["count"] += 1
    if first:
        log.error(f"AI calls paused for {OUTAGE_PAUSE_S / 60:.0f} min - the API is refusing "
                  f"this account: {str(exc)[:200]}")


def _create(**kwargs):
    """messages.create behind the in-flight gate and the outage breaker."""
    o = outage()
    if o:
        raise AIUnavailable(f"AI paused: {o['reason']}")
    with _ai_slots:
        try:
            return _get_client().messages.create(**kwargs)
        except Exception as exc:
            _note_account_error(exc)
            raise


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


def _call(system, user_content, max_tokens=1500, model=None):
    """system and user_content may be plain strings or content-block lists
    (the block form is how score_events marks the shared catalog prefix with
    cache_control, so a hundred people analysed in the same window pay for
    those input tokens roughly once instead of a hundred times)."""
    # NOTE: do NOT pass `temperature` here. It is deprecated on this model and
    # the API rejects the request outright with a 400, which takes down every
    # analysis (extract_profile included, so uploads come back as "not a CV").
    # Sampling variance near the 65 threshold has to be handled some other way.
    resp = _create(
        model=model or MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    _note_usage(resp)
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


# A CV is a few pages. These caps exist because this runs on the single
# Gunicorn worker that serves everyone: a 5 MB PDF with ten thousand pages,
# or a 5 MB DOCX that inflates to gigabytes, used to pin that worker until
# Gunicorn killed it - every user got 502s and the in-memory catalogue was
# lost. Anything past the caps is simply not read.
MAX_PDF_PAGES = 30
MAX_DOCX_INFLATED_BYTES = 40 * 1024 * 1024
EXTRACT_TIMEOUT_S = 25


def _extract_pdf_pdfplumber(file_bytes: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages[:MAX_PDF_PAGES])


def _extract_pdf_pypdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])


def _check_docx_size(file_bytes: bytes):
    """Refuse a DOCX whose declared uncompressed size is absurd for a CV
    before python-docx inflates it."""
    import zipfile
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_DOCX_INFLATED_BYTES or len(z.infolist()) > 2000:
            raise ValueError("That document is far larger inside than a CV should be. "
                             "Try a PDF or paste the text.")


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Runs the extractor under a hard time limit. The thread cannot be killed
    if it overruns, but the request returns and the limiter still applies;
    the leaked thread ends when the parser does. ValueError messages are
    user-facing and surface unchanged."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_extract_text_raw, file_bytes, filename)
        try:
            text = fut.result(timeout=EXTRACT_TIMEOUT_S)
        except _Timeout:
            raise ValueError("That file took too long to read. Try a smaller PDF, "
                             "or paste your CV as text.")
    finally:
        ex.shutdown(wait=False)
    return _strip_nul(text)


def _strip_nul(text: str) -> str:
    """Postgres text columns cannot store a literal NUL (0x00) byte at all -
    not "won't render it", the insert itself raises. Some PDF extractions
    (font/encoding quirks) and, less often, DOCX or plain-text files can
    carry one through. Caught live: the analysis itself succeeded end to
    end (3 real Claude calls, a real result shown to the user) and then
    silently failed to save, because this was missing at the one place
    that would have caught it before the text ever reached the database."""
    return text.replace("\x00", "") if text else text


def _extract_text_raw(file_bytes: bytes, filename: str) -> str:
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
        _check_docx_size(file_bytes)
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
        # Listings the catalogue flagged as off-topic (nightlife, sport, job
        # adverts, forms) never reach the scorer: a 70 for a padel social is
        # not a match anyone wants, and it costs a slot a real one could use.
        if e.get("offtopic"):
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
            "format": e.get("format") or _infer_format(e.get("title", "")),
            "description": (e.get("description", "") or "")[:400],
            "location": e.get("location", ""),
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
read what a person gave us and produce an honest, evidence-grounded profile of them. \
You do not see events or make recommendations - that's a separate step done by someone else.

STEP 1 - CLASSIFY the input into exactly one of three kinds:

- "cv": a CV / resume. Work or education history, skills, contact-style details.

- "self_description": the person describing THEMSELVES or their situation in their own \
  words, at any length. This includes a single sentence. "I am a 21 year old college \
  student studying psychology and I wanna get into intelligence" is a self_description \
  and is a completely valid, extremely common way to use this product. So is "founder \
  looking for co-founders", "career changer moving from teaching into data", or a bare \
  "cybersecurity, London, junior". If there is a real person with a real situation, \
  direction, or interest in the text, it is a self_description - even if it is short, \
  informal, misspelled, or contains almost no detail.

- "unusable": there is no person in it at all. A recipe, a news article, a product \
  manual, a legal contract, random characters, an empty extraction. This is the ONLY \
  kind that stops here.

Do NOT classify something as "unusable" because it is short, thin, or not a CV. \
Thin is normal. Thin is what most people type. Thin is handled at STEP 2 by saying \
so honestly and asking questions - not by refusing.

STEP 2 - Read the actual person:
- Ground EVERY statement in what they actually gave you. Reference the specific role, \
  project, skill, course, or stated goal you are inferring from. No generic flattery \
  like "passionate innovator." If you cannot point to something they said, do not say it.
- Give an honest read INCLUDING gaps. For a thin self_description the biggest honest \
  gap is usually the thinness itself - say plainly what you do not know about them \
  rather than filling it in. "No stated technical background" is an honest gap; \
  inventing one is not.
- Infer seniority from actual signals. For a student who says they are a student, \
  "student" is the answer - that is a fact they gave you, not a guess.
- Read their TRAJECTORY - where they seem to be headed. For a CV this is a pivot or a \
  newly acquired skill cited against a concrete detail. For a self_description their \
  stated ambition IS the trajectory, and you should treat it as real: "wants to move \
  from psychology into intelligence work" is a trajectory, not a guess.

STEP 3 - Set "evidence_level":
- "rich": you have enough to judge this person's fit properly (a CV, or a detailed \
  self-description with concrete background).
- "thin": you are working mostly from a stated ambition with little or no track record. \
  This is not a failure state. It changes how the match should be framed downstream, \
  and it is what triggers the orientation and follow-up questions.

STEP 4 - If evidence_level is "thin", write "clarifying_questions": 2-3 questions whose \
answers would MOST change which events we recommend. Ask like a knowledgeable person \
would in conversation - specific, not a form. Each question gets 3-4 short suggested \
answers the person can pick from, plus they can always write their own. Ask about things \
that genuinely change the recommendation (what draws them to the field, what they've \
already done, what they want out of the next six months, technical vs policy leaning). \
Do NOT ask for their name, email, or anything we do not use. If evidence_level is \
"rich", return an empty list - do not manufacture questions.

Keep strengths/gaps/interests items and the trajectory read to ONE tight sentence \
each. Specific beats long.

Return ONLY valid JSON - no markdown, no backticks, no preamble - in EXACTLY this schema:
{
  "input_kind": string,            // "cv" | "self_description" | "unusable"
  "person_name": string,           // the person's own name as the document states it
                                    // (from the CV header / contact block, or "my name
                                    // is X" in a self-description). "" if no name is
                                    // given. NEVER invent or guess a name; a company or
                                    // university name is not a person's name.
  "confidence": number,            // 0-1, how sure you are of the classification
  "document_type": string,         // "cv" | "self_description" | "other"
  "message_if_not_cv": string,     // short friendly note, ONLY if input_kind="unusable", else ""
  "evidence_level": string,        // "rich" | "thin"
  "field": string,                 // the industry/field they are aiming at, in plain words
                                    // (e.g. "intelligence and national security analysis").
                                    // Empty string if genuinely unclear.
  "clarifying_questions": [
     { "question": string, "options": [string] }   // 3-4 short options each
  ],
  "profile": {
     "summary": string,            // honest 2-3 sentence read, grounded in what they said
     "strengths": [string],        // each tied to something specific they told us
     "gaps": [string],             // honest and specific, including "we don't know X yet"
     "interests": [string],        // stated or clearly implied professional interests
     "seniority": string,          // "student" | "junior" | "mid" | "senior" | "exec"
     "aspiration": string,         // what they are trying to move toward, in their terms;
                                    // empty string if they didn't state a direction
     "trajectory": string          // where they're headed next, grounded in a specific
                                    // detail; empty string if no clear signal
  }
}
If input_kind is "unusable", profile fields are empty strings/lists."""


def _empty_result(message: str, failed: bool = False) -> dict:
    """`failed=True` means WE broke, not that the document isn't a CV. The two
    must stay distinguishable: rendering a transient API error as "this doesn't
    look like a CV" tells the user their perfectly good CV is unreadable, which
    is both false and the single most damaging thing this product can say."""
    return {
        "is_cv": False,
        "input_kind": "unusable",
        "evidence_level": "thin",
        "field": "",
        "clarifying_questions": [],
        "orientation": None,
        "confidence": 0.0,
        "document_type": "unknown",
        "message_if_not_cv": message,
        "person_name": "",
        "analysis_failed": failed,
        "profile": {"summary": "", "strengths": [], "gaps": [], "interests": [],
                    "seniority": "", "aspiration": "", "trajectory": ""},
        "recommendations": [],
    }


def extract_profile(file_text: str) -> dict:
    user_content = f"WHAT THE PERSON GAVE US:\n{file_text[:12000]}"
    data = _call(PROFILE_SYSTEM_PROMPT, user_content, max_tokens=2000, model=MODEL_EXTRACT)

    # `is_cv` is the flag the rest of the app and the frontend already branch on.
    # It now means "we have a usable person", not literally "this file is a CV" -
    # a one-line self-description is a valid input, not a rejection. Only
    # input_kind="unusable" closes the gate.
    kind = data.get("input_kind") or ("cv" if data.get("is_cv") else "unusable")
    data["input_kind"] = kind
    data["is_cv"] = kind in ("cv", "self_description")
    data.setdefault("evidence_level", "rich" if kind == "cv" else "thin")
    data.setdefault("field", "")
    data["person_name"] = str(data.get("person_name") or "").strip()[:120]
    data.setdefault("clarifying_questions", [])
    data.setdefault("recommendations", [])
    data.setdefault("profile", {})
    data["profile"].setdefault("aspiration", "")

    # Guard the questions shape - the frontend renders these directly.
    qs = []
    for q in data.get("clarifying_questions") or []:
        if isinstance(q, dict) and (q.get("question") or "").strip():
            opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
            qs.append({"question": q["question"].strip(), "options": opts[:4]})
    data["clarifying_questions"] = qs[:3]
    return data


# --------------------------------------------------------------------------
# 3b. Orientation - for someone with an ambition and no track record yet
# --------------------------------------------------------------------------
# A thin profile ("21, psychology student, wants to get into intelligence") is the
# most common input this product gets, and a list of four events answers a question
# that person has not asked yet. Before the matches, they need to know what the
# field actually is, what's honestly true about getting into it, and what an event
# does and doesn't do for them. That last part matters: events are a weak lever
# compared to a clearance, a language, or a degree, and saying otherwise sells them
# something. This runs Claude with web search so "currently true" means currently
# true, not true as of the training cutoff.
ORIENTATION_SYSTEM_PROMPT = """You are OrbitCast's orientation writer. Someone has told \
us they want to get into a field and they do not yet have a track record in it. Before \
we show them events, you tell them the truth about the field as it stands RIGHT NOW.

You have web search. USE IT - run 2-4 searches for current hiring conditions, entry \
routes, and recent developments in this field, weighted to the UK/London where the \
field is location-dependent. Your knowledge of "the current state" of an industry goes \
stale; search is how you fix that. Prefer sources from the last 12 months.

Write three things:

1. "field_reality" - what this industry actually is and what the work actually looks \
like day to day. Correct the popular image of it if the popular image is wrong (it \
usually is). 3-4 sentences.

2. "honest_truths" - 3-4 separate, specific, uncomfortable-if-necessary truths about \
getting in, as it stands now. These must be CONCRETE and CURRENT: actual entry routes, \
actual gatekeeping requirements (citizenship, clearance, language, a specific degree), \
actual timelines, actual competition, what the market is doing this year. Anchor them \
in what you found. Do not write encouragement. Do not write "it's competitive but with \
passion anything is possible" - that is filler and it helps nobody. If the honest \
answer is "this route effectively requires something they do not have", say that, and \
say what the realistic alternative route is.

3. "what_events_do" - what attending events in this space will and will not do for \
them, specifically. Be straight about the ceiling: events build familiarity with the \
language of a field, surface people who are already in it, and occasionally produce a \
referral. They do not substitute for the actual gate (a clearance, a qualification, a \
technical skill). Name what they should be doing ALONGSIDE events, given where they \
are. 3-4 sentences.

Write in plain, direct, second-person prose ("you"). No bullet-point voice, no hype, \
no hedging into meaninglessness. Assume the reader is smart and would rather hear it \
straight.

Return ONLY valid JSON - no markdown, no backticks, no preamble - in EXACTLY this schema:
{
  "field_name": string,            // the field, named plainly
  "field_reality": string,
  "honest_truths": [string],       // 3-4 items, each one specific sentence or two
  "what_events_do": string,
  "sources": [                     // the pages you actually used, 2-4 of them
     { "title": string, "url": string }
  ]
}"""

# Web search is slow and costs money, and the honest truths about an industry do not
# change hour to hour. Cache per field for the day - a Railway dyno holds this fine,
# and a cold start just means one more search.
_ORIENTATION_TTL = 12 * 3600
# A failed orientation is remembered briefly too: when fifty people in the same
# field arrive together and search is down, they should not each wait 90s for
# the same failure in turn.
_ORIENTATION_FAIL_TTL = 10 * 60
_orientation_cache: dict = {}
# One in-flight search per field. The first person in a field runs the web
# search; anyone else in that field who arrives meanwhile waits for it and
# reuses the answer, instead of firing an identical 60-90s search each.
_orientation_locks: dict = {}
_orientation_locks_guard = threading.Lock()


def _orientation_lock(key):
    with _orientation_locks_guard:
        lock = _orientation_locks.get(key)
        if lock is None:
            lock = _orientation_locks[key] = threading.Lock()
        return lock


def _orientation_cached(key):
    hit = _orientation_cache.get(key)
    if not hit:
        return False, None
    ttl = _ORIENTATION_TTL if hit["data"] is not None else _ORIENTATION_FAIL_TTL
    if (time.time() - hit["at"]) < ttl:
        return True, hit["data"]
    return False, None


def _call_with_search(system: str, user_content: str, max_tokens: int = 8000) -> dict:
    """Same contract as _call(), but with the server-side web_search tool enabled.
    The tool runs on Anthropic's side; we only have to keep re-sending when the
    server pauses a long tool turn.

    max_tokens defaults much higher than a plain _call() because each search's
    result content counts against it same as any other output - at 3000 the
    budget was consumed entirely by 2-4 searches' worth of result blocks,
    leaving zero room for the model to actually write the JSON answer. That
    produced an empty text block and a confusing "Expecting value" JSON error
    that masked the real cause (verified live: happened on every orientation
    call in production)."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    messages = [{"role": "user", "content": user_content}]

    resp = None
    for _ in range(4):  # bounded: a paused turn resumes, it doesn't loop forever
        resp = _create(
            model=MODEL, max_tokens=max_tokens, system=system,
            tools=tools, messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        # Re-send the paused assistant turn verbatim; the server resumes it.
        messages = [{"role": "user", "content": user_content},
                    {"role": "assistant", "content": resp.content}]

    raw = "".join(b.text for b in resp.content
                  if getattr(b, "type", "") == "text").strip()
    if not raw:
        raise ValueError(
            f"orientation call produced no text (stop_reason={getattr(resp, 'stop_reason', None)}, "
            f"{len(resp.content) if resp else 0} content blocks) - likely ran out of "
            f"max_tokens on search results before writing the answer")
    return json.loads(_extract_json_object(raw))


def build_orientation(field: str, profile: dict) -> dict:
    """None on any failure - orientation is an addition to the answer, never a
    precondition for it. If search or the model falls over, the person still gets
    their profile and their matches."""
    key = (field or "").strip().lower()[:80]
    if not key:
        return None

    found, data = _orientation_cached(key)
    if found:
        return data
    with _orientation_lock(key):
        found, data = _orientation_cached(key)      # filled while we waited
        if found:
            return data
        data = _build_orientation_uncached(field, profile)
        _orientation_cache[key] = {"at": time.time(), "data": data}
        return data


def _build_orientation_uncached(field: str, profile: dict):
    user_content = (
        f"TODAY'S DATE: {date.today().isoformat()}\n"
        f"FIELD THEY WANT TO GET INTO: {field}\n\n"
        "WHO THEY ARE (JSON):\n"
        f"{json.dumps(profile, ensure_ascii=False)}\n\n"
        "Search for the current state of this field, then write their orientation."
    )
    try:
        data = _call_with_search(ORIENTATION_SYSTEM_PROMPT, user_content)
    except Exception as exc:
        log.warning(f"Orientation pass failed, continuing without it: {exc}")
        return None

    if not isinstance(data, dict) or not data.get("field_reality"):
        return None
    data["honest_truths"] = [str(t) for t in (data.get("honest_truths") or [])][:4]
    data["sources"] = [
        s for s in (data.get("sources") or [])
        if isinstance(s, dict) and s.get("url")
    ][:4]
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
- Be selective. Most events will NOT be a strong fit. Return AT MOST 5 \
  recommendations - 2-5 strong ones is correct and expected, not 15. Never pad \
  towards five: four strong and one weak is worse than four. If nothing fits \
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

Every recommendation also gets a "weakness" field - ONE sentence naming the single \
most honest limitation of this match for THIS person. Not a disclaimer, not a \
hedge, not "it might be busy" - a specific thing about THIS event vs THIS person \
that a thoughtful friend would mention when recommending it. Examples:
GOOD: "The speaker lineup is heavily US-focused, so London regulatory nuance may \
be thin on the ground." / "This is an advanced-practitioner room; you'll follow \
~60% without the two years of SOC experience most attendees have." / "The venue \
is in Zone 4 and the event ends at 10pm - the commute back central is a factor."
BAD: "You might not know anyone there." / "It could be useful." / Anything that \
could apply to any event. If you genuinely can't name a specific weakness for \
this match, write "None material." - do NOT invent one.

WHEN THE PROFILE SAYS evidence_level IS "thin":
This person told us where they want to go and has little or no track record there \
yet - a student, a career changer, someone starting out. They are a real user with a \
real question, not a bad input. Two failure modes to avoid, in both directions:
- Do NOT score everything low just because they have no CV. The question you are \
  answering for them is different: "would being in this room move this person toward \
  the direction they stated?" A genuinely entry-accessible event in their target \
  field is a strong match for them even though they have no experience in it.
- Do NOT inflate. Score an advanced practitioner event low for a beginner and say \
  why - being in a room you cannot follow is worth less than being in one you can. \
  Prefer events that are introductory, community-run, open to outsiders, or \
  adjacent-and-learnable over expert-level or invite-shaped ones.
Ground "why" in what they actually stated (their field of study, their stated goal, \
their age/stage) rather than a track record they do not have, and say plainly in \
"why_now" what this specific room gets them at their specific starting point.

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
       "benefit": string,          // concrete, specific payoff for them
       "weakness": string          // the ONE honest limit of this match for them,
                                    // or "None material." - never invented, never generic
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


# Band label is a single source of truth for every surface that renders a
# match. The UI never does the mapping itself: an upgrade here (new band,
# different cut points, localised strings) deploys to the card, the deck,
# the digest email and the admin console in one step. Only the integer
# scores above FIT_THRESHOLD (65) ever reach a card at all; anything lower
# was dropped before we got here.
def band_label(score) -> str:
    try:
        s = int(score or 0)
    except (TypeError, ValueError):
        return "Worth a look"
    if s >= 85:
        return "Strong fit"
    if s >= 75:
        return "Good match"
    return "Worth a look"       # 65-74


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

    # Prompt caching: the catalog is identical for everyone between scrapes
    # and it dwarfs the profile (20k+ tokens vs ~1k), so it goes FIRST, in a
    # block marked cache_control, with the per-person part after it. The first
    # analysis after a scrape writes the cache (+25% on those tokens, once);
    # every analysis in the next few minutes reads it at a tenth of the price.
    # The system prompt is cached the same way. Order matters: anything that
    # varies per user must come after the marked blocks or nothing caches.
    system_blocks = [{"type": "text", "text": build_scoring_prompt(),
                      "cache_control": {"type": "ephemeral"}}]
    shared = (
        f"TODAY'S DATE: {date.today().isoformat()}\n\n"
        "EVENT CATALOG (JSON array):\n"
        f"{json.dumps(catalog, ensure_ascii=False)}"
    )
    personal = (
        "PERSON PROFILE (JSON):\n"
        f"{json.dumps(profile, ensure_ascii=False)}"
        + (f"\n\n{rag_block}" if rag_block else "")
    )
    user_content = [
        {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": personal},
    ]
    data = _call(system_blocks, user_content, max_tokens=4096)
    valid_ids = {e["id"] for e in catalog}
    # Stamp a band derived from the raw score. The server owns this mapping so
    # the UI never has to - every surface (card, deck, digest email, API)
    # agrees on the label and a prompt-nudge can't produce a 85 called
    # "worth a look". Bands are the primary read; the integer remains in the
    # payload for the few surfaces that still want it (admin, export).
    for r in data.get("recommendations", []):
        if isinstance(r, dict):
            r["band"] = band_label(r.get("fit_score"))
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

IF the profile says evidence_level is "thin", judge against what that person actually
told us - their field of study, their stated goal, their stage - because that is the
whole evidence base that exists. "A psychology undergraduate with no security
background gets an accessible first look at how the field talks about itself" is
SPECIFIC and passes. Do not reject a recommendation for failing to cite a career
history the person never claimed to have - that would silently return nothing to
exactly the users who most need an answer. Still reject text that could apply to
anyone regardless of what they said.

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
        data = _call(CRITIQUE_SYSTEM_PROMPT, user_content, max_tokens=1000,
                     model=MODEL_CRITIQUE)
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
# 5b. Event geocoding + distance - free, keyless: OpenStreetMap's Nominatim
# for text -> lat/lng, plain Haversine for distance. No Google Maps API key,
# no billing account, nothing for the operator to configure. A "get
# directions" link still deep-links to Google Maps for real turn-by-turn
# routing - that needs no API key either, it's just a URL scheme.
# --------------------------------------------------------------------------
_GEOCODE_CACHE: dict = {}          # location text -> {lat,lng} | None (negative-cached too)
_NOMINATIM_LAST_CALL = [0.0]       # single-element list = mutable closure cell
_NOMINATIM_MIN_INTERVAL = 1.05     # Nominatim's usage policy: max 1 req/sec
# The spacing above was not thread-safe: with many analyses finishing at once,
# every thread read the same "last call" time and they all fired together -
# exactly the burst that gets an IP blocked by Nominatim. One lookup at a time.
_NOMINATIM_LOCK = threading.Lock()


def geocode_location(location_text: str):
    """Text -> {"lat":.., "lng":..} via OpenStreetMap Nominatim, or None if
    it can't be resolved or the lookup fails. Cached in-memory per unique
    location string - event locations are a small, slow-changing set (a few
    dozen venues), so this rarely makes a real network call after warmup.
    Never raises: geocoding is a nice-to-have (map + distance), not
    something that should be able to break a recommendation."""
    text = (location_text or "").strip()
    if not text or text.lower() == "london":
        return None  # too vague to place a pin - "London" alone isn't a venue
    if text in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[text]
    # Bounded wait: a map pin is not worth holding an analysis for long.
    if not _NOMINATIM_LOCK.acquire(timeout=8):
        return None
    try:
        if text in _GEOCODE_CACHE:              # resolved while we waited
            return _GEOCODE_CACHE[text]
        return _geocode_uncached(text)
    finally:
        _NOMINATIM_LOCK.release()


def _geocode_uncached(text: str):
    elapsed = time.time() - _NOMINATIM_LAST_CALL[0]
    if elapsed < _NOMINATIM_MIN_INTERVAL:
        time.sleep(_NOMINATIM_MIN_INTERVAL - elapsed)
    _NOMINATIM_LAST_CALL[0] = time.time()

    result = None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": text, "limit": 1, "countrycodes": "gb"},
            # Nominatim's usage policy requires an identifying User-Agent -
            # an unlabelled default requests UA gets blocked.
            headers={"User-Agent": "OrbitCastAI/1.0 (event recommendation app)"},
            timeout=5,
        )
        rows = resp.json()
        if rows:
            result = {"lat": float(rows[0]["lat"]), "lng": float(rows[0]["lon"])}
    except Exception as exc:
        log.warning(f"geocode_location failed for {text!r}: {exc}")

    _GEOCODE_CACHE[text] = result
    return result


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0  # Earth radius, km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def directions_url(origin_lat, origin_lng, dest_lat, dest_lng) -> str:
    """A plain Google Maps deep link - no API key, no billing account.
    Opens the Maps app on mobile or maps.google.com on desktop with real
    turn-by-turn transit/walking directions already loaded."""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_lat},{origin_lng}&destination={dest_lat},{dest_lng}"
        "&travelmode=transit"
    )


def citymapper_url(origin_lat, origin_lng, dest_lat, dest_lng,
                   start_name: str = None, end_name: str = None) -> str:
    """Citymapper directions deep link. On mobile this opens the Citymapper
    app directly (via universal link); on desktop it opens the web preview.
    Why we offer it alongside Google Maps: in London specifically, Citymapper
    beats Google at transit routing - it knows the Tube line statuses, bus
    routes, Overground vs Elizabeth line preferences, and gives a reliable
    fare quote. Free, no key, same shape as the Google URL."""
    import urllib.parse as _u
    params = [("startcoord", f"{origin_lat},{origin_lng}"),
              ("endcoord",   f"{dest_lat},{dest_lng}")]
    if start_name:
        params.append(("startname", start_name))
    if end_name:
        params.append(("endname", end_name))
    return "https://citymapper.com/directions?" + _u.urlencode(params)


# --------------------------------------------------------------------------
# 6. Orchestration - the single entry point the route calls
# --------------------------------------------------------------------------
def analyze_upload(file_text: str, events: list, user_location: dict = None,
                   identity_gate=None) -> dict:
    """identity_gate, when given, is called with the extracted data after
    classification and before any scoring spend. It returns None to proceed or
    a user-facing refusal string to stop - app.py uses it to hold each account
    to one person's CV. It runs here, not in the route, because this is the
    only point where the document is classified but no scoring tokens have
    been spent yet.

    user_location, when the person has granted it, is {"lat":.., "lng":..}
    - used only to attach a distance and directions link to each
    recommendation. Never required: every path below degrades to no
    distance/no map rather than failing when it's absent or a geocode
    lookup can't resolve a venue."""
    if not file_text or not file_text.strip():
        return _empty_result("That file looks empty - try a PDF, DOCX, or TXT CV.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _empty_result("AI analysis isn't configured yet (no API key set).")

    # Profile extraction is a model call and fails transiently the same way
    # scoring does. Retry once, and if it still fails say so as a FAILURE -
    # the old code funnelled every exception into _empty_result(), whose
    # is_cv=False the frontend renders as "This doesn't look like a CV",
    # telling people with perfectly valid CVs that their CV is unreadable.
    data, last_exc = None, None
    for attempt in (1, 2):
        try:
            data = extract_profile(file_text)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            log.warning(f"extract_profile failed (attempt {attempt}/2): {exc}")

    if data is None:
        if isinstance(last_exc, json.JSONDecodeError):
            return _empty_result(
                "We couldn't finish reading that file - this is a failure on our "
                "side, not a verdict on your CV. Please try again.", failed=True)
        return _empty_result(
            "Analysis is temporarily unavailable - please try again in a moment. "
            "This is a failure on our side, not a verdict on your CV.", failed=True)

    data.setdefault("analysis_failed", False)

    if not data.get("is_cv"):
        data["recommendations"] = []
        data.setdefault("orientation", None)
        data.setdefault("clarifying_questions", [])
        return data

    if identity_gate is not None:
        refusal = identity_gate(data)
        if refusal:
            out = _empty_result(refusal)
            out["identity_refused"] = True
            return out

    scored = score_and_enrich(data["profile"], data.get("evidence_level", "rich"),
                               data.get("field", ""), events, user_location)
    data.update(scored)
    return data


def score_and_enrich(profile: dict, evidence_level: str, field: str,
                      events: list, user_location: dict = None) -> dict:
    """The back half of analyze_upload - orientation + scoring + event
    enrichment - split out so a returning visitor's ALREADY-extracted
    profile can be re-run against the current catalog without paying for
    another extract_profile call (or asking them to paste their CV again).
    Same honesty rules, same retry/failure handling, same distance/map
    logic as a fresh analysis; the only thing skipped is re-reading the
    person, since nothing about them changed since last time."""
    # evidence_level lives at the top level but the scorer needs it to pick which
    # question it's answering (fit-to-record vs fit-to-direction).
    scoring_profile = {**profile, "evidence_level": evidence_level or "rich"}

    compact_events = build_compact_events(events)

    def _do_scoring():
        # Scoring is a model call and can fail transiently (truncated/invalid
        # JSON, API hiccup). Retry once before giving up - measured ~1 in 4
        # analyses returning zero purely from a transient failure, on a
        # profile whose top match otherwise scored 87-90 consistently.
        #
        # And critically: DO NOT let a failure masquerade as "no matches".
        # The frontend tells the user "that's an honest result, not a
        # failure", which is a lie if scoring never actually completed.
        # scoring_failed makes the difference explicit so the UI can say
        # "try again" instead.
        recs, failed = [], False
        for attempt in (1, 2):
            try:
                recs = score_events(scoring_profile, compact_events)
                recs = critique_recommendations(scoring_profile, recs)
                failed = False
                break
            except Exception as exc:
                failed = True
                log.warning(f"score_events failed (attempt {attempt}/2): {exc}")
        if failed:
            log.warning("scoring failed twice - reporting as failure, not as 'no matches'")
            recs = []
        return recs, failed

    orientation_field = None
    if evidence_level == "thin":
        orientation_field = (field or "").strip() or (profile.get("aspiration") or "").strip()

    result = {}
    if orientation_field:
        # Orientation (a web-search call, can run 60-90s) and scoring are
        # independent - both only need the profile. Sequentially they added
        # up to 2+ minutes of a user staring at a spinner; run them in
        # parallel threads so wall-clock time is roughly the slower of the
        # two, not the sum. build_orientation() never raises - it already
        # returns None on any failure - so no error handling needed here.
        with ThreadPoolExecutor(max_workers=2) as pool:
            orient_future = pool.submit(build_orientation, orientation_field, profile)
            score_future = pool.submit(_do_scoring)
            result["orientation"] = orient_future.result()
            recs, scoring_failed = score_future.result()
    else:
        result["orientation"] = None
        recs, scoring_failed = _do_scoring()

    result["scoring_failed"] = scoring_failed

    # Enrich with the real event object and cap at the honesty-rule max.
    by_id = {e["id"]: e for e in compact_events}
    enriched = []
    for i, r in enumerate(recs[:MAX_RECOMMENDATIONS]):
        ev = by_id.get(r["event_id"], {})
        location = ev.get("location", "")
        row = {**r, "category": ev.get("category", ""),
               "date": ev.get("date", ""), "source": ev.get("source", ""),
               "url": ev.get("url", ""), "emoji": ev.get("emoji", ""),
               "format": ev.get("format", ""), "location": location}

        # Distance + directions, only when the person granted location AND
        # the venue text resolves to a real point - both best-effort, never
        # required for a recommendation to render. Response-time fix:
        # geocode_location sleeps up to Nominatim's mandated 1.05s between
        # calls for anything not already cached, serially, on the request's
        # critical path - up to ~3s of pure rate-limit sleep for a full
        # MAX_RECOMMENDATIONS=4 set of venues nobody's looked up yet. recs
        # arrive sorted by fit_score, so capping this to the top matches
        # trims most of that tail; lower-ranked recs just render without a
        # map/distance rather than block the whole response on them.
        if user_location and location and i < 2:
            geo = geocode_location(location)
            if geo:
                row["event_lat"] = geo["lat"]
                row["event_lng"] = geo["lng"]
                row["distance_km"] = round(
                    _haversine_km(user_location["lat"], user_location["lng"],
                                  geo["lat"], geo["lng"]), 1)
                row["directions_url"] = directions_url(
                    user_location["lat"], user_location["lng"], geo["lat"], geo["lng"])
                row["citymapper_url"] = citymapper_url(
                    user_location["lat"], user_location["lng"], geo["lat"], geo["lng"],
                    start_name="Home", end_name=location)
        enriched.append(row)

    result["recommendations"] = enriched
    return result
