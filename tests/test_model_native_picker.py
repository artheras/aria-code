"""/model under the Rust frontend opens the host's menu instead of printing a list.

The worker's stdin is a pipe, so the TTY arrow picker cannot run there, and the
fallback was a numbered list the user had to type a number back from. The host
already draws choice menus (arrow keys, Enter, Esc); /model now uses it.
"""
from __future__ import annotations

import unittest
from unittest import mock

from aria_code.apps.cli.commands import model_cmds

CATALOGUE = {
    "a": {"id": "google/gemini-3.5-flash", "name": "Gemini Flash", "badge": "", "description": "stable default"},
    "b": {"id": "qwen2.5:7b", "name": "Qwen 2.5", "badge": "", "description": "bilingual workhorse"},
}


class _Console:
    def __init__(self):
        self.lines = []

    def print(self, text=""):
        self.lines.append(str(text))


class _Mixin(model_cmds.ModelCommandsMixin):
    def __init__(self):
        self.context = mock.Mock(console=_Console(), has_rich=True)
        self.set_ids: list = []
        self.routed: list = []

    def _set_model_by_id(self, model_id, provider=None):
        self.set_ids.append(model_id)

    async def cmd_model(self, args):
        self.routed.append(args)


async def _call(mixin, select, *, current="qwen2.5:7b", provider="ollama", community=()):
    rich = [{"name": n, "execution": "local"} for n in community]
    with mock.patch.object(model_cmds, "_get_MODELS", lambda: CATALOGUE):
        return await mixin._pick_model_in_host(
            select, picker_title="Select model", current_id=current, current_provider=provider,
            rich_models=rich, aria_ids={m["id"] for m in CATALOGUE.values()},
            status_tag=lambda mid, badge: "●", short_desc=lambda m: m["description"], lang="en",
        )


class NativePickerTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_model_is_one_row_with_the_current_one_marked_and_preselected(self):
        seen = {}

        def select(options, start, title, **kwargs):
            seen.update(options=options, start=start, title=title, kwargs=kwargs)
            return -1

        await _call(_Mixin(), select, community=["llama3.2:3b"])
        labels = [label for label, _help in seen["options"]]
        self.assertEqual(len(labels), 3)  # two catalogue models and one community model
        self.assertIn("current", labels[1])  # Qwen 2.5 is current
        self.assertNotIn("current", labels[0])
        self.assertEqual(seen["start"], 1, "the menu should open on the current model")
        # The id leads the detail, so entries that share a display name can be told apart.
        self.assertEqual(seen["options"][0][1], "google/gemini-3.5-flash · stable default")
        self.assertEqual(seen["title"], "Select model")
        # The frontend is asked for a searchable picker with the models grouped.
        self.assertTrue(seen["kwargs"]["picker"])
        self.assertEqual(seen["kwargs"]["groups"], ["Aria catalogue", "Aria catalogue", "Community models (Ollama)"])

    async def test_choosing_a_local_model_sets_it(self):
        mixin = _Mixin()
        await _call(mixin, lambda o, s, t, **kw: 2, community=["llama3.2:3b"])
        self.assertEqual(mixin.set_ids, ["llama3.2:3b"])
        self.assertEqual(mixin.routed, [])

    async def test_choosing_a_provider_model_goes_through_the_credential_checking_route(self):
        mixin = _Mixin()
        await _call(mixin, lambda o, s, t, **kw: 0)
        self.assertEqual(mixin.routed, ["google/gemini-3.5-flash"])
        self.assertEqual(mixin.set_ids, [], "setting it directly would skip the API key / Vertex check")

    async def test_cancelling_changes_nothing_and_says_so(self):
        mixin = _Mixin()
        await _call(mixin, lambda o, s, t, **kw: -1)
        self.assertEqual((mixin.set_ids, mixin.routed), ([], []))
        self.assertTrue(any("unchanged" in line for line in mixin.context.console.lines))

    async def test_an_interrupted_or_invalid_menu_is_a_cancel_not_a_crash(self):
        for failure in (KeyboardInterrupt(), ValueError("bad menu response")):
            mixin = _Mixin()

            def select(o, s, t, failure=failure, **kw):
                raise failure

            await _call(mixin, select)
            self.assertEqual((mixin.set_ids, mixin.routed), ([], []))

    async def test_an_out_of_range_answer_is_ignored(self):
        mixin = _Mixin()
        await _call(mixin, lambda o, s, t, **kw: 99)
        self.assertEqual((mixin.set_ids, mixin.routed), ([], []))


if __name__ == "__main__":
    unittest.main()
