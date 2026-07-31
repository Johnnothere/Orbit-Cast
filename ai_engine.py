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
import os
import re

from anthropic import Anthropic

MODEL = "claude-sonnet-4-20250514"   # matches your current model
FIT_THRESHOLD = 65                   # only surface events scoring this or higher
MAX_RECOMMENDATIONS = 4              # honesty rule: 2-4 strong matches, not a list

_client = None


def _get_client():
    """Lazy client so the module imports even with no key set (route returns a
    clean 'not configured' result instead of crashing)."""
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


def _call(system, user_content, max_tokens=1500):
    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if raw.startswith("```"):
        raw = raw[3:]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# --------------------------------------------------------------------------
# 1. Text extraction - CVs arrive as PDF / DOCX / TXT
# --------------------------------------------------------------------------
def extract_text(file_bytes: bytes, filename: str) -> str:
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)

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


def build_compact_events(events: list) -> list:
    """Shared shape passed to both scoring and (eventually) the frontend."""
    return [
        {
            "id": str(e.get("id") or e.get("url") or e.get("title", "")),
            "title": e.get("title", ""),
            "category": e.get("category", ""),
            "date": e.get("date", ""),
            "source": e.get("source", ""),
            "url": e.get("url", ""),
            "emoji": e.get("emoji", ""),
            "format": _infer_format(e.get("title", "")),
            "description": (e.get("description", "") or "")[:400],
        }
        for e in events
    ]


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
    data = _call(PROFILE_SYSTEM_PROMPT, user_content, max_tokens=1200)
    data.setdefault("recommendations", [])
    return data


# --------------------------------------------------------------------------
# 4. PASS 2 - match the profile to events, honestly, with why-now reasoning
# --------------------------------------------------------------------------
SCORING_SYSTEM_PROMPT = """You are OrbitCast's event-matching engine. You receive an \
already-built profile of a person (from a separate reading step) and a catalog of \
real events. Your ONE job: score honest fit and explain it.

HONESTY RULES - this is the entire point of the product:
- Be selective. Most events will NOT be a strong fit. Returning 2-4 strong \
  recommendations is correct and expected, not 15. If nothing fits well, return an \
  empty list.
- Score fit honestly on 0-100. Do not inflate. 60 is a real "maybe", 90 means this \
  person should clearly go. Only include events scoring 65 or above.
- Never recommend an event that is not in the provided catalog. Use the exact \
  event_id from the catalog.
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


def score_events(profile: dict, compact_events: list) -> list:
    user_content = (
        "PERSON PROFILE (JSON):\n"
        f"{json.dumps(profile, ensure_ascii=False)}\n\n"
        "EVENT CATALOG (JSON array):\n"
        f"{json.dumps(compact_events, ensure_ascii=False)}"
    )
    data = _call(SCORING_SYSTEM_PROMPT, user_content, max_tokens=1800)
    valid_ids = {e["id"] for e in compact_events}
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
        data = _call(CRITIQUE_SYSTEM_PROMPT, user_content, max_tokens=800)
    except Exception:
        # Critique failing shouldn't sink the whole analysis - fall back to
        # the unaudited (but already threshold-enforced) recommendations.
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
        return _empty_result(f"Analysis is temporarily unavailable. ({exc})")

    if not data.get("is_cv"):
        data["recommendations"] = []
        return data

    compact_events = build_compact_events(events)

    try:
        recs = score_events(data["profile"], compact_events)
        recs = critique_recommendations(data["profile"], recs)
    except json.JSONDecodeError:
        recs = []
    except Exception:
        recs = []

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
