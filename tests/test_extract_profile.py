"""The profile pass: provider selection, truncation, and shape coercion.

No network. The provider seam is faked at _create / _create_openai, which is
the lowest point that still exercises the response parsing, the usage
accounting and the breaker.
"""

import json
import os
import unittest
from unittest import mock

import ai_engine


class _Usage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _AnthropicResp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Usage(type="text", text=text)]
        self.stop_reason = stop_reason
        self.usage = _Usage(input_tokens=10, output_tokens=5,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)


class _OpenAIResp:
    def __init__(self, text, finish_reason="stop"):
        msg = _Usage(content=text)
        self.choices = [_Usage(message=msg, finish_reason=finish_reason)]
        self.usage = _Usage(prompt_tokens=10, completion_tokens=5,
                            prompt_tokens_details=_Usage(cached_tokens=4))


GOOD = {
    "input_kind": "cv", "person_name": "Ada", "confidence": 0.9,
    "document_type": "cv", "message_if_not_cv": "", "evidence_level": "rich",
    "field": "intelligence", "clarifying_questions": [],
    "profile": {"summary": "s", "strengths": ["a"], "gaps": ["b"],
                "interests": ["c"], "seniority": "mid", "aspiration": "x",
                "trajectory": "y"},
}


class ProviderSelectionTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    def test_falls_back_to_anthropic_without_a_key(self):
        """The default is openai; a deploy without the key must not break."""
        os.environ.pop("OPENAI_API_KEY", None)
        self.assertEqual(_provider_with("openai"), "anthropic")

    def test_uses_openai_when_configured(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertEqual(_provider_with("openai"), "openai")

    def test_anthropic_when_explicitly_selected(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertEqual(_provider_with("anthropic"), "anthropic")

    def test_model_follows_the_provider(self):
        self.assertEqual(ai_engine.extract_model("openai"), ai_engine.MODEL_EXTRACT_OPENAI)
        self.assertEqual(ai_engine.extract_model("anthropic"), ai_engine.MODEL_EXTRACT_ANTHROPIC)


class TruncationTests(unittest.TestCase):
    def setUp(self):
        _pin_anthropic(self)    # Claude path; an OPENAI_API_KEY must not reroute it

    def test_anthropic_max_tokens_raises(self):
        with _fake_anthropic(_AnthropicResp(json.dumps(GOOD), stop_reason="max_tokens")):
            with self.assertRaises(ai_engine.AITruncated):
                ai_engine._call("sys", "user", max_tokens=10, model="m")

    def test_openai_length_finish_raises(self):
        with _fake_openai(_OpenAIResp(json.dumps(GOOD), finish_reason="length")):
            with self.assertRaises(ai_engine.AITruncated):
                ai_engine._call_json("sys", "user", max_tokens=10,
                                     model="gpt-4o", provider="openai")

    def test_long_cv_keeps_head_and_tail(self):
        text = "HEAD" + ("x" * ai_engine.MAX_PROFILE_CHARS) + "TAIL"
        out = ai_engine._clip_for_profile(text)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertLessEqual(len(out), ai_engine.MAX_PROFILE_CHARS + 80)

    def test_short_cv_is_untouched(self):
        self.assertEqual(ai_engine._clip_for_profile("short"), "short")


class CoerceTests(unittest.TestCase):
    def test_profile_as_a_string_does_not_raise(self):
        """The crash that rendered a valid CV as 'not a CV'."""
        out = ai_engine._coerce_profile(dict(GOOD, profile="not an object"))
        self.assertEqual(out["profile"]["summary"], "")
        self.assertEqual(out["profile"]["strengths"], [])

    def test_bogus_seniority_is_dropped(self):
        out = ai_engine._coerce_profile(
            dict(GOOD, profile=dict(GOOD["profile"], seniority="Supreme Overlord")))
        self.assertEqual(out["profile"]["seniority"], "")

    def test_valid_seniority_survives_casing(self):
        out = ai_engine._coerce_profile(
            dict(GOOD, profile=dict(GOOD["profile"], seniority="SENIOR")))
        self.assertEqual(out["profile"]["seniority"], "senior")

    def test_unknown_input_kind_becomes_unusable(self):
        out = ai_engine._coerce_profile(dict(GOOD, input_kind="brochure"))
        self.assertEqual(out["input_kind"], "unusable")

    def test_confidence_is_clamped(self):
        self.assertEqual(ai_engine._coerce_profile(dict(GOOD, confidence=7))["confidence"], 1.0)
        self.assertEqual(ai_engine._coerce_profile(dict(GOOD, confidence="nonsense"))["confidence"], 0.0)

    def test_list_fields_coerce_to_strings(self):
        out = ai_engine._coerce_profile(
            dict(GOOD, profile=dict(GOOD["profile"], strengths=[1, None, "ok"])))
        self.assertEqual(out["profile"]["strengths"], ["1", "ok"])

    def test_non_dict_is_rejected(self):
        with self.assertRaises(ValueError):
            ai_engine._coerce_profile("a string")


class SchemaTests(unittest.TestCase):
    """strict json_schema has rules the API enforces and we cannot test
    against without a key: every object needs additionalProperties:false and
    must list every one of its properties in `required`. Check them here, or
    the first real call fails with a 400 nobody expected."""

    def test_schema_is_strict_mode_legal(self):
        problems = []

        def walk(node, path="profile_schema"):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object":
                if node.get("additionalProperties") is not False:
                    problems.append(f"{path}: additionalProperties must be False")
                props = set(node.get("properties") or {})
                required = set(node.get("required") or [])
                if props != required:
                    problems.append(
                        f"{path}: required {sorted(required)} != properties {sorted(props)}")
                for k, v in (node.get("properties") or {}).items():
                    walk(v, f"{path}.{k}")
            elif node.get("type") == "array":
                walk(node.get("items") or {}, f"{path}[]")

        walk(ai_engine.PROFILE_SCHEMA)
        self.assertEqual(problems, [])

    def test_schema_matches_the_values_the_code_accepts(self):
        prof = ai_engine.PROFILE_SCHEMA["properties"]["profile"]["properties"]
        self.assertEqual(set(prof["seniority"]["enum"]),
                         set(ai_engine.SENIORITY_VALUES) | {""})
        self.assertEqual(
            set(ai_engine.PROFILE_SCHEMA["properties"]["input_kind"]["enum"]),
            set(ai_engine.INPUT_KINDS))

    def test_openai_path_sends_the_schema_and_current_token_param(self):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            return _OpenAIResp(json.dumps(GOOD))

        with _patch("_create_openai", capture):
            ai_engine._call_json("sys", "user", max_tokens=4000, model="gpt-4o",
                                 provider="openai", schema=ai_engine.PROFILE_SCHEMA,
                                 schema_name="orbitcast_profile")
        self.assertEqual(seen["max_completion_tokens"], 4000)
        self.assertNotIn("max_tokens", seen)
        self.assertTrue(seen["response_format"]["json_schema"]["strict"])
        self.assertEqual(seen["messages"][0]["role"], "system")

    def test_anthropic_path_ignores_the_schema(self):
        """Claude has no server-side schema here; _coerce_profile covers it."""
        _pin_anthropic(self)
        seen = {}

        def capture(**kw):
            seen.update(kw)
            return _AnthropicResp(json.dumps(GOOD))

        with _patch("_create", capture):
            ai_engine._call_json("sys", "user", max_tokens=4000, model="claude-x",
                                 provider="anthropic", schema=ai_engine.PROFILE_SCHEMA)
        self.assertNotIn("response_format", seen)
        # The caller's 4000 is the answer; thinking gets headroom on top.
        self.assertEqual(seen["max_tokens"], 4000 + ai_engine.THINKING_HEADROOM)


class ThinkingBudgetTests(unittest.TestCase):
    """Current Claude models think by default and thinking counts against
    max_tokens. Scoring once spent all 4096 tokens thinking and returned
    nothing, twice, ~45s each. These pin the fix."""

    def setUp(self):
        _pin_anthropic(self)    # Claude path; an OPENAI_API_KEY must not reroute it

    def _seen(self, **call_kw):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            return _AnthropicResp(json.dumps({"ok": True}))

        with _patch("_create", capture):
            ai_engine._call("sys", "user", **call_kw)
        return seen

    def test_answer_budget_gets_thinking_headroom(self):
        seen = self._seen(max_tokens=4096, model="claude-sonnet-5")
        self.assertEqual(seen["max_tokens"], 4096 + ai_engine.THINKING_HEADROOM)

    def test_headroom_stays_inside_the_timeout(self):
        """At ~100 output tokens/s a ceiling past the timeout turns a long
        think into a timeout, and the SDK's retries multiply the wait."""
        worst = (4096 + ai_engine.THINKING_HEADROOM) / 100.0
        self.assertLess(worst, ai_engine.AI_TIMEOUT_S)

    def test_effort_is_sent_when_asked(self):
        seen = self._seen(max_tokens=10, model="claude-sonnet-5", effort="medium")
        self.assertEqual(seen["output_config"], {"effort": "medium"})

    def test_no_effort_means_no_output_config(self):
        self.assertNotIn("output_config", self._seen(max_tokens=10, model="claude-sonnet-5"))

    def test_effort_is_not_sent_to_models_that_reject_it(self):
        """Haiku 4.5 returns a 400 on output_config.effort."""
        seen = self._seen(max_tokens=10, model="claude-haiku-4-5-20251001", effort="medium")
        self.assertNotIn("output_config", seen)

    def test_scoring_and_critique_default_to_medium(self):
        self.assertEqual(ai_engine.EFFORT_SCORE, "medium")
        self.assertEqual(ai_engine.EFFORT_CRITIQUE, "medium")


class OrientationSpeedTests(unittest.TestCase):
    """The thin-profile briefing runs alongside scoring, so it sets the wait.
    It took ~105s at default effort with five searches."""

    def setUp(self):
        _pin_anthropic(self)    # Claude path; an OPENAI_API_KEY must not reroute it

    def _seen(self):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            return _AnthropicResp(json.dumps({"ok": True}))

        with _patch("_create", capture):
            ai_engine._call_with_search("sys", "user")
        return seen

    def test_runs_at_low_effort(self):
        self.assertEqual(self._seen()["output_config"], {"effort": "low"})

    def test_searches_fewer_times(self):
        tool = self._seen()["tools"][0]
        self.assertEqual(tool["max_uses"], 3)
        self.assertEqual(tool["type"], "web_search_20260209")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self._saved = json.loads(json.dumps(ai_engine.USAGE))

    def tearDown(self):
        ai_engine.USAGE.clear()
        ai_engine.USAGE.update(self._saved)

    def test_providers_are_counted_separately(self):
        # Deltas, not absolutes: a truncated call still spends tokens and
        # still records them, so earlier tests legitimately leave counts here.
        before = json.loads(json.dumps(ai_engine.USAGE["by_model"]))
        prior = before.get("openai:gpt-4o", {}).get("cache_read", 0)
        ai_engine._note_usage(_AnthropicResp("{}"), "anthropic", "claude-x")
        ai_engine._note_usage(_OpenAIResp("{}"), "openai", "gpt-4o")
        by = ai_engine.USAGE["by_model"]
        self.assertIn("anthropic:claude-x", by)
        self.assertIn("openai:gpt-4o", by)
        self.assertEqual(by["openai:gpt-4o"]["cache_read"] - prior, 4)
        # Anthropic's response carries no cached_tokens field; it must not
        # inherit OpenAI's reading of one.
        self.assertEqual(by["anthropic:claude-x"]["cache_read"], 0)


class BreakerTests(unittest.TestCase):
    def setUp(self):
        ai_engine._outage.clear()

    def tearDown(self):
        ai_engine._outage.clear()

    def test_openai_billing_does_not_pause_anthropic(self):
        """Two accounts, two breakers - the whole reason this is per-provider."""
        exc = RuntimeError("insufficient_quota: exceeded your current quota")
        exc.status_code = 429
        ai_engine._note_account_error(exc, "openai")
        self.assertIsNotNone(ai_engine.outage("openai"))
        self.assertIsNone(ai_engine.outage("anthropic"))

    def test_bare_outage_reports_any_open_breaker(self):
        """app.py and /api/health call outage() with no argument."""
        exc = RuntimeError("Your credit balance is too low")
        exc.status_code = 400
        ai_engine._note_account_error(exc, "anthropic")
        o = ai_engine.outage()
        self.assertEqual(o["provider"], "anthropic")

    def test_a_plain_rate_limit_is_not_an_account_error(self):
        exc = RuntimeError("429 Too Many Requests, please slow down")
        exc.status_code = 429
        ai_engine._note_account_error(exc, "openai")
        self.assertIsNone(ai_engine.outage("openai"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _pin_anthropic(test):
    """Route this test's model calls to the Claude path, undone afterwards."""
    patcher = mock.patch.object(ai_engine, "AI_PROVIDER", "anthropic")
    patcher.start()
    test.addCleanup(patcher.stop)


def _provider_with(value):
    saved = ai_engine.EXTRACT_PROVIDER
    ai_engine.EXTRACT_PROVIDER = value
    try:
        return ai_engine.extract_provider()
    finally:
        ai_engine.EXTRACT_PROVIDER = saved


class _patch:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.saved = getattr(ai_engine, self.name)
        setattr(ai_engine, self.name, self.value)

    def __exit__(self, *a):
        setattr(ai_engine, self.name, self.saved)


def _fake_anthropic(resp):
    return _patch("_create", lambda **kw: resp)


def _fake_openai(resp):
    return _patch("_create_openai", lambda **kw: resp)


if __name__ == "__main__":
    unittest.main()
