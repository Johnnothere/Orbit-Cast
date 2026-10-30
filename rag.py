"""
OrbitCast AI - retrieval-augmented scoring (RAG).

Mirrors the workflow from Fin.ai-style "upload documents to train it":
someone uploads a document of example CV-to-event judgments (good match,
bad match, borderline - with the reasoning that makes it so), we parse it
into structured examples, embed each one, and store it in labeled_examples.
At scoring time, instead of always showing the model the same static
few-shot examples, we retrieve the judgments most similar to the person
actually being scored right now and inject those instead.

Nothing here changes the model's weights. It changes what the model is
shown immediately before it reasons - the standard, well-understood way
these systems actually "learn" from a growing example bank.

Degrades gracefully: no VOYAGE_API_KEY or no DB configured -> retrieval
returns [] and ai_engine falls back to its static few-shot examples.
"""

import json
import logging
import os

import requests

import db

log = logging.getLogger("orbitcast.rag")

# Admin document ingestion. Was hardcoded to claude-sonnet-5 in the call
# itself; named here so it is switchable like every other model in the app.
INGEST_MODEL = os.environ.get("OC_MODEL_RAG", "claude-sonnet-5")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_MODEL = "voyage-3"     # 1024-dim - matches labeled_examples.embedding column
EMBED_DIM = 1024
RETRIEVE_K = 3                # how many similar past judgments to pull in per scoring call


def is_configured() -> bool:
    return bool(VOYAGE_API_KEY) and db.is_configured()


def embed_text(text: str):
    """Returns a 1024-dim embedding, or None if not configured / request fails."""
    if not VOYAGE_API_KEY or not text or not text.strip():
        return None
    try:
        r = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {VOYAGE_API_KEY}", "Content-Type": "application/json"},
            json={"input": [text[:8000]], "model": VOYAGE_MODEL, "input_type": "query"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception as exc:
        log.warning(f"embed_text failed: {exc}")
        return None


def _profile_text(profile: dict) -> str:
    """Flatten a profile dict into text worth embedding - summary and
    trajectory carry the most signal for finding similar past judgments."""
    parts = [
        profile.get("summary", ""),
        profile.get("trajectory", ""),
        " ".join(profile.get("strengths", []) or []),
        " ".join(profile.get("interests", []) or []),
    ]
    return "\n".join(p for p in parts if p)


def retrieve_examples(profile: dict) -> list:
    """Returns up to RETRIEVE_K past labeled judgments most similar to this
    profile, formatted for direct injection into the scoring prompt."""
    if not is_configured():
        return []
    text = _profile_text(profile)
    embedding = embed_text(text)
    if embedding is None:
        return []
    return db.search_labeled_examples(embedding, k=RETRIEVE_K)


def format_examples_for_prompt(examples: list) -> str:
    """Turns retrieved rows into a text block the scoring prompt can drop in
    as additional, request-specific few-shot examples."""
    if not examples:
        return ""
    lines = ["ADDITIONAL EXAMPLES FROM PAST REVIEWED JUDGMENTS (most similar to this person):"]
    for ex in examples:
        tag = {"good": "GOOD JUDGMENT", "bad": "BAD JUDGMENT (do not repeat this pattern)",
               "borderline": "BORDERLINE - handled correctly"}.get(ex["judgment"], ex["judgment"].upper())
        lines.append(
            f"- [{tag}] Person: {ex['profile_summary'][:300]}\n"
            f"  Event context: {(ex.get('event_context') or '')[:200]}\n"
            f"  Reasoning: {(ex.get('ideal_why') or '')[:300]}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Ingestion - parse an uploaded document into structured examples, embed
# and store each one. Mirrors the "upload a messy PDF, we structure it"
# experience from Fin.ai.
# --------------------------------------------------------------------------
_INGEST_PROMPT = """You extract structured training examples from a document \
about CV-to-event matching judgments for OrbitCast, an honest event \
recommendation engine.

The document may be messy or conversational - your job is to pull out \
every distinct example of "this kind of person" being judged against \
"this kind of event", with the reasoning for why it was a good, bad, or \
borderline match. If the document doesn't contain any such examples, \
return an empty list - do not invent examples that aren't actually there.

Return ONLY valid JSON - no markdown, no preamble - in EXACTLY this schema:
{
  "examples": [
    {
      "profile_summary": string,   // who this person is, evidence-grounded
      "event_context": string,     // what kind of event they were judged against
      "judgment": "good" | "bad" | "borderline",
      "ideal_why": string          // the specific, non-generic reasoning for the judgment
    }
  ]
}"""


def parse_document_to_examples(document_text: str) -> list:
    """Uses Claude to structure a raw uploaded document into example rows.
    Returns [] on any failure - ingestion failing should never crash the
    admin route, it should just report zero examples found."""
    if not document_text or not document_text.strip():
        return []
    try:
        # Goes through ai_engine rather than building a client here. The
        # inline Anthropic() this replaces had no timeout, no retry budget, no
        # in-flight semaphore and no outage breaker, so on the single worker a
        # hung ingest call held a thread for the SDK's ten-minute default
        # while the breaker that exists to stop exactly that sat one import
        # away. Imported lazily to keep the ai_engine -> rag import one-way.
        import ai_engine
        data = ai_engine._call(_INGEST_PROMPT, document_text[:20000],
                               max_tokens=4096, model=INGEST_MODEL)
        return data.get("examples", []) if isinstance(data, dict) else []
    except Exception as exc:
        log.warning(f"parse_document_to_examples failed: {exc}")
        return []


def ingest_document(document_text: str, source_name: str, reviewed_by: str = None) -> dict:
    """Parses a document into examples, embeds and stores each. Returns a
    summary: {found, stored, skipped}."""
    examples = parse_document_to_examples(document_text)
    stored = 0
    for ex in examples:
        summary = ex.get("profile_summary", "")
        context = ex.get("event_context", "")
        embedding = embed_text(f"{summary}\n{context}")
        if embedding is None:
            continue
        row_id = db.insert_labeled_example(
            source=source_name,
            profile_summary=summary,
            profile_json=None,
            event_context=context,
            judgment=ex.get("judgment", "borderline"),
            ideal_why=ex.get("ideal_why", ""),
            embedding=embedding,
            reviewed_by=reviewed_by,
        )
        if row_id is not None:
            stored += 1
    return {"found": len(examples), "stored": stored, "skipped": len(examples) - stored}
