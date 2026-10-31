"""Three changes from 31 Oct 2026, after a real attendance record was read
as a person and matched to events, and a CV came back with only 3 results.

- Uploads must be a CV, resume or LinkedIn profile, checked three ways.
- The scorer sees each event's write-up and THIS person's most related
  events, not 100 events chosen by date and judged by title alone.
- Results reach five with near misses that are labelled as such and keep
  their real scores - never by inflating a weak match into a strong one.

No network: the model seams are faked."""

import json
import os
import unittest
from unittest import mock

import ai_engine

CV_TEXT = ("Jane Doe\nExperience\nThreat intelligence analyst, 2019-2024\n"
           "Education\nMSc Cyber Security\nSkills\nOSINT, malware triage")
ATTENDANCE_TEXT = ("Student attendance report\nName: Jane Doe\nModule: Digital Forensics\n"
                   "Week 1 Present\nWeek 2 Absent\nWeek 3 Present\nOverall 66%")


def _profile_reply(**over):
    base = {"input_kind": "cv", "person_name": "Jane Doe", "confidence": 0.9,
            "document_type": "cv_resume", "message_if_not_cv": "", "evidence_level": "rich",
            "field": "cyber threat intelligence", "clarifying_questions": [],
            "profile": {"summary": "s", "strengths": ["OSINT"], "gaps": ["g"],
                        "interests": ["threat intel"], "seniority": "mid",
                        "aspiration": "", "trajectory": ""}}
    base.update(over)
    return base


class FileGateTests(unittest.TestCase):
    def _extract(self, text, reply, source="file"):
        with mock.patch.object(ai_engine, "_call_json", return_value=reply):
            return ai_engine.extract_profile(text, source)

    def test_a_cv_is_read(self):
        out = self._extract(CV_TEXT, _profile_reply())
        self.assertTrue(out["is_cv"])
        self.assertFalse(out["rejected_document"])

    def test_a_linkedin_export_is_read(self):
        out = self._extract(CV_TEXT, _profile_reply(document_type="linkedin_profile"))
        self.assertTrue(out["is_cv"])

    def test_an_attendance_record_is_refused_and_named(self):
        """The real case. The model is told SOURCE: uploaded file and names it."""
        reply = _profile_reply(input_kind="unusable", document_type="other_document",
                               message_if_not_cv="This is an attendance record, not a CV.")
        out = self._extract(ATTENDANCE_TEXT, reply)
        self.assertFalse(out["is_cv"])
        self.assertTrue(out["rejected_document"])
        self.assertIn("attendance record", out["message_if_not_cv"])
        self.assertIn("only reads CVs", out["message_if_not_cv"])
        self.assertEqual(out["profile"]["strengths"], [])         # no profile built from it

    def test_refused_even_if_the_model_calls_it_a_person(self):
        """Old behaviour: 'there is a person in it' was enough. Not for files."""
        reply = _profile_reply(input_kind="self_description", document_type="self_description",
                               evidence_level="thin")
        self.assertFalse(self._extract(ATTENDANCE_TEXT, reply)["is_cv"])

    def test_refused_if_the_model_is_unsure(self):
        out = self._extract(CV_TEXT, _profile_reply(confidence=0.3))
        self.assertTrue(out["rejected_document"])

    def test_refused_if_the_text_has_no_cv_section(self):
        """The check that does not depend on the model at all."""
        out = self._extract(ATTENDANCE_TEXT, _profile_reply())       # model wrongly says CV
        self.assertTrue(out["rejected_document"])

    def test_typed_words_keep_the_generous_rule(self):
        reply = _profile_reply(input_kind="self_description", document_type="self_description",
                               evidence_level="thin")
        out = self._extract("I'm a psychology student who wants to get into intelligence",
                            reply, source="text")
        self.assertTrue(out["is_cv"])

    def test_the_model_is_told_the_source(self):
        seen = {}

        def capture(system, user_content, **kw):
            seen["user"] = user_content
            return _profile_reply()
        with mock.patch.object(ai_engine, "_call_json", capture):
            ai_engine.extract_profile(CV_TEXT, "file")
        self.assertTrue(seen["user"].startswith("SOURCE: uploaded file"))

    def test_unknown_document_types_count_as_not_a_cv(self):
        out = ai_engine._coerce_profile(_profile_reply(document_type="spreadsheet"))
        self.assertEqual(out["document_type"], "other_document")
        self.assertEqual(ai_engine._coerce_profile(_profile_reply(document_type="cv"))["document_type"],
                         "cv_resume")

    def test_schema_offers_only_the_known_types(self):
        enum = ai_engine.PROFILE_SCHEMA["properties"]["document_type"]["enum"]
        self.assertEqual(set(enum), set(ai_engine.DOCUMENT_TYPES))


def _event(eid, title, category="Tech & AI", date="2099-01-01", description=""):
    return {"id": eid, "title": title, "category": category, "date": date,
            "format": "", "location": "London", "description": description,
            "url": f"https://example.org/{eid}", "source": "SecretSource", "emoji": "x"}


class RelevanceTests(unittest.TestCase):
    PROFILE = {"field": "digital forensics", "interests": ["malware analysis"],
               "strengths": ["incident response"], "summary": "Forensics analyst"}

    def test_related_events_rank_first(self):
        evs = [_event("a", "Startup pitch night"),
               _event("b", "Digital forensics and incident response meetup"),
               _event("c", "Malware analysis workshop")]
        ranked = [e["id"] for e in ai_engine.rank_for_profile(evs, self.PROFILE)]
        self.assertEqual(ranked[:2], ["b", "c"])
        self.assertNotIn("a", ranked)                   # shares nothing, so not ranked at all

    def test_a_relevant_event_beyond_the_date_cut_is_still_seen(self):
        """The old cut took the soonest per category, so a perfect event a
        few weeks out could be invisible to the scorer."""
        soon = [_event(f"s{i}", f"Generic networking evening {i}", date=f"2099-01-{i % 28 + 1:02d}")
                for i in range(200)]
        late = _event("late", "Digital forensics practitioners day", date="2099-12-01")
        chosen = ai_engine.select_catalog(soon + [late], cap=40, profile=self.PROFILE)
        self.assertIn("late", [e["id"] for e in chosen])
        self.assertEqual(len(chosen), 40)

    def test_without_a_profile_selection_is_unchanged(self):
        evs = [_event(f"e{i}", f"Event {i}") for i in range(10)]
        self.assertEqual(ai_engine.select_catalog(evs, cap=50), evs)

    def test_writeups_reach_the_scorer(self):
        with mock.patch("event_details.summaries",
                        return_value={"b": "A hands-on DFIR session. Suits: analysts"}):
            out = ai_engine._with_writeups([_event("a", "A"), _event("b", "B")])
        self.assertEqual(out[1]["description"], "A hands-on DFIR session. Suits: analysts")
        self.assertEqual(out[0]["description"], "")

    def test_writeup_failure_never_breaks_scoring(self):
        with mock.patch("event_details.summaries", side_effect=RuntimeError("db down")):
            out = ai_engine._with_writeups([_event("a", "A")])
        self.assertEqual(out[0]["id"], "a")


class _Chat:
    def __init__(self, payload):
        msg = type("M", (), {"content": json.dumps(payload)})()
        self.choices = [type("C", (), {"message": msg, "finish_reason": "stop"})()]
        self.usage = None


class NearMissTests(unittest.TestCase):
    EVENTS = [_event(x, f"Event {x}") for x in "abcdefgh"]

    def setUp(self):
        for p in (mock.patch.object(ai_engine, "AI_PROVIDER", "openai"),
                  mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                  mock.patch.object(ai_engine, "rag", None),
                  mock.patch.object(ai_engine, "get_scoring_rules", return_value="RULES"),
                  mock.patch("event_details.summaries", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def _score(self, reply):
        seen = []

        def fake(**kw):
            seen.append(kw)
            return _Chat(reply)
        with mock.patch.object(ai_engine, "_create_openai", fake):
            out = ai_engine.score_events({"summary": "s"}, self.EVENTS)
        self.seen = seen
        return out

    def test_near_misses_are_validated_like_matches(self):
        rec = dict(title="t", why="w", why_now="n", prepare="p", benefit="b", weakness="x")
        near_item = dict(why="w", why_not="n")
        recs, near = self._score({
            "recommendations": [dict(rec, event_id="a", fit_score=80)],
            "near_misses": [dict(near_item, event_id="b", fit_score=60),
                            dict(near_item, event_id="c", fit_score=70),       # 65+: not a near miss
                            dict(near_item, event_id="d", fit_score=40),       # under the floor
                            dict(near_item, event_id="a", fit_score=60),       # already a match
                            dict(near_item, event_id="ghost", fit_score=60),   # not in the catalogue
                            dict(near_item, event_id="e", fit_score=55)]})
        self.assertEqual([r["event_id"] for r in recs], ["a"])
        self.assertEqual([n["event_id"] for n in near], ["b", "e"])
        self.assertTrue(all(n["band"] == "Below the bar" for n in near))

    def test_scorer_never_sees_urls_or_source_names(self):
        self._score({"recommendations": [], "near_misses": []})
        sent = self.seen[0]["messages"][1]["content"]
        self.assertNotIn("SecretSource", sent)
        self.assertNotIn("https://example.org", sent)

    def test_results_fill_to_five_and_matches_stay_matches(self):
        recs = [dict(event_id=x, fit_score=s, why="w") for x, s in (("a", 80), ("b", 72), ("c", 66))]
        near = [dict(event_id=x, fit_score=s, why="w", why_not="n", band="Below the bar")
                for x, s in (("d", 62), ("e", 58), ("f", 52))]
        with mock.patch.object(ai_engine, "score_events", return_value=(recs, near)), \
             mock.patch.object(ai_engine, "critique_recommendations", side_effect=lambda p, r: r):
            out = ai_engine.score_and_enrich({}, "rich", "", self.EVENTS)
        self.assertEqual([r["event_id"] for r in out["recommendations"]], ["a", "b", "c"])
        self.assertEqual([n["event_id"] for n in out["near_misses"]], ["d", "e"])     # 3 + 2 = 5
        self.assertTrue(all(r["fit_score"] >= 65 for r in out["recommendations"]))

    def test_five_real_matches_need_no_near_misses(self):
        recs = [dict(event_id=x, fit_score=80, why="w") for x in "abcde"]
        near = [dict(event_id="f", fit_score=60, why="w", why_not="n", band="Below the bar")]
        with mock.patch.object(ai_engine, "score_events", return_value=(recs, near)), \
             mock.patch.object(ai_engine, "critique_recommendations", side_effect=lambda p, r: r):
            out = ai_engine.score_and_enrich({}, "rich", "", self.EVENTS)
        self.assertEqual(out["near_misses"], [])

    def test_a_scoring_failure_shows_no_near_misses(self):
        with mock.patch.object(ai_engine, "score_events", side_effect=RuntimeError("down")):
            out = ai_engine.score_and_enrich({}, "rich", "", self.EVENTS)
        self.assertTrue(out["scoring_failed"])
        self.assertEqual(out["near_misses"], [])


if __name__ == "__main__":
    unittest.main()
