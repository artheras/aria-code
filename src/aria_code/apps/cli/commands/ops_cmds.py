"""OpsCommandsMixin — watch, services, plan, git, and GitHub helper commands."""

from __future__ import annotations


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

# display_path lives in ui.render.output; aria_cli only re-exports it under a
# private alias, so going through aria_cli was a pointless second hop.
from aria_code.ui.render.output import display_path as _display_path
def _tool_github(*args, **kwargs):
    from aria_cli import _tool_github as fn
    return fn(*args, **kwargs)
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _get_provider_key(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import get_provider_key as fn
    return fn(*args, **kwargs)
def _tool_run_command(*args, **kwargs):
    from aria_cli import _tool_run_command as fn
    return fn(*args, **kwargs)
def _tool_write_file(*args, **kwargs):
    from aria_cli import _tool_write_file as fn
    return fn(*args, **kwargs)
def _get_Syntax():
    from ._ui import Syntax as val
    return val
def _get__SYNTAX_THEME():
    from aria_cli import _SYNTAX_THEME as val
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


class OpsCommandsMixin:
    """Mixin: operational and workflow helper commands."""

    def cmd_watch(self, args: str):
        parts = args.split() if args else ["list"]
        action = parts[0].lower() if parts else "list"
        watchlist = self.terminal.config.get("watchlist", [])

        if action == "add" and len(parts) > 1:
            symbol = parts[1].upper()
            if symbol not in watchlist:
                watchlist.append(symbol)
                self.terminal.config["watchlist"] = watchlist
                self.context.save_config(self.terminal.config)
                self.context.console.print(f"[green]Added {symbol} to watchlist[/green]" if self.context.has_rich
                              else f"Added {symbol}")
            else:
                self.context.console.print(f"[dim]{symbol} already in watchlist[/dim]" if self.context.has_rich
                              else f"{symbol} already in watchlist")

        elif action == "remove" and len(parts) > 1:
            symbol = parts[1].upper()
            if symbol in watchlist:
                watchlist.remove(symbol)
                self.terminal.config["watchlist"] = watchlist
                self.context.save_config(self.terminal.config)
                self.context.console.print(f"[dim]Removed {symbol} from watchlist[/dim]" if self.context.has_rich
                              else f"Removed {symbol}")
            else:
                self.context.console.print(f"[red]{symbol} not in watchlist[/red]" if self.context.has_rich
                              else f"{symbol} not in watchlist")

        else:
            if self.context.has_rich:
                if watchlist:
                    self.context.console.print(f"  [dim]Watchlist:[/dim] {', '.join(watchlist)}")
                else:
                    self.context.console.print("  [dim]Watchlist: Empty[/dim]")
            else:
                print(f"Watchlist: {', '.join(watchlist)}")

    def cmd_services(self, args: str):
        """Show CLI service tiers and core workflows."""
        provider_summary = None
        try:
            from packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH
            provider_summary = GLOBAL_PROVIDER_HEALTH.summary()
        except Exception:
            provider_summary = None

        cfg = getattr(self.terminal, "config", {}) or {}
        auto_compact = bool(cfg.get("auto_compact_context", True))
        try:
            auto_compact_threshold = int(float(cfg.get("auto_compact_threshold", 0.78)) * 100)
        except Exception:
            auto_compact_threshold = 78

        def _key_status(provider: str) -> str:
            try:
                return "configured" if _get_provider_key(provider) else "not set"
            except Exception:
                return "unknown"

        runtime_services = [
            ("LLM runtime", cfg.get("model", "unknown"), "local/Ollama with cloud fallback"),
            ("Context manager", f"auto compact {'on' if auto_compact else 'off'} @ {auto_compact_threshold}%", "preflight + post-turn guards"),
            ("Market data", "DataService", "quote/history/fundamentals/TA + provider health"),
            ("Charts/reports", "artifact services", "HTML/PNG charts, dashboards, Markdown/HTML reports"),
            ("News/Web", f"finnhub:{_key_status('finnhub')} · newsapi:{_key_status('newsapi')} · brave:{_key_status('brave')}", "news command + web search fallback"),
            ("Cloud AI/data", f"dashscope:{_key_status('dashscope')} · openai:{_key_status('openai')} · anthropic:{_key_status('anthropic')}", "optional external providers"),
            ("MCP/tools", f"{len(_get_LOCAL_TOOLS())} local tools", "tool loop with repeat-call guard"),
        ]

        service_groups = [
            (
                "CORE (Standard)",
                [
                    "Code agent with local tools (read/write/edit/search/run)",
                    "Slash command workflows for quote/analyze/backtest/risk/screen",
                    "Session save/load/export and interactive history management",
                    "Model switching + thinking mode controls for response depth",
                ],
            ),
            (
                "QUANTUM Automation",
                [
                    "Agentic multi-step loop (auto read -> analyze -> edit -> execute)",
                    "Auto-recovery guidance for failed commands and code fixes",
                    "Strategy generation, backtest reporting, and risk analysis skills",
                    "Cross-workspace research sync hooks (session + export pipeline)",
                ],
            ),
            (
                "ENTERPRISE Controls (included in Quantum)",
                [
                    "Service health diagnostics (/health) for backend + local model stack",
                    "Governed command execution with dangerous-command blocking",
                    "Audit-friendly session logs and reproducible command trails",
                    "MCP-ready service integration path via external tool endpoints",
                ],
            ),
        ]

        quick_flow = [
            "/model",
            "/gen-strategy momentum AAPL",
            "/backtest momentum AAPL 2024-01-01 2025-01-01",
            "/risk AAPL",
            "/export md strategy_report.md",
        ]

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]CLI Services[/bold] [dim](runtime boundaries + workflow)[/dim]")
            self.context.console.print()
            self.context.console.print("  [bold]Runtime Service Map[/bold]")
            for name, status, detail in runtime_services:
                self.context.console.print(f"    [dim]{name:<16}[/dim] [bold]{status}[/bold]  [dim]{detail}[/dim]")
            if provider_summary is not None:
                color = "green" if provider_summary.status == "ok" else ("red" if provider_summary.status == "err" else "yellow")
                self.context.console.print(f"    [dim]{'Provider health':<16}[/dim] [{color}]{provider_summary.status}[/{color}]  [dim]{provider_summary.detail}[/dim]")
                self.context.console.print(f"    [dim]{'Suggestion':<16}[/dim] [dim]{provider_summary.suggestion}[/dim]")
            self.context.console.print()
            for group_name, items in service_groups:
                self.context.console.print(f"  [bold #C08050]{group_name}[/bold #C08050]")
                for item in items:
                    self.context.console.print(f"    [dim]> {item}[/dim]")
                self.context.console.print()

            self.context.console.print("  [bold]Quick Start Flow[/bold]")
            for cmd in quick_flow:
                self.context.console.print(f"    [bold]{cmd}[/bold]")
            self.context.console.print()
        else:
            print("\nCLI Services (runtime boundaries + workflow)\n")
            print("  Runtime Service Map")
            for name, status, detail in runtime_services:
                print(f"    {name:<16} {status}  {detail}")
            if provider_summary is not None:
                print(f"    {'Provider health':<16} {provider_summary.status}  {provider_summary.detail}")
                print(f"    {'Suggestion':<16} {provider_summary.suggestion}")
            print()
            for group_name, items in service_groups:
                print(f"  {group_name}")
                for item in items:
                    print(f"    > {item}")
                print()

            print("  Quick Start Flow")
            for cmd in quick_flow:
                print(f"    {cmd}")
            print()

    def cmd_plan(self, args: str):
        """Create an executable plan and store it for /apply-plan."""
        raw = args.strip()
        if not raw:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /plan <steps>  — see examples below[/dim]")
                self.context.console.print("[dim]  /plan fetch AAPL quote -> generate chart -> write report[/dim]")
                self.context.console.print("[dim]  /plan 1. Analyze sentiment  2. Build model  3. Backtest[/dim]")
            else:
                print("Usage: /plan <steps>")
                print("  /plan fetch AAPL quote -> generate chart -> write report")
                print("  /plan 1. Analyze sentiment  2. Build model  3. Backtest")
            return

        from aria_code.plan_utils import parse_plan
        plan_steps = parse_plan(raw)
        if not plan_steps:
            self.context.console.print("[dim]No valid steps found[/dim]" if self.context.has_rich else "No valid steps found")
            return

        self.terminal.pending_plan = [s.description for s in plan_steps]

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"[bold]Execution Plan[/bold]  [dim]({len(plan_steps)} steps)[/dim]")
            self.context.console.print()
            for s in plan_steps:
                dep_str = f"  [dim](after {', '.join(str(d) for d in s.deps)})[/dim]" if s.deps else ""
                label = f" [dim][{s.name}][/dim]" if s.name else ""
                self.context.console.print(f"  [dim]{s.index}.[/dim]{label} [bold]{s.description}[/bold]{dep_str}")
            self.context.console.print()
            self.context.console.print("[dim]Run /apply-plan to execute these steps.[/dim]")
            self.context.console.print()
        else:
            print(f"\nExecution Plan ({len(plan_steps)} steps)")
            for s in plan_steps:
                dep_str = f"  (after {', '.join(str(d) for d in s.deps)})" if s.deps else ""
                label = f" [{s.name}]" if s.name else ""
                print(f"  {s.index}.{label} {s.description}{dep_str}")
            print("Run /apply-plan to execute these steps.\n")

    def cmd_plan_report(self, args: str):
        """Show or export last plan execution report."""
        rows = list(getattr(self.terminal, "last_plan_results", []) or [])
        if not rows:
            msg = "No plan report available. Run /apply-plan first."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return

        parts = args.split()
        open_after = "--open" in parts
        parts = [p for p in parts if p != "--open"]
        fmt = parts[0].lower() if parts else "show"
        out_file = parts[1] if len(parts) > 1 else None

        if fmt == "show":
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("[bold]Last Plan Report[/bold]")
                for idx, row in enumerate(rows, 1):
                    status_color = "green" if row["status"] == "completed" else ("yellow" if row["status"] == "blocked" else "red")
                    self.context.console.print(
                        f"  [dim]{idx}.[/dim] [{status_color}]{row['status']}[/{status_color}] "
                        f"[bold]{row['step']}[/bold] [dim]({row['duration']}s, exit={row.get('exit_code')})[/dim]"
                    )
                    if row.get("error"):
                        self.context.console.print(f"     [red]{row['error']}[/red]")
                self.context.console.print()
            else:
                print("\nLast Plan Report")
                for idx, row in enumerate(rows, 1):
                    print(f"  {idx}. {row['status']}  {row['step']} ({row['duration']}s, exit={row.get('exit_code')})")
                    if row.get("error"):
                        print(f"     ERROR: {row['error']}")
            return

        if fmt not in {"md", "json"}:
            msg = "Usage: /plan-report [md|json] [file] [--open]"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return

        if not out_file:
            out_file = f"plan_report.{fmt}"

        try:
            if fmt == "json":
                content = json.dumps(rows, ensure_ascii=False, indent=2)
            else:
                md_lines = ["# Plan Execution Report", ""]
                for idx, row in enumerate(rows, 1):
                    md_lines.append(
                        f"{idx}. **{row['status']}** `{row['step']}` "
                        f"({row['duration']}s, exit={row.get('exit_code')})"
                    )
                    if row.get("error"):
                        md_lines.append(f"   - Error: {row['error']}")
                md_lines.append("")
                content = "\n".join(md_lines)

            result = _tool_write_file({"path": out_file, "content": content})
            if result.get("success"):
                saved_path = result["data"]["path"]
                msg = f"Plan report saved to {_display_path(saved_path)}"
                self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
                if open_after:
                    self._open_file(saved_path)
            else:
                err = result.get("error", "Failed to save report")
                self.context.console.print(f"[red]{err}[/red]" if self.context.has_rich else err)
        except Exception as e:
            self.context.console.print(f"[red]{e}[/red]" if self.context.has_rich else str(e))

    def cmd_git(self, args: str):
        """Git helper shortcuts."""
        policy = self.terminal.config.get("command_policy", "safe")
        raw = args.strip()
        if not raw:
            sub = "status"
            sub_args = ""
        else:
            parts = raw.split(maxsplit=1)
            sub = parts[0].lower()
            sub_args = parts[1].strip() if len(parts) > 1 else ""

        mapping = {
            "status": "git status --short --branch",
            "diff": "git diff --stat",
            "summary": "git status --short --branch && git diff --stat",
            "branch": "git branch -v",
            "stash": "git stash list",
            "remote": "git remote -v",
        }
        if sub == "patch":
            cmd = "git diff" if not sub_args else f"git diff -- {sub_args}"
        elif sub == "log":
            limit = sub_args if sub_args and sub_args.isdigit() else "15"
            cmd = f"git log --oneline --graph --decorate -{limit}"
        elif sub == "commit":
            status_probe = _tool_run_command({"command": "git status --porcelain", "policy": policy})
            if not status_probe.get("success"):
                err = status_probe.get("error", "Failed to inspect git status")
                self.context.console.print(f"[red]{err}[/red]" if self.context.has_rich else err)
                return

            status_out = status_probe.get("data", {}).get("stdout", "").strip()
            if not status_out:
                msg = "No changes to commit."
                self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
                return

            changed_files = []
            for line in status_out.splitlines():
                if len(line) >= 4:
                    changed_files.append(line[3:].strip())
            unique_files = [f for f in changed_files if f]
            total_files = len(unique_files)
            file_preview = ", ".join(unique_files[:5]) if unique_files else "workspace"
            body_summary = f"Files changed: {total_files}"
            body_preview = f"Top files: {file_preview}"

            if not sub_args:
                files = []
                for line in status_out.splitlines()[:3]:
                    if len(line) >= 4:
                        files.append(line[3:].strip())
                sample = ", ".join(files) if files else "workspace"
                total = len(status_out.splitlines())
                sub_args = f"chore: update {total} file(s) ({sample})"
                if self.context.has_rich:
                    self.context.console.print(f"[dim]Auto commit message:[/dim] {sub_args}")
                else:
                    print(f"Auto commit message: {sub_args}")

            cmd = (
                f"git add -A && git commit "
                f"-m {shlex.quote(sub_args)} "
                f"-m {shlex.quote(body_summary)} "
                f"-m {shlex.quote(body_preview)}"
            )
        elif sub in mapping:
            cmd = mapping[sub]
        else:
            msg = "Usage: /git [status|diff|summary|patch|log [N]|branch|stash|remote|commit <msg>]"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        result = _tool_run_command({"command": cmd, "policy": policy})
        if not result.get("success"):
            self.context.console.print(f"[red]{result.get('error', 'Command failed')}[/red]" if self.context.has_rich
                          else result.get("error", "Command failed"))
            return
        data = result.get("data", {})
        out = (data.get("stdout", "") + ("\n" + data.get("stderr", "") if data.get("stderr") else "")).strip()
        if out:
            if self.context.has_rich:
                self.context.console.print(_get_Syntax()(out, "text", theme=_get__SYNTAX_THEME()))
            else:
                print(out)

    def cmd_gh(self, args: str):
        """GitHub CLI helper — prs | issues | pr N | issue N | search | create-pr | diff N | checks N"""
        raw = args.strip()
        if not raw or raw in ("help", "--help"):
            lines = [
                "Usage: /gh <command>",
                "  prs            List open pull requests",
                "  issues         List open issues",
                "  pr <N>         View pull request #N",
                "  issue <N>      View issue #N",
                "  diff <N>       Show PR #N diff",
                "  checks <N>     Show PR #N CI checks",
                "  search <q>     Search code in this repo",
                "  create-pr      Create a PR (follow prompts)",
                "  commits [N]    Show last N commits (default 10)",
            ]
            for ln in lines:
                self.context.console.print(f"  [dim]{ln}[/dim]" if self.context.has_rich else ln)
            return

        parts = raw.split(maxsplit=1)
        sub = parts[0].lower()
        subarg = parts[1].strip() if len(parts) > 1 else ""

        def _run(action: str, extra: dict = None):
            p = {"action": action}
            if extra:
                p.update(extra)
            r = _tool_github(p)
            if not r.get("success"):
                msg = r.get("error", "GitHub command failed")
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return
            data = r.get("data", {})
            out = data.get("stdout", "") if isinstance(data, dict) else str(data)
            if out.strip():
                if self.context.has_rich:
                    try:
                        import json as _jj
                        parsed = _jj.loads(out)
                        from rich.pretty import pprint as _pp
                        _pp(parsed, expand_all=False)
                    except Exception:
                        self.context.console.print(_get_Syntax()(out, "text", theme=_get__SYNTAX_THEME()))
                else:
                    print(out)

        if sub in ("prs", "pr_list"):
            _run("list_prs")
        elif sub in ("issues", "issue_list"):
            _run("list_issues")
        elif sub == "pr" and subarg.isdigit():
            _run("view_pr", {"number": int(subarg)})
        elif sub == "issue" and subarg.isdigit():
            _run("view_issue", {"number": int(subarg)})
        elif sub == "diff" and subarg.isdigit():
            _run("pr_diff", {"number": int(subarg)})
        elif sub == "checks" and subarg.isdigit():
            _run("pr_checks", {"number": int(subarg)})
        elif sub in ("commits", "log"):
            n = int(subarg) if subarg.isdigit() else 10
            _run("list_commits", {"limit": n})
        elif sub == "search":
            if not subarg:
                self.context.console.print("[dim]Usage: /gh search <query>[/dim]" if self.context.has_rich else "Usage: /gh search <query>")
                return
            _run("search", {"q": subarg, "kind": "code"})
        elif sub in ("create-pr", "createpr", "create_pr"):
            try:
                title = (self.context.console.input("  PR title: ") if self.context.has_rich else input("  PR title: ")).strip()
                body = (self.context.console.input("  PR body (optional): ") if self.context.has_rich else input("  PR body (optional): ")).strip()
                base = (self.context.console.input("  Base branch [main]: ") if self.context.has_rich else input("  Base branch [main]: ")).strip() or "main"
                _run("create_pr", {"title": title, "body": body, "base": base})
            except (EOFError, KeyboardInterrupt):
                self.context.console.print("[dim]Cancelled[/dim]" if self.context.has_rich else "Cancelled")
        else:
            self.context.console.print(f"[dim]Unknown /gh sub-command: {sub}. Try /gh help[/dim]" if self.context.has_rich
                          else f"Unknown /gh sub-command: {sub}. Try /gh help")

    def cmd_lsp(self, args: str):
        """Language-server diagnostics for code files.

        /lsp                — show which language servers are installed
        /lsp <file>         — run diagnostics (errors/warnings) on a file
        """
        try:
            from runtime.lsp import available_servers, server_for, get_diagnostics
        except ImportError as _e:
            self.context.console.print(f"[red]runtime.lsp not available: {_e}[/red]") if self.context.has_rich else print(f"Error: {_e}")
            return

        target = args.strip().strip('"\'')

        # No arg or "status" → show installed servers
        if not target or target.lower() == "status":
            servers = available_servers()
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [bold]Language Servers[/bold]  [dim]on-demand diagnostics[/dim]")
                self.context.console.print()
                _hints = {
                    "pylsp": "pip install 'python-lsp-server[all]'",
                    "typescript-language-server": "npm i -g typescript-language-server typescript",
                }
                for exe, installed in servers.items():
                    if installed:
                        self.context.console.print(f"  [green]●[/green] [bold]{exe}[/bold]  [dim]installed[/dim]")
                    else:
                        self.context.console.print(f"  [dim]○ {exe}  not installed — {_hints.get(exe, '')}[/dim]")
                self.context.console.print()
                self.context.console.print("  [dim]/lsp <file>  — run diagnostics on a file[/dim]")
                self.context.console.print()
            else:
                print("\n  Language Servers:")
                for exe, installed in servers.items():
                    print(f"  {'OK' if installed else '--'}  {exe}")
            return

        # File path → run diagnostics
        import pathlib
        p = pathlib.Path(target).expanduser()
        if not p.exists():
            self.context.console.print(f"[red]File not found: {p}[/red]" if self.context.has_rich else f"File not found: {p}")
            return
        if not server_for(p):
            msg = f"No language server installed for '{p.suffix}' files"
            self.context.console.print(f"[yellow]{msg}[/yellow]" if self.context.has_rich else msg)
            return

        if self.context.has_rich:
            with self.context.console.status(f"[dim]Analyzing {p.name}…[/dim]", spinner="dots"):
                diags = get_diagnostics(p)
        else:
            print(f"  Analyzing {p.name}…")
            diags = get_diagnostics(p)

        if not diags:
            self.context.console.print(f"[green]✓ No diagnostics — {p.name} is clean[/green]" if self.context.has_rich
                          else f"No diagnostics for {p.name}")
            return

        errors = sum(1 for d in diags if d["severity"] == "error")
        warnings = sum(1 for d in diags if d["severity"] == "warning")
        if self.context.has_rich:
            from rich.table import Table
            from rich import box as _rbox
            self.context.console.print()
            self.context.console.print(f"  [bold]{p.name}[/bold]  "
                          f"[red]{errors} error(s)[/red] · [yellow]{warnings} warning(s)[/yellow]")
            self.context.console.print()
            t = Table(box=_rbox.SIMPLE, padding=(0, 1), show_header=True)
            t.add_column("Loc", style="dim", justify="right", min_width=7)
            t.add_column("Severity", min_width=8)
            t.add_column("Message")
            _sev_color = {"error": "red", "warning": "yellow", "info": "cyan", "hint": "dim"}
            for d in diags[:40]:
                color = _sev_color.get(d["severity"], "white")
                src = f" [dim]({d['source']})[/dim]" if d["source"] else ""
                t.add_row(
                    f"{d['line']}:{d['col']}",
                    f"[{color}]{d['severity']}[/{color}]",
                    f"{d['message']}{src}",
                )
            self.context.console.print(t)
            self.context.console.print()
        else:
            print(f"\n  {p.name}: {errors} errors, {warnings} warnings")
            for d in diags[:40]:
                print(f"  {d['line']}:{d['col']}  {d['severity']:8s} {d['message']}")

    def cmd_completions(self, args: str):
        """Generate shell completion script for aria-code slash commands.

        /completions           — show instructions
        /completions bash      — output bash completion snippet
        /completions zsh       — output zsh completion snippet
        /completions install   — write and source the completion script
        """
        # NOTE: this method's __globals__ are rebound to aria_cli's namespace by
        # _rebind_mixin_globals(), so module-level names from this file (Path and
        # the _build_*/_detect_user_shell helpers) must be imported locally.
        from pathlib import Path
        from apps.cli.commands.ops_cmds import (
            _build_bash_completion, _build_zsh_completion, _detect_user_shell,
        )

        shell = args.strip().lower() or "show"

        # Build sorted slash command list from VISIBLE_SLASH_COMMANDS
        try:
            from apps.cli.commands.catalog import VISIBLE_SLASH_COMMANDS
            cmds = sorted(VISIBLE_SLASH_COMMANDS)
        except ImportError:
            cmds = ["/help", "/model", "/config", "/recall", "/permissions", "/deep", "/quote"]

        if shell == "bash":
            script = _build_bash_completion(cmds)
            self.context.console.print(script if not self.context.has_rich else f"[dim]{script}[/dim]")
            return

        if shell == "zsh":
            script = _build_zsh_completion(cmds)
            self.context.console.print(script if not self.context.has_rich else f"[dim]{script}[/dim]")
            return

        if shell == "install":
            import subprocess as _sp
            import shutil as _sh
            detected = _detect_user_shell()
            if detected == "zsh":
                script = _build_zsh_completion(cmds)
                target = Path.home() / ".zsh" / "_aria-code"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(script)
                rc = Path.home() / ".zshrc"
                fpath_line = f'fpath=("{target.parent}" $fpath)'
                if rc.exists():
                    existing = rc.read_text()
                    if str(target.parent) not in existing:
                        rc.write_text(existing.rstrip("\n") + f"\n{fpath_line}\nautoload -U compinit && compinit\n")
                msg = f"Zsh completion installed → {target}\nRestart your shell or run: source ~/.zshrc"
            else:
                script = _build_bash_completion(cmds)
                target = Path.home() / ".bash_completion.d" / "aria-code"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(script)
                rc = Path.home() / ".bashrc"
                source_line = f'[ -f "{target}" ] && source "{target}"'
                if rc.exists():
                    existing = rc.read_text()
                    if str(target) not in existing:
                        rc.write_text(existing.rstrip("\n") + f"\n{source_line}\n")
                msg = f"Bash completion installed → {target}\nRestart your shell or run: source ~/.bashrc"
            if self.context.has_rich:
                self.context.console.print(f"[green]✓[/green]  {msg}")
            else:
                print(msg)
            return

        # Default: instructions
        detected = _detect_user_shell()
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]Shell Completions[/bold]  [dim]aria-code slash commands[/dim]")
            self.context.console.print()
            self.context.console.print(f"  Detected shell: [bold]{detected}[/bold]")
            self.context.console.print()
            self.context.console.print("  [dim]/completions bash     — print bash completion script[/dim]")
            self.context.console.print("  [dim]/completions zsh      — print zsh completion script[/dim]")
            self.context.console.print("  [dim]/completions install  — auto-install for your shell[/dim]")
            self.context.console.print()
            self.context.console.print(f"  [dim]{len(cmds)} slash commands registered[/dim]")
            self.context.console.print()
        else:
            print(f"\n  Shell: {detected}  |  {len(cmds)} commands registered")
            print("  /completions bash|zsh|install")


def _detect_user_shell() -> str:
    import os as _os
    shell = _os.environ.get("SHELL", "").lower()
    if "zsh" in shell:
        return "zsh"
    if "fish" in shell:
        return "fish"
    return "bash"


def _build_bash_completion(cmds: list) -> str:
    cmd_list = " ".join(cmds)
    return f"""# aria-code bash completion
# Add to ~/.bashrc:  source ~/.bash_completion.d/aria-code
_aria_code_complete() {{
    local cur prev words
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    # Complete slash commands when passed via -p flag
    if [[ "${{cur}}" == /* ]]; then
        COMPREPLY=( $(compgen -W "{cmd_list}" -- "${{cur}}") )
        return 0
    fi
    # Complete top-level flags
    COMPREPLY=( $(compgen -W "--help --version --print -p --no-color" -- "${{cur}}") )
}}
complete -F _aria_code_complete aria-code
complete -F _aria_code_complete aria_cli.py
"""


def _build_zsh_completion(cmds: list) -> str:
    lines = ["#compdef aria-code aria_cli.py", "", "_aria_code() {", "  local -a slash_cmds", "  slash_cmds=("]
    for cmd in cmds:
        lines.append(f"    '{cmd}'")
    lines += [
        "  )",
        "  _arguments \\",
        "    '(-h --help)'{-h,--help}'[show help]' \\",
        "    '(-v --version)'{-v,--version}'[show version]' \\",
        "    '(-p --print)'{-p,--print}'[non-interactive mode]:prompt:->prompt' \\",
        "    '*: :->args'",
        "  case $state in",
        "    prompt|args)",
        "      if [[ $PREFIX == /* ]]; then",
        "        _values 'slash command' $slash_cmds",
        "      fi",
        "      ;;",
        "  esac",
        "}",
        "",
        "_aria_code",
    ]
    return "\n".join(lines) + "\n"
