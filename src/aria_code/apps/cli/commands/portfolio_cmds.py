"""
PortfolioCommandsMixin — Portfolio commands: journal, report, portfolio, apply_plan, team.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shlex
import sys
import time
from typing import Any, Dict, Optional

from aria_code.apps.cli.i18n import ui_text
from aria_code.apps.cli.tool_executor import execute_aria_tool
from aria_code.ui.render.output import file_uri as _file_uri

from ._ui import Panel, print_error

logger = logging.getLogger(__name__)


def export_report_pdf(*args, **kwargs):
    from aria_cli import export_report_pdf as fn
    return fn(*args, **kwargs)


def save_markdown_report(*args, **kwargs):
    from aria_cli import save_markdown_report as fn
    return fn(*args, **kwargs)


def all_agents_failed(*args, **kwargs):
    from aria_cli import all_agents_failed as fn
    return fn(*args, **kwargs)


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


def update_report_index(*args, **kwargs):
    from aria_cli import update_report_index as fn
    return fn(*args, **kwargs)


def build_markdown_report_prompt(*args, **kwargs):
    from aria_cli import build_markdown_report_prompt as fn
    return fn(*args, **kwargs)


def parse_report_args(*args, **kwargs):
    from aria_cli import parse_report_args as fn
    return fn(*args, **kwargs)


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
        """Portfolio analysis moved to financial-service MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/portfolio 已迁移至 financial-service MCP。[/dim]")

    def cmd_apply_plan(self, args: str):
        """Execute pending plan steps sequentially (native Rust CLI also handles /plan and /apply-plan)."""
        plan = list(getattr(self.terminal, "pending_plan", []) or [])
        if not plan:
            msg = "No pending plan. Use /plan first."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        from aria_code.safety import SafetyService
        policy = self.terminal.config.get("command_policy", "safe")
        results = []
        for idx, step in enumerate(plan, 1):
            decision = SafetyService(self.terminal.config).evaluate_command(step, policy)
            if decision.risk == "high" and not self._confirm_high_risk_command(
                decision.normalized_command, decision.risk, policy
            ):
                self.terminal.pending_plan = plan[idx - 1:]
                self.terminal.last_plan_results = results
                return
            res = _tool_run_command({"command": step, "policy": policy})
            exit_code = res.get("data", {}).get("exit_code") if res.get("success") else None
            ok = bool(res.get("success") and exit_code == 0)
            results.append({"step": step, "status": "completed" if ok else "failed", "exit_code": exit_code})
            if not ok:
                self.terminal.pending_plan = plan[idx - 1:]
                self.terminal.last_plan_results = results
                err = res.get("error") or f"Command exited {exit_code}"
                msg = f"Plan failed at step {idx}: {step} ({err})"
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return
        self.terminal.last_plan_results = results
        self.terminal.pending_plan = []
        msg = f"Plan completed ({len(plan)} steps)"
        self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)

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
