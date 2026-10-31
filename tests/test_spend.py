"""Daily spend ceilings: the policy in spend.py, with the database faked.

The SQL in db.spend_take is exercised against a real Postgres when the
migration is applied; these pin down what the app does with each answer -
especially the fallback, which is the part that only runs on a bad day.
"""

import unittest
from datetime import date
from unittest import mock

import spend

AI = (spend.AI_DAILY, 3)
ANON = (spend.ANON_DAILY, 1)


class _FakeDb:
    """Counts like spend_counters would, or pretends to be unreachable."""

    def __init__(self, up=True):
        self.up = up
        self.n = {}
        self.refunds = []

    def spend_take(self, day, wants):
        if not self.up:
            return "unavailable", None
        for scope, limit in wants:
            if self.n.get((day, scope), 0) >= limit:
                return "full", scope
        for scope, _ in wants:
            self.n[(day, scope)] = self.n.get((day, scope), 0) + 1
        return "ok", None

    def spend_refund(self, day, scopes):
        self.refunds.append((day, list(scopes)))
        return True

    def spend_counts(self, day):
        if not self.up:
            return None
        return {s: n for (d, s), n in self.n.items() if d == day}


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDb()
        patches = [mock.patch.object(spend.db, name, getattr(self.db, name))
                   for name in ("spend_take", "spend_refund", "spend_counts")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        spend._mem.clear()
        spend._db_down = False

    # -- database answering ---------------------------------------------------

    def test_take_counts_in_the_database(self):
        r = spend.take([AI])
        self.assertTrue(r.in_db)
        self.assertEqual(self.db.spend_counts(spend.today()), {spend.AI_DAILY: 1})

    def test_full_raises_and_names_the_scope(self):
        spend.take([AI, ANON])
        with self.assertRaises(spend.LimitReached) as cm:
            spend.take([AI, ANON])
        self.assertEqual(cm.exception.scope, spend.ANON_DAILY)

    def test_refund_goes_back_to_the_database(self):
        spend.take([AI]).refund()
        self.assertEqual(self.db.refunds, [(spend.today(), [spend.AI_DAILY])])

    def test_counts_report_the_database(self):
        spend.take([AI])
        self.assertEqual(spend.counts()["source"], "database")

    # -- database unreachable: the old in-memory behaviour --------------------

    def test_falls_back_to_memory_and_still_limits(self):
        self.db.up = False
        for _ in range(3):
            self.assertFalse(spend.take([AI]).in_db)
        with self.assertRaises(spend.LimitReached):
            spend.take([AI])

    def test_memory_take_is_all_or_nothing(self):
        """A full anon ceiling must not still spend an ai-daily unit."""
        self.db.up = False
        spend.take([AI, ANON])
        with self.assertRaises(spend.LimitReached):
            spend.take([AI, ANON])
        self.assertEqual(spend._mem[(spend.today(), spend.AI_DAILY)], 1)

    def test_memory_refund_stays_in_memory(self):
        self.db.up = False
        spend.take([AI]).refund()
        self.assertEqual(spend._mem[(spend.today(), spend.AI_DAILY)], 0)
        self.assertEqual(self.db.refunds, [])

    def test_memory_refund_never_goes_negative(self):
        self.db.up = False
        r = spend.take([AI])
        r.refund()
        r.refund()
        self.assertEqual(spend._mem[(spend.today(), spend.AI_DAILY)], 0)

    def test_yesterdays_memory_counts_are_dropped(self):
        self.db.up = False
        spend._mem[(date(2000, 1, 1), spend.AI_DAILY)] = 99
        spend.take([AI])
        self.assertNotIn((date(2000, 1, 1), spend.AI_DAILY), spend._mem)

    def test_counts_report_memory_when_the_database_is_down(self):
        self.db.up = False
        spend.take([AI])
        c = spend.counts()
        self.assertEqual(c["source"], "memory")
        self.assertEqual(c["counts"], {spend.AI_DAILY: 1})

    def test_transition_is_logged_once_each_way(self):
        with self.assertLogs("orbitcast.spend", level="INFO") as logs:
            self.db.up = False
            spend.take([AI])
            spend.take([AI])                 # still down: no second warning
            self.db.up = True
            spend.take([AI])
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertEqual(logs.records[1].levelname, "INFO")


if __name__ == "__main__":
    unittest.main()
