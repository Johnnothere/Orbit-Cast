"""Site-wide daily spend ceilings on AI analyses.

Two ceilings, both counted per UTC day across the whole site:

  ai-daily    every analysis started, signed in or not (AI_DAILY_LIMIT)
  anon-daily  analyses started by anonymous visitors (ANON_DAILY_LIMIT)

They used to be flask-limiter limits on in-process storage, which resets on
every deploy. Railway deploys on every push, so on a busy day of pushing a
"daily" ceiling reset several times over - a ceiling only in name, on the
one limit that exists to bound the bill.

The counters now live in Postgres (spend_counters, migration 0024) and are
taken at the moment a job is accepted, not when the request arrives: a
refused upload, a failed bot check or an "already running" reply no longer
uses up anyone's allowance.

When the database cannot answer, counting falls back to this process's
memory - exactly the old behaviour, resetting on deploy. The alternative,
refusing every analysis while the database is down, would turn a database
outage into an analysis outage too, and the 18-hour one on 30-31 Oct 2026
showed how long that can go unnoticed. Being briefly over-generous during a
database outage is the cheaper failure. A flapping database can split one
day's count between the two stores, allowing up to twice the limit; the
transition is logged both ways so that is visible.
"""

import logging
import threading
from datetime import datetime, timezone

import db

log = logging.getLogger("orbitcast.spend")

AI_DAILY = "ai-daily"
ANON_DAILY = "anon-daily"

# (day, scope) -> count. Only written while the database cannot answer.
_mem: dict = {}
_mem_lock = threading.Lock()
_db_down = False


class LimitReached(Exception):
    """A ceiling has no room left today. `scope` names which one."""

    def __init__(self, scope):
        super().__init__(scope)
        self.scope = scope


class Receipt:
    """Proof of a take, so a job that is counted and then turned away (a full
    queue) can hand its units back to whichever store they came from."""

    def __init__(self, day, scopes, in_db):
        self.day, self.scopes, self.in_db = day, list(scopes), in_db

    def refund(self):
        if self.in_db:
            db.spend_refund(self.day, self.scopes)
            return
        with _mem_lock:
            for scope in self.scopes:
                key = (self.day, scope)
                if _mem.get(key, 0) > 0:
                    _mem[key] -= 1


def today():
    return datetime.now(timezone.utc).date()


def take(wants):
    """Take one unit from each (scope, limit) pair, all or nothing.

    Returns a Receipt, or raises LimitReached naming the first full scope."""
    day = today()
    status, full = db.spend_take(day, wants)
    if status == "ok":
        _note_db(up=True)
        return Receipt(day, [s for s, _ in wants], in_db=True)
    if status == "full":
        _note_db(up=True)
        raise LimitReached(full)

    _note_db(up=False)
    with _mem_lock:
        for key in [k for k in _mem if k[0] != day]:
            del _mem[key]                          # yesterday's counts
        for scope, limit in wants:
            if _mem.get((day, scope), 0) >= limit:
                raise LimitReached(scope)
        for scope, _ in wants:
            _mem[(day, scope)] = _mem.get((day, scope), 0) + 1
    return Receipt(day, [s for s, _ in wants], in_db=False)


def counts():
    """Today's counts and where they came from, for the admin view."""
    day = today()
    stored = db.spend_counts(day)
    if stored is not None:
        return {"day": day.isoformat(), "source": "database", "counts": stored}
    with _mem_lock:
        mem = {scope: n for (d, scope), n in _mem.items() if d == day}
    return {"day": day.isoformat(), "source": "memory", "counts": mem}


def _note_db(up):
    """Log the transition, not every request."""
    global _db_down
    if up and _db_down:
        log.info("Spend ceilings are counting in the database again.")
    elif not up and not _db_down:
        log.warning("Spend ceilings cannot reach the database; counting in "
                    "process memory until it answers (resets on deploy).")
    _db_down = not up
