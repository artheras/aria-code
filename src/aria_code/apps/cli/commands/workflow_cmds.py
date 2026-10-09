"""WorkflowCommandsMixin — hooks, regen, undo, retry, note, review commands."""

from __future__ import annotations

import logging
import os
import pathlib
from datetime import datetime


logger = logging.getLogger(__name__)


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional

def _run_event_hook(*args, **kwargs):
    from aria_cli import _run_event_hook as fn
    return fn(*args, **kwargs)
def _load_hooks(*args, **kwargs):
    from aria_cli import _load_hooks as fn
    return fn(*args, **kwargs)
# display_path lives in ui.render.output; aria_cli only re-exports it under a
# private alias, so going through aria_cli was a pointless second hop.
from aria_code.ui.render.output import display_path as _display_path
def _get_MODELS():
    from aria_code.apps.cli.model_catalog import MODELS as val
    return val
def _get__HAS_JSON_HOOKS():
    from aria_cli import _HAS_JSON_HOOKS as val
    return val
def _tool_run_command(*args, **kwargs):
    from aria_cli import _tool_run_command as fn
    return fn(*args, **kwargs)
def resolve_model_key(*args, **kwargs):
    from ..model_catalog import resolve_model_key as fn
    return fn(*args, **kwargs)
def _load_project_context(*args, **kwargs):
    from aria_code.apps.cli.helpers import _load_project_context as fn
    return fn(*args, **kwargs)
def _fire_json_hook(*args, **kwargs):
    from aria_cli import _fire_json_hook as fn
    return fn(*args, **kwargs)
def _print_phase(*args, **kwargs):
    from aria_cli import _print_phase as fn
    return fn(*args, **kwargs)
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


class WorkflowCommandsMixin:
    """Mixin: interactive workflow and edit-review commands."""

    def cmd_hooks(self, args: str):
        global _JSON_HOOKS
        hooks_dirs = [
            _get_CONFIG_DIR() / "hooks",
            pathlib.Path.cwd() / ".aria" / "hooks",
        ]
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "reload":
            if _get__HAS_JSON_HOOKS():
                try:
                    _JSON_HOOKS = _load_hooks()
                    n = sum(len(v) for v in _JSON_HOOKS.values())
                    if self.context.has_rich:
                        self.context.console.print(f"  [green]✓[/green] [dim]hooks.json reloaded ({n} entries)[/dim]")
                    else:
                        print(f"  hooks.json reloaded ({n} entries)")
                except Exception as exc:
                    if self.context.has_rich:
                        self.context.console.print(f"  [red]✗ reload failed: {exc}[/red]")
                    else:
                        print(f"  reload failed: {exc}")
            return

        if sub == "list":
            if _get__HAS_JSON_HOOKS():
                try:
                    from apps.cli.hooks import list_hooks as _list_json_hooks
                    _json_rows = _list_json_hooks()
                    if _json_rows:
                        if self.context.has_rich:
                            self.context.console.print()
                            self.context.console.print("  [bold]JSON Hooks[/bold]  [dim](~/.arthera/hooks.json)[/dim]")
                            for r in _json_rows:
                                _block = " [red][blocking][/red]" if r["blocking"] else ""
                                _tool = f"[{r['tool']}]" if r["tool"] != "*" else ""
                                self.context.console.print(
                                    f"  [cyan]{r['event']:<16}[/cyan]{_tool:<14}  "
                                    f"[dim]{r['command']}[/dim]{_block}"
                                )
                        else:
                            for r in _json_rows:
                                print(f"  {r['event']:<16} {r['tool']:<12} {r['command']}")
                except Exception:
                    pass

            found: list[tuple] = []
            for hdir in hooks_dirs:
                if hdir.exists():
                    for f in sorted(hdir.iterdir()):
                        if f.is_file() and not f.name.startswith("."):
                            found.append((str(hdir), f.name, str(f)))
            if not found:
                if self.context.has_rich:
                    self.context.console.print("  [dim]No hooks found.[/dim]")
                    self.context.console.print("  [dim]Hook dirs:[/dim]")
                    for d in hooks_dirs:
                        self.context.console.print(f"    [dim]{_display_path(d, fallback='hook dir')}[/dim]")
                    self.context.console.print("  [dim]Events: prompt_submit  response_done  tool_use  compact[/dim]")
                else:
                    print("No hooks. Dirs:", [str(d) for d in hooks_dirs])
                return
            if self.context.has_rich:
                self.context.console.print()
                for hdir, name, path in found:
                    self.context.console.print(f"  [dim]{name:<28}[/dim]  {_display_path(path, fallback='hook')}")
                self.context.console.print()
            else:
                for hdir, name, path in found:
                    print(f"  {name}  {_display_path(path, fallback='hook')}")

        elif sub == "edit":
            if not rest:
                if _get__HAS_JSON_HOOKS():
                    from apps.cli.hooks import hooks_file_path, create_example_hooks
                    _hpath = hooks_file_path("global")
                    create_example_hooks(_hpath)
                    editor = os.getenv("EDITOR", "nano")
                    try:
                        import subprocess as _sp
                        _sp.run([editor, str(_hpath)])
                        _JSON_HOOKS = _load_hooks()
                    except Exception as exc:
                        if self.context.has_rich:
                            self.context.console.print(f"[red]Could not open editor: {exc}[/red]")
                        else:
                            print(f"Could not open editor: {exc}")
                return
            event = rest
            hdir = _get_CONFIG_DIR() / "hooks"
            hdir.mkdir(parents=True, exist_ok=True)
            script = hdir / f"{event}.sh"
            if not script.exists():
                script.write_text(
                    f"#!/bin/bash\n# Aria hook: {event}\n# "
                    f"Env vars: ARIA_EVENT ARIA_TOOL ARIA_TOOL_PARAMS ARIA_RESPONSE ARIA_SESSION\n\n"
                    f'echo "Hook {event} fired"\n',
                    encoding="utf-8"
                )
                script.chmod(0o755)
            editor = os.getenv("EDITOR", "nano")
            try:
                import subprocess as _sp
                _sp.run([editor, str(script)])
            except Exception as exc:
                self.context.console.print(f"[red]Could not open editor: {exc}[/red]" if self.context.has_rich else str(exc))

        elif sub == "run":
            event = rest or "ResponseDone"
            if _get__HAS_JSON_HOOKS():
                _fire_json_hook(event, session_id=getattr(self.terminal, "session_id", ""), hooks=_JSON_HOOKS)
            _run_event_hook(event, {"ARIA_EVENT": event, "ARIA_SESSION": getattr(self.terminal, "session_id", "")})
            if self.context.has_rich:
                self.context.console.print(f"  [dim]Hook '{event}' triggered[/dim]")
            else:
                print(f"Hook '{event}' triggered")

        else:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /hooks list|edit [event]|reload|run [event][/dim]")
            else:
                print("Usage: /hooks list|edit [event]|reload|run [event]")

    async def cmd_regen(self, args: str):
        last_user_msg = None
        for i in range(len(self.terminal.conversation) - 1, -1, -1):
            if self.terminal.conversation[i]["role"] == "assistant":
                self.terminal.conversation.pop(i)
                break
        for msg in reversed(self.terminal.conversation):
            if msg["role"] == "user":
                last_user_msg = msg["content"]
                break
        if last_user_msg:
            for i in range(len(self.terminal.conversation) - 1, -1, -1):
                if self.terminal.conversation[i]["role"] == "user" and self.terminal.conversation[i]["content"] == last_user_msg:
                    self.terminal.conversation.pop(i)
                    break
            self.context.console.print("[dim]Regenerating...[/dim]" if self.context.has_rich else "Regenerating...")
            await self.terminal.send_message(last_user_msg)
        else:
            self.context.console.print("[dim]No message to regenerate[/dim]" if self.context.has_rich else "Nothing to regenerate")

    def cmd_undo(self, args: str):
        if len(self.terminal.conversation) < 2:
            self.context.console.print("[dim]Nothing to undo[/dim]" if self.context.has_rich else "Nothing to undo")
            return
        removed = 0
        for role in ("assistant", "user"):
            for i in range(len(self.terminal.conversation) - 1, -1, -1):
                if self.terminal.conversation[i]["role"] == role:
                    self.terminal.conversation.pop(i)
                    removed += 1
                    break
        if self.context.has_rich:
            self.context.console.print(f"[dim]Undone ({removed} messages removed, {len(self.terminal.conversation)} remaining)[/dim]")
        else:
            print(f"Undone ({removed} removed)")

    def cmd_rewind(self, args: str):
        """Restore code checkpoints, conversation history, or both."""
        from runtime.checkpoints import (
            CheckpointConflictError,
            CheckpointNotFoundError,
            CheckpointStore,
        )

        parts = [part for part in args.strip().split() if part]
        assume_yes = "--yes" in parts or "-y" in parts
        parts = [part for part in parts if part not in {"--yes", "-y"}]
        mode = parts[0].lower() if parts else "code"
        identifier = parts[1] if len(parts) > 1 else ""
        is_zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")

        if mode in {"conversation", "chat"}:
            self.cmd_undo("")
            return
        if mode in {"turn", "turns", "green"}:
            self._rewind_transaction(mode, identifier, assume_yes=assume_yes, is_zh=is_zh)
            return
        if mode not in {"code", "both", "list"}:
            identifier = parts[0]
            mode = "code"

        try:
            store = CheckpointStore()
        except Exception as exc:
            message = f"无法打开检查点存储: {exc}" if is_zh else f"Cannot open checkpoint store: {exc}"
            self.context.console.print(f"[red]{message}[/red]" if self.context.has_rich else message)
            return

        if mode == "list":
            records = store.list(session_id=self.terminal.session_id, status="active", limit=12)
            if not records:
                records = store.list(status="active", limit=12)
            if not records:
                message = "没有可恢复的代码检查点" if is_zh else "No active code checkpoints"
                self.context.console.print(f"[dim]{message}[/dim]" if self.context.has_rich else message)
                return
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("  [bold]Code checkpoints[/bold]")
                for record in records:
                    paths = ", ".join(pathlib.Path(item.path).name for item in record.files)
                    run_label = (record.run_id or "standalone")[:10]
                    self.context.console.print(
                        f"  [#C08050]{record.checkpoint_id[:10]}[/#C08050]  "
                        f"[dim]{run_label:<10} · {record.source:<10} · {paths}[/dim]"
                    )
                self.context.console.print()
            else:
                for record in records:
                    paths = ", ".join(pathlib.Path(item.path).name for item in record.files)
                    print(f"{record.checkpoint_id[:10]} {record.run_id or '-'} {record.source} {paths}")
            return

        if not assume_yes:
            prompt = (
                "  恢复代码到检查点状态？检查点之后的修改不会被覆盖，存在冲突时会停止。 [y/N] "
                if is_zh else
                "  Rewind code to its checkpoint? Later changes are protected by conflict checks. [y/N] "
            )
            try:
                answer = (self.context.console.input(prompt) if self.context.has_rich else input(prompt)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if answer not in {"y", "yes"}:
                message = "已取消" if is_zh else "Cancelled"
                self.context.console.print(f"[dim]{message}[/dim]" if self.context.has_rich else message)
                return

        try:
            if identifier:
                checkpoint = store.get(identifier)
                if checkpoint is not None:
                    result = store.restore_checkpoint(checkpoint.checkpoint_id)
                else:
                    result = store.restore_run(identifier)
            else:
                try:
                    result = store.restore_latest(session_id=self.terminal.session_id)
                except CheckpointNotFoundError:
                    result = store.restore_latest()
        except CheckpointConflictError as exc:
            message = f"恢复已停止: {exc}" if is_zh else f"Rewind stopped: {exc}"
            self.context.console.print(f"[red]{message}[/red]" if self.context.has_rich else message)
            return
        except CheckpointNotFoundError:
            message = "未找到可恢复的检查点" if is_zh else "No restorable checkpoint found"
            self.context.console.print(f"[dim]{message}[/dim]" if self.context.has_rich else message)
            return
        except Exception as exc:
            message = f"恢复失败: {exc}" if is_zh else f"Rewind failed: {exc}"
            self.context.console.print(f"[red]{message}[/red]" if self.context.has_rich else message)
            return

        if mode == "both":
            self.cmd_undo("")
        count = len(result.restored_paths)
        message = (
            f"已恢复 {count} 个文件" if is_zh else f"Rewound {count} file{'s' if count != 1 else ''}"
        )
        if self.context.has_rich:
            self.context.console.print(f"[green]✓[/green] [dim]{message}[/dim]")
            for path in result.restored_paths:
                self.context.console.print(f"  [dim]{path}[/dim]")
        else:
            print(message)
            for path in result.restored_paths:
                print(f"  {path}")

    def _rewind_transaction(self, mode: str, ref: str, *, assume_yes: bool, is_zh: bool) -> None:
        """``/rewind turns`` · ``/rewind turn [N|id]`` · ``/rewind green``.

        A turn rewind puts back files, conversation, task list, approvals,
        background tasks and the task worktree as they were before that turn.
        """
        from apps.cli import transactions
        from aria_code.runtime.checkpoints import CheckpointConflictError

        con = self.context.console if self.context.has_rich else None

        def say(text: str, style: str = "dim") -> None:
            if con is not None:
                from rich.markup import escape
                con.print(f"[{style}]{escape(text)}[/{style}]" if style else escape(text))
            else:
                print(text)

        session_id = str(self.terminal.session_id or "default")
        store = transactions.store()
        if mode == "turns":
            points = store.list(session_id)
            if not points:
                say("还没有可回退的轮次" if is_zh else "No turns to rewind yet")
                return
            say("回退点（✓ 检查通过 ✗ 失败 ≈ 仅改动前已有的失败 · 未验证）" if is_zh
                else "Rewind points (✓ checks passed  ✗ failed  ≈ only failures that predate it  · unverified)", "bold")
            for line in transactions.describe(points):
                say(line, "")
            say("/rewind turn N  ·  /rewind green")
            return

        point = store.latest_green(session_id) if mode == "green" else store.find(session_id, ref or "1")
        if point is None:
            if mode == "green":
                say("没有检查通过的回退点" if is_zh else "No point where the checks passed")
            else:
                say(f"没有回退点 {ref or '1'}" if is_zh else f"No rewind point {ref or '1'}; see /rewind turns")
            return
        if not assume_yes:
            question = (
                f"  回退到第 {point.turn} 轮之前（文件、对话、待办、授权一起恢复）？ [y/N] " if is_zh else
                f"  Rewind to before turn {point.turn} — files, conversation, tasks and approvals? [y/N] "
            )
            try:
                answer = (con.input(question) if con is not None else input(question)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            if answer not in {"y", "yes"}:
                say("已取消" if is_zh else "Cancelled")
                return
        try:
            outcome = transactions.rewind(self.terminal, point)
        except CheckpointConflictError as exc:
            say((f"回退已停止，未做任何改动: {exc}" if is_zh else f"Rewind stopped, nothing changed: {exc}"), "red")
            return
        except Exception as exc:
            say((f"回退失败: {exc}" if is_zh else f"Rewind failed: {exc}"), "red")
            return
        for index, line in enumerate(outcome.lines()):
            say(line, "green" if index == 0 else "dim")

    def _workflow_workspace(self) -> str:
        return self.terminal.config.get("_session_workspace_root") or os.getcwd()

    def _wf_say(self, text: str, style: str = "dim") -> None:
        if self.context.has_rich:
            from rich.markup import escape
            self.context.console.print(f"[{style}]{escape(text)}[/{style}]" if style else escape(text))
        else:
            print(text)

    def _wf_ask(self, question: str) -> bool:
        try:
            answer = (self.context.console.input(question) if self.context.has_rich else input(question))
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}

    async def cmd_workflow(self, args: str):
        """/workflow list | show <name> | run <name> [args] | new <name>."""
        from aria_code.runtime import workflows

        parts = args.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else "list"
        name = parts[1].lstrip("/").lower() if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        workspace = self._workflow_workspace()

        if sub == "list":
            found = workflows.names(workspace)
            if not found:
                self._wf_say("No workflows. /workflow new <name> creates .aria/workflows/<name>.yaml")
                return
            for item in found:
                try:
                    workflow = workflows.find(workspace, item)
                    self._wf_say(f"  /{item:<14} {workflow.description or ''}  ({len(workflow.steps)} steps)", "")
                except workflows.WorkflowError as exc:
                    self._wf_say(f"  /{item:<14} invalid: {exc}", "red")
            return
        if sub == "new":
            if not name:
                self._wf_say("Usage: /workflow new <name>")
                return
            target = pathlib.Path(workspace) / ".aria" / "workflows" / f"{name}.yaml"
            if target.exists():
                self._wf_say(f"{target} already exists", "yellow")
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(workflows.TEMPLATE.format(name=name), encoding="utf-8")
            self._wf_say(f"Created {target}; edit its steps, then run /{name}", "green")
            return
        if sub not in {"show", "run"} or not name:
            self._wf_say("Usage: /workflow list | show <name> | run <name> [args] | new <name>")
            return
        try:
            workflow = workflows.find(workspace, name)
        except workflows.WorkflowError as exc:
            self._wf_say(str(exc), "red")
            return
        if workflow is None:
            self._wf_say(f"No workflow '{name}' in .aria/workflows; /workflow list", "yellow")
            return
        if sub == "show":
            for line in workflow.outline():
                self._wf_say(line, "")
            return
        await self._run_workflow(workflow, rest)

    async def _run_workflow(self, workflow, args: str) -> None:
        from aria_code.runtime.workflows import TrustStore
        from aria_code.apps.cli.workflow_runner import run_workflow, summary_lines

        trust = TrustStore.load()
        if not trust.trusted(workflow):
            self._wf_say("This workflow comes from the repository and has not run here before"
                      " (or its file changed). It will run:", "yellow")
            for line in workflow.outline():
                self._wf_say(line, "")
            if not self._wf_ask("  Trust and run it? [y/N] "):
                self._wf_say("Cancelled")
                return
            trust.trust(workflow)
        results = await run_workflow(self.terminal, workflow, args, ask=self._wf_ask, say=self._wf_say)
        for index, line in enumerate(summary_lines(workflow, results)):
            style = ("red" if "FAILED" in line else "yellow" if "STOPPED" in line else "green") if index == 0 else "dim"
            self._wf_say(line, style)

    async def cmd_retry(self, args: str):
        last_user_msg = None
        for i in range(len(self.terminal.conversation) - 1, -1, -1):
            if self.terminal.conversation[i]["role"] == "assistant":
                self.terminal.conversation.pop(i)
                break
        for msg in reversed(self.terminal.conversation):
            if msg["role"] == "user":
                last_user_msg = msg["content"]
                break
        if not last_user_msg:
            self.context.console.print("[dim]No message to retry[/dim]" if self.context.has_rich else "Nothing to retry")
            return
        for i in range(len(self.terminal.conversation) - 1, -1, -1):
            if self.terminal.conversation[i]["role"] == "user" and self.terminal.conversation[i]["content"] == last_user_msg:
                self.terminal.conversation.pop(i)
                break
        orig_model_key = resolve_model_key(self.terminal.config.get("model", "qwen2.5:7b"))
        _fallback_model = _get_MODELS().get("qwen-fast") or _get_MODELS().get("qwen7b") or next(iter(_get_MODELS().values()))
        orig_temp = _get_MODELS().get(orig_model_key, _fallback_model).get("temperature", 0.3)
        _get_MODELS()[orig_model_key]["temperature"] = min(0.9, orig_temp + 0.3)
        if self.context.has_rich:
            self.context.console.print(f"[dim]Retrying with temperature {_get_MODELS()[orig_model_key]['temperature']:.1f}...[/dim]")
        else:
            print("Retrying (temp +0.3)...")
        try:
            await self.terminal.send_message(last_user_msg)
        finally:
            _get_MODELS()[orig_model_key]["temperature"] = orig_temp

    def cmd_note(self, args: str):
        text = args.strip()
        if not text:
            self.context.console.print("[dim]Usage: /note <text>[/dim]" if self.context.has_rich else "Usage: /note <text>")
            return
        aria_md = pathlib.Path.cwd() / "ARIA.md"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n- [{now_str}] {text}"
        if aria_md.exists():
            content = aria_md.read_text(encoding="utf-8")
            if "## Notes" not in content:
                content += "\n\n## Notes\n"
            content += entry
        else:
            content = f"# Aria Project Notes\n\n## Notes\n{entry}\n"
        aria_md.write_text(content, encoding="utf-8")
        global _PROJECT_CONTEXT
        _PROJECT_CONTEXT = _load_project_context()
        if self.context.has_rich:
            self.context.console.print(f"[dim]Note saved to {aria_md.name}[/dim]")
        else:
            print(f"Saved to {aria_md.name}")

    async def cmd_review(self, args: str):
        """Review a change with an isolated reviewer: /review [--staged | --base B | --commit SHA | PATH] [--json]."""
        from aria_code import review_service as rs
        from aria_code.apps.cli.review_runner import make_model_call

        console = self.context.console if self.context.has_rich else None
        say = (lambda text, style="": console.print(f"[{style}]{text}[/{style}]" if style else text)) if console \
            else (lambda text, style="": print(text))
        lang = "zh" if str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh") else "en"
        try:
            tokens = shlex.split(args)
            as_json = "--json" in tokens
            target = rs.target_from_args([t for t in tokens if t != "--json"])
            inp = rs.collect(target, pathlib.Path.cwd())
        except (ValueError, rs.ReviewError) as exc:
            say(str(exc), "red")
            say("Usage: /review [--staged | --base BRANCH | --commit SHA | PATH] [--json]", "dim")
            return
        if not inp.diff.strip():
            say("No changes to review." if lang == "en" else "没有可审查的改动。", "dim")
            return
        say(f"  ↳ {target.describe()} · {len(inp.files)} files · +{inp.added} −{inp.removed}", "dim")
        if inp.truncated:
            say(f"  ↳ too large to review whole; not included: {', '.join(inp.omitted_files)}", "yellow")
        _print_phase("Reviewing")
        try:
            result = await rs.run_review(inp, make_model_call(self.terminal.config), lang=lang)
        except rs.ReviewError as exc:
            say(str(exc), "red")
            return
        if as_json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        elif console:
            _print_review(console, result, lang)
        else:
            print(rs.render_text(result, lang=lang))
        # The main conversation remembers the review, so "fix finding 2" works.
        self.terminal.conversation.append({"role": "user", "content": f"/review {args}".strip()})
        self.terminal.conversation.append({"role": "assistant", "content": rs.history_note(result)})


_PRIORITY_STYLE = {0: "bold red", 1: "red", 2: "yellow", 3: "dim"}


def _print_review(console, result, lang: str) -> None:
    from rich.markup import escape

    from aria_code import review_service as rs

    if not result.structured:
        console.print(escape(rs.render_text(result, lang=lang)))
        return
    t = rs._TEXT["zh" if lang == "zh" else "en"]
    verdict, style = ((t["unknown"], "dim") if result.correct is None else
                      (t["correct"], "green") if result.correct else (t["incorrect"], "red"))
    console.print(f"[bold {style}]{verdict}[/bold {style}] [dim]· {t['confidence']} {result.confidence:.2f}[/dim]")
    if result.explanation:
        console.print(escape(result.explanation))
    if not result.findings:
        console.print(f"[dim]{t['none']}[/dim]")
    shown_outside = False
    for number, f in enumerate(result.findings, 1):
        if not f.in_change and not shown_outside:
            console.print(f"[dim]— {t['outside']} —[/dim]")
            shown_outside = True
        style = _PRIORITY_STYLE.get(f.priority, "dim")
        console.print(f"\n[{style}]{number}. {escape('[' + f.label + ']')}[/{style}] [bold]{escape(f.title)}[/bold]  "
                      f"[dim]{escape(f.location)} · {f.confidence:.2f}[/dim]")
        # Under the finding's title, wrapped rows included: printed with
        # three leading spaces, a long explanation fell back to column 0.
        from aria_code.ui.render.output import print_hanging

        for line in f.body.splitlines():
            if line.strip():
                print_hanging(console, "   ", line.strip())
    if result.omitted_files:
        console.print(f"\n[yellow]{t['omitted']}: {escape(', '.join(result.omitted_files))}[/yellow]")
