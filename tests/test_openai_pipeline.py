"""The whole pipeline on OpenAI (OC_AI_PROVIDER=openai, the default since
31 Oct 2026). No network: the OpenAI seam is faked at _create_openai and
_create_openai_search, the lowest points that still exercise routing, the
request shape, response parsing and usage accounting."""

import json
import os
import time
import unittest
from unittest import mock

import ai_engine


class _U:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _chat(payload, finish="stop"):
    return _U(choices=[_U(message=_U(content=json.dumps(payload)), finish_reason=finish)],
              usage=_U(prompt_tokens=100, completion_tokens=20,
                       prompt_tokens_details=_U(cached_tokens=80)))


class _OnOpenAI(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(ai_engine, "AI_PROVIDER", "openai"),
                        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def capture(self, name, reply):
        seen = []

        def fake(**kw):
            seen.append(kw)
            return reply
        p = mock.patch.object(ai_engine, name, fake)
        p.start()
        self.addCleanup(p.stop)
        return seen


class RoutingTests(_OnOpenAI):
    def test_call_goes_to_openai_with_the_openai_model(self):
        seen = self.capture("_create_openai", _chat({"ok": True}))
        claude = self.capture("_create", None)
        ai_engine._call("sys", "user", max_tokens=50, model="claude-sonnet-5")
        self.assertEqual(len(seen), 1)
        self.assertEqual(claude, [])                           # Claude never called
        self.assertEqual(seen[0]["model"], ai_engine.MODEL_OPENAI)

    def test_blocks_are_flattened_catalogue_first(self):
        """Scoring sends content blocks; OpenAI wants a string. The stable
        catalogue must stay first or OpenAI's automatic prefix cache misses."""
        seen = self.capture("_create_openai", _chat({"ok": True}))
        ai_engine._call([{"type": "text", "text": "RULES"}],
                        [{"type": "text", "text": "CATALOGUE", "cache_control": {"type": "ephemeral"}},
                         {"type": "text", "text": "PERSON"}], max_tokens=50)
        msgs = seen[0]["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "RULES\n\nRespond with a single JSON object."})
        self.assertTrue(msgs[1]["content"].startswith("CATALOGUE"))
        self.assertIn("PERSON", msgs[1]["content"])

    def test_json_mode_prompt_always_mentions_json(self):
        """json_object mode 400s without the word JSON in the messages."""
        seen = self.capture("_create_openai", _chat({"ok": True}))
        ai_engine._call("Score these.", "events", max_tokens=50)
        self.assertIn("json", seen[0]["messages"][0]["content"].lower())

    def test_schema_makes_it_strict(self):
        seen = self.capture("_create_openai", _chat({"recommendations": []}))
        ai_engine._call("sys json", "u", max_tokens=50,
                        schema=ai_engine.SCORING_SCHEMA, schema_name="orbitcast_scores")
        rf = seen[0]["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertTrue(rf["json_schema"]["strict"])

    def test_truncation_is_a_failure_not_a_short_answer(self):
        self.capture("_create_openai", _chat({"recommendations": []}, finish="length"))
        with self.assertRaises(ai_engine.AITruncated):
            ai_engine._call("json", "u", max_tokens=5)

    def test_configured_without_an_anthropic_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertTrue(ai_engine.ai_configured())


class SchemaShapeTests(unittest.TestCase):
    """Strict mode rules the API enforces: every object closed, every property
    required. Broken here means a 400 on every analysis."""

    def _problems(self, node, path):
        out = []
        if isinstance(node, dict) and node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                out.append(f"{path}: additionalProperties must be False")
            if set(node.get("properties") or {}) != set(node.get("required") or []):
                out.append(f"{path}: required != properties")
            for k, v in (node.get("properties") or {}).items():
                out += self._problems(v, f"{path}.{k}")
        elif isinstance(node, dict) and node.get("type") == "array":
            out += self._problems(node.get("items") or {}, f"{path}[]")
        return out

    def test_scoring_schema(self):
        self.assertEqual(self._problems(ai_engine.SCORING_SCHEMA, "scoring"), [])

    def test_critique_schema(self):
        self.assertEqual(self._problems(ai_engine.CRITIQUE_SCHEMA, "critique"), [])

    def test_scoring_schema_matches_the_prompt(self):
        """The prompt's footer and the schema describe the same fields."""
        fields = ai_engine.SCORING_SCHEMA["properties"]["recommendations"]["items"]["properties"]
        for f in fields:
            self.assertIn(f'"{f}"', ai_engine.SCORING_FOOTER)


class HonestyStillEnforcedTests(_OnOpenAI):
    """Moving providers must not move the rules: whatever GPT returns, the
    code still drops sub-65 scores and ids that are not in the catalogue."""

    def test_score_events_filters_gpt_output(self):
        events = [{"id": "a", "title": "A", "date": "2099-01-01", "category": "Tech & AI"},
                  {"id": "b", "title": "B", "date": "2099-01-02", "category": "Tech & AI"}]
        rec = dict(title="t", why="w", why_now="n", prepare="p", benefit="b", weakness="x")
        reply = {"recommendations": [
            dict(rec, event_id="a", fit_score=80),
            dict(rec, event_id="b", fit_score=50),          # below 65
            dict(rec, event_id="ghost", fit_score=95),      # not in the catalogue
        ]}
        seen = self.capture("_create_openai", _chat(reply))
        with mock.patch.object(ai_engine, "rag", None), \
             mock.patch.object(ai_engine, "get_scoring_rules", return_value="RULES"):
            recs = ai_engine.score_events({"summary": "s"}, events)
        self.assertEqual([r["event_id"] for r in recs], ["a"])
        self.assertEqual(seen[0]["response_format"]["json_schema"]["name"], "orbitcast_scores")


class BriefingTests(_OnOpenAI):
    def test_briefing_searches_the_web_from_london(self):
        reply = _U(status="completed", output_text=json.dumps({"field_reality": "x"}),
                   usage=_U(input_tokens=50, output_tokens=10,
                            input_tokens_details=_U(cached_tokens=0)))
        seen = self.capture("_create_openai_search", reply)
        out = ai_engine._call_with_search("sys", "user")
        self.assertEqual(out, {"field_reality": "x"})
        tool = seen[0]["tools"][0]
        self.assertEqual(tool["type"], "web_search")
        self.assertEqual(tool["user_location"]["country"], "GB")

    def test_incomplete_briefing_is_an_error(self):
        reply = _U(status="incomplete", incomplete_details={"reason": "max_output_tokens"},
                   output_text="", usage=None)
        self.capture("_create_openai_search", reply)
        with self.assertRaises(ValueError):
            ai_engine._call_with_search("sys", "user")

    def test_results_do_not_wait_for_a_slow_briefing(self):
        """The whole point: matches go out once scoring is done."""
        def slow_briefing(field, profile):
            time.sleep(1.5)
            return {"field_reality": "late"}

        with mock.patch.object(ai_engine, "build_orientation", slow_briefing), \
             mock.patch.object(ai_engine, "score_events", return_value=[]), \
             mock.patch.object(ai_engine, "critique_recommendations", return_value=[]), \
             mock.patch.object(ai_engine, "ORIENTATION_WAIT_S", 0.05):
            t0 = time.monotonic()
            out = ai_engine.score_and_enrich({"aspiration": "intel"}, "thin", "intelligence", [])
            took = time.monotonic() - t0
        self.assertLess(took, 1.0)
        self.assertIsNone(out["orientation"])

    def test_a_fast_briefing_is_still_included(self):
        with mock.patch.object(ai_engine, "build_orientation", return_value={"field_reality": "ok"}), \
             mock.patch.object(ai_engine, "score_events", return_value=[]), \
             mock.patch.object(ai_engine, "critique_recommendations", return_value=[]):
            out = ai_engine.score_and_enrich({}, "thin", "intelligence", [])
        self.assertEqual(out["orientation"], {"field_reality": "ok"})


class UsageTests(unittest.TestCase):
    def test_responses_api_usage_is_counted(self):
        before = json.loads(json.dumps(ai_engine.USAGE["by_model"].get("openai:t", {})))
        ai_engine._note_usage(_U(usage=_U(input_tokens=7, output_tokens=3,
                                          input_tokens_details=_U(cached_tokens=2))), "openai", "t")
        row = ai_engine.USAGE["by_model"]["openai:t"]
        self.assertEqual(row["input"] - before.get("input", 0), 7)
        self.assertEqual(row["output"] - before.get("output", 0), 3)
        self.assertEqual(row["cache_read"] - before.get("cache_read", 0), 2)


if __name__ == "__main__":
    unittest.main()
