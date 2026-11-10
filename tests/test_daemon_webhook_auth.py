"""The daemon's TradingView webhook endpoint authenticates like a secret should.

It compared the token with ``==`` (a timing side channel), also took it from
``?token=`` / ``?secret=`` (where it lands in proxy and access logs), and when
the open-intake guard failed to import it skipped the guard entirely.
"""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest

aiohttp = pytest.importorskip("aiohttp")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    from aria_code import aria_daemon
    import apps.cli.tradingview_bridge as bridge

    queued = []
    monkeypatch.setattr(bridge, "enqueue_tradingview_alert",
                        lambda payload, db_path=None: queued.append(payload) or {"success": True})
    monkeypatch.setattr(aria_daemon, "_DB_PATH", tmp_path / "daemon.db", raising=False)
    port = _free_port()
    monkeypatch.setenv("ARIA_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("ARIA_WEBHOOK_PORT", str(port))
    monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("ARIA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("WEBHOOK_TOKEN", "s3cret-token")
    return aria_daemon, f"http://127.0.0.1:{port}/api/v1/webhook/tradingview", queued


async def _serve(aria_daemon, url):
    task = asyncio.create_task(aria_daemon._webhook_http_server())
    for _ in range(50):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url.rsplit("/api", 1)[0] + "/"):
                    return task
        except aiohttp.ClientConnectorError:
            await asyncio.sleep(0.05)
    task.cancel()
    raise AssertionError("webhook server did not start")


async def _post(url, *, headers=None):
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data='{"symbol": "AAPL"}', headers=headers or {}) as resp:
            return resp.status


async def test_header_token_is_accepted_and_query_token_is_not(daemon):
    aria_daemon, url, queued = daemon
    task = await _serve(aria_daemon, url)
    try:
        assert await _post(url, headers={"X-Aria-Token": "s3cret-token"}) == 202
        assert await _post(url, headers={"Authorization": "Bearer s3cret-token"}) == 202
        assert await _post(url + "?token=s3cret-token") == 401
        assert await _post(url + "?secret=s3cret-token") == 401
        assert await _post(url, headers={"X-Aria-Token": "wrong"}) == 401
        assert len(queued) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_missing_open_intake_guard_refuses(daemon, monkeypatch):
    aria_daemon, url, queued = daemon
    monkeypatch.delenv("WEBHOOK_TOKEN")
    monkeypatch.setitem(sys.modules, "apps.channels.intake", None)   # import fails
    task = await _serve(aria_daemon, url)
    try:
        assert await _post(url) == 503
        assert queued == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_token_comparison_is_constant_time():
    import inspect

    from aria_code import aria_daemon

    source = inspect.getsource(aria_daemon._webhook_http_server)
    assert "compare_digest" in source
    assert "supplied == token" not in source
    assert 'request.query.get("token")' not in source
