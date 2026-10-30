"""Regression tests for the operational fixes: per-host fetch back-off, the
AI outage breaker, parallel reads. No network, no database.

Run:  python -m unittest tests.test_ops
"""
import time
import unittest
from unittest import mock

import ai_engine
import db
import scraper


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.url = "https://example.org/x"

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise scraper.requests.exceptions.HTTPError(f"{self.status_code}")


class HostBackoff(unittest.TestCase):
    def setUp(self):
        scraper._host_blocks.clear()
        scraper.clear_fetch_error()

    def test_403_pauses_host_and_skips_next_fetch(self):
        with mock.patch.object(scraper, "_is_public_host", return_value=True), \
             mock.patch.object(scraper.requests, "get", return_value=_Resp(403)) as get:
            self.assertIsNone(scraper.fetch("https://blocked.example/a"))
            self.assertEqual(get.call_count, 1)
            self.assertIsNone(scraper.fetch("https://blocked.example/b"))
            self.assertEqual(get.call_count, 1)             # no second request
        self.assertIn("HTTP 403", scraper.host_paused("https://blocked.example/c"))
        self.assertEqual(scraper.last_fetch_error(), "paused after HTTP 403 for https://blocked.example/a")
        ph = scraper.paused_hosts()
        self.assertIn("blocked.example", ph)
        self.assertGreater(ph["blocked.example"]["resumes_in_s"], 5 * 3600)

    def test_429_is_a_short_pause(self):
        with mock.patch.object(scraper, "_is_public_host", return_value=True), \
             mock.patch.object(scraper.requests, "get", return_value=_Resp(429)):
            scraper.fetch("https://ratelimited.example/a")
        self.assertLess(scraper.paused_hosts()["ratelimited.example"]["resumes_in_s"], 16 * 60)

    def test_repeat_strikes_double(self):
        scraper._pause_host("https://h.example/", "HTTP 403", 3600)
        scraper._host_blocks["h.example"] = (time.monotonic() - 1, 1, "HTTP 403")   # expired
        scraper._pause_host("https://h.example/", "HTTP 403", 3600)
        self.assertEqual(scraper._host_blocks["h.example"][1], 2)
        self.assertGreater(scraper.paused_hosts()["h.example"]["resumes_in_s"], 7000)

    def test_other_hosts_unaffected(self):
        scraper._pause_host("https://a.example/", "HTTP 403", 3600)
        self.assertIsNone(scraper.host_paused("https://b.example/"))

    def test_connection_error_pauses_briefly(self):
        err = scraper.requests.exceptions.ConnectionError("Remote end closed connection")
        with mock.patch.object(scraper, "_is_public_host", return_value=True), \
             mock.patch.object(scraper.requests, "get", side_effect=err):
            self.assertIsNone(scraper.fetch("https://flaky.example/"))
        p = scraper.paused_hosts()["flaky.example"]
        self.assertTrue(3000 < p["resumes_in_s"] <= 3600)


class _ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status


class OutageBreaker(unittest.TestCase):
    def setUp(self):
        # _outage is keyed by provider now: Anthropic and OpenAI are separate
        # accounts that fail independently, so they get separate breakers.
        with ai_engine._outage_lock:
            ai_engine._outage.clear()

    def test_billing_error_opens_breaker(self):
        client = mock.Mock()
        client.messages.create.side_effect = _ApiError(400, "Your credit balance is too low to access the Anthropic API.")
        with mock.patch.object(ai_engine, "_get_client", return_value=client):
            with self.assertRaises(_ApiError):
                ai_engine._create(model="m", max_tokens=1, system="", messages=[])
            self.assertIsNotNone(ai_engine.outage())
            with self.assertRaises(ai_engine.AIUnavailable):
                ai_engine._create(model="m", max_tokens=1, system="", messages=[])
            self.assertEqual(client.messages.create.call_count, 1)   # breaker, not the API

    def test_transient_error_does_not_open_breaker(self):
        client = mock.Mock()
        client.messages.create.side_effect = _ApiError(529, "overloaded")
        with mock.patch.object(ai_engine, "_get_client", return_value=client):
            with self.assertRaises(_ApiError):
                ai_engine._create(model="m", max_tokens=1, system="", messages=[])
        self.assertIsNone(ai_engine.outage())

    def test_breaker_expires(self):
        with ai_engine._outage_lock:
            ai_engine._outage["anthropic"] = {"until": time.time() - 1, "reason": "x",
                                              "since": None, "count": 1}
        self.assertIsNone(ai_engine.outage())
        self.assertIsNone(ai_engine.outage("anthropic"))


class ParallelReads(unittest.TestCase):
    def test_order_and_failure_isolation(self):
        def boom():
            raise RuntimeError("no")
        out = db.parallel(lambda: 1, boom, lambda: 3)
        self.assertEqual(out, [1, None, 3])

    def test_single_call(self):
        self.assertEqual(db.parallel(lambda: "a"), ["a"])


if __name__ == "__main__":
    unittest.main()
