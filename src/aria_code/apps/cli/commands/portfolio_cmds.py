"""
PortfolioCommandsMixin — Portfolio commands: journal, report, portfolio, apply_plan, team.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""

from __future__ import annotations

from aria_code.ui.render.output import file_uri as _file_uri

from aria_code.apps.cli.i18n import ui_text

from ._ui import Panel, print_error


def _detect_lang_for_team(text: str) -> str:
    if not text:
        return "zh"
    zh_chars = sum(1 for c in text if '一' <= c <= '鿿')
    return "zh" if zh_chars / max(len(text), 1) > 0.15 else "en"


import logging
import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def parse_team_args(*args, **kwargs):
    from aria_cli import parse_team_args as fn
    return fn(*args, **kwargs)
def resolve_team_symbols(*args, **kwargs):
    from aria_cli import resolve_team_symbols as fn
    return fn(*args, **kwargs)
def team_agent_names(*args, **kwargs):
    from aria_cli import team_agent_names as fn
    return fn(*args, **kwargs)
# execute_aria_tool lives in apps/cli/tool_executor.py and needs nothing from
# aria_cli — the cache it reads is owned there now, so the hop was pointless.
from aria_code.apps.cli.tool_executor import execute_aria_tool
def export_report_pdf(*args, **kwargs):
    from aria_cli import export_report_pdf as fn
    return fn(*args, **kwargs)
def run_team_analysis(*args, **kwargs):
    from aria_cli import run_team_analysis as fn
    return fn(*args, **kwargs)
def save_markdown_report(*args, **kwargs):
    from aria_cli import save_markdown_report as fn
    return fn(*args, **kwargs)
def all_agents_failed(*args, **kwargs):
    from aria_cli import all_agents_failed as fn
    return fn(*args, **kwargs)
def _get_Panel():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return Panel
def save_team_report(*args, **kwargs):
    from aria_cli import save_team_report as fn
    return fn(*args, **kwargs)
def _print_verdict_banner(*args, **kwargs):
    from aria_cli import _print_verdict_banner as fn
    return fn(*args, **kwargs)
def _get_mdc(*args, **kwargs):
    from .._optional import get_mdc as fn
    return fn(*args, **kwargs)
def report_agent_names(*args, **kwargs):
    from aria_cli import report_agent_names as fn
    return fn(*args, **kwargs)
def _tool_run_command(*args, **kwargs):
    from aria_cli import _tool_run_command as fn
    return fn(*args, **kwargs)
def run_deep_cli(*args, **kwargs):
    from aria_cli import run_deep_cli as fn
    return fn(*args, **kwargs)
def report_agent_health(*args, **kwargs):
    from aria_cli import report_agent_health as fn
    return fn(*args, **kwargs)
def report_file_size_kb(*args, **kwargs):
    from aria_cli import report_file_size_kb as fn
    return fn(*args, **kwargs)
def generate_html_report(*args, **kwargs):
    from aria_cli import generate_html_report as fn
    return fn(*args, **kwargs)
def _get__HAS_MDC():
    from .._optional import HAS_MDC as val
    return val
def _sanitize_team_result_with_market_data(*args, **kwargs):
    from aria_cli import _sanitize_team_result_with_market_data as fn
    return fn(*args, **kwargs)
def update_report_index(*args, **kwargs):
    from aria_cli import update_report_index as fn
    return fn(*args, **kwargs)
def build_markdown_report_prompt(*args, **kwargs):
    from aria_cli import build_markdown_report_prompt as fn
    return fn(*args, **kwargs)
def parse_report_args(*args, **kwargs):
    from aria_cli import parse_report_args as fn
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

# A real logger. This was a `def logger(*args, **kwargs)` forwarding to
# aria_cli, so every `logger.debug(...)` raised AttributeError — and all but
# one of them sit in an except block, where the failure replaced whatever
# error was being reported.
logger = logging.getLogger(__name__)


class PortfolioCommandsMixin:
    """Mixin: Portfolio commands: journal, report, portfolio, apply_plan, team."""

    async def cmd_journal(self, args: str):
        """Portfolio ledger moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/journal 已迁移至 Arthera 后端服务。[/dim]")

    async def cmd_report(self, args: str):
        """生成综合投资报告（图表 + 多 Agent 分析 → HTML / Markdown 文件）。

        Usage:
            /report AAPL
            /report 000333
            /report AAPL --format md      # Markdown 投研报告（离线可用）
            /report AAPL --type deep      # 深度研报（8页）
            /report AAPL --type brief     # 简评（1页）
            /report AAPL --pdf            # 同时导出 PDF（自动检测 Chrome/Edge，macOS 零安装）
        """
        from datetime import datetime as _dt

        report_args = parse_report_args(args)
        symbol = report_args.symbol
        fmt = report_args.fmt
        report_type = report_args.report_type
        export_pdf_flag = report_args.export_pdf
        out_dir = report_args.output_dir
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime("%Y%m%d_%H%M")

        # ── Markdown report mode (works fully offline) ────────────────────────
        if fmt in ("md", "markdown"):
            _gen = ui_text(self, f"生成 {symbol} Markdown 投研报告 ({report_type})", f"Writing the {symbol} Markdown report ({report_type})")
            self.context.console.print(f"\n  [dim]{_gen}…[/dim]") if self.context.has_rich else print(f"\n  {_gen}…")

            # Fetch real data through the service boundary so provenance and
            # quality metadata travel with the report prompt and artifact.
            mdc_data = {}
            data_bundle = None
            data_quality = {}
            try:
                from packages.aria_services.data import DataService as _ReportDataService
                data_bundle = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _ReportDataService().bundle(symbol, history_days=370, technical_days=120),
                )
                quote = data_bundle.quote or {}
                technical = data_bundle.technical or {}
                mdc_data = {**quote, **technical}
                data_quality = data_bundle.quality or {}
            except Exception as _ds_exc:
                logger.debug("report markdown data service failed: %s", _ds_exc)
                if _get__HAS_MDC():
                    try:
                        mdc = _get_mdc()
                        q = mdc.quote(symbol)
                        ti = mdc.technical_indicators(symbol, days=120)
                        mdc_data = {**q, **ti}
                        data_quality = {
                            "status": "partial",
                            "stale": False,
                            "providers": mdc_data.get("provider_chain") or list(dict.fromkeys(
                                str(v) for v in [mdc_data.get("provider"), mdc_data.get("source")] if v
                            )),
                            "warnings": [f"data service unavailable: {_ds_exc}"],
                        }
                    except Exception:
                        data_quality = {"status": "data_unavailable", "warnings": [str(_ds_exc)]}

            ai_prompt = build_markdown_report_prompt(
                symbol=symbol,
                report_type=report_type,
                market_data=mdc_data,
                data_quality=data_quality,
                data_bundle=data_bundle,
                now=_dt.now(),
                lang=ui_text(self, "zh", "en"),
            )

            await self.terminal.send_message(ai_prompt)

            # Extract last AI response and save as markdown
            last_ai = next(
                (m["content"] for m in reversed(self.terminal.conversation)
                 if m.get("role") == "assistant"), ""
            )
            if last_ai:
                saved = save_markdown_report(
                    symbol=symbol,
                    report_type=report_type,
                    markdown_text=last_ai,
                    timestamp=ts,
                    output_dir=out_dir,
                    market_data=mdc_data,
                    data_quality=data_quality,
                    data_bundle=data_bundle,
                    created_at=_dt.now(),
                )
                out_f = saved.path
                # A short, clickable path: the full temp-dir path wrapped over
                # three lines, and the "预览: open …" line repeated it.
                from aria_code.ui.render.output import display_path as _short_path
                _saved = ui_text(self, "报告已保存", "Report saved")
                if self.context.has_rich:
                    self.context.console.print(f"\n  [green]✓[/green] {_saved}: "
                                               f"[link={_file_uri(str(out_f))}]{_short_path(str(out_f))}[/link]\n")
                else:
                    print(f"\n  {_saved}: {out_f}")

                # ── Markdown 报告 → 排版 PDF（--pdf；中英文模板自动检测）──
                if export_pdf_flag:
                    from pathlib import Path as _Path  # mixin 重绑定后无模块级 Path
                    try:
                        from aria_code.markdown_pdf import markdown_to_pdf as _md_to_pdf
                        _pdf_out = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: _md_to_pdf(_Path(out_f),
                                               _Path(out_f).with_suffix(".pdf")),
                        )
                        if _pdf_out:
                            if self.context.has_rich:
                                self.context.console.print(
                                    f"  [green]PDF 导出成功[/green]  "
                                    f"[link={_file_uri(_pdf_out)}]{_pdf_out.name}[/link]"
                                )
                            else:
                                print(f"  PDF: {_pdf_out}")
                            import subprocess as _subp_md
                            try:
                                _subp_md.Popen(["open", str(_pdf_out)])
                            except Exception:
                                pass
                        else:
                            _msg = "PDF 导出失败：安装 Chrome/Edge，或 pip install pyobjc-framework-WebKit"
                            self.context.console.print(f"  [yellow]{_msg}[/yellow]") if self.context.has_rich else print(f"  {_msg}")
                    except Exception as _pdf_exc:
                        logger.debug("[report] md pdf export error: %s", _pdf_exc)
            return

        # ── HTML 研报（Bloomberg 暗色主题）────────────────────────────────
        if self.context.has_rich:
            self.context.console.print(f"\n  [dim]正在生成 [bold]{symbol}[/bold] 专业研报（数据清洗 + 图表 + Agent 分析）…[/dim]")
        else:
            print(f"\n  正在生成 {symbol} 研报…")

        _agent_names_for_report = report_agent_names(report_type)
        def _report_agent_done(name, result):
            success = bool(getattr(result, "success", False))
            degraded = bool(getattr(result, "degraded", False))
            if success:
                icon = "≈" if degraded else "✓"
                detail = "降级结果" if degraded else "完成"
            else:
                icon = "✗"
                error = str(getattr(result, "error", "") or "失败")
                detail = "超时" if error == "timeout" else error[:60]
            if self.context.has_rich:
                color = "yellow" if degraded else "green" if success else "red"
                self.context.console.print(f"  [{color}]{icon}[/{color}] {name}  [dim]{detail}[/dim]")
            else:
                print(f"  {icon} {name}  {detail}")

        def _report_synthesis_start(results):
            succeeded = sum(1 for result in results if getattr(result, "success", False))
            total = len(results)
            message = f"Agent 阶段完成 {succeeded}/{total}，正在整理报告…"
            self.context.console.print(f"  [dim]{message}[/dim]") if self.context.has_rich else print(f"  {message}")

        try:
            if self.context.has_rich:
                self.context.console.print(f"  [dim]{len(_agent_names_for_report)} agents 并行分析…[/dim]")
                _html_report = await generate_html_report(
                    symbol=symbol,
                    report_type=report_type,
                    output_dir=out_dir,
                    config=self.terminal.config,
                    on_agent_done=_report_agent_done,
                    on_synthesis_start=_report_synthesis_start,
                )
            else:
                _html_report = await generate_html_report(
                    symbol=symbol,
                    report_type=report_type,
                    output_dir=out_dir,
                    config=self.terminal.config,
                    on_agent_done=_report_agent_done,
                    on_synthesis_start=_report_synthesis_start,
                )
            out_f = _html_report.path
            _team_result = _html_report.team_result
        except Exception as e:
            if self.context.has_rich:
                self.context.console.print(f"  [red]研报生成失败: {e}[/red]")
            else:
                print(f"  研报生成失败: {e}")
            return

        if not out_f:
            self.context.console.print("  [red]研报生成失败（无输出文件）[/red]") if self.context.has_rich else print("  研报生成失败")
            return

        path = str(out_f)
        from ui.render.output import display_path as _display_path
        path_label = _display_path(out_f, fallback="report")
        _file_kb = report_file_size_kb(out_f)
        # Check if all agents failed — show warning instead of false success
        _agent_health = _html_report.agent_health or report_agent_health(
            _team_result, _agent_names_for_report
        )
        _all_agents_failed = all_agents_failed(_team_result) or bool(
            _agent_health.get("expected") and not _agent_health.get("succeeded")
        )
        _agents_partial = bool(
            _agent_health.get("failed") and _agent_health.get("succeeded")
        )
        if self.context.has_rich:
            if _all_agents_failed:
                self.context.console.print(
                    f"\n  [yellow]⚠ 研报已保存（所有 Agent 分析失败，内容仅含基础数据）[/yellow]"
                    f"  [dim]{out_f.name}  ({_file_kb}KB)[/dim]"
                )
            elif _agents_partial:
                self.context.console.print(
                    f"\n  [yellow]⚠ 研报已保存（Agent 部分完成 "
                    f"{_agent_health['succeeded']}/{_agent_health['expected']}，结论置信度已降级）[/yellow]"
                    f"  [dim]{out_f.name}  ({_file_kb}KB)[/dim]"
                )
            else:
                self.context.console.print(
                    f"\n  [green]✅ 研报已保存[/green]"
                    f"  [link={_file_uri(path)}]{path_label}[/link]"
                    f"  [dim]({_file_kb}KB)[/dim]"
                )
            self.context.console.print(f"  [dim]文件: {path_label}[/dim]")
            if _team_result:
                _health_suffix = (
                    f" · agents {_agent_health['succeeded']}/{_agent_health['expected']}"
                    if _agent_health.get("failed") else ""
                )
                _print_verdict_banner(
                    _team_result.final_signal,
                    subtitle=f"耗时 {_team_result.elapsed_sec:.1f}s{_health_suffix}",
                    confidence=_team_result.confidence,
                )
        else:
            _pfx = "⚠ 研报已保存（Agent 全部失败）" if _all_agents_failed else "✅ 研报已保存"
            print(f"\n  {_pfx}: {path_label}  ({_file_kb}KB)")

        # ── PDF 导出 ──────────────────────────────────────────────────────────
        if export_pdf_flag:
            try:
                if self.context.has_rich:
                    with self.context.console.status(f"[dim]{ui_text(self, '导出 PDF', 'Exporting PDF')}…[/dim]", spinner="dots"):
                        _pdf_path = await export_report_pdf(out_f)
                else:
                    _pdf_path = await export_report_pdf(out_f)
                if _pdf_path:
                    _pdf_kb = report_file_size_kb(_pdf_path)
                    if self.context.has_rich:
                        self.context.console.print(
                            f"  [green]PDF 导出成功[/green]"
                            f"  [link={_file_uri(_pdf_path)}]{_pdf_path.name}[/link]"
                            f"  [dim]({_pdf_kb}KB)[/dim]"
                        )
                    else:
                        print(f"  PDF: {_pdf_path}  ({_pdf_kb}KB)")
                    import subprocess as _subp2
                    try:
                        _subp2.Popen(["open", str(_pdf_path)])
                    except Exception:
                        pass
                else:
                    _hint = "安装 Chrome/Edge 即可（自动检测），或 pip install weasyprint / brew install wkhtmltopdf"
                    if self.context.has_rich:
                        self.context.console.print(
                            f"  [yellow]PDF 导出失败[/yellow]  "
                            f"[dim]请安装: {_hint}  或在浏览器按 Cmd+P → 存储为 PDF[/dim]"
                        )
                    else:
                        print(f"  PDF 导出失败，请安装: {_hint}")
            except Exception as _e:
                logger.debug("[report] pdf export error: %s", _e)

        # ── 更新研报索引 ──────────────────────────────────────────────────────
        try:
            _idx = await update_report_index(out_f.parent)
            if _idx and self.context.has_rich:
                self.context.console.print(
                    f"  [dim]索引已更新: [link={_file_uri(_idx)}]{_idx.name}[/link][/dim]"
                )
        except Exception as _e:
            logger.debug("[report] index update error: %s", _e)

        import subprocess as _subp
        try:
            _subp.Popen(["open", path])
        except Exception:
            pass

    async def cmd_portfolio(self, args: str):
        """
        组合级跨标的分析（相关性/分散度/风险）
        Usage:
          /portfolio                    → 分析 watchlist（最多 10 只）
          /portfolio analyze            → 同上
          /portfolio analyze AAPL TSLA MSFT
          /portfolio rebalance          → 生成再平衡建议（同 analyze，着重操作）
        """
        import sys as _sys
        parts      = args.strip().split()
        sub        = parts[0].lower() if parts else "analyze"
        sym_parts  = parts[1:] if parts else []
        rebalance  = (sub == "rebalance")

        # ── holdings: 实盘持仓看板，按来源策略分组 ──────────────────────────
        if sub in ("holdings", "book", "positions"):
            await self._portfolio_holdings_by_strategy()
            return

        # 解析标的：命令行 > 真实持仓账本 > watchlist
        # 之前这里没有命令行参数时永远用 watchlist（等权虚构组合），哪怕
        # portfolio_ledger 里有真实持仓也不会用——risk verdict 算的是一个
        # 假想的等权组合，不是用户真实的仓位暴露。现在优先读真实持仓
        # （按成本加权），ledger 为空才退回 watchlist。
        ledger_weights: Optional[Dict[str, float]] = None
        if sym_parts:
            symbols = [s.strip(",").upper() for s in sym_parts if s.strip(",")]
        else:
            try:
                from aria_code.domain.portfolio_ledger import PortfolioLedger as _PL_default
                positions = _PL_default().get_positions()
            except ImportError:
                positions = []
            if positions:
                symbols = [p["symbol"].upper() for p in positions]
                ledger_weights = {p["symbol"].upper(): p["cost_basis"] for p in positions}
            else:
                symbols = self.terminal.config.get("watchlist", ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"])[:10]

        _lang = "zh" if str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh") else "en"
        _T = (lambda a, b: a) if _lang == "zh" else (lambda a, b: b)

        if not symbols:
            msg = _T("请先设置 watchlist、记录持仓（/journal add buy ...）或指定标的：/portfolio analyze AAPL TSLA MSFT",
                     "Set a watchlist, record positions (/journal add buy ...) or name the symbols: "
                     "/portfolio analyze AAPL TSLA MSFT")
            self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
            return

        # 尝试使用新 PortfolioAgent
        _use_new = False
        try:
            from agents.portfolio_agent import PortfolioAgent as _PA
            _use_new = True
        except ImportError:
            pass

        if _use_new:
            if sym_parts:
                hdr = _T(f"分析组合：{' '.join(symbols)}", f"portfolio of {' '.join(symbols)}")
            elif ledger_weights:
                hdr = _T("分析真实持仓组合（按成本加权）", "your positions, weighted by cost")
            else:
                hdr = _T("分析 watchlist 组合（等权，无真实持仓数据）",
                         "watchlist, equal weight (no positions recorded)")
            if rebalance:
                hdr = _T("再平衡方案：", "Rebalance · ") + hdr
            _sym_label = _T("标的", "Symbols")
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(f"  [bold cyan]━━━ /portfolio {hdr} ━━━[/bold cyan]")
                self.context.console.print(f"  [dim]{_sym_label} ({len(symbols)}): {', '.join(symbols)}[/dim]")
                self.context.console.print()
            else:
                print(f"\n  ━━━ /portfolio ━━━\n  {_sym_label}: {', '.join(symbols)}\n")

            # The model the session is configured for. This took the first
            # local provider, so a Gemini session asked Ollama, which was not
            # running, and printed the connection error as a log line.
            from aria_code.apps.cli.commands.team import build_team_llm_provider
            _llm = build_team_llm_provider(self.terminal.config)

            tokens: list = []
            def _on_tok(t):
                tokens.append(t)
                _sys.stdout.write(t); _sys.stdout.flush()

            try:
                agent  = _PA(llm_provider=_llm, on_token=_on_tok, lang=_lang)
                result = await agent.run_portfolio(symbols, weights=ledger_weights)
                if tokens:
                    print()  # 换行（流式输出后）

                if not result:
                    if self.context.has_rich:
                        self.context.console.print(f"[yellow]  {_T('组合分析返回空结果', 'The portfolio analysis came back empty')}[/yellow]")
                    return

                if not tokens and getattr(agent, "llm_error", None):
                    _note = _T("模型不可用，以下仅为统计结果", "Model unavailable, so this is the statistics only")
                    if self.context.has_rich:
                        self.context.console.print(f"  [dim]{_note}[/dim]")
                    else:
                        print(f"  {_note}")

                # The verdict the analysis reached. Rebuilt from the signal,
                # NEEDS_ATTENTION (signal SELL) came out as HIGH_RISK.
                _port_verdict = str((result.data_used or {}).get("verdict") or "NEEDS_ATTENTION")
                if self.context.has_rich:
                    # The header already ends in a blank line, and the banner's
                    # panel opens with one: two blank lines each side before.
                    if tokens:
                        self.context.console.print()
                    for pt in (result.key_points or []):
                        self.context.console.print(f"  [dim]• {pt}[/dim]")
                    _subtitle = " · ".join(result.key_points[:2]) if result.key_points else ""
                    _print_verdict_banner(_port_verdict, subtitle=_subtitle,
                                          confidence=result.confidence, lang=_lang)
                else:
                    for pt in result.key_points:
                        print(f"  • {pt}")
                    print(f"\n  {_T('置信度', 'Confidence')}: {result.confidence:.0%}  {_port_verdict}")

                if rebalance and self.context.has_rich:
                    self.context.console.print("\n  [dim]" + _T(
                        "提示: 再平衡建议已包含在上方分析中。如需详细方案，可追问 Aria 具体操作步骤。",
                        "The rebalancing suggestions are in the analysis above; ask Aria for the steps.") + "[/dim]")

            except Exception as e:
                msg = _T(f"组合分析失败: {e}", f"Portfolio analysis failed: {e}")
                self.context.console.print(f"  [red]{msg}[/red]") if self.context.has_rich else print(f"  {msg}")
            return

        # 旧路径回退（无新 agents 包时）
        if self.context.has_rich:
            self.context.console.print("[dim]Assessing portfolio risk...[/dim]")
        else:
            print("Assessing portfolio risk...")
        result = await execute_aria_tool(self.terminal.api_url, "assess_portfolio_risk", {
            "symbols": symbols[:10],
        })
        if result.get("success") and result.get("data"):
            if self.context.has_rich:
                self.context.console.print("\n  [bold]Portfolio Risk[/bold]\n")
                self.context.console.print(f"[dim]{json.dumps(result['data'], indent=2, ensure_ascii=False)[:1000]}[/dim]")
            else:
                print(json.dumps(result.get("data", {}), indent=2, ensure_ascii=False))
        else:
            self.context.console.print(f"[dim]No data: {result.get('error', '')}[/dim]" if self.context.has_rich
                          else f"No data: {result.get('error', '')}")

    async def _portfolio_holdings_by_strategy(self):
        """实盘持仓看板：按来源策略分组（策略名匹配交易 reason），含实时浮盈。"""
        try:
            from aria_code.domain.portfolio_ledger import PortfolioLedger
        except ImportError:
            self.context.console.print("[red]portfolio_ledger 模块未找到[/red]") if self.context.has_rich else print("portfolio_ledger 未找到")
            return
        ledger = PortfolioLedger()
        names = []
        try:
            from aria_code.domain.strategy_vault import get_vault
            names = get_vault().list_all_names()
        except Exception:
            pass
        groups = ledger.positions_by_strategy(names)
        if not groups:
            self.context.console.print("[dim]暂无持仓。用 /deploy 或 /journal add 建仓后在此按策略查看。[/dim]"
                          if self.context.has_rich else "暂无持仓。")
            return

        syms = sorted({p["symbol"] for poss in groups.values() for p in poss})
        if self.context.has_rich:
            self.context.console.print(f"  [dim]获取 {len(syms)} 只实时报价…[/dim]")
        live = {}
        try:
            import yfinance as yf
            for s in syms:
                try:
                    h = yf.Ticker(s).history(period="1d")
                    if not h.empty:
                        live[s] = float(h["Close"].iloc[-1])
                except Exception:
                    pass
        except ImportError:
            pass

        g_cost = g_mv = 0.0
        if self.context.has_rich:
            from rich.table import Table
            self.context.console.print()
            self.context.console.print("  [bold cyan]持仓看板 · 按来源策略[/bold cyan]")
            for grp, poss in groups.items():
                tbl = Table(box=None, header_style="bold", pad_edge=False)
                tbl.add_column("标的", width=8)
                tbl.add_column("净持仓", justify="right", width=10)
                tbl.add_column("均价", justify="right", width=10)
                tbl.add_column("现价", justify="right", width=10)
                tbl.add_column("市值", justify="right", width=12)
                tbl.add_column("浮动盈亏", justify="right", width=13)
                tbl.add_column("%", justify="right", width=8)
                sub_cost = sub_mv = 0.0; have = False
                for p in poss:
                    cost = p["cost_basis"]; sub_cost += cost
                    px = live.get(p["symbol"])
                    if px is not None:
                        have = True
                        mv = px * p["net_qty"]; sub_mv += mv
                        pnl = mv - cost; pct = (pnl / cost * 100) if cost else 0.0
                        c = "green" if pnl >= 0 else "red"
                        tbl.add_row(p["symbol"], f"{p['net_qty']:,.4g}", f"{p['avg_cost']:,.2f}",
                                    f"{px:,.2f}", f"{mv:,.0f}", f"[{c}]{pnl:+,.0f}[/{c}]",
                                    f"[{c}]{pct:+.1f}%[/{c}]")
                    else:
                        sub_mv += cost
                        tbl.add_row(p["symbol"], f"{p['net_qty']:,.4g}", f"{p['avg_cost']:,.2f}",
                                    "N/A", f"{cost:,.0f}", "—", "—")
                g_cost += sub_cost; g_mv += sub_mv
                self.context.console.print(f"\n  [bold]{grp}[/bold]  [dim]{len(poss)} 持仓 · 成本 {sub_cost:,.0f}[/dim]")
                self.context.console.print(tbl)
                if have:
                    spnl = sub_mv - sub_cost; sc = "green" if spnl >= 0 else "red"
                    spct = spnl / sub_cost * 100 if sub_cost else 0.0
                    self.context.console.print(f"  [dim]小计 市值 {sub_mv:,.0f} · 浮盈 [{sc}]{spnl:+,.0f} ({spct:+.1f}%)[/{sc}][/dim]")
            gpnl = g_mv - g_cost; gc = "green" if gpnl >= 0 else "red"
            gpct = gpnl / g_cost * 100 if g_cost else 0.0
            self.context.console.print(f"\n  [bold]合计[/bold] 成本 {g_cost:,.0f} · 市值 {g_mv:,.0f} · "
                          f"浮盈 [{gc}]{gpnl:+,.0f} ({gpct:+.1f}%)[/{gc}]")
            self.context.console.print()
        else:
            for grp, poss in groups.items():
                print(f"[{grp}]")
                for p in poss:
                    px = live.get(p["symbol"])
                    pnl = (px * p["net_qty"] - p["cost_basis"]) if px else None
                    print(f"  {p['symbol']} net={p['net_qty']:g} avg={p['avg_cost']:.2f}"
                          + (f" pnl={pnl:+.0f}" if pnl is not None else ""))

    def cmd_apply_plan(self, args: str):
        """Execute the pending command plan sequentially."""
        plan = list(getattr(self.terminal, "pending_plan", []) or [])
        arg_tokens = args.split()
        start_idx = 0
        if "--from" in arg_tokens:
            idx = arg_tokens.index("--from")
            if idx + 1 >= len(arg_tokens):
                msg = "Usage: /apply-plan --from <step_number>"
                self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
                return
            try:
                start_idx = max(0, int(arg_tokens[idx + 1]) - 1)
            except ValueError:
                msg = "Invalid step number for --from"
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return

        if not plan:
            self.context.console.print("[dim]No pending plan. Use /plan first.[/dim]" if self.context.has_rich
                          else "No pending plan. Use /plan first.")
            return
        if start_idx > 0:
            if start_idx >= len(plan):
                msg = f"--from {start_idx + 1} exceeds available steps ({len(plan)})"
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return
            plan = plan[start_idx:]
        if "--resume" in arg_tokens and self.context.has_rich:
            self.context.console.print(f"[dim]Resuming execution from step 1 of remaining {len(plan)} step(s).[/dim]")

        policy = self.terminal.config.get("command_policy", "safe")
        results = []
        failed = None
        for i, step in enumerate(plan, 1):
            started_at = time.time()
            if self.context.has_rich:
                self.context.console.print(f"[dim]Step {i}/{len(plan)}:[/dim] [bold]{step}[/bold]")
            else:
                print(f"Step {i}/{len(plan)}: {step}")

            # Was evaluate_command_policy(step, policy) — which silently
            # evaluated every step as workspace-write with networking on,
            # ignoring the session's permission_mode and network_enabled.
            # Only .risk was read (and risk is mode-independent), so nothing
            # executed that shouldn't have; the service closes the gap before
            # anyone reads .allowed here and gets a wrong answer.
            from aria_code.safety import SafetyService
            step_decision = SafetyService(self.terminal.config).evaluate_command(step, policy)
            if step_decision.risk == "high":
                if not self._confirm_high_risk_command(step_decision.normalized_command, step_decision.risk, policy):
                    failed = (i, step, "Cancelled by user at high-risk step confirmation")
                    results.append({
                        "step": step,
                        "status": "blocked",
                        "duration": round(time.time() - started_at, 3),
                        "exit_code": None,
                        "error": failed[2],
                    })
                    break

            res = _tool_run_command({"command": step, "policy": policy})
            duration = time.time() - started_at
            exit_code = res.get("data", {}).get("exit_code", None) if res.get("success") else None
            status = "completed" if res.get("success") and exit_code == 0 else "failed"
            results.append({
                "step": step,
                "status": status,
                "duration": round(duration, 3),
                "exit_code": exit_code,
                "error": None if status == "completed" else (res.get("error") or f"Command exited {exit_code}"),
            })
            if not res.get("success"):
                failed = (i, step, res.get("error", "Unknown error"))
                break
            exit_code = res.get("data", {}).get("exit_code", 0)
            if exit_code != 0:
                failed = (i, step, f"Command exited {exit_code}")
                break

        self.terminal.last_plan_results = results

        if failed:
            idx, step, err = failed
            self.terminal.pending_plan = plan[idx - 1:]
            if self.context.has_rich:
                self.context.console.print(f"[red]Plan failed at step {idx}[/red]: [bold]{step}[/bold]")
                self.context.console.print(f"[red]{err}[/red]")
                self.context.console.print("[dim]Recovery hints:[/dim]")
                if "blocked by policy" in (err or "").lower():
                    self.context.console.print("  [dim]> /run --dry-run <command> to inspect risk[/dim]")
                    self.context.console.print("  [dim]> /config set command_policy=balanced (or full) if needed[/dim]")
                else:
                    self.context.console.print("  [dim]> Fix code/config, then rerun /apply-plan[/dim]")
                    self.context.console.print("  [dim]> Use /git diff to inspect changes[/dim]")
            else:
                print(f"Plan failed at step {idx}: {step}\n{err}")
                if "blocked by policy" in (err or "").lower():
                    print("Recovery: /run --dry-run <command> and /config set command_policy=balanced")
                else:
                    print("Recovery: fix issue, then rerun /apply-plan")
        else:
            if self.context.has_rich:
                self.context.console.print(f"[green]Plan completed ({len(plan)} steps)[/green]")
                for i, row in enumerate(results, 1):
                    self.context.console.print(f"  [dim]{i}. {row['step']} ({row['duration']}s)[/dim]")
            else:
                print(f"Plan completed ({len(plan)} steps)")
            self.terminal.pending_plan = []

    async def cmd_deep(self, args: str):
        """深度多层研究（已从核心精简包中移除）"""
        msg = "Deep analysis vertical has been removed from the core build."
        self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)

    async def cmd_team(self, args: str):
        """Multi-agent financial research team moved to Arthera backend service."""
        if self.context.has_rich:
            self.context.console.print("[dim]/team 已迁移至 Arthera 后端服务。[/dim]")

    async def _save_team_report(self, symbol: str, team_result, data_bundle=None, quality_notes: Optional[list] = None,
                                lang: str = "zh") -> None:
        """将 /team 分析结果保存为 Markdown 报告"""
        saved = save_team_report(
            symbol=symbol,
            team_result=team_result,
            data_bundle=data_bundle,
            quality_notes=quality_notes,
        )
        try:
            parts = saved.path.parts
            short_path = "/".join(parts[-5:]) if len(parts) > 5 else str(saved.path)
        except Exception:
            short_path = str(saved.path)
        label = "报告已保存" if str(lang).startswith("zh") else "Report saved"
        if self.context.has_rich:
            self.context.console.print(
                f"    [dim]{label}:[/dim] [link={_file_uri(saved.path)}]…/{short_path}[/link]")
        else:
            print(f"    {label}: .../{short_path}")
