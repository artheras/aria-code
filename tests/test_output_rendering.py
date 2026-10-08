import builtins
import os
import pathlib
import sys

from aria_code.apps.cli.context import AriaContext


class _ContextOnly:
    """Stand-in for SlashCommands where only self.context is reached."""

    context = AriaContext(console=None, has_rich=False)


_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)


class _SnapshotMDC:
    def quote(self, symbol):
        prices = {
            "AAPL": {"name": "Apple", "price": 195.2, "change_pct": 0.8, "market_cap": 2_990_000_000_000, "high": 197.0, "low": 193.0},
            "NVDA": {"name": "NVIDIA", "price": 204.87, "change_pct": 2.22, "market_cap": 5_020_000_000_000, "high": 205.66, "low": 199.54},
            "MC.PA": {"name": "MC.PA", "price": 508.8, "change_pct": 0.73, "market_cap": 250_000_000_000, "high": 510.0, "low": 507.8},
            "300806": {"name": "斯迪克", "price": 12.34, "change_pct": -1.2, "market_cap": 5_000_000_000, "high": 12.8, "low": 12.1},
        }
        row = prices.get(symbol, prices["AAPL"])
        return {
            "success": True,
            "symbol": symbol,
            "name": row["name"],
            "price": row["price"],
            "change_pct": row["change_pct"],
            "market_cap": row["market_cap"],
            "high": row["high"],
            "low": row["low"],
            "currency": "USD",
            "provider": "test_provider",
            "provider_chain": ["test_provider"],
        }

    def fundamentals(self, symbol):
        return {"success": True, "provider": "fundamentals", "market_cap": 1_000_000_000_000}

    def technical_indicators(self, *_args, **_kwargs):
        symbol = _args[0] if _args else ""
        if symbol == "AAPL":
            return {
                "success": True,
                "provider": "local_pandas",
                "rsi": 42.0,
                "macd_hist": -0.18,
                "ma20": 198.4,
                "ma60": 188.7,
                "bb_upper": 207.5,
                "bb_lower": 184.2,
            }
        if symbol == "300806":
            return {
                "success": True,
                "provider": "local_pandas",
                "rsi": 66.0,
                "macd_hist": 0.25,
                "ma20": 11.8,
                "ma60": 10.7,
                "bb_upper": 13.0,
                "bb_lower": 10.4,
            }
        return {"success": False, "error": "history unavailable"}

    def history(self, symbol, days=252, interval="1d"):
        if symbol != "AAPL":
            return {"success": False, "error": "history unavailable"}
        count = 96 if interval == "1h" else 260
        records = []
        for i in range(count):
            if interval == "1h":
                close = 191.0 + (i % 24) * 0.25 + (i // 24) * 0.35
            else:
                close = 165.0 + i * 0.14 + ((i % 18) - 9) * 0.22
            high = close + 1.1
            low = close - 1.2
            if i % 17 == 0:
                high += 2.4
            if i % 19 == 0:
                low -= 2.2
            records.append({
                "date": f"2026-01-{(i % 28) + 1:02d}",
                "open": round(close - 0.3, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": 1_000_000 + i,
            })
        return {"success": True, "symbol": symbol, "data": records, "provider": "test_history"}


def test_market_snapshot_output_avoids_na_placeholders(monkeypatch):
    import aria_cli

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("yfinance unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(aria_cli, "_HAS_MDC", True)
    monkeypatch.setattr(aria_cli, "_get_mdc", lambda: _SnapshotMDC())
    monkeypatch.setattr(aria_cli, "_get_provider_key", lambda _provider: "")

    result = aria_cli._try_handle_market_snapshot_analysis("分析 AAPL 市场")

    assert result["success"] is True
    assert "最新价" in result["response"]
    assert "结论" in result["response"]
    assert "观察位" in result["response"]
    assert "多周期关键位" in result["response"]
    assert "4H/短线" in result["response"]
    assert "日线/波段" in result["response"]
    assert "周线/长线" in result["response"]
    assert "| 周期 | 适合 | 支撑 | 压力 | 用法 |" not in result["response"]
    # Two lines a timeframe: levels, then what to do with them.
    assert "- **4H/短线**（" in result["response"]
    assert "— 支撑 USD " in result["response"]
    assert " · 压力 USD " in result["response"]
    assert "来源：" not in result["response"]
    assert "N/A" not in result["response"]
    assert result.get("analysis_complete") is True
    assert result.get("timeframe_levels")


def test_market_snapshot_handles_multi_symbol_company_names(monkeypatch):
    import aria_cli

    monkeypatch.setattr(aria_cli, "_HAS_MDC", True)
    monkeypatch.setattr(aria_cli, "_get_mdc", lambda: _SnapshotMDC())

    result = aria_cli._try_handle_market_snapshot_analysis("我想知道今天苹果和英伟达的市值和股票走势")

    assert result["success"] is True
    text = result["response"]
    assert "AAPL" in text
    assert "NVDA" in text
    assert "市值" in text
    assert "对比结论" in text
    assert "逐项分析" in text
    assert "NVDA  NVDA" not in text
    assert "- stale: none" in text
    assert "technical shown as trend only" not in text
    assert result.get("analysis_complete") is True
    assert "/ta AAPL" in text
    assert "/ta NVDA" in text


def test_market_snapshot_resolves_sidike_without_inheriting_previous_symbol(monkeypatch):
    import aria_cli
    import local_finance_tools
    from apps.cli.utils.market_detect import _extract_market_symbol

    monkeypatch.setattr(aria_cli, "_HAS_MDC", True)
    monkeypatch.setattr(aria_cli, "_get_mdc", lambda: _SnapshotMDC())
    monkeypatch.setattr(aria_cli, "_get_provider_key", lambda _provider: "")

    # Forecasting is another data call, separate from the mocked snapshot MDC.
    # Keep this rendering test offline and verify the forecast gets this symbol.
    prediction_symbols = []

    def fake_predictions(params):
        prediction_symbols.extend(params["symbols"])
        return {"predictions": [{
            "direction": "bullish", "predicted_return": 0.01, "confidence": 0.6,
        }]}

    monkeypatch.setattr(local_finance_tools, "_get_predictions", fake_predictions)

    assert _extract_market_symbol("斯迪克的走势和预测") == "300806"
    history = [{"role": "user", "content": "紫金矿业走势"}]
    result = aria_cli._try_handle_market_snapshot_analysis("斯迪克的走势和预测", history=history)

    assert result["success"] is True
    text = result["response"]
    assert "斯迪克" in text
    assert "`300806`" in text
    assert "601899" not in text
    assert "预测参考" in text
    assert "信号拆解" in text
    assert prediction_symbols == ["sz300806"]


def test_market_snapshot_repeat_notice_compresses_identical_cache():
    import aria_cli

    result = {
        "symbol": "AAPL",
        "name": "Apple Inc",
        "price": 195.2,
        "change_pct": 0.8,
        "currency": "USD",
        "signal": "NEUTRAL",
        "support": "USD 190.00",
        "resistance": "USD 200.00",
        "as_of": "2026-06-21",
    }
    previous = aria_cli._market_snapshot_cache_entry(result, now=100.0)

    notice = aria_cli._build_market_snapshot_repeat_notice(result, previous, now=120.0)

    assert "行情未变化" in notice
    assert "已省略完整表格" in notice
    assert "`/quote AAPL`" in notice


def test_market_snapshot_repeat_notice_expires_after_ttl():
    import aria_cli

    result = {"symbol": "AAPL", "price": 195.2, "change_pct": 0.8, "signal": "NEUTRAL"}
    previous = aria_cli._market_snapshot_cache_entry(result, now=100.0)

    assert aria_cli._build_market_snapshot_repeat_notice(result, previous, now=200.0) == ""


def test_tradingview_indicator_readout_explains_bullish_and_bearish_drivers():
    import aria_cli

    readout = aria_cli._build_tradingview_indicator_readout(
        {
            "symbol": "AAPL",
            "name": "Apple Inc",
            "currency": "USD",
            "price": 298.01,
            "change_pct": 0.7,
            "signal": "NEUTRAL",
            "signal_confidence": 0.46,
            "rsi": 39.1,
            "macd_hist": -2.018,
            "ma20": 303.4,
            "ma60": 282.91,
            "supports": [287.43, 282.91],
            "resistances": [303.4, 319.37],
        },
        tv_symbol="NASDAQ:AAPL",
        mode="bullish",
    )

    assert "TradingView 已作为图表界面打开" in readout
    assert "不直接抓取 TradingView 页面数据" in readout
    assert "看涨数据" in readout
    assert "价格高于 MA60" in readout
    assert "看跌/需要确认" in readout
    assert "价格低于 MA20" in readout
    assert "NASDAQ:AAPL" in readout


def test_lvmh_prefetch_normalizes_symbol_name_and_currency(monkeypatch):
    import aria_cli

    monkeypatch.setattr(aria_cli, "_HAS_MDC", True)
    monkeypatch.setattr(aria_cli, "_get_mdc", lambda: _SnapshotMDC())

    block = aria_cli._try_prefetch_market_data("分析lvmh股票和成交量")

    assert "LVMH/路易威登(MC.PA)" in block
    assert "交易代码**：MC.PA（Euronext Paris）" in block
    assert "LVMH Moet Hennessy Louis Vuitton SE" in block
    assert "最新价**：EUR 508.8" in block
    assert "USD 508.8" not in block


def test_unresolved_market_name_does_not_inherit_history():
    import aria_cli

    history = [{"role": "user", "content": "紫金矿业走势"}]
    result = aria_cli._try_handle_market_snapshot_analysis("不存在公司走势", history=history)

    assert result["success"] is False or "无法识别" in result.get("response", "")


def test_tool_error_summary_hides_curl_details():
    import aria_cli

    summary = aria_cli._format_tool_summary(
        "get_market_data",
        {"success": False, "error": "curl: (28) Connection timed out after 30002 milliseconds"},
    )

    assert "请求超时" in summary
    assert "curl" not in summary.lower()


def test_market_tool_summary_preserves_data_provenance_and_rsi_direction():
    import aria_cli

    summary = aria_cli._format_tool_summary(
        "get_market_data",
        {
            "success": True,
            "symbol": "AAPL",
            "price": 205.5,
            "change_pct": -0.4,
            "currency": "USD",
            "provider": "test_provider",
            "as_of": "2026-08-15T09:31:00-04:00",
            "rsi": 25.77,
            "macd_hist": -2.6242,
            "source": {"path": "/Users/mac/Desktop/aria-code/generated/tmp_ohlc.csv"},
        },
    )

    assert "Price: USD 205.5" in summary
    assert "Provider timestamp: 2026-08-15T09:31:00-04:00" in summary
    assert "RSI(14): 25.77 (oversold)" in summary
    assert "MACD histogram: -2.6242" in summary
    assert "/Users/mac" not in summary


def test_run_command_activity_summary_uses_exit_code_field():
    from ui.render.output import _one_line_tool_summary

    icon, detail = _one_line_tool_summary(
        "run_command",
        {"success": True, "data": {"exit_code": 2}},
        0.0,
        {},
    )

    assert "red" in icon
    assert "exit 2" in detail


def test_activity_summary_hides_local_file_paths():
    from ui.render.output import _one_line_tool_summary

    _icon, detail = _one_line_tool_summary(
        "write_file",
        {"success": True, "data": {"path": "/Users/mac/Desktop/aria-code/secret.py", "lines": 12, "size_bytes": 48}},
        0.0,
        {"path": "/Users/mac/Desktop/aria-code/secret.py", "content": "x\n"},
    )

    assert "file tool" in detail
    assert "12 lines" in detail
    assert "/Users" not in detail
    assert "secret.py" not in detail


def test_activity_summary_hides_web_fetch_url():
    from ui.render.output import _one_line_tool_summary

    _icon, detail = _one_line_tool_summary(
        "web_fetch",
        {"success": True, "data": {"url": "https://example.com/private/report", "length": 1234}},
        0.0,
        {"url": "https://example.com/private/report"},
    )

    assert "web fetch" in detail
    assert "1,234c" in detail
    assert "example.com" not in detail
    assert "/private/report" not in detail


def test_activity_summary_hides_full_output_path():
    from ui.render.output import _one_line_tool_summary

    _icon, detail = _one_line_tool_summary(
        "run_command",
        {"success": True, "data": {"exit_code": 0, "full_output_path": "/Users/mac/.aria/artifacts/command-output.txt"}},
        0.0,
        {},
    )

    assert "full output saved" in detail
    assert "/Users" not in detail
    assert "command-output.txt" not in detail


def test_tool_display_label_marks_mcp_without_target_details():
    from ui.render.output import tool_display_label

    assert tool_display_label("mcp__github__read_file") == "github · read file · MCP"
    assert tool_display_label("web_search") == "web_search · web search"


def test_display_path_returns_filename_only():
    from ui.render.output import display_path

    assert display_path("/Users/mac/Desktop/aria-code/report.html") == "report.html"
    assert display_path("", fallback="artifact") == "artifact"


def test_report_markdown_prompt_omits_na_placeholders(monkeypatch, tmp_path):
    import aria_cli
    import packages.aria_services.data as service_data

    prompts = []

    class FakeTerminal:
        conversation = [{"role": "assistant", "content": "# Report\nok"}]
        # The report prompt follows ui_lang; this test checks the Chinese labels.
        config = {"ui_lang": "zh"}

        async def send_message(self, prompt):
            prompts.append(prompt)

    class FakeCommands:
        terminal = FakeTerminal()
        # cmd_report prints through self.context; has_rich False keeps it on the
        # plain-print branch so no console object is needed.
        context = AriaContext(console=None, has_rich=False)

    class FakeBundle:
        quote = {"success": True, "price": 100.0, "provider": "test", "provider_chain": ["test"]}
        technical = {"success": False, "error": "history unavailable"}
        history = {}
        fundamentals = {}
        provider_chain = ["test"]
        missing_fields = ["rsi", "macd", "ma20", "ma60"]
        warnings = ["history unavailable"]
        errors = []
        status = "partial"
        quality = {
            "status": "partial",
            "stale": False,
            "providers": ["test"],
            "missing_fields": ["rsi", "macd", "ma20", "ma60"],
            "warnings": ["history unavailable"],
            "errors": [],
        }

    class FakeDataService:
        def bundle(self, *_args, **_kwargs):
            return FakeBundle()

    monkeypatch.setattr(service_data, "DataService", FakeDataService)
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)

    import asyncio
    asyncio.run(aria_cli.SlashCommands.cmd_report(FakeCommands(), "AAPL --format md"))

    assert prompts
    assert "N/A" not in prompts[0]
    assert "当前价" in prompts[0]
    assert "数据质量" in prompts[0]
    assert "数据状态: partial" in prompts[0]


def test_display_value_uses_dash_for_missing_values():
    import aria_cli

    assert aria_cli._display_value(None) == "—"
    assert aria_cli._display_value("N/A") == "—"


def test_market_render_import_does_not_require_prompt_toolkit():
    import subprocess
    import textwrap

    code = textwrap.dedent("""
        import importlib.abc
        import sys

        class BlockPromptToolkit(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "prompt_toolkit" or fullname.startswith("prompt_toolkit."):
                    raise ModuleNotFoundError("No module named 'prompt_toolkit'")
                return None

        sys.meta_path.insert(0, BlockPromptToolkit())
        from ui.render.market import print_quote_result, print_ta_result
        from ui.input_box import HAS_PROMPT_TOOLKIT
        assert HAS_PROMPT_TOOLKIT is False
        print("ok")
    """)

    repo_root = pathlib.Path(__file__).parents[1]
    # `ui` moved under src/aria_code, so a bare subprocess at the repo root can
    # no longer import it — mirror the roots pyproject's pytest pythonpath and
    # the aria-code launcher put on sys.path.
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([
            str(repo_root / "src" / "aria_code"),
            str(repo_root / "src"),
            os.environ.get("PYTHONPATH", ""),
        ]).rstrip(os.pathsep),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_repetition_stopped_text_is_recovered_for_display():
    import aria_cli

    text = "已经生成文件。\n```python\nprint('ok')\n*[model stopped — repetition detected]*"
    recovered = aria_cli._recover_repetition_stopped_text(text)

    assert "*[model stopped" not in recovered
    assert recovered.count("```") == 2
    assert "已检测到模型开始重复输出" in recovered


def test_repetition_recovery_drops_unfinished_markdown_table():
    import aria_cli

    text = (
        "## 短期预测\n"
        "| 场景 | 价格区间 | 触发条件 |\n"
        "| --- | --- | --- |\n"
        "*[model stopped — repetition detected]*"
    )

    recovered = aria_cli._recover_repetition_stopped_text(text)

    assert "| 场景 |" not in recovered
    assert "未完成的尾部已隐藏" in recovered


def test_narrow_terminal_converts_markdown_table_to_stacked_records():
    from ui.render.output import adapt_markdown_for_width

    source = (
        "| 维度 | 观察 | 结论 |\n"
        "| --- | --- | --- |\n"
        "| 趋势 | 价格高于 MA20 | 偏多 |"
    )

    adapted = adapt_markdown_for_width(source, 80)

    assert "| --- |" not in adapted
    # One line a row; a column that names a dimension keeps its name.
    assert adapted.strip() == "- **趋势**：观察：价格高于 MA20 · 结论：偏多"
    assert adapt_markdown_for_width(source, 120) == source


def test_a_narrow_key_value_table_is_one_line_a_row():
    """A ten-row snapshot table took about thirty lines as nested bullets."""
    from ui.render.output import adapt_markdown_for_width

    source = (
        "| 技术指标 | 数值 | 含义 |\n|---|---|---|\n"
        "| RSI(14) | 51.9，中性 | 中性 |\n| MA20 | USD 332.54 | 价格高于MA20 ↑ |\n| 支撑位 | USD 332.54 | |"
    )
    assert adapt_markdown_for_width(source, 80).splitlines() == [
        "- **RSI(14)**：51.9，中性",
        "- **MA20**：USD 332.54 · 价格高于MA20 ↑",
        "- **支撑位**：USD 332.54",
    ]
    wide = "| Symbol | Price | Change |\n|---|---|---|\n| AAPL | USD 333 | +0.8% |"
    assert adapt_markdown_for_width(wide, 80) == "- **AAPL**: Price: USD 333 · Change: +0.8%"


def test_turn_footer_defaults_to_compact_without_token_noise():
    from runtime import AgentTurnState
    from ui.render.output import format_turn_footer

    state = AgentTurnState(provider="ollama")
    state.add_usage({"prompt_tokens": 100, "completion_tokens": 50})
    state.tools_used.extend(["market_snapshot", "web_search"])
    metadata = state.build_metadata(elapsed=3.0)

    compact = format_turn_footer(metadata, mode="compact", copy_available=True)
    full = format_turn_footer(metadata, mode="full", copy_available=True)

    assert compact == "3.0s · ollama · market_snapshot web_search · /copy"
    assert "tokens" not in compact
    assert "150 tokens" in full
    assert format_turn_footer(metadata, mode="off", copy_available=True) == ""


def test_context_compaction_decision_uses_incoming_content():
    from apps.cli.message_processing import context_compaction_decision

    messages = [{"role": "user", "content": "x" * 1500} for _ in range(8)]
    decision = context_compaction_decision(
        messages,
        model_key="qwen-fast",
        extra_content="y" * 30_000,
        threshold=0.50,
    )

    assert decision["should_compact"] is True
    assert decision["estimated_tokens"] > 10_000


def test_analyze_context_uses_data_service_quality(monkeypatch):
    import asyncio
    import aria_cli
    import packages.aria_services.data as service_data

    class FakeBundle:
        quote = {"success": True, "price": 100.0, "change_pct": 1.2, "name": "Apple"}
        technical = {"success": True, "rsi": 55.0, "macd_hist": 0.12, "ma20": 98.0, "ma60": 90.0}
        fundamentals = {}
        history = {}
        provider_chain = ["fake_quote", "fake_ta"]
        missing_fields = ["fundamentals"]
        quality = {
            "status": "partial",
            "stale": False,
            "providers": ["fake_quote", "fake_ta"],
            "missing_fields": ["fundamentals"],
        }

    class FakeDataService:
        def __init__(self, *args, **kwargs):
            pass

        def bundle(self, *_args, **_kwargs):
            return FakeBundle()

    monkeypatch.setattr(service_data, "DataService", FakeDataService)
    monkeypatch.setattr(aria_cli, "_HAS_MDC", False)

    ctx = asyncio.run(aria_cli.SlashCommands._build_analyze_context(object(), "AAPL", False))

    assert "### Data Quality" in ctx
    assert "Status: partial" in ctx
    assert "Providers: fake_quote, fake_ta" in ctx
    assert "Missing fields: fundamentals" in ctx
    assert "Price: 100.00" in ctx


def test_team_result_sanitizer_removes_stale_split_prices():
    import aria_cli
    from agents.base import AgentResult
    from agents.team import TeamResult
    from data_service import DataBundle

    team_result = TeamResult(
        symbol="NVDA",
        agents_run=["technical"],
        results=[
            AgentResult(
                agent="technical",
                symbol="NVDA",
                analysis="Current price is ~$945 and target is $660.",
                confidence=0.8,
                signal="BUY",
            )
        ],
        synthesis="Entry around $580-$590, target $660.",
        final_signal="BUY",
        confidence=0.7,
    )
    bundle = DataBundle(
        symbol="NVDA",
        quote={"price": 204.87, "currency": "USD"},
        status="partial",
    )

    notes = aria_cli._sanitize_team_result_with_market_data(team_result, bundle)

    assert notes
    assert team_result.final_signal == "HOLD"
    assert team_result.confidence == 0.2
    assert team_result.results[0].success is False
    assert "当前参考价: 204.87" in team_result.results[0].analysis
    assert "$945" not in team_result.synthesis


def test_team_report_includes_data_quality_section(monkeypatch, tmp_path):
    import asyncio
    import aria_cli
    from agents.base import AgentResult
    from agents.team import TeamResult
    from data_service import DataBundle

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
    )
    bundle = DataBundle(
        symbol="NVDA",
        quote={"price": 204.87, "currency": "USD", "market_cap": 5_020_000_000_000},
        provider_chain=["finnhub", "yfinance"],
        missing_fields=["macd"],
        errors=["technical unavailable"],
        quality={"status": "partial", "stale": True, "errors": ["technical unavailable"]},
        status="partial",
    )

    asyncio.run(
        aria_cli.SlashCommands._save_team_report(
            _ContextOnly(),
            "NVDA",
            team_result,
            bundle,
            ["technical: removed stale/conflicting prices ($945)"],
        )
    )

    reports = list((tmp_path / "user-output").rglob("*_NVDA_team_report.md"))
    assert reports
    text = reports[0].read_text(encoding="utf-8")
    assert "## 数据质量" in text
    assert "是否过期: `yes`" in text
    assert "数据错误: `technical unavailable`" in text
    assert "当前参考价: `USD 204.87`" in text
    assert "输出校验" in text
    assert "TECHNICAL (UNUSABLE)" in text
