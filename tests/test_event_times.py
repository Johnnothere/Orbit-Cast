"""Regression tests for event_times - the reader that keeps "TBC" off events
whose page states a time. No network: every case is an inline page.

Run:  python -m unittest tests.test_event_times
"""
import unittest

from bs4 import BeautifulSoup

import event_times as et


def soup(html):
    return BeautifulSoup(html, "lxml")


class IsoTimes(unittest.TestCase):
    def test_utc_winter(self):
        self.assertEqual(et.when_from_iso("2026-11-03T19:00:00Z", "2026-11-03T20:30:00Z"),
                         ("2026-11-03", "19:00", "20:30"))

    def test_utc_summer_is_shifted_to_london(self):
        self.assertEqual(et.when_from_iso("2026-07-03T19:00:00Z", "2026-07-03T20:30:00Z"),
                         ("2026-07-03", "20:00", "21:30"))

    def test_naive_is_london(self):
        self.assertEqual(et.when_from_iso("2026-12-01T13:00:00", "2026-12-01T14:00:00"),
                         ("2026-12-01", "13:00", "14:00"))

    def test_fractional_and_offset(self):
        self.assertEqual(et.when_from_iso("2026-10-29T17:30:00.000+00:00"), ("2026-10-29", "17:30", None))

    def test_midnight_is_no_time(self):
        self.assertEqual(et.when_from_iso("2026-11-01T00:00:00.000+00:00", "2026-11-13T00:00:00.000+00:00"),
                         ("2026-11-01", None, None))

    def test_date_only(self):
        self.assertEqual(et.when_from_iso("2026-11-05"), ("2026-11-05", None, None))

    def test_end_on_another_day_is_dropped(self):
        self.assertEqual(et.when_from_iso("2026-11-10T09:00:00Z", "2026-11-12T17:00:00Z"),
                         ("2026-11-10", "09:00", None))

    def test_label(self):
        self.assertEqual(et.label("18:30", "20:30"), "18:30–20:30")
        self.assertEqual(et.label("18:30", None), "18:30")
        self.assertIsNone(et.label(None))

    def test_usable_time(self):
        self.assertFalse(et.usable_time("00:00"))
        self.assertFalse(et.usable_time(""))
        self.assertFalse(et.usable_time(None))
        self.assertTrue(et.usable_time("09:15"))
        self.assertTrue(et.usable_time("18:30–20:30"))


class ProseTimes(unittest.TestCase):
    CASES = {
        "Thursday, Oct 29 · 5:30 PM to 8:30 PM GMT": ("17:30", "20:30"),
        "11am - 12pm Governance briefing": ("11:00", "12:00"),
        "Time: 18:00 – 20:00": ("18:00", "20:00"),
        "Doors 6pm. Talk at 7pm.": ("18:00", None),
        "Tuesday, November 3 • 7 PM - 8:30 PM": ("19:00", "20:30"),
        "Fri 30 October 2026, 14.00–15.00GMT": ("14:00", "15:00"),
        "13:00 – 14:00 GMT 1 December 2026": ("13:00", "14:00"),
        "Fri, Sep 4, 12:30 PM": ("12:30", None),
        "Starts at 18.30 sharp": ("18:30", None),
        "Registration 08:30, sessions 09:00-17:00": ("09:00", "17:00"),
        "Join at 7.30 p.m.": ("19:30", None),
        "Monday 3 November, 10:30": ("10:30", None),
        "1-2pm lunch": ("13:00", "14:00"),
        "11-1pm": ("11:00", "13:00"),
        "10am-2": ("10:00", "14:00"),
        "9:00 - 5:00": ("09:00", "17:00"),
        "12pm - 2pm lunch": ("12:00", "14:00"),
        # Not times
        "Tickets from 7.50 and ages 18-25 welcome": (None, None),
        "Dec 3-4 26 conference, priced at 10.50": (None, None),
        "9 - 5 working hours": (None, None),
        "The event runs 2026-10-29 to 2026-10-30": (None, None),
        "Score was 3-1 at 90 minutes": (None, None),
        "7pm somewhere in text with no label": (None, None),
        "12am - 2am club night": (None, None),
        "Wednesday 12 Nov 2026": (None, None),
        "Tue 4 Nov 2026 at £15": (None, None),
    }

    def test_cases(self):
        for text, expected in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(et.times_from_text(text), expected)


class PageExtraction(unittest.TestCase):
    def test_jsonld_wins(self):
        page = soup('''<html><head><script type="application/ld+json">
        {"@type":"Event","name":"X","startDate":"2026-11-10T08:15:00+00:00","endDate":"2026-11-10T09:15:00+00:00"}
        </script></head><body><main>Breakfast 7am - 8am for sponsors</main></body></html>''')
        self.assertEqual(et.extract(page, "2026-11-10", "https://x.org/event/a/"),
                         {"date": "2026-11-10", "start": "08:15", "end": "09:15", "method": "jsonld"})

    def test_jsonld_education_event_type(self):
        page = soup('''<script type="application/ld+json">{"@type":"EducationEvent","name":"X",
        "startDate":"2026-11-03T19:00:00Z","endDate":"2026-11-03T20:30:00Z"}</script>''')
        hit = et.extract(page, "2026-11-03", "https://www.eventbrite.co.uk/e/x-123")
        self.assertEqual((hit["start"], hit["end"]), ("19:00", "20:30"))

    def test_jsonld_graph(self):
        page = soup('''<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},
        {"@type":"Event","startDate":"2026-12-01T13:00:00","endDate":"2026-12-01T14:00:00"}]}</script>''')
        self.assertEqual(et.extract(page, "2026-12-01", "https://x.org/e/1")["start"], "13:00")

    def test_other_date_is_reported_then_refused(self):
        """A series page shows its next instance; extract reports that date
        honestly and _apply refuses to put its time on a different day."""
        page = soup('''<script type="application/ld+json">{"@type":"Event","startDate":"2026-12-01T13:00:00"}</script>
        <main>13:00 – 14:00</main>''')
        hit = et.extract(page, "2026-11-20", "https://x.org/e/1")
        self.assertEqual(hit["date"], "2026-12-01")
        ev = {"date": "2026-11-20", "time": None}
        self.assertFalse(et._apply(ev, {"found": True, **hit}))
        self.assertIsNone(ev["time"])

    def test_listing_page_with_many_dates_yields_nothing(self):
        page = soup('''<script type="application/ld+json">[{"@type":"Event","startDate":"2026-11-01"},
        {"@type":"Event","startDate":"2026-11-02"}]</script><main>Doors 7pm</main>''')
        self.assertIsNone(et.extract(page, "2026-11-03", "https://x.org/events/"))

    def test_homepage_text_is_not_trusted(self):
        page = soup("<main>Opening hours 9am - 5pm</main>")
        self.assertIsNone(et.extract(page, "2026-11-03", "https://x.org/"))

    def test_meta_tags(self):
        page = soup('''<meta property="event:start_time" content="2026-11-03T19:00:00Z">
        <meta property="event:end_time" content="2026-11-03T20:30:00Z">''')
        hit = et.extract(page, "2026-11-03", "https://x.org/e/1")
        self.assertEqual((hit["start"], hit["end"], hit["method"]), ("19:00", "20:30", "meta"))

    def test_time_tag(self):
        page = soup('<p>When: <time datetime="2026-10-30T14:00:00+00:00">Fri</time></p>')
        hit = et.extract(page, "2026-10-30", "https://x.org/e/1")
        self.assertEqual((hit["start"], hit["method"]), ("14:00", "time-tag"))

    def test_midnight_time_tag_falls_through_to_text(self):
        page = soup('''<time datetime="2026-11-01T00:00:00.000+00:00">Nov 1</time>
        <main>Hackathon kick-off at 10:00 on the Saturday</main>''')
        hit = et.extract(page, "2026-11-01", "https://x.org/e/1")
        self.assertEqual((hit["start"], hit["method"]), ("10:00", "text"))

    def test_time_outside_article_in_labelled_banner(self):
        """RUSI keeps the time in a page banner, outside <article>."""
        page = soup('''<body><div class="page-banner"><span class="event-time">13:00 – 14:00 GMT 1 December 2026</span></div>
        <article>Panel discussion. Members only.</article><footer>Office hours 9am - 5pm</footer></body>''')
        hit = et.extract(page, "2026-12-01", "https://my.rusi.org/events/x.html")
        self.assertEqual((hit["start"], hit["end"]), ("13:00", "14:00"))

    def test_footer_hours_are_not_the_event(self):
        page = soup('''<body><main>A talk about things.</main><footer>Open 9am - 5pm</footer></body>''')
        self.assertIsNone(et.extract(page, "2026-12-01", "https://x.org/e/1"))


class Catalogue(unittest.TestCase):
    def test_normalise_iso_date_string(self):
        ev = {"date": "2026-11-05T13:00:00+00:00", "time": ""}
        self.assertEqual(et.normalise(ev)["time"], "13:00")

    def test_normalise_eventbrite_card_text(self):
        ev = {"date": "Fri, Sep 4, 12:30 PM"}
        self.assertEqual(et.normalise(ev)["time"], "12:30")

    def test_normalise_imperial_range(self):
        ev = {"date": "Fri 30 October 2026, 14.00–15.00GMT"}
        self.assertEqual(et.normalise(ev)["time"], "14:00–15:00")

    def test_normalise_keeps_a_real_time_and_drops_midnight(self):
        self.assertEqual(et.normalise({"date": "2026-11-05", "time": "18:30"})["time"], "18:30")
        self.assertIsNone(et.normalise({"date": "2026-11-05", "time": "00:00"})["time"])

    def test_apply_refuses_other_date(self):
        ev = {"date": "2026-11-01", "time": None}
        rec = {"found": True, "start": "13:00", "end": None, "date": "2026-10-19"}
        self.assertFalse(et._apply(ev, rec))
        self.assertIsNone(ev["time"])

    def test_apply_fills_time_and_missing_date(self):
        ev = {"date": None, "time": None}
        rec = {"found": True, "start": "09:00", "end": "17:00", "date": "2026-11-20"}
        self.assertTrue(et._apply(ev, rec, today="2026-10-29"))
        self.assertEqual((ev["time"], ev["date"]), ("09:00–17:00", "2026-11-20"))

    def test_apply_refuses_placeholder_date(self):
        ev = {"date": None, "time": None}
        rec = {"found": True, "start": "09:00", "end": None, "date": "2035-09-07"}
        self.assertFalse(et._apply(ev, rec, today="2026-10-29"))

    def test_apply_with_prose_catalogue_date(self):
        ev = {"date": "1 December 2026", "time": None}
        rec = {"found": True, "start": "13:00", "end": "14:00", "date": "2026-12-01"}
        self.assertTrue(et._apply(ev, rec))
        self.assertEqual(ev["time"], "13:00–14:00")

    def test_coverage(self):
        self.assertEqual(et.coverage([{"time": "10:00"}, {"time": ""}, {}]),
                         {"with": 1, "without": 2, "pct": 33.3})


if __name__ == "__main__":
    unittest.main()
