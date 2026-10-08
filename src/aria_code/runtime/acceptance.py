"""The acceptance gate — making "done" a claim that was executed.

Why this exists
---------------
Aria could already *suggest* verification.  ``VerificationPlanner`` infers the
right checks from what changed, ``write_file`` attaches the inferred command to
its result as ``suggested_verification``, and ``/verify`` runs the plan when a
human asks for it.  Every one of those paths ends with a suggestion.  Nothing
in the loop ever ran the check, read the failure, or refused to let the turn
finish; the model announced "已完成" and the turn ended, verified or not.

That is the whole distance between an agent that writes code and an agent you
can hand a repository to.  The strong coding agents are not better at emitting
a patch — they are structurally unable to stop while the check is red.  The
loop, not the model, is what holds that line, which is exactly why this belongs
in the runtime and works the same whether the turn is driven by a 7B local
model or a frontier one.

What it does
------------
The gate arms itself when a turn actually mutates a file on disk, and fires at
the moment the model stops requesting tools — the point where the loop would
otherwise ``break`` and report success.  It runs the inferred checks; if they
pass, the turn ends as it always did, now carrying evidence.  If they fail, the
loop gets a repair directive built from the *failing* output — trimmed, with
``file:line`` anchors pulled out — and continues, so the model repairs against
a real signal instead of its own recollection of what it wrote.

Three constraints keep it from becoming a tax:

  - **Read-only turns never pay it.**  Arming requires an applied write.  A
    market question, a code review, a staged-but-unapplied edit: no checks run.
  - **It is bounded.**  After ``max_attempts`` failed rounds the gate stops
    re-arming and the turn ends *honestly* — ``verified: false`` with the
    failing command in the result — rather than looping until the budget dies.
    An unverified answer that says so is useful; an infinite loop is not.
  - **It never invents a sandbox.**  Commands go through the caller's own
    runner (the CLI passes ``run_command``, with its policy, permission mode
    and approval path intact), so the gate cannot execute anything the user's
    current mode would not already allow.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Union

__all__ = [
    "AcceptanceGate",
    "AcceptanceReport",
    "CheckResult",
    "DEFAULT_MUTATING_TOOLS",
    "extract_mutated_paths",
]


# Tools whose success means bytes changed on disk.  ``run_command`` is
# deliberately absent: it can mutate, but gating on it would re-arm the gate
# with every check the gate itself runs — the checks would never terminate.
DEFAULT_MUTATING_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "multi_edit",
    "apply_patch",
    "notebook_edit",
    "str_replace",
    "create_file",
})

# A failing test log is the single most context-hungry payload in a coding
# turn — a full pytest run can exceed the window on its own.  Head carries the
# collection error, tail carries the assertion and the summary line; the middle
# is almost always repeated passing output.
_HEAD_LINES = 20
_TAIL_LINES = 45
_MAX_CHARS = 6000

# ``File "x.py", line 12`` (Python), ``x.ts:12:5`` (tsc/eslint/jest).
_PY_ANCHOR = re.compile(r'File "([^"]+)", line (\d+)')
_COLON_ANCHOR = re.compile(r"(?m)^\s*([\w./\\-]+\.[A-Za-z]{1,4}):(\d+)(?::\d+)?")


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_mutated_paths(
    tool: str,
    result: Any,
    *,
    mutating_tools: Iterable[str] = DEFAULT_MUTATING_TOOLS,
) -> tuple[str, ...]:
    """Paths this tool result actually wrote to disk.

    Returns nothing for a failed call or an unapplied staged change.  The
    distinction matters: ``write_file`` in review mode returns success with
    ``applied: False`` and the file on disk is untouched, so running the test
    suite would verify the previous state and report a green that means
    nothing.
    """
    canonical = str(tool or "").rsplit("__", 1)[-1]
    if canonical not in set(mutating_tools):
        return ()

    payload = _as_dict(result)
    if payload.get("success") is False or payload.get("error"):
        return ()

    data = _as_dict(payload.get("data")) or payload
    if data.get("applied") is False or (
        data.get("staged") is True and data.get("applied") is not True
    ):
        return ()

    found: List[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    _add(data.get("path"))
    _add(data.get("file_path"))
    for entry in data.get("paths", ()) or ():
        _add(entry)
    # multi_edit reports per-edit records rather than a single path.
    for entry in data.get("edits", ()) or ():
        _add(_as_dict(entry).get("path"))

    return tuple(found)


def _trim(text: str) -> str:
    """Head + tail of command output, with the uninformative middle dropped."""
    body = (text or "").strip()
    if not body:
        return ""
    lines = body.splitlines()
    if len(lines) > _HEAD_LINES + _TAIL_LINES:
        omitted = len(lines) - _HEAD_LINES - _TAIL_LINES
        lines = (
            lines[:_HEAD_LINES]
            + [f"… [{omitted} lines omitted] …"]
            + lines[-_TAIL_LINES:]
        )
    joined = "\n".join(lines)
    if len(joined) > _MAX_CHARS:
        keep = _MAX_CHARS // 2
        joined = f"{joined[:keep]}\n… [truncated] …\n{joined[-keep:]}"
    return joined


def _anchors(text: str, limit: int = 5) -> tuple[str, ...]:
    """``file:line`` locations named in a failure, most recent frame last.

    Handing the model the anchors separately is what turns a wall of log into
    an edit: without them a weaker model tends to rewrite the whole file
    instead of patching the line the traceback already identified.
    """
    out: List[str] = []
    for match in _PY_ANCHOR.finditer(text or ""):
        entry = f"{match.group(1)}:{match.group(2)}"
        if entry not in out:
            out.append(entry)
    for match in _COLON_ANCHOR.finditer(text or ""):
        entry = f"{match.group(1)}:{match.group(2)}"
        if entry not in out:
            out.append(entry)
    # The last frames are the ones inside the code that just changed; the first
    # are usually the runner's own stack.
    return tuple(out[-limit:])


@dataclass(frozen=True)
class CheckResult:
    """One verification command, run."""

    command: str
    exit_code: int
    passed: bool
    output: str = ""
    duration: float = 0.0
    error: str = ""

    @property
    def anchors(self) -> tuple[str, ...]:
        return () if self.passed else _anchors(self.output)

    def summary(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "duration": round(self.duration, 3),
            "error": self.error,
        }


@dataclass(frozen=True)
class AcceptanceReport:
    """The outcome of one pass of the gate."""

    attempt: int
    checks: tuple[CheckResult, ...] = ()
    reason: str = ""
    paths: tuple[str, ...] = ()
    attempts_remaining: int = 0

    @property
    def ran(self) -> bool:
        return bool(self.checks)

    @property
    def passed(self) -> bool:
        """True only when at least one check ran and every check passed.

        A gate that reports "verified" without running anything is worse than
        no gate: it launders an unchecked claim into a checked-looking one.
        """
        return self.ran and all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def headline(self) -> str:
        if not self.ran:
            return "验收未执行：未推断出检查命令"
        if self.passed:
            names = ", ".join(check.command for check in self.checks)
            return f"验收通过 ({names})"
        first = self.failures[0]
        return f"验收失败：`{first.command}` exit {first.exit_code}"

    def repair_directive(self) -> str:
        """What the model is told when the check came back red.

        Written as evidence plus one instruction.  The failing output is the
        evidence; the instruction exists because the observed failure mode is
        not that the model cannot fix the bug — it is that it re-announces
        completion without having looked at why the command failed.
        """
        blocks: List[str] = [
            "## 验收未通过 (Acceptance check failed)",
            "",
            "你声明任务已完成，但改动过的文件没有通过自动验收检查。"
            "以下是**实际执行**的命令和真实输出：",
            "",
        ]
        for check in self.failures:
            blocks.append(f"### `{check.command}` → exit {check.exit_code}")
            if check.error:
                blocks.append(f"（执行错误：{check.error}）")
            body = _trim(check.output)
            if body:
                blocks.append(f"```\n{body}\n```")
            if check.anchors:
                blocks.append("定位：" + ", ".join(f"`{a}`" for a in check.anchors))
            blocks.append("")

        blocks.append(
            "请依次：(1) 读出错位置的代码确认原因，(2) 用 `edit_file` 定点修补"
            "——不要整文件重写，(3) 修好后直接说明修了什么。"
            "修改后验收会自动重跑，你**不需要**自己调用验收命令。"
        )
        if self.attempts_remaining <= 0:
            blocks.append(
                "注意：这是最后一次修复机会。若仍未通过，请如实说明失败原因，"
                "不要声称任务已完成。"
            )
        else:
            blocks.append(f"剩余自动重试次数：{self.attempts_remaining}。")
        return "\n".join(blocks)

    def summary(self) -> dict:
        return {
            "attempt": self.attempt,
            "passed": self.passed,
            "ran": self.ran,
            "reason": self.reason,
            "paths": list(self.paths),
            "checks": [check.summary() for check in self.checks],
        }


# ``(command) -> result dict`` — sync or async.  The result is read loosely
# because every surface in this codebase wraps command output slightly
# differently; see ``_read_run_result``.
CommandRunner = Callable[[str], Union[dict, Awaitable[dict]]]


def _read_run_result(raw: Any) -> tuple[int, str, str]:
    """Normalise a runner result into ``(exit_code, output, error)``.

    Deliberately permissive.  ``tool_run_command`` returns
    ``{"success": True, "data": {"exit_code": .., "stdout": .., "stderr": ..}}``
    on a completed command and ``{"success": False, "error": "…timed out"}``
    when it never completed — and a timeout is a *failed* check, not a passing
    one, so an unreadable result must never be read as exit 0.
    """
    payload = _as_dict(raw)
    if not payload:
        return 1, "", "runner returned no result"

    if payload.get("success") is False:
        return 1, str(payload.get("output") or ""), str(payload.get("error") or "command failed")

    data = _as_dict(payload.get("data")) or payload
    for key in ("exit_code", "returncode", "code", "status"):
        if key in data:
            try:
                exit_code = int(data[key])
                break
            except (TypeError, ValueError):
                continue
    else:
        return 1, "", "runner returned no exit status"

    stdout = str(data.get("stdout") or data.get("output") or "")
    stderr = str(data.get("stderr") or "")
    output = "\n".join(part for part in (stdout, stderr) if part.strip())
    return exit_code, output, ""


class AcceptanceGate:
    """Arms on real writes, fires when the model says it is done."""

    def __init__(
        self,
        *,
        runner: CommandRunner,
        root: Union[str, Path] = ".",
        planner: Any = None,
        max_attempts: int = 2,
        mutating_tools: Iterable[str] = DEFAULT_MUTATING_TOOLS,
        enabled: bool = True,
        commands: Optional[Sequence[str]] = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.runner = runner
        self.max_attempts = max(0, int(max_attempts))
        self.mutating_tools = frozenset(mutating_tools)
        self.enabled = bool(enabled)
        # An explicit command list overrides inference — this is the hook a
        # domain pack or a repo's own config uses to say what "green" means
        # for it (``make check``, ``bazel test //...``), which inference can
        # never guess.
        self.commands = tuple(commands or ())
        self._planner = planner
        self.attempts = 0
        self.reports: List[AcceptanceReport] = []
        self._pending: List[str] = []
        self._all_paths: List[str] = []

    # ── arming ────────────────────────────────────────────────────────────

    def record_tool(self, tool: str, result: Any) -> tuple[str, ...]:
        """Note one tool result; arms the gate if it wrote to disk."""
        if not self.enabled:
            return ()
        paths = extract_mutated_paths(tool, result, mutating_tools=self.mutating_tools)
        for path in paths:
            if path not in self._pending:
                self._pending.append(path)
            if path not in self._all_paths:
                self._all_paths.append(path)
        return paths

    @property
    def armed(self) -> bool:
        return bool(self._pending)

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    def should_run(self) -> bool:
        return self.enabled and self.armed and not self.exhausted

    # ── firing ────────────────────────────────────────────────────────────

    def _plan(self, paths: Sequence[str]) -> tuple[tuple[str, ...], str]:
        if self.commands:
            return self.commands, "configured acceptance commands"
        planner = self._planner
        if planner is None:
            try:
                from aria_code.workspace.verify import VerificationPlanner
            except Exception:
                return (), "verification planner unavailable"
            planner = VerificationPlanner(root=self.root)
            self._planner = planner
        try:
            plan = planner.infer(list(paths))
        except Exception as exc:  # a planner fault must not fail the turn
            return (), f"verification planning failed: {exc}"
        return tuple(plan.commands or ()), str(plan.reason or "")

    async def _run_one(self, command: str) -> CheckResult:
        started = time.time()
        try:
            raw = self.runner(command)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:
            return CheckResult(
                command=command,
                exit_code=1,
                passed=False,
                error=str(exc),
                duration=time.time() - started,
            )
        exit_code, output, error = _read_run_result(raw)
        return CheckResult(
            command=command,
            exit_code=exit_code,
            passed=exit_code == 0 and not error,
            output=output,
            error=error,
            duration=time.time() - started,
        )

    async def run(self) -> Optional[AcceptanceReport]:
        """Run the inferred checks once.  ``None`` when the gate is not armed.

        Disarms before running: the paths this pass covers are consumed, so a
        turn that fixes nothing cannot re-fire the same checks forever, while
        a repair that writes a file re-arms the gate for the next pass.
        """
        if not self.should_run():
            return None

        paths = tuple(self._pending)
        self._pending.clear()
        self.attempts += 1

        commands, reason = self._plan(paths)
        checks: List[CheckResult] = []
        for command in commands:
            check = await self._run_one(command)
            checks.append(check)
            # Stop at the first red.  Later checks usually fail for the same
            # cause, and each extra failing log costs context the repair needs.
            if not check.passed:
                break

        report = AcceptanceReport(
            attempt=self.attempts,
            checks=tuple(checks),
            reason=reason,
            paths=paths,
            attempts_remaining=max(0, self.max_attempts - self.attempts),
        )
        self.reports.append(report)
        return report

    # ── reporting ─────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """What the turn result carries so a caller can prove the claim.

        ``verified`` is tri-state on purpose.  ``True`` means checks ran green,
        ``False`` means they ran red, and ``None`` means nothing was verified —
        a read-only turn, or a workspace with no inferable check.  Collapsing
        the last two into ``False`` would make every analysis turn look failed;
        collapsing it into ``True`` would be the exact lie this module exists
        to prevent.
        """
        last = self.reports[-1] if self.reports else None
        verified: Optional[bool] = None
        if last is not None and last.ran:
            verified = last.passed and not self.armed
        return {
            "verified": verified,
            "attempts": self.attempts,
            "paths": list(self._all_paths),
            "reports": [report.summary() for report in self.reports],
            "pending_paths": list(self._pending),
            "headline": "改动后尚未重新验收" if self.armed else (last.headline() if last is not None else ""),
        }
