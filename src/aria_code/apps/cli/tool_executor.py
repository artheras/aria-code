
from __future__ import annotations

import json
import asyncio
import pathlib
import subprocess
import os
import re as re_module
import time
import sys
import re
from typing import Dict, Any, Optional
from datetime import datetime

from aria_code.runtime import ApprovalDecision, apply_approval_decision
from aria_code.runtime.tool_policy import check_tool_policy

try:
    from rich import box as rich_box
    from rich.panel import Panel
except ImportError:  # The legacy CLI injects these when Rich is available.
    rich_box = None
    Panel = None

# This module is imported by ``aria_cli`` and its public functions are rebound
# to that module's globals for backward compatibility.  Keep safe defaults so
# the extracted module can still be imported, linted and type-checked on its
# own.  The defaults are deliberately conservative; interactive execution uses
# the values owned by ``aria_cli`` after rebinding.
_ACTIVE_NETWORK_ENABLED = [True]
_ARIA_BOT_MODE = False
_CONFIRM_TOOLS: set[str] = set()
_HAS_JSON_HOOKS = False
_JSON_HOOKS: dict[str, object] = {}
# Real values, not placeholders. These two used to be empty stubs here and were
# defined for real in aria_cli.py, so execute_aria_tool only cached when it ran
# as the rebound copy with aria_cli's globals — and a direct import of it
# silently got a TTL table with nothing in it, i.e. no caching at all. aria_cli
# imports them from here now, so both copies share one cache and one config.
_CACHE_TTL: dict[str, int] = {
    "get_market_data": 30, "get_market_history": 300, "get_crypto_data": 30, "get_forex_data": 30,
    "get_commodities_data": 60, "get_bonds_data": 60, "get_futures_data": 60,
    "get_news": 300, "get_sector_performance": 60, "get_market_overview": 60,
}
_TOOL_CACHE: dict[str, tuple] = {}  # key -> (result, timestamp)
_auto_approve_session = False
_session_always_allow: set[str] = set()
_session_command_prefixes: set[tuple[str, ...]] = set()


def _command_matches_session_prefix(command: str) -> bool:
    return False


def _command_approval_prefix(command: str) -> tuple[str, ...]:
    return ()


def _arrow_select(options, selected: int = 0, title: str = "", **_kwargs) -> int:
    return len(options) - 1


def _fire_json_hook(*args, **kwargs) -> bool:
    return True

from aria_code.apps.cli.tools.system_tools import (
    tool_run_command as _src_run_command,
    tool_web_fetch   as _src_web_fetch,
    tool_github      as _src_github,
)
from aria_code.apps.cli.tools.notebook_tools import (
    tool_glob          as _src_glob,
    tool_notebook_read as _src_notebook_read,
    tool_notebook_edit as _src_notebook_edit,
)
from aria_code.apps.cli.tools.file_tools import (
    tool_read_file   as _src_read_file,
    tool_list_files  as _src_list_files,
    tool_search_code as _src_search_code,
)
from aria_code.apps.cli.tools.market_tools import (
    tool_get_market_data    as _src_get_market_data,
    tool_get_market_history as _src_get_market_history,
    tool_broker_query       as _src_broker_query,
    tool_broker_order       as _src_broker_order,
)




def _g(name):
    from aria_code import aria_cli
    return getattr(aria_cli, name)

def _tool_analyze_file(params: dict) -> dict:
    """Parse & analyse a local document/image (pdf/docx/xlsx/csv/image/…)."""
    from aria_code.file_analysis_tools import tool_analyze_file as _f
    return _f(params)
def _tool_read_file(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/file_tools.py."""
    return _src_read_file(params)
def _strip_markdown_fences(content: str) -> str:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import _strip_markdown_fences as _f
    return _f(content)
def _auto_fix_python(content: str, path: str) -> str:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import _auto_fix_python as _f
    return _f(content, path)
def _write_policy_confirm(p: pathlib.Path, content: str, existed: bool) -> tuple:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import _write_policy_confirm as _f
    return _f(p, content, existed)
def _tool_write_file(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import tool_write_file as _f
    return _f(params)
def _tool_edit_file(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import tool_edit_file as _f
    return _f(params)
def _tool_multi_edit(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/write_tools.py."""
    from aria_code.apps.cli.tools.write_tools import tool_multi_edit as _f
    return _f(params)
def _tool_update_todos(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/todo_tracker.py."""
    from aria_code.apps.cli.todo_tracker import update_todos as _f
    return _f(params)
def _tool_list_files(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/file_tools.py."""
    return _src_list_files(params)
def _tool_search_code(params: dict) -> dict:
    """Thin shim — implementation in apps/cli/tools/file_tools.py."""
    return _src_search_code(params)
def _tool_run_command(params: dict) -> dict:
    """Run a shell command — thin wrapper supplying global defaults."""
    params.setdefault("permission_mode", _g("_ACTIVE_PERMISSION_MODE")[0])
    params.setdefault("network_enabled", _g("_ACTIVE_NETWORK_ENABLED")[0])
    return _src_run_command(params, console=_g("console"), has_rich=_g("HAS_RICH"),
                            quiet=bool(_g("_ACTION_VIEW_ACTIVE")[0]))
def _tool_web_fetch(params: dict) -> dict:
    return _src_web_fetch(params)
def _tool_github(params: dict) -> dict:
    params.setdefault("permission_mode", _g("_ACTIVE_PERMISSION_MODE")[0])
    params.setdefault("network_enabled", _g("_ACTIVE_NETWORK_ENABLED")[0])
    return _src_github(params, console=_g("console"), has_rich=_g("HAS_RICH"))
def _tool_glob(params: dict) -> dict:
    return _src_glob(params)
def _tool_notebook_read(params: dict) -> dict:
    return _src_notebook_read(params)
def _tool_notebook_edit(params: dict) -> dict:
    return _src_notebook_edit(params)
def _tool_broker_query(params: dict) -> dict:
    return _src_broker_query(params)
def _tool_broker_order(params: dict) -> dict:
    return _src_broker_order(params)
def _tool_get_market_data(params: dict) -> dict:
    return _src_get_market_data(params)
def _tool_get_market_history(params: dict) -> dict:
    return _src_get_market_history(params)
def _todo_schema():
    from aria_code.apps.cli.todo_tracker import UPDATE_TODOS_SCHEMA
    return UPDATE_TODOS_SCHEMA
def _wrap_bare_schemas(bare: list) -> list:
    """Convert Anthropic-style {name, description, parameters} schemas into the
    OpenAI {type:function, function:{…}} envelope used by _g("LOCAL_TOOL_SCHEMAS").

    The subagent and LSP modules declare schemas in the bare form; without this
    wrapping their tools execute but are invisible to the model (it is never
    told they exist).
    """
    wrapped = []
    for s in bare or []:
        if "function" in s:          # already enveloped
            wrapped.append(s)
        else:
            wrapped.append({"type": "function", "function": s})
    return wrapped
def _dedup_tool_schemas() -> None:
    """Drop duplicate tool schemas, keeping the LAST occurrence by name.

    Some tools (e.g. web_fetch, get_market_data) are declared both by the
    finance-tools registry and the static schema block. The static block runs
    after and carries the richer description, so keeping the last copy wins.

    Schemas arrive in two shapes: the OpenAI-style ``{"type": "function",
    "function": {...}}`` wrapper and the bare ``{"name", "description",
    "parameters"}`` form that several register_* helpers emit. Reading only
    the wrapped shape gave a bare schema an empty name, and the ``if name``
    guard then dropped it — silently, and only from the schema list, so the
    handler stayed in LOCAL_TOOLS while the model was never told the tool
    existed. analyze_logistics_data, analyze_stripe_data and
    analyze_financial_statements were all unreachable this way: registered,
    callable, and invisible.

    It also normalises, which is why it reads both shapes and writes one.
    Leaving the bare form in the list sends Ollama and the OpenAI-compatible
    providers a schema they reject, and forces every consumer to handle two
    shapes — one test already assumed the wrapper and broke on the other.
    """
    kept: dict = {}
    for schema in _g("LOCAL_TOOL_SCHEMAS"):
        name = (schema.get("function") or schema).get("name", "")
        if not name:
            continue  # malformed: nothing can call it, nothing can describe it
        kept[name] = schema if "function" in schema else {
            "type": "function", "function": schema,
        }
    _g("LOCAL_TOOL_SCHEMAS")[:] = list(kept.values())
def _show_edit_preview(params: dict):
    """Show a diff preview for edit_file (Claude Code style, Panel-boxed)."""
    if _g("_ARIA_BOT_MODE"):
        return
    path = params.get("path", "")
    old_str = params.get("old_string", params.get("old_str", ""))
    new_str = params.get("new_string", params.get("new_str", ""))
    if not path or not old_str:
        return

    p = pathlib.Path(path).expanduser().resolve()
    short = p.name or "file tool"

    if not _g("HAS_RICH"):
        print(f"\n  Edit file  {short}")
        return

    body_parts: list = []
    try:
        content = p.read_text(errors="replace")
        pos = content.find(old_str)
        if pos >= 0:
            line_num = content[:pos].count("\n") + 1
            all_lines = content.splitlines()
            old_lines = old_str.splitlines()
            new_lines = new_str.splitlines()

            # Context before (up to 2 lines)
            ctx_start = max(0, line_num - 3)
            for i in range(ctx_start, line_num - 1):
                if i < len(all_lines):
                    body_parts.append(f"[dim]{i+1:4}  {all_lines[i][:100]}[/dim]")

            # Removed lines
            for i, ol in enumerate(old_lines):
                ln = line_num + i
                body_parts.append(f"[red]{ln:4} -  {ol[:100]}[/red]")

            # Added lines
            for i, nl in enumerate(new_lines):
                ln = line_num + i
                body_parts.append(f"[green]{ln:4} +  {nl[:100]}[/green]")

            # Context after (up to 2 lines)
            after_start = line_num - 1 + len(old_lines)
            for i in range(after_start, min(after_start + 2, len(all_lines))):
                body_parts.append(f"[dim]{i+1:4}  {all_lines[i][:100]}[/dim]")
        else:
            # String not found — fallback to plain diff lines
            for ol in old_str.splitlines()[:6]:
                body_parts.append(f"[red]-  {ol[:100]}[/red]")
            for nl in new_str.splitlines()[:6]:
                body_parts.append(f"[green]+  {nl[:100]}[/green]")
    except Exception:
        for ol in old_str.splitlines()[:6]:
            body_parts.append(f"[red]-  {ol[:100]}[/red]")
        for nl in new_str.splitlines()[:6]:
            body_parts.append(f"[green]+  {nl[:100]}[/green]")

    _g("console").print()
    _g("console").print(Panel(
        "\n".join(body_parts) if body_parts else "[dim](no preview)[/dim]",
        title=f"[yellow]Edit file[/yellow] [dim]{short}[/dim]",
        title_align="left",
        border_style="yellow",
        box=rich_box.ROUNDED,
        padding=(0, 1),
    ))
def _show_multi_edit_preview(params: dict):
    """Show a compact preview of all edits in a multi_edit call."""
    if _g("_ARIA_BOT_MODE"):
        return
    path = params.get("path", "")
    edits = params.get("edits", [])
    if not path or not edits:
        return
    if not _g("HAS_RICH"):
        print(f"\n  Multi-edit {path}  ({len(edits)} edits)")
        return
    body_parts: list = []
    for i, ed in enumerate(edits):
        old_s = ed.get("old_string", ed.get("old_str", "")) if isinstance(ed, dict) else ""
        new_s = ed.get("new_string", ed.get("new_str", "")) if isinstance(ed, dict) else ""
        body_parts.append(f"[bold]#{i+1}[/bold]")
        for ol in old_s.splitlines()[:3]:
            body_parts.append(f"[red]  -  {ol[:96]}[/red]")
        for nl in new_s.splitlines()[:3]:
            body_parts.append(f"[green]  +  {nl[:96]}[/green]")
    _g("console").print()
    _g("console").print(Panel(
        "\n".join(body_parts) if body_parts else "[dim](no preview)[/dim]",
        title=f"[yellow]Multi-edit[/yellow] [dim]{pathlib.Path(path).name} · {len(edits)} edits[/dim]",
        title_align="left",
        border_style="yellow",
        box=rich_box.ROUNDED,
        padding=(0, 1),
    ))
def _show_write_preview(params: dict):
    """Show a content preview for write_file (Claude Code style, Panel-boxed)."""
    if _g("_ARIA_BOT_MODE"):
        return
    path = params.get("path", "")
    content = params.get("content", "")
    if not path:
        return
    # Show cleaned content (without markdown fences)
    content = _strip_markdown_fences(content)

    p = pathlib.Path(path).expanduser().resolve()
    short = p.name or "file tool"

    existed = p.exists()
    action = "Overwrite file" if existed else "Write new file"
    action_color = "yellow" if existed else "green"
    lines = content.count("\n") + 1

    if not _g("HAS_RICH"):
        print(f"\n  {action}  {short} ({lines} lines)")
        return

    if lines > 80:
        body_parts = [
            f"[dim]Large generated file: {lines} lines[/dim]",
            "[dim]Preview suppressed; the full content will be written as an artifact/file.[/dim]",
            "[dim]Use /read after creation if you need to inspect it.[/dim]",
        ]
    else:
        preview_lines = content.splitlines()[:6]
        body_parts = [f"[green]+ {pl[:100]}[/green]" for pl in preview_lines]
        if lines > 6:
            n = lines - 6
            body_parts.append(f"[dim]… +{n} more line{'s' if n != 1 else ''}[/dim]")
    body = "\n".join(body_parts)

    _g("console").print()
    _g("console").print(Panel(
        body,
        title=f"[{action_color}]{action}[/{action_color}] [dim]{short}  ({lines} lines)[/dim]",
        title_align="left",
        border_style=action_color if existed else "dim",
        box=rich_box.ROUNDED,
        padding=(0, 1),
    ))
def _apply_tool_approval(params: dict, decision: ApprovalDecision) -> dict:
    """Apply approval state to CLI globals and execution params."""
    # The two sets are only mutated in place, so only the rebound flag needs this.
    global _auto_approve_session
    if decision.auto_approve_session:
        _auto_approve_session = True
    if decision.tool_scope:
        _session_always_allow.add(decision.tool_scope)
    if decision.command_prefix:
        _session_command_prefixes.add(tuple(decision.command_prefix))
    return apply_approval_decision(params, decision)
_DENY_LABEL = "No, tell Aria what to do instead"


def _deny_help(zh: bool) -> str:
    return "拒绝；可以说明想怎么做，Enter 直接停止" if zh else "decline; say what to do, or Enter to stop"


def _deny_with_feedback(chosen: bool) -> ApprovalDecision:
    """A denial, carrying what the user wants instead when they say it.

    With feedback the turn continues — the model reads it in place of the
    call's result; with none (Enter, or Esc on the menu) it stops, as before.
    """
    if not chosen:
        return ApprovalDecision.deny("user denied")
    from aria_code.apps.cli.tools.write_tools import _ui_zh

    prompt = ("  告诉 Aria 该怎么做（Enter 直接停止）: " if _ui_zh()
              else "  Tell Aria what to do instead (Enter to stop): ")
    try:
        feedback = (_g("console").input(prompt) if _g("HAS_RICH") else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt, OSError):
        feedback = ""
    return ApprovalDecision.deny("user denied", feedback=feedback)


def _assess_for_approval(tool_name: str, params: dict):
    """(assessment, requirement) for an approval; ("ask") if assessment fails."""
    try:
        import os as _os
        from aria_code.safety.risk import approval_requirement, assess_tool

        assessment = assess_tool(tool_name, params, root=_os.getcwd())
        mode = _g("_ACTIVE_APPROVAL_MODE")[0]
        level = _g("_ACTIVE_AUTO_APPROVE_LEVEL")[0]
        return assessment, approval_requirement(assessment, mode=mode, auto_level=level)
    except Exception:
        return None, "ask"


def _show_risk_card(assessment) -> None:
    if assessment is None or _g("_ARIA_BOT_MODE"):
        return
    from aria_code.ui.render.output import print_risk_card

    print_risk_card(_g("console") if _g("HAS_RICH") else None, assessment)


def _note_auto_approval(tool_name: str, assessment) -> None:
    """One dim line, so an approval that did not ask is still visible."""
    if assessment is None or _g("_ARIA_BOT_MODE"):
        return
    text = f"auto-approved · L{assessment.level} {assessment.name} · {assessment.summary or tool_name}"
    if _g("HAS_RICH"):
        _g("console").print(f"  [dim]✓ {text}[/dim]", highlight=False)
    else:
        print(f"  ✓ {text}")


def _confirm_tool_execution_decision(tool_name: str, params: dict,
                                     config_policy: str = None) -> ApprovalDecision:
    """Ask user to confirm before executing a destructive tool.
    Returns a structured approval decision.

    For run_command: pre-flight policy check happens HERE, before showing the
    picker. If the command would be blocked even with user approval (high-risk),
    show error immediately. If medium-risk with 'safe' policy, offer to upgrade
    policy inline so the user can act without leaving the flow.
    """
    if config_policy is None:
        config_policy = _g("_ACTIVE_COMMAND_POLICY")[0]
    # ── Persistent per-tool policy check (allowlist / denylist) ─────────────
    _policy_verdict = check_tool_policy(tool_name)
    if _policy_verdict == "deny":
        if _g("HAS_RICH"):
            _g("console").print(
                f"  [red]✗ 工具 '{tool_name}' 被永久黑名单拒绝[/red]  "
                f"[dim]（/config policy remove {tool_name} 可解除）[/dim]"
            )
        else:
            print(f"  ✗ '{tool_name}' blocked by tool policy")
        return ApprovalDecision.deny("blocked by tool policy (deny list)")

    # ── Risk: what the call touches decides how it is approved ───────────────
    # L4 is asked about every time — a standing "always allow" was given for
    # something milder. Under approval_mode=risk, calls at or below the
    # auto-approve level run without a prompt (see safety.risk).
    _assessment, _requirement = _assess_for_approval(tool_name, params)
    _standing_ok = _requirement != "always"

    if _standing_ok and _policy_verdict == "allow":
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy=config_policy, user_approved=True)
        return ApprovalDecision.allow()

    if _standing_ok and _auto_approve_session:
        # Still inject policy so run_command doesn't re-block
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy=config_policy, user_approved=True)
        return ApprovalDecision.allow()
    if _standing_ok and tool_name == "run_command" and _command_matches_session_prefix(params.get("command", "")):
        return ApprovalDecision.allow(policy="balanced", user_approved=True)
    # Per-tool session allow — user previously chose "Always allow [tool] this session"
    if _standing_ok and tool_name in _session_always_allow:
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy=config_policy, user_approved=True)
        return ApprovalDecision.allow()

    # ── Plan mode: confirm ALL tools (even non-CONFIRM_TOOLS) ────────────────
    if _g("_PLAN_MODE").active and tool_name not in _CONFIRM_TOOLS:
        return _g("_PLAN_MODE").confirm_step(
            tool_name, params,
            console=_g("console"),
            has_rich=_g("HAS_RICH"),
            arrow_select_fn=_arrow_select,
        )
    # Ask-always policy: force confirmation even for normally silent tools
    if _policy_verdict == "ask" and tool_name not in _CONFIRM_TOOLS:
        _CONFIRM_TOOLS.add(tool_name)  # promote for this call only

    if tool_name not in _CONFIRM_TOOLS:
        return ApprovalDecision.allow()

    # ── JSON PreToolUse hook — can block execution ────────────────────────────
    if _HAS_JSON_HOOKS and _JSON_HOOKS.get("PreToolUse"):
        _allowed = _fire_json_hook(
            "PreToolUse", tool=tool_name, params=params, hooks=_JSON_HOOKS,
        )
        if not _allowed:
            return ApprovalDecision.deny("Blocked by PreToolUse hook")

    if _requirement == "auto":
        _note_auto_approval(tool_name, _assessment)
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy="balanced", user_approved=True)
        return ApprovalDecision.allow()

    # ── Pre-flight for run_command ────────────────────────────────────────────
    from aria_code.apps.cli.tools.write_tools import _ui_zh
    zh = _ui_zh()
    if tool_name == "run_command":
        from aria_code.safety import classify_command_risk
        from aria_code.safety.permissions import is_verification_command
        cmd = params.get("command", "")
        if isinstance(cmd, list):
            import shlex as _shlex_tmp
            cmd = _shlex_tmp.join(str(c) for c in cmd)
            params["command"] = cmd
        risk = classify_command_risk(cmd)
        if risk != "high":
            _show_risk_card(_assessment)

        if risk == "high":
            # Always block high-risk regardless of user approval
            if _g("HAS_RICH"):
                _g("console").print(Panel(
                    (f"[red]✗ 高风险命令已拒绝[/red]\n[dim]{cmd[:120]}[/dim]\n"
                     f"[dim]高风险操作（rm -rf / docker / sudo 等）需要在终端手动执行。[/dim]") if zh else
                    (f"[red]✗ High-risk command refused[/red]\n[dim]{cmd[:120]}[/dim]\n"
                     f"[dim]Run high-risk operations (rm -rf, docker, sudo…) yourself in a terminal.[/dim]"),
                    border_style="red", box=rich_box.ROUNDED, padding=(0, 1),
                ))
            else:
                print(f"  ✗ {'高风险命令已拒绝' if zh else 'High-risk command refused'}: {cmd[:80]}")
            return ApprovalDecision.deny("high-risk command")

        # Tests and type checks run under `safe` (see evaluate_command_policy);
        # offering to "upgrade to balanced" for them misstated the policy.
        if risk == "medium" and config_policy == "safe" and not is_verification_command(cmd):
            # Show a richer picker that includes a "Allow & upgrade policy" option
            if _g("HAS_RICH"):
                _g("console").print()
                _g("console").print(Panel(
                    f"[yellow]⚠ {'此命令需要 balanced 策略（当前: safe）' if zh else 'This command needs the balanced policy (current: safe)'}[/yellow]\n"
                    f"[dim]{cmd[:120]}[/dim]",
                    border_style="yellow", box=rich_box.ROUNDED, padding=(0, 1),
                ))
            _prefix = _command_approval_prefix(cmd)
            _prefix_label = " ".join(_prefix)[:72] if _prefix else cmd[:72]
            options = [
                ("Allow once", "仅此次允许（不改变策略）" if zh else "this time only; the policy stays safe"),
                ("Allow similar this session", f"{'本会话允许前缀' if zh else 'this session, for'}: {_prefix_label}"),
                ("Allow & set balanced", "允许并升级策略（本会话有效）" if zh else "and use balanced for this session"),
                (_DENY_LABEL, _deny_help(zh)),
            ]
            # Imported here: aria_cli rebinds this function to its own globals.
            from aria_code.apps.cli.runtime_consumer import approval_subject as _subject
            choice = _arrow_select(options, selected=0, title="",
                                   collapse_to=_subject(tool_name, params),
                                   shortcuts={"y": 0, "a": 1, "b": 2, "n": 3}, numbered=True)
            if choice == 0:
                return ApprovalDecision.allow(policy="balanced", user_approved=True)
            if choice == 1:
                return ApprovalDecision.allow(
                    policy="balanced",
                    user_approved=True,
                    command_prefix=_prefix,
                )
            if choice == 2:
                return ApprovalDecision.allow(
                    policy="balanced",
                    user_approved=True,
                    upgrade_policy=True,
                )
            return _deny_with_feedback(choice == 3)

    # ── Default confirmation for write_file / edit_file / low-risk run ────────
    if tool_name == "edit_file":
        # Resolve a symbol edit now, so the preview shows the real change and
        # the approved params are the ones that run.
        from aria_code.runtime.symbol_edit import resolve_symbol_edit
        resolve_symbol_edit(params)
        _show_edit_preview(params)
    elif tool_name == "multi_edit":
        _show_multi_edit_preview(params)
    elif tool_name == "write_file":
        _show_write_preview(params)
    elif tool_name == "run_command":
        # Header already printed by on_tool_call — just pass through policy
        pass
    if tool_name != "run_command":
        _show_risk_card(_assessment)

    _tool_label = ({"write_file": "写文件", "edit_file": "编辑文件", "multi_edit": "批量编辑", "run_command": "运行命令"}
                   if zh else {"write_file": "file writes", "edit_file": "file edits", "multi_edit": "multi-edits",
                               "run_command": "commands"}).get(tool_name, tool_name)
    _command_prefix = _command_approval_prefix(params.get("command", "")) if tool_name == "run_command" else ()
    _scope_label = (
        f"Always allow {' '.join(_command_prefix)[:64]}"
        if _command_prefix else f"Always allow {_tool_label}"
    )
    _scope_help = (
        ("本会话内允许相同命令前缀" if zh else "for the rest of this session")
        if _command_prefix else (f"本会话内自动允许所有 {_tool_label}" if zh else f"all {_tool_label} this session")
    )
    options = [
        ("Yes",                              ""),
        (_scope_label,                       _scope_help),
        ("Yes, allow all tools",             "本会话内所有工具自动允许" if zh else "every tool, this session"),
        (_DENY_LABEL,                        _deny_help(zh)),
    ]
    # Imported here: aria_cli rebinds this function to its own globals.
    from aria_code.apps.cli.runtime_consumer import approval_subject as _subject
    choice = _arrow_select(options, selected=0, title="",
                           collapse_to=_subject(tool_name, params),
                           shortcuts={"y": 0, "a": 1, "n": 3}, numbered=True)

    if choice == 0:
        if tool_name == "run_command":
            return ApprovalDecision.allow(policy=config_policy, user_approved=True)
        return ApprovalDecision.allow()
    if choice == 1:
        return ApprovalDecision.allow(
            policy=config_policy if tool_name == "run_command" else None,
            user_approved=True,
            command_prefix=_command_prefix,
            tool_scope="" if _command_prefix else tool_name,
        )
    if choice == 2:
        if tool_name == "run_command":
            return ApprovalDecision.allow(
                policy=config_policy,
                user_approved=True,
                auto_approve_session=True,
            )
        return ApprovalDecision.allow(auto_approve_session=True)
    return _deny_with_feedback(choice == 3)
async def execute_aria_tool(base_url: str, tool_name: str, params: dict,
                           timeout: int = 30, auth_token: str = None,
                           max_retries: int = 2) -> dict:
    """Execute an Aria tool via the backend API with auto-retry and TTL cache."""
    # --- Parameter validation before sending to API ---
    _symbol_tools = {
        "get_market_data", "get_risk_metrics", "calculate_factors",
        "get_alpha158_factors", "assess_portfolio_risk",
    }
    _date_tools = {"backtest_strategy", "stress_test_strategy"}

    if tool_name in _symbol_tools and "symbol" in params:
        sym = str(params["symbol"]).strip().upper()
        if not re_module.match(r'^[A-Z0-9.\-/=]{1,12}$', sym):
            return {"success": False, "error": f"Invalid symbol format: '{sym}'"}
        params = {**params, "symbol": sym}

    if tool_name in _date_tools:
        for date_key in ("start_date", "end_date", "start", "end"):
            if date_key in params:
                date_val = str(params[date_key]).strip()
                if not re_module.match(r'^\d{4}-\d{2}-\d{2}$', date_val):
                    return {"success": False, "error": f"Invalid date format for '{date_key}': '{date_val}' (expected YYYY-MM-DD)"}
        # Check chronological order
        start = params.get("start_date") or params.get("start")
        end = params.get("end_date") or params.get("end")
        if start and end and start > end:
            return {"success": False, "error": f"start_date ({start}) must be before end_date ({end})"}

    # Check cache for read-only tools
    ttl = _CACHE_TTL.get(tool_name)
    if ttl:
        cache_key = f"{tool_name}:{json.dumps(params, sort_keys=True)}"
        cached = _TOOL_CACHE.get(cache_key)
        if cached and (time.time() - cached[1]) < ttl:
            return cached[0]
    import aiohttp
    url = f"{base_url}/api/aria/execute-tool"
    payload = {"tool_name": tool_name, "params": params}
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    result = await resp.json()
                    if result.get("success") or attempt >= max_retries:
                        # Cache successful results for read-only tools
                        if result.get("success") and ttl:
                            _TOOL_CACHE[cache_key] = (result, time.time())
                        return result
                    last_error = result.get("error", "Unknown error")
        except Exception as e:
            last_error = str(e)
        if attempt < max_retries:
            await asyncio.sleep(1 * (attempt + 1))  # 1s, 2s backoff
    return {"success": False, "error": f"Failed after {max_retries + 1} attempts: {last_error}"}
# Hard ceiling on one tool result fed back into the model, in characters.
# Individual branches below apply their own caps, but several produced
# unbounded text, and nothing capped the total: a turn could inject tens of
# thousands of characters of tool output into the context with no limit.
DEFAULT_TOOL_RESULT_CHAR_LIMIT = 4000

# Never truncate below this, or a "summary" degrades into an unusable stub.
MIN_TOOL_RESULT_CHAR_LIMIT = 400


def truncate_tool_summary(summary: str, limit: int = DEFAULT_TOOL_RESULT_CHAR_LIMIT) -> str:
    """Clip a tool summary to *limit* characters, saying so explicitly.

    The marker matters: silently dropping the tail makes a model treat a
    partial result as complete.  Head and tail are both kept because the useful
    signal in command output and file content sits at the two ends — an error
    traceback ends at the bottom, a file's structure shows at the top.
    """
    text = summary if isinstance(summary, str) else str(summary)
    limit = max(int(limit or 0), MIN_TOOL_RESULT_CHAR_LIMIT)
    if len(text) <= limit:
        return text
    marker_for = (
        lambda n: f"\n\n… [{n} characters truncated to fit the context budget] …\n\n"
    )
    # The marker counts against the budget, so the returned text never exceeds
    # *limit* — a cap that its own notice can overshoot is not a cap.
    budget = max(limit - len(marker_for(len(text))), MIN_TOOL_RESULT_CHAR_LIMIT // 2)
    head = int(budget * 0.7)
    tail = budget - head
    kept = text[:head] + (text[-tail:] if tail > 0 else "")
    return text[:head] + marker_for(len(text) - len(kept)) + (text[-tail:] if tail > 0 else "")


def _format_tool_summary(
    tool_name: str,
    result: dict,
    *,
    char_limit: int = DEFAULT_TOOL_RESULT_CHAR_LIMIT,
) -> str:
    """Format a tool result into a concise, length-bounded follow-up summary."""
    return truncate_tool_summary(_format_tool_summary_raw(tool_name, result), char_limit)


def _format_tool_summary_raw(tool_name: str, result: dict) -> str:
    """Format tool result into a concise summary for AI follow-up context."""
    if not result.get("success"):
        return f"Error: {_g('_clean_tool_error_message')(result.get('error', 'failed'))}"
    data = result.get("data", {})

    # Local finance tools return their payload at the top level, whereas some
    # remote adapters wrap it in ``data``.  Treat both shapes consistently.
    # Previously a successful local quote became ``{}`` here, leaving the LLM
    # to invent a price/RSI despite the terminal having displayed real data.
    tool_data = data if isinstance(data, dict) and data else result
    if tool_name == "get_market_data":
        symbol = tool_data.get("symbol", "")
        price = tool_data.get("price", tool_data.get("latest_close"))
        change = tool_data.get("change_pct")
        provider = tool_data.get("provider", "unknown")
        as_of = tool_data.get("as_of")
        retrieved_at = tool_data.get("retrieved_at")
        lines = [f"Market data for {symbol} (provider: {provider})"]
        if price is not None:
            currency = tool_data.get("currency", "")
            lines.append(f"Price: {currency} {price}".strip())
        if change is not None:
            lines.append(f"Change: {change}%")
        if as_of:
            lines.append(f"Provider timestamp: {as_of}")
        elif retrieved_at:
            lines.append(f"Retrieved at: {retrieved_at} (provider did not supply trade timestamp)")
        rsi = tool_data.get("rsi")
        if rsi is not None:
            try:
                rsi_value = float(rsi)
                rsi_label = "oversold" if rsi_value <= 30 else ("overbought" if rsi_value >= 70 else "neutral")
                lines.append(f"RSI(14): {rsi_value:.2f} ({rsi_label})")
            except (TypeError, ValueError):
                lines.append("RSI(14): unavailable")
        macd_hist = tool_data.get("macd_hist")
        if macd_hist is not None:
            lines.append(f"MACD histogram: {macd_hist}")
        lines.append("Use only these values. Do not call this quote 'latest' beyond its stated timestamp.")
        return "\n".join(lines)

    if tool_name == "analyze_news":
        news = tool_data.get("news") or []
        provider = tool_data.get("provider", "unknown")
        if not news:
            return (
                f"No relevant news returned (provider: {provider}). "
                "Do not infer news sentiment; state that news evidence is unavailable."
            )
        lines = [f"News evidence (provider: {provider}, items: {len(news)}):"]
        for item in news[:3]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "untitled")[:220]
            published = str(item.get("time") or item.get("published_at") or "time unavailable")
            publisher = str(item.get("publisher") or item.get("source") or "source unavailable")
            lines.append(f"- {title} | {publisher} | {published}")
        return "\n".join(lines)

    if tool_name == "run_command":
        exit_code = data.get("exit_code", -1)
        stdout = data.get("stdout", "").strip()
        stderr = data.get("stderr", "").strip()
        out = f"exit_code={exit_code}"
        if stdout:
            out += f"\nstdout:\n{stdout[:2000]}"
        if stderr and exit_code != 0:
            out += f"\nstderr:\n{stderr[:500]}"
        # Add actionable hints for common errors
        if exit_code != 0:
            combined = (stdout + " " + stderr).lower()
            combined_raw = stdout + " " + stderr
            if "can't open file" in combined or "no such file" in combined:
                out += "\n\nHINT: The file does not exist. You must create it with write_file first, then run it."
            elif "modulenotfounderror" in combined or "no module named" in combined:
                # Extract module name
                import re as _re
                mod_match = _re.search(r"no module named ['\"]?(\w+)", combined)
                mod_name = mod_match.group(1) if mod_match else "<module_name>"
                out += f"\n\nHINT: Module '{mod_name}' is missing. Fix: run_command pip3 install {mod_name}, then run_command python3 to retry."
            elif "nameerror" in combined:
                # Extract the undefined name
                import re as _re
                name_match = _re.search(r"name ['\"](\w+)['\"] is not defined", combined_raw)
                if name_match:
                    missing_name = name_match.group(1)
                    out += (f"\n\nHINT: '{missing_name}' is not defined — you forgot to import it. "
                            f"Use edit_file to add the missing import (e.g., 'import {missing_name}') at the top of the script, then retry.")
                else:
                    out += "\n\nHINT: A variable or module is not defined. Use read_file to check imports, edit_file to add the missing import, then retry."
            elif "syntaxerror" in combined:
                import re as _re
                line_match = _re.search(r"line (\d+)", combined)
                line_hint = f" at line {line_match.group(1)}" if line_match else ""
                out += f"\n\nHINT: Syntax error{line_hint}. Use read_file to see the code, then edit_file to fix the exact line, then retry."
            elif "typeerror" in combined:
                out += "\n\nHINT: Type error — wrong argument types or wrong number of arguments. Use read_file to inspect, edit_file to fix, then retry."
            elif "keyerror" in combined or "indexerror" in combined:
                # Special hint for yfinance MultiIndex KeyError
                if any(col in combined_raw for col in ("'Close'", "'Open'", "'High'", "'Low'", "'Volume'")):
                    out += ("\n\nHINT: yfinance MultiIndex KeyError — yf.download() returns MultiIndex columns "
                            "when downloading multiple tickers. Fix: add `if isinstance(df.columns, pd.MultiIndex): "
                            "df.columns = df.columns.droplevel(1)` right after yf.download(). "
                            "Use edit_file to add this fix, then retry.")
                else:
                    out += "\n\nHINT: Data structure mismatch. Use read_file to check the code logic. The data may have different column names or fewer elements than expected."
            elif "attributeerror" in combined:
                out += "\n\nHINT: Attribute error — the object doesn't have that method/property. Check the library version or API docs. Use read_file then edit_file to fix."
            elif "valueerror" in combined:
                out += "\n\nHINT: Value error — invalid value passed to a function. Use read_file to check the data types and fix with edit_file."
            elif "permission denied" in combined:
                out += "\n\nHINT: Permission denied. Try adding chmod +x, or run with python3 explicitly."
            else:
                out += "\n\nHINT: Script failed. Use read_file to inspect the code, find the error, edit_file to fix it, then run_command to retry. Do NOT give up."
        else:
            # Script succeeded — auto-verify and auto-open output files.
            from aria_code.artifacts import user_generated_dir as _aria_user_generated_dir
            output_dir = _aria_user_generated_dir()
            try:
                recent_files = []
                for ext in ("*.png", "*.html", "*.csv", "*.pdf", "*.xlsx"):
                    for f in output_dir.rglob(ext):
                        if (time.time() - f.stat().st_mtime) < 30:
                            recent_files.append(f)
                # Also detect files mentioned in stdout (e.g., "Saved to /path/to/file.png")
                saved_pattern = re_module.findall(r'(?:saved?\s+(?:to|as|at)|wrote|output|created)[:\s]+([^\s\'"]+\.(?:png|html|csv|pdf))', stdout, re_module.IGNORECASE)
                for sp in saved_pattern:
                    p = pathlib.Path(sp).expanduser().resolve()
                    if p.exists() and p not in recent_files:
                        recent_files.append(p)
                if recent_files:
                    names = [f.name for f in recent_files]
                    out += f"\n\nVerified: output files created: {', '.join(names)}"
                    # Auto-open on macOS (non-blocking)
                    for f in recent_files[:3]:
                        try:
                            subprocess.Popen(["open", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                    if _g("HAS_RICH"):
                        _g("console").print(f"  [dim]Opened {', '.join(names[:3])}[/dim]")
                else:
                    combined_check = (stdout + " " + stderr).lower()
                    if any(kw in combined_check for kw in ("chart", "plot", "figure", "savefig", "save")):
                        out += ("\n\nWARNING: Script ran but no output files detected in the Aria generated output directory. "
                                "Check the save path uses ~/Documents/Aria Code/generated.")
            except Exception:
                pass
        return out
    if tool_name == "write_file":
        _base = f"OK: {data.get('action', 'created')} {data.get('path', '')} ({data.get('lines', 0)} lines)"
        if result.get("warning"):
            _base += f"\n\n{result['warning']}"
        return _base
    if tool_name == "edit_file":
        _base = f"OK: edited {data.get('path', '')} ({data.get('replacements', 0)} replacements)"
        if result.get("warning"):
            _base += f"\n\n{result['warning']}"
        return _base
    if tool_name == "multi_edit":
        _base = f"OK: {data.get('edits_applied', 0)} edits applied to {data.get('path', '')}"
        if result.get("warning"):
            _base += f"\n\n{result['warning']}"
        return _base
    if tool_name == "update_todos":
        _todos = data.get("todos", [])
        _lines = [f"任务进度 {data.get('completed', 0)}/{data.get('total', len(_todos))}:"]
        _mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        for _t in _todos:
            _lines.append(f"  {_mark.get(_t.get('status'), '[ ]')} {_t.get('content', '')}")
        return "\n".join(_lines)
    if tool_name == "read_file":
        content = data.get("content", "")
        return f"OK: {data.get('lines', 0)} lines\n{content[:2000]}"
    if tool_name == "list_files":
        items = data.get("items", [])
        names = [it["name"] if isinstance(it, dict) else str(it) for it in items[:20]]
        return f"OK: {data.get('count', len(items))} items: {', '.join(names)}"
    if tool_name == "search_code":
        matches = data.get("matches", [])
        return f"OK: {len(matches)} matches\n" + "\n".join(str(m)[:200] for m in matches[:10])
    # Remote tools — JSON summary
    return json.dumps(data, ensure_ascii=False)[:2000]

__all__ = ['_g', '_tool_analyze_file', '_tool_read_file', '_strip_markdown_fences', '_auto_fix_python', '_write_policy_confirm', '_tool_write_file', '_tool_edit_file', '_tool_multi_edit', '_tool_update_todos', '_tool_list_files', '_tool_search_code', '_tool_run_command', '_tool_web_fetch', '_tool_github', '_tool_glob', '_tool_notebook_read', '_tool_notebook_edit', '_tool_broker_query', '_tool_broker_order', '_tool_get_market_data', '_tool_get_market_history', '_todo_schema', '_wrap_bare_schemas', '_dedup_tool_schemas', '_show_edit_preview', '_show_multi_edit_preview', '_show_write_preview', '_apply_tool_approval', '_DENY_LABEL', '_deny_help', '_deny_with_feedback', '_assess_for_approval', '_show_risk_card', '_note_auto_approval', '_confirm_tool_execution_decision', 'execute_aria_tool', '_format_tool_summary', '_format_tool_summary_raw', 'truncate_tool_summary', 'DEFAULT_TOOL_RESULT_CHAR_LIMIT', 'MIN_TOOL_RESULT_CHAR_LIMIT']
