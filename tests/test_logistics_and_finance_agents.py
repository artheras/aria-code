"""
tests/test_logistics_and_finance_agents.py — Tests for Logistics Tools
"""

from aria_code.tools.logistics_tools import tool_analyze_logistics_data


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


def test_logistics_csv_import(tmp_path):
    path = tmp_path / "waybills.csv"
    path.write_text("waybill_no,carrier,total_cost,is_on_time\nA,One,12.5,false\n", encoding="utf-8")
    res = tool_analyze_logistics_data({"file_path": str(path)})
    assert res["success"] is True
    assert res["source"] == str(path)
    assert res["data"]["overall_on_time_rate"] == 0
