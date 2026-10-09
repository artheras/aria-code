"""Provider events are read whichever import root built them.

The CLI's modules import under two roots, ``aria_code.apps.cli…`` and the bare
``apps.cli…``, and each root had its own copy of every event class.
runtime_bridge builds providers from the bare root; the stream consumers
checked events against the packaged root with isinstance. Every event failed
the check, so a provider streamed "ready", the consumer saw nothing, and the
turn ended as empty_response — for every chat turn through ConfiguredProvider
(OpenAI-compatible endpoints, LM Studio, the registry providers, Gemini via a
gcloud login). This file imports base only from the bare root on purpose.
"""

from __future__ import annotations

import asyncio

import apps.cli.providers.base as bare
import aria_code.packages.aria_sdk.streaming as streaming


def test_the_two_roots_now_hold_the_same_classes():
    # aria_code/__init__.py aliases the bare root onto the packaged one; the
    # stream below is still read whichever root built it.
    assert bare.LLMDone is streaming.LLMDone


def test_a_stream_from_the_bare_root_is_read():
    class Provider:
        async def stream(self, messages, tools, cancel_event=None):
            yield bare.LLMToken(text="ready")
            yield bare.LLMToolCall(tool="write_file", params={"path": "fx.py"})
            yield bare.LLMDone(response="", provider="google")

    result = asyncio.run(streaming.stream_provider_result(Provider(), "hi", []))
    assert result["success"] and result["response"] == "ready" and result["provider"] == "google"
    assert result["tool_calls_pending"] == [{"tool": "write_file", "params": {"path": "fx.py"}}]
