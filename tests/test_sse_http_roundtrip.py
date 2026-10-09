"""Actual HTTP → deferred local tool → HTTP continuation acceptance.

The server only requests a tool. A local permission-aware handler supplies
the result, whose call id and contents must survive the next HTTP request.
"""
import json

import pytest
from aiohttp import web

from aria_code.apps.cli.providers.llm.sse_stream import stream_chat
from aria_code.apps.cli.tools.file_tools import tool_read_file


@pytest.mark.asyncio
@pytest.mark.parametrize("react", [False, True])
async def test_tool_result_returns_to_backend_with_correct_local_contents(tmp_path, react):
    source = tmp_path / "app.py"
    source.write_text("def answer():\n    return 42\n")
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append(payload)
        assert payload["tool_protocol"] == "aria-local-v1"
        assert payload["tool_execution"] == "client"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [{"type": "status", "tool_protocol": "aria-local-v1", "tool_execution": "client"}]
        if len(requests) == 1:
            events.append({"type": "tool_call", "tool": "read_file", "params": {"path": str(source)}})
        else:
            history = payload["history" if react else "conversation_history"]
            assert history[-1]["role"] == "tool"
            assert history[-1]["tool_call_id"] == history[-2]["tool_calls"][0]["id"]
            assert "return 42" in history[-1]["content"]
            events.append({"type": "delta", "text": "已读取本地文件，答案是 42。"})
        for event in events:
            # CRLF is valid SSE, including the multi-byte Chinese response.
            await response.write(("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/api/v2/chat/react" if react else "/api/v2/ai/chat/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    kwargs = {"local_tool_execution": True, "tool_schemas": [{"name": "read_file"}],
              "use_react_gateway": react}
    try:
        first = await stream_chat(url, "read app.py", [], **kwargs)
        assert first["success"] and first["tool_calls_pending"]
        # The actual file tool, not the HTTP server, reads the host's file.
        local = tool_read_file({"path": str(source), "_workspace": str(tmp_path),
                                "permission_mode": "read-only"})
        assert local["success"], local
        history = [{"role": "user", "content": "read app.py"},
                   {"role": "assistant", "content": "", "tool_calls": [{
                       "function": {"name": "read_file", "arguments": {"path": str(source)}}}]},
                   {"role": "tool", "name": "read_file", "content": json.dumps(local)}]
        second = await stream_chat(url, "continue", history, **kwargs)
        assert second["success"] and second["response"] == "已读取本地文件，答案是 42。"
        assert not second["tool_calls_pending"] and len(requests) == 2
        assert source.read_text() == "def answer():\n    return 42\n"
    finally:
        await runner.cleanup()
