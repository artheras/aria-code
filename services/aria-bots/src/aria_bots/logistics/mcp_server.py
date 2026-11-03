"""The logistics tools as an MCP server, so the Go engine can use them.

    aria-logistics-mcp [--data-dir DIR]

Speaks MCP over stdio (newline-delimited JSON-RPC 2.0). Three read-only
tools: analyze_logistics_data, score_carriers, plan_inventory_policy.

Shipper scope is not the model's choice. The server reads ARIA_OWNER_SCOPE
from its own environment (the engine passes it down from the bot that started
the turn) and every tool goes through logistics.tenancy, which fails closed.

A `file_path` argument is read only from inside the data folder
(`--data-dir`, else ARIA_LOGISTICS_DATA_DIR). With no data folder, files are
refused and the tools take inline records only: a chat must not be able to
name any file on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TextIO

from .carriers import SCHEMA as CARRIER_SCHEMA, tool_score_carriers
from .freight import tool_analyze_logistics_data
from .inventory import SCHEMA as INVENTORY_SCHEMA, tool_plan_inventory_policy
from .tenancy import OWNER_SCOPE_ENV, owner_params_schema

PROTOCOL_VERSION = "2024-11-05"
DATA_DIR_ENV = "ARIA_LOGISTICS_DATA_DIR"

FREIGHT_SCHEMA = {
    "name": "analyze_logistics_data",
    "description": "Analyze actual waybills from supplied records, a CSV/JSON file, or an existing local database",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV or JSON waybill file path"},
            "waybills": {"type": "array", "description": "Waybill records with total_cost"},
            **owner_params_schema(),
        },
    },
}

TOOLS: Dict[str, tuple[Callable[[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = {
    "analyze_logistics_data": (tool_analyze_logistics_data, FREIGHT_SCHEMA),
    "score_carriers": (tool_score_carriers, CARRIER_SCHEMA),
    "plan_inventory_policy": (tool_plan_inventory_policy, INVENTORY_SCHEMA),
}


def tool_list() -> list[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": schema["description"],
            "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        }
        for name, (_, schema) in TOOLS.items()
    ]


def confine(arguments: Dict[str, Any], data_dir: Optional[Path]) -> Dict[str, Any]:
    """Resolve file_path inside the data folder, or refuse it."""
    file_path = arguments.get("file_path")
    if not file_path:
        return arguments
    if data_dir is None:
        raise ValueError(f"Files are not available here; set {DATA_DIR_ENV} or pass records inline")
    root = data_dir.resolve()
    candidate = Path(str(file_path)).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if path != root and root not in path.parents:
        raise ValueError("file_path must be inside the logistics data folder")
    return {**arguments, "file_path": str(path)}


def call_tool(name: str, arguments: Any, data_dir: Optional[Path]) -> Dict[str, Any]:
    entry = TOOLS.get(name)
    if entry is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = entry[0](confine(arguments, data_dir))
    except ValueError as exc:
        result = {"success": False, "error": str(exc)}
    except Exception as exc:  # a tool bug must not take the server down
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    failed = isinstance(result, dict) and result.get("success") is False
    text = json.dumps(result, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def handle(message: Dict[str, Any], data_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    if "id" not in message:
        return None  # a notification
    reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
    params = message.get("params") or {}
    if method == "initialize":
        scope = os.environ.get(OWNER_SCOPE_ENV, "").strip()
        reply["result"] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aria-logistics", "version": "0.1.0"},
            "instructions": "Logistics analyses scoped to one shipper." if scope else
            "Logistics analyses; data spanning shippers needs owner_id or all_owners.",
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": tool_list()}
    elif method == "tools/call":
        reply["result"] = call_tool(str(params.get("name", "")), params.get("arguments"), data_dir)
    else:
        reply["error"] = {"code": -32601, "message": f"Method not found: {method}"}
    return reply


def serve(stdin: TextIO, stdout: TextIO, data_dir: Optional[Path]) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(message, data_dir) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="aria-logistics-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get(DATA_DIR_ENV) or None,
                        help=f"folder file_path is read from (default ${DATA_DIR_ENV}; none: inline records only)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    serve(sys.stdin, sys.stdout, data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
