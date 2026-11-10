"""
ui/render/finance.py — Finance tool result renderers
====================================================
All public functions accept ``console`` and ``has_rich`` as keyword-only
arguments following the same contract as ui/render/team.py:

    render_finance_result(tool_name, result, console=console, has_rich=HAS_RICH)

``aria_cli.py`` keeps thin wrappers that supply its module-level globals.
No imports from aria_cli.py — dependency flows one way only.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

try:
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def _clean_error_msg(error: object) -> str:
    """Convert provider/runtime errors into short user-facing messages."""
    raw = str(error or "failed").strip()
    low = raw.lower()
    if not raw:
        return "操作失败"
    if "curl: (28)" in low or "timed out" in low or "timeout" in low:
        return "请求超时，数据源暂时不可用。请稍后重试或运行 /health 检查服务。"
    if "connection refused" in low:
        return "连接被拒绝，服务暂时不可用。请检查本地服务或网络。"
    if "connection aborted" in low or "remotedisconnected" in low:
        return "网络连接中断，数据源未完成响应。请稍后重试。"
    if "rate" in low or "429" in low or "too many requests" in low:
        return "数据源请求频率受限，请稍后重试。"
    if "traceback" in low:
        return raw.splitlines()[-1][:160] if raw.splitlines() else "运行失败"
    return raw[:240]


def render_finance_result(tool_name: str, result: dict, *, console=None, has_rich: bool = True, bot_mode: bool = False) -> None:
    """
    Rich-formatted display for all finance tool results.
    Shows structured tables instead of raw dicts.
    """
    if bot_mode:
        return
    if not result or not isinstance(result, dict):
        return
    if not result.get("success"):
        err = _clean_error_msg(
            result.get("error") or result.get("message") or "数据暂不可用（服务离线或无数据）"
        )
        chain = result.get("provider_chain") or []
        chain_text = f"\n[dim]已尝试: {' -> '.join(chain)}[/dim]" if chain else ""
        if has_rich:
            from rich.panel import Panel
            from rich import box as rich_box
            console.print(Panel(
                f"[yellow]⚠ {err}[/yellow]{chain_text}",
                border_style="yellow",
                box=rich_box.ROUNDED,
                padding=(0, 1),
            ))
        else:
            print(f"  ⚠ {err}")
        return

    provider = result.get("provider", "")
    prov_tag = f" [dim][{provider}][/dim]" if provider else ""

    # ── Market data / quote ────────────────────────────────────────────
    if tool_name in ("get_market_data", "get_crypto_data", "get_forex_data"):
        sym   = result.get("symbol", "")
        px    = result.get("latest_close", result.get("price", 0))
        chg   = result.get("change_pct", result.get("change_pct_24h", 0)) or 0
        vol   = result.get("volume", 0)
        name  = result.get("name", "")
        curr  = result.get("currency", "")
        color = "green" if chg >= 0 else "red"
        arrow = "▲" if chg >= 0 else "▼"
        if has_rich:
            from rich.table import Table
            t = Table(show_header=False, box=None, padding=(0, 1))
            t.add_column(style="dim", width=20)
            t.add_column()
            title_str = f"[bold]{sym}[/bold]" + (f"  {name}" if name else "")
            t.add_row("标的", title_str)
            px_disp = f"{curr} {px:,.4g}" if curr else f"{px:,.4g}"
            t.add_row("最新价", f"[bold]{px_disp}[/bold]")
            t.add_row("涨跌幅", f"[{color}]{arrow} {chg:+.2f}%[/{color}]")
            _hi = result.get("high"); _lo = result.get("low")
            if _hi and _lo:
                t.add_row("日内区间", f"{_lo:,.4g} — {_hi:,.4g}")
            if vol:
                t.add_row("成交量", f"{int(vol):,}")
            as_of = result.get("as_of")
            retrieved_at = result.get("retrieved_at")
            if as_of:
                t.add_row("数据时点", str(as_of))
            elif retrieved_at:
                t.add_row("获取时间", f"{retrieved_at}（源未提供成交时点）")
            if provider:
                t.add_row("数据来源", str(provider))
            # Technical indicators from local tool
            _rsi = result.get("rsi")
            if _rsi is not None:
                _rsi_color = "red" if _rsi >= 70 else ("cyan" if _rsi <= 30 else "white")
                t.add_row("RSI(14)", f"[{_rsi_color}]{_rsi:.1f}[/{_rsi_color}]")
            _mh = result.get("macd_hist")
            if _mh is not None:
                _mh_color = "green" if _mh > 0 else "red"
                t.add_row("MACD hist", f"[{_mh_color}]{_mh:+.4f}[/{_mh_color}]")
            _ma20 = result.get("ma20"); _ma60 = result.get("ma60")
            if _ma20:
                t.add_row("MA20", f"{_ma20:,.4g}")
            if _ma60:
                t.add_row("MA60", f"{_ma60:,.4g}")
            # Legacy cloud fields
            for k in ("high_52w", "low_52w", "bid", "ask"):
                v = result.get(k)
                if v is not None:
                    t.add_row(k.replace("_", " ").title(), f"{v:,.4g}")
            console.print(t)
            if prov_tag:
                console.print(f"  {prov_tag}")
        else:
            print(f"  {sym}: {px} ({chg:+.2f}%)")
        return

    # ── Market history (OHLC summary + recent candles) ──────────────────
    if tool_name == "get_market_history":
        sym  = result.get("symbol", "")
        name = result.get("name", "")
        s    = result.get("summary", {}) or {}
        chg  = s.get("change_pct") or 0
        color = "green" if chg >= 0 else "red"
        arrow = "▲" if chg >= 0 else "▼"
        candles = result.get("recent_candles", []) or []
        closes  = [c.get("close") for c in candles if c.get("close") is not None]
        # Unicode sparkline of the recent closes
        spark = ""
        if len(closes) >= 2:
            blocks = "▁▂▃▄▅▆▇█"
            lo, hi = min(closes), max(closes)
            rng = (hi - lo) or 1
            spark = "".join(blocks[min(7, int((c - lo) / rng * 7))] for c in closes)
        if has_rich:
            from rich.table import Table
            t = Table(show_header=False, box=None, padding=(0, 1))
            t.add_column(style="dim", width=20)
            t.add_column()
            title_str = f"[bold]{sym}[/bold]" + (f"  {name}" if name and name != sym else "")
            t.add_row("标的", title_str)
            interval = result.get("interval", "1d")
            n = result.get("total_points", len(candles))
            t.add_row("区间", f"{s.get('start_date','')} → {s.get('end_date','')}  ({n} 根 · {interval})")
            sc, ec = s.get("start_close"), s.get("end_close")
            if sc is not None and ec is not None:
                t.add_row("期间涨跌", f"{sc:,.4g} → {ec:,.4g}  [{color}]{arrow} {chg:+.2f}%[/{color}]")
            ph, pl = s.get("period_high"), s.get("period_low")
            if ph is not None and pl is not None:
                t.add_row("期间高低", f"{pl:,.4g} — {ph:,.4g}")
            ma_parts = []
            for label, key in (("MA5", "ma5"), ("MA20", "ma20"), ("MA60", "ma60")):
                v = s.get(key)
                if v is not None:
                    ma_parts.append(f"{label} {v:,.4g}")
            if ma_parts:
                t.add_row("均线", "  ".join(ma_parts))
            av = s.get("avg_volume")
            if av:
                t.add_row("平均成交量", f"{int(av):,}")
            if spark:
                t.add_row(f"近{len(closes)}日走势", f"[{color}]{spark}[/{color}]")
            console.print(t)
            if prov_tag:
                console.print(f"  {prov_tag}")
        else:
            print(f"  {sym} {s.get('start_date','')}→{s.get('end_date','')}: "
                  f"{s.get('end_close')} ({chg:+.2f}%)")
        return

    # ── Commodity data ─────────────────────────────────────────────────
    if tool_name == "get_commodities_data":
        sym  = result.get("symbol", "")
        px   = result.get("latest_close", 0)
        chg  = result.get("change_pct", 0) or 0
        unit = result.get("unit", "")
        color = "green" if chg >= 0 else "red"
        arrow = "▲" if chg >= 0 else "▼"
        if has_rich:
            console.print(
                f"  [bold]{sym}[/bold]  {px:,.3g} {unit}  "
                f"[{color}]{arrow} {chg:+.3f}%[/{color}]{prov_tag}"
            )
            for k in ("pct_from_52w_high", "pct_from_52w_low", "year_return"):
                v = result.get(k)
                if v is not None:
                    console.print(f"    [dim]{k:<25s}[/dim] {v:+.3%}")
        else:
            print(f"  {sym}: {px} ({chg:+.3f}%)")
        return

    # ── AI signal ──────────────────────────────────────────────────────
    if tool_name == "get_ai_signal":
        action = result.get("action", "HOLD")
        conf   = result.get("confidence", 0)
        reason = result.get("reasoning", "")
        sl     = result.get("stop_loss")
        tp     = result.get("take_profit")
        color  = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}.get(action, "white")
        if has_rich:
            console.print(f"  Signal: [{color}][bold]{action}[/bold][/{color}]  "
                          f"Confidence: [bold]{conf:.1%}[/bold]{prov_tag}")
            if reason:
                console.print(f"  [dim]{reason[:120]}[/dim]")
            if sl is not None:
                console.print(f"  [dim]Stop-loss: {sl}   Take-profit: {tp}[/dim]")
        else:
            print(f"  {action} ({conf:.1%}) — {reason[:80]}")
        return

    # ── Factors ────────────────────────────────────────────────────────
    if tool_name == "calculate_factors":
        sym = result.get("symbol", "")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"Factors — {sym}", show_header=True, box=None, padding=(0, 1))
            t.add_column("Factor", style="dim", width=24)
            t.add_column("Value",  justify="right")
            t.add_column("Signal", width=6)
            def _sig(v, neutral_lo=-0.1, neutral_hi=0.1):
                if v is None: return ""
                return "[green]▲[/green]" if v > neutral_hi else "[red]▼[/red]" if v < neutral_lo else "[yellow]─[/yellow]"
            FACTOR_ROWS = [
                ("rsi_14",          "RSI(14)",         lambda v: "[red]OB[/red]" if v and v > 70 else "[green]OS[/green]" if v and v < 30 else "[dim]─[/dim]"),
                ("macd_hist",       "MACD Hist",       lambda v: "[green]▲[/green]" if v and v > 0 else "[red]▼[/red]"),
                ("trend_score",     "Trend Score",     lambda v: _sig(v, -0.2, 0.2)),
                ("bb_position",     "BB Position",     lambda v: "[red]OB[/red]" if v and v > 0.9 else "[green]OS[/green]" if v and v < 0.1 else "[dim]─[/dim]"),
                ("ma_20_gap",       "vs MA20",         lambda v: "[green]▲[/green]" if v and v > 0 else "[red]▼[/red]"),
                ("ma_60_gap",       "vs MA60",         lambda v: "[green]▲[/green]" if v and v > 0 else "[red]▼[/red]"),
                ("volatility_20d",  "Vol(20d)",        lambda v: ""),
                ("volume_ratio_20d","Vol Ratio",       lambda v: "[green]⬆[/green]" if v and v > 1.5 else ""),
                ("return_5d",       "Return 5d",       lambda v: "[green]▲[/green]" if v and v > 0 else "[red]▼[/red]"),
                ("return_20d",      "Return 20d",      lambda v: "[green]▲[/green]" if v and v > 0 else "[red]▼[/red]"),
            ]
            for key, label, sig_fn in FACTOR_ROWS:
                v = result.get(key)
                if v is not None:
                    val_str = f"{v:+.4f}" if isinstance(v, float) else str(v)
                    t.add_row(label, val_str, sig_fn(v))
            console.print(t)
            console.print(f"  {prov_tag}")
        else:
            for k, v in result.items():
                if k not in ("success", "symbol", "provider") and isinstance(v, (int, float)):
                    print(f"  {k:<25s} {v:.5g}")
        return

    # ── Portfolio backtest (event-driven, multi-asset) ──────────────────
    if tool_name == "run_portfolio_backtest":
        syms = ", ".join(result.get("symbols", []) or [])
        strat = result.get("strategy", "")
        m = result.get("metrics", {}) or {}
        bench = (result.get("benchmark", {}) or {}).get("total_return")
        alpha = result.get("alpha_vs_buyhold")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"Portfolio Backtest — {syms}  [{strat}]", show_header=True, box=None)
            t.add_column("Metric", style="dim", width=24)
            t.add_column("Value",  justify="right")
            ROWS = [
                ("total_return",  "Total Return",  lambda v: f"[{'green' if v >= 0 else 'red'}]{v:+.2%}[/]"),
                ("annual_return", "Annual Return", lambda v: f"[{'green' if v >= 0 else 'red'}]{v:+.2%}[/]"),
                ("sharpe",        "Sharpe",        lambda v: f"[{'green' if v >= 1 else 'yellow' if v >= 0.5 else 'red'}]{v:.3f}[/]"),
                ("max_drawdown",  "Max Drawdown",  lambda v: f"[red]{v:.2%}[/red]"),
                ("win_rate",      "Win Rate",      lambda v: f"{v:.1%}"),
                ("n_trades",      "Trades",        lambda v: str(int(v))),
            ]
            for key, label, fmt_fn in ROWS:
                v = m.get(key)
                if v is not None:
                    t.add_row(label, fmt_fn(v))
            if bench is not None:
                t.add_row("Benchmark (B&H)", f"{bench:+.2%}")
            if alpha is not None:
                t.add_row("Alpha vs B&H", f"[{'green' if alpha >= 0 else 'red'}]{alpha:+.2%}[/]")
            console.print(t)
            miss = result.get("missing") or []
            if miss:
                console.print(f"  [yellow]未获取到数据: {', '.join(miss)}[/yellow]")
            console.print(f"  [dim]{result.get('n_points', 0)} bars[/dim]{prov_tag}")
        else:
            print(f"  {syms} [{strat}]: return={m.get('total_return')} sharpe={m.get('sharpe')} "
                  f"mdd={m.get('max_drawdown')} trades={m.get('n_trades')}")
        return

    # ── Backtest ───────────────────────────────────────────────────────
    if tool_name in ("backtest_strategy", "cloud_backtest"):
        sym  = result.get("symbol", result.get("symbols", ""))
        strat = result.get("strategy", result.get("model_type", ""))
        if has_rich:
            from rich.table import Table
            t = Table(title=f"Backtest — {sym}  [{strat}]", show_header=True, box=None)
            t.add_column("Metric",    style="dim", width=24)
            t.add_column("Value",     justify="right")
            PERF_ROWS = [
                ("total_return",    "Total Return",    lambda v: f"[{'green' if v >= 0 else 'red'}]{v:+.2%}[/]"),
                ("annual_return",   "Annual Return",   lambda v: f"[{'green' if v >= 0 else 'red'}]{v:+.2%}[/]"),
                ("sharpe_ratio",    "Sharpe Ratio",    lambda v: f"[{'green' if v >= 1 else 'yellow' if v >= 0.5 else 'red'}]{v:.3f}[/]"),
                ("sortino_ratio",   "Sortino Ratio",   lambda v: f"{v:.3f}"),
                ("max_drawdown",    "Max Drawdown",    lambda v: f"[red]{v:.2%}[/red]"),
                ("win_rate",        "Win Rate",        lambda v: f"{v:.1%}"),
                ("total_trades",    "Trades",          lambda v: str(int(v))),
                ("benchmark_return","Benchmark (B&H)", lambda v: f"{v:+.2%}"),
                ("alpha",           "Alpha",           lambda v: f"[{'green' if v >= 0 else 'red'}]{v:+.2%}[/]"),
            ]
            for key, label, fmt_fn in PERF_ROWS:
                v = result.get(key)
                if v is not None:
                    t.add_row(label, fmt_fn(v))
            console.print(t)
            console.print(f"  {result.get('start', '')} → {result.get('end', '')}  "
                          f"[dim]{result.get('bars', '')} bars[/dim]{prov_tag}")
        else:
            for k in ("total_return", "sharpe_ratio", "max_drawdown", "win_rate"):
                v = result.get(k)
                if v is not None:
                    print(f"  {k:<20s} {v:.4g}")
        return

    # ── Predictions ────────────────────────────────────────────────────
    if tool_name == "get_predictions":
        preds = result.get("predictions", [])
        days  = result.get("prediction_days", 5)
        if has_rich and preds:
            from rich.table import Table
            t = Table(title=f"ML Predictions ({days}d)", show_header=True, box=None)
            t.add_column("Symbol",   style="bold", width=12)
            t.add_column("Predicted Return", justify="right")
            t.add_column("Confidence",       justify="right")
            for p in preds:
                ret  = p.get("predicted_return", 0)
                conf = p.get("confidence", 0)
                color = "green" if ret >= 0 else "red"
                t.add_row(p["symbol"], f"[{color}]{ret:+.2%}[/{color}]", f"{conf:.0%}")
            console.print(t)
            console.print(f"  {prov_tag}")
        else:
            for p in preds:
                print(f"  {p.get('symbol')}: {p.get('predicted_return',0):+.2%}")
        return

    # ── Northbound flow ────────────────────────────────────────────────
    if tool_name == "get_northbound_flow":
        latest = result.get("latest_net_buy_yi", 0)
        total  = result.get("total_net_buy_yi", 0)
        trend  = result.get("trend", "")
        color  = "green" if latest >= 0 else "red"
        if has_rich:
            console.print(f"  北向资金  Today: [{color}][bold]{latest:+.2f}亿[/bold][/{color}]  "
                          f"Period Total: {total:+.2f}亿  [{trend}]{prov_tag}")
        else:
            print(f"  北向 Today: {latest:+.2f}亿  Period: {total:+.2f}亿")
        return

    # ── Market indices ────────────────────────────────────────────────
    if tool_name == "get_market_indices":
        indices = result.get("indices", result)
        if has_rich:
            from rich.table import Table
            t = Table(title="全球市场指数", show_header=True, box=None, padding=(0, 1))
            t.add_column("指数",   style="bold", width=16)
            t.add_column("最新价", justify="right")
            t.add_column("涨跌",   justify="right")
            # Handles both list-of-dicts (yfinance) and dict-of-dicts (legacy) formats
            rows = (
                [(d.get("name",""), d) for d in indices]
                if isinstance(indices, list)
                else [(k, v) for k, v in indices.items() if isinstance(v, dict)]
            )
            for name, d in rows:
                px  = d.get("price", d.get("latest_close", d.get("close", 0))) or 0
                chg = d.get("change_pct", d.get("changePercent", 0)) or 0
                color = "green" if chg >= 0 else "red"
                t.add_row(name or d.get("ticker",""), f"{px:,.2f}",
                          f"[{color}]{chg:+.2f}%[/{color}]")
            console.print(t)
            console.print(f"  [dim]{result.get('date','')}  {prov_tag}[/dim]")
        else:
            rows = indices if isinstance(indices, list) else list(indices.values())
            for d in rows[:10]:
                nm = d.get("name", d.get("ticker", ""))
                print(f"  {nm:<16} {d.get('price',0):>10,.2f}  {d.get('change_pct',0):+.2f}%")
        return

    # ── Risk metrics ──────────────────────────────────────────────────
    if tool_name == "get_risk_metrics":
        sym = result.get("symbol", "")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"[bold]{sym}[/bold] 风险指标", show_header=False, box=None, padding=(0,1))
            t.add_column(style="dim", width=22)
            t.add_column(justify="right")
            conf = result.get("confidence_level", 0.95)
            rows_r = [
                ("年化波动率",    f"{result.get('annual_volatility',0):.2%}"),
                ("年化收益率",    f"{result.get('annual_return',0):+.2%}"),
                (f"VaR({conf:.0%}) 日",  f"[red]{result.get('var_daily',0):.2%}[/red]"),
                (f"VaR({conf:.0%}) 月",  f"[red]{result.get('var_monthly',0):.2%}[/red]"),
                ("CVaR 日",      f"[red]{result.get('cvar_daily',0):.2%}[/red]"),
                ("最大回撤",      f"[red]{result.get('max_drawdown',0):.2%}[/red]"),
                ("Sharpe Ratio", f"{result.get('sharpe_ratio',0):.3f}"),
                ("Calmar Ratio", f"{result.get('calmar_ratio',0):.3f}"),
                ("偏度",         f"{result.get('skewness',0):.3f}"),
                ("峰度",         f"{result.get('kurtosis',0):.3f}"),
            ]
            for label, val in rows_r:
                t.add_row(label, val)
            console.print(t)
            console.print(f"  [dim]{prov_tag}[/dim]")
        else:
            for k in ("annual_volatility","var_daily","max_drawdown","sharpe_ratio"):
                print(f"  {k:<25} {result.get(k,0):.4g}")
        return

    # ── optimize_positions ────────────────────────────────────────────
    if tool_name == "optimize_positions":
        weights = result.get("weights", {})
        method  = result.get("method", "max_sharpe")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"组合优化 [{method}]", show_header=True, box=None, padding=(0,1))
            t.add_column("代码",   style="bold")
            t.add_column("权重",   justify="right")
            t.add_column("占比",   justify="right")
            for sym_k, w in sorted(weights.items(), key=lambda x: -x[1]):
                bar = "█" * int(w * 20)
                t.add_row(sym_k, f"{w:.2%}", f"[cyan]{bar}[/cyan]")
            console.print(t)
            p_ret = result.get("portfolio_return", 0)
            p_vol = result.get("portfolio_vol", 0)
            sr    = result.get("sharpe_ratio", 0)
            console.print(
                f"  期望收益 [bold]{p_ret:+.2%}[/bold]  "
                f"波动率 {p_vol:.2%}  "
                f"Sharpe [bold]{sr:.3f}[/bold]  [dim]{prov_tag}[/dim]"
            )
        else:
            for sym_k, w in weights.items():
                print(f"  {sym_k:<10} {w:.2%}")
        return

    # ── get_sector_performance ────────────────────────────────────────
    if tool_name == "get_sector_performance":
        top    = result.get("top_sectors", [])
        bottom = result.get("bottom_sectors", [])
        all_s  = result.get("sectors", top + bottom)
        mkt    = result.get("market", "")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"板块表现 [{mkt.upper()}]", show_header=True, box=None, padding=(0,1))
            t.add_column("板块", style="bold", min_width=14)
            t.add_column("涨跌", justify="right")
            for row in sorted(all_s, key=lambda x: -(x.get("change_pct") or 0)):
                chg   = row.get("change_pct") or 0
                color = "green" if chg >= 0 else "red"
                t.add_row(row.get("sector",""), f"[{color}]{chg:+.2f}%[/{color}]")
            console.print(t)
            console.print(f"  [dim]{result.get('date','')}  {prov_tag}[/dim]")
        else:
            for row in sorted(all_s, key=lambda x: -(x.get("change_pct") or 0))[:10]:
                print(f"  {row.get('sector',''):<16} {row.get('change_pct',0):+.2f}%")
        return

    # ── screen_ashare ─────────────────────────────────────────────────
    if tool_name == "screen_ashare":
        stocks = result.get("stocks", [])
        count  = result.get("count", len(stocks))
        if has_rich:
            from rich.markup import escape
            from rich.table import Table
            from ui.render.responsive import StackedRecord, render_stacked_records, structured_layout

            layout = structured_layout(console)
            if layout == "stacked":
                records = []
                for stock in stocks[:30]:
                    change = stock.get("change_pct") or 0
                    color = "green" if change >= 0 else "red"
                    pe = f"{stock.get('pe_dynamic', 0):.1f}" if stock.get("pe_dynamic") else "—"
                    market_cap = stock.get("market_cap_yi", (stock.get("market_cap") or 0) / 1e8)
                    records.append(StackedRecord(
                        headline=(
                            f"{escape(str(stock.get('code', '')))}  "
                            f"{escape(str(stock.get('name', '')))}"
                        ),
                        lines=(
                            f"[dim]价格[/dim] {stock.get('price', 0):.2f}  ·  "
                            f"[dim]涨跌[/dim] [{color}]{change:+.2f}%[/{color}]",
                            f"[dim]PE[/dim] {pe}  ·  [dim]市值[/dim] {market_cap:.0f} 亿",
                        ),
                    ))
                render_stacked_records(
                    console,
                    title=f"A股筛选 · 共 {count} 只",
                    records=records,
                    footer=str(prov_tag).strip(),
                )
                return
            t = Table(title=f"A股筛选  共 {count} 只", show_header=True, box=None, padding=(0,1))
            t.add_column("代码",   style="bold", width=8)
            t.add_column("名称",   width=10)
            t.add_column("价格",   justify="right")
            t.add_column("涨跌%",  justify="right")
            t.add_column("PE",     justify="right", style="dim")
            t.add_column("市值(亿)",justify="right", style="dim")
            for s in stocks[:30]:
                chg = s.get("change_pct") or 0
                color = "green" if chg >= 0 else "red"
                pe  = f"{s.get('pe_dynamic',0):.1f}" if s.get("pe_dynamic") else "—"
                mc  = f"{s.get('market_cap_yi', (s.get('market_cap') or 0)/1e8):.0f}"
                t.add_row(
                    str(s.get("code","")), str(s.get("name",""))[:10],
                    f"{s.get('price',0):.2f}",
                    f"[{color}]{chg:+.2f}%[/{color}]",
                    pe, mc,
                )
            console.print(t)
            console.print(f"  [dim]{prov_tag}[/dim]")
        else:
            for s in stocks[:20]:
                print(f"  {s.get('code','')} {s.get('name','')} {s.get('change_pct',0):+.2f}%")
        return

    # ── get_limit_up_pool ─────────────────────────────────────────────
    if tool_name == "get_limit_up_pool":
        stocks = result.get("stocks", [])
        count  = result.get("count", len(stocks))
        date_s = result.get("date", "")
        if has_rich:
            from rich.markup import escape
            from rich.table import Table
            from ui.render.responsive import StackedRecord, render_stacked_records, structured_layout

            if structured_layout(console) == "stacked":
                records = []
                for stock in stocks[:30]:
                    streak = stock.get("consecutive") or stock.get("limit_streak") or "—"
                    records.append(StackedRecord(
                        headline=(
                            f"{escape(str(stock.get('code', '')))}  "
                            f"{escape(str(stock.get('name', '')))}"
                        ),
                        lines=(
                            f"[dim]连板[/dim] {escape(str(streak))}  ·  "
                            f"[dim]类型[/dim] {escape(str(stock.get('limit_type') or '—'))}",
                            f"[dim]首封时间[/dim] {escape(str(stock.get('first_lock_time') or '—'))}",
                        ),
                    ))
                render_stacked_records(
                    console,
                    title=f"涨停板池 · {escape(str(date_s))} · 共 {count} 只",
                    records=records,
                    footer=str(prov_tag).strip(),
                )
                return
            t = Table(title=f"涨停板池  {date_s}  共 {count} 只",
                      show_header=True, box=None, padding=(0,1))
            t.add_column("代码",  style="bold", width=8)
            t.add_column("名称",  width=10)
            t.add_column("连板",  justify="right")
            t.add_column("首封时间", style="dim")
            t.add_column("类型",  style="dim")
            for s in stocks[:30]:
                consec = s.get("consecutive") or s.get("limit_streak") or ""
                t.add_row(
                    str(s.get("code","")), str(s.get("name",""))[:10],
                    str(consec), str(s.get("first_lock_time",""))[:8],
                    str(s.get("limit_type",""))[:6],
                )
            console.print(t)
            console.print(f"  [dim]{prov_tag}[/dim]")
        else:
            for s in stocks[:20]:
                print(f"  {s.get('code','')} {s.get('name','')} 连板:{s.get('consecutive','')}")
        return

    # ── get_futures_data / get_bonds_data ─────────────────────────────
    if tool_name == "get_futures_data":
        sym   = result.get("symbol", result.get("ticker", ""))
        price = result.get("price", result.get("current_price", 0)) or 0
        chg   = result.get("change_pct", result.get("changePercent", 0)) or 0
        vol   = result.get("volume", 0)
        if has_rich:
            color = "green" if chg >= 0 else "red"
            console.print(
                f"  [bold]{sym}[/bold]  {price:,.2f}  "
                f"[{color}]{chg:+.2f}%[/{color}]"
                + (f"  vol {vol:,.0f}" if vol else "")
                + f"  [dim]{prov_tag}[/dim]"
            )
        else:
            print(f"  {sym}  {price}  {chg:+.2f}%")
        return

    if tool_name == "get_bonds_data":
        yields = result.get("yields", {})
        spread = yields.get("10Y_2Y_spread")
        curve  = yields.get("curve_shape", "")
        if has_rich:
            from rich.table import Table
            t = Table(title="美国国债收益率", show_header=False, box=None, padding=(0,1))
            t.add_column(style="dim", width=6)
            t.add_column(justify="right")
            for tenor in ("2Y", "5Y", "10Y", "30Y"):
                y = yields.get(tenor)
                if y is not None:
                    t.add_row(tenor, f"[bold]{y:.3f}%[/bold]")
            console.print(t)
            if spread is not None:
                color = "green" if spread >= 0 else "red"
                console.print(
                    f"  10Y-2Y spread: [{color}]{spread:+.3f}%[/{color}]  "
                    f"[dim]{curve}  {prov_tag}[/dim]"
                )
        else:
            for tenor, y in yields.items():
                if isinstance(y, float):
                    print(f"  {tenor}: {y:.3f}%")
        return

    # ── get_market_insights ───────────────────────────────────────────
    if tool_name == "get_market_insights":
        summaries = result.get("summaries", [])
        note      = result.get("note", "")
        if has_rich:
            from rich.table import Table
            t = Table(title="市场洞察", show_header=True, box=None, padding=(0,1))
            t.add_column("代码",  style="bold", width=10)
            t.add_column("RSI",   justify="right")
            t.add_column("MACD Hist", justify="right")
            t.add_column("量比",  justify="right")
            t.add_column("趋势",  justify="right")
            for s in summaries:
                rsi_v = s.get("rsi_14") or 0
                mh    = s.get("macd_hist") or 0
                tr    = s.get("trend_score") or 0
                vr    = s.get("vol_ratio") or 1.0
                rsi_c = "red" if rsi_v > 70 else "green" if rsi_v < 30 else ""
                mh_c  = "green" if mh > 0 else "red"
                t.add_row(
                    s.get("symbol",""),
                    f"[{rsi_c}]{rsi_v:.1f}[/{rsi_c}]" if rsi_c else f"{rsi_v:.1f}",
                    f"[{mh_c}]{mh:+.4f}[/{mh_c}]",
                    f"{vr:.2f}x",
                    f"{'↑' if tr > 0 else '↓' if tr < 0 else '→'} {tr:.2f}" if tr else "—",
                )
            console.print(t)
            if note:
                console.print(f"  [dim]{note}[/dim]")
            console.print(f"  [dim]{prov_tag}[/dim]")
        else:
            for s in summaries:
                print(f"  {s.get('symbol','')} RSI:{s.get('rsi_14','')} MACD:{s.get('macd_hist','')}")
        return

    # ── calculate_factors ─────────────────────────────────────────────
    if tool_name == "calculate_factors":
        sym  = result.get("symbol", "")
        if has_rich:
            from rich.table import Table
            t = Table(title=f"[bold]{sym}[/bold] 因子分析", show_header=False, box=None, padding=(0,1))
            t.add_column(style="dim", width=22)
            t.add_column(justify="right")
            _FACTOR_LABELS = {
                "rsi_14":        "RSI(14)",
                "macd_hist":     "MACD Hist",
                "trend_score":   "趋势评分",
                "volume_ratio_20d": "量比(20d)",
                "volatility_20d":   "波动率(20d)",
                "bb_position":   "布林带位置",
                "return_5d":     "5日收益",
                "return_20d":    "20日收益",
                "return_60d":    "60日收益",
            }
            for key, label in _FACTOR_LABELS.items():
                val = result.get(key)
                if val is None:
                    continue
                if key in ("return_5d","return_20d","return_60d","volatility_20d"):
                    color = "green" if val > 0 else "red"
                    t.add_row(label, f"[{color}]{val:+.2%}[/{color}]")
                elif key == "rsi_14":
                    color = "red" if val > 70 else "green" if val < 30 else ""
                    t.add_row(label, f"[{color}]{val:.1f}[/{color}]" if color else f"{val:.1f}")
                elif key == "macd_hist":
                    color = "green" if val > 0 else "red"
                    t.add_row(label, f"[{color}]{val:+.4f}[/{color}]")
                elif key == "bb_position":
                    t.add_row(label, f"{val:.1%}  {'超买区' if val > 0.8 else '超卖区' if val < 0.2 else '中间带'}")
                else:
                    t.add_row(label, f"{val:.4g}")
            console.print(t)
            console.print(f"  [dim]{prov_tag}[/dim]")
        else:
            for k in ("rsi_14","macd_hist","trend_score","return_20d"):
                v = result.get(k)
                if v is not None:
                    print(f"  {k:<22} {v:.4g}")
        return

    # ── Generic fallback ──────────────────────────────────────────────
    if has_rich:
        # Show key=value pairs, skip large nested objects
        out = Text()
        for k, v in result.items():
            if k in ("success", "provider", "history_tail", "equity_curve", "trades"):
                continue
            if isinstance(v, (int, float)):
                color = "green" if v > 0 else "red" if v < 0 else ""
                out.append(f"  {k.replace('_',' ').title():<24s}", style="dim")
                out.append(f"{v:,.5g}\n", style=color)
            elif isinstance(v, str) and len(v) < 80:
                out.append(f"  {k.replace('_',' ').title():<24s}", style="dim")
                out.append(f"{v}\n")
        if str(out):
            console.print(out)
        if provider:
            console.print(f"  {prov_tag}")
    else:
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("success",) and not isinstance(v, list)},
                         indent=2, ensure_ascii=False, default=str)[:400])

    # ── Broker query results ───────────────────────────────────────────
    if tool_name in ("broker_query", "broker_order"):
        query   = result.get("query", "")
        broker  = result.get("broker", "券商")

        if query == "account":
            currency = result.get("currency", "CNY")
            total    = result.get("total_assets", 0)
            cash     = result.get("cash", 0)
            mv       = result.get("market_value", 0)
            pnl_day  = result.get("pnl_today", 0)
            pnl_tot  = result.get("pnl_total", 0)
            if has_rich:
                pday_c = "green" if pnl_day >= 0 else "red"
                ptot_c = "green" if pnl_tot >= 0 else "red"
                from rich.table import Table
                t = Table(show_header=False, box=None, padding=(0, 1))
                t.add_column(style="dim", width=16)
                t.add_column()
                t.add_row("账户", f"[bold]{broker}[/bold]  [dim]{result.get('account_id','****')}[/dim]")
                t.add_row("总资产", f"[bold]{currency} {total:,.2f}[/bold]")
                t.add_row("持仓市值", f"{mv:,.2f}")
                t.add_row("可用现金", f"{cash:,.2f}")
                if pnl_day:
                    t.add_row("当日盈亏", f"[{pday_c}]{pnl_day:+,.2f}[/{pday_c}]")
                if pnl_tot:
                    t.add_row("累计盈亏", f"[{ptot_c}]{pnl_tot:+,.2f}[/{ptot_c}]")
                console.print(t)
            else:
                print(f"{broker}: 总资产 {total:,.2f}  可用 {cash:,.2f}")
            return

        if query == "positions":
            positions = result.get("positions", [])
            if not positions:
                if has_rich:
                    console.print(f"[dim]{broker} — 当前无持仓[/dim]")
                return
            if has_rich:
                from rich.markup import escape
                from rich.table import Table
                from ui.render.responsive import StackedRecord, render_stacked_records, structured_layout

                ordered = sorted(positions, key=lambda x: -abs(x.get("market_value", 0)))
                total_mv = sum(p.get("market_value", 0) for p in positions)
                total_pnl = sum(p.get("pnl", 0) for p in positions)
                total_color = "green" if total_pnl >= 0 else "red"
                layout = structured_layout(console)
                if layout == "stacked":
                    records = []
                    for p in ordered:
                        pnl = p.get("pnl", 0)
                        pct = p.get("pnl_pct", 0)
                        color = "green" if pnl >= 0 else "red"
                        records.append(StackedRecord(
                            headline=(
                                f"{escape(str(p.get('symbol', '')))}  "
                                f"{escape(str(p.get('name') or '—'))}"
                            ),
                            lines=(
                                f"[dim]持仓[/dim] {p.get('quantity', 0):,.0f}",
                                f"[dim]成本[/dim] {p.get('cost', 0):.3f}  ·  "
                                f"[dim]现价[/dim] {p.get('price', 0):.3f}",
                                f"[dim]市值[/dim] {p.get('market_value', 0):,.2f}  ·  "
                                f"[dim]盈亏[/dim] [{color}]{pnl:+,.2f} ({pct:+.2f}%)[/{color}]",
                            ),
                        ))
                    render_stacked_records(
                        console,
                        title=f"{escape(str(broker))} 持仓",
                        records=records,
                        footer=(
                            f"共 {len(positions)} 只 · 总市值 {total_mv:,.2f} · "
                            f"总盈亏 [{total_color}]{total_pnl:+,.2f}[/{total_color}]"
                        ),
                    )
                    return
                t = Table(title=f"[bold]{broker}[/bold] 持仓", show_header=True, header_style="bold")
                t.add_column("代码",   style="bold", no_wrap=True)
                t.add_column("名称",   max_width=10)
                t.add_column("持仓",   justify="right")
                if layout == "full":
                    t.add_column("成本",   justify="right", style="dim")
                    t.add_column("现价",   justify="right")
                t.add_column("市值",   justify="right")
                t.add_column("盈亏",   justify="right")
                t.add_column("盈亏%",  justify="right")
                for p in ordered:
                    pnl = p.get("pnl", 0)
                    pct = p.get("pnl_pct", 0)
                    c   = "green" if pnl >= 0 else "red"
                    row = [
                        p.get("symbol", ""), p.get("name", "—")[:10],
                        f"{p.get('quantity', 0):,.0f}",
                    ]
                    if layout == "full":
                        row.extend([f"{p.get('cost', 0):.3f}", f"{p.get('price', 0):.3f}"])
                    row.extend([
                        f"{p.get('market_value',0):,.2f}",
                        f"[{c}]{pnl:+,.2f}[/{c}]",
                        f"[{c}]{pct:+.2f}%[/{c}]",
                    ])
                    t.add_row(*row)
                console.print(t)
                console.print(
                    f"  [dim]{len(positions)} 只  市值 {total_mv:,.2f}  "
                    f"总盈亏 [{total_color}]{total_pnl:+,.2f}[/{total_color}][/dim]"
                )
            else:
                for p in positions:
                    print(f"  {p.get('symbol',''):<8} {p.get('name',''):<10} 持仓:{p.get('quantity',0):.0f}  盈亏:{p.get('pnl',0):+,.2f}")
            return

        if query == "orders":
            orders = result.get("orders", [])
            if not orders:
                if has_rich:
                    console.print(f"[dim]{broker} — 无订单记录[/dim]")
                return
            if has_rich:
                from rich.markup import escape
                from rich.table import Table
                from ui.render.responsive import StackedRecord, render_stacked_records, structured_layout

                layout = structured_layout(console)
                _ss = {"filled":"[green]成交[/green]","partial":"[yellow]部成[/yellow]",
                       "open":"[cyan]委托中[/cyan]","cancelled":"[dim]已撤[/dim]"}
                _sd = {"buy":"[green]买入[/green]","sell":"[red]卖出[/red]"}
                if layout == "stacked":
                    records = []
                    for o in orders:
                        avg_price = f"{o.get('avg_price', 0):.3f}" if o.get("avg_price") else "—"
                        records.append(StackedRecord(
                            headline=f"{escape(str(o.get('symbol', '')))}",
                            lines=(
                                f"[dim]方向[/dim] {_sd.get(o.get('side', ''), escape(str(o.get('side', ''))))}  ·  "
                                f"[dim]状态[/dim] {_ss.get(o.get('status', ''), escape(str(o.get('status', ''))))}",
                                f"[dim]委托[/dim] {o.get('quantity', 0):,.0f} @ {o.get('price', 0):.3f}",
                                f"[dim]成交[/dim] {o.get('filled', 0):,.0f} @ {avg_price}",
                                f"[dim]时间[/dim] {escape(str(o.get('time') or '—'))}",
                            ),
                        ))
                    render_stacked_records(
                        console,
                        title=f"{escape(str(broker))} 订单",
                        records=records,
                        footer=f"共 {len(orders)} 笔",
                    )
                    return
                t = Table(title=f"[bold]{broker}[/bold] 订单", show_header=True, header_style="bold")
                t.add_column("代码",  style="bold")
                t.add_column("方向",  justify="center")
                t.add_column("委托量", justify="right")
                t.add_column("委托价", justify="right", style="dim")
                t.add_column("状态")
                t.add_column("时间",   style="dim", max_width=14)
                if layout == "full":
                    t.add_column("成交量", justify="right")
                    t.add_column("均价",   justify="right")
                for o in orders:
                    row = [
                        o.get("symbol",""),
                        _sd.get(o.get("side",""), o.get("side","")),
                        f"{o.get('quantity',0):,.0f}",
                        f"{o.get('price',0):.3f}",
                        _ss.get(o.get("status",""), o.get("status","")),
                        str(o.get("time",""))[:14],
                    ]
                    if layout == "full":
                        row.extend([
                            f"{o.get('filled', 0):,.0f}",
                            f"{o.get('avg_price', 0):.3f}" if o.get("avg_price") else "—",
                        ])
                    t.add_row(*row)
                console.print(t)
            else:
                for o in orders:
                    print(f"  {o.get('symbol','')} {o.get('side','')} {o.get('quantity',0):.0f} @ {o.get('price',0):.3f} [{o.get('status','')}]")
            return

        # ── broker_order: confirmation required ────────────────────────
        if tool_name == "broker_order" and result.get("confirmation_required"):
            preview = result.get("order_preview", {})
            if has_rich:
                from rich.panel import Panel
                from rich import box as _rbox
                _side_cn = preview.get("side_cn", preview.get("side", ""))
                _side_color = "green" if preview.get("side") == "buy" else "red"
                _preview_id = preview.get("preview_id") or result.get("preview_id") or ""
                _mode = preview.get("mode") or (result.get("trade_preview") or {}).get("mode") or ""
                _broker = preview.get("broker") or (result.get("trade_preview") or {}).get("broker_label") or ""
                _blockers = (result.get("trade_preview") or {}).get("execution_blockers") or []
                _body = (
                    f"preview_id: [bold]{_preview_id}[/bold]\n"
                    f"模式: [bold]{_mode or '—'}[/bold]  券商: [bold]{_broker or '—'}[/bold]\n\n"
                    f"[bold]{_side_cn}[/bold]  "
                    f"[bold]{preview.get('symbol','')}[/bold]  "
                    f"数量: [bold]{preview.get('qty', 0):,}[/bold]  "
                    f"价格: [bold]{preview.get('price_display','市价')}[/bold]\n\n"
                    "[yellow]确认执行时必须携带 preview_id · 其他任何回复取消[/yellow]"
                )
                if _blockers:
                    _body += "\n\n[red]执行限制:[/red]\n" + "\n".join(f"  - {b}" for b in _blockers)
                console.print(Panel(
                    _body,
                    title="[yellow]⚠ 订单确认[/yellow]",
                    border_style="yellow",
                    box=_rbox.ROUNDED,
                    padding=(0, 1),
                ))
            else:
                msg = result.get("message", "请确认订单")
                print(f"\n⚠ 订单确认\n{msg}\n")
            return

        # ── broker_order: placed successfully ──────────────────────────
        if tool_name == "broker_order" and result.get("success"):
            if has_rich:
                _side_cn = "买入" if result.get("side") == "buy" else "卖出"
                console.print(
                    f"[green]✓ 订单已提交[/green]  {result.get('broker','')} — "
                    f"{_side_cn} [bold]{result.get('symbol','')}[/bold] "
                    f"× {result.get('qty',0):,}  "
                    f"[dim]#{result.get('order_id','—')}  {result.get('status','')}[/dim]"
                )
            else:
                print(f"✓ 订单已提交: {result}")
            return


def render_macro_result(r: dict, title: str, *, console=None, has_rich: bool = True,
                        lang: str = "zh") -> None:
    """Render US or CN macro result dict."""
    zh = str(lang).lower().startswith("zh")
    if not r.get("success"):
        error = r.get("error", "数据获取失败" if zh else "data unavailable")
        if "akshare" in str(error):
            # China data is optional; a missing package is a note, not a red error.
            error = ("中国宏观数据需要 akshare：pip install akshare" if zh
                     else "China macro data needs akshare: pip install akshare")
            if has_rich: console.print(f"  [dim]{error}[/dim]")
            else: print(f"  {error}")
            return
        if has_rich: console.print(f"  [red]{error}[/red]")
        else: print(f"  {error}")
        return
    data = r.get("data", {})
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        console.print(Rule(f"[bold]{title}[/bold]", style="dim"))
        t = Table(show_header=True, box=None, padding=(0, 1))
        t.add_column("指标" if zh else "Indicator", style="dim", min_width=20)
        t.add_column("最新值" if zh else "Latest", justify="right", min_width=10)
        t.add_column("环比变化" if zh else "Change", justify="right")
        t.add_column("时间" if zh else "Date", style="dim")
        for key, item in data.items():
            if key.startswith("_"): continue
            if not isinstance(item, dict): continue
            latest = item.get("latest", {}) or {}
            val    = latest.get("value")
            date   = latest.get("date", "")
            change = item.get("change")
            unit   = item.get("unit", "")
            label  = item.get("label", key) if zh else item.get("label_en", item.get("label", key))
            if val is None: continue
            val_str = f"{val:.2f}{unit}"
            if change is not None:
                color = "green" if change > 0 else "red" if change < 0 else ""
                chg_str = f"[{color}]{change:+.3f}[/{color}]" if color else f"{change:+.3f}"
            else:
                chg_str = "—"
            t.add_row(label, val_str, chg_str, str(date)[:10])
        console.print(t)
        yc = data.get("_yield_curve", {})
        if yc:
            sp = yc.get("spread_10y_2y", 0)
            shape = yc.get("shape", "") if zh else yc.get("shape_en", yc.get("shape", ""))
            color = "green" if sp > 0 else "red"
            if zh:
                console.print(f"  收益率曲线: [{color}]{shape}[/{color}]  10Y-2Y利差: [{color}]{sp:+.3f}%[/{color}]")
            else:
                console.print(f"  Yield curve: [{color}]{shape}[/{color}]  10Y-2Y spread: [{color}]{sp:+.3f}%[/{color}]")
    else:
        print(f"\n{title}")
        for key, item in data.items():
            if not isinstance(item, dict) or key.startswith("_"): continue
            v = (item.get("latest") or {}).get("value")
            if v is not None:
                print(f"  {(item.get('label', key) if zh else item.get('label_en', key)):<28} {v:.3g}")


def render_cb_rates(r: dict, *, console=None, has_rich: bool = True, lang: str = "zh") -> None:
    """Render central bank rates, each with its date; a stale value is marked, not shown as current."""
    zh = str(lang).lower().startswith("zh")
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    details = r.get("details") or [
        {"label_zh": name, "label_en": name, "value": val, "date": "", "stale": False}
        for name, val in (r.get("rates") or {}).items()
    ]
    title = "主要央行政策利率" if zh else "Central bank rates"
    if has_rich:
        from rich.rule import Rule
        from rich.table import Table
        console.print(Rule(f"[bold]{title}[/bold]", style="dim"))
        t = Table(show_header=False, box=None, padding=(0,1))
        t.add_column(style="dim", min_width=28)
        t.add_column(justify="right")
        t.add_column(style="dim")
        for row in details:
            if row.get("value") is None:
                continue
            label = row["label_zh"] if zh else row["label_en"]
            if row.get("stale"):
                note = (f"数据停在 {row['date']}，未显示" if zh else f"no data since {row['date']}, not shown")
                t.add_row(label, "—", note)
            else:
                t.add_row(label, f"[bold]{row['value']:.2f}%[/bold]", row.get("date", ""))
        console.print(t)
    else:
        print("\n" + title)
        for row in details:
            if row.get("value") is not None and not row.get("stale"):
                print(f"  {(row['label_zh'] if zh else row['label_en']):<30} {row['value']:.2f}%  {row.get('date', '')}")


def render_econ_calendar(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render economic calendar."""
    events = r.get("events", [])
    if has_rich:
        from rich.rule import Rule
        from rich.table import Table
        console.print(Rule("[bold]📅 经济事件日历[/bold]", style="dim"))
        t = Table(show_header=True, box=None, padding=(0,1))
        t.add_column("时间", style="dim", width=12)
        t.add_column("事件", min_width=30)
        t.add_column("重要性", justify="center")
        for ev in events[:15]:
            imp = ev.get("importance", ev.get("importance_level",""))
            imp_colored = (
                "[red]HIGH[/red]" if str(imp).upper() in ("HIGH","3","★★★")
                else "[yellow]MED[/yellow]" if str(imp).upper() in ("MEDIUM","2","★★")
                else f"[dim]{imp}[/dim]"
            )
            t.add_row(
                str(ev.get("time","") or ev.get("date",""))[:12],
                str(ev.get("event","") or ev.get("title",""))[:45],
                imp_colored,
            )
        console.print(t)
    else:
        for ev in events[:10]:
            print(f"  {ev.get('event','')} [{ev.get('importance','')}]")


def render_options_chain(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render options chain."""
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    symbol = r.get("symbol","")
    price  = r.get("price", 0)
    expiry = r.get("expiry","")
    all_exp = r.get("all_expiries", [])
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        console.print(Rule(f"[bold]{symbol}[/bold] 期权链  到期: [cyan]{expiry}[/cyan]  现价: [bold]{price:.2f}[/bold]", style="dim"))
        if all_exp:
            console.print(f"  [dim]可用到期日: {', '.join(all_exp)}[/dim]")
        for side in ("calls", "puts"):
            rows = r.get(side, [])
            if not rows: continue
            label = "认购期权 (Calls)" if side == "calls" else "认沽期权 (Puts)"
            t = Table(title=f"[bold]{label}[/bold]", show_header=True, box=None, padding=(0,1))
            t.add_column("行权价",  justify="right", style="bold")
            t.add_column("最新价",  justify="right")
            t.add_column("买/卖",   justify="right", style="dim")
            t.add_column("IV%",     justify="right")
            t.add_column("OI",      justify="right")
            t.add_column("价内?",   justify="center")
            for row in rows:
                itm  = row.get("inTheMoney", False)
                itm_s = "[green]✓[/green]" if itm else "[dim]—[/dim]"
                bid   = row.get("bid", 0) or 0
                ask   = row.get("ask", 0) or 0
                t.add_row(
                    f"{row.get('strike',0):.2f}",
                    f"{row.get('lastPrice',0):.2f}",
                    f"{bid:.2f}/{ask:.2f}",
                    f"{row.get('iv_pct',0):.1f}%",
                    f"{int(row.get('openInterest',0)):,}",
                    itm_s,
                )
            console.print(t)
    else:
        for side in ("calls","puts"):
            print(f"\n{side.upper()}")
            for row in r.get(side, []):
                print(f"  K={row.get('strike')} last={row.get('lastPrice')} IV={row.get('iv_pct')}%")


def render_quality_scores(symbol: str, f_r: dict, z_r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render Piotroski F-Score + Altman Z-Score side by side."""
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        from rich.columns import Columns
        from rich.panel import Panel
        from rich import box as _rbox

        console.print(Rule(f"[bold]{symbol}[/bold] 财务质量双维评估", style="dim"))

        # F-Score panel
        if f_r.get("success"):
            fs   = f_r["f_score"]
            sig  = f_r.get("signal","")
            color = "green" if sig == "bullish" else "red" if sig == "bearish" else "yellow"
            bars  = "█" * fs + "░" * (9 - fs)
            f_body = (
                f"[bold {color}]{fs}/9[/bold {color}]  [{color}]{f_r.get('verdict','')}[/{color}]\n"
                f"[{color}]{bars}[/{color}]\n\n"
            )
            scores = f_r.get("scores", {})
            categories = [
                ("盈利能力", ["F1_ROA_positive","F2_CFO_positive","F3_ROA_increasing","F4_CFO_gt_ROA"]),
                ("杠杆/流动性", ["F5_Leverage_lower","F6_CurrentRatio_up","F7_NoDilution"]),
                ("运营效率", ["F8_GrossMargin_up","F9_AssetTurnover_up"]),
            ]
            for cat, keys in categories:
                f_body += f"[dim]{cat}[/dim]\n"
                for k in keys:
                    v = scores.get(k, 0)
                    check = "[green]✓[/green]" if v else "[dim]✗[/dim]"
                    f_body += f"  {check} {k[3:].replace('_',' ')}\n"
            f_panel = Panel(f_body.strip(), title="[bold]Piotroski F-Score[/bold]",
                            border_style=color, box=_rbox.ROUNDED, padding=(0,1))
        else:
            f_panel = Panel(f"[red]{f_r.get('error','失败')}[/red]",
                            title="Piotroski F-Score", border_style="red")

        # Z-Score panel
        if z_r.get("success"):
            zs    = z_r["z_score"]
            risk  = z_r.get("risk","medium")
            zone  = z_r.get("zone","")
            zcolor = "green" if risk == "low" else "red" if risk == "high" else "yellow"
            z_body = (
                f"[bold {zcolor}]Z'' = {zs:.3f}[/bold {zcolor}]\n"
                f"[{zcolor}]{zone}[/{zcolor}]\n\n"
                f"[dim]安全区 >2.6  |  灰色区 1.1-2.6  |  风险区 <1.1[/dim]\n\n"
            )
            comp = z_r.get("components", {})
            labels = {
                "X1_working_capital_ratio":    "X1 营运资本/总资产",
                "X2_retained_earnings_ratio":  "X2 留存收益/总资产",
                "X3_ebit_ratio":               "X3 EBIT/总资产",
                "X4_equity_to_debt":           "X4 权益/负债",
            }
            weights = {"X1": 6.56, "X2": 3.26, "X3": 6.72, "X4": 1.05}
            for k, label in labels.items():
                v = comp.get(k, 0)
                prefix = k[:2]
                contrib = round(v * weights.get(prefix, 1), 3)
                c = "green" if v >= 0 else "red"
                z_body += f"  [dim]{label}[/dim]: [{c}]{v:.4f}[/{c}] (贡献 {contrib:+.3f})\n"
            z_body += f"\n[dim]{z_r.get('formula','')}[/dim]"
            z_panel = Panel(z_body.strip(), title="[bold]Altman Z''-Score[/bold]",
                            border_style=zcolor, box=_rbox.ROUNDED, padding=(0,1))
        else:
            z_panel = Panel(f"[red]{z_r.get('error','失败')}[/red]",
                            title="Altman Z-Score", border_style="red")

        console.print(Columns([f_panel, z_panel]))
    else:
        if f_r.get("success"):
            print(f"\nPiotroski F-Score: {f_r['f_score']}/9  {f_r.get('verdict','')}")
        if z_r.get("success"):
            print(f"Altman Z''-Score: {z_r['z_score']}  {z_r.get('zone','')}")


def render_ichimoku(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render Ichimoku Cloud analysis."""
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        sym  = r.get("symbol","")
        sig  = r.get("signal","")
        price = r.get("price", 0)
        is_bull = r.get("above_cloud", False)
        sig_color = "green" if is_bull else "red" if r.get("below_cloud") else "yellow"
        console.print(Rule(f"[bold]{sym}[/bold] 一目均衡表  [{sig_color}]{sig}[/{sig_color}]", style="dim"))
        t = Table(show_header=False, box=None, padding=(0,1))
        t.add_column(style="dim", width=18)
        t.add_column(justify="right")
        t.add_column(style="dim")
        t.add_row("现价",      f"[bold]{price:.3f}[/bold]", "")
        t.add_row("转换线 (9)", f"{r.get('tenkan',0):.3f}",
                  "[green]↑ 多头[/green]" if r.get('tenkan',0) > r.get('kijun',0) else "[red]↓ 空头[/red]")
        t.add_row("基准线 (26)",f"{r.get('kijun',0):.3f}",  "")
        if r.get("senkou_a"):
            t.add_row("先行带A",   f"{r.get('senkou_a',0):.3f}", "")
        if r.get("senkou_b"):
            t.add_row("先行带B",   f"{r.get('senkou_b',0):.3f}", "")
        t.add_row("云层厚度",   f"{r.get('cloud_thickness',0):.3f}", r.get("cloud_color",""))
        t.add_row("TK交叉",    r.get("tk_cross",""),    "")
        console.print(t)
        console.print(f"  [bold {sig_color}]结论: {sig}[/bold {sig_color}]  [dim]先行带偏移 26期，迟行线(Chikou)={r.get('chikou',0):.3f}[/dim]")
    else:
        print(f"\n{r.get('symbol','')} 一目均衡表")
        for k in ("tenkan","kijun","senkou_a","senkou_b","signal"):
            print(f"  {k}: {r.get(k,'')}")


def render_fear_greed(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render Fear & Greed Index with ASCII gauge."""
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    val   = r.get("value", 50)
    label = r.get("label", "")
    sig   = r.get("signal", "中性")
    if has_rich:
        from rich.rule import Rule
        color = "green" if val <= 25 else "red" if val >= 75 else "yellow"
        # ASCII gauge
        filled = int(val / 5)
        bar    = "█" * filled + "░" * (20 - filled)
        console.print(Rule("[bold]₿ 加密恐惧贪婪指数[/bold]", style="dim"))
        console.print(f"\n  [{color}]{bar}[/{color}]  [bold {color}]{val}/100[/bold {color}]  [{color}]{label}[/{color}]\n")
        console.print(f"  操作信号: [bold]{sig}[/bold]  [dim]>75 极度贪婪(卖出信号)  <25 极度恐惧(买入信号)[/dim]\n")
        hist = r.get("history", [])
        if len(hist) > 1:
            hist_str = "  [dim]近7天: "
            for h in hist[:7]:
                v = h.get("value", 0)
                c = "green" if v <= 25 else "red" if v >= 75 else "yellow"
                hist_str += f"[{c}]{v}[/{c}] "
            console.print(hist_str + "[/dim]")
    else:
        print(f"\n恐惧贪婪指数: {val}/100 ({label})  信号: {sig}")


def render_funding_rates(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render perpetual funding rates."""
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    rates = r.get("rates", [])
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        exchange = r.get("exchange","")
        bias = r.get("market_bias","")
        console.print(Rule(f"[bold]{exchange.upper()}[/bold] 永续合约资金费率", style="dim"))
        t = Table(show_header=True, box=None, padding=(0,1))
        t.add_column("合约",  style="bold", width=12)
        t.add_column("费率",  justify="right")
        t.add_column("年化",  justify="right")
        t.add_column("下次结算", style="dim")
        t.add_column("信号",  justify="center")
        for rt in rates:
            rate = rt.get("rate", 0)
            color = "red" if rate > 0.05 else "green" if rate < -0.01 else "dim"
            sig_s = rt.get("signal","中性")
            sig_c = "red" if sig_s == "空" else "green" if sig_s == "多" else "dim"
            t.add_row(
                rt.get("symbol",""),
                f"[{color}]{rt.get('rate_pct','')}[/{color}]",
                rt.get("annualized",""),
                rt.get("next_funding","")[:12],
                f"[{sig_c}]{sig_s}[/{sig_c}]",
            )
        console.print(t)
        console.print(f"  [dim]市场偏向: [bold]{bias}[/bold]  正费率=多头付费给空头，负费率=空头付费给多头[/dim]")
    else:
        for rt in rates:
            print(f"  {rt.get('symbol','')} {rt.get('rate_pct','')} (年化{rt.get('annualized','')})")


def render_peer_comparison(r: dict, *, console=None, has_rich: bool = True) -> None:
    """Render peer comparison table."""
    if not r.get("success"):
        if has_rich: console.print(f"  [red]{r.get('error')}[/red]")
        return
    rows = r.get("table", [])
    if has_rich:
        from rich.table import Table
        from rich.rule import Rule
        symbol = r.get("symbol","")
        zh = str(r.get("lang", "zh")).lower().startswith("zh")
        console.print(Rule(f"[bold]{symbol}[/bold] " + ("同行估值对比" if zh else "peer valuation"), style="dim"))
        t = Table(show_header=True, box=None, padding=(0,1))
        t.add_column("代码" if zh else "Symbol", style="bold",  width=8)
        t.add_column("名称" if zh else "Name",   width=14)
        t.add_column("PE",        justify="right")
        t.add_column("PB",        justify="right")
        t.add_column("ROE%",      justify="right")
        t.add_column("股息%" if zh else "Div%",   justify="right")
        t.add_column("市值(B)" if zh else "Mkt cap (B)", justify="right", style="dim")
        for row in rows:
            is_t = row.get("is_target", False)
            pe   = f"{row['pe']:.1f}" if row.get("pe") else "—"
            pb   = f"{row['pb']:.2f}" if row.get("pb") else "—"
            roe  = f"{row['roe_pct']:.1f}" if row.get("roe_pct") else "—"
            dy   = f"{row['div_yield']:.2f}" if row.get("div_yield") else "—"
            mc   = f"{row['market_cap_b']:.0f}" if row.get("market_cap_b") else "—"
            # Highlight target row
            style = "bold cyan" if is_t else ""
            t.add_row(
                f"[{style}]{row['symbol']}[/{style}]" if style else row["symbol"],
                (row.get("name","") or "")[:14],
                pe, pb, roe, dy, mc,
            )
        console.print(t)
        analysis = r.get("analysis", [])
        for line in analysis:
            console.print(f"  [dim]▸ {line}[/dim]")
    else:
        zh = str(r.get("lang", "zh")).lower().startswith("zh")
        print(f"\n{r.get('symbol','')} " + ("同行对比" if zh else "peer comparison"))
        for row in rows:
            print(f"  {row['symbol']:<8} PE:{row.get('pe','—')} PB:{row.get('pb','—')}")


# ── 不动产渲染函数 ─────────────────────────────────────────────────────────────

def render_house_price(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','获取失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    c1 = str(r.get("city1") or "城市1")
    c2 = str(r.get("city2") or "城市2")
    lc1, lc2 = r.get("latest_city1") or {}, r.get("latest_city2") or {}

    def _fmt(v):
        if v is None: return "[dim]—[/dim]"
        fv = float(v) if not isinstance(v, float) else v
        color = "green" if fv > 0 else "red" if fv < 0 else "dim"
        return f"[{color}]{fv:+.2f}%[/{color}]"

    tb = _T(title="[bold]🏠 房价指数对比[/bold]", box=_box.ROUNDED, show_header=True)
    tb.add_column("指标", style="dim")
    tb.add_column(c1, justify="right")
    tb.add_column(c2, justify="right")
    tb.add_row("新建商品房同比", _fmt(lc1.get("new_yoy")), _fmt(lc2.get("new_yoy")))
    tb.add_row("新建商品房环比", _fmt(lc1.get("new_mom")), _fmt(lc2.get("new_mom")))
    tb.add_row("二手房价同比",   _fmt(lc1.get("second_yoy")), _fmt(lc2.get("second_yoy")))
    tb.add_row("二手房价环比",   _fmt(lc1.get("second_mom")), _fmt(lc2.get("second_mom")))
    tb.add_row("[dim]数据期[/dim]", str(lc1.get("date") or "—"), str(lc2.get("date") or "—"))
    console.print(tb)
    console.print("[dim]数据来源：国家统计局 via akshare[/dim]")


def render_reits_list(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        for row in r.get("reits", [])[:10]: print(row); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','获取失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    tb = _T(title="[bold]🏗 中国 REITs 实时行情[/bold]", box=_box.ROUNDED)
    tb.add_column("代码", style="cyan")
    tb.add_column("名称")
    tb.add_column("最新价", justify="right")
    tb.add_column("涨跌幅", justify="right")
    tb.add_column("昨收", justify="right", style="dim")
    tb.add_column("成交额(万)", justify="right", style="dim")
    for row in r.get("reits", [])[:20]:
        chg = row.get("涨跌幅") or 0
        try: chg_f = float(chg)
        except Exception: chg_f = 0
        color = "green" if chg_f > 0 else "red" if chg_f < 0 else "dim"
        vol_wan = ""
        try: vol_wan = f"{float(row.get('成交额',0))/10000:.0f}"
        except Exception as _e: logger.debug("vol_wan parse error: %s", _e)
        tb.add_row(
            str(row.get("代码","")),
            str(row.get("名称",""))[:12],
            str(row.get("最新价","")),
            f"[{color}]{chg}%[/{color}]",
            str(row.get("昨收","")),
            vol_wan,
        )
    console.print(tb)
    console.print(f"[dim]共 {r.get('count',0)} 只 REITs · 数据来源：东方财富[/dim]")


def render_rental_yield(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','计算失败')}[/red]"); return
    from rich.panel import Panel as _P
    from rich.columns import Columns as _C
    from rich import box as _box
    assess_color = "green" if "优质" in str(r.get("assessment","")) else \
                   "yellow" if "合理" in str(r.get("assessment","")) else "red"
    lines = [
        f"[bold cyan]购入价格[/bold cyan]   {r['purchase_price_wan']:.1f} 万元",
        f"[bold cyan]月租金[/bold cyan]     {r['monthly_rent']:.0f} 元/月",
        "[dim]───────────────────────────────[/dim]",
        f"[bold]毛租金收益率[/bold]  [{assess_color}]{r['gross_yield_pct']:.2f}%[/{assess_color}]",
        f"[bold]净收益率[/bold]      {r['net_yield_pct']:.2f}%",
        f"[bold]资本化率[/bold]      {r['cap_rate_pct']:.2f}%",
        f"[bold]回本年限[/bold]      {r['payback_years']:.1f} 年",
    ]
    if r.get("leveraged_yield_pct") is not None:
        lines.append(f"[bold]杠杆收益率[/bold]    {r['leveraged_yield_pct']:.2f}%  [dim](含贷款)[/dim]")
    lines += [
        "[dim]───────────────────────────────[/dim]",
        f"[{assess_color}]综合评级：{r.get('assessment','')}[/{assess_color}]",
        f"[dim]{r.get('benchmark','')}[/dim]",
    ]
    console.print(_P("\n".join(lines), title="[bold]💰 租金收益率分析[/bold]",
                     border_style="cyan"))


def render_property_val(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','估值失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    from rich.panel import Panel as _P
    verd = r.get("verdict","")
    verd_color = "green" if "低估" in verd else "red" if "高估" in verd else "yellow"
    tb = _T(title="[bold]🏢 物业三合一估值[/bold]", box=_box.ROUNDED)
    tb.add_column("方法", style="dim")
    tb.add_column("估值(万元)", justify="right")
    tb.add_column("说明", style="dim")
    tb.add_row("收益法 (Cap Rate)", f"{r['income_approach']:.1f}",   f"资本化率 {r['cap_rate_used']:.1f}%")
    tb.add_row("DCF 折现法",        f"{r['dcf_approach']:.1f}",     f"折现率 {r['discount_rate_used']:.1f}%")
    tb.add_row("市场比较法",        f"{r['market_approach']:.1f}", "基于租金倍数推算")
    tb.add_row("[bold]综合估值[/bold]", f"[bold cyan]{r['blended_value_wan']:.1f}[/bold cyan]", "权重 4:4:2")
    console.print(tb)
    lo, hi = r.get("market_range_wan", [0, 0])
    console.print(f"  区位参考区间: [dim]{lo:.0f} — {hi:.0f} 万元[/dim]")
    console.print(f"  单价参考: [bold]{r.get('price_per_sqm',0):,.0f}[/bold] 元/㎡")
    console.print(f"  [{verd_color}]{verd}[/{verd_color}]")


def render_multi_city(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        for c in r.get("cities", []): print(c); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','获取失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    tb = _T(title="[bold]🗺 多城市房价对比[/bold]", box=_box.ROUNDED)
    tb.add_column("城市", style="bold")
    tb.add_column("等级", style="dim")
    tb.add_column("数据期", style="dim")
    tb.add_column("新房同比", justify="right")
    tb.add_column("新房环比", justify="right")
    tb.add_column("二手同比", justify="right")
    for city in r.get("cities", []):
        def _fc(v):
            if v is None: return "[dim]—[/dim]"
            c = "green" if v > 0 else "red" if v < 0 else "dim"
            return f"[{c}]{v:+.2f}%[/{c}]"
        tb.add_row(
            city["city"], city.get("tier",""),
            city.get("date",""),
            _fc(city.get("new_yoy")), _fc(city.get("new_mom")),
            _fc(city.get("second_yoy")),
        )
    console.print(tb)
    console.print(f"  涨幅最高: [green]{r.get('top_riser','')}[/green]  "
                  f"涨幅最低/下跌: [red]{r.get('top_faller','')}[/red]")


def render_asset_score(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','评分失败')}[/red]"); return
    from rich.panel import Panel as _P
    score = r.get("score", 0)
    rating = r.get("rating", "")
    color = "green" if score >= 75 else "yellow" if score >= 60 else "red"
    bar_len = int(score / 5)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    lines = [
        f"综合评分: [{color}]{score}[/{color}] / 100",
        f"评级: [{color}]{rating}[/{color}]",
        f"[{color}]{bar}[/{color}]",
        "[dim]─────────────────────────────[/dim]",
    ]
    for k, v in r.get("breakdown", {}).items():
        lines.append(f"  {k:<12} [dim]{v}[/dim]")
    if r.get("suitable_businesses"):
        lines.append("[dim]─────────────────────────────[/dim]")
        lines.append(f"推荐业态: [cyan]{' / '.join(r['suitable_businesses'])}[/cyan]")
    console.print(_P("\n".join(lines), title="[bold]📍 资产区位评分[/bold]",
                     border_style=color))


# ── 数据分析渲染函数 ────────────────────────────────────────────────────────────

def render_corr_matrix(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r.get("corr_matrix", {}), ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','计算失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    syms = r.get("symbols", [])
    corr = r.get("corr_matrix", {})
    tb = _T(title=f"[bold]📊 相关性矩阵 ({r.get('period','')}/{r.get('interval','1d')})[/bold]",
            box=_box.SIMPLE_HEAVY)
    tb.add_column("", style="bold")
    for s in syms:
        tb.add_column(s, justify="right")
    for s1 in syms:
        row_vals = []
        for s2 in syms:
            v = corr.get(s1, {}).get(s2)
            if v is None:
                row_vals.append("[dim]—[/dim]")
            elif s1 == s2:
                row_vals.append("[dim]1.00[/dim]")
            else:
                abs_v = abs(v)
                color = "red" if abs_v > 0.8 else "yellow" if abs_v > 0.5 else "green"
                row_vals.append(f"[{color}]{v:+.3f}[/{color}]")
        tb.add_row(f"[bold]{s1}[/bold]", *row_vals)
    console.print(tb)
    console.print("[dim]红 > 0.8 高相关 | 黄 0.5-0.8 中度 | 绿 < 0.5 低相关[/dim]")
    # Stats table
    stats = r.get("stats", {})
    if stats:
        st = _T(title="[dim]个股统计[/dim]", box=_box.MINIMAL)
        st.add_column("标的"); st.add_column("总收益%", justify="right")
        st.add_column("年化波动%", justify="right"); st.add_column("夏普", justify="right")
        st.add_column("最大回撤%", justify="right")
        for sym, sv in stats.items():
            ret = sv.get("return_total")
            rcolor = "green" if (ret or 0) > 0 else "red"
            st.add_row(sym,
                       f"[{rcolor}]{ret:+.1f}[/{rcolor}]" if ret is not None else "—",
                       f"{sv.get('volatility',0):.1f}" if sv.get("volatility") else "—",
                       f"{sv.get('sharpe',0):.2f}" if sv.get("sharpe") else "—",
                       f"[red]{sv.get('max_drawdown',0):.1f}[/red]")
        console.print(st)


def render_portfolio_bt(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','回测失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    from rich.panel import Panel as _P
    pf = r.get("portfolio", {})
    bm = r.get("benchmark", {})
    ret  = pf.get("total_return_pct", 0)
    rcolor = "green" if ret > 0 else "red"
    lines = [
        f"  [bold]总收益率[/bold]    [{rcolor}]{ret:+.2f}%[/{rcolor}]",
        f"  [bold]年化波动率[/bold]  {pf.get('annual_vol_pct',0):.2f}%",
        f"  [bold]夏普比率[/bold]    {pf.get('sharpe_ratio','—')}",
        f"  [bold]最大回撤[/bold]    [red]{pf.get('max_drawdown_pct',0):.2f}%[/red]",
        f"  [bold]卡玛比率[/bold]    {pf.get('calmar_ratio','—')}",
    ]
    if bm:
        br = bm.get("total_return_pct", 0)
        bc = "green" if br > 0 else "red"
        alpha = round(ret - br, 2)
        ac = "green" if alpha > 0 else "red"
        lines += [
            "  [dim]─────────────────────────────────[/dim]",
            f"  [dim]基准 {bm['symbol']}[/dim]    [{bc}]{br:+.2f}%[/{bc}]",
            f"  [bold]超额收益[/bold]    [{ac}]{alpha:+.2f}%[/{ac}]",
        ]
    console.print(_P("\n".join(lines), title="[bold]📈 组合回测结果[/bold]",
                     border_style=rcolor))
    # Allocation table
    alloc = r.get("allocation", [])
    if alloc:
        ta = _T(title=f"[dim]持仓分配 · 回测区间 {r.get('period','N/A')} · 再平衡 {r.get('rebalance','—')}[/dim]",
                box=_box.MINIMAL)
        ta.add_column("标的"); ta.add_column("权重%", justify="right")
        for a in alloc:
            ta.add_row(a["symbol"], f"{a['weight_pct']:.1f}%")
        console.print(ta)


def render_sql_result(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    if not r.get("success"):
        console.print(f"[red]{r.get('error','查询失败')}[/red]"); return
    from rich.table import Table as _T
    from rich import box as _box
    rows = r.get("rows", [])
    cols = r.get("columns", [])
    if not rows:
        console.print("[dim]查询返回 0 行[/dim]"); return
    tb = _T(title=f"[bold]🦆 DuckDB 查询结果[/bold]  [dim]({r.get('row_count',0)} 行)[/dim]",
            box=_box.ROUNDED)
    for c in cols:
        tb.add_column(str(c))
    for row in rows[:100]:
        tb.add_row(*[str(row.get(c, "")) for c in cols])
    console.print(tb)
    if r.get("tables_loaded"):
        console.print(f"[dim]已加载表: {', '.join(r['tables_loaded'])}[/dim]")


def render_alerts(r: dict, *, console=None, has_rich: bool = True) -> None:
    if not has_rich:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    from rich.table import Table as _T
    from rich import box as _box
    active = r.get("active_alerts", [])
    triggered = r.get("triggered_alerts", [])
    cond_lbl = {"gt": "高于", "lt": "低于", "cross_up": "向上突破", "cross_down": "向下跌破"}
    if active:
        ta = _T(title="[bold]🔔 活跃预警[/bold]", box=_box.ROUNDED)
        ta.add_column("标的", style="cyan"); ta.add_column("条件")
        ta.add_column("触发价", justify="right"); ta.add_column("备注", style="dim")
        ta.add_column("ID", style="dim")
        for a in active:
            ta.add_row(a["symbol"],
                       cond_lbl.get(a["condition"], a["condition"]),
                       str(a["price"]), a.get("note",""),
                       a["id"][:16]+"…")
        console.print(ta)
    if triggered:
        tt = _T(title="[dim]已触发预警[/dim]", box=_box.MINIMAL)
        tt.add_column("标的"); tt.add_column("触发价"); tt.add_column("触发时间", style="dim")
        for a in triggered:
            tt.add_row(a["symbol"], str(a.get("triggered_price","")),
                       str(a.get("triggered_at",""))[:16])
        console.print(tt)
    if not active and not triggered:
        console.print("[dim]暂无预警记录。使用 /alert add AAPL gt 200 设置预警[/dim]")


def _prompt_float(label: str, default: float) -> float:
    """交互式数字输入，失败时返回 default。"""


# Moved from aria_cli.py
def format_backtest_output(data: dict):
    """Format backtest results as clean rows."""
    if not HAS_RICH:
        return json.dumps(data, indent=2, ensure_ascii=False)

    d = data.get("data", data.get("backtest", data))
    total_ret = d.get("total_return", 0)
    ann_ret = d.get("annualized_return", 0)
    sharpe = d.get("sharpe_ratio", 0)
    max_dd = d.get("max_drawdown", 0)
    win_rate = d.get("win_rate", 0)
    trades = d.get("num_trades", 0)
    bh_ret = d.get("buy_hold_return", 0)
    outperf = d.get("outperformance", 0)

    def _c(v): return "green" if v >= 0 else "red"

    out = Text()
    out.append("  Backtest Results\n", style="bold")
    out.append(f"  {'Total Return':<18s}", style="dim")
    out.append(f"{total_ret*100:+.2f}%", style=_c(total_ret))
    out.append("  vs B&H ", style="dim")
    out.append(f"{bh_ret*100:+.2f}%\n", style=_c(bh_ret))
    out.append(f"  {'Annualized':<18s}", style="dim")
    out.append(f"{ann_ret*100:+.2f}%\n")
    out.append(f"  {'Sharpe Ratio':<18s}", style="dim")
    out.append(f"{sharpe:.2f}\n")
    out.append(f"  {'Max Drawdown':<18s}", style="dim")
    out.append(f"{max_dd*100:.2f}%\n", style="red")
    out.append(f"  {'Win Rate':<18s}", style="dim")
    out.append(f"{win_rate*100:.1f}%\n")
    out.append(f"  {'Trades':<18s}", style="dim")
    out.append(f"{trades}\n")
    out.append(f"  {'Outperformance':<18s}", style="dim")
    out.append(f"{outperf*100:+.2f}%\n", style=_c(outperf))
    return out
