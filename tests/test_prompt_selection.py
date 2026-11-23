"""Every provider must run under the same rules, not just Ollama.

CODING_SYSTEM_PROMPT was built inside ollama_stream and nowhere else, so a
cloud model reached the API with an empty system prompt. It showed exactly as
the missing rules predict: given a failing test, Gemini narrated a plan, read
the test file, said "now I will read calc.py" and stopped — no tool call, no
edit. It was never told that describing an edit is not making one.
"""

import inspect
import unittest

from aria_code.apps.cli.prompts.select import build_turn_system_prompt, select_base_prompt


class BasePromptSelectionTests(unittest.TestCase):
    def test_a_coding_message_gets_the_tool_discipline(self):
        prompt = select_base_prompt("这个项目的测试挂了，找出原因并修好")
        self.assertIn("ABSOLUTE RULES", prompt)
        self.assertIn("NEVER say", prompt)

    def test_a_coding_message_is_told_orientation_and_verification(self):
        prompt = select_base_prompt("修好 paginate.py 里的 off-by-one")
        self.assertIn("repo_map", prompt)
        self.assertIn("VERIFICATION IS AUTOMATIC", prompt)

    def test_a_market_message_gets_the_analysis_prompt_not_the_coding_one(self):
        prompt = select_base_prompt("分析 $AAPL")
        self.assertIn("quantitative finance", prompt)

    def test_every_intent_gets_some_prompt(self):
        # Returning "" for an intent is how the original bug worked: the model
        # reached the API knowing nothing about what it was or what it had.
        for message in ("什么是市盈率", "...???...", "hello", "帮我想个名字"):
            with self.subTest(message=message):
                self.assertTrue(select_base_prompt(message).strip())

    def test_a_general_turn_is_still_told_not_to_claim_unverified_work(self):
        prompt = select_base_prompt("什么是市盈率")
        self.assertIn("never claim to have read a file", prompt)

    def test_selection_never_raises(self):
        for message in ("", "   ", "\n", "🙂", "x" * 5000):
            with self.subTest(message=message[:12]):
                self.assertIsInstance(select_base_prompt(message), str)


class TurnPromptTests(unittest.TestCase):
    def test_an_explicit_override_wins_outright(self):
        # Callers that set one have already decided what the model is told;
        # appending the general rules would contradict them.
        self.assertEqual(
            build_turn_system_prompt("修好这个 bug", override="ONLY THIS"),
            "ONLY THIS",
        )

    def test_project_context_is_appended_not_substituted(self):
        built = build_turn_system_prompt("修好这个 bug", project_context="# Repo: demo")
        self.assertIn("ABSOLUTE RULES", built)
        self.assertIn("# Repo: demo", built)

    def test_an_active_pack_contributes_its_guidance(self):
        from aria_code.packs import BaseDomainPack, EntityMatch, register_pack, unregister_pack

        class _DemoPack(BaseDomainPack):
            name = "_test_prompt_guidance_pack"
            description = "Synthetic pack for prompt guidance test"

            def resolve_entities(self, message: str):
                if "$AAPL" in message:
                    return (EntityMatch(pack=self.name, kind="ticker", value="AAPL", surface="$AAPL", confidence=0.95),)
                return ()

            def prompt_fragment(self, activation):
                return "金融标的已识别: " + ", ".join(m.value for m in activation.entities)

        register_pack(_DemoPack())
        try:
            built = build_turn_system_prompt("分析 $AAPL 的走势")
            self.assertIn("金融标的已识别", built)
        finally:
            unregister_pack("_test_prompt_guidance_pack")

    def test_a_code_turn_is_never_handed_market_guidance(self):
        built = build_turn_system_prompt("修好 paginate.py 的 off-by-one")
        self.assertNotIn("金融标的已识别", built)

    def test_a_blank_override_does_not_suppress_the_rules(self):
        built = build_turn_system_prompt("修好这个 bug", override="   ")
        self.assertIn("ABSOLUTE RULES", built)


class ConfiguredPathWiringTests(unittest.TestCase):
    """The fix only counts if the cloud path actually receives it."""

    def test_the_configured_provider_is_given_a_built_prompt(self):
        from aria_code.apps.cli.providers import runtime_bridge

        source = inspect.getsource(runtime_bridge.make_provider_fn)
        self.assertIn("_system_for(prompt)", source)
        self.assertNotIn(
            "ConfiguredProvider(config, model, system_override=system_override)",
            source,
            "the cloud path is back to passing the bare override",
        )

    def test_the_builder_is_per_message(self):
        # The right prompt depends on what was asked, so it cannot be hoisted
        # out of the per-turn closure.
        from aria_code.apps.cli.providers import runtime_bridge

        source = inspect.getsource(runtime_bridge.make_provider_fn)
        self.assertIn("def _system_for(prompt: str)", source)


if __name__ == "__main__":
    unittest.main()
