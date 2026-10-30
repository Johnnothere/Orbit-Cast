"""The extraction pool is bounded and its slots are returned.

The behaviour that matters: a parser that overruns keeps holding its slot
(because the thread really is still running), and when every slot is held the
next upload is refused instead of leaking another unkillable thread.
"""

import threading
import time
import unittest

import ai_engine


class ExtractPoolTests(unittest.TestCase):
    def setUp(self):
        self._raw = ai_engine._extract_text_raw
        self._timeout = ai_engine.EXTRACT_TIMEOUT_S
        # Drain whatever the pool has, so each test starts from full capacity.
        self._drained = []
        while ai_engine._extract_slots.acquire(blocking=False):
            self._drained.append(1)
        for _ in self._drained:
            ai_engine._extract_slots.release()

    def tearDown(self):
        ai_engine._extract_text_raw = self._raw
        ai_engine.EXTRACT_TIMEOUT_S = self._timeout

    def test_slot_is_returned_after_a_normal_read(self):
        ai_engine._extract_text_raw = lambda b, f: "hello"
        before = _free_slots()
        self.assertEqual(ai_engine.extract_text(b"x", "cv.txt"), "hello")
        self.assertEqual(_free_slots(), before)

    def test_timeout_raises_a_user_facing_message(self):
        release = threading.Event()
        ai_engine._extract_text_raw = lambda b, f: (release.wait(5), "late")[1]
        ai_engine.EXTRACT_TIMEOUT_S = 0.05
        try:
            with self.assertRaises(ValueError) as cm:
                ai_engine.extract_text(b"x", "cv.pdf")
            self.assertIn("too long to read", str(cm.exception))
        finally:
            release.set()

    def test_a_stuck_parser_keeps_holding_its_slot(self):
        release = threading.Event()
        ai_engine._extract_text_raw = lambda b, f: (release.wait(5), "late")[1]
        ai_engine.EXTRACT_TIMEOUT_S = 0.05
        before = _free_slots()
        try:
            with self.assertRaises(ValueError):
                ai_engine.extract_text(b"x", "cv.pdf")
            # Still running, so still accounted for.
            self.assertEqual(_free_slots(), before - 1)
        finally:
            release.set()
            _wait_until(lambda: _free_slots() == before)
        self.assertEqual(_free_slots(), before)

    def test_saturated_pool_refuses_rather_than_leaking(self):
        held = []
        for _ in range(ai_engine.EXTRACT_WORKERS):
            self.assertTrue(ai_engine._extract_slots.acquire(blocking=False))
            held.append(1)
        try:
            ai_engine._extract_text_raw = lambda b, f: "never runs"
            with self.assertRaises(ValueError) as cm:
                ai_engine.extract_text(b"x", "cv.pdf")
            self.assertIn("Too many files", str(cm.exception))
        finally:
            for _ in held:
                ai_engine._extract_slots.release()


def _free_slots():
    n = 0
    while ai_engine._extract_slots.acquire(blocking=False):
        n += 1
    for _ in range(n):
        ai_engine._extract_slots.release()
    return n


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


if __name__ == "__main__":
    unittest.main()
