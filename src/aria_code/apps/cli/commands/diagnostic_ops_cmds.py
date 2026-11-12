"""DiagnosticOpsCommandsMixin — bug, accuracy, cost, todo, doctor, datasource commands."""

from __future__ import annotations

from ._ui import Panel, print_error, rich_box

import asyncio
import json
import pathlib

# Status glyph + colour per architecture-layer status.
_ARCH_ICON = {
    "done":    ("✓", "#3fb950"),
    "partial": ("◐", "#d29922"),
    "planned": ("○", "dim"),
    "blocked": ("✗", "#f85149"),
}


def format_architecture_report(layers, counts, *, gaps_only: bool = False,
                               rich: bool = True) -> list:
    """Render the architecture contract as display lines (pure / testable).

    ``layers`` are ArchitectureLayer objects; ``counts`` is the status→n map.
    With ``gaps_only`` the DONE layers are dropped so only work-to-do shows.
    """
    total = sum(counts.values()) or len(layers)
    done = counts.get("done", 0)
    lines = []
    if rich:
        lines.append(f"[bold]架构契约[/bold]  [dim]{done}/{total} 层完成[/dim]")
        lines.append("  " + "   ".join(
            f"[{_ARCH_ICON[s][1]}]{_ARCH_ICON[s][0]}[/{_ARCH_ICON[s][1]}] {s} {counts.get(s, 0)}"
            for s in ("done", "partial", "planned", "blocked")
        ))
    else:
        lines.append(f"架构契约  {done}/{total} 层完成")
    lines.append("")
    for layer in layers:
        st = getattr(layer.status, "value", str(layer.status))
        if gaps_only and st == "done":
            continue
        icon, color = _ARCH_ICON.get(st, ("•", "dim"))
        if rich:
            lines.append(f"[{color}]{icon}[/{color}] [bold]{layer.name}[/bold]  "
                         f"[dim]{layer.responsibility}[/dim]")
        else:
            lines.append(f"{icon} {layer.name}  {layer.responsibility}")
        if st != "done":
            for ns in (layer.next_steps or [])[:2]:
                lines.append(f"    → {ns}")
            for bl in (layer.blockers or [])[:1]:
                lines.append(f"    [#f85149]⚠ {bl}[/#f85149]" if rich else f"    ⚠ {bl}")
    return lines


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def _test_datasource(*args, **kwargs):
    from aria_cli import _test_datasource as fn
    return fn(*args, **kwargs)
def _get_ARIA_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import ARIA_TOOLS as val
    return val
def get_model_cfg(*args, **kwargs):
    from ..model_catalog import get_model_cfg as fn
    return fn(*args, **kwargs)
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _get___version__():
    from aria_code._version import __version__ as val
    return val
def _get_Panel():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return Panel
def _get_provider_key(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import get_provider_key as fn
    return fn(*args, **kwargs)
def _get_rich_box():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return rich_box
def _get__HAS_MCP():
    from aria_code.apps.cli._optional import HAS_MCP as val
    return val
def _get_CONFIG_DIR():
    from aria_cli import CONFIG_DIR as val
    return val

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


class DiagnosticOpsCommandsMixin:
    """Mixin: diagnostics, feedback, usage, and source inspection commands."""

    def cmd_architecture(self, args: str):
        """显示分层架构契约(各层状态 + 每层下一步)。用法: /architecture [--gaps]"""
        try:
            from packages.aria_core import (
                list_architecture_layers, architecture_status_counts)
        except Exception as exc:  # pragma: no cover - import guard
            msg = f"架构契约不可用: {exc}"
            self.context.console.print(f"[red]{msg}[/red]") if self.context.has_rich else print(msg)
            return
        gaps_only = "gap" in args.lower()
        lines = format_architecture_report(
            list_architecture_layers(), architecture_status_counts(),
            gaps_only=gaps_only, rich=self.context.has_rich)
        if self.context.has_rich:
            self.context.console.print(_get_Panel()("\n".join(lines), title="[bold]Aria 架构[/bold]",
                                border_style="#C08050", box=_get_rich_box().ROUNDED,
                                padding=(0, 1)))
        else:
            print("\n".join(lines))

    def cmd_bug(self, args: str):
        desc = args.strip()
        if not desc:
            self.context.console.print("[dim]用法: /bug <描述你遇到的问题>[/dim]" if self.context.has_rich
                          else "Usage: /bug <description>")
            return
        ctx_parts = []
        for m in self.terminal.conversation[-6:]:
            _c = (m.get("content", "") or "")[:300]
            ctx_parts.append(f"{m.get('role','')}: {_c}")
        ctx = "\n".join(ctx_parts)
        import platform as _pf
        env = (f"v{_get___version__()} · {_pf.system()} · py{_pf.python_version()} · "
               f"model={self.terminal.config.get('model','')}")
        self.terminal._record_feedback("bug", ctx, comment=f"{desc}\n\n[env] {env}")
        gh = "https://github.com/artheras/aria-code/issues"
        if self.context.has_rich:
            self.context.console.print("  [#C08050]✓ 已记录问题（本地）[/#C08050]")
            self.context.console.print(f"  [dim]上传需 /privacy opt-in · 或直接提 issue: {gh}[/dim]")
        else:
            print(f"  ✓ Bug recorded locally. Upload via /privacy opt-in, or file: {gh}")

    def cmd_accuracy(self, args: str):
        res = self.terminal._verify_predictions(min_age_hours=24.0)
        try:
            from apps.cli.prediction_feedback import PredictionTracker
            acc = PredictionTracker(_get_CONFIG_DIR()).accuracy()
        except Exception:
            acc = {}
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]预测战绩[/bold]  [dim]LLM 方向判断 vs 实际行情[/dim]")
            if res.get("settled"):
                self.context.console.print(f"  [dim]本次结算 {res['settled']} 笔："
                              f"命中 [green]{res['correct']}[/green] / "
                              f"落空 [red]{res['wrong']}[/red][/dim]")
            _acc = acc.get("accuracy")
            _acc_str = f"{_acc:.0%}" if _acc is not None else "—"
            self.context.console.print(
                f"  累计：已结算 [bold]{acc.get('settled',0)}[/bold] · "
                f"命中率 [#C08050]{_acc_str}[/#C08050] · "
                f"待结算 [dim]{acc.get('pending',0)}[/dim]"
            )
            if not acc.get("total"):
                self.context.console.print("  [dim]暂无记录 — 用 /team 或 /analyze 让 AI 给出方向判断后会自动追踪[/dim]")
        else:
            print(f"  预测战绩: 结算{res.get('settled',0)} 命中率"
                  f"{acc.get('accuracy')} 待结算{acc.get('pending',0)}")

    def cmd_cost(self, args: str):
        import time as _t
        elapsed = _t.time() - self.terminal._session_start
        inp = self.terminal._session_input_tokens
        out = self.terminal._session_output_tokens
        think = self.terminal._session_thinking_tokens
        turns = self.terminal._session_turns
        total = inp + out + think

        is_local = self.terminal._last_provider in ("ollama", "ollama_cache", "local")
        cost_usd = 0.0
        if not is_local:
            cost_usd = (inp * 0.14 + out * 0.28 + think * 1.10) / 1_000_000

        hh = int(elapsed // 3600)
        mm = int((elapsed % 3600) // 60)
        ss = int(elapsed % 60)
        duration = f"{hh}h {mm:02d}m {ss:02d}s" if hh else f"{mm}m {ss:02d}s"

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]Session Usage[/bold]")
            self.context.console.print()
            self.context.console.print(f"  [dim]{'Duration':<22}[/dim]{duration}")
            self.context.console.print(f"  [dim]{'Turns':<22}[/dim]{turns}")
            self.context.console.print(f"  [dim]{'Input tokens':<22}[/dim]{inp:,}")
            self.context.console.print(f"  [dim]{'Output tokens':<22}[/dim]{out:,}")
            if think:
                self.context.console.print(f"  [dim]{'Thinking tokens':<22}[/dim]{think:,}")
            self.context.console.print(f"  [dim]{'Total tokens':<22}[/dim][bold]{total:,}[/bold]")
            if is_local:
                self.context.console.print(f"  [dim]{'Est. cost':<22}[/dim][green]$0.00 (local)[/green]")
            elif total > 0:
                self.context.console.print(f"  [dim]{'Est. cost':<22}[/dim]${cost_usd:.4f} USD")
            self.context.console.print(f"  [dim]{'Provider':<22}[/dim]{self.terminal._last_provider}")
            self.context.console.print()
        else:
            print(f"  Session: {duration}  Turns: {turns}")
            print(f"  Tokens: {inp:,} in / {out:,} out / {total:,} total")
            if not is_local and total > 0:
                print(f"  Est. cost: ${cost_usd:.4f}")

    def cmd_todo(self, args: str):
        import json as _json
        todo_file = _get_CONFIG_DIR() / "todos.json"

        def _load():
            try:
                if todo_file.exists():
                    return _json.loads(todo_file.read_text(encoding="utf-8"))
            except Exception:
                pass
            return []

        def _save(tasks):
            _get_CONFIG_DIR().mkdir(parents=True, exist_ok=True)
            todo_file.write_text(_json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")

        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""
        tasks = _load()

        if sub in ("", "list", "ls"):
            if not tasks:
                self.context.console.print("[dim]No tasks. Add with: /todo add <task>[/dim]" if self.context.has_rich else "No tasks")
                return
            if self.context.has_rich:
                self.context.console.print()
                for i, t in enumerate(tasks):
                    status_icon = "[green]✓[/green]" if t.get("done") else "[yellow]○[/yellow]"
                    style = "dim" if t.get("done") else ""
                    text = t.get("text", "")
                    self.context.console.print(f"  {status_icon} [dim]{i}[/dim]  [{style}]{text}[/{style}]" if style
                                  else f"  {status_icon} [dim]{i}[/dim]  {text}")
                pending = sum(1 for t in tasks if not t.get("done"))
                self.context.console.print(f"\n  [dim]{pending}/{len(tasks)} pending[/dim]")
                self.context.console.print()
            else:
                for i, t in enumerate(tasks):
                    mark = "✓" if t.get("done") else "○"
                    print(f"  {mark} {i}  {t.get('text', '')}")
        elif sub == "add":
            if not rest:
                self.context.console.print("[dim]Usage: /todo add <task text>[/dim]" if self.context.has_rich else "Usage: /todo add <task>")
                return
            task = {"text": rest, "done": False, "id": len(tasks)}
            tasks.append(task)
            _save(tasks)
            self.context.console.print(f"  [dim]✓ Added: {rest}[/dim]" if self.context.has_rich else f"Added: {rest}")
        elif sub in ("done", "check", "complete"):
            try:
                idx = int(rest)
                tasks[idx]["done"] = True
                _save(tasks)
                self.context.console.print(f"  [dim]✓ Done: {tasks[idx]['text']}[/dim]" if self.context.has_rich
                              else f"Done: {tasks[idx]['text']}")
            except (ValueError, IndexError):
                self.context.console.print("[dim]Usage: /todo done <id>[/dim]" if self.context.has_rich else "Usage: /todo done <id>")
        elif sub in ("remove", "rm", "delete", "del"):
            try:
                idx = int(rest)
                removed = tasks.pop(idx)
                _save(tasks)
                self.context.console.print(f"  [dim]Removed: {removed['text']}[/dim]" if self.context.has_rich
                              else f"Removed: {removed['text']}")
            except (ValueError, IndexError):
                self.context.console.print("[dim]Usage: /todo remove <id>[/dim]" if self.context.has_rich else "bad index")
        elif sub == "clear":
            _save([])
            self.context.console.print("[dim]All tasks cleared[/dim]" if self.context.has_rich else "Cleared")
        else:
            full_text = (sub + " " + rest).strip()
            task = {"text": full_text, "done": False, "id": len(tasks)}
            tasks.append(task)
            _save(tasks)
            self.context.console.print(f"  [dim]✓ Added: {full_text}[/dim]" if self.context.has_rich else f"Added: {full_text}")

    def cmd_doctor(self, args: str):
        try:
            from doctor import run_doctor

            _ctx_stats = None
            try:
                from packages.aria_services.context import context_health_snapshot
                _mc = get_model_cfg(self.terminal.config.get("model", "qwen2.5:7b"))
                _ctx_stats = context_health_snapshot(
                    self.terminal.conversation,
                    max_tokens=int(_mc.get("num_ctx", 16384)),
                    threshold=float(self.terminal.config.get("auto_compact_threshold", 0.78)),
                )
            except Exception:
                _ctx_stats = None
            report = run_doctor(
                self.terminal.config,
                check_network="--network" in (args or "").split(),
                context_stats=_ctx_stats,
            )
            if self.context.has_rich:
                from rich.table import Table as _DoctorTable
                table = _DoctorTable(title="Aria Code doctor", box=_get_rich_box().ROUNDED)
                table.add_column("Status", width=8)
                table.add_column("Check", style="bold")
                table.add_column("Detail", style="dim")
                table.add_column("Suggestion", style="dim")
                icons = {"ok": "[green]OK[/green]", "warn": "[yellow]WARN[/yellow]", "err": "[red]ERR[/red]"}
                for check in report.checks:
                    table.add_row(
                        icons.get(check.status, check.status.upper()),
                        check.name,
                        check.detail,
                        check.suggestion,
                    )
                self.context.console.print()
                self.context.console.print(table)
                color = "green" if report.errors == 0 and report.warnings == 0 else ("yellow" if report.errors == 0 else "red")
                self.context.console.print(f"[{color}]{report.passed} passed · {report.warnings} warnings · {report.errors} errors[/{color}]")
                self.context.console.print()
            else:
                from doctor import format_doctor_plain
                print(format_doctor_plain(report))
            return
        except Exception as exc:
            self.context.console.print(f"[yellow]doctor module unavailable, using legacy checks: {exc}[/yellow]" if self.context.has_rich else f"doctor module unavailable: {exc}")

        import importlib as _il
        import subprocess as _sp
        import shutil as _sh
        cfg = self.terminal.config
        ollama_url = cfg.get("ollama_url", "http://localhost:11434")
        api_url = cfg.get("api_url", "http://localhost:8000")

        checks: list[tuple] = []

        def _ok(label, detail=""): checks.append(("ok", label, detail))
        def _warn(label, detail=""): checks.append(("warn", label, detail))
        def _err(label, detail=""): checks.append(("err", label, detail))

        import sys as _sys
        pyver = f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}"
        if _sys.version_info >= (3, 9):
            _ok("Python", pyver)
        else:
            _warn("Python", f"{pyver} (3.9+ recommended)")

        try:
            import urllib.request as _ur
            _opener = _ur.build_opener(_ur.ProxyHandler({}))
            _r = _opener.open(f"{ollama_url}/api/tags", timeout=3)
            _data = json.loads(_r.read())
            models = [m["name"] for m in _data.get("models", [])]
            if models:
                _ok("Ollama", f"{len(models)} models: {', '.join(models[:4])}")
            else:
                _warn("Ollama", "running but no models installed (ollama pull qwen2.5-coder:1.5b)")
        except Exception as e:
            _err("Ollama", f"not reachable at {ollama_url} ({e})")

        try:
            import urllib.request as _ur
            _opener = _ur.build_opener(_ur.ProxyHandler({}))
            _r = _opener.open(f"{api_url}/health", timeout=3)
            _ok("Backend", f"running at {api_url}")
        except Exception:
            _warn("Backend", f"offline at {api_url} — local Ollama mode will be used")

        key_checks = [
            ("finnhub", "股票行情"),
            ("alphavantage", "历史数据"),
            ("newsapi", "新闻"),
            ("brave", "网络搜索"),
            ("coingecko", "加密货币"),
        ]
        for svc, desc in key_checks:
            k = _get_provider_key(svc)
            if k:
                _ok(f"API key: {svc}", f"{desc} ({'*'*6}{k[-4:]})")
            else:
                _warn(f"API key: {svc}", f"{desc} 未配置 (/apikey set {svc} <key>)")

        llm_keys = [("deepseek", "DeepSeek"), ("openai", "OpenAI"),
                    ("siliconflow", "SiliconFlow"), ("moonshot", "Moonshot")]
        _has_any_llm = False
        for svc, name in llm_keys:
            k = _get_provider_key(svc)
            if k:
                _ok(f"LLM key: {svc}", f"{name} configured")
                _has_any_llm = True
        if not _has_any_llm:
            _warn("LLM keys", "No cloud LLM keys — Ollama must be running for AI responses")

        _pkgs = [
            ("aiohttp", "async HTTP"),
            ("rich", "terminal UI"),
            ("prompt_toolkit", "autocomplete"),
            ("yfinance", "market data"),
            ("pandas", "data processing"),
            ("requests", "HTTP client"),
        ]
        for pkg, desc in _pkgs:
            try:
                m = _il.import_module(pkg)
                ver = getattr(m, "_get___version__()", "?")
                _ok(f"pkg: {pkg}", f"{desc} v{ver}")
            except ImportError:
                _warn(f"pkg: {pkg}", f"{desc} not installed (pip install {pkg})")

        aria_md = pathlib.Path.cwd() / "ARIA.md"
        if aria_md.exists():
            lines = len(aria_md.read_text(encoding="utf-8").splitlines())
            _ok("ARIA.md", f"{lines} lines of project context")
        else:
            _warn("ARIA.md", f"not found in {pathlib.Path.cwd()} (use /init to create)")

        if _get__HAS_MCP():
            try:
                reg = self.terminal._mcp_registry
                if reg and hasattr(reg, "list_tools"):
                    tools = reg.list_tools()
                    _ok("MCP", f"{len(tools)} tools from MCP servers")
                else:
                    _warn("MCP", "registry not started yet")
            except Exception:
                _warn("MCP", "loaded but no active servers")
        else:
            _warn("MCP", "mcp_client not found — MCP support disabled")

        tool_count = len(_get_ARIA_TOOLS()) + len(_get_LOCAL_TOOLS())
        _ok("Aria tools", f"{tool_count} tools loaded")

        self.context.console.print() if self.context.has_rich else None
        if self.context.has_rich:
            self.context.console.print("[bold]Aria Code — Diagnostics[/bold]")
            self.context.console.print()
            icons = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "err": "[red]✗[/red]"}
            for status, label, detail in checks:
                icon = icons[status]
                detail_str = f"  [dim]{detail}[/dim]" if detail else ""
                self.context.console.print(f"  {icon}  {label:<28}{detail_str}")
            self.context.console.print()
            n_ok = sum(1 for s, *_ in checks if s == "ok")
            n_w = sum(1 for s, *_ in checks if s == "warn")
            n_e = sum(1 for s, *_ in checks if s == "err")
            summary_color = "green" if n_e == 0 and n_w == 0 else ("yellow" if n_e == 0 else "red")
            self.context.console.print(f"  [{summary_color}]{n_ok} passed · {n_w} warnings · {n_e} errors[/{summary_color}]")
            self.context.console.print()

            _fh_ok = bool(_get_provider_key("finnhub"))
            _av_ok = bool(_get_provider_key("alphavantage"))
            _na_ok = bool(_get_provider_key("newsapi"))
            _ak_ok = True
            _llm_ok = any(_get_provider_key(p) for p in ("deepseek", "openai", "anthropic", "groq"))

            _guide_needed = not (_fh_ok and _av_ok and _na_ok and _llm_ok)
            if _guide_needed:
                self.context.console.print("[bold]数据源配置指南[/bold]  [dim](完整功能需要以下 key)[/dim]")
                self.context.console.print()
            self.context.console.print("  [dim]Use /doctor --network for network checks[/dim]")
        else:
            print("Diagnostics complete")

    async def cmd_install(self, args: str):
        """
        检测并安装缺失的依赖包（环境补全）。

          /install              扫描全部已知依赖，列出缺失并询问是否安装
          /install <pkg> [pkg]  直接安装指定 Python 包
          /install --auto       根据最近一次提问的意图检测缺失项
          /install --required   仅安装"必需"包，跳过可选项
          /install --plan       只展示安装计划，不安装
          /install --yes        非交互确认，直接安装选中包
        """
        import shlex as _shlex
        import subprocess as _sp
        import sys as _sys
        from apps.cli.preflight import (
            build_full_dependency_report,
            build_intent_preflight,
            build_install_plan,
            package_to_module,
            select_install_packages,
        )

        raw = (args or "").strip()
        tokens = raw.split()
        flags = {t for t in tokens if t.startswith("--")}
        explicit_pkgs = [t for t in tokens if not t.startswith("--")]

        # ── Resolve what to install ───────────────────────────────────────────
        report = None
        if explicit_pkgs:
            # Direct package install — no detection needed
            pip_packages = explicit_pkgs
            command_hints: tuple = ()
            env_hints: tuple = ()
        else:
            if "--auto" in flags:
                # Detect from the user's last real question
                last_msg = ""
                for m in reversed(self.terminal.conversation):
                    if m.get("role") == "user" and m.get("content"):
                        last_msg = m["content"] if isinstance(m["content"], str) else ""
                        break
                if not last_msg:
                    self.context.console.print("[yellow]没有可分析的历史提问，改用全量扫描[/yellow]" if self.context.has_rich
                                  else "No history; full scan")
                    report = build_full_dependency_report(include_optional="--required" not in flags)
                else:
                    report = build_intent_preflight(last_msg)
            else:
                report = build_full_dependency_report(include_optional="--required" not in flags)

            plan = build_install_plan(report)
            pip_packages = list(plan.pip_packages)
            command_hints = plan.command_hints
            env_hints = plan.env_hints

        # ── Nothing missing ───────────────────────────────────────────────────
        if not pip_packages and not (report and (report.missing_commands or report.missing_env)):
            self.context.console.print("[green]✓ 环境完整，没有检测到缺失的 Python 包[/green]" if self.context.has_rich
                          else "All dependencies satisfied")
            return

        # ── Show findings ─────────────────────────────────────────────────────
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]环境检测结果[/bold]")
            if pip_packages:
                self.context.console.print(f"  [yellow]缺少 {len(pip_packages)} 个 Python 包:[/yellow]")
                for p in pip_packages:
                    purpose = ""
                    if report:
                        for r in report.missing_python:
                            if r.package == p:
                                purpose = f"  [dim]— {r.purpose}{'（可选）' if not r.required else ''}[/dim]"
                                break
                    self.context.console.print(f"    • [cyan]{p}[/cyan]{purpose}")
            if command_hints:
                self.context.console.print("  [yellow]缺少命令行工具:[/yellow]")
                for h in command_hints:
                    self.context.console.print(f"    • [dim]{h}[/dim]")
            if env_hints:
                self.context.console.print("  [dim]未配置的环境变量（可选，不自动处理）:[/dim]")
                for h in env_hints:
                    self.context.console.print(f"    • [dim]{h}[/dim]")
            self.context.console.print()
        else:
            print(f"Missing packages: {', '.join(pip_packages)}")
            for h in command_hints:
                print(f"  tool: {h}")

        if not pip_packages:
            if command_hints:
                self.context.console.print("[dim]命令行工具需手动安装（见上方提示），Aria 不会自动执行系统级安装[/dim]"
                              if self.context.has_rich else "Install CLI tools manually (see hints above)")
            return

        # ── Select packages ───────────────────────────────────────────────────
        if report:
            if "--plan" in flags:
                selection = select_install_packages(plan, report, mode="plan")
            elif "--yes" in flags:
                _mode = "required" if "--required" in flags else "all"
                selection = select_install_packages(plan, report, mode=_mode)
            else:
                required_pkgs = [
                    r.package for r in report.missing_python
                    if r.required and r.package in pip_packages
                ]
                optional_pkgs = [
                    r.package for r in report.missing_python
                    if not r.required and r.package in pip_packages
                ]
                if self.context.has_rich:
                    self.context.console.print("[bold]选择安装范围[/bold]")
                    self.context.console.print("  [cyan]all[/cyan]      安装全部缺失 Python 包")
                    self.context.console.print("  [cyan]required[/cyan]  只安装必需包")
                    self.context.console.print("  [cyan]optional[/cyan]  只安装可选增强包")
                    self.context.console.print("  [cyan]custom[/cyan]    手动输入包名或编号")
                    self.context.console.print("  [cyan]plan[/cyan]      只显示计划，不安装")
                    self.context.console.print("  [cyan]skip[/cyan]      跳过")
                    for idx, pkg in enumerate(pip_packages, 1):
                        kind = "required" if pkg in required_pkgs else "optional"
                        self.context.console.print(f"    [dim]{idx}.[/dim] {pkg} [dim]({kind})[/dim]")
                else:
                    print("Select install scope: all | required | optional | custom | plan | skip")
                    for idx, pkg in enumerate(pip_packages, 1):
                        kind = "required" if pkg in required_pkgs else "optional"
                        print(f"  {idx}. {pkg} ({kind})")
                default_mode = "required" if required_pkgs and optional_pkgs else "all"
                try:
                    choice = self.context.console.input(f"安装范围 [{default_mode}]: ") if self.context.has_rich else input(f"Install scope [{default_mode}]: ")
                except (EOFError, KeyboardInterrupt):
                    self.context.console.print("\n[dim]已取消[/dim]" if self.context.has_rich else "Cancelled")
                    return
                choice = (choice or default_mode).strip().lower()
                custom_items: list[str] = []
                if choice == "custom":
                    try:
                        raw_custom = self.context.console.input("输入包名或编号（空格/逗号分隔）: ") if self.context.has_rich else input("Packages or numbers: ")
                    except (EOFError, KeyboardInterrupt):
                        self.context.console.print("\n[dim]已取消[/dim]" if self.context.has_rich else "Cancelled")
                        return
                    for item in raw_custom.replace(",", " ").split():
                        if item.isdigit():
                            idx = int(item)
                            if 1 <= idx <= len(pip_packages):
                                custom_items.append(pip_packages[idx - 1])
                        else:
                            custom_items.append(item)
                selection = select_install_packages(
                    plan, report, mode=choice, custom=custom_items
                )
            pip_packages = list(selection.pip_packages)
            if selection.mode in {"plan", "dry-run", "dry_run"}:
                self.context.console.print("[dim]已生成安装计划，未执行安装。[/dim]" if self.context.has_rich else "Install plan only; no changes made.")
                return
            if not pip_packages:
                self.context.console.print("[dim]没有选择任何 Python 包，未安装。[/dim]" if self.context.has_rich else "No packages selected.")
                return
            if selection.skipped_packages and self.context.has_rich:
                self.context.console.print(f"[dim]跳过: {', '.join(selection.skipped_packages)}[/dim]")
        elif "--plan" in flags:
            self.context.console.print("[dim]显式包安装计划已显示，未执行安装。[/dim]" if self.context.has_rich else "Install plan only; no changes made.")
            return

        # ── Confirm ───────────────────────────────────────────────────────────
        pip_cmd = [_sys.executable, "-m", "pip", "install", *pip_packages]
        pretty = " ".join(_shlex.quote(c) for c in pip_cmd)
        if "--yes" not in flags:
            prompt = f"将运行: {pretty}\n确认安装? [y/N]: "
            try:
                answer = self.context.console.input(prompt) if self.context.has_rich else input(prompt)
            except (EOFError, KeyboardInterrupt):
                self.context.console.print("\n[dim]已取消[/dim]" if self.context.has_rich else "Cancelled")
                return
            if answer.strip().lower() not in {"y", "yes"}:
                self.context.console.print("[dim]已取消，未安装任何包[/dim]" if self.context.has_rich else "Cancelled")
                return
        elif self.context.has_rich:
            self.context.console.print(f"[dim]Auto install: {pretty}[/dim]")

        # ── Install ───────────────────────────────────────────────────────────
        self.context.console.print(f"\n[dim]⏳ 安装中: {' '.join(pip_packages)}…[/dim]" if self.context.has_rich
                      else f"Installing {' '.join(pip_packages)}...")
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _sp.run(pip_cmd, capture_output=True, text=True, timeout=300),
            )
        except Exception as exc:
            self.context.console.print(f"[red]安装失败: {exc}[/red]" if self.context.has_rich else f"Install failed: {exc}")
            return

        if proc.returncode == 0:
            # Verify each package now imports
            import importlib as _il
            _il.invalidate_caches()
            ok_list, fail_list = [], []
            for p in pip_packages:
                mod = package_to_module(p)
                try:
                    _il.import_module(mod)
                    ok_list.append(p)
                except Exception:
                    fail_list.append(p)
            # File parser availability is cached for performance.  Refresh it
            # now so DOCX/PDF/XLSX tools work in this same Aria session.
            try:
                from aria_code.tools.file_analysis_tools import refresh_optional_parsers

                refresh_optional_parsers()
            except Exception:
                pass
            if self.context.has_rich:
                self.context.console.print(f"  [green]✓ 安装完成: {', '.join(ok_list) or '—'}[/green]")
                if fail_list:
                    self.context.console.print(f"  [yellow]⚠ 已安装但当前会话需重启才能加载: {', '.join(fail_list)}[/yellow]")
                elif ok_list:
                    self.context.console.print("  [dim]能力已刷新，当前会话可直接使用。[/dim]")
            else:
                print(f"Installed: {', '.join(ok_list)}")
        else:
            err_tail = (proc.stderr or proc.stdout or "")[-400:]
            self.context.console.print(f"[red]pip 安装失败 (code {proc.returncode}):[/red]\n[dim]{err_tail}[/dim]"
                          if self.context.has_rich else f"pip failed: {err_tail}")

    async def cmd_datasource(self, args: str):
        sub = args.strip().lower()
        if sub.startswith("test "):
            src_name = sub[5:].strip()
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: _test_datasource(src_name)
            )
            return

        if sub == "config":
            paths = [
                "~/.aria/datasources.yaml",
                "~/.aria/.env",
                str(_get_CONFIG_DIR() / "providers.json"),
            ]
            if self.context.has_rich:
                self.context.console.print("  [bold]数据源配置文件:[/bold]")
                for p in paths:
                    import pathlib
                    full = pathlib.Path(p).expanduser()
                    exists = "[green]✓[/green]" if full.exists() else "[dim]✗ (未创建)[/dim]"
                    self.context.console.print(f"  {exists}  [dim]{p}[/dim]")
                self.context.console.print("\n  [dim]环境变量: TUSHARE_TOKEN FRED_API_KEY ALPHA_VANTAGE_KEY[/dim]")
            return

        try:
            from datasources.router import _SOURCE_REGISTRY, DataRouter
            router = DataRouter()
        except ImportError:
            print_error(self.context, "datasources 模块未找到")
            return

        if self.context.has_rich:
            from rich.markup import escape
            from rich.table import Table
            from rich import box
            from ui.render.responsive import StackedRecord, render_stacked_records, structured_layout

            _DESC = {
                "yfinance": "Yahoo Finance (免费)",
                "akshare": "AkShare A股 (免费)",
                "tushare": "Tushare Pro (需Token)",
                "fred": "美联储经济数据 (免费)",
                "edgar": "SEC EDGAR 财报 (免费)",
                "alpha_vantage": "Alpha Vantage (免费Key)",
                "world_bank": "世界银行 (免费)",
            }
            source_rows = []
            for name, cls in _SOURCE_REGISTRY.items():
                try:
                    source = cls()
                    configured = source.is_configured()
                    status = "[green]✓ 就绪[/green]" if configured else "[dim]✗ 未配置[/dim]"
                    needs_key = "是" if cls.requires_key else "否"
                    markets = ", ".join(getattr(cls, "markets", []))
                except Exception:
                    status, needs_key, markets = "[red]错误[/red]", "?", "?"
                source_rows.append((name, markets, needs_key, status, _DESC.get(name, "")))

            layout = structured_layout(self.context.console)
            if layout == "stacked":
                records = [
                    StackedRecord(
                        headline=f"{escape(str(name))}  {status}",
                        lines=(
                            f"[dim]市场[/dim] {escape(str(markets or '—'))}  ·  "
                            f"[dim]需要 Key[/dim] {needs_key}",
                            f"[dim]说明[/dim] {escape(str(description or '—'))}",
                        ),
                    )
                    for name, markets, needs_key, status, description in source_rows
                ]
                render_stacked_records(
                    self.context.console,
                    title="数据源状态",
                    records=records,
                    footer="/datasource config — 配置文件路径",
                )
                return

            table = Table(title="数据源状态", box=_get_rich_box().SIMPLE, header_style="bold dim")
            table.add_column("名称", width=16)
            table.add_column("市场", width=20)
            table.add_column("需要Key", width=8)
            table.add_column("状态", width=8)
            if layout == "full":
                table.add_column("说明")
            for name, markets, needs_key, status, description in source_rows:
                row = [name, markets, needs_key, status]
                if layout == "full":
                    row.append(description)
                table.add_row(*row)
            self.context.console.print(table)
            self.context.console.print("  [dim]/datasource config — 配置文件路径[/dim]")
        else:
            for name, cls in _SOURCE_REGISTRY.items():
                src = cls()
                print(f"  {name}: {'ready' if src.is_configured() else 'not configured'}")
