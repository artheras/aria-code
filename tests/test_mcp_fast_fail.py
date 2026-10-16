"""A server that exits at startup is reported at once, with its own explanation."""
import asyncio
import logging
import sys
import time

import pytest

from aria_code.mcp_client import MCPServer


def _client(code: str, timeout: float = 30.0) -> MCPServer:
    return MCPServer("probe", sys.executable, ["-c", code], timeout=timeout)


@pytest.mark.asyncio
async def test_a_server_that_exits_immediately_fails_the_handshake_at_once_and_says_why(caplog):
    client = _client("import sys; sys.stderr.write('configure ARIA_MERCHANT_TOKEN first\\n'); sys.exit(1)")
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="aria_code.mcp_client"):
        ok = await client.start()
    elapsed = time.monotonic() - started
    assert ok is False
    assert elapsed < 5, f"waited {elapsed:.1f}s for a server that had already exited"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "exited" in messages and "configure ARIA_MERCHANT_TOKEN first" in messages and "exit 1" in messages


@pytest.mark.asyncio
async def test_a_server_that_exits_after_the_handshake_fails_later_requests_at_once():
    code = (
        "import sys, json\n"
        "line = sys.stdin.readline()\n"
        "req = json.loads(line)\n"
        "print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'protocolVersion':'2025-06-18','capabilities':{}}}), flush=True)\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"  # the initialized notification, then tools/list
        "sys.exit(0)\n"
    )
    client = _client(code)
    started = time.monotonic()
    assert await client.start() is True  # handshake answered; tools/list is lost to the exit
    assert time.monotonic() - started < 5
    result = await client.call_tool("anything", {})
    assert result["success"] is False


@pytest.mark.asyncio
async def test_a_healthy_server_is_unaffected():
    code = (
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if 'id' not in req: continue\n"
        "    if req['method'] == 'tools/list':\n"
        "        result = {'tools': [{'name': 'ping'}]}\n"
        "    else:\n"
        "        result = {'protocolVersion': '2025-06-18', 'capabilities': {}}\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}), flush=True)\n"
    )
    client = _client(code)
    try:
        assert await client.start() is True
        assert [t["name"] for t in client.tools] == ["ping"]
    finally:
        await client.stop()
