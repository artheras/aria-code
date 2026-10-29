"""Background subagent task system.

Allows the main agent to spawn independent sub-tasks that run concurrently.
Tasks are tracked in memory; code checkpoints and applied worktree changes are
persisted by the shared runtime store.

Tool functions exposed to the LLM:
    spawn_task(prompt, context?)  → {"task_id": "abc123", "status": "pending"}
    task_status(task_id)          → {"status": "running|done|failed", ...}
    task_result(task_id)          → {"result": "...", "success": bool}
    task_list()                   → [{"task_id": ..., "status": ...}, ...]
    task_cancel(task_id)          → {"cancelled": bool}

Background execution is wired up in aria_cli.py via _subagent_runner().
If no runner is registered, spawn_task stores the task for manual execution.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .task_ledger import TaskLedger
from .task_verification import capture_baseline, validate_checks, verify_checks
from .agent_profiles import PROFILES, get_profile, tool_list_agents

_TASKS: Dict[str, "SubagentTask"] = {}
_RUNNER: Optional[Callable] = None  # set by aria_cli.py
_LEDGER: Optional[TaskLedger] = None


@dataclass
class SubagentTask:
    task_id: str
    prompt: str
    context: str = ""
    status: str = "pending"   # pending | running | done | failed | cancelled
    result: str = ""
    error: str = ""
    agent: str = "general"
    success_criteria: str = ""
    max_rounds: int = 30
    timeout_seconds: int = 300
    mode: str = "read-only"
    isolation: str = "shared"
    backend: str = "aria"    # aria | claude | codex — which agent actually runs the task
    workspace: str = ""
    session_id: str = ""
    branch: str = ""
    diff_summary: str = ""
    applied: bool = False
    applied_paths: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    handoff: dict[str, Any] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    evidence_truncated: bool = False
    verification_baseline: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    async_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)
    worktree_spec: Optional[Any] = field(default=None, repr=False, compare=False)

    def age_str(self) -> str:
        elapsed = time.time() - self.created_at
        if elapsed < 60:
            return f"{int(elapsed)}s"
        if elapsed < 3600:
            return f"{elapsed/60:.1f}m"
        return f"{elapsed/3600:.1f}h"

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "success_criteria": self.success_criteria,
            "max_rounds": self.max_rounds,
            "timeout_seconds": self.timeout_seconds,
            "task_id": self.task_id,
            "status": self.status,
            "prompt": self.prompt[:200] + ("…" if len(self.prompt) > 200 else ""),
            "age": self.age_str(),
            "mode": self.mode,
            "isolation": self.isolation,
            "backend": self.backend,
            "workspace": self.workspace or None,
            "branch": self.branch or None,
            "applied": self.applied,
            "handoff": self.handoff or None,
            "verification": self.verification or None,
            "verified": self.verification.get("verified"),
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the serializable task contract and latest outcome."""
        return {
            "agent": self.agent,
            "success_criteria": self.success_criteria,
            "max_rounds": self.max_rounds,
            "timeout_seconds": self.timeout_seconds,
            "task_id": self.task_id, "prompt": self.prompt, "context": self.context,
            "status": self.status, "result": self.result, "error": self.error,
            "mode": self.mode, "isolation": self.isolation, "backend": self.backend,
            "workspace": self.workspace, "session_id": self.session_id,
            "branch": self.branch, "diff_summary": self.diff_summary,
            "applied": self.applied, "applied_paths": list(self.applied_paths),
            "created_at": self.created_at, "completed_at": self.completed_at,
            "handoff": self.handoff,
            "checks": self.checks, "evidence": self.evidence,
            "evidence_truncated": self.evidence_truncated,
            "verification_baseline": self.verification_baseline,
            "verification": self.verification,
        }


def _ledger() -> TaskLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = TaskLedger()
    return _LEDGER


def _persist(task: SubagentTask) -> None:
    """A ledger write must never make a live task fail."""
    try:
        _ledger().upsert(task.snapshot())
    except Exception:
        pass


def restore_tasks() -> int:
    """Restore prior task records after a CLI restart.

    A running coroutine cannot safely be resumed, so it is retained as an
    explicit interrupted task rather than silently disappearing.
    """
    restored = 0
    interrupted = []
    for record in _ledger().restore():
        task_id = str(record.get("task_id") or "").strip()
        if not task_id or task_id in _TASKS:
            continue
        saved_status = str(record.get("status") or "pending")
        status = saved_status
        error = str(record.get("error") or "")
        if status == "running":
            status = "interrupted"
            error = "CLI restarted before this task completed; rerun or delegate it again."
        elif status == "pending":
            # Nothing picks up a queued task later: it was spawned with no
            # runner registered, and the process that queued it is gone. Left
            # as pending it never changes, and these piled up by the thousand.
            status = "interrupted"
            error = "Queued without a runner and never started; rerun or delegate it again."
        task = SubagentTask(
            task_id=task_id, prompt=str(record.get("prompt") or ""),
            agent=str(record.get("agent") or "general"),
            success_criteria=str(record.get("success_criteria") or ""),
            max_rounds=int(record.get("max_rounds") or 30),
            timeout_seconds=int(record.get("timeout_seconds") or 300),
            context=str(record.get("context") or ""), status=status,
            result=str(record.get("result") or ""), error=error,
            mode=str(record.get("mode") or "read-only"),
            isolation=str(record.get("isolation") or "shared"),
            backend=str(record.get("backend") or "aria"),
            workspace=str(record.get("workspace") or ""),
            session_id=str(record.get("session_id") or ""),
            branch=str(record.get("branch") or ""),
            diff_summary=str(record.get("diff_summary") or ""),
            applied=bool(record.get("applied")),
            applied_paths=tuple(record.get("applied_paths") or ()),
            created_at=float(record.get("created_at") or time.time()),
            completed_at=float(record.get("completed_at") or 0.0),
            handoff=dict(record.get("handoff") or {}),
            checks=list(record.get("checks") or []),
            evidence=list(record.get("evidence") or [])[:256],
            evidence_truncated=bool(record.get("evidence_truncated")),
            verification_baseline=dict(record.get("verification_baseline") or {}),
            verification={**dict(record.get("verification") or {}), "status": "stale", "verified": None},
        )
        _TASKS[task_id] = task
        if status != saved_status:
            interrupted.append(task.snapshot())
        restored += 1
    if interrupted:
        # Completed/queued records have not changed. Persist all interrupted
        # records together; per-task upserts made restoration quadratic.
        try:
            _ledger().upsert_many(interrupted)
        except Exception:
            pass  # Persistence failure must not discard restored live state.
    return restored


def register_runner(runner: Callable) -> None:
    """Register the async runner function from aria_cli.py."""
    global _RUNNER
    _RUNNER = runner


# ── Tool functions ─────────────────────────────────────────────────────────────

def tool_spawn_task(params: dict) -> dict:
    """Spawn an independent background agent task."""
    prompt = params.get("prompt", "")
    context = params.get("context", "")
    if not prompt:
        return {"success": False, "error": "Missing 'prompt'"}

    mode = str(params.get("mode") or "read-only").lower()
    isolation = str(params.get("isolation") or "auto").lower()
    backend = str(params.get("backend") or "aria").lower()
    if mode not in {"read-only", "workspace-write"}:
        return {"success": False, "error": "mode must be read-only or workspace-write"}
    if isolation not in {"auto", "worktree", "shared"}:
        return {"success": False, "error": "isolation must be auto, worktree, or shared"}
    if backend not in {"aria", "claude", "codex"}:
        return {"success": False, "error": "backend must be aria, claude, or codex"}
    try:
        profile = get_profile(str(params.get("agent") or "general"))
        checks = validate_checks(params.get("checks", []))
        max_rounds = params.get("max_rounds", profile.max_rounds)
        timeout_seconds = params.get("timeout_seconds", profile.timeout_seconds)
        if type(max_rounds) is not int or not 1 <= max_rounds <= profile.max_rounds:
            raise ValueError(f"max_rounds must be 1–{profile.max_rounds}")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= profile.timeout_seconds:
            raise ValueError(f"timeout_seconds must be 1–{profile.timeout_seconds}")
        if mode == "workspace-write" and not profile.writable:
            raise ValueError(f"{profile.name} is a read-only role")
        if mode == "workspace-write" and params.get("_parent_permission_mode") in {"read-only", "readonly", "read_only", "plan"}:
            raise ValueError("A child cannot exceed the parent's read-only permissions")
        if profile.name != "general" and backend != "aria":
            raise ValueError("Restricted profiles currently require the aria backend")
        if sum(t.status in {"pending", "running"} for t in _TASKS.values()) >= 8:
            raise ValueError("Active task limit reached (8); wait or cancel a task first")
        if profile.name != "general" and _RUNNER is None:
            raise ValueError("No Aria runner is registered; this role cannot start")
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    success_criteria = str(params.get("success_criteria") or "")
    if len(str(prompt)) > 32000 or len(str(context)) > 32000 or len(success_criteria) > 4000:
        return {"success": False, "error": "Task brief exceeds bounded context limits"}
    if isolation == "auto":
        isolation = "worktree" if mode == "workspace-write" else "shared"
    if mode == "read-only" and isolation == "worktree":
        isolation = "shared"
    if mode == "workspace-write" and isolation == "shared":
        return {
            "success": False,
            "error": "workspace-write tasks require worktree isolation",
        }

    task_id = uuid.uuid4().hex[:8]
    task = SubagentTask(
        task_id=task_id,
        prompt=prompt,
        context=context,
        agent=profile.name, success_criteria=success_criteria,
        checks=checks, verification={"status": "pending" if checks else "not_requested", "verified": None},
        max_rounds=max_rounds, timeout_seconds=timeout_seconds,
        mode=mode,
        isolation=isolation,
        backend=backend,
        workspace=str(params.get("_workspace") or params.get("_spawn_workspace") or Path.cwd()),
        session_id=str(params.get("_session_id") or ""),
    )
    _TASKS[task_id] = task
    _persist(task)

    # An external backend (claude/codex) always has a runner (the CLI itself);
    # the "aria" backend needs one registered via register_runner().
    if backend != "aria" or _RUNNER is not None:
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                task.async_task = asyncio.ensure_future(_run_background(task))
            else:
                asyncio.run(_run_background(task))
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.completed_at = time.time()
            _persist(task)
    else:
        # No runner — task stays in "pending" for manual execution
        task.status = "pending"

    return {
        "success": True,
        "task_id": task_id,
        "agent": task.agent,
        "success_criteria": task.success_criteria,
        "status": task.status,
        "mode": task.mode,
        "isolation": task.isolation,
        "message": (
            f"Task {task_id}: {task.status}. Use task_status/task_result to inspect the outcome."
        ),
    }


async def _run_background(task: SubagentTask) -> None:
    try:
        await asyncio.wait_for(_run_task(task), timeout=task.timeout_seconds)
    except asyncio.TimeoutError:
        task.status = "failed"
        task.error = "Task execution deadline exceeded; inspect any partial artifacts before retrying."
        task.completed_at = time.time()
        _persist(task)


async def _run_task(task: SubagentTask) -> None:
    task.status = "running"
    _persist(task)
    try:
        if task.mode == "workspace-write" and task.isolation == "worktree":
            from apps.cli.config_paths import resolve_config_dir
            from .worktrees import WorktreeManager

            manager = WorktreeManager(resolve_config_dir() / "worktrees")
            spec = manager.create(task_id=task.task_id, workspace=task.workspace)
            task.worktree_spec = spec
            task.workspace = spec.path
            task.branch = spec.branch

        task.verification_baseline = capture_baseline(task.workspace, task.checks)
        full_prompt = task.prompt
        if task.context:
            full_prompt = f"{task.context}\n\n{task.prompt}"
        execution_contract = (
            "[Subagent execution contract]\n"
            f"Mode: {task.mode}\n"
            f"Workspace: {task.workspace}\n"
            f"Isolation: {task.isolation}\n"
            "Keep every file and command operation inside this workspace. "
            "Do not spawn another subagent or push changes. End with a structured "
            "handoff: FINDINGS: ...; CHANGED_FILES: ...; VERIFICATION: ...; BLOCKERS: ..."
        )
        profile = get_profile(task.agent)
        full_prompt = (f"{execution_contract}\nRole: {profile.name}\n{profile.instructions}\n"
                       f"Acceptance criteria: {task.success_criteria or 'Report observable evidence and unresolved blockers.'}\n"
                       f"Declared predicates (not permission grants): {json.dumps(task.checks, ensure_ascii=False)}\n\n{full_prompt}")
        if task.backend != "aria":
            from external_agent_runner import RUNNERS

            result_text = await RUNNERS[task.backend](full_prompt, cwd=task.workspace or None)
        else:
            runner_parameters = inspect.signature(_RUNNER).parameters
            if len(runner_parameters) >= 2:
                result_text = await _RUNNER(full_prompt, task)
            else:
                result_text = await _RUNNER(full_prompt)
        task.result = result_text or ""
        task.handoff = _extract_handoff(task.result)
        task.status = "done"
        _refresh_verification(task)
        if task.worktree_spec is not None:
            from .worktrees import WorktreeManager
            task.diff_summary = WorktreeManager.diff(task.worktree_spec)
    except asyncio.CancelledError:
        task.status = "cancelled"
        raise
    except Exception as exc:
        task.error = str(exc)
        task.status = "failed"
    finally:
        if task.status != "done":
            task.verification = {"status": "execution_incomplete", "verified": False if task.checks else None}
        task.completed_at = time.time()
        _persist(task)


def _refresh_verification(task: SubagentTask) -> None:
    try:
        task.verification = verify_checks(task.workspace, task.checks, task.evidence,
                                          task.verification_baseline, receipts_truncated=task.evidence_truncated)
    except (ValueError, TypeError, OSError):
        task.verification = {"status": "unavailable", "verified": False, "checked_at": time.time()}


def _extract_handoff(result: str) -> dict[str, str]:
    """Extract a compact handoff when the agent supplied the task contract."""
    text = str(result or "")
    # Models commonly return Markdown headings or title case even when the
    # contract asks for uppercase labels. Preserve the reported text; parsing
    # it is not independent verification of the claims.
    pattern = r"(?im)(?:^|[;\n])[ \t]*(?:\*\*)?(FINDINGS|CHANGED[ _]FILES|VERIFICATION|BLOCKERS)[ \t]*:[ \t]*(?:\*\*)?"
    matches = list(re.finditer(pattern, text))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        key = match.group(1).lower().replace(" ", "_")
        out[key] = text[match.end():end].strip().strip(";")
    return out


def tool_task_status(params: dict) -> dict:
    """Check the status of a background task."""
    task_id = params.get("task_id", "")
    if not task_id:
        return {"success": False, "error": "Missing 'task_id'"}
    task = _TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"Task '{task_id}' not found"}
    return {
        "success": True,
        "task_id": task_id,
        "agent": task.agent,
        "success_criteria": task.success_criteria,
        "status": task.status,
        "verified": task.verification.get("verified"),
        "verification": task.verification or None,
        "age": task.age_str(),
        "prompt_preview": task.prompt[:100],
        "error": task.error or None,
        "mode": task.mode,
        "isolation": task.isolation,
        "workspace": task.workspace or None,
        "branch": task.branch or None,
    }


def tool_task_result(params: dict) -> dict:
    """Retrieve the result of a completed background task."""
    task_id = params.get("task_id", "")
    if not task_id:
        return {"success": False, "error": "Missing 'task_id'"}
    task = _TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"Task '{task_id}' not found"}
    if task.status == "running":
        return {"success": False, "error": "Task is still running", "status": "running"}
    if task.status == "failed":
        return {"success": False, "error": task.error, "status": "failed"}
    if task.status in ("pending", "cancelled", "interrupted"):
        return {"success": False, "error": f"Task status is '{task.status}'", "status": task.status}
    # Re-read declared artifacts on retrieval; a prior green must not survive
    # deletion or modification of the artifact. Command receipts remain historical.
    _refresh_verification(task)
    _persist(task)
    return {
        "success": task.verification.get("verified") is not False,
        "error": "Declared acceptance checks did not pass" if task.verification.get("verified") is False else None,
        "task_id": task_id,
        "agent": task.agent,
        "success_criteria": task.success_criteria,
        "status": task.status,
        "verified": task.verification.get("verified"),
        "verification": task.verification,
        "result": task.result,
        "age": task.age_str(),
        "workspace": task.workspace or None,
        "branch": task.branch or None,
        "diff_summary": task.diff_summary or None,
        "applied": task.applied,
        "applied_paths": list(task.applied_paths),
        "handoff": task.handoff or None,
    }


def tool_task_list(params: dict) -> dict:
    """List all tracked background tasks."""
    tasks = [t.to_dict() for t in _TASKS.values()]
    if not tasks:
        return {"success": True, "tasks": [], "message": "No active tasks."}
    by_status: dict = {}
    for t in tasks:
        s = t["status"]
        by_status.setdefault(s, []).append(t)
    return {
        "success": True,
        "total": len(tasks),
        "tasks": tasks,
        "summary": {s: len(v) for s, v in by_status.items()},
    }


def clear_finished_tasks() -> int:
    """Forget every task that can no longer change, in memory and on disk."""
    finished = [
        task_id for task_id, task in _TASKS.items()
        if task.status in ("done", "failed", "cancelled", "interrupted")
    ]
    for task_id in finished:
        del _TASKS[task_id]
    try:
        _ledger().remove_many(finished)
    except Exception:
        pass
    return len(finished)


def tool_task_cancel(params: dict) -> dict:
    """Cancel a pending or running background task."""
    task_id = params.get("task_id", "")
    if not task_id:
        return {"success": False, "error": "Missing 'task_id'"}
    task = _TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"Task '{task_id}' not found"}
    if task.status in ("done", "failed", "cancelled"):
        return {"success": False, "error": f"Task already in terminal state: {task.status}"}
    task.status = "cancelled"
    task.completed_at = time.time()
    if task.async_task is not None and not task.async_task.done():
        task.async_task.cancel()
    _persist(task)
    return {"success": True, "task_id": task_id, "cancelled": True}


def apply_task_worktree(task_id: str, *, cleanup: bool = True) -> dict:
    """Apply a completed isolated task to the original clean workspace."""
    task = _TASKS.get(task_id)
    if not task:
        return {"success": False, "error": f"Task '{task_id}' not found"}
    if task.status != "done":
        return {"success": False, "error": f"Task status is '{task.status}', expected 'done'"}
    if task.applied:
        return {"success": False, "error": "Task changes were already applied"}
    if task.worktree_spec is None:
        return {"success": False, "error": "Task has no isolated worktree changes"}
    try:
        from .worktrees import WorktreeManager
        from apps.cli.config_paths import resolve_config_dir

        manager = WorktreeManager(resolve_config_dir() / "worktrees")
        tracked, untracked = manager.changed_paths(task.worktree_spec)
        target_root = Path(task.worktree_spec.repository)
        snapshots = {}
        for relative in (*tracked, *untracked):
            target = target_root / relative
            if target.is_file():
                try:
                    content = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                snapshots[relative] = (
                    True,
                    content,
                    target.stat().st_mode & 0o777,
                )
            elif not target.exists():
                snapshots[relative] = (False, "", None)
        result = manager.apply(task.worktree_spec)
        checkpoint_errors = []
        try:
            from .checkpoints import CheckpointStore
            checkpoint_store = CheckpointStore()
            for relative in result.paths:
                target = target_root / relative
                before = snapshots.get(relative)
                if before is None or not target.is_file():
                    continue
                try:
                    after_content = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                checkpoint_store.record_change(
                    path=target,
                    before_content=before[1],
                    after_content=after_content,
                    existed_before=before[0],
                    before_mode=before[2],
                    source="subagent_apply",
                    session_id=task.session_id,
                    label=f"task {task.task_id}: {relative}",
                    metadata={"task_id": task.task_id, "branch": task.branch},
                )
        except Exception as exc:
            checkpoint_errors.append(str(exc))
        task.applied = True
        task.applied_paths = result.paths
        _persist(task)
        cleanup_error = ""
        if cleanup:
            try:
                manager.remove(task.worktree_spec, force=True)
                manager.delete_branch(task.worktree_spec, force=True)
            except Exception as exc:
                cleanup_error = str(exc)
        return {
            "success": True,
            "task_id": task_id,
            "paths": list(result.paths),
            "cleanup_error": cleanup_error or None,
            "checkpoint_errors": checkpoint_errors,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── Tool registry (added to LOCAL_TOOLS in aria_cli.py) ───────────────────────

SUBAGENT_TOOLS = {
    "list_agents": (tool_list_agents, "Discover general-purpose task roles, limits and tool policies"),
    "spawn_task":    (tool_spawn_task,   "Spawn a background sub-task; returns task_id"),
    "task_status":   (tool_task_status,  "Check status of a background task by task_id"),
    "task_result":   (tool_task_result,  "Retrieve result of a completed background task"),
    "task_list":     (tool_task_list,    "List all active background tasks"),
    "task_cancel":   (tool_task_cancel,  "Cancel a pending or running background task"),
}

SUBAGENT_SCHEMAS = [
    {"name": "list_agents", "description": "Discover task roles for exploration, planning, research, implementation, review, data inspection and writing. Does not start tasks or connect services.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {
        "name": "spawn_task",
        "description": "Delegate a bounded task to a role. Put executable acceptance predicates in the checks argument, not only in prompt/context. For example checks=[{kind:tool,tool:read_file,path:brief.md},{kind:artifact,path:brief.md,format:text,min_bytes:1}]. Retrieve task_result: verified=true covers these predicates, null means no checks, false means failure. A child saying VERIFIED is not machine evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": list(PROFILES), "description": "Use explorer for sources, planner for plans, researcher for sourced research, implementer for changes, reviewer for independent review, data_analyst for datasets, writer for documents. general is the fallback. Specialized roles use aria."},
                "checks": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["artifact", "tool", "command"]}, "path": {"type": "string"}, "format": {"type": "string", "enum": ["binary", "text", "json", "csv"]}, "min_bytes": {"type": "integer", "minimum": 0, "maximum": 1048576}, "min_rows": {"type": "integer", "minimum": 0, "maximum": 10000}, "required_keys": {"type": "array", "items": {"type": "string"}, "maxItems": 32}, "contains": {"type": "string", "maxLength": 256}, "sha256": {"type": "string"}, "changed": {"type": "boolean"}, "tool": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["kind"], "additionalProperties": False}, "description": "Declared acceptance predicates: artifact(path, optional format/min_bytes/required_keys/min_rows/contains/sha256/changed), tool(tool, optional path), command(command, optional cwd). Paths are workspace-relative. Commands are matched to actual executor receipts, never run by this verifier. Checks verify only their predicates, not every aspect of the user goal."},
                "success_criteria": {"type": "string", "description": "Observable acceptance conditions; a completion message alone is not evidence."},
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 30},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "prompt":  {"type": "string", "description": "The task for the sub-agent to perform"},
                "context": {"type": "string", "description": "Optional background context to inject into the sub-agent's system prompt"},
                "mode": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write"],
                    "description": "Use workspace-write only when the subagent must edit code. Default: read-only.",
                },
                "isolation": {
                    "type": "string",
                    "enum": ["auto", "worktree", "shared"],
                    "description": "auto uses a Git worktree for write tasks; shared is only valid for read-only tasks.",
                },
                "backend": {
                    "type": "string",
                    "enum": ["aria", "claude", "codex"],
                    "description": "Which agent actually runs the task. 'aria' (default) uses this session's own loop. 'claude'/'codex' shell out to the Claude Code or Codex CLI (must be installed and on PATH) headlessly — use for coding tasks you want a different coding agent to own.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "task_status",
        "description": "Check the status of a background task spawned with spawn_task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by spawn_task"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_result",
        "description": "Retrieve the full result text of a completed background task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by spawn_task"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List all active and completed background tasks with their statuses.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "task_cancel",
        "description": "Cancel a pending or running background task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id to cancel"},
            },
            "required": ["task_id"],
        },
    },
]
