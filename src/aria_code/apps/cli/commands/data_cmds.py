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
        """/data（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/data 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_alert(self, args: str):
        """/alert（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/alert 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_corr(self, args: str):
        """/corr（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/corr 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_portfolio_bt(self, args: str):
        """/portfolio-bt（已迁移至 Arthera 后端服务）."""
        if self.context.has_rich:
            self.context.console.print("[dim]/portfolio-bt 已迁移至 Arthera 后端服务。[/dim]")

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
