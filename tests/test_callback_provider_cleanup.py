import asyncio

import pytest

from aria_code.apps.cli.providers.base import _stream_callback_provider


@pytest.mark.asyncio
async def test_probe_deadline_cancels_callback_request():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def invoke(*callbacks):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    async def consume():
        async for _ in _stream_callback_provider(invoke, done_provider="test"):
            pass

    request = asyncio.create_task(consume())
    await started.wait()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(request, timeout=0.01)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_closing_stream_after_token_stops_callback_request():
    stopped = asyncio.Event()

    async def invoke(on_token, *callbacks):
        on_token("READY")
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    stream = _stream_callback_provider(invoke, done_provider="test")
    assert (await stream.__anext__()).text == "READY"
    await stream.aclose()
    assert stopped.is_set()
