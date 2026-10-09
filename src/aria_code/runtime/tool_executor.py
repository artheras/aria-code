"""Tool execution layer for Aria Code runtimes."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from .approval import ApprovalDecision, apply_approval_decision
from .events import RuntimeTrace, ToolCallRecord

ToolHandler = Callable[[dict], dict]
RemoteExecutor = Callable[[str, dict], Awaitable[dict]]
Hook = Callable[[str, str, dict, Optional[dict]], None]
ExecutionContextProvider = Callable[[], Mapping[str, Any]]


class ToolExecutor:
    """Execute local/remote tools with hooks, policy injection, and trace records."""

    def __init__(
        self,
        local_tools: Mapping[str, tuple],
        *,
        remote_executor: RemoteExecutor | None = None,
        hook: Hook | None = None,
        trace: RuntimeTrace | None = None,
        config: Dict[str, Any] | None = None,
        execution_context: ExecutionContextProvider | None = None,
    ) -> None:
        self.local_tools = local_tools
        self.remote_executor = remote_executor
        self.hook = hook
        self.trace = trace or RuntimeTrace()
        self.config = config or {}
        self.execution_context = execution_context

    def execute_local(
        self, tool_name: str, params: dict, *, approval: ApprovalDecision | None = None
    ) -> dict:
        """Execute a local tool synchronously."""
        if tool_name not in self.local_tools:
            return {"success": False, "error": f"Unknown local tool: {tool_name}"}
        if approval is not None and not approval.approved:
            reason = approval.reason or "Tool approval denied."
            self.trace.emit("tool_denied", {"tool": tool_name, "reason": reason})
            return {"success": False, "error": reason}
        handler = self.local_tools[tool_name][0]
        params = self._prepare_params(
            tool_name,
            params,
            include_execution_context=True,
            bind_research_context=True,
            approval=approval,
        )
        context_error = params.pop("_execution_context_error", None)
        if context_error:
            result = {"success": False, "error": context_error}
            self.trace.emit("tool_denied", {"tool": tool_name, "reason": context_error})
            return result
        return self._call_with_trace(tool_name, params, lambda: handler(params))

    async def execute(self, tool_name: str, params: dict) -> dict:
        """Execute a tool asynchronously, using remote executor when needed."""
        if tool_name in self.local_tools:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.execute_local, tool_name, params)
        if self.remote_executor is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        params = self._prepare_params(tool_name, params, bind_research_context=True)
        start = time.time()
        self._run_hook("pre_tool", tool_name, params)
        self.trace.emit("tool_call", {"tool": tool_name, "params": params})
        try:
            result = await self.remote_executor(tool_name, params)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        self._run_hook("post_tool", tool_name, params, result)
        end = time.time()
        self.trace.add_tool_call(ToolCallRecord(
            tool=tool_name,
            params=params,
            result=result,
            elapsed_ms=(end - start) * 1000,
            started_at=start,
            ended_at=end,
        ))
        return result

    def _call_with_trace(self, tool_name: str, params: dict, fn: Callable[[], dict]) -> dict:
        start = time.time()
        self._run_hook("pre_tool", tool_name, params)
        self.trace.emit("tool_call", {"tool": tool_name, "params": params})
        try:
            result = fn()
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        self._run_hook("post_tool", tool_name, params, result)
        end = time.time()
        self.trace.add_tool_call(ToolCallRecord(
            tool=tool_name,
            params=params,
            result=result,
            elapsed_ms=(end - start) * 1000,
            started_at=start,
            ended_at=end,
        ))
        return result

    def _prepare_params(
        self,
        tool_name: str,
        params: dict,
        *,
        include_execution_context: bool = False,
        bind_research_context: bool = False,
        approval: ApprovalDecision | None = None,
    ) -> dict:
        prepared = dict(params or {})
        # A model cannot grant itself directories or replace the host workspace.
        for key in tuple(prepared):
            if key.startswith("_"):
                prepared.pop(key)
        if tool_name == "run_command":
            # Tool arguments come from the model. Only the host configuration and
            # a typed approval decision may set execution controls.
            for key in (
                "policy", "permission_mode", "network_enabled",
                "user_approved", "_upgrade_policy", "sandbox", "os_sandbox",
            ):
                prepared.pop(key, None)
        workspace_tools = {"read_file", "write_file", "edit_file", "multi_edit", "list_files",
                           "search_code", "glob", "analyze_file", "notebook_read", "notebook_edit",
                           "apply_patch", "run_command", "github"}
        context = {
            "_workspace": self.config.get("_session_workspace_root") or self.config.get("workspace_root") or str(Path.cwd()),
            "_allowed_read_roots": [*(self.config.get("read_roots") or ()), *(self.config.get("_session_read_roots") or ())],
            "_allowed_write_roots": [*(self.config.get("write_roots") or ()), *(self.config.get("_session_write_roots") or ())],
            "_permission_mode": self.config.get("permission_mode", "workspace-write"),
        } if tool_name in workspace_tools else {}
        if self.execution_context is not None and (
            include_execution_context or bind_research_context
        ):
            try:
                context.update(dict(self.execution_context() or {}))
                if bind_research_context:
                    self._bind_research_run(tool_name, prepared, context)
            except Exception as exc:
                if include_execution_context:
                    prepared["_execution_context_error"] = (
                        f"Execution context unavailable: {exc}"
                    )
        if include_execution_context:
            for key, value in context.items():
                if key.startswith("_") and value is not None:
                    prepared[key] = value
            self._bind_workspace(tool_name, prepared, context)
            self._check_file_permission(tool_name, prepared)
            if tool_name in {"write_file", "edit_file", "multi_edit", "notebook_edit"}:
                if approval is not None and approval.approved:
                    prepared["_skip_confirm"] = True
        if tool_name == "run_command":
            prepared["policy"] = self.config.get("command_policy", "safe")
            prepared["permission_mode"] = self.config.get("permission_mode", "workspace-write")
            prepared["network_enabled"] = bool(self.config.get("network_enabled", True))
            prepared["sandbox"] = bool(self.config.get("command_sandbox", False))
            prepared["os_sandbox"] = self.config.get("os_sandbox", "auto")
            if approval is not None and approval.approved:
                apply_approval_decision(prepared, approval)
        return prepared

    def _check_file_permission(self, tool_name: str, prepared: dict) -> None:
        if prepared.get("_execution_context_error"):
            return
        from aria_code.workspace.files import WorkspaceSecurity
        write_tools = {"write_file", "edit_file", "multi_edit", "apply_patch", "notebook_edit"}
        read_tools = {"read_file", "list_files", "search_code", "glob", "analyze_file", "notebook_read"}
        canonical = tool_name.rsplit("__", 1)[-1]
        mode = str(self.config.get("permission_mode", "workspace-write"))
        if canonical in write_tools and mode in {"read-only", "readonly", "read_only", "plan"}:
            prepared["_execution_context_error"] = "Writes are blocked in read-only mode."
            return
        if canonical not in write_tools | read_tools or mode == "full-access":
            return
        security = WorkspaceSecurity.from_tool_params(prepared)
        try:
            security.require_safe(
                prepared.get("root" if canonical == "glob" else "path") or ".",
                write=canonical in write_tools,
            )
        except PermissionError as exc:
            prepared["_execution_context_error"] = (
                f"{exc}. Add --add-dir for writes or --read-dir for reads."
            )

    @staticmethod
    def _bind_research_run(
        tool_name: str,
        prepared: dict,
        context: Mapping[str, Any],
    ) -> None:
        """Correlate an institutional research run with its upstream Aria run."""
        canonical_name = str(tool_name).rsplit("__", 1)[-1]
        if canonical_name != "research_run_create":
            return
        control_run_id = context.get("_run_id")
        trace_id = context.get("_trace_id") or control_run_id
        if control_run_id:
            prepared.setdefault("control_run_id", str(control_run_id))
        if trace_id:
            prepared.setdefault("trace_id", str(trace_id))

    @staticmethod
    def _bind_workspace(tool_name: str, prepared: dict, context: Mapping[str, Any]) -> None:
        workspace_value = context.get("_workspace")
        if not workspace_value:
            return
        workspace = Path(str(workspace_value)).expanduser().resolve()
        restricted = bool(context.get("_workspace_restricted", False))
        # A task worktree stands in for the repository it was made from. The
        # model still sees that repository's paths — in the project context,
        # in earlier turns — so a path into it is taken to mean the same file
        # in the worktree, and the user's own copy is never written.
        origins = _origins(context.get("_workspace_origin"), workspace)
        path_tools = {
            "read_file",
            "write_file",
            "edit_file",
            "multi_edit",
            "list_files",
            "search_code",
            "glob",
            "analyze_file",
            "notebook_read",
            "notebook_edit",
        }
        if tool_name in path_tools:
            path_key = "root" if tool_name == "glob" else "path"
            raw_path = str(prepared.get(path_key) or ".")
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                target = workspace / target
            target = _into_workspace(target.resolve(), origins, workspace)
            if restricted and not target.is_relative_to(workspace):
                prepared["_execution_context_error"] = (
                    f"Tool path is outside the isolated workspace: {target}"
                )
                return
            prepared[path_key] = str(target)
        if tool_name in {"run_command", "github"}:
            raw_cwd = str(prepared.get("cwd") or workspace)
            cwd = Path(raw_cwd).expanduser()
            if not cwd.is_absolute():
                cwd = workspace / cwd
            cwd = _into_workspace(cwd.resolve(), origins, workspace)
            if origins and prepared.get("command"):
                prepared["command"] = _rebase_command(str(prepared["command"]), origins, workspace)
            if restricted and not cwd.is_relative_to(workspace):
                prepared["_execution_context_error"] = (
                    f"Command cwd is outside the isolated workspace: {cwd}"
                )
                return
            prepared["cwd"] = str(cwd)

    def _run_hook(self, hook_type: str, tool_name: str, params: dict, result: dict | None = None) -> None:
        if self.hook is None:
            return
        try:
            self.hook(hook_type, tool_name, params, result)
        except Exception:
            pass


def _origins(value: Any, workspace: Path) -> tuple[Path, ...]:
    """The origin repository as written and as resolved, unless it is the workspace."""
    if not value:
        return ()
    raw = Path(str(value)).expanduser()
    found = []
    for candidate in (raw, raw.resolve()):
        if candidate != workspace and candidate not in found:
            found.append(candidate)
    return tuple(found)


def _into_workspace(target: Path, origins: tuple[Path, ...], workspace: Path) -> Path:
    if target.is_relative_to(workspace):
        return target
    for origin in origins:
        if target.is_relative_to(origin):
            return workspace / target.relative_to(origin)
    return target


def _rebase_command(command: str, origins: tuple[Path, ...], workspace: Path) -> str:
    """Point absolute paths into the origin at the worktree: ``cd /repo && …``."""
    for origin in sorted(origins, key=lambda path: len(str(path)), reverse=True):
        pattern = re.escape(str(origin)) + r"(?=[/\s'\"`;:)|&]|$)"
        command = re.sub(pattern, lambda _match: str(workspace), command)
    return command
