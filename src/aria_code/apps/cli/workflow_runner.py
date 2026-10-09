"""Run a ``.aria/workflows`` file in the REPL, one step at a time.

Each kind of step goes through what already exists for it, so a workflow has
no powers of its own: ``run`` through the ``run_command`` tool (command
policy, sandbox, output display), ``prompt`` through ``send_message`` (tools,
approvals, checks, task worktree, transaction point), ``command`` through
the slash-command dispatcher.

A step fails when its command exits non-zero, when its turn leaves the code
failing checks it did not fail before (or gets no answer), or when its slash
command does not exist. The first failure stops the workflow unless the step
says ``continue_on_error``; declining a ``confirm`` step stops it too.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from aria_code.runtime.workflows import Workflow, WorkflowStep, variables

PASSED, FAILED, SKIPPED, DECLINED = "passed", "failed", "skipped", "declined"
_MARKS = {PASSED: "✓", FAILED: "✗", SKIPPED: "–", DECLINED: "–"}


@dataclass
class StepResult:
    name: str
    status: str
    detail: str = ""
    seconds: float = 0.0


def _namespace(terminal: Any) -> dict:
    return vars(sys.modules[type(terminal).__module__])


async def _run_command_step(terminal: Any, step: WorkflowStep, text: str) -> tuple[bool, str]:
    from aria_code.apps.cli.providers.runtime_bridge import build_tool_executor
    from aria_code.runtime.approval import ApprovalDecision

    config = terminal.config
    executor = build_tool_executor(
        _namespace(terminal)["LOCAL_TOOLS"], config,
        lambda: {"_session_id": getattr(terminal, "session_id", "")},
    )
    root = config.get("_session_workspace_root") or None
    params = {"command": text, "timeout": step.timeout, **({"cwd": root} if root else {})}
    # The user ran the workflow, and trusted its file before the first run;
    # the command policy and sandbox still apply, as they do to /verify.
    approval = ApprovalDecision.allow(policy="balanced", user_approved=True)
    result = await asyncio.to_thread(executor.execute_local, "run_command", params, approval=approval)
    if not result.get("success"):
        return False, str(result.get("error") or "command failed")
    code = (result.get("data") or {}).get("exit_code", 0)
    return code == 0, f"exit {code}"


async def _prompt_step(terminal: Any, text: str) -> tuple[bool, str]:
    before = str(getattr(terminal, "_code_verdict", "") or "")
    length = len(terminal.conversation)
    await terminal.send_message(text)
    answered = len(terminal.conversation) > length and terminal.conversation[-1].get("role") == "assistant"
    after = str(getattr(terminal, "_code_verdict", "") or "")
    if not answered:
        return False, "no answer"
    if after == "failed" and before != "failed":
        return False, "checks failed after this step"
    return True, f"checks {after}" if after else "answered"


async def _slash_step(terminal: Any, workflow: Workflow, text: str) -> tuple[bool, str]:
    name = text.split(maxsplit=1)[0].lower()
    if name.lstrip("/") == workflow.name:
        return False, "a workflow cannot run itself"
    commands = terminal.commands
    if not commands.is_command(text):
        return False, f"unknown command {name}"
    await commands.execute(text)
    return True, name


async def run_workflow(terminal: Any, workflow: Workflow, args: str = "", *,
                       ask: Callable[[str], bool], say: Callable[[str, str], None]) -> list[StepResult]:
    values = variables(workflow, args)
    results: list[StepResult] = []
    total = len(workflow.steps)
    stopped = False
    for index, step in enumerate(workflow.steps, 1):
        if stopped:
            results.append(StepResult(step.name, SKIPPED))
            continue
        text = step.render(values)
        say(f"[{index}/{total}] {step.name}", "bold")
        if step.confirm and not ask(f"  Run “{step.name}”: {text[:80]}? [y/N] "):
            results.append(StepResult(step.name, DECLINED, "declined"))
            stopped = True
            continue
        started = time.time()
        try:
            if step.kind == "run":
                ok, detail = await _run_command_step(terminal, step, text)
            elif step.kind == "prompt":
                ok, detail = await _prompt_step(terminal, text)
            else:
                ok, detail = await _slash_step(terminal, workflow, text)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(StepResult(step.name, PASSED if ok else FAILED, detail, time.time() - started))
        if not ok and not step.continue_on_error:
            stopped = True
    return results


def summary_lines(workflow: Workflow, results: list[StepResult]) -> list[str]:
    failed = [r for r in results if r.status == FAILED]
    declined = any(r.status == DECLINED for r in results)
    verdict = "FAILED" if failed else "STOPPED" if declined else "DONE"
    lines = [f"/{workflow.name} {verdict}"]
    for result in results:
        timing = f" · {result.seconds:.1f}s" if result.seconds >= 0.05 else ""
        detail = f"  {result.detail}" if result.detail else ""
        lines.append(f"  {_MARKS[result.status]} {result.name}{detail}{timing}")
    return lines
