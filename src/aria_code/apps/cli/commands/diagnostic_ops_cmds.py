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
        """Prediction accuracy tracking moved to Arthera backend / financial-service MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/accuracy 已迁移至 Arthera 后端 / financial-service MCP。[/dim]")

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
        eff_turns = max(turns, 1)
        tokens_per_turn = total // eff_turns
        avoided_monolith_tokens = eff_turns * 12500
        eff_pct = (avoided_monolith_tokens / max(total + avoided_monolith_tokens, 1)) * 100.0

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]Session Usage & AI ROI Scorecard[/bold]")
            self.context.console.print()
            self.context.console.print(f"  [dim]{'Duration':<22}[/dim]{duration}")
            self.context.console.print(f"  [dim]{'Turns':<22}[/dim]{turns} ({tokens_per_turn:,} tokens/turn)")
            self.context.console.print(f"  [dim]{'Input tokens':<22}[/dim]{inp:,}")
            self.context.console.print(f"  [dim]{'Output tokens':<22}[/dim]{out:,}")
            if think:
                self.context.console.print(f"  [dim]{'Thinking tokens':<22}[/dim]{think:,}")
            self.context.console.print(f"  [dim]{'Total tokens':<22}[/dim][bold]{total:,}[/bold]")
            self.context.console.print(
                f"  [dim]{'Token efficiency':<22}[/dim][green]+{eff_pct:.1f}%[/green] "
                f"[dim](~{avoided_monolith_tokens:,} tokens saved via on-demand MCP/skill offload)[/dim]"
            )
            if is_local:
                self.context.console.print(f"  [dim]{'Est. cost':<22}[/dim][green]$0.00 (local)[/green]")
            elif total > 0:
                self.context.console.print(f"  [dim]{'Est. cost':<22}[/dim]${cost_usd:.4f} USD")
            self.context.console.print(f"  [dim]{'Provider':<22}[/dim]{self.terminal._last_provider}")
            self.context.console.print()
        else:
            print(f"  Session: {duration}  Turns: {turns} ({tokens_per_turn:,} tokens/turn)")
            print(f"  Tokens: {inp:,} in / {out:,} out / {total:,} total")
            print(f"  Token efficiency: +{eff_pct:.1f}% (~{avoided_monolith_tokens:,} tokens saved via MCP offload)")
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
            self.context.console.print(f"[yellow]doctor module unavailable: {exc}[/yellow]" if self.context.has_rich else f"doctor module unavailable: {exc}")

    async def cmd_install(self, args: str):
        """Show missing optional dependencies or install explicit packages via pip."""
        import shlex as _shlex
        import subprocess as _sp
        import sys as _sys
        from apps.cli.preflight import build_full_dependency_report, build_install_plan

        tokens = (args or "").strip().split()
        flags = {t for t in tokens if t.startswith("--")}
        explicit_pkgs = [t for t in tokens if not t.startswith("--")]
        if explicit_pkgs:
            pip_packages = explicit_pkgs
        else:
            report = build_full_dependency_report(include_optional="--required" not in flags)
            pip_packages = list(build_install_plan(report).pip_packages)

        if not pip_packages:
            msg = "All dependencies satisfied."
            self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
            return
        pip_cmd = [_sys.executable, "-m", "pip", "install", *pip_packages]
        pretty = " ".join(_shlex.quote(c) for c in pip_cmd)
        if "--yes" not in flags:
            msg = f"Run `{pretty}` (or pass `/install --yes` to install)."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        proc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _sp.run(pip_cmd, capture_output=True, text=True, timeout=300)
        )
        if proc.returncode == 0:
            self.context.console.print(f"[green]Installed: {', '.join(pip_packages)}[/green]" if self.context.has_rich else f"Installed: {', '.join(pip_packages)}")
        else:
            err = (proc.stderr or proc.stdout or "")[-300:]
            self.context.console.print(f"[red]pip failed ({proc.returncode}): {err}[/red]" if self.context.has_rich else f"pip failed: {err}")

    async def cmd_datasource(self, args: str):
        """Financial datasources moved to financial-service MCP."""
        if self.context.has_rich:
            self.context.console.print("[dim]/datasource 已迁移至 financial-service MCP（financial_datasource_status）。[/dim]")
