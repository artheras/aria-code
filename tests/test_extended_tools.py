"""
tests/test_extended_tools.py — Unit tests for Slack, Feishu, TradingView, QuickBooks, Shopify, Snowflake tools
"""

import json
from unittest.mock import patch
from aria_code.tools.extended_tools import (
    tool_send_slack_notification,
    tool_push_feishu_card,
    tool_parse_tradingview_alert,
    tool_get_quickbooks_pnl,
    tool_get_shopify_store_analytics,
    tool_query_snowflake_data,
    register_extended_tools,
)


def test_send_slack_notification_requires_config(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    res = tool_send_slack_notification({"channel": "#trading-desk", "text": "Trade Alert: Buy AAPL"})
    assert res["success"] is False
    assert "SLACK_WEBHOOK_URL" in res["error"]


def test_send_slack_notification_via_webhook(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T00/B00/XXX")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with patch("aria_code.tools.extended_tools._post_json", return_value=(200, "ok")) as mock_post:
        res = tool_send_slack_notification({"channel": "#trading-desk", "text": "Trade Alert: Buy AAPL"})
    assert res["success"] is True
    assert res["mode"] == "webhook"
    assert mock_post.call_args[0][0] == "https://hooks.slack.com/services/T00/B00/XXX"


def test_send_slack_notification_via_bot_api(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    with patch(
        "aria_code.tools.extended_tools._post_json",
        return_value=(200, {"ok": True, "channel": "C123", "ts": "1700000.01"}),
    ) as mock_post:
        res = tool_send_slack_notification({"channel": "#trading-desk", "text": "Trade Alert"})
    assert res["success"] is True
    assert res["mode"] == "bot_api"
    assert res["ts"] == "1700000.01"
    assert mock_post.call_args[1]["headers"]["Authorization"] == "Bearer xoxb-test-token"


def test_push_feishu_card_requires_config(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    res = tool_push_feishu_card({"title": "量化调仓审批", "content": "请审批"})
    assert res["success"] is False
    assert "FEISHU_WEBHOOK_URL" in res["error"]


def test_push_feishu_card_via_webhook_with_secret(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.setenv("FEISHU_WEBHOOK_SECRET", "sec_123")
    with patch("aria_code.tools.extended_tools._post_json", return_value=(200, {"code": 0})) as mock_post:
        res = tool_push_feishu_card({"title": "量化调仓审批", "content": "NVDA +100"})
    assert res["success"] is True
    assert res["mode"] == "webhook"
    sent_payload = mock_post.call_args[0][1]
    assert sent_payload["msg_type"] == "interactive"
    assert "sign" in sent_payload and "timestamp" in sent_payload


def test_parse_tradingview_alert_valid_and_invalid(monkeypatch):
    monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)
    res = tool_parse_tradingview_alert({
        "raw_payload": json.dumps({
            "ticker": "NASDAQ:NVDA",
            "action": "BUY",
            "price": 130.5,
            "quantity": 25,
            "strategy": "Trend Breakout V1",
        })
    })
    assert res["success"] is True
    assert res["order_intent"]["exchange"] == "NASDAQ"
    assert res["order_intent"]["symbol"] == "NVDA"
    assert res["order_intent"]["limit_price"] == 130.5
    assert res["order_intent"]["quantity"] == 25.0

    bad = tool_parse_tradingview_alert({"ticker": "NVDA", "action": "BUY", "price": -5})
    assert bad["success"] is False

    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "my-secret")
    denied = tool_parse_tradingview_alert({"ticker": "NVDA", "action": "BUY", "price": 100, "passphrase": "wrong"})
    assert denied["success"] is False


def test_get_quickbooks_pnl_real_api_and_config(monkeypatch):
    monkeypatch.delenv("QUICKBOOKS_REALM_ID", raising=False)
    monkeypatch.delenv("QUICKBOOKS_ACCESS_TOKEN", raising=False)
    assert tool_get_quickbooks_pnl({})["success"] is False

    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "913035000")
    monkeypatch.setenv("QUICKBOOKS_ACCESS_TOKEN", "qbo-token")
    qbo_payload = {
        "Header": {"Currency": "USD", "StartPeriod": "2026-04-01", "EndPeriod": "2026-06-30"},
        "Rows": {
            "Row": [
                {"group": "Income", "Summary": {"ColData": [{"value": "Total Income"}, {"value": "100000.00"}]}},
                {"group": "COGS", "Summary": {"ColData": [{"value": "Total COGS"}, {"value": "25000.00"}]}},
                {"group": "Expenses", "Summary": {"ColData": [{"value": "Total Expenses"}, {"value": "35000.00"}]}},
            ]
        },
    }
    with patch("aria_code.tools.extended_tools._get_json", return_value=(200, qbo_payload)):
        res = tool_get_quickbooks_pnl({})
    assert res["success"] is True
    assert res["metrics"]["total_revenue"] == 100000.0
    assert res["metrics"]["gross_profit"] == 75000.0
    assert res["metrics"]["net_income"] == 40000.0


def test_get_shopify_store_analytics_real_api_and_config(monkeypatch):
    monkeypatch.delenv("SHOPIFY_STORE_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    assert tool_get_shopify_store_analytics({})["success"] is False

    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "demo.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_123")
    shopify_payload = {
        "orders": [
            {"total_price": "120.00", "currency": "USD", "financial_status": "paid"},
            {"total_price": "80.00", "currency": "USD", "financial_status": "refunded"},
        ]
    }
    with patch("aria_code.tools.extended_tools._get_json", return_value=(200, shopify_payload)):
        res = tool_get_shopify_store_analytics({"period_days": 14})
    assert res["success"] is True
    assert res["metrics"]["total_gmv"] == 200.0
    assert res["metrics"]["total_orders"] == 2
    assert res["metrics"]["average_order_value"] == 100.0
    assert res["metrics"]["refund_rate_pct"] == 50.0


def test_query_snowflake_data_real_api_and_readonly_guard(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "xy12345.us-east-1")
    monkeypatch.setenv("SNOWFLAKE_TOKEN", "jwt-token")

    blocked = tool_query_snowflake_data({"sql": "DELETE FROM analytics.trades"})
    assert blocked["success"] is False
    assert "read-only" in blocked["error"]

    sf_payload = {
        "statementHandle": "stmt-001",
        "resultSetMetaData": {
            "numRows": 1,
            "rowType": [{"name": "TICKER"}, {"name": "SCORE"}],
        },
        "data": [["NVDA", "2.84"]],
    }
    with patch("aria_code.tools.extended_tools._post_json", return_value=(200, sf_payload)):
        res = tool_query_snowflake_data({"sql": "SELECT ticker, score FROM factors LIMIT 1"})
    assert res["success"] is True
    assert res["columns"] == ["TICKER", "SCORE"]
    assert res["rows"] == [["NVDA", "2.84"]]


def test_register_extended_tools_and_schemas():
    dummy = {}
    schemas = []
    register_extended_tools(dummy, schemas)
    expected = {
        "send_slack_notification",
        "push_feishu_card",
        "parse_tradingview_alert",
        "get_quickbooks_pnl",
        "get_shopify_store_analytics",
        "query_snowflake_data",
    }
    assert expected.issubset(dummy.keys())
    assert {s["function"]["name"] for s in schemas} == expected
