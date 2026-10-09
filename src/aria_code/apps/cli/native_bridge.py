"""Experimental, workspace-confined JSON-RPC worker for the Rust CLI.

The host selects the workspace and approves one named write via process flags.
Request parameters never establish permissions. Existing CLI handlers retain
their diff, change-store and verification behavior; stdout is protocol only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

MAX_MESSAGE = 1_048_576
READ_TOOLS = frozenset({"read_file", "list_files", "search_code"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def error(code: int, message: str, request_id: Any = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def execute(name: str, arguments: dict, workspace: Path, approved_tool: str | None) -> dict:
    from aria_code.runtime.approval import ApprovalDecision
    from aria_code.runtime.tool_executor import ToolExecutor
    from aria_code.runtime.tool_policy import check_tool_policy

    if name not in READ_TOOLS | WRITE_TOOLS:
        return {"success": False, "error": f"Tool not exposed by native bridge: {name}"}
    verdict = check_tool_policy(name)
    if verdict == "deny":
        return {"success": False, "error": "Blocked by persistent tool policy"}
    approved = name in WRITE_TOOLS and name == approved_tool
    if verdict == "ask" and not approved:
        return {"success": False, "error": "Tool requires explicit approval; use the interactive Python CLI"}
    if name in WRITE_TOOLS and not approved:
        return {"success": False, "error": "Writes require host --approve-write for this invocation"}
    def handler(params: dict) -> dict:
        # Load UI-dependent write code only after ToolExecutor grants the path.
        if name in READ_TOOLS:
            from aria_code.apps.cli.tools import file_tools
            return getattr(file_tools, "tool_" + name)(params)
        from aria_code.apps.cli.tools import write_tools
        cli = write_tools._ac()
        console = getattr(cli, "console", None)
        previous = console.file if console is not None else None
        try:
            if console is not None:
                console.file = sys.stderr
            return getattr(write_tools, "tool_" + name)(params)
        finally:
            if console is not None:
                console.file = previous
    executor = ToolExecutor(
        {name: (handler,)},
        config={"workspace_root": str(workspace), "permission_mode": "workspace-write" if approved else "read-only"},
        execution_context=lambda: {"_workspace_restricted": True},
    )
    decision = ApprovalDecision.allow(user_approved=True, tool_scope=name) if approved else None
    return executor.execute_local(name, arguments, approval=decision)


def dispatch(request: Any, workspace: Path, approved_tool: str | None = None) -> dict | None:
    if not isinstance(request, dict):
        return error(-32600, "Expected a single JSON-RPC request object")
    request_id = request.get("id")
    valid_id = request_id is None or isinstance(request_id, str) or type(request_id) is int
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str) or not valid_id:
        return error(-32600, "Invalid JSON-RPC request")
    # This bridge only executes requests with an ID. Notifications never cause
    # file changes and never produce a response, including unknown methods.
    if "id" not in request:
        return None
    if request["method"] != "tools.call":
        return error(-32601, "Method not found", request_id)
    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments"), dict):
        return error(-32602, "Expected name and arguments object", request_id)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(params["name"], params["arguments"], workspace, approved_tool)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return error(-32603, f"Python tool failed: {exc}", request_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--approve-tool", choices=sorted(WRITE_TOOLS))
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        parser.error("workspace must be a directory")
    # One request per process bounds trace memory and approval lifetime. Read
    # size is bounded before JSON decoding; no unbounded readline allocation.
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE:
        response = error(-32600, "Request exceeds 1 MiB")
    else:
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            response = error(-32700, "Parse error")
        else:
            response = dispatch(request, workspace, args.approve_tool)
    if response is not None:
        encoded = json.dumps(response, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(encoded) + 1 > MAX_MESSAGE:
            encoded = json.dumps(error(-32603, "Response exceeds 1 MiB", response["id"])).encode("ascii")
        sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
