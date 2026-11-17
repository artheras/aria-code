"""
tests/test_logistics_and_finance_agents.py — Tests for Logistics & Corporate Finance Agents and Tools
"""

import asyncio
from aria_code.agents.registry import get_registry
from aria_code.agents.warehouse.logistics_cost import LogisticsCostOptimizerAgent
from aria_code.agents.warehouse.fulfillment_leadtime import FulfillmentLeadTimeAgent
from aria_code.tools.logistics_tools import tool_analyze_logistics_data
from aria_code.tools.enterprise_finance_tools import tool_analyze_financial_statements


def test_enterprise_agents_registry_discovery():
    registry = get_registry()
    logistics_cost_cls = registry.get("warehouse_logistics_cost")
    fulfillment_cls = registry.get("warehouse_fulfillment_leadtime")

    assert logistics_cost_cls is not None
    assert fulfillment_cls is not None


def test_logistics_cost_optimizer_agent():
    agent = LogisticsCostOptimizerAgent()
    sample_waybills = [
        {"waybill_no": "WB001", "carrier": "FedEx", "actual_weight_kg": 10.0, "billed_weight_kg": 10.0, "base_freight": 100.0, "fuel_surcharge": 12.0, "total_cost": 112.0, "transit_days": 2.0, "is_on_time": True},
        {"waybill_no": "WB002", "carrier": "FedEx", "actual_weight_kg": 10.0, "billed_weight_kg": 16.0, "base_freight": 100.0, "fuel_surcharge": 25.0, "total_cost": 185.0, "transit_days": 4.0, "is_on_time": False, "status": "DELAYED"},
        {"waybill_no": "WB003", "carrier": "SF Express", "actual_weight_kg": 20.0, "billed_weight_kg": 20.0, "base_freight": 140.0, "fuel_surcharge": 10.0, "total_cost": 150.0, "transit_days": 1.5, "is_on_time": True},
    ]

    async def _run():
        res = await agent.analyze("GLOBAL_SHIPPING", {"waybills": sample_waybills})
        assert res.success is True
        assert "运费总支出" in res.analysis
        assert "异常计费发现" in res.analysis
        assert res.data_used["total_waybills"] == 3
        assert len(res.data_used["billing_anomalies"]) >= 1

    asyncio.run(_run())


def test_fulfillment_leadtime_agent():
    agent = FulfillmentLeadTimeAgent()
    sample_stages = [
        {"stage_name": "Order-to-Pack", "avg_hours": 3.5, "sla_target_hours": 4.0, "compliance_rate": 95.0, "bottleneck": False},
        {"stage_name": "Dock-to-Stock", "avg_hours": 7.5, "sla_target_hours": 6.0, "compliance_rate": 80.0, "bottleneck": True, "details": "入库质检排队延误"},
    ]

    async def _run():
        res = await agent.analyze("WH_EAST_01", {"fulfillment_stages": sample_stages})
        assert res.success is True
        assert "SLA 达标率" in res.analysis
        assert "瓶颈" in res.analysis
        assert len(res.data_used["bottlenecks"]) == 1

    asyncio.run(_run())


def test_logistics_tool_requires_actual_records(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    tool_res = tool_analyze_logistics_data({})
    assert tool_res["success"] is False
    assert "No waybills" in tool_res["error"]


def test_logistics_tool_calculates_only_known_delivery_status():
    tool_res = tool_analyze_logistics_data({"waybills": [
        {"waybill_no": "A", "carrier": "One", "total_cost": 100, "actual_weight_kg": 10, "billed_weight_kg": 15, "is_on_time": True},
        {"waybill_no": "B", "carrier": "One", "total_cost": 50},
    ]})
    assert tool_res["success"] is True
    assert tool_res["data"]["total_freight_spend"] == 150
    assert tool_res["data"]["overall_on_time_rate"] == 100
    assert tool_res["data"]["known_delivery_count"] == 1
    assert len(tool_res["data"]["billing_anomalies"]) == 1


def test_logistics_agent_reports_missing_data(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    res = asyncio.run(LogisticsCostOptimizerAgent().analyze("SHIPPING", {}))
    assert res.success is False
    assert res.data_used == {}


def test_logistics_csv_import(tmp_path):
    path = tmp_path / "waybills.csv"
    path.write_text("waybill_no,carrier,total_cost,is_on_time\nA,One,12.5,false\n", encoding="utf-8")
    res = tool_analyze_logistics_data({"file_path": str(path)})
    assert res["success"] is True
    assert res["source"] == str(path)
    assert res["data"]["overall_on_time_rate"] == 0


def test_enterprise_finance_tool_refuses_a_name_without_statements():
    """A company name is not financial data.

    This test used to assert the opposite — that passing only company_name
    returned a successful DuPont and working-capital analysis. It did, by
    inventing the statements. Refusing is the same correction this file already
    makes for the logistics tool one test above: a tool that cannot know
    something must say so rather than produce a confident number.
    """
    tool_res = tool_analyze_financial_statements({"company_name": "Enterprise Test"})
    assert tool_res["success"] is False
    assert "does not estimate" in tool_res["error"]


def test_enterprise_finance_tool_computes_from_supplied_statements():
    """With real statements it still does the arithmetic it always did."""
    tool_res = tool_analyze_financial_statements({
        "company_name": "Enterprise Test",
        "financials": {
            "income_statement": {"revenue": 1000.0, "net_income": 100.0},
            "balance_sheet": {
                "total_assets": 2000.0, "total_equity": 800.0,
                "current_assets": 600.0, "current_liabilities": 300.0,
            },
            "cashflow": {},
        },
    })
    assert tool_res["success"] is True, tool_res.get("error")
    assert "dupont" in tool_res["data"]
    assert "working_capital" in tool_res["data"]
    # Provenance travels with the numbers: a caller cannot judge the analysis
    # without knowing whether the statements were theirs or fetched.
    assert tool_res["data"]["data_source"] == "caller-supplied statements"
