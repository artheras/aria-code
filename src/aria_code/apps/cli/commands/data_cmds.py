"""DataCommandsMixin — data, alert, correlation, and comparison commands."""

from __future__ import annotations

from aria_code.apps.cli.i18n import ui_text

from ._ui import print_error


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def _render_portfolio_bt(*args, **kwargs):
    from aria_cli import _render_portfolio_bt as fn
    return fn(*args, **kwargs)
def _render_alerts(*args, **kwargs):
    from aria_cli import _render_alerts as fn
    return fn(*args, **kwargs)
def _get__HAS_LOCAL_FINANCE():
    from aria_cli import _HAS_LOCAL_FINANCE as val
    return val
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _render_corr_matrix(*args, **kwargs):
    from aria_cli import _render_corr_matrix as fn
    return fn(*args, **kwargs)
def _render_peer_comparison(*args, **kwargs):
    from aria_cli import _render_peer_comparison as fn
    return fn(*args, **kwargs)
def _render_sql_result(*args, **kwargs):
    from aria_cli import _render_sql_result as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class DataCommandsMixin:
    """Mixin: data analysis and comparison commands."""

    async def cmd_data(self, args: str):
        """
        /data sql "SELECT ..."     — DuckDB SQL 查询
        /data export [filename]    — 导出上次结果到 Excel
        /data load <csv_path>      — 加载 CSV 到 DuckDB
        /data tables               — 列出已加载的表
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().split(None, 1) if args.strip() else []
        sub = parts[0].lower() if parts else "help"
        rest = parts[1] if len(parts) > 1 else ""

        try:
            from aria_code.tools.data_analysis_tools import (sql_query, sql_list_tables,
                                              export_to_excel, load_csv_data)
        except ImportError as e:
            if self.context.has_rich:
                self.context.console.print(f"[red]data_analysis_tools 未加载: {e}[/red]")
            return

        if sub == "sql":
            query = rest.strip().strip('"').strip("'")
            if not query:
                if self.context.has_rich:
                    self.context.console.print("[dim]用法: /data sql \"SELECT ...\"|/dim]")
                return
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '执行 SQL', 'Running SQL')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, sql_query, {"query": query})
            else:
                r = sql_query({"query": query})
            _render_sql_result(r)

        elif sub == "export":
            fname = rest.strip() or None
            watchlist = self.terminal.config.get("watchlist", ["AAPL", "MSFT", "SPY"])
            try:
                import yfinance as _yf
                raw = _yf.download(watchlist[:5], period="1mo", progress=False, auto_adjust=True)
                closes = raw["Close"] if hasattr(raw.columns, "levels") else raw
                export_data = {"价格历史": closes.reset_index().to_dict("records")}
            except Exception:
                export_data = {"示例数据": [{"symbol": s, "note": "需 yfinance"} for s in watchlist]}
            p = {"data": export_data, "filename": fname}
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '生成 Excel', 'Building Excel')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, export_to_excel, p)
            else:
                r = export_to_excel(p)
            if r.get("success"):
                msg = f"✓ 已导出: {r['path']}  ({r['total_rows']} 行)"
                if self.context.has_rich:
                    self.context.console.print(f"[green]{msg}[/green]")
                else:
                    print(msg)
            else:
                if self.context.has_rich:
                    self.context.console.print(f"[red]{r.get('error')}[/red]")

        elif sub == "load":
            csv_path = rest.strip()
            if not csv_path:
                if self.context.has_rich:
                    self.context.console.print("[dim]用法: /data load <csv文件路径>[/dim]")
                return
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '加载 CSV', 'Loading CSV')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, load_csv_data, {"path": csv_path})
            else:
                r = load_csv_data({"path": csv_path})
            if r.get("success"):
                if self.context.has_rich:
                    self.context.console.print(f"[green]✓ 已加载 {r['rows']} 行 → 表 {r['table_name']}[/green]")
                    self.context.console.print(f"[dim]列: {', '.join(r['columns'][:10])}[/dim]")
                    self.context.console.print(f"[dim]现在可以: /data sql \"SELECT * FROM {r['table_name']} LIMIT 10\"[/dim]")
            else:
                if self.context.has_rich:
                    self.context.console.print(f"[red]{r.get('error')}[/red]")

        elif sub == "tables":
            r = sql_list_tables()
            if r.get("success"):
                tables = r.get("tables", [])
                if self.context.has_rich:
                    if tables:
                        self.context.console.print(f"[bold]已加载表:[/bold] {', '.join(tables)}")
                    else:
                        self.context.console.print("[dim]暂无已加载的表。使用 /data load <csv> 加载数据[/dim]")

        else:
            if self.context.has_rich:
                self.context.console.print("[dim]用法: /data [sql|export|load|tables][/dim]")
                self.context.console.print("[dim]  /data sql \"SELECT * FROM my_table LIMIT 10\"[/dim]")
                self.context.console.print("[dim]  /data load ~/Desktop/data.csv[/dim]")
                self.context.console.print("[dim]  /data export my_report.xlsx[/dim]")
                self.context.console.print("[dim]  /data tables[/dim]")

    async def cmd_alert(self, args: str):
        """
        /alert add AAPL gt 200     — 设置预警（gt/lt/cross_up/cross_down）
        /alert list                 — 列出所有预警
        /alert delete <id>          — 删除预警
        /alert check                — 检查所有预警状态
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().split() if args.strip() else []
        sub = parts[0].lower() if parts else "list"

        try:
            from aria_code.tools.data_analysis_tools import (add_price_alert, list_price_alerts,
                                              delete_price_alert, check_alerts)
        except ImportError as e:
            if self.context.has_rich:
                self.context.console.print(f"[red]data_analysis_tools 未加载: {e}[/red]")
            return

        if sub == "add":
            if len(parts) < 4:
                if self.context.has_rich:
                    self.context.console.print("[dim]用法: /alert add <symbol> <gt|lt|cross_up|cross_down> <price> [备注][/dim]")
                return
            sym = parts[1].upper()
            cond = parts[2].lower()
            try:
                price = float(parts[3])
            except ValueError:
                if self.context.has_rich:
                    self.context.console.print("[red]价格必须是数字[/red]")
                return
            note = " ".join(parts[4:]) if len(parts) > 4 else ""
            r = add_price_alert({"symbol": sym, "condition": cond, "price": price, "note": note})
            if r.get("success"):
                msg = r.get("message", "预警已设置")
                if self.context.has_rich:
                    self.context.console.print(f"[green]✓ {msg}[/green]")
                else:
                    print(f"✓ {msg}")
            else:
                if self.context.has_rich:
                    self.context.console.print(f"[red]{r.get('error')}[/red]")

        elif sub == "list":
            r = list_price_alerts()
            _render_alerts(r)

        elif sub in ("delete", "del", "remove"):
            alert_id = parts[1] if len(parts) > 1 else ""
            if not alert_id:
                if self.context.has_rich:
                    self.context.console.print("[dim]用法: /alert delete <预警ID>[/dim]")
                return
            r = delete_price_alert({"alert_id": alert_id})
            if r.get("success"):
                if self.context.has_rich:
                    self.context.console.print(f"[green]✓ 已删除预警 {r['deleted_id']}[/green]")
            else:
                if self.context.has_rich:
                    self.context.console.print(f"[red]{r.get('error')}[/red]")

        elif sub == "check":
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{ui_text(self, '检查价格预警', 'Checking price alerts')}...[/dim]", spinner="dots"):
                    r = await loop.run_in_executor(None, check_alerts)
            else:
                r = check_alerts()
            triggered = r.get("triggered", [])
            if triggered:
                if self.context.has_rich:
                    self.context.console.print(f"[bold yellow]🔔 {len(triggered)} 个预警已触发![/bold yellow]")
                    for a in triggered:
                        self.context.console.print(f"  [yellow]{a['symbol']}[/yellow] {a.get('condition','')} "
                                      f"{a['price']} → 当前 [bold]{a.get('triggered_price','')}[/bold]")
            else:
                msg = r.get("message", "暂无触发的预警")
                if self.context.has_rich:
                    self.context.console.print(f"[dim]{msg}[/dim]")

        else:
            if self.context.has_rich:
                self.context.console.print("[dim]用法: /alert [add|list|delete|check][/dim]")

    async def cmd_corr(self, args: str):
        """/corr AAPL MSFT TSLA SPY [1y|2y|6mo]  — 计算相关性矩阵"""
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().upper().split() if args.strip() else []

        period = "1y"
        if parts and parts[-1].lower() in ("1y", "2y", "3y", "6mo", "ytd", "5y"):
            period = parts[-1].lower()
            parts = parts[:-1]

        symbols = parts if parts else ["AAPL", "MSFT", "TSLA", "SPY", "QQQ"]

        try:
            from aria_code.tools.data_analysis_tools import calc_correlation_matrix
        except ImportError as e:
            if self.context.has_rich:
                self.context.console.print(f"[red]data_analysis_tools 未加载: {e}[/red]")
            return

        if self.context.has_rich:
            _names = ', '.join(symbols)
            with self.context.console.status(f"[dim]{ui_text(self, f'计算 {_names} 相关性矩阵', f'Computing the {_names} correlation matrix')}...[/dim]", spinner="dots"):
                r = await loop.run_in_executor(None, calc_correlation_matrix,
                                               {"symbols": symbols, "period": period})
        else:
            r = calc_correlation_matrix({"symbols": symbols, "period": period})
        _render_corr_matrix(r)

    async def cmd_portfolio_bt(self, args: str):
        """/ptbt AAPL MSFT GOOG [0.4 0.3 0.3] [2y] [monthly]  — 多资产组合回测"""
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().split() if args.strip() else []

        try:
            from aria_code.tools.data_analysis_tools import portfolio_backtest
        except ImportError as e:
            if self.context.has_rich:
                self.context.console.print(f"[red]data_analysis_tools 未加载: {e}[/red]")
            return

        symbols, weights, period, rebalance = [], [], "2y", "monthly"
        _PERIODS = {"1y", "2y", "3y", "5y", "6mo", "ytd", "max"}
        _REBALANCE = {"monthly", "quarterly", "none"}
        for p in parts:
            pl = p.lower()
            if pl in _PERIODS:
                period = pl
                continue
            if pl in _REBALANCE:
                rebalance = pl
                continue
            try:
                f = float(p)
                if f < 2:
                    weights.append(f)
                else:
                    symbols.append(p.upper())
            except ValueError:
                symbols.append(p.upper())

        if not symbols:
            symbols = ["AAPL", "MSFT", "GOOGL", "SPY"]
            if self.context.has_rich:
                self.context.console.print(f"[dim]未指定标的，使用默认: {symbols}[/dim]")

        p_params = {"symbols": symbols, "period": period, "rebalance": rebalance}
        if weights:
            p_params["weights"] = weights

        if self.context.has_rich:
            _names = ', '.join(symbols)
            with self.context.console.status(f"[dim]{ui_text(self, f'回测 {_names} ({period})', f'Backtesting {_names} ({period})')}...[/dim]", spinner="dots"):
                r = await loop.run_in_executor(None, portfolio_backtest, p_params)
        else:
            r = portfolio_backtest(p_params)
        _render_portfolio_bt(r)

    async def cmd_peer(self, args: str):
        """/peer <symbol> [peer1 peer2 ...]  — 同行估值对比"""
        parts = args.strip().upper().split() if args.strip() else []
        symbol = parts[0] if parts else "AAPL"
        peers = parts[1:] if len(parts) > 1 else []

        if not _get__HAS_LOCAL_FINANCE():
            if self.context.has_rich:
                self.context.console.print("[red]local_finance_tools 未加载[/red]")
            return

        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{ui_text(self, f'获取 {symbol} 同行数据', f'Fetching {symbol} peer data')}...[/dim]", spinner="dots"):
                from local_finance_tools import _peer_comparison
                r = await loop.run_in_executor(None, _peer_comparison,
                                               {"symbol": symbol, "peers": peers, "lang": ui_text(self, "zh", "en")})
        else:
            from local_finance_tools import _peer_comparison
            r = _peer_comparison({"symbol": symbol, "peers": peers, "lang": ui_text(self, "zh", "en")})

        _render_peer_comparison(r)

    async def cmd_compare(self, args: str):
        """/compare SYMBOL [start] [end] — several strategies on one symbol, against buy-and-hold.

        /compare AAPL MSFT read MSFT as the start date, and the local fallback
        ran four of its five strategies as buy-and-hold (the local engine had
        no such names and fell through to it), so the table showed four
        identical rows. Its "start → end" title was the dates asked for, not
        the ones used, and the Buy & Hold row was a total return in the
        annual-return column with a made-up trade count.
        """
        import re as _re

        parts = args.split() if args and args.strip() else ["SPY"]
        symbol = parts[0].upper()
        rest = parts[1:]
        def is_date(token: str) -> bool:
            # A real date: 2024-13-01 has the shape and reached yfinance, whose
            # failure was then explained as rate limiting.
            try:
                __import__("datetime").date.fromisoformat(token)
                return len(token) == 10
            except ValueError:
                return False
        if rest and not is_date(rest[0]):
            # A symbol has a letter (AAPL, 0700.HK) or is a six-digit A-share code.
            if all(_re.fullmatch(r"(?=.*[A-Za-z])[A-Za-z0-9.^=-]{1,12}|\d{6}", t) for t in rest):
                # Two or more symbols: the user is comparing stocks, which is /peer.
                peers = " ".join(t.upper() for t in rest)
                note = f"→ /peer {symbol} {peers}"
                self.context.console.print(f"  [dim]{note}[/dim]") if self.context.has_rich else print(f"  {note}")
                await self.cmd_peer(f"{symbol} {peers}")
                return
            print_error(self.context, ui_text(self, f"无法识别的日期：{rest[0]}", f"Not a date: {rest[0]}"),
                        "Usage: /compare SYMBOL [YYYY-MM-DD] [YYYY-MM-DD]")
            return
        if len(rest) > 1 and not is_date(rest[1]):
            print_error(self.context, ui_text(self, f"无法识别的日期：{rest[1]}", f"Not a date: {rest[1]}"),
                        "Usage: /compare SYMBOL [YYYY-MM-DD] [YYYY-MM-DD]")
            return
        start = rest[0] if rest else "2020-01-01"
        end = rest[1] if len(rest) > 1 else __import__("datetime").date.today().isoformat()
        api_url = self.terminal.config.get("api_url", "http://localhost:8000")
        import aiohttp

        _STRATS = ["momentum", "mean_reversion", "breakout", "turtle", "ma_crossover"]
        # What the local engine can run, under the names /compare shows.
        _LOCAL = {"momentum": "momentum", "mean_reversion": "rsi_mean_revert", "ma_crossover": "sma_cross"}

        async def _do():
            payload = {"symbol": symbol, "strategies": _STRATS,
                       "start_date": start, "end_date": end, "initial_capital": 100000, "commission_rate": 0.0003}
            async with aiohttp.ClientSession(trust_env=True) as sess:
                async with sess.post(f"{api_url}/api/v1/backtest/compare-strategies", json=payload, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    body = await resp.json()
                    return body.get("data", body)

        def _row(name: str, d: dict) -> dict:
            ann = float(d.get("annual_return", 0) or 0)
            mdd = float(d.get("max_drawdown", 0) or 0)
            return {
                "name": name,
                "annualized_return_pct": ann * 100,
                "sharpe_ratio": float(d.get("sharpe_ratio", 0) or 0),
                "max_drawdown_pct": mdd * 100,
                "calmar_ratio": (ann / abs(mdd)) if mdd else 0.0,
                "sortino_ratio": float(d.get("sortino_ratio", 0) or 0),
                "win_rate_pct": float(d.get("win_rate", 0) or 0) * 100,
                "n_trades": int(d.get("total_trades", 0) or 0),
            }

        def _do_local():
            """The strategies the local engine has, over the same dates, and
            buy-and-hold run through the same engine as the benchmark."""
            run = _get_LOCAL_TOOLS()["backtest_strategy"][0]
            rows, period, errors = [], None, []
            for shown, local in _LOCAL.items():
                try:
                    res = run({"symbol": symbol, "strategy": local, "start": start, "end": end})
                except Exception as exc:
                    errors.append(f"{shown}: {exc}")
                    continue
                if not res.get("success"):
                    errors.append(f"{shown}: {res.get('error', 'failed')}")
                    continue
                d = res.get("data", res)
                period = period or (d.get("start"), d.get("end"))
                rows.append(_row(shown, d))
            rows.sort(key=lambda r: r["sharpe_ratio"], reverse=True)
            for i, r in enumerate(rows, 1):
                r["rank_by_sharpe"] = i
            bench = {}
            try:
                b = run({"symbol": symbol, "strategy": "buy_hold", "start": start, "end": end})
                if b.get("success"):
                    bench = _row("buy_hold", b.get("data", b))
            except Exception:
                pass
            return {"strategies": rows, "benchmark": bench, "provider": "local", "period": period,
                    "skipped": [s for s in _STRATS if s not in _LOCAL], "errors": errors}

        async def _do_with_fallback():
            try:
                return await _do()
            except Exception:
                # Backend unavailable / missing endpoint → local engine
                note = ui_text(self, "后端不可用，使用本地回测引擎对比…",
                               "Backend unavailable; comparing with the local backtest engine…")
                if self.context.has_rich:
                    self.context.console.print(f"  [dim]{note}[/dim]")
                import asyncio as _aio
                return await _aio.get_event_loop().run_in_executor(None, _do_local)

        status = ui_text(self, f"正在对比 {symbol} 的策略", f"Comparing strategies on {symbol}")
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{status}...[/dim]", spinner="dots"):
                try:
                    data = await _do_with_fallback()
                except Exception as e:
                    print_error(self.context, str(e), "tool")
                    return
        else:
            print(f"{status}...")
            try:
                data = await _do_with_fallback()
            except Exception as e:
                print_error(self.context, str(e), "tool")
                return
        if not data.get("strategies"):
            detail = "; ".join(data.get("errors") or []) or ui_text(
                self, "检查标的代码和日期是否正确", "check the symbol and the dates")
            print_error(self.context, ui_text(self, "策略对比无结果", "No strategy results"), detail)
            return

        strategies = data.get("strategies", [])
        bh = data.get("benchmark", {}) or {}
        period = data.get("period") or (start, end)
        if self.context.has_rich:
            from rich.table import Table
            tbl = Table(title=f"[bold]{symbol} Strategy Comparison[/bold]  {period[0]} → {period[1]}",
                        show_header=True, header_style="bold")
            for col in ["Rank", "Strategy", "Ann.Ret%", "Sharpe", "MaxDD%", "Calmar", "Sortino", "Win%", "Trades"]:
                tbl.add_column(col, justify="right")
            for s in strategies:
                tbl.add_row(
                    str(s.get("rank_by_sharpe", "")),
                    s["name"],
                    f"{s.get('annualized_return_pct',0):+.1f}%",
                    f"{s.get('sharpe_ratio',0):.3f}",
                    f"{s.get('max_drawdown_pct',0):.1f}%",
                    f"{s.get('calmar_ratio',0):.2f}",
                    f"{s.get('sortino_ratio',0):.2f}",
                    f"{s.get('win_rate_pct',0):.0f}%",
                    str(s.get("n_trades",0)),
                )
            if bh:
                tbl.add_row("—", "[dim]Buy & Hold[/dim]",
                            f"{bh.get('annualized_return_pct',0):+.1f}%",
                            f"{bh.get('sharpe_ratio',0):.3f}",
                            f"{bh.get('max_drawdown_pct',0):.1f}%",
                            f"{bh['calmar_ratio']:.2f}" if "calmar_ratio" in bh else "—",
                            f"{bh['sortino_ratio']:.2f}" if "sortino_ratio" in bh else "—",
                            "—", str(bh["n_trades"]) if "n_trades" in bh else "—")
            self.context.console.print(tbl)
            if data.get("skipped"):
                self.context.console.print("  [dim]" + ui_text(
                    self, f"本地引擎没有 {', '.join(data['skipped'])}，需后端才能对比。",
                    f"{', '.join(data['skipped'])} need the backend; the local engine does not have them.")
                    + "[/dim]")
        else:
            for s in strategies:
                print(f"{s['name']}: Ann={s.get('annualized_return_pct',0):+.1f}% Sharpe={s.get('sharpe_ratio',0):.2f} DD={s.get('max_drawdown_pct',0):.1f}%")
