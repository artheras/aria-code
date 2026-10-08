"""Write/edit tool implementations extracted from aria_cli.py.

These functions depend on aria_cli globals (GLOBAL_CHANGE_STORE, console, etc.).
They use lazy runtime imports to avoid circular imports at load time — aria_cli
is already fully initialised by the time any tool executes.
"""
from __future__ import annotations

import pathlib
import re as _re
import stat as _stat


# ── Lazy access to aria_cli singletons ───────────────────────────────────────

def _ac():
    """Return the aria_cli module (already loaded, never reimported from scratch)."""
    from aria_code import aria_cli
    return aria_cli


def _ui():
    """Return (console, HAS_RICH) from the live aria_cli namespace."""
    m = _ac()
    return getattr(m, "console", None), getattr(m, "HAS_RICH", False)


def _change_store():
    return _ac().GLOBAL_CHANGE_STORE


def _verify_python_syntax(path: "pathlib.Path", content: str) -> "str | None":
    """Compile-check a Python file after a write/edit.

    Returns a short LLM-actionable error string if the file no longer parses,
    or None if it is fine / not a Python file. This mirrors Claude Code's
    edit→verify discipline so syntax breakage is caught immediately instead of
    at the next run.
    """
    if path.suffix != ".py":
        return None
    try:
        compile(content, str(path), "exec")
        return None
    except SyntaxError as exc:
        line = exc.lineno or "?"
        msg = exc.msg or "syntax error"
        # Show the offending line for context
        ctx = ""
        try:
            lines = content.splitlines()
            if isinstance(exc.lineno, int) and 1 <= exc.lineno <= len(lines):
                ctx = f"\n  → 第 {line} 行: {lines[exc.lineno - 1].strip()[:120]}"
        except Exception:
            pass
        return (
            f"⚠ 语法检查失败 (SyntaxError: {msg} @ line {line}){ctx}\n"
            f"改动已写入，但文件无法编译。请用 read_file 查看该行附近，再用 edit_file 修复语法。"
        )
    except Exception:
        # Non-syntax compile issues (e.g. null bytes) — don't block, just skip
        return None


def _write_policy():
    return _ac()._ACTIVE_WRITE_POLICY


def _lsp_autocheck(path: "pathlib.Path") -> "tuple[str | None, list]":
    """Optionally run language-server diagnostics after a write/edit.

    Gated on config flag `lsp_autocheck` (default off) so the common edit path
    pays zero cost. When on, this surfaces real diagnostics — undefined names,
    type errors, unused imports — that the syntax compile-check can't catch, the
    way Claude Code injects LSP feedback after edits. Returns (summary, diags);
    summary is None when there's nothing to report. Never raises.
    """
    try:
        if not _ac()._ACTIVE_LSP_AUTOCHECK[0]:
            return None, []
    except Exception:
        return None, []
    try:
        from aria_code.runtime.lsp import server_for, get_diagnostics
        if not server_for(path):
            return None, []
        diags = get_diagnostics(path, timeout=6.0)
    except Exception:
        return None, []
    if not diags:
        return None, []
    errors = sum(1 for d in diags if d["severity"] == "error")
    warnings = sum(1 for d in diags if d["severity"] == "warning")
    if errors == 0 and warnings == 0:
        return None, diags
    head = diags[0]
    summary = (
        f"LSP 诊断: {errors} error / {warnings} warning. "
        f"首条 — 第 {head['line']} 行: {head['message'][:120]}"
    )
    return summary, diags


def _print_lsp_diags(diags: list, console, has_rich: bool, limit: int = 6) -> None:
    """Print a compact diagnostics list after an auto-checked edit."""
    if not (diags and console):
        return
    _color = {"error": "red", "warning": "yellow", "info": "cyan", "hint": "dim"}
    for d in diags[:limit]:
        if has_rich:
            c = _color.get(d["severity"], "white")
            console.print(f"  [{c}]●[/{c}] [dim]{d['line']}:{d['col']}[/dim] "
                          f"{d['message'][:100]}")
        else:
            print(f"  {d['line']}:{d['col']} {d['severity']}: {d['message'][:100]}")
    if len(diags) > limit:
        extra = len(diags) - limit
        console.print(f"  [dim]… +{extra} more[/dim]") if has_rich else print(f"  ... +{extra} more")


def _is_safe(p: pathlib.Path, params: dict | None = None) -> bool:
    if params and params.get("_workspace"):
        from aria_code.workspace.files import WorkspaceSecurity
        return WorkspaceSecurity.from_tool_params(params).is_safe_path(p, write=True)
    return _ac()._is_safe_path(p)


def _config_dir() -> str:
    return str(_ac().CONFIG_DIR)


def _sessions_dir() -> str:
    return str(_ac().SESSIONS_DIR)


def _ChangeConflictError():
    return _ac().ChangeConflictError


def _record_checkpoint(change, params: dict, *, existed_before: bool, before_mode: "int | None") -> tuple:
    """Persist an applied change without making checkpoint I/O break the write."""
    if params.get("_disable_checkpoints"):
        return None, None
    run_id = params.get("_run_id")
    session_id = str(params.get("_session_id") or "")
    if not run_id and not session_id:
        return None, None
    try:
        from aria_code.runtime.checkpoints import CheckpointStore
        checkpoint = CheckpointStore().record_change(
            path=change.path,
            before_content=change.before_content,
            after_content=change.after_content,
            existed_before=existed_before,
            before_mode=before_mode,
            source=change.source,
            run_id=run_id,
            session_id=session_id,
            label=pathlib.Path(change.path).name,
            metadata={"change_id": change.change_id},
        )
        return checkpoint.checkpoint_id, None
    except Exception as exc:
        return None, str(exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_markdown_fences(content: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap around file content."""
    stripped = content.strip()
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl >= 0:
            stripped = stripped[first_nl + 1:]
        else:
            return content
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3].rstrip()
    if stripped != content.strip():
        return stripped + "\n"
    return content


def _auto_fix_python(content: str, path: str) -> str:
    """Auto-inject missing imports and validate syntax for Python files."""
    if not path.endswith(".py"):
        return content

    lines = content.split("\n")
    imports_present: set[str] = set()
    first_non_comment = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith('"""') and not s.startswith("'''"):
            first_non_comment = i
            break

    for line in lines:
        s = line.strip()
        if s.startswith("import "):
            parts = s.split()
            if len(parts) >= 2:
                imports_present.add(parts[1].split(".")[0].split(",")[0])
        elif s.startswith("from "):
            parts = s.split()
            if len(parts) >= 2:
                imports_present.add(parts[1].split(".")[0])

    code = content
    needed: list[str] = []

    if ("os.path" in code or "os.expanduser" in code or "os.getcwd" in code
            or "os.makedirs" in code) and "os" not in imports_present:
        needed.append("import os")
    if ("sys." in code or "sys.exit" in code) and "sys" not in imports_present:
        needed.append("import sys")
    if "np." in code and "numpy" not in imports_present and "np" not in imports_present:
        needed.append("import numpy as np")
    if "pd." in code and "pandas" not in imports_present and "pd" not in imports_present:
        needed.append("import pandas as pd")
    if "yf." in code and "yfinance" not in imports_present and "yf" not in imports_present:
        needed.append("import yfinance as yf")

    has_plt = "plt." in code
    has_matplotlib_use = "matplotlib.use" in code
    if has_plt and "matplotlib" not in imports_present:
        needed.append("import matplotlib; matplotlib.use('Agg')")
        needed.append("import matplotlib.pyplot as plt")
    elif has_plt and not has_matplotlib_use:
        for i, line in enumerate(lines):
            if "import matplotlib.pyplot" in line and "matplotlib.use" not in "\n".join(lines[:i]):
                lines.insert(i, "import matplotlib; matplotlib.use('Agg')")
                content = "\n".join(lines)
                break

    if "mpf." in code and "mplfinance" not in imports_present and "mpf" not in imports_present:
        needed.append("import mplfinance as mpf")
    if "re." in code and "re" not in imports_present:
        needed.append("import re")
    if "json." in code and "json" not in imports_present:
        needed.append("import json")
    if "datetime" in code and "datetime" not in imports_present:
        needed.append("from datetime import datetime, timedelta")
    if (_re.search(r'\bta\.(?:sma|ema|rsi|macd|bbands|stoch|atr|adx|obv|vwap)\b', code)
            or "pandas_ta" in code) and "ta" not in imports_present and "pandas_ta" not in imports_present:
        needed.append("import pandas_ta as ta")
    if (_re.search(r'\bgo\.(?:Figure|Candlestick|Scatter|Bar|Heatmap|Layout|Table)', code)
            or "px." in code or "plotly" in code) and "plotly" not in imports_present:
        if "go.Figure" in code or "go.Candlestick" in code:
            needed.append("import plotly.graph_objects as go")
        if "px." in code:
            needed.append("import plotly.express as px")
        if "make_subplots" in code:
            needed.append("from plotly.subplots import make_subplots")
    if "scipy" in code and "scipy" not in imports_present:
        needed.append("import scipy")

    has_warnings_in_needed = any("warnings" in n for n in needed)
    if ("yf." in code or "pd." in code) and "warnings" not in imports_present and not has_warnings_in_needed:
        needed.insert(0, "import warnings; warnings.filterwarnings('ignore')")
    elif "warnings" in code and "warnings" not in imports_present and not has_warnings_in_needed:
        needed.append("import warnings")

    if needed:
        for imp in reversed(needed):
            lines.insert(first_non_comment, imp)
        content = "\n".join(lines)

    try:
        import ast
        ast.parse(content)
    except SyntaxError as e:
        console, has_rich = _ui()
        if has_rich and console:
            console.print(f"  [dim]Warning: syntax issue at line {e.lineno}: {e.msg}[/dim]")

    return content


def _write_policy_confirm(p: pathlib.Path, content: str, existed: bool) -> tuple:
    """Prompt user to confirm a write. Returns (approved: bool, final_path: Path)."""
    import difflib
    console, has_rich = _ui()
    lines_new = content.count("\n") + 1
    desktop = pathlib.Path.home() / "Desktop"
    is_desktop = str(p).startswith(str(desktop))

    if has_rich and console:
        console.print()
        if existed:
            old_content = p.read_text(errors="replace")
            diff = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"current/{p.name}",
                tofile=f"new/{p.name}",
                n=2,
            ))
            added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            console.print(f"  [yellow]⚠ Overwrite[/yellow]  [bold]{p}[/bold]")
            console.print(f"  [dim]  +{added} lines  -{removed} lines  ({lines_new} total)[/dim]")
            for line in diff[:8]:
                if line.startswith("+") and not line.startswith("+++"):
                    console.print(f"  [green]{line.rstrip()}[/green]")
                elif line.startswith("-") and not line.startswith("---"):
                    console.print(f"  [red]{line.rstrip()}[/red]")
        else:
            loc = "[dim cyan](Desktop)[/dim cyan]" if is_desktop else "[yellow](outside Desktop)[/yellow]"
            console.print(f"  [cyan]New file[/cyan] {loc}  [bold]{p}[/bold]  ({lines_new} lines)")
        console.print()
        choice = console.input("  [bold]Write this file?[/bold] [dim]\\[y/n/r=redirect path][/dim] ").strip().lower()
    else:
        print()
        print(f"  {'Overwrite' if existed else 'New file'}: {p}  ({lines_new} lines)")
        choice = input("  Write this file? [y/n/r=redirect path] ").strip().lower()

    if choice == "r":
        if has_rich and console:
            new_path_str = console.input("  [dim]Enter new path: [/dim]").strip()
        else:
            new_path_str = input("  Enter new path: ").strip()
        if new_path_str:
            new_p = pathlib.Path(new_path_str).expanduser().resolve()
            if _is_safe(new_p):
                return True, new_p
            if has_rich and console:
                console.print(f"  [red]Path not allowed: {new_p}[/red]")
            else:
                print(f"  Path not allowed: {new_p}")
        return False, p

    return choice in ("y", "yes", ""), p


# ── Rich diff helper ─────────────────────────────────────────────────────────

def _print_inline_diff(old_str: str, new_str: str, console, max_lines: int = 12) -> None:
    """Print a compact color-coded inline diff after an edit_file call."""
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()

    total_changed = max(len(old_lines), len(new_lines))
    if total_changed > max_lines:
        console.print(f"  [dim]  (diff too large to display inline — {total_changed} lines)[/dim]")
        return

    for line in old_lines:
        display = line[:120]
        console.print(f"  [red dim]-  {display}[/red dim]")
    for line in new_lines:
        display = line[:120]
        console.print(f"  [green dim]+  {display}[/green dim]")


# ── Public tool functions ─────────────────────────────────────────────────────

def _attach_verification_hint(result_data: dict, path, console=None, has_rich: bool = False) -> None:
    """Note the checks this change calls for, on the tool result.

    One function instead of three pasted copies. The copies differed only in
    what the result dict and the console were called in each enclosing scope,
    and papered over that with ``_wdata if "_wdata" in locals() else _data``
    plus ``locals().get("console", locals().get("_console2"))``. Both names are
    compiled unconditionally, so whichever was absent from a given function was
    an undefined reference — three F821s that only avoided raising because a
    bare ``except Exception: pass`` sat around them, which would equally have
    hidden a real failure. Passing them in makes the dependency visible and
    checkable.

    Advisory only: the acceptance gate is what actually runs these commands
    after the turn. This tells the model what is coming, so it can factor the
    check into what it does next rather than being surprised by it.
    """
    try:
        import os

        from aria_code.workspace.verify import VerificationPlanner

        plan = VerificationPlanner(root=os.getcwd()).infer([str(path)])
    except Exception:
        # Planning is a convenience; a successful write must not be downgraded
        # because the planner is unavailable.
        return
    if not plan.commands:
        return

    joined = " && ".join(plan.commands)
    result_data["suggested_verification"] = (
        f"Verification recommended ({plan.reason}): run `{joined}`"
    )
    try:
        shown = joined.replace(str(path), str(pathlib.Path(path).relative_to(pathlib.Path(os.getcwd()).resolve())))
    except ValueError:
        shown = joined
    if has_rich and console is not None:
        try:
            console.print(f"  [cyan]✓ {'推荐验证命令' if _ui_zh() else 'Suggested check'}: {shown}[/cyan]")
        except Exception:
            pass


def tool_write_file(params: dict) -> dict:
    """Write content to a file (create or overwrite)."""
    path = params.get("path", "")
    content = params.get("content", "")
    skip_confirm = params.get("_skip_confirm", False)
    stage_only = bool(params.get("stage_only", False))

    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    if not content:
        return {"success": False, "error": "Missing 'content' parameter"}

    content = _strip_markdown_fences(content)
    stripped_check = content.strip()

    if len(stripped_check) < 20:
        return {"success": False,
                "error": f"Content too short ({len(stripped_check)} chars). "
                "You must write the COMPLETE script code, not a placeholder."}

    if (stripped_check.startswith("<") and stripped_check.endswith(">")
            and "\n" not in stripped_check and len(stripped_check) < 200
            and not stripped_check.lower().startswith("<!doctype")
            and not stripped_check.lower().startswith("<html")):
        return {"success": False,
                "error": f"Content appears to be a placeholder tag: '{stripped_check[:120]}'. "
                "Write the complete code with imports, data fetching, computation, and output."}

    # Reject trivial stub Python scripts — only print() with no real logic
    if path.endswith(".py"):
        import re as _re
        _boilerplate = {
            "#!/usr/bin/env python", "#!/usr/bin/python", "# -*- coding:",
            'if __name__ == "__main__":', "if __name__ == '__main__':",
            "def main():", "main()", "",
        }
        _work_lines = [
            ln.strip() for ln in stripped_check.splitlines()
            if ln.strip() and ln.strip() not in _boilerplate
            and not ln.strip().startswith("#")
        ]
        _all_print = _work_lines and all(
            _re.match(r'^print\s*\(', ln) for ln in _work_lines
        )
        if _all_print and len(_work_lines) <= 3:
            return {
                "success": False,
                "error": (
                    f"拒绝写入: '{pathlib.Path(path).name or 'file'}' 只包含 print() 语句，是无意义的占位脚本。"
                    " 请直接用文字输出结果，或者写包含真实逻辑的代码（网络请求、数据处理、计算等）。"
                ),
            }

    content = _auto_fix_python(content, path)

    try:
        raw_path = pathlib.Path(path).expanduser()
        if not raw_path.is_absolute():
            raw_path = _relative_write_base() / raw_path
        p = raw_path.resolve()
        if not _is_safe(p, params):
            return {"success": False, "error": f"Access denied: path '{p}' is outside allowed directories"}

        existed = p.exists()
        desktop = pathlib.Path.home() / "Desktop"
        import tempfile as _tf
        from aria_code.artifacts import user_output_root
        user_root = user_output_root().resolve()
        _auto_trusted_prefixes = (
            str(desktop),
            str(user_root),
            str(pathlib.Path(_tf.gettempdir()).resolve()),
            "/tmp", "/private/tmp", "/private/var/folders",
            _config_dir(), _sessions_dir(),
        )
        is_auto_trusted = any(str(p).startswith(pfx) for pfx in _auto_trusted_prefixes)
        policy = _write_policy()[0]

        needs_confirm = (
            not skip_confirm
            and not is_auto_trusted
            and (
                policy == "always_confirm"
                or policy in ("desktop_only", "confirm_outside")
                or existed
            )
        )

        if needs_confirm:
            approved, p = _write_policy_confirm(p, content, existed)
            if not approved:
                return {"success": False, "error": "Write cancelled by user.",
                        "data": {"cancelled": True}}

        existed = p.exists()
        before_mode = _stat.S_IMODE(p.stat().st_mode) if p.is_file() else None

        console, has_rich = _ui()
        store = _change_store()
        change = store.stage(p, content, source="write_file")
        lines = content.count("\n") + 1
        action = "Updated" if existed else "Created"

        if stage_only:
            label = "Staged update" if existed else "Staged create"
            if has_rich and console:
                console.print(f"  [dim]{label} {p} ({lines} lines, change {change.change_id})[/dim]")
            else:
                print(f"  {label} {p} ({lines} lines, change {change.change_id})")
            return {"success": True, "data": {
                "path": str(p), "action": "staged", "lines": lines,
                "change_id": change.change_id,
                "before_hash": change.before_hash, "after_hash": change.after_hash,
                "diff": change.diff, "staged": True, "applied": False,
            }}

        try:
            applied = store.apply(change.change_id)
        except _ChangeConflictError() as exc:
            return {"success": False, "error": str(exc), "data": {"change_id": change.change_id}}
        checkpoint_id, checkpoint_error = _record_checkpoint(
            applied,
            params,
            existed_before=existed,
            before_mode=before_mode,
        )

        desktop = pathlib.Path.home() / "Desktop"
        is_on_desktop = str(p).startswith(str(desktop))
        # In the project the user is working in, a written file is where they
        # expect it: name it relative to the project, and do not open Finder.
        # Revealing every .py write popped a Finder window per file in a coding
        # session; that, and the "saved to" hint, are for files that land
        # somewhere else (the generated folder).
        try:
            in_project = p.is_relative_to(pathlib.Path.cwd().resolve())
        except (OSError, ValueError):
            in_project = False
        shown = str(p.relative_to(pathlib.Path.cwd().resolve())) if in_project else str(p)

        import platform as _platform
        import subprocess as _sub
        _sys_name = _platform.system()
        if _sys_name == "Darwin":
            _reveal_hint = f'open -R "{p}"'
        elif _sys_name == "Windows":
            _reveal_hint = f'explorer /select,"{p}"'
        else:
            _reveal_hint = f'xdg-open "{p.parent}"'

        if has_rich and console:
            console.print(f"  [dim]{action} [bold]{shown}[/bold] ({lines} lines)[/dim]")
            if not in_project and not is_on_desktop and p.suffix == ".py":
                zh = _ui_zh()
                console.print(
                    f"  [dim]{'提示: 文件保存在' if zh else 'Saved to'} [yellow]{p}[/yellow]\n"
                    f"  {'打开所在目录' if zh else 'Show in folder'}: [cyan]{_reveal_hint}[/cyan][/dim]"
                )
        else:
            print(f"  {action} {shown} ({lines} lines)")

        # Auto-reveal .py/.ipynb strategy files in file manager (non-blocking)
        if p.suffix in (".py", ".ipynb") and not in_project:
            try:
                if _sys_name == "Darwin":
                    _sub.Popen(["open", "-R", str(p)],
                               stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
                elif _sys_name == "Windows":
                    _sub.Popen(["explorer", f"/select,{str(p)}"],
                               stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
            except Exception:
                pass

        try:
            size_bytes = p.stat().st_size
        except Exception:
            size_bytes = len(content.encode("utf-8"))

        _syntax_warn = _verify_python_syntax(p, content)
        _console2, _has_rich2 = _ui()
        if _syntax_warn and _has_rich2 and _console2:
            _console2.print("  [yellow]⚠ 语法检查未通过[/yellow]")

        _wdata = {
            "path":           str(p),
            "absolute_path":  str(p),
            "action":         action.lower(),
            "lines":          lines,
            "size_bytes":     size_bytes,
            "change_id":      applied.change_id,
            "before_hash":    applied.before_hash,
            "after_hash":     applied.after_hash,
            "diff":           applied.diff,
            "staged":         True,
            "applied":        True,
            "user_message":   (f"文件已保存到: {p}  打开所在目录: {_reveal_hint}" if _ui_zh()
                               else f"Saved to {p}  ·  show in folder: {_reveal_hint}"),
        }
        try:
            from aria_code.artifacts import register_existing_artifact

            artifact = register_existing_artifact(
                p,
                topic=str(params.get("artifact_topic") or ""),
                kind=str(params.get("artifact_kind") or ""),
                metadata={"source": "write_file"},
            )
            if artifact is not None:
                _wdata["artifact_registered"] = True
                _wdata["artifact_metadata_path"] = str(artifact.metadata_path)
        except Exception:
            # Artifact indexing is convenience metadata; a successful file
            # write must not be downgraded if indexing is unavailable.
            pass
        if checkpoint_id:
            _wdata["checkpoint_id"] = checkpoint_id
        if checkpoint_error:
            _wdata["checkpoint_error"] = checkpoint_error
        if _syntax_warn:
            _wdata["syntax_check"] = "failed"
            return {"success": True, "data": _wdata, "warning": _syntax_warn}


        _attach_verification_hint(_wdata, p, _console2, _has_rich2)

        _lsp_warn, _lsp_diags = _lsp_autocheck(p)
        if _lsp_warn:
            _print_lsp_diags(_lsp_diags, _console2, _has_rich2)
            _wdata["diagnostics"] = _lsp_diags
            return {"success": True, "data": _wdata, "warning": _lsp_warn}
        return {"success": True, "data": _wdata}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _ui_zh() -> bool:
    """Whether the user's configured UI language is Chinese (tools get no config)."""
    try:
        from aria_code.apps.cli.bootstrap import default_config, runtime_paths
        from aria_code.apps.cli.config_store import load_cli_config

        return str(load_cli_config(runtime_paths(), default_config()).get("ui_lang", "en")).lower().startswith("zh")
    except Exception:
        return False


def _relative_write_base() -> pathlib.Path:
    """Where a relative write_file path lands: the working directory.

    It used to be ~/Documents/Aria Code/generated always, while read_file,
    edit_file and run_command all work in the current directory — so "write
    fx.py and run its tests" wrote the file in one place and ran the tests in
    another, and edit_file could not find what write_file had just made.
    Only a session started in the home directory or at the filesystem root
    keeps the generated folder, so chatting from ~ does not scatter files.
    """
    from aria_code.artifacts import user_generated_dir

    cwd = pathlib.Path.cwd().resolve()
    if cwd in (pathlib.Path.home().resolve(), pathlib.Path(cwd.anchor)):
        return user_generated_dir()
    return cwd


def _find_all(text: str, needle: str) -> list[int]:
    found, at = [], text.find(needle)
    while at >= 0:
        found.append(at)
        at = text.find(needle, at + len(needle))
    return found


def tool_edit_file(params: dict) -> dict:
    """Edit a file by replacing old_string with new_string.

    old_string must match exactly one place unless replace_all is set. This
    replaced the first match whatever the count, so an old_string that also
    appeared earlier in the file (a repeated `return None`, a second
    `except Exception:`) edited the wrong place and reported success.
    multi_edit already refused that.
    """
    path = params.get("path", "")
    old_str = params.get("old_string", params.get("old_str", ""))
    new_str = params.get("new_string", params.get("new_str", ""))
    replace_all = bool(params.get("replace_all", False))
    stage_only = bool(params.get("stage_only", False))

    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    if not old_str:
        return {"success": False, "error": "Missing 'old_string' parameter"}

    try:
        p = pathlib.Path(path).expanduser().resolve()
        if not p.exists():
            return {"success": False, "error": f"File not found: {p}"}
        if not _is_safe(p, params):
            return {"success": False, "error": f"Access denied: path '{p}' is outside allowed directories"}

        content = p.read_text(errors="replace")
        before_mode = _stat.S_IMODE(p.stat().st_mode)
        if content.count(old_str) == 0:
            preview = "\n".join(content.splitlines()[:10])
            return {"success": False,
                    "error": f"old_string not found in file. "
                    f"The file starts with:\n{preview}\n\n"
                    f"HINT: Use read_file to see the actual content, then retry edit_file "
                    f"with the correct old_string. Or use write_file to overwrite the entire file."}
        occurrences = content.count(old_str)
        if occurrences > 1 and not replace_all:
            lines = [content.count("\n", 0, i) + 1 for i in _find_all(content, old_str)][:10]
            return {"success": False,
                    "error": f"old_string matches {occurrences} places (lines {', '.join(map(str, lines))}). "
                    f"No change made. Include more surrounding lines so it matches one place, "
                    f"or pass replace_all=true to change every one."}

        replacements = occurrences if replace_all else 1
        new_content = content.replace(old_str, new_str, -1 if replace_all else 1)
        store = _change_store()
        change = store.stage(p, new_content, source="edit_file")
        added = len(new_str.splitlines())
        removed = len(old_str.splitlines())
        console, has_rich = _ui()

        if stage_only:
            if has_rich and console:
                console.print(f"  [dim]Staged edit {p} (change {change.change_id})[/dim]")
            else:
                print(f"  Staged edit {p} (change {change.change_id})")
            return {"success": True, "data": {
                "path": str(p), "replacements": replacements,
                "lines": new_content.count("\n") + 1,
                "change_id": change.change_id,
                "before_hash": change.before_hash, "after_hash": change.after_hash,
                "diff": change.diff, "staged": True, "applied": False,
            }}

        try:
            applied = store.apply(change.change_id)
        except _ChangeConflictError() as exc:
            return {"success": False, "error": str(exc), "data": {"change_id": change.change_id}}
        checkpoint_id, checkpoint_error = _record_checkpoint(
            applied,
            params,
            existed_before=True,
            before_mode=before_mode,
        )

        if has_rich and console:
            parts = []
            if added > 0:
                parts.append(f"[green]+{added}[/green]")
            if removed > 0:
                parts.append(f"[red]-{removed}[/red]")
            short_path = str(p.name) if len(str(p)) > 60 else str(p)
            console.print(f"  [dim]✎ [bold]{short_path}[/bold]  ({', '.join(parts)} lines)[/dim]")
            _print_inline_diff(old_str, new_str, console)
        else:
            print(f"  Applied (+{added}, -{removed} lines)")

        _syntax_warn = _verify_python_syntax(p, new_content)
        if _syntax_warn and has_rich and console:
            console.print("  [yellow]⚠ 语法检查未通过[/yellow]")

        _data = {
            "path": str(p), "replacements": replacements,
            "lines": new_content.count("\n") + 1,
            "change_id": applied.change_id,
            "before_hash": applied.before_hash, "after_hash": applied.after_hash,
            "diff": applied.diff, "staged": True, "applied": True,
        }
        if checkpoint_id:
            _data["checkpoint_id"] = checkpoint_id
        if checkpoint_error:
            _data["checkpoint_error"] = checkpoint_error
        if _syntax_warn:
            _data["syntax_check"] = "failed"
            return {"success": True, "data": _data, "warning": _syntax_warn}

        # Opt-in LSP diagnostics (catches what the syntax check can't)

        _attach_verification_hint(_data, p, console, has_rich)

        _lsp_warn, _lsp_diags = _lsp_autocheck(p)
        if _lsp_warn:
            _print_lsp_diags(_lsp_diags, console, has_rich)
            _data["diagnostics"] = _lsp_diags
            return {"success": True, "data": _data, "warning": _lsp_warn}
        return {"success": True, "data": _data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_multi_edit(params: dict) -> dict:
    """Apply multiple find/replace edits to a single file atomically.

    All edits must succeed or none are applied (the file is only written once,
    after every edit has been validated against the in-memory content). Edits
    are applied in order, so a later edit can match text introduced by an
    earlier one.
    """
    path = params.get("path", "")
    edits = params.get("edits", [])
    stage_only = bool(params.get("stage_only", False))

    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    if not isinstance(edits, list) or not edits:
        return {"success": False, "error": "Missing 'edits' — expected a non-empty array of "
                                            "{old_string, new_string, replace_all?}"}

    try:
        p = pathlib.Path(path).expanduser().resolve()
        if not p.exists():
            return {"success": False, "error": f"File not found: {p}"}
        if not _is_safe(p, params):
            return {"success": False, "error": f"Access denied: path '{p}' is outside allowed directories"}

        content = p.read_text(errors="replace")
        before_mode = _stat.S_IMODE(p.stat().st_mode)
        working = content
        applied_count = 0
        total_added = 0
        total_removed = 0

        # ── Phase 1: validate + apply every edit in memory (atomic) ───────────
        for i, ed in enumerate(edits):
            if not isinstance(ed, dict):
                return {"success": False, "error": f"edit #{i + 1} is not an object"}
            old_s = ed.get("old_string", ed.get("old_str", ""))
            new_s = ed.get("new_string", ed.get("new_str", ""))
            replace_all = bool(ed.get("replace_all", False))
            if not old_s:
                return {"success": False, "error": f"edit #{i + 1} missing 'old_string'"}
            occurrences = working.count(old_s)
            if occurrences == 0:
                return {"success": False, "error":
                        f"edit #{i + 1}: old_string not found. No edits applied (atomic). "
                        f"HINT: read_file first to copy exact text. old_string was:\n"
                        f"{old_s[:200]}"}
            if occurrences > 1 and not replace_all:
                return {"success": False, "error":
                        f"edit #{i + 1}: old_string matches {occurrences} places — pass "
                        f"replace_all=true to replace all, or make old_string unique."}
            working = working.replace(old_s, new_s, -1 if replace_all else 1)
            applied_count += 1
            total_added += len(new_s.splitlines())
            total_removed += len(old_s.splitlines())

        if working == content:
            return {"success": False, "error": "No changes — edits produced identical content."}

        # ── Phase 2: stage + apply once ───────────────────────────────────────
        store = _change_store()
        change = store.stage(p, working, source="multi_edit")
        console, has_rich = _ui()

        if stage_only:
            return {"success": True, "data": {
                "path": str(p), "edits_applied": applied_count,
                "lines": working.count("\n") + 1,
                "change_id": change.change_id,
                "before_hash": change.before_hash, "after_hash": change.after_hash,
                "diff": change.diff, "staged": True, "applied": False,
            }}

        try:
            applied = store.apply(change.change_id)
        except _ChangeConflictError() as exc:
            return {"success": False, "error": str(exc), "data": {"change_id": change.change_id}}
        checkpoint_id, checkpoint_error = _record_checkpoint(
            applied,
            params,
            existed_before=True,
            before_mode=before_mode,
        )

        if has_rich and console:
            console.print(f"  [dim]Applied {applied_count} edits "
                          f"([green]+{total_added}[/green]/[red]-{total_removed}[/red] lines)[/dim]")
        else:
            print(f"  Applied {applied_count} edits (+{total_added}/-{total_removed} lines)")

        _syntax_warn = _verify_python_syntax(p, working)
        if _syntax_warn and has_rich and console:
            console.print("  [yellow]⚠ 语法检查未通过[/yellow]")

        _data = {
            "path": str(p), "edits_applied": applied_count,
            "lines": working.count("\n") + 1,
            "change_id": applied.change_id,
            "before_hash": applied.before_hash, "after_hash": applied.after_hash,
            "diff": applied.diff, "staged": True, "applied": True,
        }
        if checkpoint_id:
            _data["checkpoint_id"] = checkpoint_id
        if checkpoint_error:
            _data["checkpoint_error"] = checkpoint_error
        if _syntax_warn:
            _data["syntax_check"] = "failed"
            return {"success": True, "data": _data, "warning": _syntax_warn}


        _attach_verification_hint(_data, p, console, has_rich)

        _lsp_warn, _lsp_diags = _lsp_autocheck(p)
        if _lsp_warn:
            _print_lsp_diags(_lsp_diags, console, has_rich)
            _data["diagnostics"] = _lsp_diags
            return {"success": True, "data": _data, "warning": _lsp_warn}
        return {"success": True, "data": _data}
    except Exception as e:
        return {"success": False, "error": str(e)}
