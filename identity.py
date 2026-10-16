"""
ORBITCAST — one account, one person's CV.

WHY THIS EXISTS
Analyses are free and each one costs real Anthropic tokens. The obvious way to
farm that is one Google account running everyone's CVs through it - a friend's,
a flatmate's, a client list. The deal this module enforces is simple and easy
to say out loud: an account analyses ONE person. Your CV, your LinkedIn export,
your self-description, as many versions as you like - but yours.

HOW
The extraction pass already reads the document; it now also reports the
person's name exactly as the document states it. The first named document on an
account locks that account to that name (stored on the accounts row). Every
later document with a name must belong to the same person, judged by a
forgiving comparison that accepts initials, middle names, dropped honorifics
and accents - and refuses a different human. A CV or LinkedIn export with no
readable name at all is refused outright ("a CV without a name" is the classic
shape of scraped or fabricated input); a typed self-description needs no name.

The check runs between classification and scoring, so a refused document costs
one cheap extraction call, never the expensive scoring pipeline.

WHAT IT IS NOT
Not identity verification. It never checks the name against the Google account
(people's CV names legitimately differ from their Google names), it just holds
the account to ONE consistent person. The lock survives "delete my CV" on
purpose - otherwise upload-analyse-delete-repeat defeats it - and disappears
with the account itself (GDPR forget), because it belongs to the account.
"""

import re
import unicodedata

# Titles and suffixes that are not part of who a person is.
_DROP = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "lord",
         "jr", "sr", "ii", "iii", "iv", "phd", "mba", "msc", "bsc", "cissp"}


def normalize_tokens(name: str) -> list:
    """Lowercase, de-accent, strip punctuation, drop titles. Keeps initials."""
    if not name:
        return []
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    tokens = [t for t in re.split(r"[^a-z]+", text) if t]
    return [t for t in tokens if t not in _DROP][:8]


def same_person(stored: str, given: str) -> bool:
    """Forgiving same-human check between two name strings.

    Accepts: identical names, initials ("T. Gohil" vs "Tej Gohil"), one name
    being a subset of the other ("Tej Gohil" vs "Tej R Gohil"), accent and
    case differences. Refuses a different first name on the same surname
    (a sibling) and any name sharing nothing.
    """
    a, b = normalize_tokens(stored), normalize_tokens(given)
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = set(a), set(b)
    # Full-token subset: every real (non-initial) token of the shorter name
    # appears in the longer one.
    fa = {t for t in sa if len(t) > 1}
    fb = {t for t in sb if len(t) > 1}
    if fa and fb and (fa <= sb or fb <= sa):
        return True
    # Count matches, letting an initial stand in for a full token.
    matched = len(fa & fb)
    for init in (t for t in sa if len(t) == 1):
        if any(t.startswith(init) for t in fb):
            matched += 1
            break
    for init in (t for t in sb if len(t) == 1):
        if any(t.startswith(init) for t in fa):
            matched += 1
            break
    return matched >= 2


NAME_REQUIRED_MSG = (
    "We couldn't find a name on that document. A CV or LinkedIn export needs "
    "the person's own name on it to be analysed - if yours doesn't have one, "
    "add it, or use “Describe yourself” instead."
)


def mismatch_msg(locked_name: str) -> str:
    return (
        f"This account analyses documents for one person ({locked_name}). The "
        "document you uploaded appears to be someone else's, so it wasn't "
        "analysed. OrbitCast is free because each account serves its own "
        "person - if that's your friend's CV, they can sign in with their own "
        "Google account in seconds. If this IS you (changed name, married "
        "name), use “Delete everything” in your account panel and start "
        "fresh, or get in touch."
    )


def make_gate(account_id, stored_name, set_identity):
    """Builds the identity_gate callable ai_engine.analyze_upload runs after
    classification. `set_identity(account_id, name)` persists a first lock and
    returns the name that won (another thread may have raced); `stored_name`
    is the current lock or None. Returns None to proceed, or the refusal text.
    """
    def gate(data) -> str:
        kind = data.get("input_kind")
        name = (data.get("person_name") or "").strip()
        if kind == "cv" and not name:
            return NAME_REQUIRED_MSG
        if not name:
            return None                      # nameless self-description: fine
        locked = stored_name
        if not locked:
            locked = set_identity(account_id, name) or name
        if same_person(locked, name):
            return None
        return mismatch_msg(locked)
    return gate
