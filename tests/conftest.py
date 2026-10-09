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


# ── 与开发者真实主目录隔离 ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_user_directories(tmp_path_factory, monkeypatch):
    """Keep every test out of the developer's real home directory.

    build_session_diagnostic_bundle falls back to artifacts.artifact_summary(),
    which rglobs artifact_root() — by default ``~/Documents/Aria Code``. On a
    machine where Documents is iCloud-backed, the first walk blocks while the
    placeholders materialise: measured at 251s here against 0.0s once warm.

    That is why four TestSessionManager cases failed intermittently, why the
    whole suite occasionally took 17 or 27 minutes instead of 100 seconds, and
    why they always passed when re-run. The tests were reading whatever the
    person running them happened to have generated.

    autouse and session-scoped roots: the point is that no test can reach the
    real directories, not that these four remember to opt in. A test that wants
    a specific root still sets its own — monkeypatch.setenv here is overridden
    by a later setenv in the test itself.
    """
    base = tmp_path_factory.mktemp("isolated-home")
    for var in ("ARIA_ARTIFACT_ROOT", "ARIA_USER_OUTPUT_ROOT"):
        monkeypatch.setenv(var, str(base / var.lower()))
    # ARIA_HOME covers config/sessions/credentials for the same reason: an
    # earlier round of this work found tests writing to the real brokers.json.
    monkeypatch.setenv("ARIA_HOME", str(base / "aria_home"))
    # The task ledger lives in ~/.aria, not ARIA_HOME. Tests that spawned tasks
    # with no runner registered left them there as pending, a few at a time.
    monkeypatch.setenv("ARIA_TASK_LEDGER_PATH", str(base / "task_ledger.json"))


@pytest.fixture(autouse=True)
def _clear_market_caches():
    """Start every test with empty market-data caches.

    market_handlers keeps the indicators and price history it fetched for a
    symbol for five to ten minutes, keyed by symbol alone. A test that feeds a
    fake AAPL history therefore hands it to the next test that asks for AAPL:
    test_market_snapshot_en drew its key levels from test_output_rendering's
    data whenever the two ran in one process. Cleared only when the module is
    already loaded, so tests that never touch it don't pay for the import.
    """
    handlers = sys.modules.get("aria_code.apps.cli.handlers.market_handlers")
    if handlers is not None:
        handlers._TA_SESSION_CACHE.clear()
        handlers._LEVEL_HISTORY_CACHE.clear()


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


# ── Terminal snapshots ────────────────────────────────────────────────────────

SNAPSHOT_DIR = pathlib.Path(__file__).parent / "snapshots"


class _Snapshot:
    """Compare terminal output with tests/snapshots/<name>.txt.

    ARIA_UPDATE_SNAPSHOTS=1 writes the file instead, for a change to the
    output that is meant. A missing file is written and the test fails, so a
    new snapshot is always looked at once before it is trusted.
    """

    @staticmethod
    def console(width: int = 100):
        import io
        from rich.console import Console

        return Console(file=io.StringIO(), width=width, color_system=None, force_terminal=False,
                       legacy_windows=False, emoji=False, highlight=False, soft_wrap=False)

    @staticmethod
    def normalise(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines) + "\n"

    def __call__(self, name: str, text: str) -> None:
        import os

        path = SNAPSHOT_DIR / f"{name}.txt"
        got = self.normalise(text)
        if os.environ.get("ARIA_UPDATE_SNAPSHOTS") == "1" or not path.exists():
            created = not path.exists()
            SNAPSHOT_DIR.mkdir(exist_ok=True)
            path.write_text(got, encoding="utf-8")
            if created and os.environ.get("ARIA_UPDATE_SNAPSHOTS") != "1":
                pytest.fail(f"new snapshot {path.name} written; check it and run again")
            return
        want = path.read_text(encoding="utf-8")
        if got != want:
            import difflib

            diff = "".join(difflib.unified_diff(want.splitlines(True), got.splitlines(True),
                                                f"snapshots/{path.name}", "this run"))
            pytest.fail(f"terminal output changed:\n{diff}\n"
                        "If the change is meant, rerun with ARIA_UPDATE_SNAPSHOTS=1.")


@pytest.fixture
def snapshot():
    return _Snapshot()
