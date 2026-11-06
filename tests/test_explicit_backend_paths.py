"""A fresh clone must not acquire services from somebody's desktop checkout."""

from pathlib import Path
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest


def test_no_backend_discovery_or_mcp_config_without_explicit_opt_in(monkeypatch, tmp_path):
    from aria_code.packages.aria_infra.arthera import discover_arthera_packages
    from aria_code.packages.aria_mcp.config import arthera_quant_engine_server_config

    monkeypatch.delenv("ARTHERA_ROOT", raising=False)
    # A valid-looking historical developer layout must have no effect.
    (tmp_path / "Desktop/Arthera/packages/quant_engine").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    discovered = discover_arthera_packages()
    assert discovered.available is False
    assert discovered.root is None
    assert discovered.to_dict()["root"] == ""
    with pytest.raises(ValueError, match="explicit ARTHERA_ROOT"):
        arthera_quant_engine_server_config()


def test_explicit_backend_is_configurable_without_changing_the_default(monkeypatch, tmp_path):
    from aria_code.packages.aria_infra.arthera import discover_arthera_packages
    from aria_code.packages.aria_mcp.config import arthera_quant_engine_server_config

    backend = tmp_path / "team-backend"
    (backend / "packages/quant_engine").mkdir(parents=True)
    monkeypatch.setenv("ARTHERA_ROOT", str(backend))
    assert discover_arthera_packages().root == backend / "packages"
    assert arthera_quant_engine_server_config()["env"]["PYTHONPATH"] == str(backend)
    monkeypatch.setenv("ARTHERA_ROOT", "relative-checkout")
    with pytest.raises(ValueError, match="absolute"):
        arthera_quant_engine_server_config()


def test_financial_helpers_do_not_append_implicit_source_directories(monkeypatch):
    from aria_code.packages.aria_tools.financial._paths import ensure_arthera_root, ensure_arthera_sdk

    for name in ("ARTHERA_ROOT", "ARTHERA_PATH", "ARTHERA_SDK_PATH"):
        monkeypatch.delenv(name, raising=False)
    before = list(sys.path)
    # Even if every historical guessed directory existed, none is consulted.
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    assert ensure_arthera_sdk() is False
    assert ensure_arthera_root() is False
    assert sys.path == before


@pytest.mark.parametrize("backend_state", ["unset", "missing", "valid"])
def test_connect_only_writes_an_explicit_real_backend(monkeypatch, tmp_path, capsys, backend_state):
    from aria_code.apps.cli.commands import workspace_cmds as commands

    config_path = tmp_path / "mcp_servers.json"
    existing = {"servers": [{"name": "other-team-server", "command": "keep-me"}]}
    config_path.write_text(json.dumps(existing))
    backend = tmp_path / "backend"
    server = backend / "packages/quant_engine/mcp_server.py"
    monkeypatch.delenv("ARTHERA_ROOT", raising=False)
    if backend_state != "unset":
        monkeypatch.setenv("ARTHERA_ROOT", str(backend))
    if backend_state == "valid":
        server.parent.mkdir(parents=True)
        server.write_text("# Test fixture; never executed by connect.\n")
    monkeypatch.setattr(commands, "_get_MCP_CONFIG_PATH", lambda: config_path)
    monkeypatch.setattr(commands, "_get__HAS_MCP", lambda: True)
    monkeypatch.setattr(commands, "_get___version__", lambda: "test")
    monkeypatch.setattr(commands, "_get_LOCAL_TOOLS", dict)
    monkeypatch.setattr(commands, "_get_LOCAL_TOOL_SCHEMAS", list)
    instance = commands.WorkspaceCommandsMixin()
    instance.context = SimpleNamespace(has_rich=False, console=None)
    instance.terminal = SimpleNamespace()
    asyncio.run(instance.cmd_packages("connect arthera"))
    written = json.loads(config_path.read_text())
    if backend_state == "valid":
        assert written["servers"][0] == existing["servers"][0]
        assert written["servers"][1]["args"] == [str(server)]
        assert "Connected" in capsys.readouterr().out
    else:
        assert written == existing
        assert "Connected" not in capsys.readouterr().out
