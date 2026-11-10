"""Tool rounds go to Gemini as function calls and responses, not as prose.

Rendering them as text ("[read_file] ...", "## Tool Results ...") taught
gemini-3.5-flash to write its next calls — and their results — as text: in
eval runs it "read" files and reported passing tests without anything having
run, and the turn ended as a success with no file changed.
"""

import asyncio
import base64
import unittest
from types import SimpleNamespace

try:
    from google.genai import types  # noqa: F401

    HAS_GENAI = True
except ImportError:  # optional dependency
    HAS_GENAI = False

from aria_code.runtime import build_tool_followup

SIGNATURE = base64.b64encode(b"opaque-signature").decode("ascii")


def _tool_round(signature=SIGNATURE):
    """History of one tool round, shaped the way runtime.agent_loop records it."""
    results = [{"tool": "read_file", "result": "def f(): pass"}]
    call = {
        "function": {"name": "read_file", "arguments": {"path": "lots.py"}},
        "id": "call-1",
    }
    if signature:
        call["thought_signature"] = signature
    return [
        {"role": "user", "content": "fix lots.py"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "name": "read_file", "content": "def f(): pass"},
        {"role": "user", "content": build_tool_followup(results)},
    ]


@unittest.skipUnless(HAS_GENAI, "google-genai not installed")
class VertexToolHistoryTests(unittest.TestCase):
    def _provider(self, model="gemini-3.5-flash"):
        from aria_code.apps.cli.providers.vertexai_stream import VertexAIProvider

        return VertexAIProvider(model=model, config={})

    def test_request_never_ends_on_a_model_turn(self):
        # Gemini answers 400 "Requests ending with a model turn are not
        # supported" when the last turn is the model's.
        call = {"function": {"name": "read_file", "arguments": {"path": "a.py"}},
                "id": "call-1", "thought_signature": SIGNATURE}
        unanswered = [{"role": "user", "content": "fix a.py"},
                      {"role": "assistant", "content": "", "tool_calls": [call]},
                      {"role": "user", "content": ""}]
        trailing_text = [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "Hello"},
                         {"role": "user", "content": "   "}]
        for messages in (unanswered, trailing_text):
            contents, _ = self._provider()._messages_to_contents(messages)
            self.assertEqual(contents[-1].role, "user", messages)
        contents, _ = self._provider()._messages_to_contents(unanswered)
        self.assertEqual(contents[-1].parts[0].function_response.response, {"error": "not executed"})

    def test_tool_round_is_sent_as_function_call_and_response(self):
        contents, _ = self._provider()._messages_to_contents(_tool_round())

        self.assertEqual([c.role for c in contents], ["user", "model", "user"])
        call_part = contents[1].parts[0]
        self.assertEqual(call_part.function_call.name, "read_file")
        self.assertEqual(call_part.function_call.args, {"path": "lots.py"})
        self.assertEqual(call_part.function_call.id, "call-1")
        self.assertEqual(call_part.thought_signature, b"opaque-signature")

        response_part = contents[2].parts[0]
        self.assertEqual(response_part.function_response.name, "read_file")
        self.assertEqual(response_part.function_response.id, "call-1")
        self.assertEqual(response_part.function_response.response, {"result": "def f(): pass"})

    def test_text_copy_of_results_is_not_sent_alongside_native_ones(self):
        contents, _ = self._provider()._messages_to_contents(_tool_round())

        texts = [p.text for c in contents for p in c.parts if p.text]
        self.assertFalse(any("## Tool Results" in t for t in texts))
        self.assertFalse(any("[read_file]" in t for t in texts))
        # The guidance after the results is kept.
        self.assertTrue(any(t.startswith("All tools completed successfully.") for t in texts))

    def test_loop_guard_directives_after_the_results_survive(self):
        history = _tool_round()
        history[-1]["content"] += "\n\nDo not retry with the same arguments."
        contents, _ = self._provider()._messages_to_contents(history)

        last_texts = [p.text for p in contents[-1].parts if p.text]
        self.assertTrue(any("Do not retry with the same arguments." in t for t in last_texts))

    def test_unsigned_calls_fall_back_to_text_on_gemini_3(self):
        # History recorded before signatures were kept: Gemini 3 would reject
        # the replayed call, so the old text rendering is used instead.
        contents, _ = self._provider()._messages_to_contents(_tool_round(signature=None))

        parts = [p for c in contents for p in c.parts]
        self.assertFalse(any(p.function_call or p.function_response for p in parts))
        self.assertTrue(any(p.text and "[read_file]" in p.text for p in parts))

    def test_unsigned_calls_stay_native_on_gemini_2(self):
        contents, _ = self._provider("gemini-2.5-flash")._messages_to_contents(
            _tool_round(signature=None))

        self.assertIsNotNone(contents[1].parts[0].function_call)
        self.assertIsNone(contents[1].parts[0].thought_signature)

    def test_every_call_gets_a_response_even_when_the_batch_was_cut_short(self):
        history = _tool_round()
        history[1]["tool_calls"].append({
            "function": {"name": "run_command", "arguments": {"command": "pytest"}},
        })
        contents, _ = self._provider()._messages_to_contents(history)

        responses = [p.function_response for p in contents[2].parts if p.function_response]
        self.assertEqual([r.name for r in responses], ["read_file", "run_command"])
        self.assertEqual(responses[1].response, {"error": "not executed"})

    def test_stream_keeps_call_id_and_signature(self):
        from aria_code.apps.cli.providers.base import LLMDone, LLMToolCall

        part = SimpleNamespace(
            function_call=SimpleNamespace(name="read_file", args={"path": "lots.py"}, id="call-9"),
            thought_signature=b"sig",
        )
        chunk = SimpleNamespace(
            text=None,
            function_calls=[part.function_call],
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
            usage_metadata=None,
        )

        async def fake_stream(**_kwargs):
            async def gen():
                yield chunk
            return gen()

        provider = self._provider()
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=fake_stream)))

        async def collect():
            return [e async for e in provider.stream([{"role": "user", "content": "hi"}], tools=[])]

        events = asyncio.run(collect())
        call = next(e for e in events if isinstance(e, LLMToolCall))
        self.assertEqual(call.call_id, "call-9")
        self.assertEqual(base64.b64decode(call.thought_signature), b"sig")
        done = events[-1]
        self.assertIsInstance(done, LLMDone)
        self.assertEqual(done.tool_calls_pending[0]["thought_signature"], call.thought_signature)


if __name__ == "__main__":
    unittest.main()
