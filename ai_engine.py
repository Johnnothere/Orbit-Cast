"""
OrbitCast AI - upload classification + honest CV analysis + event matching.

The whole design goal here is HONESTY: read the real person, recommend only
genuine matches, ground every claim in actual CV evidence, and be willing to
say "nothing here strongly fits you."

Deps used on demand:  anthropic, pdfplumber, python-docx
"""

import io
import json
import os

from anthropic import Anthropic

MODEL = "claude-sonnet-4-20250514"   # matches your current model
FIT_THRESHOLD = 65                   # only surface events scoring this or higher

_client = None


def _get_client():
    """Lazy client so the module imports even with no key set (route returns a
    clean 'not configured' result instead of crashing)."""
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


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
# 2. The system prompt - this is the product. Honesty lives here.
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are OrbitCast's analysis engine. You do two jobs in one pass.

JOB 1 - CLASSIFY the uploaded document. Decide if it is a CV / resume or something
else (cover letter, report, random PDF, image-of-text, etc.). Be strict: a CV has a
person's work/education history, skills, and contact-style details. If it is not
clearly a CV, set is_cv=false and STOP - do not invent a profile or recommendations.

JOB 2 - If and only if it IS a CV, read the actual person and match them to events.

HONESTY RULES - this is the entire point of the product:
- Ground EVERY statement in concrete evidence from the CV. Reference the specific
  role, project, skill, or detail you are inferring from. No generic flattery like
  "passionate innovator." If you cannot point to evidence, do not say it.
- Be selective. Most events will NOT be a strong fit. Returning 2-4 strong
  recommendations is correct and expected, not 15. If nothing fits well, return an
  empty list and say so honestly in the profile summary.
- Score fit honestly on 0-100. Do not inflate. 60 is a real "maybe", 90 means this
  person should clearly go. Only include events scoring 65 or above.
- Give an honest read of the person, INCLUDING gaps - not just strengths. If the CV
  is strong on X but thin on Y, say it plainly, and prefer events that close real
  gaps over events that just echo existing strengths.
- Never recommend an event that is not in the provided list. Never fabricate event
  details. Use the exact event_id from the provided list.

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
     "interests": [string]         // inferred professional interests, evidence-based
  },
  "recommendations": [
     {
       "event_id": string,         // MUST match an id from the provided events
       "title": string,
       "fit_score": number,        // 0-100, honest, 65+ only
       "why": string,              // why THIS person, citing their CV evidence
       "prepare": string,          // concrete prep given their actual background
       "benefit": string           // concrete, specific payoff for them
     }
  ]
}
If is_cv is false, profile fields are empty and recommendations is []."""


def _empty_result(message: str) -> dict:
    return {
        "is_cv": False,
        "confidence": 0.0,
        "document_type": "unknown",
        "message_if_not_cv": message,
        "profile": {"summary": "", "strengths": [], "gaps": [], "interests": []},
        "recommendations": [],
    }


# --------------------------------------------------------------------------
# 3. The single analysis call
# --------------------------------------------------------------------------
def analyze_upload(file_text: str, events: list) -> dict:
    if not file_text or not file_text.strip():
        return _empty_result("That file looks empty - try a PDF or DOCX CV.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _empty_result("AI analysis isn't configured yet (no API key set).")

    compact_events = [
        {
            "id": str(e.get("id") or e.get("url") or e.get("title", "")),
            "title": e.get("title", ""),
            "category": e.get("category", ""),
            "date": e.get("date", ""),
            "description": (e.get("description", "") or "")[:400],
        }
        for e in events
    ]

    user_content = (
        "DOCUMENT TEXT:\n"
        f"{file_text[:12000]}\n\n"
        "AVAILABLE EVENTS (JSON array):\n"
        f"{json.dumps(compact_events, ensure_ascii=False)}"
    )

    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # network / API failure - never 500 the route
        return _empty_result(f"Analysis is temporarily unavailable. ({exc})")

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if raw.startswith("```"):
        raw = raw[3:]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_result("I couldn't read that file clearly. Try a PDF or DOCX CV.")

    # Safety net: enforce the honesty threshold in code, not just the prompt, and
    # sort best-fit first. Guarantees the product never over-recommends.
    recs = [
        r for r in data.get("recommendations", [])
        if isinstance(r, dict) and r.get("fit_score", 0) >= FIT_THRESHOLD
    ]
    data["recommendations"] = sorted(recs, key=lambda r: r.get("fit_score", 0), reverse=True)
    return data
