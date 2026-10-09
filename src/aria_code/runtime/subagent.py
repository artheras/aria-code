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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .task_ledger import TaskLedger

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
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the serializable task contract and latest outcome."""
        return {
            "task_id": self.task_id, "prompt": self.prompt, "context": self.context,
            "status": self.status, "result": self.result, "error": self.error,
            "mode": self.mode, "isolation": self.isolation, "backend": self.backend,
            "workspace": self.workspace, "session_id": self.session_id,
            "branch": self.branch, "diff_summary": self.diff_summary,
            "applied": self.applied, "applied_paths": list(self.applied_paths),
            "created_at": self.created_at, "completed_at": self.completed_at,
            "handoff": self.handoff,
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
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task.async_task = asyncio.ensure_future(_run_background(task))
            else:
                loop.run_until_complete(_run_background(task))
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
        "status": task.status,
        "mode": task.mode,
        "isolation": task.isolation,
        "message": (
            f"Task {task_id} spawned. Use task_status('{task_id}') to check progress."
            if task.status == "running" else
            f"Task {task_id} queued (no runner registered). Check /tasks."
        ),
    }


async def _run_background(task: SubagentTask) -> None:
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
        full_prompt = f"{execution_contract}\n\n{full_prompt}"
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
        if task.worktree_spec is not None:
            from .worktrees import WorktreeManager
            task.diff_summary = WorktreeManager.diff(task.worktree_spec)
    except asyncio.CancelledError:
        task.status = "cancelled"
    except Exception as exc:
        task.error = str(exc)
        task.status = "failed"
    finally:
        task.completed_at = time.time()
        _persist(task)


def _extract_handoff(result: str) -> dict[str, str]:
    """Extract a compact handoff when the agent supplied the task contract."""
    text = str(result or "")
    keys = ("FINDINGS", "CHANGED_FILES", "VERIFICATION", "BLOCKERS")
    out: dict[str, str] = {}
    for index, key in enumerate(keys):
        marker = f"{key}:"
        start = text.rfind(marker)
        if start < 0:
            continue
        end = len(text)
        for later_key in keys[index + 1:]:
            candidate = text.find(f"{later_key}:", start + len(marker))
            if candidate >= 0:
                end = min(end, candidate)
        out[key.lower()] = text[start + len(marker):end].strip().strip(";")
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
        "status": task.status,
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
    return {
        "success": True,
        "task_id": task_id,
        "status": task.status,
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
    "spawn_task":    (tool_spawn_task,   "Spawn a background sub-task; returns task_id"),
    "task_status":   (tool_task_status,  "Check status of a background task by task_id"),
    "task_result":   (tool_task_result,  "Retrieve result of a completed background task"),
    "task_list":     (tool_task_list,    "List all active background tasks"),
    "task_cancel":   (tool_task_cancel,  "Cancel a pending or running background task"),
}

SUBAGENT_SCHEMAS = [
    {
        "name": "spawn_task",
        "description": "Spawn an independent background agent task. Useful for parallelising slow operations: research, data fetching, long analysis. Returns a task_id you can poll with task_status.",
        "parameters": {
            "type": "object",
            "properties": {
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
