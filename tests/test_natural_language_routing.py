"""A plain-language message runs a command only when it is clearly that request.

route_top_level_text ran commands silently, and upper-cased the whole
message to find lowercase tickers — so every English word became one:

  what's the latest version of python?   ran  /news WHAT
  write a report generator script        ran  /report WRITE
  plot a histogram of ages in data.csv   ran  /chart OF AGES GE IN CSV
  analyze this stack trace               ran  /analyze THIS
  strategy pattern in python             ran  /strategy pattern in python
  draw NVDA chart for 6 months           ran  /chart NVDA DRAW FOR 1y

Code and data-file messages now go to the model; a command that acts on one
instrument needs one; and a routed message says what it ran.
"""

from __future__ import annotations

import sys

import pytest

from aria_code.apps.cli.commands.market import route_top_level_text


@pytest.fixture(scope="module")
def commands():
    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli

    return set(cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG)).commands.commands)


@pytest.mark.parametrize("message", [
    "what's the latest version of python?", "show me recent changes in this repo",
    "write a report generator script", "fix the bug in the login page", "测试一下这个函数",
    "plot a histogram of ages in data.csv", "risk of this refactor?", "analyze this stack trace",
    "news app UI design ideas", "report the test coverage", "strategy pattern in python",
    "backtest my idea", "chart something", "Add tests for utils.py and run them",
])
def test_ordinary_messages_run_nothing(message, commands):
    assert route_top_level_text(message, commands) is None


@pytest.mark.parametrize("message, command", [
    ("analyze AAPL", "/analyze AAPL --lang en"),
    ("aapl news", "/news AAPL"),
    ("特斯拉最新消息", "/news TSLA"),
    ("今天有什么财经新闻", "/news 今天有什么财经新闻"),
    ("draw NVDA chart for 6 months", "/chart NVDA 6m"),
    ("画一下茅台的K线图", "/chart 600519 1y"),
    ("report on NVDA", "/report NVDA --type standard --format html"),
    ("risk TSLA", "/risk TSLA"),
])
def test_clear_requests_still_route(message, command, commands):
    routed = route_top_level_text(message, commands)
    assert routed is not None and routed.text == command


def test_logistics_requests_route_to_their_files(commands, tmp_path, monkeypatch):
    (tmp_path / "skus.csv").write_text("sku\n", encoding="utf-8")
    (tmp_path / "waybills.csv").write_text("id\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert route_top_level_text("Which SKUs in skus.csv need reordering?", commands).text == "/inventory skus.csv"
    assert route_top_level_text("carrier scorecard for waybills.csv", commands).text == "/carriers waybills.csv"
    assert route_top_level_text("reorder plan for missing.csv", commands) is None


def test_a_routed_message_says_what_it_ran(monkeypatch):
    import io

    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    console = Console(file=io.StringIO(), color_system=None)
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(cli, "HAS_RICH", True)
    cli._announce_route("/news AAPL")
    assert console.file.getvalue().strip() == "→ /news AAPL"


def test_the_interactive_loop_announces_its_route_too():
    """The input loop routes before send_message: "aapl news" ran /news with no line saying so."""
    import asyncio

    from aria_code.apps.cli.commands.market import try_top_level_route

    ran, said = [], []

    class Commands:
        commands = {"/news": None}

        async def execute(self, text):
            ran.append(text)

    assert asyncio.run(try_top_level_route("aapl news", Commands(), announce=said.append))
    assert said == ran == ["/news AAPL"]


@pytest.mark.parametrize("raw, shown", [
    ("Mon, 05 Oct 2026 12:00:00 GMT", "2026-10-05"),       # was "Mon, 05 Oc"
    ("2026-10-05T08:00:00Z", "2026-10-05"),
])
def test_news_dates_are_dates(raw, shown):
    from aria_code.apps.cli.commands.market_cmds import _news_date

    assert _news_date(raw) == shown


def test_the_chart_summary_follows_the_ui_language():
    import inspect

    from aria_code.apps.cli.commands import core_cmds

    source = inspect.getsource(core_cmds.CoreCommandsMixin.cmd_chart)
    assert "self-check passed" in source and '"偏多": "bullish"' in source
