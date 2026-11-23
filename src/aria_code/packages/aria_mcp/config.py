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
    # -I (isolated mode): the server is spawned in the user's workspace, and a
    # plain `-m` puts the current directory first on sys.path, so a planted
    # arthera_finance/ or a stdlib-named .py there would run in its place. -I
    # also ignores PYTHON* variables and the user site directory.
    #
    # No "env": an entry here overrides the host's environment, so a pinned
    # FINANCIAL_SERVICE_NETWORK=0 meant market data and brokers could never be
    # enabled. The service itself defaults to offline; the host decides.
    return {"name": "arthera-finance", "command": str(python),
            "args": ["-I", "-m", "arthera_finance.server"],
            "description": "Shared financial analysis and simulation; offline by default",
            "enabled": True}


def trading_service_server_config(python: Path) -> Dict[str, object]:
    """Separate paper/trading MCP server from financial-service (gated by host env)."""
    python = Path(python).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Provide an absolute path to the financial-service Python interpreter")
    return {"name": "arthera-trading", "command": str(python),
            "args": ["-I", "-m", "arthera_finance.trading_server"],
            "description": "Paper/trading order preview and execution; gated by ARTHERA_TRADING_ENABLED",
            "enabled": True}


def healthcare_service_server_config(python: Path) -> Dict[str, object]:
    """Separately installed healthcare-service MCP server (biomedical literature & evidence)."""
    python = Path(python).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Provide an absolute path to the healthcare-service Python interpreter")
    return {"name": "aria-healthcare", "command": str(python),
            "args": ["-I", "-m", "aria_healthcare.server"],
            "description": "Biomedical literature, trials, and clinical evidence over MCP",
            "enabled": True}


def erp_service_server_config(python: Path) -> Dict[str, object]:
    """Separately installed erp-service MCP server (warehouse, inventory, carriers)."""
    python = Path(python).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Provide an absolute path to the erp-service Python interpreter")
    return {"name": "erp-service", "command": str(python),
            "args": ["-I", "-m", "erp_service.mcp_server"],
            "description": "3PL warehouse, inventory planning, carrier scoring, and freight audit",
            "enabled": True}


def creative_media_plugin_server_config(plugin_python_dir: Path) -> Dict[str, object]:
    """External creative-media MCP plugin from artheras/aria-plugins."""
    plugin_python_dir = Path(plugin_python_dir).expanduser().resolve()
    if not plugin_python_dir.is_dir():
        raise ValueError("Provide a valid directory for aria-plugins creative-media python/")
    return {
        "name": "aria-creative-media",
        "command": str(Path(sys.executable)),
        "args": ["-m", "aria_creative.mcp_server"],
        "env": {"PYTHONPATH": str(plugin_python_dir)},
        "description": "Figma, Canva, OpenAI Images, Kling, and Runway creative MCP tools",
        "enabled": True,
    }


def computer_use_server_config(python: Path) -> Dict[str, object]:
    """Isolated local browser Computer Use MCP server (host sets ARIA_COMPUTER_ORIGINS)."""
    python = Path(python).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Provide an absolute path to the aria-code Python interpreter")
    return {
        "name": "aria-computer-use",
        "command": str(python),
        "args": ["-I", "-m", "aria_code.computer_use_server"],
        "description": "Isolated browser automation with Set-of-Marks, scroll/wait/batch actions, and native approval",
        "enabled": True,
    }


def discover_sibling_ecosystem_servers(
    workspace: Optional[Path] = None,
) -> List[Dict[str, object]]:
    """Auto-discover sibling financial-service, erp-service, and healthcare-service MCP servers."""
    roots: List[Path] = []
    for candidate in (
        Path(workspace).expanduser().resolve().parent if workspace is not None else None,
        Path.cwd().resolve().parent,
        Path(__file__).resolve().parents[4].parent,
        Path.home() / "Desktop",
    ):
        if candidate is not None and candidate not in roots:
            roots.append(candidate)

    specs = (
        ("financial-service", financial_service_server_config),
        ("erp-service", erp_service_server_config),
        ("healthcare-service", healthcare_service_server_config),
    )
    servers: List[Dict[str, object]] = []
    seen_names: set[str] = set()
    for repo_dir, builder in specs:
        for root in roots:
            for rel_py in (Path(".venv") / "bin" / "python", Path(".venv") / "Scripts" / "python.exe"):
                py_path = root / repo_dir / rel_py
                if py_path.is_file():
                    cfg = builder(py_path)
                    name = str(cfg.get("name", ""))
                    if name and name not in seen_names:
                        seen_names.add(name)
                        servers.append(cfg)
                    break
            if any(str(s.get("command", "")).startswith(str(root / repo_dir)) for s in servers):
                break
    for root in roots:
        creative_py = root / "aria-plugins" / "plugins" / "creative-media" / "python"
        if (creative_py / "aria_creative" / "mcp_server.py").is_file():
            if "aria-creative-media" not in seen_names:
                seen_names.add("aria-creative-media")
                servers.append(creative_media_plugin_server_config(creative_py))
            break
    return servers


def _atomic_write_json(path: Path, payload: Dict[str, object], prefix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=prefix, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def install_financial_service_config(path: Path, python: Path, *, include_trading: bool = False) -> Path:
    """Explicit host operation: preserve other servers and replace config atomically."""
    server = financial_service_server_config(python)
    path = Path(path)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"servers": []}
    if (not isinstance(existing, dict) or not isinstance(existing.get("servers", []), list)
            or any(not isinstance(item, dict) for item in existing.get("servers", []))):
        raise ValueError("MCP configuration must contain a list of server objects")
    merged = merge_server_config(existing, server)
    if include_trading:
        merged = merge_server_config(merged, trading_service_server_config(python))
    return _atomic_write_json(path, merged, ".finance-mcp-")


def install_ecosystem_service_configs(
    path: Path,
    *,
    financial_python: Optional[Path] = None,
    include_trading: bool = False,
    healthcare_python: Optional[Path] = None,
    erp_python: Optional[Path] = None,
) -> Path:
    """Atomically register any subset of ecosystem MCP services in `path`."""
    path = Path(path)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"servers": []}
    if (not isinstance(existing, dict) or not isinstance(existing.get("servers", []), list)
            or any(not isinstance(item, dict) for item in existing.get("servers", []))):
        raise ValueError("MCP configuration must contain a list of server objects")
    merged = existing
    if financial_python is not None:
        merged = merge_server_config(merged, financial_service_server_config(financial_python))
        if include_trading:
            merged = merge_server_config(merged, trading_service_server_config(financial_python))
    if healthcare_python is not None:
        merged = merge_server_config(merged, healthcare_service_server_config(healthcare_python))
    if erp_python is not None:
        merged = merge_server_config(merged, erp_service_server_config(erp_python))
    return _atomic_write_json(path, merged, ".ecosystem-mcp-")


def arthera_quant_engine_server_config(
    arthera_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Return a ready-to-write MCP server entry for Arthera QuantEngine."""

    configured = arthera_root or os.getenv("ARTHERA_ROOT", "").strip()
    if not configured:
        raise ValueError("The private backend bridge requires an explicit ARTHERA_ROOT; use /finance setup for the standalone financial-service.")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ValueError("ARTHERA_ROOT must be an absolute path.")
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
