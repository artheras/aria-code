from types import SimpleNamespace

import pytest

from aria_code.mcp_client import MCPToolRegistry, model_safe_tool_name
from aria_code.runtime.tool_executor import ToolExecutor


def test_model_safe_tool_name_uses_provider_compatible_identifier():
    assert model_safe_tool_name("quant engine", "market/quote") == (
        "mcp__quant_engine__market_quote"
    )


def test_mcp_registry_exposes_safe_model_name_without_losing_internal_route(tmp_path):
    registry = MCPToolRegistry(config_path=tmp_path / "mcp.json")
    registry._servers = {
        "quant engine": SimpleNamespace(
            tools=[{
                "name": "market/quote",
                "description": "Fetch a quote",
                "inputSchema": {"type": "object", "properties": {}},
            }]
        )
    }
    tools = {}
    schemas = []

    added = registry.register_into(tools, schemas)

    safe_name = "mcp__quant_engine__market_quote"
    assert added == 1
    assert safe_name in tools
    assert "quant engine/market/quote" not in tools
    assert schemas[0]["function"]["name"] == safe_name


@pytest.mark.asyncio
async def test_registered_mcp_handler_runs_on_registry_owner_loop(tmp_path, monkeypatch):
    registry = MCPToolRegistry(config_path=tmp_path / "mcp.json")
    registry._event_loop = __import__("asyncio").get_running_loop()
    registry._servers = {
        "quant": SimpleNamespace(
            tools=[{
                "name": "health",
                "description": "Health check",
                "inputSchema": {"type": "object", "properties": {}},
            }]
        )
    }
    calls = []

    async def fake_call_tool(name, params):
        calls.append((name, params))
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(registry, "call_tool", fake_call_tool)
    tools = {}
    registry.register_into(tools, [])

    result = await ToolExecutor(tools).execute("mcp__quant__health", {})

    assert result == {"success": True, "status": "ok"}
    assert calls == [("quant/health", {})]


@pytest.mark.asyncio
async def test_mcp_server_never_sees_the_executor_private_arguments(tmp_path):
    # ToolExecutor adds _run_id and _session_id to every call. A strict MCP
    # server rejected the whole call over them: "unexpected keyword argument
    # '_run_id'".
    registry = MCPToolRegistry(config_path=tmp_path / "mcp.json")
    registry._event_loop = __import__("asyncio").get_running_loop()
    sent = []

    async def server_call_tool(name, arguments):
        sent.append((name, arguments))
        return {"success": True, "insights": []}

    registry._servers = {
        "quant": SimpleNamespace(
            tools=[{
                "name": "get_market_insights",
                "description": "Insights",
                "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}},
            }],
            is_alive=True,
            call_tool=server_call_tool,
        )
    }
    tools = {}
    registry.register_into(tools, [])
    executor = ToolExecutor(tools, execution_context=lambda: {"_run_id": "run-1", "_session_id": "s-1"})

    result = await executor.execute("mcp__quant__get_market_insights", {"symbol": "AAPL"})

    assert result["success"] is True
    assert sent == [("get_market_insights", {"symbol": "AAPL"})]
