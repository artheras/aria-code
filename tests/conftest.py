"""
tests/conftest.py — 共享 fixtures 和 SSE mock 辅助类
=====================================================
供 test_provider_integration.py / test_streaming_pipeline.py 使用。
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import List

import pytest

# 确保 apps/cli 在 Python 路径上
_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)


# ── SSE mock 基础结构 ─────────────────────────────────────────────────────────

class FakeSSEContent:
    """
    模拟 aiohttp response.content 的 async iterable。
    每个 line str 被编码为 bytes + 换行，供 provider.stream() 的
    `async for raw in resp.content:` 循环消费。
    """

    def __init__(self, lines: List[str]):
        self._chunks = [(line + "\n").encode("utf-8") for line in lines]

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk


class FakeSSEResp:
    """
    模拟 aiohttp ClientResponse（POST response）。
    用于 `async with sess.post(...) as resp:` 上下文。
    """

    def __init__(self, lines: List[str], status: int = 200):
        self.status  = status
        self.content = FakeSSEContent(lines)
        self._body   = "\n".join(lines)

    async def text(self) -> str:
        """HTTP 错误时 provider 会调用 await resp.text() 读取响应体。"""
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSSESession:
    """
    模拟 aiohttp.ClientSession。
    用于 `async with aiohttp.ClientSession() as sess:` 上下文。
    """

    def __init__(self, resp: FakeSSEResp):
        self._resp = resp

    def post(self, *args, **kwargs) -> FakeSSEResp:
        """返回 FakeSSEResp（本身是 async ctx mgr）。"""
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def make_sse_mock():
    """
    工厂 fixture — 用法：

        fake_session = make_sse_mock(lines, status=200)
        with patch.object(aiohttp, 'ClientSession', return_value=fake_session):
            ...
    """
    def _factory(lines: List[str], status: int = 200) -> FakeSSESession:
        return FakeSSESession(FakeSSEResp(lines, status=status))
    return _factory


# ── providers.json 临时文件辅助 ───────────────────────────────────────────────

def make_providers_file(tmp_path: pathlib.Path, llm_section: dict) -> pathlib.Path:
    """
    在 tmp_path 下写一个 providers.json，包含 {"llm": llm_section}。
    返回该文件路径，可用于 patch providers.llm.registry._CONFIG_PATHS。

    示例:
        p = make_providers_file(tmp, {"deepseek": {"api_key": "sk-xxx"}})
        with patch.object(_reg, "_CONFIG_PATHS", [p]):
            ...
    """
    p = tmp_path / "providers.json"
    p.write_text(json.dumps({"llm": llm_section}), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _isolate_video_engine_registry():
    """Give every test a clean VideoEngineRegistry.

    It is a process-wide singleton, and configuring an engine mutates it for
    everything that runs afterwards. test_video_engines.py configures a custom
    engine and makes it active; test_video_session.py then patches the
    "google" engine while the director resolves the *active* one and calls the
    custom engine instead. Each passed alone and the pair failed, which is the
    hardest shape of failure to place.
    """
    try:
        from aria_code.video_workflow.engines import VideoEngineRegistry
    except Exception:  # the video extra is optional
        yield
        return
    VideoEngineRegistry.reset_instance()
    try:
        yield
    finally:
        VideoEngineRegistry.reset_instance()
