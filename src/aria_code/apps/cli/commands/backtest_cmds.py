"""BacktestCommandsMixin — backtest/strategy/scaffold commands.

Extracted from aria_cli.py. Module globals imported lazily inside method bodies.
"""

from __future__ import annotations

from ._ui import has_rich, print_error


def format_backtest_data_error(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    local_error: str = "",
    bars: int = 0,
) -> str:
    """Return a user-facing backtest failure message."""
    if bars and bars < 5:
        return (
            f"{symbol} 历史数据仅 {bars} 个交易日，不足以回测。"
            "请换历史更长的标的或缩短策略周期。"
        )
    if local_error:
        low = local_error.lower()
        if "histor" in low or "data" in low or "empty" in low:
            return (
                f"{symbol} 回测失败：{local_error}。"
                "请检查数据源是否可用、ticker 是否正确，或先运行 /doctor /health。"
            )
    return (
        f"{symbol} 在 {start_date} → {end_date} 范围内没有可用历史数据。"
        "请检查代码是否正确、标的是否已上市/未停牌，或缩短回测区间。"
    )


def _bt_num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _bt_pct(value, digits: int = 1, signed: bool = False) -> str:
    number = _bt_num(value)
    sign = "+" if signed and number >= 0 else ""
    return f"{sign}{number * 100:.{digits}f}%"


def _bt_money(value, currency: str = "USD") -> str:
    number = _bt_num(value)
    return f"{currency} {number:,.0f}" if abs(number) >= 1000 else f"{currency} {number:,.2f}"


def _bt_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bt_trade_count(data: dict) -> int:
    for key in ("total_trades", "num_trades", "n_trades", "trades"):
        if key in data and data.get(key) is not None:
            return _bt_int(data.get(key))
    return 0


def _bt_value(data: dict, *keys, default=None):
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    return default


def _bt_volume_summary(data: dict) -> dict:
    summary = data.get("volume_summary")
    return summary if isinstance(summary, dict) else {}


def _bt_result_summary(data: dict) -> str:
    total = _bt_num(data.get("total_return"))
    benchmark = _bt_num(_bt_value(data, "buy_hold_return", "benchmark_return", default=0))
    sharpe = _bt_num(data.get("sharpe_ratio"))
    max_dd = _bt_num(data.get("max_drawdown"))
    relation = "高于" if total > benchmark else "低于" if total < benchmark else "持平"
    return (
        f"结论：策略收益 {_bt_pct(total)}，{relation}买入持有 {_bt_pct(benchmark)}；"
        f"Sharpe {sharpe:.2f}，最大回撤 {_bt_pct(max_dd)}。"
    )


import logging
import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

def format_sparkline(*args, **kwargs):
    from aria_code.apps.cli.helpers import format_sparkline as fn
    return fn(*args, **kwargs)
def _get_broker_registry(*args, **kwargs):
    from .._optional import get_registry as fn
    return fn(*args, **kwargs)
def _get__HAS_BROKERS():
    from .._optional import HAS_BROKERS as val
    return val
def _tool_run_command(*args, **kwargs):
    from aria_cli import _tool_run_command as fn
    return fn(*args, **kwargs)
def _get__HAS_VAULT():
    from aria_cli import _HAS_VAULT as val
    return val
def _get__HAS_MDC():
    from .._optional import HAS_MDC as val
    return val
import pathlib
def _get_vault(*args, **kwargs):
    from aria_cli import _get_vault as fn
    return fn(*args, **kwargs)
def _tool_write_file(*args, **kwargs):
    from aria_cli import _tool_write_file as fn
    return fn(*args, **kwargs)
def _ai_review(*args, **kwargs):
    from aria_cli import _ai_review as fn
    return fn(*args, **kwargs)
def _get_mdc(*args, **kwargs):
    from .._optional import get_mdc as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

# A real logger. This was a `def logger(*args, **kwargs)` forwarding to
# aria_cli, so every `logger.debug(...)` raised AttributeError — and all but
# one of them sit in an except block, where the failure replaced whatever
# error was being reported.
logger = logging.getLogger(__name__)


class BacktestCommandsMixin:
    """Mixin providing backtest, strategy, factor-lab, and scaffold commands."""

    _bt_num = staticmethod(_bt_num)
    _bt_pct = staticmethod(_bt_pct)
    _bt_money = staticmethod(_bt_money)
    _bt_int = staticmethod(_bt_int)
    _bt_trade_count = staticmethod(_bt_trade_count)
    _bt_value = staticmethod(_bt_value)
    _bt_volume_summary = staticmethod(_bt_volume_summary)
    _bt_result_summary = staticmethod(_bt_result_summary)

    async def cmd_backtest(self, args: str):
        """Direct REST backtest → /api/v1/backtest (falls back to Aria tool).

        Usage:
          /backtest [strategy] [symbol] [start_date] [end_date]
          /backtest momentum AAPL 2023-01-01 2024-12-31
          /backtest momentum AAPL --period 1y
          /backtest momentum AAPL --period 6m
        """

        import re as _re_bt
        today = __import__("datetime").date.today()

        raw_parts = args.split() if args else ["momentum", "SPY"]

        # Handle flags (e.g. --period 1y, --fast 20, --slow 60, --symbol AAPL)
        _period_match = None
        _symbol_flag = None
        _fast_period = 20
        _slow_period = 60
        _momentum_period = 20
        _initial_capital = 100000
        _output_dir = None
        _cleaned = []
        i = 0
        while i < len(raw_parts):
            if raw_parts[i] == "--period" and i + 1 < len(raw_parts):
                _period_match = raw_parts[i + 1]
                i += 2
            elif raw_parts[i].startswith("--period="):
                _period_match = raw_parts[i].split("=", 1)[1]
                i += 1
            elif raw_parts[i] == "--symbol" and i + 1 < len(raw_parts):
                _symbol_flag = raw_parts[i + 1].upper()
                i += 2
            elif raw_parts[i].startswith("--symbol="):
                _symbol_flag = raw_parts[i].split("=", 1)[1].upper()
                i += 1
            elif raw_parts[i] == "--fast" and i + 1 < len(raw_parts):
                try:
                    _fast_period = int(raw_parts[i + 1])
                except Exception:
                    pass
                i += 2
            elif raw_parts[i].startswith("--fast="):
                try:
                    _fast_period = int(raw_parts[i].split("=", 1)[1])
                except Exception:
                    pass
                i += 1
            elif raw_parts[i] == "--slow" and i + 1 < len(raw_parts):
                try:
                    _slow_period = int(raw_parts[i + 1])
                except Exception:
                    pass
                i += 2
            elif raw_parts[i].startswith("--slow="):
                try:
                    _slow_period = int(raw_parts[i].split("=", 1)[1])
                except Exception:
                    pass
                i += 1
            elif raw_parts[i] == "--momentum" and i + 1 < len(raw_parts):
                try:
                    _momentum_period = int(raw_parts[i + 1])
                except Exception:
                    pass
                i += 2
            elif raw_parts[i].startswith("--momentum="):
                try:
                    _momentum_period = int(raw_parts[i].split("=", 1)[1])
                except Exception:
                    pass
                i += 1
            elif raw_parts[i] == "--capital" and i + 1 < len(raw_parts):
                try:
                    _initial_capital = float(raw_parts[i + 1])
                except Exception:
                    pass
                i += 2
            elif raw_parts[i].startswith("--capital="):
                try:
                    _initial_capital = float(raw_parts[i].split("=", 1)[1])
                except Exception:
                    pass
                i += 1
            elif raw_parts[i] == "--output" and i + 1 < len(raw_parts):
                _output_dir = raw_parts[i + 1]
                i += 2
            elif raw_parts[i].startswith("--output="):
                _output_dir = raw_parts[i].split("=", 1)[1]
                i += 1
            else:
                _cleaned.append(raw_parts[i])
                i += 1
        parts = _cleaned

        # Resolve --period to a start date
        if _period_match:
            _pm = _period_match.lower()
            _months = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "2y": 24, "3y": 36, "5y": 60}
            if _pm in _months:
                from datetime import timedelta
                _delta_days = _months[_pm] * 30
                _start_dt = today - timedelta(days=_delta_days)
                _resolved_start = _start_dt.isoformat()
            else:
                _resolved_start = None
        else:
            _resolved_start = None

        _known_strategies = {"momentum", "mom", "sma_cross", "ma_cross", "moving_average",
                              "buy_hold", "buyhold", "hold", "ml", "ml_signal", "agent"}
        if len(parts) == 1 and parts[0].lower() not in _known_strategies:
            strategy = "momentum"
            symbol = parts[0].upper()
        else:
            strategy = parts[0] if len(parts) > 0 else "momentum"
            symbol = parts[1].upper() if len(parts) > 1 else "SPY"
        if _symbol_flag:
            symbol = _symbol_flag

        # Positional start/end dates only accepted if they look like YYYY-MM-DD
        #
        # 2026-08-19 修复 UnboundLocalError：这段原本在 ML / Agent 两个分支
        # **之后**（第 240 行），但那两个分支在第 226/233 行就把 start_date /
        # end_date 当关键字参数传了出去。也就是说 `/backtest ml ...` 和
        # `/backtest agent ...` 这两条路径必然抛 UnboundLocalError——只有走到
        # 后面通用分支的调用才碰巧能工作。
        # mypy 的 used-before-def 一次就报了出来（4 处），AST 独立复核确认：
        # start_date 首次赋值在第 240 行，而使用出现在 226、233。
        _date_re = _re_bt.compile(r'^\d{4}-\d{2}-\d{2}$')
        _raw_start = parts[2] if len(parts) > 2 else None
        _raw_end   = parts[3] if len(parts) > 3 else None
        start_date = (_raw_start if _raw_start and _date_re.match(_raw_start) else None) \
                     or _resolved_start or "2023-01-01"
        end_date   = (_raw_end if _raw_end and _date_re.match(_raw_end) else None) \
                     or today.isoformat()

        # ── ML 信号组合回测 ──────────────────────────────────────────────────
        if strategy.lower() in ("ml", "ml_signal"):
            await self._cmd_ml_signal_backtest(parts[1:], start_date=start_date,
                                                end_date=end_date,
                                                capital=_initial_capital)
            return

        # ── Agent 信号回测 ──────────────────────────────────────────────────
        if strategy.lower() == "agent":
            await self._cmd_agent_backtest(symbol, start_date=start_date, end_date=end_date, capital=_initial_capital)
            return

        label = f"Backtesting {strategy} on {symbol} ({start_date}→{end_date})"
        api_url = self.terminal.config.get("api_url", "http://localhost:8000")

        async def _do_backtest():
            from backtest_report import BacktestConfig, generate_backtest_report
            local_config = BacktestConfig(
                symbol=symbol,
                strategy=strategy,
                start_date=start_date,
                end_date=end_date,
                initial_capital=float(_initial_capital),
                fast_period=int(_fast_period),
                slow_period=int(_slow_period),
                momentum_period=int(_momentum_period),
            )
            local_error = ""
            try:
                _out_path = None
                if _output_dir:
                    _out_path = pathlib.Path(_output_dir).expanduser()
                    if not _out_path.is_absolute():
                        from artifacts import user_generated_dir
                        _out_path = user_generated_dir() / _out_path
                local_result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: generate_backtest_report(local_config, output_dir=_out_path)
                )
                if local_result and local_result.get("success"):
                    return {"success": True, "data": local_result, "_source": "local-real-data"}
                local_error = local_result.get("error") if local_result else ""
                logger.debug("local backtest failed, falling back to yfinance direct: %s", local_error or "None")
            except Exception as _e:
                local_error = str(_e)
                logger.debug("local backtest failed, falling back to yfinance direct: %s", _e)

            # ── Direct yfinance backtest — works offline, no backend needed ──
            try:
                import yfinance as _yf
                import numpy as _np
                import statistics as _stats

                _yf_bars = [0]   # records bars found, for a precise error message

                def _run_yf_backtest():
                    _ticker = _yf.Ticker(symbol)
                    _df = _ticker.history(start=start_date, end=end_date, auto_adjust=True)
                    if _df is None or _df.empty:
                        return {"success": False, "error": format_backtest_data_error(
                            symbol,
                            start_date=start_date,
                            end_date=end_date,
                            bars=0,
                        ), "bars": 0}
                    _close = _df["Close"].dropna()
                    _yf_bars[0] = len(_close)
                    if len(_close) < 5:
                        return {"success": False, "error": format_backtest_data_error(
                            symbol,
                            start_date=start_date,
                            end_date=end_date,
                            bars=_yf_bars[0],
                        ), "bars": _yf_bars[0]}
                    _prices = list(_close)
                    n = len(_prices)
                    # Momentum strategy: buy when N-day momentum > 0
                    _mp = int(_momentum_period)
                    _signals = [0] * n
                    for i in range(_mp, n):
                        _signals[i] = 1 if _prices[i] > _prices[i - _mp] else -1
                    # Simulate portfolio
                    _cap = float(_initial_capital)
                    _position = 0.0  # shares
                    _cash = _cap
                    _trades = 0
                    _portfolio = []
                    for i in range(1, n):
                        _p = _prices[i]
                        _sig = _signals[i - 1]
                        if _sig == 1 and _position == 0 and _cash > 0:
                            _shares = _cash / _p
                            _position = _shares
                            _cash = 0
                            _trades += 1
                        elif _sig == -1 and _position > 0:
                            _cash = _position * _p
                            _position = 0
                            _trades += 1
                        _portfolio.append(_cash + _position * _p)
                    if not _portfolio:
                        return None
                    _final = _portfolio[-1]
                    _total_return = (_final - _cap) / _cap
                    _bh_return = (_prices[-1] - _prices[0]) / _prices[0]
                    # Daily returns for Sharpe
                    _rets = [(_portfolio[i] - _portfolio[i-1]) / _portfolio[i-1] for i in range(1, len(_portfolio)) if _portfolio[i-1] > 0]
                    _ann_return = sum(_rets) / len(_rets) * 252 if _rets else 0
                    _ann_vol = _stats.stdev(_rets) * (252 ** 0.5) if len(_rets) > 1 else 0
                    _sharpe = _ann_return / _ann_vol if _ann_vol > 0 else 0
                    # Max drawdown
                    _peak = _portfolio[0]
                    _max_dd = 0.0
                    for v in _portfolio:
                        if v > _peak:
                            _peak = v
                        _dd = (_peak - v) / _peak if _peak > 0 else 0
                        if _dd > _max_dd:
                            _max_dd = _dd
                    # Equity curve (sampled monthly)
                    _step = max(1, n // 24)
                    _equity_curve = [
                        {"date": str(_close.index[min(i + 1, n - 1)].date()), "strategy": round(_portfolio[min(i, len(_portfolio)-1)], 2)}
                        for i in range(0, len(_portfolio), _step)
                    ]
                    _win_trades = sum(1 for i in range(1, len(_portfolio)) if _portfolio[i] > _portfolio[i-1])
                    _vol = _df["Volume"].dropna() if "Volume" in _df else []
                    _vol_count = len(_vol) if hasattr(_vol, "__len__") else 0
                    return {
                        "success": True,
                        "symbol": symbol,
                        "strategy": strategy,
                        "total_return": round(_total_return, 4),
                        "buy_hold_return": round(_bh_return, 4),
                        "annualized_return": round(_ann_return, 4),
                        "sharpe_ratio": round(_sharpe, 3),
                        "max_drawdown": round(-_max_dd, 4),
                        "win_rate": round(_win_trades / max(len(_portfolio) - 1, 1), 3),
                        "num_trades": _trades,
                        "equity_curve": _equity_curve,
                        "data_provider": "yfinance",
                        "provider_chain": ["yfinance"],
                        "start_date": start_date,
                        "end_date": end_date,
                        "initial_capital": float(_initial_capital),
                        "bars": n,
                        "volume_summary": {
                            "last": round(float(_vol.iloc[-1]), 2) if _vol_count else None,
                            "average": round(float(_vol.mean()), 2) if _vol_count else None,
                            "min": round(float(_vol.min()), 2) if _vol_count else None,
                            "max": round(float(_vol.max()), 2) if _vol_count else None,
                            "coverage": round(_vol_count / max(len(_df), 1), 4),
                        },
                    }

                yf_result = await asyncio.get_event_loop().run_in_executor(None, _run_yf_backtest)
                if yf_result and yf_result.get("success"):
                    return {"success": True, "data": yf_result, "_source": "yfinance-local"}
                if yf_result and not yf_result.get("success"):
                    return yf_result
            except Exception as _e:
                logger.debug("yfinance direct backtest failed: %s", _e)

            import aiohttp
            payload = {
                "symbols": [symbol],
                "strategy_type": strategy,
                "start_date": start_date,
                "end_date": end_date,
                "initial_capital": float(_initial_capital),
                "commission_rate": 0.0003,
                "include_monte_carlo": False,
            }
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(f"{api_url}/api/v1/backtest", json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status == 200:
                            body = await resp.json()
                            _rest_data = body.get("data", body)
                            if _rest_data and isinstance(_rest_data, dict):
                                return {"success": True, "data": _rest_data, "_source": "rest"}
            except Exception as _e:
                logger.debug("backtest REST call failed: %s", _e)
            # Honest, actionable error: the dominant cause is too little history
            # (new IPO / halted / wrong ticker), not "all data sources down".
            if 0 < _yf_bars[0] < 5:
                return {"success": False, "error": format_backtest_data_error(
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                    bars=_yf_bars[0],
                )}
            return {"success": False, "error": format_backtest_data_error(
                symbol,
                start_date=start_date,
                end_date=end_date,
                local_error=local_error,
                bars=_yf_bars[0],
            )}

        if self.context.has_rich:
            with self.context.console.status(f"[dim]{label}...[/dim]", spinner="dots"):
                result = await _do_backtest()
        else:
            print(label)
            result = await _do_backtest()

        # Guard: execute_aria_tool / REST fallback can return None
        if not result:
            print_error(self.context, "回测服务不可用 (API未运行)  — 先启动后端: cd apps/api && python -m uvicorn src.main:app", "tool")
            return

        if result.get("success"):
            d = result.get("data", result)
            if not isinstance(d, dict):
                print_error(self.context, f"回测结果格式异常: {type(d)}", "tool")
                return
            src = result.get("_source", "aria")
            if self.context.has_rich:
                from rich.table import Table
                tbl = Table(title=f"[bold]{symbol} · {strategy.upper()}[/bold]", show_header=True, header_style="bold")
                tbl.add_column("Metric", style="#57606a")
                tbl.add_column("Value", justify="right")
                tbl.add_column("vs B&H", justify="right", style="#57606a")
                bh = self._bt_num(self._bt_value(d, "buy_hold_return", "benchmark_return", default=0))
                trades = self._bt_trade_count(d)
                rows = [
                    ("Total Return", self._bt_pct(d.get("total_return")), self._bt_pct(bh)),
                    ("Ann. Return",  self._bt_pct(self._bt_value(d, "annualized_return", "annual_return", default=0)), ""),
                    ("Sharpe Ratio", f"{self._bt_num(d.get('sharpe_ratio')):.2f}", ""),
                    ("Max Drawdown", self._bt_pct(d.get("max_drawdown")), ""),
                    ("Win Rate",     self._bt_pct(d.get("win_rate")), ""),
                    ("# Trades",     str(trades), ""),
                ]
                if d.get("calmar_ratio"):
                    rows.append(("Calmar Ratio", f"{d['calmar_ratio']:.2f}", ""))
                if d.get("sortino_ratio"):
                    rows.append(("Sortino Ratio", f"{d['sortino_ratio']:.2f}", ""))
                for r in rows:
                    tbl.add_row(*r)
                self.context.console.print(tbl)
                self.context.console.print(f"  [bold]{self._bt_result_summary(d)}[/bold]")

                actual_start = self._bt_value(d, "start", "start_date", default=start_date)
                actual_end = self._bt_value(d, "end", "end_date", default=end_date)
                bars = self._bt_int(d.get("bars"))
                initial = self._bt_money(d.get("initial_capital", _initial_capital))
                self.context.console.print(
                    f"  [#57606a]source:[/#57606a] {src}"
                    f"  [#57606a]period:[/#57606a] {actual_start} → {actual_end}"
                    f"  [#57606a]bars:[/#57606a] {bars}"
                    f"  [#57606a]capital:[/#57606a] {initial}"
                )
                self.context.console.print(
                    f"  [#57606a]params:[/#57606a] "
                    f"momentum={_momentum_period} fast={_fast_period} slow={_slow_period}"
                )
                if d.get("provider_chain"):
                    chain = " → ".join(str(x) for x in d.get("provider_chain") or [])
                    status = d.get("data_status") or "complete"
                    missing = ", ".join(str(x) for x in (d.get("missing_fields") or [])) or "none"
                    self.context.console.print(
                        f"  [#57606a]data:[/#57606a] {chain}"
                        f"  [#57606a]status:[/#57606a] {status}"
                        f"  [#57606a]missing:[/#57606a] {missing}"
                    )
                vol = self._bt_volume_summary(d)
                if vol:
                    avg = vol.get("average")
                    last = vol.get("last")
                    coverage = self._bt_num(vol.get("coverage"))
                    self.context.console.print(
                        f"  [#57606a]volume:[/#57606a] "
                        f"avg {avg:,.0f} · last {last:,.0f} · coverage {coverage:.0%}"
                        if avg is not None and last is not None
                        else "  [#57606a]volume:[/#57606a] unavailable"
                    )
                if d.get("report_path"):
                    self.context.console.print(f"  [#57606a]report:[/#57606a] {d['report_path']}")
                if trades == 0:
                    self.context.console.print(
                        "  [yellow]注意:[/yellow] # Trades 为 0，表示本次规则没有触发入场；"
                        "收益可能来自全程空仓/持仓逻辑或上游交易统计口径。"
                    )
            else:
                print(f"Total Return: {d.get('total_return',0)*100:.1f}%  Sharpe: {d.get('sharpe_ratio',0):.2f}  MaxDD: {d.get('max_drawdown',0)*100:.1f}%")
                if d.get("report_path"):
                    print(f"HTML Report: {d['report_path']}")

            eq = d.get("equity_curve", [])
            if eq:
                strat_vals = [p.get("strategy", p.get("portfolio_value", 0)) for p in eq if isinstance(p, dict)]
                if strat_vals:
                    spark = format_sparkline(strat_vals)
                    if spark:
                        self.context.console.print(f"  [#57606a]Equity:[/#57606a] [green]{spark}[/green]" if self.context.has_rich else f"  Equity: {spark}")
            await self._print_backtest_broker_plan(d)
        else:
            print_error(self.context, f"Backtest failed: {result.get('error', 'Unknown')}", "tool")

    async def _print_backtest_broker_plan(self, backtest_result: dict):
        """Print an account-aware order plan for a successful backtest, if a broker is connected."""

        if not _get__HAS_BROKERS() or not isinstance(backtest_result, dict):
            return
        try:
            reg = _get_broker_registry()
            broker = reg.active() if reg else None
            if not broker:
                return
            from brokers import plans_from_strategy_results, snapshot_from_broker
            import asyncio as _aio

            def _build_plan():
                snapshot = snapshot_from_broker(broker)
                plans = plans_from_strategy_results(snapshot, [backtest_result])
                return snapshot, plans[0] if plans else None

            snapshot, plan = await _aio.get_event_loop().run_in_executor(None, _build_plan)
            if not plan:
                return
            data = plan.to_dict()
            order = data.get("estimated_order") or {}
            risk = data.get("risk") or {}
            if self.context.has_rich:
                from rich.table import Table
                t = Table(title=f"Broker Plan — {snapshot.broker_label}", show_header=False, box=None)
                t.add_column("Field", style="dim")
                t.add_column("Value")
                t.add_row("Current Weight", f"{data.get('current_weight', 0) * 100:.2f}%")
                t.add_row("Target Weight", f"{data.get('target_weight', 0) * 100:.2f}%")
                if order:
                    side = "买入" if order.get("side") == "buy" else "卖出"
                    t.add_row("Suggested Order", f"{side} {order.get('quantity', 0):,.0f} {data.get('symbol')} @ {order.get('price', 0):,.2f}")
                    t.add_row("Estimated Value", f"{snapshot.currency} {order.get('estimated_value', 0):,.2f}")
                    t.add_row("Cash After", f"{snapshot.currency} {data.get('cash_after', 0):,.2f}")
                else:
                    t.add_row("Suggested Order", "No trade")
                status = "passed" if risk.get("passed") else "blocked"
                t.add_row("Risk Gate", status)
                self.context.console.print(t)
                for msg in risk.get("violations", []):
                    self.context.console.print(f"  [red]- {msg}[/red]")
                for msg in risk.get("warnings", []):
                    self.context.console.print(f"  [yellow]- {msg}[/yellow]")
                if order and risk.get("passed"):
                    self.context.console.print("  [dim]这是订单计划，不会自动下单。执行前仍需用户明确确认。[/dim]")
            else:
                print(f"Broker Plan: {snapshot.broker_label}")
                if order:
                    print(f"  {order.get('side')} {order.get('quantity')} {data.get('symbol')} @ {order.get('price')}")
                print(f"  Risk: {'passed' if risk.get('passed') else 'blocked'}")
        except Exception as exc:
            logger.debug("backtest broker plan skipped: %s", exc)

    async def cmd_walk_forward(self, args: str):
        """Walk-Forward 滚动回测 → /api/v1/backtest/walk-forward"""

        parts = args.split() if args else ["SPY"]
        symbol = parts[0].upper() if parts else "SPY"
        strategy = parts[1] if len(parts) > 1 else "momentum"
        method = parts[2] if len(parts) > 2 else "rolling"
        api_url = self.terminal.config.get("api_url", "http://localhost:8000")

        label = f"Walk-Forward ({method}) · {strategy} · {symbol}"
        import aiohttp

        async def _do_wf():
            payload = {
                "symbol": symbol, "strategy_type": strategy, "method": method,
                "start_date": "2020-01-01",
                "end_date": __import__("datetime").date.today().isoformat(),
                "train_period_days": 252, "test_period_days": 63, "step_days": 21,
            }
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{api_url}/api/v1/backtest/walk-forward", json=payload, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    body = await resp.json()
                    return body.get("data", body)

        if self.context.has_rich:
            with self.context.console.status(f"[dim]{label}...[/dim]", spinner="dots"):
                try:
                    data = await _do_wf()
                except Exception as e:
                    print_error(self.context, str(e), "tool"); return
        else:
            print(label)
            try:
                data = await _do_wf()
            except Exception as e:
                print_error(self.context, str(e), "tool"); return

        summary = data.get("summary", data)
        folds = data.get("folds", [])
        verdict = summary.get("verdict", "?")
        verdict_color = "green" if verdict == "PASS" else "red"

        if self.context.has_rich:
            from rich.table import Table
            # Summary
            self.context.console.print(f"\n[bold]{symbol} · {strategy} · {method}[/bold]  Verdict: [bold {verdict_color}]{verdict}[/bold {verdict_color}]")
            self.context.console.print(f"  Folds: {summary.get('n_folds')}  "
                          f"Avg OOS Sharpe: [bold]{summary.get('avg_oos_sharpe', 0):.3f}[/bold]  "
                          f"Consistency: {summary.get('consistency_ratio_pct', 0):.0f}%  "
                          f"Robustness: {summary.get('robustness_score', 0):.3f}  "
                          f"p-value: {summary.get('p_value', 1):.4f}")
            # Fold table
            if folds:
                tbl = Table(title="Fold Results", show_header=True, header_style="bold dim")
                for col in ["Fold", "Test Period", "OOS Return", "OOS Sharpe", "OOS MaxDD", "Win%"]:
                    tbl.add_column(col, justify="right")
                for f in folds[:12]:
                    ret = f.get("test_return_pct", 0)
                    tbl.add_row(
                        str(f.get("fold_id", "")),
                        f.get("test_period", ""),
                        f"{'+'if ret>=0 else ''}{ret:.1f}%",
                        f"{f.get('test_sharpe', 0):.3f}",
                        f"{f.get('test_max_drawdown_pct', 0):.1f}%",
                        f"{f.get('test_win_rate_pct', 0):.0f}%",
                    )
                self.context.console.print(tbl)
        else:
            print(f"Verdict: {verdict}  Folds: {summary.get('n_folds')}  Avg OOS Sharpe: {summary.get('avg_oos_sharpe',0):.3f}")

    async def cmd_auto_strategy(self, args: str):
        """AI strategy auto-optimization loop (unique to Aria).

        Generates a strategy, runs backtest, reads results, iterates until
        the target metric is reached or max rounds exhausted.

        Usage:
            /auto-strategy momentum SPY
            /auto-strategy momentum SPY --target sharpe=1.5
            /auto-strategy meanrev AAPL --target sharpe=1.2 --rounds 3
        """

        import re as _re
        import time as _time

        parts = args.split()
        strategy_type = parts[0].lower() if parts else "momentum"
        symbol = parts[1].upper() if len(parts) > 1 else "SPY"
        target_sharpe = 1.0
        max_rounds = 3
        for p in parts[2:]:
            m = _re.match(r"--target\s*sharpe=([0-9.]+)", p)
            if m:
                target_sharpe = float(m.group(1))
            m = _re.match(r"--rounds=?([0-9]+)", p)
            if m:
                max_rounds = int(m.group(1))

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold cyan]🔄 策略自动优化[/bold cyan]  [dim]{strategy_type} / {symbol}  目标 Sharpe≥{target_sharpe}  最多{max_rounds}轮[/dim]")
            self.context.console.print()

        best_sharpe = 0.0
        best_version = None

        for round_num in range(1, max_rounds + 1):
            self.context.console.print(f"  [bold]第 {round_num}/{max_rounds} 轮[/bold]") if self.context.has_rich else print(f"  Round {round_num}/{max_rounds}")

            # ── Step 1: Generate strategy code ──────────────────────────────
            feedback_ctx = ""
            if round_num > 1 and best_version:
                feedback_ctx = (
                    f"\n\nPrevious backtest Sharpe={best_sharpe:.2f} (target={target_sharpe})."
                    " Modify the strategy to improve Sharpe: adjust lookback period, "
                    "add momentum filter, tighten stop-loss, or change position sizing."
                )

            gen_prompt = (
                f"Generate a complete, self-contained Python backtest strategy script.\n"
                f"Strategy type: {strategy_type}\n"
                f"Symbol: {symbol}\n"
                f"Requirements:\n"
                f"1. Use yfinance to download 2 years of daily OHLCV data\n"
                f"2. Implement the {strategy_type} strategy with clear entry/exit signals\n"
                f"3. Simulate trades: track portfolio value, returns, Sharpe ratio\n"
                f"4. Print EXACTLY this at the end (machine-parseable):\n"
                f"   BACKTEST_RESULT: sharpe=X.XX annual_return=X.XX% max_drawdown=X.XX% trades=N\n"
                f"5. All code in one file, no external dependencies except yfinance/pandas/numpy\n"
                f"{feedback_ctx}\n"
                f"Output ONLY the Python code in ```python``` fences."
            )

            _fname = f"auto_strat_{strategy_type}_{symbol}_r{round_num}_{int(_time.time())}.py"
            from artifacts import user_generated_dir as _user_generated_dir
            _fpath = _user_generated_dir() / _fname

            self.context.console.print("  [dim]生成策略代码...[/dim]") if self.context.has_rich else print("  Generating strategy...")
            await self.terminal.send_message(gen_prompt)

            # Extract code from last response
            last_ai = next(
                (m["content"] for m in reversed(self.terminal.conversation)
                 if m.get("role") == "assistant"), ""
            )
            import re as _re2
            py_blocks = _re2.findall(r"```python\n(.*?)```", last_ai, _re2.DOTALL)
            if not py_blocks:
                # fallback: grab after fence
                m = _re2.search(r"```python\n(.*)", last_ai, _re2.DOTALL)
                if m:
                    py_blocks = [m.group(1)]

            if not py_blocks:
                self.context.console.print("  [yellow]⚠ 未生成代码，跳过本轮[/yellow]") if self.context.has_rich else print("  No code generated, skipping")
                continue

            code = py_blocks[-1].strip()
            _tool_write_file({"path": str(_fpath), "content": code, "_skip_confirm": True})
            self.context.console.print(f"  [dim]策略已保存: {_fpath.name}[/dim]") if self.context.has_rich else print(f"  Saved: {_fpath.name}")

            # ── Step 2: Run backtest ─────────────────────────────────────────
            self.context.console.print("  [dim]运行回测...[/dim]") if self.context.has_rich else print("  Running backtest...")
            bt_result = _tool_run_command({
                "command": f"python3 {_fpath}",
                "timeout": 120,
            })
            stdout = bt_result.get("data", {}).get("stdout", "") or ""
            stderr = bt_result.get("data", {}).get("stderr", "") or ""

            # ── Step 3: Parse backtest metrics ──────────────────────────────
            sharpe = 0.0
            ann_return = 0.0
            max_dd = 0.0
            n_trades = 0
            m = _re2.search(r"BACKTEST_RESULT:.*?sharpe=([0-9.-]+)", stdout)
            if m:
                sharpe = float(m.group(1))
            m = _re2.search(r"annual_return=([0-9.-]+)%", stdout)
            if m:
                ann_return = float(m.group(1))
            m = _re2.search(r"max_drawdown=([0-9.-]+)%", stdout)
            if m:
                max_dd = float(m.group(1))
            m = _re2.search(r"trades=([0-9]+)", stdout)
            if m:
                n_trades = int(m.group(1))

            # Update best
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_version = _fpath

            # Display round result
            sharpe_color = "green" if sharpe >= target_sharpe else ("yellow" if sharpe > 0 else "red")
            if self.context.has_rich:
                self.context.console.print(
                    f"  [dim]回测结果:[/dim]  "
                    f"Sharpe=[{sharpe_color}]{sharpe:.2f}[/{sharpe_color}]  "
                    f"年化={ann_return:.1f}%  "
                    f"最大回撤={max_dd:.1f}%  "
                    f"交易次数={n_trades}"
                )
            else:
                print(f"  Backtest: Sharpe={sharpe:.2f}  Return={ann_return:.1f}%  MaxDD={max_dd:.1f}%  Trades={n_trades}")

            if stderr and "Error" in stderr:
                self.context.console.print(f"  [red]执行错误: {stderr[:200]}[/red]") if self.context.has_rich else print(f"  Error: {stderr[:200]}")

            # ── Step 4: Check convergence ────────────────────────────────────
            if sharpe >= target_sharpe:
                self.context.console.print(f"\n  [green]✅ 目标达成！Sharpe={sharpe:.2f} ≥ {target_sharpe}[/green]") if self.context.has_rich else print(f"\n  ✓ Target reached: Sharpe={sharpe:.2f}")
                break
            elif round_num < max_rounds:
                self.context.console.print(f"  [dim]Sharpe={sharpe:.2f} < 目标{target_sharpe}，继续优化...[/dim]\n") if self.context.has_rich else print(f"  Sharpe={sharpe:.2f} < {target_sharpe}, optimizing...\n")

        # ── Summary ──────────────────────────────────────────────────────────
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold]优化完成[/bold]  最佳 Sharpe=[{'green' if best_sharpe >= target_sharpe else 'yellow'}]{best_sharpe:.2f}[/{'green' if best_sharpe >= target_sharpe else 'yellow'}]")
            if best_version:
                self.context.console.print(f"  最优策略文件: [dim]{best_version}[/dim]")
                self.context.console.print(f"  [dim]运行: python3 {best_version}[/dim]")
            self.context.console.print()
        else:
            print(f"\n  Best Sharpe={best_sharpe:.2f}  File: {best_version}")

    async def cmd_factor_lab(self, args: str):
        """Factor analysis workstation — compute IC, ICIR, factor returns (Aria exclusive).

        Usage:
            /factor-lab AAPL
            /factor-lab QQQ --days 252
            /factor-lab SPY --factors momentum,value,quality
        """

        import re as _re

        parts = args.split()
        symbol = parts[0].upper() if parts else "SPY"
        days = 252
        for p in parts[1:]:
            m = _re.match(r"--days=?(\d+)", p)
            if m:
                days = int(m.group(1))

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold cyan]🔬 因子分析工作台[/bold cyan]  [dim]{symbol}  {days}天数据[/dim]")
            self.context.console.print()

        if not _get__HAS_MDC():
            self.context.console.print("[red]需要 market_data_client 模块[/red]") if self.context.has_rich else print("market_data_client not available")
            return

        try:
            import numpy as np
            import pandas as pd

            mdc = _get_mdc()

            # ── Fetch data ────────────────────────────────────────────────────
            self.context.console.print("  [dim]拉取行情数据...[/dim]") if self.context.has_rich else print("  Fetching data...")
            hist = mdc.history(symbol, days=days)
            if not hist.get("success") or not hist.get("data"):
                self.context.console.print(f"[red]无法获取 {symbol} 历史数据[/red]") if self.context.has_rich else print(f"No data for {symbol}")
                return

            df = pd.DataFrame(hist["data"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["volume"] = pd.to_numeric(df.get("volume", pd.Series()), errors="coerce")
            df = df.dropna(subset=["close"])
            close = df["close"]
            returns = close.pct_change().dropna()

            # ── Compute factors ───────────────────────────────────────────────
            factors: dict = {}

            # 1. Momentum (1M, 3M, 6M, 12M)
            for months, label in [(21, "Mom1M"), (63, "Mom3M"), (126, "Mom6M"), (252, "Mom12M")]:
                if len(close) > months:
                    factors[label] = close.pct_change(months)

            # 2. Mean Reversion (short-term)
            if len(close) > 5:
                factors["MeanRev5D"] = -close.pct_change(5)

            # 3. Volatility (annualized)
            if len(returns) > 20:
                factors["Vol20D"] = returns.rolling(20).std() * np.sqrt(252)

            # 4. Volume trend
            if "volume" in df.columns and df["volume"].notna().sum() > 20:
                vol_series = df["volume"].astype(float)
                factors["VolTrend"] = vol_series.pct_change(20)

            # 5. RSI factor
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, np.nan)
            factors["RSI14"] = 100 - 100 / (1 + rs)

            # ── Compute IC (Information Coefficient) for each factor ──────────
            # IC = correlation between factor value at t and next-period return
            fwd_returns = returns.shift(-1)  # 1-day forward return

            ic_results = {}
            for fname, fseries in factors.items():
                try:
                    aligned = pd.concat([fseries, fwd_returns], axis=1).dropna()
                    aligned.columns = ["factor", "fwd"]
                    if len(aligned) < 20:
                        continue
                    ic = aligned["factor"].corr(aligned["fwd"])
                    if np.isnan(ic):
                        continue
                    # Rolling IC (window=20) — compute manually to avoid rolling.apply issues
                    roll_ics = []
                    for start in range(0, len(aligned) - 20, 5):
                        chunk = aligned.iloc[start:start + 20]
                        chunk_ic = chunk["factor"].corr(chunk["fwd"])
                        if not np.isnan(chunk_ic):
                            roll_ics.append(chunk_ic)
                    icir = ic / (np.std(roll_ics) + 1e-9) if len(roll_ics) >= 3 else 0.0
                    ic_results[fname] = {"ic": ic, "icir": float(icir), "abs_ic": abs(ic)}
                except Exception:
                    continue

            # ── Current factor values (latest bar) ───────────────────────────
            latest = {fname: float(fseries.dropna().iloc[-1]) if not fseries.dropna().empty else None
                      for fname, fseries in factors.items()}

            # ── Display results ───────────────────────────────────────────────
            if self.context.has_rich:
                self.context.console.print(f"  [bold]{symbol}[/bold]  [dim]当前价: {close.iloc[-1]:.2f}  数据: {len(df)}天[/dim]")
                self.context.console.print()
                self.context.console.print("  [bold]因子分析[/bold]")
                self.context.console.print()
                self.context.console.print(f"  [dim]{'因子':<14s}{'IC':>8s}{'|IC|':>8s}{'ICIR':>8s}{'当前值':>12s}  信号[/dim]")
                self.context.console.print("  " + "─" * 60)
                for fname, metrics in sorted(ic_results.items(), key=lambda x: -abs(x[1]["ic"])):
                    ic   = metrics["ic"]
                    icir = metrics["icir"]
                    curr = latest.get(fname)
                    curr_str = f"{curr:.3f}" if curr is not None else "N/A"
                    signal = ""
                    if abs(ic) > 0.03:
                        signal = "↑ 看多" if ic > 0 else "↓ 看空"
                    ic_color = "green" if ic > 0.03 else ("red" if ic < -0.03 else "dim")
                    self.context.console.print(
                        f"  [{ic_color}]{fname:<14s}[/{ic_color}]"
                        f"[{ic_color}]{ic:>8.3f}[/{ic_color}]"
                        f"{abs(ic):>8.3f}"
                        f"{icir:>8.2f}"
                        f"{curr_str:>12s}"
                        f"  [dim]{signal}[/dim]"
                    )
                self.context.console.print()
                # AI interpretation
                top_factors = sorted(ic_results.items(), key=lambda x: -abs(x[1]["ic"]))[:3]
                if top_factors:
                    self.context.console.print("  [bold]AI 解读[/bold]")
                    fac_summary = ", ".join(f"{f}(IC={m['ic']:.3f})" for f, m in top_factors)
                    self.context.console.print(f"  [dim]最有效因子: {fac_summary}[/dim]")
                    self.context.console.print(f"  [dim]使用 /deep-analysis {symbol} 获取完整 AI 投研分析[/dim]")
                    self.context.console.print()
            else:
                print(f"  {symbol} Factor Analysis ({len(df)} days)")
                print(f"  {'Factor':<14} {'IC':>8} {'|IC|':>8} {'ICIR':>8} {'Current':>12}")
                for fname, metrics in sorted(ic_results.items(), key=lambda x: -abs(x[1]["ic"])):
                    curr = latest.get(fname)
                    curr_str = f"{curr:.3f}" if curr is not None else "N/A"
                    print(f"  {fname:<14} {metrics['ic']:>8.3f} {abs(metrics['ic']):>8.3f} {metrics['icir']:>8.2f} {curr_str:>12}")

        except ImportError as e:
            self.context.console.print(f"[red]需要 numpy/pandas: {e}[/red]") if self.context.has_rich else print(f"Missing: {e}")
        except Exception as e:
            self.context.console.print(f"[red]因子分析失败: {e}[/red]") if self.context.has_rich else print(f"Error: {e}")

    def _scaffold_with_llm(self, project_name: str, description: str, base_dir) -> None:
        """Call the configured LLM to generate a custom project structure and write files."""

        import json
        import urllib.request
        import textwrap
        import pathlib

        ollama_url = self.terminal.config.get("ollama_url", "http://localhost:11434")
        model      = self.terminal.config.get("model", "qwen2.5:7b")

        _SCAFFOLD_SYS = (
            "You are a project scaffolding assistant. Output ONLY valid JSON — no markdown, no explanation.\n"
            "Schema:\n"
            '{"description": "one-line summary", "entry": "main.py", '
            '"files": {"relative/path.py": "file content", ...}}\n'
            "CRITICAL JSON rules:\n"
            r'- Inside string values use \n for newlines (backslash-n), NEVER literal newlines.'
            "\n"
            r'- Inside string values use \" for double quotes, \\ for backslashes.'
            "\n"
            "- 3–8 files total. Content must be complete and runnable.\n"
            "- Always include: main entry point, requirements.txt, README.md\n"
            "- requirements.txt: one package per line. README.md: install + usage.\n"
            "- No markdown code fences. Raw JSON only."
        )
        _SCAFFOLD_USER = (
            f"Project name: {project_name}\n"
            f"Description:  {description}\n"
            "Generate the complete file structure."
        )

        if self.context.has_rich:
            self.context.console.print(f"\n  [#C08050]⏺[/#C08050]  [bold]LLM 生成项目结构[/bold]  [dim]{description}[/dim]")
        else:
            print(f"\n⏺ 生成项目结构: {description}")

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SCAFFOLD_SYS},
                {"role": "user",   "content": _SCAFFOLD_USER},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 4096},
        }
        try:
            req = urllib.request.Request(
                ollama_url.rstrip("/") + "/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            raw = data.get("message", {}).get("content", "").strip()
        except Exception as e:
            msg = f"LLM 调用失败: {e}"
            self.context.console.print(f"  [red]{msg}[/red]") if self.context.has_rich else print(f"  {msg}")
            return

        # Strip accidental markdown fences
        import re as _re
        raw = _re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()

        def _parse_scaffold_json(text: str):
            """Try several strategies to extract valid JSON from LLM output."""
            # Strategy 1: strict parse
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            # Strategy 2: replace literal newlines inside string values
            try:
                # Escape literal newlines that appear inside JSON string values
                fixed = _re.sub(
                    r'("(?:[^"\\]|\\.)*")',
                    lambda m: m.group().replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t'),
                    text,
                )
                return json.loads(fixed)
            except Exception:
                pass
            # Strategy 3: find outermost {...} block
            m = _re.search(r'\{.*\}', text, _re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    # Strategy 4: same but with newline escaping
                    try:
                        blob = m.group()
                        fixed = _re.sub(
                            r'("(?:[^"\\]|\\.)*")',
                            lambda mx: mx.group().replace('\n', '\\n').replace('\r', '\\r'),
                            blob,
                        )
                        return json.loads(fixed)
                    except Exception:
                        pass
            return None

        structure = _parse_scaffold_json(raw)
        if not structure or "files" not in structure:
            msg = "LLM 未返回有效 JSON 结构，请重试或使用 --template"
            self.context.console.print(f"  [red]{msg}[/red]") if self.context.has_rich else print(f"  {msg}")
            return

        files: dict = structure["files"]
        proj_desc   = structure.get("description", description)
        entry       = structure.get("entry", "main.py")

        # ── Preview ───────────────────────────────────────────────────────────
        if self.context.has_rich:
            self.context.console.print(f"  [green]✓[/green]  [dim]{proj_desc}[/dim]")
            self.context.console.print(f"\n  [dim]{base_dir.name}/[/dim]")
            for fname, fcontent in files.items():
                lines = fcontent.count("\n") + 1 if fcontent else 0
                self.context.console.print(f"  [dim]  ├── {fname:<26s}[/dim] {lines} lines")
            self.context.console.print()
            choice = self.context.console.input(
                "  [bold]Create these files?[/bold] [dim]\\[y=all / n=cancel / r=review each][/dim] "
            ).strip().lower()
        else:
            print(f"\n  {base_dir.name}/")
            for fname, fcontent in files.items():
                lines = fcontent.count("\n") + 1 if fcontent else 0
                print(f"    ├── {fname:<26s}  {lines} lines")
            choice = input("  Create these files? [y/all / n=cancel / r=review each] ").strip().lower()

        if choice in ("n", "no"):
            self.context.console.print("[dim]取消。[/dim]") if self.context.has_rich else print("Cancelled.")
            return

        approve_each = choice in ("r", "review")
        created, skipped = [], []

        for fname, fcontent in files.items():
            target = pathlib.Path(base_dir) / fname
            target.parent.mkdir(parents=True, exist_ok=True)

            if approve_each:
                if self.context.has_rich:
                    self.context.console.print(f"\n  [dim]{fname}[/dim]  ({fcontent.count(chr(10))+1} lines)")
                    sub = self.context.console.input("  [dim]写入? [y/n] [/dim]").strip().lower()
                else:
                    print(f"\n  {fname}  ({fcontent.count(chr(10))+1} lines)")
                    sub = input("  写入? [y/n] ").strip().lower()
                if sub not in ("y", "yes", ""):
                    skipped.append(fname)
                    continue

            result = _tool_write_file({"path": str(target), "content": fcontent, "_skip_confirm": True})
            if result["success"]:
                created.append(fname)
            else:
                err = result.get("error", "?")
                self.context.console.print(f"  [red]Failed {fname}: {err}[/red]") if self.context.has_rich else print(f"  Failed {fname}: {err}")

        if self.context.has_rich:
            self.context.console.print()
            if created:
                self.context.console.print(f"  [green]✓[/green] 创建 {len(created)} 个文件 → [bold]{base_dir}[/bold]")
                for f in created:
                    self.context.console.print(f"    [dim]{f}[/dim]")
            if skipped:
                self.context.console.print(f"  [dim]跳过: {', '.join(skipped)}[/dim]")
            self.context.console.print(f"\n  [dim]启动: cd \"{base_dir}\" && python3 {entry}[/dim]\n")
        else:
            print(f"\n创建 {len(created)} 个文件 → {base_dir}")
            if skipped:
                print(f"跳过: {', '.join(skipped)}")
            print(f"启动: cd \"{base_dir}\" && python3 {entry}")

    def cmd_scaffold(self, args: str):
        """Generate a project folder structure with files, with user approval.

        Usage:
          /scaffold <project_name>                         → blank template
          /scaffold <project_name> <description...>        → LLM generates custom structure
          /scaffold <project_name> --template analysis     → fixed finance template
          /scaffold <project_name> --template strategy
          /scaffold <project_name> --template pipeline

        Examples:
          /scaffold my-api FastAPI REST API with JWT auth and PostgreSQL
          /scaffold price-alert CLI tool that monitors stock prices and sends alerts
          /scaffold aapl-analysis --template analysis
        """

        import textwrap

        parts = args.strip().split()
        if not parts:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /scaffold <name> [description] | [--template analysis|strategy|pipeline|blank][/dim]")
                self.context.console.print("[dim]Examples:[/dim]")
                self.context.console.print("[dim]  /scaffold my-api  FastAPI REST API with JWT auth[/dim]")
                self.context.console.print("[dim]  /scaffold price-bot  CLI tool that monitors stock prices[/dim]")
                self.context.console.print("[dim]  /scaffold aapl-analysis --template analysis[/dim]")
            else:
                print("Usage: /scaffold <name> [description] | [--template analysis|strategy|pipeline|blank]")
            return

        # Parse project name, template flag, and optional description
        project_name = parts[0]
        template = None
        description = ""
        if "--template" in parts:
            idx = parts.index("--template")
            if idx + 1 < len(parts):
                template = parts[idx + 1]
            # remaining words before --template are ignored
        elif len(parts) > 1:
            description = " ".join(parts[1:])  # everything after name = LLM description

        # Resolve base directory under the user's local Aria Code workspace.
        # Generated strategy/code projects must not silently land in the source repo.
        from artifacts import user_projects_dir as _user_projects_dir
        base_dir = _user_projects_dir() / project_name

        # ── LLM-generated scaffold (when user gives a description) ────────────
        if description and not template:
            self._scaffold_with_llm(project_name, description, base_dir)
            return

        # Fallback to blank when no template and no description
        if template is None:
            template = "blank"

        # Built-in templates
        TEMPLATES = {
            "analysis": {
                "description": "Stock/asset analysis project",
                "files": {
                    "main.py": textwrap.dedent("""\
                        #!/usr/bin/env python3
                        \"\"\"
                        {project} — market analysis entry point.
                        Usage: python3 main.py AAPL
                        \"\"\"
                        import sys
                        import os
                        import numpy as np
                        import pandas as pd
                        import yfinance as yf
                        import matplotlib; matplotlib.use('Agg')
                        import matplotlib.pyplot as plt
                        from analysis import run_analysis
                        from report import generate_report

                        if __name__ == "__main__":
                            symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
                            data = run_analysis(symbol)
                            generate_report(symbol, data)
                        """),
                    "analysis.py": textwrap.dedent("""\
                        \"\"\"Core analysis logic for {project}.\"\"\"
                        import numpy as np
                        import pandas as pd
                        import yfinance as yf


                        def run_analysis(symbol: str, period: str = "1y") -> dict:
                            ticker = yf.Ticker(symbol)
                            hist = ticker.history(period=period, auto_adjust=True, progress=False)
                            if hist.empty:
                                raise ValueError(f"No data for {{symbol}}")
                            hist.columns = hist.columns.droplevel(1) if hasattr(hist.columns, 'droplevel') and hist.columns.nlevels > 1 else hist.columns
                            close = hist["Close"]
                            returns = close.pct_change().dropna()
                            sma20 = close.rolling(20).mean()
                            sma50 = close.rolling(50).mean()
                            rsi = _calc_rsi(close)
                            return {{
                                "symbol": symbol,
                                "current_price": round(float(close.iloc[-1]), 2),
                                "sma20": round(float(sma20.iloc[-1]), 2),
                                "sma50": round(float(sma50.iloc[-1]), 2),
                                "rsi": round(float(rsi.iloc[-1]), 1),
                                "annual_return": round(float(returns.mean() * 252), 4),
                                "volatility": round(float(returns.std() * (252 ** 0.5)), 4),
                                "hist": hist,
                                "returns": returns,
                            }}


                        def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
                            delta = close.diff()
                            gain = delta.clip(lower=0).rolling(period).mean()
                            loss = (-delta.clip(upper=0)).rolling(period).mean()
                            rs = gain / loss.replace(0, float("nan"))
                            return 100 - 100 / (1 + rs)
                        """),
                    "report.py": textwrap.dedent("""\
                        \"\"\"Report generation for {project}.\"\"\"
                        import os
                        import matplotlib; matplotlib.use('Agg')
                        import matplotlib.pyplot as plt


                        def generate_report(symbol: str, data: dict):
                            fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
                            hist = data["hist"]
                            close = hist["Close"]
                            # Price + SMAs
                            axes[0].plot(close.index, close, label="Close", color="#C08050", linewidth=1.5)
                            axes[0].plot(close.index, hist["Close"].rolling(20).mean(), label="SMA20", color="#2AE8A5", linewidth=1)
                            axes[0].plot(close.index, hist["Close"].rolling(50).mean(), label="SMA50", color="#EF4444", linewidth=1)
                            axes[0].set_title(f"{{symbol}} — Price & Moving Averages", fontsize=14)
                            axes[0].legend(); axes[0].grid(alpha=0.3)
                            # Volume
                            axes[1].bar(hist.index, hist["Volume"], color="#C08050", alpha=0.5, label="Volume")
                            axes[1].set_title("Volume"); axes[1].grid(alpha=0.3)
                            plt.tight_layout()
                            os.makedirs("outputs", exist_ok=True)
                            out = os.path.abspath(os.path.join("outputs", f"{symbol}_analysis.png"))
                            plt.savefig(out, dpi=150, bbox_inches="tight")
                            plt.close()
                            print(f"Chart saved: {{out}}")
                            print(f"Price: ${{data['current_price']}}  RSI: {{data['rsi']}}  "
                                  f"Annual Return: {{data['annual_return']*100:.1f}}%  Vol: {{data['volatility']*100:.1f}}%")
                        """),
                    "requirements.txt": "numpy\npandas\nyfinance\nmatplotlib\n",
                    "README.md": textwrap.dedent("""\
                        # {project}
                        Stock analysis project generated by Aria CLI.

                        ## Usage
                        ```bash
                        pip3 install -r requirements.txt
                        python3 main.py AAPL
                        ```
                        """),
                },
            },
            "strategy": {
                "description": "Quant trading strategy with backtesting",
                "files": {
                    "main.py": textwrap.dedent("""\
                        #!/usr/bin/env python3
                        \"\"\"
                        {project} — backtest entry point.
                        Usage: python3 main.py AAPL 2022-01-01 2024-01-01
                        \"\"\"
                        import sys
                        from strategy import MomentumStrategy
                        from backtest import run_backtest

                        if __name__ == "__main__":
                            symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
                            start  = sys.argv[2] if len(sys.argv) > 2 else "2022-01-01"
                            end    = sys.argv[3] if len(sys.argv) > 3 else "2024-01-01"
                            strat  = MomentumStrategy(lookback=20)
                            result = run_backtest(strat, symbol, start, end)
                            print(result)
                        """),
                    "strategy.py": textwrap.dedent("""\
                        \"\"\"Strategy definitions for {project}.\"\"\"
                        import pandas as pd


                        class MomentumStrategy:
                            def __init__(self, lookback: int = 20):
                                self.lookback = lookback
                                self.name = f"Momentum({{lookback}})"

                            def generate_signals(self, prices: pd.Series) -> pd.Series:
                                \"\"\"Return +1 (long), -1 (short), 0 (flat) signals.\"\"\"
                                momentum = prices.pct_change(self.lookback)
                                signals = pd.Series(0, index=prices.index)
                                signals[momentum > 0] = 1
                                signals[momentum < 0] = -1
                                return signals.shift(1).fillna(0)  # avoid lookahead
                        """),
                    "backtest.py": textwrap.dedent("""\
                        \"\"\"Backtest engine for {project}.\"\"\"
                        import os
                        import numpy as np
                        import pandas as pd
                        import yfinance as yf
                        import matplotlib; matplotlib.use('Agg')
                        import matplotlib.pyplot as plt


                        def run_backtest(strategy, symbol: str, start: str, end: str) -> dict:
                            ticker = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
                            if ticker.empty:
                                raise ValueError(f"No data for {{symbol}}")
                            prices = ticker["Close"].squeeze()
                            signals = strategy.generate_signals(prices)
                            returns = prices.pct_change().fillna(0)
                            strat_returns = signals * returns
                            equity = (1 + strat_returns).cumprod()
                            bh_equity = (1 + returns).cumprod()
                            # Metrics
                            ann_return = strat_returns.mean() * 252
                            ann_vol    = strat_returns.std() * (252 ** 0.5)
                            sharpe     = ann_return / ann_vol if ann_vol > 0 else 0
                            max_dd     = (equity / equity.cummax() - 1).min()
                            # Plot
                            fig, ax = plt.subplots(figsize=(14, 6))
                            ax.plot(equity.index, equity, label=strategy.name, color="#C08050", linewidth=2)
                            ax.plot(bh_equity.index, bh_equity, label="Buy & Hold", color="#2AE8A5", linewidth=1.5, linestyle="--")
                            ax.set_title(f"{{symbol}} — {{strategy.name}} Backtest"); ax.legend(); ax.grid(alpha=0.3)
                            os.makedirs("outputs", exist_ok=True)
                            out = os.path.abspath(os.path.join("outputs", f"{{symbol}}_backtest.png"))
                            plt.savefig(out, dpi=150, bbox_inches="tight"); plt.close()
                            result = {{
                                "symbol": symbol, "strategy": strategy.name,
                                "ann_return": round(ann_return * 100, 2),
                                "ann_vol": round(ann_vol * 100, 2),
                                "sharpe": round(sharpe, 3),
                                "max_drawdown": round(max_dd * 100, 2),
                                "chart": out,
                            }}
                            print(f"Sharpe: {{result['sharpe']}}  Return: {{result['ann_return']}}%  "
                                  f"MaxDD: {{result['max_drawdown']}}%  Chart: {{out}}")
                            return result
                        """),
                    "requirements.txt": "numpy\npandas\nyfinance\nmatplotlib\n",
                    "README.md": textwrap.dedent("""\
                        # {project}
                        Quant strategy backtest project generated by Aria CLI.

                        ## Usage
                        ```bash
                        pip3 install -r requirements.txt
                        python3 main.py SPY 2022-01-01 2024-01-01
                        ```
                        """),
                },
            },
            "pipeline": {
                "description": "Market data pipeline (fetch → process → store)",
                "files": {
                    "main.py": textwrap.dedent("""\
                        #!/usr/bin/env python3
                        \"\"\"
                        {project} — data pipeline entry point.
                        Usage: python3 main.py AAPL MSFT TSLA
                        \"\"\"
                        import sys
                        from pipeline import DataPipeline

                        if __name__ == "__main__":
                            symbols = sys.argv[1:] or ["AAPL", "MSFT", "TSLA"]
                            pipe = DataPipeline(symbols)
                            pipe.run()
                        """),
                    "pipeline.py": textwrap.dedent("""\
                        \"\"\"Data pipeline for {project}.\"\"\"
                        import os
                        import pandas as pd
                        import yfinance as yf


                        class DataPipeline:
                            def __init__(self, symbols: list, period: str = "1y", output_dir: str = "data"):
                                self.symbols = symbols
                                self.period = period
                                self.output_dir = os.path.expanduser(output_dir)
                                os.makedirs(self.output_dir, exist_ok=True)

                            def fetch(self, symbol: str) -> pd.DataFrame:
                                df = yf.download(symbol, period=self.period, auto_adjust=True, progress=False)
                                df.columns = df.columns.droplevel(1) if df.columns.nlevels > 1 else df.columns
                                return df

                            def process(self, df: pd.DataFrame) -> pd.DataFrame:
                                df = df.copy()
                                df["Returns"] = df["Close"].pct_change()
                                df["SMA20"]   = df["Close"].rolling(20).mean()
                                df["SMA50"]   = df["Close"].rolling(50).mean()
                                df["Volatility"] = df["Returns"].rolling(20).std() * (252 ** 0.5)
                                return df.dropna()

                            def store(self, symbol: str, df: pd.DataFrame):
                                path = os.path.join(self.output_dir, f"{{symbol}}.csv")
                                df.to_csv(path)
                                print(f"  Saved {{len(df)}} rows → {{path}}")

                            def run(self):
                                print(f"Running pipeline for: {{self.symbols}}")
                                for symbol in self.symbols:
                                    try:
                                        raw = self.fetch(symbol)
                                        processed = self.process(raw)
                                        self.store(symbol, processed)
                                    except Exception as e:
                                        print(f"  Error {{symbol}}: {{e}}")
                                print("Pipeline complete.")
                        """),
                    "requirements.txt": "pandas\nyfinance\n",
                    "README.md": textwrap.dedent("""\
                        # {project}
                        Market data pipeline generated by Aria CLI.

                        ## Usage
                        ```bash
                        pip3 install -r requirements.txt
                        python3 main.py AAPL MSFT TSLA
                        # Output CSVs saved to ./data/
                        ```
                        """),
                },
            },
            "blank": {
                "description": "Blank project scaffold",
                "files": {
                    "main.py": textwrap.dedent("""\
                        #!/usr/bin/env python3
                        \"\"\"
                        {project} — main entry point.
                        \"\"\"
                        import os
                        import sys
                        import numpy as np
                        import pandas as pd


                        def main():
                            print("Hello from {project}!")


                        if __name__ == "__main__":
                            main()
                        """),
                    "requirements.txt": "numpy\npandas\n",
                    "README.md": "# {project}\n\nProject generated by Aria CLI.\n",
                },
            },
        }

        if template not in TEMPLATES:
            msg = f"Unknown template '{template}'. Available: {', '.join(TEMPLATES)}"
            self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
            return

        tmpl = TEMPLATES[template]
        files = {
            k: v.format(project=project_name) if isinstance(v, str) else v
            for k, v in tmpl["files"].items()
        }

        # ── Preview: show tree + file summaries ──────────────────────────────
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold]Scaffold:[/bold] [cyan]{project_name}[/cyan]  "
                          f"[dim]({tmpl['description']}, {template} template)[/dim]")
            self.context.console.print(f"  [dim]Location:[/dim] {base_dir}")
            self.context.console.print()
            self.context.console.print(f"  [dim]{base_dir.name}/[/dim]")
            for fname, fcontent in files.items():
                lines = fcontent.count("\n") + 1 if fcontent else 0
                exists_tag = " [yellow](exists)[/yellow]" if (base_dir / fname).exists() else ""
                self.context.console.print(f"  [dim]  ├── {fname:<24s}[/dim] {lines} lines{exists_tag}")
            self.context.console.print()
        else:
            print(f"\nScaffold: {project_name}  ({template} template)")
            print(f"Location: {base_dir}")
            print(f"\n  {base_dir.name}/")
            for fname, fcontent in files.items():
                lines = fcontent.count("\n") + 1 if fcontent else 0
                exists_tag = " (exists)" if (base_dir / fname).exists() else ""
                print(f"    ├── {fname:<24s}  {lines} lines{exists_tag}")
            print()

        # ── Ask: approve all / approve each / cancel ─────────────────────────
        # In non-interactive mode (-p flag / piped stdin) auto-approve all files.
        if not sys.stdin.isatty():
            choice = "y"
            self.context.console.print("  [dim](非交互模式：自动确认创建所有文件)[/dim]") if self.context.has_rich else print("  (Auto-approved: non-interactive mode)")
        elif self.context.has_rich:
            choice = self.context.console.input(
                "  [bold]Create these files?[/bold] "
                "[dim]\\[y=all / n=cancel / r=review each][/dim] "
            ).strip().lower()
        else:
            choice = input("  Create these files? [y=all / n=cancel / r=review each] ").strip().lower()

        if choice in ("n", "no"):
            self.context.console.print("[dim]Scaffold cancelled.[/dim]" if self.context.has_rich else "Cancelled.")
            return

        approve_each = choice in ("r", "review")
        created, skipped = [], []

        for fname, fcontent in files.items():
            target = base_dir / fname
            if approve_each:
                if self.context.has_rich:
                    self.context.console.print(f"\n  [dim]{fname}[/dim]  ({fcontent.count(chr(10))+1} lines)")
                    sub = self.context.console.input(
                        "  [dim]Write this file? [y/n] [/dim]"
                    ).strip().lower()
                else:
                    print(f"\n  {fname}  ({fcontent.count(chr(10))+1} lines)")
                    sub = input("  Write? [y/n] ").strip().lower()
                if sub not in ("y", "yes", ""):
                    skipped.append(fname)
                    continue

            result = _tool_write_file({"path": str(target), "content": fcontent, "_skip_confirm": True})
            if result["success"]:
                created.append(fname)
            else:
                err = result.get("error", "?")
                if self.context.has_rich:
                    self.context.console.print(f"  [red]Failed {fname}: {err}[/red]")
                else:
                    print(f"  Failed {fname}: {err}")

        # ── Summary ───────────────────────────────────────────────────────────
        if self.context.has_rich:
            self.context.console.print()
            if created:
                self.context.console.print(f"  [green]✓[/green] Created {len(created)} file(s) in [bold]{base_dir}[/bold]")
                for f in created:
                    self.context.console.print(f"    [dim]{f}[/dim]")
            if skipped:
                self.context.console.print(f"  [dim]Skipped: {', '.join(skipped)}[/dim]")
            self.context.console.print()
            self.context.console.print(f"  [dim]Run:  cd \"{base_dir}\" && python3 main.py[/dim]")
            self.context.console.print()
        else:
            print(f"\nCreated {len(created)} files in {base_dir}")
            if skipped:
                print(f"Skipped: {', '.join(skipped)}")
            print(f"Run: cd \"{base_dir}\" && python3 main.py")

    async def _strategy_overview(self, vault):
        """所有策略一览看板：版本数 / 最新 / 回测 Sharpe·收益 / 审查 / 是否部署实盘。"""

        names = vault.list_all_names()
        if not names:
            self.context.console.print("  [dim]还没有保存任何策略。用 /strategy save 开始。[/dim]" if self.context.has_rich
                          else "no strategies")
            return
        deployed = set()
        try:
            from portfolio_ledger import PortfolioLedger
            groups = PortfolioLedger().positions_by_strategy(names)
            deployed = {k for k in groups if k != PortfolioLedger.UNATTRIBUTED}
        except Exception:
            pass

        if self.context.has_rich:
            from rich.table import Table
            self.context.console.print()
            self.context.console.print("  [bold cyan]策略总览[/bold cyan]")
            tbl = Table(box=None, header_style="bold", pad_edge=False)
            tbl.add_column("策略", width=20)
            tbl.add_column("版本", justify="right", width=4)
            tbl.add_column("最新", width=8)
            tbl.add_column("回测", width=22)
            tbl.add_column("审查", justify="center", width=4)
            tbl.add_column("实盘", justify="center", width=4)
            for nm in names:
                vers = vault.list(nm, limit=50)
                if not vers:
                    continue
                latest = vers[0]
                btv = next((v for v in vers if v.backtest_result), None)
                if btv and btv.backtest_result:
                    br = btv.backtest_result
                    sh = br.get("sharpe_ratio")
                    rt = br.get("total_return_pct", br.get("total_return"))
                    if rt is not None:
                        rt = rt * 100 if abs(rt) < 5 else rt   # 兼容 0.18 / 18.0
                    if sh is not None and rt is not None:
                        body = f"Sharpe {sh:.2f} · {rt:+.1f}%"
                    elif sh is not None:
                        body = f"Sharpe {sh:.2f}"
                    else:
                        body = "—"
                    col = "green" if (sh or 0) >= 1 else ("yellow" if (sh or 0) > 0 else "red")
                    bt = f"[{col}]{body}[/{col}]"
                else:
                    bt = "[dim]未回测[/dim]"
                rv = "[green]✓[/green]" if latest.review_result else "[dim]—[/dim]"
                live = "[green]●[/green]" if nm in deployed else "[dim]○[/dim]"
                tbl.add_row(nm[:20], str(len(vers)), latest.version_tag, bt, rv, live)
            self.context.console.print(tbl)
            self.context.console.print("  [dim]详情: /strategy show <名字>   ·   部署: /deploy <名字> SYM:qty[/dim]")
            self.context.console.print()
        else:
            for nm in names:
                vers = vault.list(nm, limit=50)
                if vers:
                    print(f"  {nm}: {len(vers)} versions, latest {vers[0].version_tag}"
                          + (" [deployed]" if nm in deployed else ""))

    @staticmethod
    def _parse_deploy_token(tok: str):
        """
        解析部署持仓 token → (symbol, qty_or_None, weight_or_None, price_or_None)。

          SYM:qty[@price]    固定股数        e.g. AAPL:10      AAPL:10@150
          SYM:pct%[@price]   占资金比例(权重)  e.g. AAPL:30%     AAPL:30%@150

        价格可省（返回 None，由调用方取实时价）；权重模式由调用方结合资金折算股数。
        非法输入抛 ValueError。作为 staticmethod 经 self 调用，以在 mixin 全局重绑后仍可用。
        """
        sym, sep, rest = tok.partition(":")
        if not sep or not rest.strip():
            raise ValueError("缺数量，用 SYM:qty 或 SYM:pct%")
        amt_s, _, px_s = rest.partition("@")
        amt_s = amt_s.strip()
        price = float(px_s) if px_s.strip() else None
        if price is not None and price <= 0:
            raise ValueError("价格须为正")
        sym = sym.strip().upper()
        if amt_s.endswith("%"):
            weight = float(amt_s[:-1]) / 100.0
            if not (0 < weight <= 1.0):
                raise ValueError("权重须在 0–100%")
            return sym, None, weight, price
        qty = float(amt_s)
        if qty <= 0:
            raise ValueError("数量须为正")
        return sym, qty, None, price

    @staticmethod
    def _value_weights(positions, prices):
        """市值权重：{sym: net_qty} + {sym: price} → {sym: weight}（总市值为 0 或无价时返回空）。"""
        vals = {s: positions[s] * prices[s] for s in positions if prices.get(s)}
        tot = sum(vals.values())
        return {s: v / tot for s, v in vals.items()} if tot > 0 else {}

    @staticmethod
    def _rebalance_plan(current, targets, prices, capital=None):
        """
        计算把现有持仓对齐到目标权重所需的调仓（纯函数，可测）。

          current: {sym: net_qty}   targets: {sym: weight}   prices: {sym: price}
          capital: 组合总值；省略则用现有持仓市值之和（在现有资金内再平衡）。

        返回按标的排序的计划项 list：{symbol, cur_weight, target_weight, side, shares, price}。
        只产生非平凡调仓；不做空（SELL 数量上限为当前持仓）。
        """
        total = capital if capital else sum(current.get(s, 0.0) * prices[s] for s in prices if s in current)
        if not total or total <= 0:
            return []
        plan = []
        for s in sorted(set(current) | set(targets)):
            px = prices.get(s)
            if not px:
                continue
            cq = current.get(s, 0.0)
            cw = (cq * px) / total
            tw = targets.get(s, 0.0)
            dshares = round((tw * total - cq * px) / px, 4)
            if abs(dshares) < 1e-4:
                continue
            side = "BUY" if dshares > 0 else "SELL"
            shares = abs(dshares)
            if side == "SELL":
                shares = min(shares, cq)        # 不做空：卖出不超过持仓
            if shares < 1e-4:
                continue
            plan.append({"symbol": s, "cur_weight": cw, "target_weight": tw,
                         "side": side, "shares": round(shares, 4), "price": px})
        return plan

    async def cmd_deploy(self, args: str):
        """
        部署策略到实盘账本，闭合 回测 → 实盘 环。

        /deploy <策略> AAPL:10 MSFT:5@320       — 按股数建仓（@价格可省，省则取实时价）
        /deploy <策略> $100000 AAPL:30% MSFT:20% — 按权重建仓（先给资金，自动按实时价折算股数）
        /deploy <策略> rebalance [apply] AAPL:30% MSFT:20% — 对齐到目标权重（默认预览，apply 落账）
        /deploy <策略> rebalance equal | like <参考策略>     — 等权 / 对齐另一策略的市值权重
        /deploy <策略> close                    — 平掉该策略当前实盘净持仓
        /deploy <策略>                          — 显示该策略回测摘要与用法

        交易自动打标记 reason="deploy <策略> @<版本>"，从而被
        /strategy show（实盘 vs 回测）与 /portfolio holdings（分组看板）关联。
        """

        if not _get__HAS_VAULT():
            self.context.console.print("[yellow]strategy_vault.py 未找到[/yellow]" if self.context.has_rich else "strategy_vault not found")
            return
        parts = args.strip().split()
        if not parts:
            msg = ("用法: /deploy <策略> SYM:qty[@price] …  |  /deploy <策略> $资金 SYM:pct% …  |  "
                   "/deploy <策略> close")
            self.context.console.print(f"  [yellow]{msg}[/yellow]" if self.context.has_rich else msg)
            return

        def _live_price(sym):
            try:
                import yfinance as yf
                h = yf.Ticker(sym).history(period="1d")
                return float(h["Close"].iloc[-1]) if not h.empty else None
            except Exception:
                return None

        name  = parts[0]
        vault = _get_vault()
        all_versions = vault.list(name, limit=20)
        if not all_versions:
            self.context.console.print(f"  [red]未找到策略 '{name}'[/red]，先 /strategy save。" if self.context.has_rich
                          else f"Not found: {name}")
            return
        latest = all_versions[0]
        bt = latest.backtest_result or next((v.backtest_result for v in all_versions if v.backtest_result), None)

        from portfolio_ledger import PortfolioLedger
        ledger = PortfolioLedger()
        rest = parts[1:]

        # /deploy <策略>  → 回测摘要 + 用法（你将对照它部署）
        if not rest:
            if self.context.has_rich:
                self.context.console.print(f"\n  [bold cyan]部署 · {name}[/bold cyan]  [dim]最新 {latest.version_tag} · {latest.created_at[:10]}[/dim]")
                if bt:
                    def _g(*ks):
                        for k in ks:
                            if bt.get(k) is not None:
                                return bt[k]
                        return None
                    sh = _g("sharpe_ratio"); rt = _g("total_return_pct", "total_return")
                    self.context.console.print(f"  [dim]回测: Sharpe {sh if sh is not None else '—'} · "
                                  f"总收益 {rt if rt is not None else '—'}[/dim]")
                else:
                    self.context.console.print("  [dim]该策略尚无回测。建议先 /backtest 再部署。[/dim]")
                self.context.console.print("  [dim]建仓: /deploy {0} AAPL:10 MSFT:5@320   平仓: /deploy {0} close[/dim]\n".format(name))
            else:
                print(f"deploy {name} (latest {latest.version_tag}); usage: /deploy {name} SYM:qty[@price] | close")
            return

        # /deploy <策略> close → 平掉该策略净持仓
        if rest[0].lower() == "close":
            poss = ledger.positions_by_strategy([name]).get(name, [])
            if not poss:
                self.context.console.print(f"  [dim]{name} 当前无实盘净持仓。[/dim]" if self.context.has_rich else "no positions")
                return
            done = []
            for p in poss:
                px = _live_price(p["symbol"]) or p["avg_cost"]
                tid = ledger.add_trade(p["symbol"], "SELL", p["net_qty"], px,
                                       reason=f"deploy {name} close @{latest.version_tag}")
                done.append((tid, p["symbol"], p["net_qty"], px))
            if self.context.has_rich:
                self.context.console.print(f"\n  [green]✓ 已平仓 {name}[/green]  [dim]{len(done)} 笔[/dim]")
                for tid, s, q, px in done:
                    self.context.console.print(f"   [red]SELL[/red] {s} × {q:g} @ {px:,.2f}  [dim]#{tid}[/dim]")
                self.context.console.print("  [dim]撤销: /journal delete <id>[/dim]\n")
            else:
                for tid, s, q, px in done:
                    print(f"  SELL {s} {q} @ {px}  #{tid}")
            return

        # /deploy <策略> rebalance [apply] [$资金] SYM:pct% … → 对齐到目标权重（默认预览）
        if rest[0].lower() == "rebalance":
            sub = rest[1:]
            do_apply = bool(sub and sub[0].lower() == "apply")
            if do_apply:
                sub = sub[1:]
            # 剥离资金 token（$100000 / cap:100000）
            capital, errs, rest2 = None, [], []
            for tok in sub:
                t = tok.strip()
                if t.startswith("$") or t.lower().startswith(("cap:", "cap=")):
                    cs = t[1:] if t.startswith("$") else t[4:]
                    try:
                        capital = float(cs.replace(",", ""))
                    except ValueError:
                        pass
                    continue
                rest2.append(tok)

            cur = {p["symbol"]: p for p in ledger.positions_by_strategy([name]).get(name, [])}
            mode = rest2[0].lower() if rest2 else ""

            # 目标权重来源：equal（当前持仓等权）/ like <策略>（对齐另一策略市值权重）/ 显式 SYM:pct%
            targets = {}   # sym -> (weight, price_or_None)
            if mode == "equal":
                if not cur:
                    self.context.console.print(f"  [yellow]{name} 无持仓，无法等权再平衡。[/yellow]" if self.context.has_rich else "no positions")
                    return
                w = 1.0 / len(cur)
                targets = {s: (w, None) for s in cur}
            elif mode == "like":
                if len(rest2) < 2:
                    self.context.console.print(f"  [yellow]用法: /deploy {name} rebalance like <参考策略>[/yellow]"
                                  if self.context.has_rich else "need ref strategy")
                    return
                ref = rest2[1]
                ref_pos = {p["symbol"]: p["net_qty"]
                           for p in ledger.positions_by_strategy([ref]).get(ref, [])}
                if not ref_pos:
                    self.context.console.print(f"  [yellow]参考策略 '{ref}' 无实盘持仓。[/yellow]" if self.context.has_rich else "ref empty")
                    return
                refw = self._value_weights(ref_pos, {s: (_live_price(s) or 0) for s in ref_pos})
                if not refw:
                    self.context.console.print(f"  [yellow]无法获取 '{ref}' 的报价以计算参考权重。[/yellow]"
                                  if self.context.has_rich else "no ref prices")
                    return
                targets = {s: (w, None) for s, w in refw.items()}
            else:
                for tok in rest2:
                    try:
                        s, q, w, px = self._parse_deploy_token(tok)
                        if w is None:
                            errs.append(f"{tok} (再平衡用权重 SYM:pct%)"); continue
                        targets[s] = (w, px)
                    except Exception as e:
                        errs.append(f"{tok} ({e})")
                if not targets:
                    self.context.console.print("  [yellow]需要目标权重：SYM:pct% …  |  equal  |  like <策略>[/yellow]"
                                  if self.context.has_rich else "need targets")
                    return
            syms = sorted(set(cur) | set(targets))
            prices = {}
            for s in syms:
                px = (targets.get(s, (None, None))[1] or _live_price(s)
                      or (cur[s]["avg_cost"] if s in cur else None))
                if px:
                    prices[s] = px
            plan = self._rebalance_plan({s: cur[s]["net_qty"] for s in cur},
                                        {s: w for s, (w, _) in targets.items()}, prices, capital)
            if not plan:
                self.context.console.print(f"  [green]{name} 已接近目标权重，无需调仓。[/green]" if self.context.has_rich else "balanced")
                return
            if self.context.has_rich:
                from rich.table import Table
                self.context.console.print()
                head = "执行再平衡" if do_apply else "再平衡预览"
                self.context.console.print(f"  [bold cyan]{name} · {head}[/bold cyan]")
                tbl = Table(box=None, header_style="bold", pad_edge=False)
                tbl.add_column("标的", width=8)
                tbl.add_column("当前%", justify="right", width=7)
                tbl.add_column("目标%", justify="right", width=7)
                tbl.add_column("操作", width=6)
                tbl.add_column("股数", justify="right", width=10)
                tbl.add_column("金额", justify="right", width=12)
                for it in plan:
                    sc = "green" if it["side"] == "BUY" else "red"
                    tbl.add_row(it["symbol"], f"{it['cur_weight']*100:.0f}%", f"{it['target_weight']*100:.0f}%",
                                f"[{sc}]{it['side']}[/{sc}]", f"{it['shares']:g}",
                                f"{it['shares']*it['price']:,.0f}")
                self.context.console.print(tbl)
                for e in errs:
                    self.context.console.print(f"   [yellow]跳过 {e}[/yellow]")
                if do_apply:
                    ids = [ledger.add_trade(it["symbol"], it["side"], it["shares"], it["price"],
                                            reason=f"deploy {name} rebalance @{latest.version_tag}")
                           for it in plan]
                    self.context.console.print(f"  [green]✓ 已执行 {len(ids)} 笔[/green] · [dim]撤销 /journal delete <id>[/dim]")
                else:
                    self.context.console.print(f"  [dim]以上为预览。执行: /deploy {name} rebalance apply …（同样参数）[/dim]")
                self.context.console.print()
            else:
                for it in plan:
                    print(f"  {it['side']} {it['symbol']} {it['shares']:g} @ {it['price']}")
                if do_apply:
                    for it in plan:
                        ledger.add_trade(it["symbol"], it["side"], it["shares"], it["price"],
                                         reason=f"deploy {name} rebalance @{latest.version_tag}")
                    print(f"  applied {len(plan)} trades")
            return

        # /deploy <策略> [$资金] SYM:qty|SYM:pct% [@price] … → 建仓
        # 先剥离资金 token（$100000 / cap:100000），用于按权重折算股数
        capital = None
        pos_tokens = []
        for tok in rest:
            t = tok.strip()
            if t.startswith("$") or t.lower().startswith(("cap:", "cap=")):
                cs = t[1:] if t.startswith("$") else t[4:]
                try:
                    capital = float(cs.replace(",", ""))
                except ValueError:
                    pass
                continue
            pos_tokens.append(tok)

        deployed, errors, total_weight = [], [], 0.0
        for tok in pos_tokens:
            try:
                sym, qty, weight, price = self._parse_deploy_token(tok)
                if price is None:
                    price = _live_price(sym)
                if not price:
                    errors.append(f"{sym} (取不到价，加 @price)"); continue
                if weight is not None:
                    if not capital:
                        errors.append(f"{sym} (按权重部署需先给资金，如 $100000)"); continue
                    total_weight += weight
                    qty = round(capital * weight / price, 4)
                tid = ledger.add_trade(sym, "BUY", qty, price,
                                       reason=f"deploy {name} @{latest.version_tag}")
                deployed.append((tid, sym, qty, price))
            except Exception as e:
                errors.append(f"{tok} ({e})")
        if total_weight > 1.0001:
            errors.append(f"权重合计 {total_weight*100:.0f}% > 100%（已按各自比例建仓，注意超配）")

        if self.context.has_rich:
            self.context.console.print(f"\n  [bold cyan]部署 {name} {latest.version_tag} → 实盘[/bold cyan]")
            total = 0.0
            for tid, s, q, px in deployed:
                total += q * px
                self.context.console.print(f"   [green]BUY[/green] {s} × {q:g} @ {px:,.2f}  = {q*px:,.0f}  [dim]#{tid}[/dim]")
            if deployed:
                self.context.console.print(f"  [dim]共投入 {total:,.0f} · 标记 reason='deploy {name} @{latest.version_tag}'[/dim]")
                self.context.console.print(f"  [dim]→ /strategy show {name} 看实盘 vs 回测 · /portfolio holdings 看分组看板[/dim]")
            for e in errors:
                self.context.console.print(f"   [yellow]跳过 {e}[/yellow]")
            self.context.console.print()
        else:
            for tid, s, q, px in deployed:
                print(f"  BUY {s} {q} @ {px}  #{tid}")
            for e in errors:
                print(f"  skip {e}")

    async def cmd_strategy(self, args: str):
        """
        策略版本管理系统 (Strategy Vault)

        /strategy save [name] [message]   — 保存当前对话中最后一段代码
        /strategy list [name]             — 列出所有版本
        /strategy diff [name] [v1] [v2]   — 查看版本差异
        /strategy load [name] [tag/id]    — 加载版本到上下文
        /strategy review                  — AI审查+静态检测
        """

        if not _get__HAS_VAULT():
            self.context.console.print("  [yellow]strategy_vault.py 未找到[/yellow]" if self.context.has_rich
                          else "  strategy_vault.py not found")
            return

        parts = args.strip().split(None, 3)
        sub   = parts[0].lower() if parts else "list"

        vault = _get_vault()

        # ── save ──────────────────────────────────────────────────────────
        if sub == "save":
            # 从对话历史中提取最后一段 Python 代码
            code = self._extract_last_code()
            if not code:
                if self.context.has_rich:
                    self.context.console.print("  [yellow]未在对话中找到代码块。先让 Aria 生成策略代码。[/yellow]")
                else:
                    print("  No code found in conversation. Generate strategy code first.")
                return
            name    = parts[1] if len(parts) > 1 and not parts[1].startswith('"') else "strategy"
            message = " ".join(parts[2:]).strip('"') if len(parts) > 2 else ""
            sv = vault.save(code, name=name, message=message)
            if self.context.has_rich:
                self.context.console.print(
                    f"\n  [green]✓[/green] 策略已保存  "
                    f"[bold]{sv.name}[/bold] [dim]{sv.version_tag}[/dim]  "
                    f"hash={sv.code_hash}  {sv.created_at[:16]}"
                )
            else:
                print(f"  Saved: {sv.name} {sv.version_tag} ({sv.created_at[:16]})")

        # ── list ──────────────────────────────────────────────────────────
        elif sub == "list":
            name = parts[1] if len(parts) > 1 else None
            if name:
                versions = vault.list(name)
                title = f"  策略: {name}"
            else:
                # Show all strategies
                all_names = vault.list_all_names()
                if not all_names:
                    self.context.console.print("  [dim]策略金库为空。使用 /strategy save 保存策略。[/dim]" if self.context.has_rich
                                  else "  Vault is empty.")
                    return
                if self.context.has_rich:
                    self.context.console.print("\n  [bold]策略金库[/bold]\n")
                    for n in all_names:
                        vs = vault.list(n, limit=3)
                        latest = vs[0] if vs else None
                        if latest:
                            bt = ""
                            if latest.backtest_result:
                                br = latest.backtest_result
                                bt = f"  sharpe={br.get('sharpe_ratio','?')} ret={br.get('total_return_pct','?')}%"
                            self.context.console.print(
                                f"  [bold]{n}[/bold]  [dim]{len(vs)}个版本  "
                                f"最新:{latest.version_tag}  {latest.created_at[:10]}{bt}[/dim]"
                            )
                    self.context.console.print()
                else:
                    for n in all_names:
                        print(f"  {n}")
                return
            if not versions:
                self.context.console.print(f"  [dim]没有找到策略 '{name}'[/dim]" if self.context.has_rich else f"  Not found: {name}")
                return
            if self.context.has_rich:
                self.context.console.print(f"\n  [bold]{title}[/bold]\n")
                for v in versions:
                    bt = ""
                    if v.backtest_result:
                        br = v.backtest_result
                        sharpe = br.get("sharpe_ratio")
                        ret    = br.get("total_return_pct")
                        bt = f"  [green]sharpe={sharpe:.2f}  ret={ret:.1f}%[/green]" if sharpe else ""
                    reviewed = "  [dim]✓reviewed[/dim]" if v.review_result else ""
                    msg = f"  [dim]{v.message[:50]}[/dim]" if v.message else ""
                    self.context.console.print(
                        f"  [dim]{v.id:4d}[/dim]  [bold]{v.version_tag}[/bold]  "
                        f"[dim]{v.created_at[:16]}[/dim]{msg}{bt}{reviewed}"
                    )
                self.context.console.print()
            else:
                for v in versions:
                    print(v.summary_line())

        # ── show: 统一策略工作台 (版本史 + 回测 + 实盘持仓) ──────────────────
        elif sub == "show":
            # 不带名字 → 所有策略总览看板
            if len(parts) <= 1:
                await self._strategy_overview(vault)
                return
            name = parts[1]
            versions = vault.list(name, limit=20)
            if not versions:
                self.context.console.print(f"  [dim]没有找到策略 '{name}'。用 /strategy list 查看全部。[/dim]" if self.context.has_rich
                              else f"  Not found: {name}")
                return
            latest = versions[0]
            bt_ver = next((v for v in versions if v.backtest_result), None)

            def _num(d, *keys):
                for k in keys:
                    val = d.get(k)
                    if val is not None:
                        return val
                return None

            def _pct(x):
                if x is None:
                    return "—"
                v = x * 100 if abs(x) < 5 else x   # 兼容 0.18 与 18.0 两种存法
                return f"{v:+.1f}%"

            def _spark(curve):
                vals = []
                for x in (curve or []):
                    if isinstance(x, (int, float)):
                        vals.append(float(x))
                    elif isinstance(x, dict):
                        for k in ("equity", "value", "nav", "total", "close"):
                            if x.get(k) is not None:
                                vals.append(float(x[k])); break
                if len(vals) < 2:
                    return ""
                blocks = "▁▂▃▄▅▆▇█"
                s = vals[:: max(1, len(vals) // 48)]
                lo, hi = min(s), max(s)
                if hi <= lo:
                    return blocks[0] * len(s)
                return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in s)

            if self.context.has_rich:
                self.context.console.print()
                reviewed = "✓ 已审查" if latest.review_result else "未审查"
                self.context.console.print(f"  [bold cyan]策略工作台 · {name}[/bold cyan]")
                self.context.console.print(f"  [dim]最新 {latest.version_tag} · {latest.created_at[:16]} · "
                              f"{len(versions)} 个版本 · {reviewed} · hash {latest.code_hash}[/dim]")
                if latest.message:
                    self.context.console.print(f"  [dim]“{latest.message[:70]}”[/dim]")

                self.context.console.print("\n  [bold]版本史[/bold]")
                for v in versions[:6]:
                    br = v.backtest_result or {}
                    sh = _num(br, "sharpe_ratio")
                    rt = _num(br, "total_return_pct", "total_return")
                    mtxt = f"  [green]Sharpe {sh:.2f} · {_pct(rt)}[/green]" if sh is not None else ""
                    marker = "●" if v is latest else "○"
                    msg = f"  [dim]{v.message[:42]}[/dim]" if v.message else ""
                    self.context.console.print(f"  [cyan]{marker}[/cyan] [bold]{v.version_tag}[/bold] "
                                  f"[dim]{v.created_at[:10]}[/dim]{mtxt}{msg}")

                self.context.console.print("\n  [bold]最新回测[/bold]")
                if bt_ver and bt_ver.backtest_result:
                    br = bt_ver.backtest_result
                    self.context.console.print(
                        f"  [dim]({bt_ver.version_tag})[/dim]  总收益 {_pct(_num(br,'total_return_pct','total_return'))}  ·  "
                        f"年化 {_pct(_num(br,'annualized_return','annual_return'))}  ·  "
                        f"Sharpe {_num(br,'sharpe_ratio') if _num(br,'sharpe_ratio') is not None else '—'}  ·  "
                        f"回撤 {_pct(_num(br,'max_drawdown'))}  ·  胜率 {_pct(_num(br,'win_rate'))}")
                    spark = _spark(br.get("equity_curve"))
                    if spark:
                        self.context.console.print(f"  [green]{spark}[/green]")
                else:
                    self.context.console.print("  [dim]尚无回测。运行 /backtest 后回测结果会关联到该策略版本。[/dim]")

                self.context.console.print("\n  [bold]实盘部署[/bold]")
                try:
                    from portfolio_ledger import PortfolioLedger
                    trades = PortfolioLedger().get_trades(limit=2000)
                    tagged = [t for t in trades if name.lower() in str(t.get("reason") or "").lower()]
                    if not tagged:
                        self.context.console.print(f"  [dim]未部署到实盘。给交易加 reason 含 '{name}' 即可在此关联。[/dim]")
                    else:
                        # 从标记交易聚合每个标的：净持仓 + 买入均价
                        agg = {}  # sym -> [net_qty, buy_qty, buy_cost]
                        for t in tagged:
                            q  = float(t.get("qty") or 0)
                            px = float(t.get("price") or 0)
                            a  = agg.setdefault(t["symbol"], [0.0, 0.0, 0.0])
                            if str(t.get("side")).upper() == "BUY":
                                a[0] += q; a[1] += q; a[2] += q * px
                            else:
                                a[0] -= q
                        held = {s: v for s, v in agg.items() if abs(v[0]) > 1e-6}
                        if not held:
                            self.context.console.print(f"  [green]已部署[/green] · {len(tagged)} 笔标记交易 · [dim](已全部平仓)[/dim]")
                        else:
                            self.context.console.print(f"  [dim]获取 {len(held)} 只实时报价…[/dim]")
                            live = {}
                            try:
                                import yfinance as yf
                                for s in held:
                                    try:
                                        h = yf.Ticker(s).history(period="1d")
                                        if not h.empty:
                                            live[s] = float(h["Close"].iloc[-1])
                                    except Exception:
                                        pass
                            except ImportError:
                                pass

                            from rich.table import Table
                            tbl = Table(box=None, header_style="bold", pad_edge=False)
                            tbl.add_column("标的", width=8)
                            tbl.add_column("净持仓", justify="right", width=10)
                            tbl.add_column("买入均价", justify="right", width=10)
                            tbl.add_column("现价", justify="right", width=10)
                            tbl.add_column("浮动盈亏", justify="right", width=13)
                            tbl.add_column("%", justify="right", width=8)
                            tot_cost = tot_mv = 0.0
                            have_px = False
                            for s, (nq, bq, bc) in sorted(held.items()):
                                avg  = bc / bq if bq else 0.0
                                cost = nq * avg
                                tot_cost += cost
                                px = live.get(s)
                                if px is not None:
                                    have_px = True
                                    mv  = nq * px
                                    tot_mv += mv
                                    pnl = mv - cost
                                    pct = (pnl / cost * 100) if cost else 0.0
                                    c = "green" if pnl >= 0 else "red"
                                    tbl.add_row(s, f"{nq:,.4g}", f"{avg:,.4f}", f"{px:,.4f}",
                                                f"[{c}]{pnl:+,.2f}[/{c}]", f"[{c}]{pct:+.1f}%[/{c}]")
                                else:
                                    tot_mv += cost
                                    tbl.add_row(s, f"{nq:,.4g}", f"{avg:,.4f}", "N/A", "—", "—")
                            self.context.console.print(tbl)
                            if have_px and tot_cost:
                                live_pnl = tot_mv - tot_cost
                                live_pct = live_pnl / tot_cost * 100
                                c = "green" if live_pnl >= 0 else "red"
                                self.context.console.print(f"  [bold]实盘 {len(tagged)} 笔 · 成本 {tot_cost:,.0f} · "
                                              f"浮盈 [{c}]{live_pnl:+,.0f} ({live_pct:+.1f}%)[/{c}][/bold]")
                                # vs 回测：口径不同（实盘自部署以来 vs 回测全程），仅作参考信号
                                if bt_ver and bt_ver.backtest_result:
                                    bt_ret = _num(bt_ver.backtest_result, "total_return_pct", "total_return")
                                    if bt_ret is not None:
                                        bt_pct = bt_ret * 100 if abs(bt_ret) < 5 else bt_ret
                                        gap = live_pct - bt_pct
                                        gc  = "green" if gap >= 0 else "yellow"
                                        self.context.console.print(f"  [dim]vs 回测总收益 {bt_pct:+.1f}% · "
                                                      f"偏离 [{gc}]{gap:+.1f}pp[/{gc}]  (口径不同，仅供参考)[/dim]")
                            else:
                                pos_txt = ", ".join(f"{s} {v[0]:g}" for s, v in sorted(held.items()))
                                self.context.console.print(f"  [green]已部署[/green] · {len(tagged)} 笔 · {pos_txt}  [dim](报价不可用)[/dim]")
                except Exception:
                    self.context.console.print("  [dim]持仓账本不可用。[/dim]")
                self.context.console.print()
            else:
                print(f"Strategy {name}: latest {latest.version_tag}, {len(versions)} versions")
                if bt_ver and bt_ver.backtest_result:
                    br = bt_ver.backtest_result
                    print(f"  backtest: sharpe={br.get('sharpe_ratio')} "
                          f"return={br.get('total_return_pct', br.get('total_return'))}")
                try:
                    from portfolio_ledger import PortfolioLedger
                    tagged = [t for t in PortfolioLedger().get_trades(limit=2000)
                              if name.lower() in str(t.get("reason") or "").lower()]
                    if tagged:
                        print(f"  deployed: {len(tagged)} tagged trades")
                except Exception:
                    pass

        # ── diff ──────────────────────────────────────────────────────────
        elif sub == "diff":
            name  = parts[1] if len(parts) > 1 else "strategy"
            tag_a = parts[2] if len(parts) > 2 else None
            tag_b = parts[3] if len(parts) > 3 else None
            diff_text = vault.diff(name, tag_a, tag_b)
            if self.context.has_rich:
                self.context.console.print()
                # Simple color: + lines green, - lines red
                for line in diff_text.splitlines():
                    if line.startswith("+++") or line.startswith("---"):
                        self.context.console.print(f"  [bold]{line}[/bold]")
                    elif line.startswith("+"):
                        self.context.console.print(f"  [green]{line}[/green]")
                    elif line.startswith("-"):
                        self.context.console.print(f"  [red]{line}[/red]")
                    elif line.startswith("@@"):
                        self.context.console.print(f"  [cyan]{line}[/cyan]")
                    else:
                        self.context.console.print(f"  {line}")
                self.context.console.print()
            else:
                print(diff_text)

        # ── load ──────────────────────────────────────────────────────────
        elif sub == "load":
            name    = parts[1] if len(parts) > 1 else "strategy"
            tag     = parts[2] if len(parts) > 2 else None
            version = vault.load(name, version_tag=tag)
            if not version:
                self.context.console.print(f"  [red]未找到: {name} {tag or '(latest)'}[/red]" if self.context.has_rich
                              else f"  Not found: {name} {tag}")
                return
            # Inject code into conversation context as a user message
            code_msg = f"以下是策略 {version.name} {version.version_tag} 的代码：\n\n```python\n{version.code}\n```"
            self.terminal.conversation.append({"role": "assistant", "content": code_msg})
            if self.context.has_rich:
                self.context.console.print(
                    f"\n  [green]✓[/green] 已加载 [bold]{version.name} {version.version_tag}[/bold]  "
                    f"[dim]{len(version.code)} chars  {version.created_at[:16]}[/dim]"
                )
                self.context.console.print(f"  [dim]{version.message}[/dim]" if version.message else "")
                lines = version.code.count("\n")
                self.context.console.print(f"  [dim]代码 {lines} 行已注入上下文，可继续对话修改。[/dim]")
            else:
                print(f"  Loaded: {version.name} {version.version_tag}")

        # ── review ────────────────────────────────────────────────────────
        elif sub == "review":
            name    = parts[1] if len(parts) > 1 else "strategy"
            tag     = parts[2] if len(parts) > 2 else None
            version = vault.load(name, version_tag=tag)
            if not version:
                code = self._extract_last_code()
                if not code:
                    self.context.console.print("  [yellow]未找到策略，请先 /strategy save 或生成代码[/yellow]" if self.context.has_rich
                                  else "  No strategy found.")
                    return
                ver_id = None
            else:
                code   = version.code
                ver_id = version.id

            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [bold]🔬 策略审查中...[/bold]")
                self.context.console.print()

            ollama_url = self.terminal.config.get("ollama_url", "http://localhost:11434")
            model      = self.terminal.config.get("model", "qwen2.5:7b")
            bt_result  = version.backtest_result if version else None

            import sys
            def on_token(tok):
                sys.stdout.write(tok)
                sys.stdout.flush()

            review = await _ai_review(code, bt_result, ollama_url, model, on_token=on_token)

            # Print static results
            static = review.get("static", {})
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(f"\n  [bold]静态检测[/bold]  评级:{static.get('grade','?')}  "
                              f"{static.get('summary','')}")
                for e in static.get("errors", []):
                    self.context.console.print(f"  [red]❌ {e['detail']}[/red]")
                for w in static.get("warnings", []):
                    self.context.console.print(f"  [yellow]⚠️  {w['detail']}[/yellow]")
                for q in static.get("quality_checks", []):
                    self.context.console.print(f"  [dim]💡 {q}[/dim]")
                self.context.console.print()
            else:
                print(f"\n  Static: {static.get('summary','')}")

            if ver_id:
                vault.save_review(ver_id, review)
                if self.context.has_rich:
                    self.context.console.print("  [dim]审查结果已保存到策略金库[/dim]")

        else:
            if self.context.has_rich:
                self.context.console.print(
                    "\n  [bold]Strategy Vault 命令[/bold]\n\n"
                    "  /strategy save [name] [message]   保存当前代码快照\n"
                    "  /strategy list [name]              列出版本历史\n"
                    "  /strategy diff [name] [v1] [v2]   查看版本差异\n"
                    "  /strategy load [name] [tag]        加载版本到上下文\n"
                    "  /strategy review [name] [tag]      AI + 静态代码审查\n"
                )
            else:
                print("  Usage: /strategy save|list|diff|load|review [name] [tag]")

    # ── ML 信号组合回测 ──────────────────────────────────────────────────────────

    async def _cmd_ml_signal_backtest(
        self, symbol_args: list, start_date: str = "2023-01-01",
        end_date: str = "", capital: float = 1_000_000,
    ):
        """
        /backtest ml [sym1 sym2 ...] [--start YYYY-MM-DD] [--capital N]

        三策略对比: ML-Weighted / Equal-Weight / Buy-and-Hold
        支持 A股(T+1)、港股、美股混合组合。
        """
        # ML signal backtest is part of the private Arthera engine (alpha IP).
        # If a local Arthera checkout is present (dev), make it importable;
        # otherwise the import below fails and we show a Pro-feature notice.

        import sys
        import os
        _arthera_pkgs = os.environ.get("ARTHERA_ROOT") or os.path.expanduser("~/Desktop/Arthera")
        _arthera_pkgs = os.path.join(_arthera_pkgs, "packages")
        if os.path.isdir(_arthera_pkgs) and _arthera_pkgs not in sys.path:
            sys.path.insert(0, _arthera_pkgs)

        if self.context.has_rich:
            self.context.console.print("\n  [bold cyan]ML 信号组合回测[/bold cyan]  三策略对比\n")
        else:
            print("\n  ML 信号组合回测  三策略对比\n")

        # 解析标的列表（去掉标志位）
        symbols = [s.upper() for s in symbol_args if not s.startswith("--")]
        if not symbols:
            symbols = ["600519", "300750", "NVDA", "AAPL"]
            if self.context.has_rich:
                self.context.console.print(f"  [dim]未指定标的，使用默认组合: {symbols}[/dim]")

        if self.context.has_rich:
            self.context.console.print(f"  标的: [yellow]{' | '.join(symbols)}[/yellow]")
            self.context.console.print(f"  区间: {start_date} → {end_date or '今日'}")
            self.context.console.print(f"  初始资金: {capital:,.0f}\n")
            self.context.console.print("  [dim]正在拉取行情并训练模型，请稍候…[/dim]")

        try:
            from quant_engine.backtest.ml_signal_backtest import MLSignalBacktest

            bt = MLSignalBacktest(
                symbols=symbols,
                initial_cash=capital,
                rebalance_freq="W",
            )
            report = bt.run(start=start_date, end=end_date or "")
            report.print_report()

            if self.context.has_rich:
                # 额外渲染净值图（纯 ASCII sparkline）
                ml_nav  = report.ml_strategy.nav_series
                ew_nav  = report.ew_strategy.nav_series
                if not ml_nav.empty and not ew_nav.empty:
                    self.context.console.print("\n  [bold]净值走势（最近 40 个交易日）[/bold]")
                    _print_sparkline("ML 权重", ml_nav,  "cyan")
                    _print_sparkline("等权基准", ew_nav, "yellow")

        except ImportError:
            # Moat feature — the ML/alpha engine ships only with the full
            # Arthera platform, not the open CLI. Degrade with a clear notice.
            _msg = ("ML 信号回测属于 Arthera 高级引擎（含 ML 选股/alpha 因子），"
                    "开源 CLI 未内置。\n  基础回测可用：/backtest momentum <symbol>")
            if self.context.has_rich:
                self.context.console.print(f"  [#C08050]◆ Pro 功能[/#C08050]  [dim]{_msg}[/dim]")
            else:
                print(f"  ◆ Pro 功能  {_msg}")
        except Exception as e:
            print_error(self.context, f"ML 回测失败: {e}")
            import traceback
            self.context.console.print(f"  [dim]{traceback.format_exc()}[/dim]") if self.context.has_rich else print(traceback.format_exc())


def _print_sparkline(label: str, nav: "pd.Series", color: str = "white", width: int = 40):
    """打印 ASCII sparkline。"""
    try:
        import sys
        HAS_RICH = "rich" in sys.modules
        vals = nav.iloc[-width:].values if len(nav) > width else nav.values
        if len(vals) < 2:
            return
        lo, hi = vals.min(), vals.max()
        chars = "▁▂▃▄▅▆▇█"
        spark  = "".join(chars[min(7, int((v - lo) / (hi - lo + 1e-9) * 8))] for v in vals)
        change = (vals[-1] / vals[0] - 1) * 100
        sign   = "+" if change >= 0 else ""
        if has_rich():
            from rich.console import Console as _C
            _C().print(f"  [{color}]{label:<8}[/{color}] {spark}  [{color}]{sign}{change:.2f}%[/{color}]")
        else:
            print(f"  {label:<8} {spark}  {sign}{change:.2f}%")
    except Exception:
        pass
