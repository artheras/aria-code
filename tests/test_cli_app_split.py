try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
import json
from types import SimpleNamespace

import pytest

from aria_code.apps.cli.commands.catalog import DIRECT_COMMAND_MAP, VISIBLE_SLASH_COMMANDS
from aria_code.apps.cli.commands.market_context import build_analyze_context, build_analyze_prompt
from aria_code.apps.cli.message_processing import context_compaction_decision, estimate_message_tokens
from aria_code.apps.cli.intent_router import build_intent_route
from aria_code.apps.cli.market_universe import resolve_market_symbol
from aria_code.apps.cli.commands.market import (
    parse_analysis_args,
    parse_symbols,
    parse_technical_args,
    route_top_level_text,
    sanitize_chart_symbol_args,
    try_top_level_route,
)
from aria_code.apps.cli.commands.market_cmds import _is_probable_football_query, _parse_nl_team_pair, _rss_items_from_xml
from aria_code.apps.cli.handlers.strategy_advice import handle_strategy_advice, is_strategy_advice_request
from aria_code.ui.render.market import compact_quote_market_cap, render_quote_plain, render_ta_plain
from aria_code.apps.cli.utils.market_detect import (
    _detect_broker_type,
    _extract_market_symbols,
    _is_broker_guide_intent,
    _is_broker_setup_intent,
)
from aria_code.apps.cli.commands.report import (
    apply_report_quality_gate,
    all_agents_failed,
    build_markdown_report_prompt,
    clean_markdown_report_response,
    export_report_pdf,
    generate_html_report,
    markdown_data_block,
    parse_report_args,
    report_agent_names,
    report_agent_health,
    report_file_size_kb,
    save_markdown_report,
    update_report_index,
)
from aria_code.apps.cli.commands.team import (
    build_team_agent_data,
    build_team_market_context,
    build_team_report_markdown,
    clean_team_synthesis_text,
    parse_team_args,
    resolve_team_symbols,
    run_team_analysis,
    save_team_report,
    team_quote_snapshot,
    team_agent_names,
)
from aria_code.ui.render.team import build_team_table_rows, render_team_rows_plain, team_mode_label, truncate_cell
from aria_code.apps.cli.direct import dispatch_direct_command, is_watchable_direct_command


class _FakeCommands:
    def __init__(self):
        self.calls = []

    async def cmd_quote(self, args):
        self.calls.append(("quote", args))

    async def cmd_ashare(self, args):
        self.calls.append(("ashare", args))

    def cmd_doctor(self, args):
        self.calls.append(("doctor", args))


class _FakeTerminal:
    def __init__(self):
        self.commands = _FakeCommands()
        self.prompts = []

    async def run_prompt(self, prompt, **kwargs):
        self.prompts.append((prompt, kwargs))


@pytest.mark.asyncio
async def test_direct_dispatch_handles_async_and_sync_commands():
    terminal = _FakeTerminal()

    assert await dispatch_direct_command(terminal, "quote", "AAPL") is True
    assert await dispatch_direct_command(terminal, "doctor", "--network") is True
    assert await dispatch_direct_command(terminal, "ashare", "status") is True

    assert terminal.commands.calls == [("quote", "AAPL"), ("doctor", "--network"), ("ashare", "status")]
    assert terminal.prompts == []


@pytest.mark.asyncio
async def test_direct_dispatch_falls_back_to_prompt_for_unknown_command():
    terminal = _FakeTerminal()

    handled = await dispatch_direct_command(
        terminal,
        "compare",
        "AAPL NVDA",
        json_output=True,
        fmt="json",
        output_file="out.json",
        quiet=True,
    )

    assert handled is False
    assert terminal.prompts == [
        (
            "compare AAPL NVDA",
            {
                "json_output": True,
                "fmt": "json",
                "output_file": "out.json",
                "quiet": True,
            },
        )
    ]


def test_context_compaction_decision_uses_incoming_prompt_pressure():
    messages = [{"role": "user", "content": "x" * 3000} for _ in range(8)]

    assert estimate_message_tokens(messages) == 8000
    decision = context_compaction_decision(
        messages,
        model_key="qwen2.5-coder:1.5b",
        extra_content="y" * 1200,
        threshold=0.78,
    )

    assert decision["should_compact"] is True
    assert decision["fill_pct"] >= 78


def test_strategy_advice_request_is_answered_without_artifact_side_effects():
    assert is_strategy_advice_request("如果我要写一个美股量化策略，你觉得要从几个角度去写")
    assert not is_strategy_advice_request("开始生成一个美股量化策略文件")

    result = handle_strategy_advice("如果我要写一个美股量化策略，你觉得要从几个角度去写")

    assert result["success"] is True
    assert result["analysis_complete"] is True
    assert result["tools_used"] == ["strategy_advice"]
    assert "不需要先写文件" in result["response"]
    assert "请明确说" in result["response"]


def test_rss_news_parser_extracts_public_news_items():
    xml = """
    <rss><channel>
      <item>
        <title>Apple &amp; market update</title>
        <link>https://example.com/a</link>
        <pubDate>Sat, 20 Jun 2026 01:23:00 GMT</pubDate>
        <source>Example News</source>
      </item>
    </channel></rss>
    """

    items = _rss_items_from_xml(xml, limit=5)

    assert items == [{
        "title": "Apple & market update",
        "url": "https://example.com/a",
        "published_at": "Sat, 20 Jun 2026 01:23:00 GMT",
        "source": "Example News",
    }]


def test_context_compaction_decision_stays_quiet_for_small_sessions():
    messages = [{"role": "user", "content": "x" * 500} for _ in range(3)]

    decision = context_compaction_decision(
        messages,
        model_key="qwen2.5-coder:1.5b",
        threshold=0.50,
    )

    assert decision["should_compact"] is False
    assert decision["message_count"] == 3


def test_football_intent_does_not_capture_stock_volume_query():
    query = "分析lvmh股票和成交量"
    pair = _parse_nl_team_pair(query)

    assert _is_probable_football_query(query, pair) is False


def test_football_intent_does_not_capture_ticker_vs_ticker_query():
    query = "AAPL vs NVDA谁赢"
    pair = _parse_nl_team_pair(query)

    assert _is_probable_football_query(query, pair) is False


def test_football_intent_still_accepts_real_match_query():
    query = "葡萄牙和波兰的比赛比分预测"
    pair = _parse_nl_team_pair(query)

    assert pair is not None
    assert _is_probable_football_query(query, pair) is True


def test_cli_catalog_exposes_watchable_direct_commands_and_visible_help():
    assert DIRECT_COMMAND_MAP["watchlist"].method_name == "cmd_watch"
    assert DIRECT_COMMAND_MAP["tv"].method_name == "cmd_tv"
    assert DIRECT_COMMAND_MAP["a-share"].method_name == "cmd_ashare"
    assert is_watchable_direct_command("quote") is True
    assert is_watchable_direct_command("backtest") is False
    assert "/packages" in VISIBLE_SLASH_COMMANDS
    assert "/positions" in VISIBLE_SLASH_COMMANDS
    assert "/upload-image" in VISIBLE_SLASH_COMMANDS
    assert "/tv" in VISIBLE_SLASH_COMMANDS
    assert "/ashare" in VISIBLE_SLASH_COMMANDS


@pytest.mark.asyncio
async def test_football_nl_query_uses_parser_after_mixin_rebind(monkeypatch):
    import aria_cli

    calls = []

    class FakeTerminal:
        pass

    commands = aria_cli.SlashCommands(FakeTerminal())

    async def fake_predict(home, away, league):
        calls.append((home, away, league))

    monkeypatch.setattr(commands, "_football_predict", fake_predict)

    await commands.cmd_football("葡萄牙和波兰的比赛比分预测")

    assert calls == [("葡萄牙", "波兰", "wc")]


def test_console_script_points_to_apps_cli_entrypoint():
    with open("pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)

    assert data["project"]["scripts"]["aria-code"] == "aria_code.apps.cli.main:main"
    # apps/ is no longer a top-level package: since the src-layout migration the
    # single "aria_code*" entry covers it and every other subpackage.
    assert "aria_code*" in data["tool"]["setuptools"]["packages"]["find"]["include"]


def test_market_slash_commands_are_registered_for_interactive_routing():
    import aria_cli

    commands = aria_cli.SlashCommands(SimpleNamespace(config={}))

    for text in (
        "/chart MC.PA 1y",
        "/dashboard brief",
        "/report AAPL",
        "/quote SPCX",
        "/analyze SPCX",
        "/ta SPCX",
        "/market",
        "/news SPCX",
    ):
        assert commands.is_command(text), text


def test_market_command_parsers_are_ui_free_and_stable():
    assert parse_symbols("", ["aapl", "nvda"]) == ["AAPL", "NVDA"]
    assert parse_symbols("aapl msft", ["NVDA"]) == ["AAPL", "MSFT"]

    parsed = parse_analysis_args("Apple volume")
    assert parsed.symbol == "AAPL"
    assert parsed.focus == "volume"
    assert parsed.lang == ""
    parsed_zh = parse_analysis_args("苹果 成交量")
    assert parsed_zh.symbol == "AAPL"
    assert parsed_zh.focus == "volume"
    assert parsed_zh.lang == "zh"
    assert _extract_market_symbols("analysis Apple volume DATA QUOTE YAHOO STOOQ") == ["AAPL"]
    assert sanitize_chart_symbol_args(["AAPL", "MA", "TTM", "INC", "BELOW", "ABOVE"]) == ["AAPL"]
    assert sanitize_chart_symbol_args(["AAPL", "MA"]) == ["AAPL", "MA"]
    assert sanitize_chart_symbol_args(["TTM"]) == ["TTM"]

    assert parse_technical_args("NVDA days=60").symbol == "NVDA"
    assert parse_technical_args("NVDA days=60").days == 60
    assert parse_technical_args("TSLA --days 90").days == 90
    assert parse_technical_args("--days=30").symbol == "AAPL"
    assert parse_technical_args("--days=30").days == 30
    assert parse_technical_args("AAPL days=bad").days == 120


def test_top_level_market_router_maps_bare_text_to_slash_commands():
    available = {"/analyze", "/backtest", "/report"}

    routed = route_top_level_text("分析 AAPL", available)
    assert routed is not None
    assert routed.text == "/analyze AAPL --lang zh"

    routed = route_top_level_text("analysis Apple volume", available)
    assert routed is not None
    assert routed.text == "/analyze AAPL --focus volume --lang en"

    routed = route_top_level_text("backtest momentum NVDA", available)
    assert routed is not None
    assert routed.text == "/backtest momentum NVDA"

    assert route_top_level_text("/analyze AAPL", available) is None
    assert route_top_level_text("market", available) is None


def test_top_level_market_router_maps_kline_artifacts_to_chart_service():
    available = {"/chart", "/dashboard", "/report"}

    routed = route_top_level_text("生成lvmh的k线和分析数据", available)
    assert routed is not None
    assert routed.command == "/chart"
    assert routed.text == "/chart MC.PA 1y"


def test_top_level_market_router_maps_tradingview_open_to_tv_command():
    routed = route_top_level_text("用 TradingView 打开英伟达", {"/tv", "/chart"})

    assert routed is not None
    assert routed.command == "/tv"
    assert routed.text == "/tv NVDA --open"


def test_top_level_market_router_maps_tradingview_open_and_bullish_analysis():
    routed = route_top_level_text("用 trading view打开苹果并且根据其的数据你觉得哪些数据看涨", {"/tv", "/chart"})

    assert routed is not None
    assert routed.command == "/tv"
    assert routed.text == "/tv AAPL --open --bullish"


def test_top_level_market_router_maps_tradingview_strategy_to_pine_export():
    routed = route_top_level_text("生成英伟达 TradingView 策略", {"/tv", "/chart"})

    assert routed is not None
    assert routed.command == "/tv"
    assert routed.text == "/tv NVDA --pine"


def test_broad_broker_discovery_is_guide_intent_not_specific_setup():
    assert _is_broker_guide_intent("分析如何和各个券商连接并且使用这个项目的各个服务")
    assert _detect_broker_type("分析如何和各个券商连接并且使用这个项目的各个服务") == ""

    assert _is_broker_setup_intent("帮我配置富途 OpenAPI")
    assert _detect_broker_type("帮我配置富途 OpenAPI") == "futu"


def test_top_level_market_router_maps_tradingview_pine_followup_options():
    routed = route_top_level_text("生成英伟达 TradingView 策略并复制到剪贴板打开所在目录生成文本副本", {"/tv", "/chart"})

    assert routed is not None
    assert routed.command == "/tv"
    assert routed.text == "/tv NVDA --pine --copy --reveal --txt"


def test_top_level_market_router_does_not_treat_atvi_as_tv_command():
    routed = route_top_level_text("分析 ATVI 股票", {"/tv", "/analyze"})

    assert routed is not None
    assert routed.command == "/analyze"


def test_top_level_market_router_maps_multi_symbol_chart_artifacts():
    available = {"/chart", "/dashboard", "/report"}

    routed = route_top_level_text("生成微软和英伟达对比图表", available)
    assert routed is not None
    assert routed.command == "/chart"
    assert routed.text == "/chart MSFT NVDA 1y"


def test_top_level_market_router_maps_news_language_to_news_command():
    available = {"/news", "/chart", "/dashboard", "/report"}

    routed = route_top_level_text("SpaceX最近的新闻", available)
    assert routed is not None
    assert routed.command == "/news"
    assert routed.text == "/news SpaceX"


def test_news_methodology_question_stays_in_conversation():
    text = "要基于新闻面进行分析，你觉得技术面还有用吗"

    assert route_top_level_text(text, {"/news", "/analyze"}) is None
    route = build_intent_route(text)
    assert route.primary == "general"
    assert route.wants_market_prefetch is False


def test_critical_a_share_name_resolves_before_market_fetch():
    assert resolve_market_symbol("分析四环生物股票和成交量") == "000518"
    routed = route_top_level_text("分析 四环生物 成交量", {"/analyze"})
    assert routed is not None
    assert routed.text == "/analyze 000518 --focus volume --lang zh"


def test_team_args_parser_and_symbol_resolution_are_ui_free():
    parsed = parse_team_args("nvda --agents macro,technical --full")
    assert parsed.symbols_raw == ["nvda"]
    assert parsed.agent_names == ["macro", "technical"]
    assert parsed.use_full_team is True
    assert team_agent_names(parsed) == ["macro", "technical"]
    assert resolve_team_symbols(parsed, {"watchlist": ["AAPL", "MSFT"]}) == ["NVDA"]

    full = parse_team_args("watchlist --full")
    assert team_agent_names(full) == ["macro", "fundamental", "technical", "risk", "news", "catalyst", "sector"]
    assert resolve_team_symbols(full, {"watchlist": ["aapl", "nvda", "msft", "tsla"]}) == ["AAPL", "NVDA", "MSFT"]

    eq = parse_team_args("AAPL --agents=technical,risk")
    assert eq.agent_names == ["technical", "risk"]


def test_team_table_rows_are_stable_for_plain_and_rich_rendering():
    results = [
        SimpleNamespace(
            agent="technical",
            success=True,
            degraded=True,
            signal="BUY",
            confidence=0.678,
            key_points=["这是一段很长很长的关键点，用来验证窄屏表格会被稳定截断"],
        ),
        SimpleNamespace(
            agent="risk",
            success=False,
            signal="HOLD",
            confidence=0.0,
            error="timeout after 60s",
            key_points=[],
        ),
        SimpleNamespace(
            agent="debate",
            success=True,
            signal="ADJ",
            confidence=0.5,
            key_points=[],
        ),
    ]

    assert truncate_cell("abcdef", 4) == "abc…"
    assert team_mode_label(results, use_full=True) == "2-agent 完整分析"

    rows = build_team_table_rows(results, key_width=12)

    assert [row.agent for row in rows] == ["technical", "risk", "debate"]
    assert rows[0].signal == "BUY"
    assert rows[0].confidence == "68%"
    assert rows[0].signal_color == "green"
    assert rows[0].key_point.endswith("…")
    assert rows[1].success is False
    assert rows[1].confidence == "-"
    assert rows[1].key_point == "timeout aft…"
    assert rows[2].is_debate is True
    assert rows[2].key_point == "信号分歧调解"

    plain = render_team_rows_plain(rows)
    assert plain[0].startswith("  DEG [technical] BUY (68%)")
    assert plain[1].startswith("  WARN [risk] HOLD (-)")


@pytest.mark.asyncio
async def test_try_top_level_route_executes_command_object():
    class _Commands:
        commands = {"/analyze": object()}

        def __init__(self):
            self.executed = []

        async def execute(self, text):
            self.executed.append(text)

    commands = _Commands()

    assert await try_top_level_route("analyze AAPL", commands) is True
    assert commands.executed == ["/analyze AAPL --lang en"]
    assert await try_top_level_route("market AAPL", commands) is False


def test_ta_plain_renderer_includes_quality_and_core_indicators():
    class _ServiceResult:
        data = {
            "price": 204.87,
            "rsi": 55.2,
            "macd_hist": 0.12345,
            "ma20": 200.0,
            "ma60": 180.0,
        }
        quality = {"status": "partial"}
        stale = False
        missing_fields = ["bb_upper"]

    def fmt(value, digits=2, suffix=""):
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.{digits}f}{suffix}"
        return f"{value}{suffix}"

    text = render_ta_plain("NVDA", 120, _ServiceResult(), fmt)

    assert "NVDA 技术指标" in text
    assert "data:partial" in text
    assert "stale:no" in text
    assert "missing:bb_upper" in text
    assert "价格: 204.87" in text
    assert "RSI: 55.20" in text
    assert "MACD_hist: 0.1235" in text
    assert "MA20: 200.0" in text


def test_quote_plain_renderer_and_market_cap_formatting():
    quote = {
        "success": True,
        "name": "NVIDIA",
        "price": 204.87,
        "change_pct": 2.22,
        "market_cap": 5_020_000_000_000,
        "currency": "USD",
    }

    text = render_quote_plain("NVDA", quote)

    assert "NVDA" in text
    assert "204.87" in text
    assert "+2.22%" in text
    assert "NVIDIA" in text
    assert compact_quote_market_cap(quote["market_cap"], "USD") == "  Mkt Cap: $5.02T"
    assert compact_quote_market_cap(123_400_000_000, "CNY") == "  Mkt Cap: ¥1234亿"
    assert compact_quote_market_cap(None, "USD") == ""


def test_build_analyze_prompt_templates_are_shared():
    cn = build_analyze_prompt("600519", "CTX", True)
    en = build_analyze_prompt("aapl", "CTX", False)
    zh_us = build_analyze_prompt("aapl", "CTX", False, response_lang="zh")

    assert cn.startswith("CTX\n\n## 终端回答风格")
    assert "技术面分析" in cn
    assert "风险提示" in cn
    assert en.startswith("CTX\n\n## Terminal Answer Style")
    assert "Technical analysis" in en
    assert "Risk assessment" in en
    assert "请对以上 AAPL" in zh_us


def test_report_args_parser_handles_format_type_pdf_and_output():
    parsed = parse_report_args("nvda --format md --type deep --pdf --output ./reports")

    assert parsed.symbol == "NVDA"
    assert parsed.fmt == "md"
    assert parsed.report_type == "deep"
    assert parsed.export_pdf is True
    assert str(parsed.output_dir).endswith("reports")
    assert parsed.is_markdown is True


def test_report_helpers_cover_agent_selection_failure_and_size(tmp_path):
    assert report_agent_names("standard") == ["macro", "fundamental", "technical", "risk"]
    assert report_agent_names("deep") == [
        "macro",
        "fundamental",
        "technical",
        "risk",
        "news",
        "catalyst",
        "sector",
    ]

    failed_team = SimpleNamespace(
        results=[
            SimpleNamespace(agent="macro", success=False),
            SimpleNamespace(agent="technical", success=False),
            SimpleNamespace(agent="synthesis", success=True),
        ]
    )
    mixed_team = SimpleNamespace(
        results=[
            SimpleNamespace(agent="macro", success=False),
            SimpleNamespace(agent="technical", success=True),
        ]
    )

    assert all_agents_failed(failed_team) is True
    assert all_agents_failed(mixed_team) is False
    assert all_agents_failed(SimpleNamespace(results=[])) is False

    partial_team = SimpleNamespace(
        confidence=0.80,
        final_signal="BUY",
        results=[
            SimpleNamespace(agent="macro", success=False),
            SimpleNamespace(agent="fundamental", success=False),
            SimpleNamespace(agent="technical", success=True),
            SimpleNamespace(agent="risk", success=False),
        ],
    )
    health = apply_report_quality_gate(
        partial_team, ["macro", "fundamental", "technical", "risk"]
    )
    assert health == report_agent_health(
        partial_team, ["macro", "fundamental", "technical", "risk"]
    )
    assert health["succeeded"] == 1
    assert health["failed"] == 3
    assert partial_team.confidence <= 0.29

    report_path = tmp_path / "report.html"
    report_path.write_text("x", encoding="utf-8")
    assert report_file_size_kb(report_path) == 1


@pytest.mark.asyncio
async def test_report_pdf_and_index_helpers_delegate_to_generator(monkeypatch, tmp_path):
    import report_generator

    report_path = tmp_path / "report.html"
    report_path.write_text("ok", encoding="utf-8")
    pdf_path = tmp_path / "report.pdf"
    index_path = tmp_path / "index.html"

    def fake_export(path):
        assert path == report_path
        pdf_path.write_text("pdf", encoding="utf-8")
        return pdf_path

    def fake_index(path):
        assert path == tmp_path
        index_path.write_text("index", encoding="utf-8")
        return index_path

    monkeypatch.setattr(report_generator, "export_pdf", fake_export)
    monkeypatch.setattr(report_generator, "update_reports_index", fake_index)

    assert await export_report_pdf(report_path) == pdf_path
    assert await update_report_index(tmp_path) == index_path


@pytest.mark.asyncio
async def test_generate_html_report_runs_team_and_generator(monkeypatch, tmp_path):
    import agents.team
    import datasources.router
    import report_generator

    calls = {}
    team_result = SimpleNamespace(final_signal="HOLD", results=[])
    output_path = tmp_path / "AAPL_report.html"

    async def fake_run_team(**kwargs):
        calls["team"] = kwargs
        return team_result

    async def fake_generate_report(**kwargs):
        calls["report"] = kwargs
        output_path.write_text("<html></html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(agents.team, "run_team", fake_run_team)
    monkeypatch.setattr(datasources.router, "get_router", lambda: "router")
    monkeypatch.setattr(report_generator, "generate_report", fake_generate_report)

    result = await generate_html_report(
        symbol="AAPL",
        report_type="deep",
        output_dir=tmp_path,
        config={"model": "fake", "ollama_url": "http://localhost:11434"},
    )

    assert result.path == output_path
    assert result.team_result is team_result
    assert result.agent_names == tuple(report_agent_names("deep"))
    assert calls["team"]["symbol"] == "AAPL"
    assert calls["team"]["agents"] == report_agent_names("deep")
    assert calls["team"]["data_router"] == "router"
    assert callable(calls["team"]["on_token"])
    assert calls["team"]["timeout_per_agent"] == 40.0
    assert calls["team"]["synthesis_timeout"] == 20.0
    assert calls["report"] == {
        "symbol": "AAPL",
        "team_result": team_result,
        "output_dir": tmp_path,
    }


@pytest.mark.asyncio
async def test_run_team_analysis_captures_noisy_output_and_sanitizes(monkeypatch):
    import agents.team
    # run_team_analysis is imported from aria_code.apps.cli.commands.team at the
    # top of this file; patching the bare copy left fetch_team_data_bundle real,
    # so the test went to the network for NVDA instead of using the stub.
    # agents.team / datasources.router stay bare — team.py imports them that way.
    import aria_code.apps.cli.commands.team as team_module
    import datasources.router

    calls = {}
    team_result = SimpleNamespace(final_signal="BUY", results=[])
    data_bundle = SimpleNamespace(quote={"price": 100})

    async def fake_run_team(**kwargs):
        print("noisy progress")
        calls["team"] = kwargs
        return team_result

    async def fake_bundle(symbol):
        calls["bundle_symbol"] = symbol
        return data_bundle

    def fake_sanitize(result, bundle):
        assert result is team_result
        assert bundle is data_bundle
        return ["cleaned"]

    monkeypatch.setattr(agents.team, "run_team", fake_run_team)
    monkeypatch.setattr(datasources.router, "get_router", lambda: "router")
    monkeypatch.setattr(team_module, "fetch_team_data_bundle", fake_bundle)
    monkeypatch.setattr(team_module, "build_team_llm_provider", lambda _config: "llm")

    result = await run_team_analysis(
        symbol="NVDA",
        args=parse_team_args("NVDA --agents technical,risk"),
        config={"model": "fake"},
        sanitize_result=fake_sanitize,
    )

    assert result.symbol == "NVDA"
    assert result.team_result is team_result
    assert result.data_bundle is data_bundle
    assert result.quality_notes[0] == "cleaned"
    assert "noisy progress" in result.captured_noise
    assert calls["bundle_symbol"] == "NVDA"
    assert calls["team"]["agents"] == ["technical", "risk"]
    assert calls["team"]["llm_provider"] == "llm"
    assert calls["team"]["data_router"] == "router"
    assert calls["team"]["on_token"] is None
    assert calls["team"]["market_context"]["quote"]["price"] == 100
    assert "price=USD 100" in calls["team"]["market_context"]["market_data_block"]
    assert calls["team"]["agent_data"] == {}
    assert result.quality_assessment["decision"] == "blocked"
    assert "no agent produced usable analysis" in result.quality_notes


def test_team_report_builder_and_save_write_quality_metadata(monkeypatch, tmp_path):
    from agents.base import AgentResult
    from agents.team import TeamResult

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    team_result = TeamResult(
        symbol="NVDA",
        agents_run=["technical"],
        results=[
            AgentResult(
                agent="technical",
                symbol="NVDA",
                analysis="数据冲突，已降级。",
                confidence=0.2,
                signal="HOLD",
                error="stale_or_conflicting_price",
            )
        ],
        synthesis="综合结论已降级。",
        final_signal="HOLD",
        confidence=0.2,
        elapsed_sec=1.2,
    )
    bundle = SimpleNamespace(
        quote={"price": 204.87, "currency": "USD", "market_cap": 5_020_000_000_000},
        history={},
        fundamentals={},
        technical={},
        provider_chain=["finnhub", "yfinance"],
        missing_fields=["macd"],
        warnings=[],
        errors=["technical unavailable"],
        quality={"status": "partial", "stale": True},
        status="partial",
    )

    markdown = build_team_report_markdown(
        symbol="NVDA",
        team_result=team_result,
        data_bundle=bundle,
        quality_notes=["technical: removed stale/conflicting prices ($945)"],
    )
    assert "## 数据质量" in markdown
    assert "是否过期: `yes`" in markdown
    assert "当前参考价: `USD 204.87`" in markdown
    assert "技术指标" in markdown
    assert "判定: `blocked`" in markdown
    assert "TECHNICAL (UNUSABLE)" in markdown

    saved = save_team_report(
        symbol="NVDA",
        team_result=team_result,
        data_bundle=bundle,
        quality_notes=["technical: removed stale/conflicting prices ($945)"],
    )

    assert saved.path.exists()
    assert str(tmp_path / "user-output" / "generated" / "reports" / "team") in str(saved.path)
    assert str(tmp_path / "project-artifacts") not in str(saved.path)
    assert saved.metadata_path is not None
    text = saved.path.read_text(encoding="utf-8")
    metadata = json.loads(saved.metadata_path.read_text(encoding="utf-8"))
    raw_data = json.loads(saved.path.with_suffix(".raw_data.json").read_text(encoding="utf-8"))

    assert "数据错误: `technical unavailable`" in text
    assert metadata["kind"] == "team_report"
    assert metadata["status"] == "data_unavailable"
    assert metadata["data"]["quality_gate"]["decision"] == "blocked"
    assert metadata["data"]["agent_count"] == 1
    assert metadata["data"]["failed_agents"] == ["technical"]
    assert metadata["data"]["quote"]["price"] == 204.87
    assert raw_data["data_bundle"]["provider_chain"] == ["finnhub", "yfinance"]


def test_build_team_market_context_extracts_real_snapshot_fields():
    bundle = SimpleNamespace(
        symbol="AAPL",
        quote={
            "price": 298.01,
            "change_pct": 0.7,
            "currency": "USD",
            "volume": 55_000_000,
            "market_cap": 4_380_000_000_000,
        },
        fundamentals={
            "pe_ratio": 31.2,
            "eps": 9.55,
            "roe": 147.3,
            "revenue_growth": 5.1,
            "analyst_target": 310.0,
        },
        technical={"rsi": 39.1, "macd_hist": -2.018, "ma20": 303.4, "ma60": 282.91},
        provider_chain=["finnhub", "yfinance", "local_pandas"],
        missing_fields=[],
        warnings=[],
        errors=[],
        quality={"status": "complete", "stale": False},
        status="complete",
    )

    context = build_team_market_context(bundle)

    assert context["quote"]["price"] == 298.01
    assert context["market_snapshot"]["analyst_target"] == 310.0
    assert "providers=finnhub, yfinance, local_pandas" in context["market_data_block"]
    assert "rsi=39.1" in context["market_data_block"]


def test_build_team_agent_data_reuses_bundle_and_computes_risk():
    rows = [
        {"close": 100 + index, "volume": 1_000_000 + index * 1000}
        for index in range(40)
    ]
    bundle = SimpleNamespace(
        quote={"price": 139.0},
        fundamentals={"pe_ratio": 20.0, "roe": 12.0},
        technical={"rsi": 55.0, "ma20": 130.0},
        history={"data": rows},
    )

    prepared = build_team_agent_data(bundle)

    assert prepared["technical"]["history"]["rsi"] == 55.0
    assert prepared["fundamental"]["fundamentals"]["pe_ratio"] == 20.0
    assert prepared["risk"]["risk_metrics"]["ann_vol"] >= 0


def test_team_quote_snapshot_downgrades_complete_when_visible_fields_are_missing():
    bundle = SimpleNamespace(
        symbol="NFLX",
        quote={
            "price": 77.38,
            "change_pct": 0.55,
            "currency": "USD",
            "market_cap": 325_800_000_000,
        },
        fundamentals={"name": "Netflix Inc"},
        technical={"rsi": 22.2, "macd_hist": -0.3282, "ma20": 83.11, "ma60": 90.35},
        provider_chain=["finnhub", "quote", "yahoo_chart", "fundamentals"],
        missing_fields=[],
        warnings=[],
        errors=[],
        quality={"status": "complete", "stale": False},
        status="complete",
    )

    snapshot = team_quote_snapshot(bundle)

    assert snapshot["status"] == "partial"
    assert snapshot["quality"]["status"] == "partial"
    assert set(snapshot["missing_fields"]) >= {"volume", "analyst_target", "risk_metrics"}


def test_clean_team_synthesis_text_removes_raw_markdown_noise():
    text = clean_team_synthesis_text("**结论**\n-\nFINAL: HOLD | Target: N/A")

    assert "**" not in text
    assert "\n-\n" not in text
    assert "结论" in text


def test_markdown_report_prompt_uses_real_fields_and_disallows_placeholders():
    prompt = build_markdown_report_prompt(
        symbol="AAPL",
        report_type="brief",
        market_data={"price": 100.0, "change_pct": 1.23, "rsi": 55.0, "macd": 0.123456},
        data_quality={"status": "partial", "stale": False, "providers": ["fake"], "missing_fields": ["ma20"]},
    )

    assert "简评版本" in prompt
    assert "- 当前价: 100.00" in prompt
    assert "- 涨跌: 1.23%" in prompt
    assert "- RSI(14): 55.00" in prompt
    assert "- MACD: 0.1235" in prompt
    assert "数据状态: partial" in prompt
    assert "数据源链: fake" in prompt
    assert "缺失字段: ma20" in prompt
    assert "不要使用占位符" in prompt


def test_markdown_data_block_warns_when_no_market_fields():
    assert "不得编造价格或指标" in markdown_data_block({})


def test_clean_markdown_report_response_removes_injected_market_block():
    text = "# Report\n\n## 📊 实时行情\nnoise\n# Final\nok"

    assert clean_markdown_report_response(text) == "# Report\n# Final\nok"


def test_save_markdown_report_writes_output_dir_without_metadata(tmp_path):
    saved = save_markdown_report(
        symbol="AAPL",
        report_type="brief",
        markdown_text="# AAPL\n\nok",
        timestamp="20260612_1200",
        output_dir=tmp_path,
        market_data={"price": 100},
        data_quality={"status": "ok"},
    )

    assert saved.path == tmp_path / "AAPL_report_20260612_1200.md"
    assert saved.path.read_text(encoding="utf-8") == "# AAPL\n\nok"
    assert saved.metadata_path is None


def test_save_markdown_report_writes_artifact_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))

    class _Bundle:
        quote = {"price": 100}
        history = {}
        fundamentals = {}
        technical = {"rsi": 55}
        provider_chain = ["fake"]
        missing_fields = ["ma20"]
        warnings = ["warn"]
        errors = []

    saved = save_markdown_report(
        symbol="AAPL",
        report_type="standard",
        markdown_text="# AAPL\n\nok",
        timestamp="ignored",
        output_dir=None,
        market_data={"price": 100},
        data_quality={"status": "partial", "stale": False},
        data_bundle=_Bundle(),
    )

    assert saved.path.exists()
    assert str(tmp_path / "user-output" / "generated" / "reports" / "market") in str(saved.path)
    assert str(tmp_path / "project-artifacts") not in str(saved.path)
    assert saved.metadata_path is not None
    metadata = json.loads(saved.metadata_path.read_text(encoding="utf-8"))
    assert metadata["kind"] == "market_report"
    assert metadata["format"] == "markdown"
    assert metadata["symbol"] == "AAPL"
    assert metadata["data"]["provider_chain"] == ["fake"]
    assert metadata["data"]["missing_fields"] == ["ma20"]
    assert saved.path.with_suffix(".raw_data.json").exists()


@pytest.mark.asyncio
async def test_build_analyze_context_uses_data_service_boundary(monkeypatch):
    import packages.aria_services.data as service_data

    class _Bundle:
        quote = {"success": True, "price": 101.2, "change_pct": -1.5, "name": "Apple"}
        technical = {"success": True, "rsi": 44.0, "macd_hist": -0.031, "ma20": 100.0}
        provider_chain = ["fake_quote", "fake_ta"]
        missing_fields = ["fundamentals"]
        quality = {"status": "partial", "stale": False}

    class _DataService:
        def __init__(self, *args, **kwargs):
            pass

        def bundle(self, *_args, **_kwargs):
            return _Bundle()

    monkeypatch.setattr(service_data, "DataService", _DataService)

    text = await build_analyze_context("AAPL", False, has_mdc=False)

    assert "## AAPL Market Data" in text
    assert "### Data Quality" in text
    assert "Status: partial" in text
    assert "Providers: fake_quote, fake_ta" in text
    assert "Missing fields: fundamentals" in text
    assert "Price: 101.20  (-1.50%)" in text
    assert "44.0" in text
    assert "-0.0310" in text
