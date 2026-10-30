"""MCP configuration helpers for Aria and Arthera package bridges."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def financial_service_server_config(python: Path) -> Dict[str, object]:
    """A separately installed finance service; never install or run code here."""
    python = Path(python).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Provide an absolute path to the financial-service Python interpreter")
    return {"name": "arthera-finance", "command": str(python),
            "args": ["-m", "arthera_finance.server"],
            "env": {"FINANCIAL_SERVICE_NETWORK": "0"},
            "description": "Shared financial analysis and simulation; offline by default",
            "enabled": True}


def install_financial_service_config(path: Path, python: Path) -> Path:
    """Explicit host operation: preserve other servers and replace config atomically."""
    server = financial_service_server_config(python)
    path = Path(path)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"servers": []}
    if (not isinstance(existing, dict) or not isinstance(existing.get("servers", []), list)
            or any(not isinstance(item, dict) for item in existing.get("servers", []))):
        raise ValueError("MCP configuration must contain a list of server objects")
    merged = merge_server_config(existing, server)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".finance-mcp-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(merged, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def arthera_quant_engine_server_config(
    arthera_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Return a ready-to-write MCP server entry for Arthera QuantEngine."""

    root = (arthera_root or Path.home() / "Desktop" / "Arthera").expanduser()
    server = root / "packages" / "quant_engine" / "mcp_server.py"
    configured_python = os.getenv("ARTHERA_MCP_PYTHON", "").strip()
    candidates = [
        Path(configured_python).expanduser() if configured_python else None,
        root / ".venv-mcp" / "bin" / "python",
        root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    python = next((path for path in candidates if path and path.exists()), Path("python3"))
    return {
        "name": "arthera_quant_engine",
        "command": str(python),
        "args": [str(server)],
        "env": {"PYTHONPATH": str(root), "PYTHONUNBUFFERED": "1"},
        "description": "Arthera QuantEngine tools exposed through MCP",
        "enabled": True,
    }


def merge_server_config(existing: Dict[str, object], server: Dict[str, object]) -> Dict[str, object]:
    """Return config with server upserted by name."""

    servers = list(existing.get("servers") or [])
    name = server.get("name")
    out = []
    replaced = False
    for item in servers:
        item_name = item.get("name") if isinstance(item, dict) else None
        is_legacy_arthera = name == "arthera_quant_engine" and item_name == "quant_engine"
        if isinstance(item, dict) and (item_name == name or is_legacy_arthera):
            if not replaced:
                out.append(server)
                replaced = True
        else:
            out.append(item)
    if not replaced:
        out.append(server)
    return {**existing, "servers": out}


def load_mcp_config(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"servers": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"servers": []}


def write_mcp_config(path: Path, config: Dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def find_server_config(config: Dict[str, object], name: str) -> Optional[Dict[str, object]]:
    for item in config.get("servers") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def mcp_server_status(
    config_path: Path,
    server_name: str = "arthera_quant_engine",
    runtime_status: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return config/file/runtime status for a configured MCP server."""

    config = load_mcp_config(config_path)
    server = find_server_config(config, server_name)
    runtime = None
    for item in runtime_status or []:
        if item.get("name") == server_name:
            runtime = item
            break

    server_path = None
    if server:
        args = server.get("args") or []
        if args:
            server_path = Path(str(args[0])).expanduser()

    return {
        "name": server_name,
        "config_path": str(config_path),
        "configured": server is not None,
        "server_path": str(server_path) if server_path else "",
        "server_file_exists": bool(server_path and server_path.exists()),
        "running": bool(runtime and runtime.get("running")),
        "tool_count": int(runtime.get("tool_count", 0)) if runtime else 0,
        "tools": list(runtime.get("tools", [])) if runtime else [],
        "description": (server or {}).get("description", ""),
    }
