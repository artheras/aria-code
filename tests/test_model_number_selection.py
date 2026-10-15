"""The numbers /models prints are the numbers /model accepts.

Under the Rust frontend the Python worker's stdin is a pipe, so it can never
read keystrokes and the arrow picker cannot run there — the numbered list is
the right output, not a fallback to be removed. What was wrong is that the
list numbered every row with something that did nothing, showed a display
name you could not type, and then told you to use an id it never printed.

So the number has to resolve, and the printer and the resolver have to agree
about what it counts.
"""
from __future__ import annotations

import unittest
from unittest import mock

from aria_code.apps.cli.commands import model_cmds


class _Console:
    def __init__(self):
        self.lines = []

    def print(self, text=""):
        self.lines.append(str(text))


class _Context:
    def __init__(self):
        self.console = _Console()
        # True so messages route to the console above rather than stdout.
        self.has_rich = True


class _Mixin(model_cmds.ModelCommandsMixin):
    """The mixin alone, with only what the number path touches."""

    def __init__(self):
        self.context = _Context()
        self.lang = "en"


ARIA = {
    "a": {"id": "google/gemini-3.5-flash", "name": "Gemini"},
    "b": {"id": "deepseek/deepseek-chat", "name": "DeepSeek"},
}


class ListedOrderTest(unittest.TestCase):
    def setUp(self):
        self.mixin = _Mixin()

    def test_the_order_is_the_aria_models_then_the_community_ones(self):
        with mock.patch.object(model_cmds, "_get_MODELS", lambda: ARIA):
            self.mixin._last_listed_community = ["qwen3:8b"]
            self.assertEqual(
                self.mixin._listed_model_ids(),
                ["google/gemini-3.5-flash", "deepseek/deepseek-chat", "qwen3:8b"],
            )

    def test_a_community_model_already_listed_is_not_counted_twice(self):
        # Two rows for one model would make every number after it wrong.
        with mock.patch.object(model_cmds, "_get_MODELS", lambda: ARIA):
            self.mixin._last_listed_community = ["deepseek/deepseek-chat", "qwen3:8b"]
            self.assertEqual(self.mixin._listed_model_ids().count("deepseek/deepseek-chat"), 1)

    def test_without_a_listing_it_is_just_the_aria_models(self):
        with mock.patch.object(model_cmds, "_get_MODELS", lambda: ARIA):
            self.assertEqual(len(self.mixin._listed_model_ids()), 2)


class NumberResolvesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mixin = _Mixin()
        self.mixin._last_listed_community = ["qwen3:8b"]

    async def resolve(self, typed: str):
        """Run cmd_model far enough to see what the number became."""
        seen = {}

        async def stop_at_name(_self, args):
            seen["name"] = args
            return None

        with mock.patch.object(model_cmds, "_get_MODELS", lambda: ARIA), \
             mock.patch.object(model_cmds, "retired_replacement", lambda _n: None, create=True):
            # Everything past resolution is provider plumbing; capture the
            # resolved name by letting the retirement lookup observe it.
            with mock.patch("aria_code.apps.cli.model_retirement.retired_replacement",
                            side_effect=lambda n: seen.setdefault("name", n) and None):
                try:
                    await self.mixin.cmd_model(typed)
                except Exception:
                    pass
        return seen.get("name")

    async def test_a_number_becomes_the_id_on_that_row(self):
        self.assertEqual(await self.resolve("2"), "deepseek/deepseek-chat")

    async def test_the_first_row_is_one_not_zero(self):
        self.assertEqual(await self.resolve("1"), "google/gemini-3.5-flash")

    async def test_a_community_row_resolves_too(self):
        self.assertEqual(await self.resolve("3"), "qwen3:8b")

    async def test_a_number_past_the_end_says_so_and_changes_nothing(self):
        await self.mixin.cmd_model("99")
        printed = " ".join(self.mixin.context.console.lines)
        self.assertIn("99", printed)
        self.assertIn("/models", printed)

    async def test_zero_is_out_of_range_rather_than_the_last_row(self):
        # Negative indexing would silently pick something.
        await self.mixin.cmd_model("0")
        self.assertIn("/models", " ".join(self.mixin.context.console.lines))


class HintTest(unittest.TestCase):
    def test_the_hint_offers_what_the_screen_actually_shows(self):
        source = model_cmds.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Use /model <number> from this list", text)
        # The old hint named only an id the list never printed.
        self.assertNotIn("Use /model <id> to switch. E.g.", text)


if __name__ == "__main__":
    unittest.main()
