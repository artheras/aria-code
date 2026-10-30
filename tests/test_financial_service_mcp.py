"""Service configuration and truthful MCP error handling for external finance tools."""
import json
import os
from pathlib import Path
import sys

import pytest
from aria_code.mcp_client import MCPServer
from aria_code.packages.aria_mcp.config import install_financial_service_config


async def test_cli_finance_command_installs_only_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aria_code.apps.cli import _optional
    from aria_code.apps.cli.commands.core_cmds import CoreCommandsMixin
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(_optional, "HAS_MCP", True)
    monkeypatch.setattr(_optional, "MCP_CONFIG_PATH", path)
    messages = []
    holder = SimpleNamespace(
        context=SimpleNamespace(console=SimpleNamespace(print=messages.append), has_rich=False),
        terminal=SimpleNamespace(_mcp_registry=None))
    await CoreCommandsMixin.cmd_mcp(holder, f'finance install "{sys.executable}"')
    assert json.loads(path.read_text())["servers"][0]["command"] == sys.executable
    assert "/mcp reload" in messages[-1]


def test_config_preserves_existing_servers_and_metadata(tmp_path):
    path = tmp_path / "mcp.json"
    original = {"servers": [{"name": "existing", "command": "kept"}], "host": {"setting": True}}
    path.write_text(json.dumps(original))
    install_financial_service_config(path, Path(sys.executable))
    config = json.loads(path.read_text())
    assert config["servers"][0] == original["servers"][0]
    assert config["host"] == original["host"]
    assert config["servers"][1]["env"]["FINANCIAL_SERVICE_NETWORK"] == "0"
    install_financial_service_config(path, Path(sys.executable))
    assert len(json.loads(path.read_text())["servers"]) == 2


def test_invalid_config_and_failed_replace_leave_original_intact(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text("{invalid")
    with pytest.raises(ValueError):
        install_financial_service_config(path, Path(sys.executable))
    assert path.read_text() == "{invalid"
    path.write_text('{"servers":[]}')
    def fail(*args):
        raise OSError("Disk write refused")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        install_financial_service_config(path, Path(sys.executable))
    assert path.read_text() == '{"servers":[]}'
    assert not list(tmp_path.glob(".finance-mcp-*"))


@pytest.mark.parametrize("payload", [
    {"isError": True, "content": [{"type": "text", "text": "validation failed"}]},
    {"isError": True, "content": [{"type": "text", "text": '{"success":true}'}]},
    {"isError": "false", "content": []},
])
async def test_mcp_error_cannot_be_reported_as_success(payload):
    server = MCPServer("test", "unused", [])
    server._running = True
    async def request(*args):
        return payload
    server._request = request
    assert (await server.call_tool("tool", {}))["success"] is False


async def test_actual_financial_service_through_aria_client():
    python = os.getenv("ARIA_FINANCIAL_TEST_PYTHON")
    if not python:
        pytest.skip("Set ARIA_FINANCIAL_TEST_PYTHON to an environment with financial-service installed")
    server = MCPServer("arthera-finance", python, ["-m", "arthera_finance.server"],
                       env={"FINANCIAL_SERVICE_NETWORK": "0", "FINANCIAL_SERVICE_BROKERS_ENABLED": "0"}, timeout=15)
    try:
        assert await server.start()
        assert len(server.tools) == 10
        denied = await server.call_tool("financial_broker_account", {"connection_id": "unknown"})
        assert denied["success"] is False and denied["error_code"] == "broker_access_disabled"
        result = await server.call_tool("financial_analyze_statements", {
            "financials": {"income_statement": {"revenue": 1000, "net_income": 100}}})
        assert result["success"] and result["data"]["net_margin_pct"] == 10
        result = await server.call_tool("financial_market_history", {"symbol": "AAPL"})
        assert result["success"] is False and result["error_code"] == "network_disabled"
        result = await server.call_tool("financial_risk_metrics", {"equity": [100, True]})
        assert result["success"] is False and result["error_code"] == "mcp_tool_error"
    finally:
        await server.stop()
