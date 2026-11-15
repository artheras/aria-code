"""Enterprise connector tools — real HTTP/REST implementations.

All six connectors call their real upstream APIs (or perform deterministic validation)
when configured, and fail honestly with a configuration/validation error otherwise:
- ``send_slack_notification``: ``SLACK_WEBHOOK_URL`` or ``SLACK_BOT_TOKEN``
- ``push_feishu_card``: ``FEISHU_WEBHOOK_URL`` (+ optional ``FEISHU_WEBHOOK_SECRET``)
  or ``FEISHU_APP_ID`` + ``FEISHU_APP_SECRET``
- ``parse_tradingview_alert``: Deterministic payload parser + optional ``TRADINGVIEW_WEBHOOK_SECRET``
- ``get_quickbooks_pnl``: QuickBooks Online Reports API (``QUICKBOOKS_REALM_ID`` + ``QUICKBOOKS_ACCESS_TOKEN``)
- ``get_shopify_store_analytics``: Shopify Admin REST API (``SHOPIFY_STORE_DOMAIN`` + ``SHOPIFY_ACCESS_TOKEN``)
- ``query_snowflake_data``: Snowflake SQL REST API v2 (``SNOWFLAKE_ACCOUNT`` + ``SNOWFLAKE_TOKEN``), read-only SQL enforced
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_READONLY_SQL_RE = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.IGNORECASE)
_MUTATING_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|PUT|REMOVE)\b",
    re.IGNORECASE,
)


def _request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> Tuple[int, Any]:
    """Execute an HTTP JSON request and return ``(status_code, parsed_json_or_text)``."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json"}
    if body is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        raise ConnectionError(f"Network error contacting {url}: {exc.reason}") from exc


def _post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Tuple[int, Any]:
    return _request_json("POST", url, payload=payload, headers=headers, timeout=timeout)


def _get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> Tuple[int, Any]:
    return _request_json("GET", url, payload=None, headers=headers, timeout=timeout)


def tool_send_slack_notification(params: Dict[str, Any]) -> Dict[str, Any]:
    """Send a notification or Block Kit message to Slack via Webhook or Bot API."""
    text = str(params.get("text") or "").strip()
    blocks = params.get("blocks")
    channel = str(params.get("channel") or "").strip()

    if not text and not blocks:
        return {"success": False, "error": "send_slack_notification requires 'text' or 'blocks'."}

    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()

    if webhook_url:
        payload: Dict[str, Any] = {}
        if text:
            payload["text"] = text
        if blocks:
            payload["blocks"] = blocks
        if channel:
            payload["channel"] = channel
        try:
            status, data = _post_json(webhook_url, payload)
        except ConnectionError as exc:
            return {"success": False, "error": str(exc)}
        if 200 <= status < 300:
            return {
                "success": True,
                "mode": "webhook",
                "channel": channel or "(webhook default)",
                "status_code": status,
                "response": data,
            }
        return {"success": False, "status_code": status, "error": f"Slack webhook failed: {data}"}

    if bot_token:
        if not channel:
            channel = os.getenv("SLACK_DEFAULT_CHANNEL", "").strip()
        if not channel:
            return {
                "success": False,
                "error": "send_slack_notification with SLACK_BOT_TOKEN requires 'channel' (or SLACK_DEFAULT_CHANNEL).",
            }
        payload = {"channel": channel, "text": text or "Aria Notification"}
        if blocks:
            payload["blocks"] = blocks
        try:
            status, data = _post_json(
                "https://slack.com/api/chat.postMessage",
                payload,
                headers={"Authorization": f"Bearer {bot_token}"},
            )
        except ConnectionError as exc:
            return {"success": False, "error": str(exc)}
        if isinstance(data, dict) and data.get("ok"):
            return {
                "success": True,
                "mode": "bot_api",
                "channel": data.get("channel", channel),
                "ts": data.get("ts", ""),
                "message": f"Posted to Slack channel {data.get('channel', channel)}",
            }
        err = data.get("error") if isinstance(data, dict) else data
        return {"success": False, "status_code": status, "error": f"Slack API error: {err}"}

    return {
        "success": False,
        "error": "send_slack_notification is not configured: set SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN.",
    }


def _build_feishu_card(params: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(params.get("card"), dict):
        return params["card"]
    title = str(params.get("title") or "Aria 通知").strip()
    body = str(params.get("content") or params.get("text") or "").strip() or title
    template = str(params.get("template") or "blue").strip()
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
        ],
    }


def _feishu_webhook_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def tool_push_feishu_card(params: Dict[str, Any]) -> Dict[str, Any]:
    """Send an interactive card to Feishu / Lark via Custom Bot Webhook or OpenAPI."""
    card = _build_feishu_card(params)
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()

    if webhook_url:
        payload: Dict[str, Any] = {"msg_type": "interactive", "card": card}
        secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
        if secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = _feishu_webhook_sign(secret, ts)
        try:
            status, data = _post_json(webhook_url, payload)
        except ConnectionError as exc:
            return {"success": False, "error": str(exc)}
        ok = (
            200 <= status < 300
            and isinstance(data, dict)
            and data.get("code", data.get("StatusCode", -1)) == 0
        )
        if ok:
            return {"success": True, "mode": "webhook", "response": data}
        return {"success": False, "status_code": status, "error": f"Feishu webhook failed: {data}"}

    if app_id and app_secret:
        receive_id = str(params.get("receive_id") or "").strip()
        receive_id_type = str(params.get("receive_id_type") or "open_id").strip()
        if not receive_id:
            return {
                "success": False,
                "error": "push_feishu_card with FEISHU_APP_ID/FEISHU_APP_SECRET requires 'receive_id'.",
            }
        try:
            _, token_resp = _post_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": app_id, "app_secret": app_secret},
            )
        except ConnectionError as exc:
            return {"success": False, "error": str(exc)}
        if not isinstance(token_resp, dict) or token_resp.get("code") != 0 or not token_resp.get("tenant_access_token"):
            return {"success": False, "error": f"Feishu auth failed: {token_resp}"}

        token = token_resp["tenant_access_token"]
        msg_url = (
            "https://open.feishu.cn/open-apis/im/v1/messages?"
            + urllib.parse.urlencode({"receive_id_type": receive_id_type})
        )
        try:
            status, msg_resp = _post_json(
                msg_url,
                {
                    "receive_id": receive_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except ConnectionError as exc:
            return {"success": False, "error": str(exc)}
        if 200 <= status < 300 and isinstance(msg_resp, dict) and msg_resp.get("code") == 0:
            msg_data = msg_resp.get("data") or {}
            return {
                "success": True,
                "mode": "openapi",
                "message_id": msg_data.get("message_id", ""),
                "target": receive_id,
            }
        return {"success": False, "status_code": status, "error": f"Feishu message failed: {msg_resp}"}

    return {
        "success": False,
        "error": "push_feishu_card is not configured: set FEISHU_WEBHOOK_URL or FEISHU_APP_ID + FEISHU_APP_SECRET.",
    }


def tool_parse_tradingview_alert(params: Dict[str, Any]) -> Dict[str, Any]:
    """Parse and validate a TradingView webhook alert payload into a structured order intent."""
    payload: Dict[str, Any] = dict(params or {})
    raw = payload.get("raw_payload")
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            return {"success": False, "error": f"Invalid TradingView JSON payload: {exc}"}
        if not isinstance(decoded, dict):
            return {"success": False, "error": "TradingView payload must be a JSON object."}
        payload = {**decoded, **{k: v for k, v in payload.items() if k != "raw_payload"}}
    elif isinstance(raw, dict):
        payload = {**raw, **{k: v for k, v in payload.items() if k != "raw_payload"}}

    expected_secret = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
    if expected_secret:
        provided = str(payload.get("passphrase") or payload.get("secret") or "")
        if not hmac.compare_digest(provided, expected_secret):
            return {"success": False, "error": "TradingView alert passphrase verification failed."}

    ticker = str(payload.get("ticker") or payload.get("symbol") or "").strip().upper()
    action = str(payload.get("action") or payload.get("side") or "").strip().upper()
    if not ticker:
        return {"success": False, "error": "TradingView alert missing required 'ticker' or 'symbol'."}
    if action not in {"BUY", "SELL", "LONG", "SHORT", "CLOSE"}:
        return {"success": False, "error": f"Unsupported TradingView action: {action!r}"}

    try:
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        return {"success": False, "error": "TradingView alert requires numeric 'price'."}
    if price <= 0:
        return {"success": False, "error": "TradingView alert 'price' must be > 0."}

    try:
        quantity = float(payload.get("quantity", payload.get("contracts", 1)))
    except (TypeError, ValueError):
        return {"success": False, "error": "TradingView alert 'quantity' must be numeric."}
    if quantity <= 0:
        return {"success": False, "error": "TradingView alert 'quantity' must be > 0."}

    exchange, symbol = ticker.split(":", 1) if ":" in ticker else ("", ticker)
    timeframe = str(payload.get("timeframe") or payload.get("interval") or "").strip()
    strategy = str(payload.get("strategy") or "").strip()
    normalized_side = "BUY" if action in {"BUY", "LONG"} else ("SELL" if action in {"SELL", "SHORT"} else "CLOSE")

    return {
        "success": True,
        "ticker": ticker,
        "action": action,
        "trigger_price": price,
        "timeframe": timeframe,
        "strategy": strategy,
        "order_intent": {
            "exchange": exchange,
            "symbol": symbol,
            "side": normalized_side,
            "limit_price": price,
            "quantity": quantity,
        },
    }


def _extract_qbo_summary_totals(rows: List[Dict[str, Any]], out: Dict[str, float]) -> None:
    """Recursively collect QuickBooks P&L section summary totals by group name."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        group = str(row.get("group") or "").strip()
        summary = row.get("Summary")
        if group and isinstance(summary, dict):
            col_data = summary.get("ColData") or []
            if len(col_data) >= 2:
                try:
                    out[group] = float(col_data[1].get("value") or 0.0)
                except (TypeError, ValueError):
                    pass
        nested = (row.get("Rows") or {}).get("Row")
        if isinstance(nested, list):
            _extract_qbo_summary_totals(nested, out)


def tool_get_quickbooks_pnl(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch corporate P&L report from QuickBooks Online Accounting API."""
    realm_id = str(params.get("realm_id") or os.getenv("QUICKBOOKS_REALM_ID", "")).strip()
    access_token = os.getenv("QUICKBOOKS_ACCESS_TOKEN", "").strip()
    if not realm_id or not access_token:
        return {
            "success": False,
            "error": "get_quickbooks_pnl is not configured: set QUICKBOOKS_REALM_ID and QUICKBOOKS_ACCESS_TOKEN.",
        }

    sandbox = os.getenv("QUICKBOOKS_SANDBOX", "").strip().lower() in {"1", "true", "yes"}
    host = "sandbox-quickbooks.api.intuit.com" if sandbox else "quickbooks.api.intuit.com"
    query_params: Dict[str, str] = {"minorversion": "70"}
    if params.get("start_date") and params.get("end_date"):
        query_params["start_date"] = str(params["start_date"])
        query_params["end_date"] = str(params["end_date"])
    elif params.get("date_macro"):
        query_params["date_macro"] = str(params["date_macro"])
    else:
        query_params["date_macro"] = "This Fiscal Quarter-to-date"

    url = f"https://{host}/v3/company/{urllib.parse.quote(realm_id)}/reports/ProfitAndLoss?{urllib.parse.urlencode(query_params)}"
    try:
        status, data = _get_json(url, headers={"Authorization": f"Bearer {access_token}"})
    except ConnectionError as exc:
        return {"success": False, "error": str(exc)}

    if not (200 <= status < 300) or not isinstance(data, dict):
        return {"success": False, "status_code": status, "error": f"QuickBooks API error: {data}"}

    header = data.get("Header") or {}
    totals: Dict[str, float] = {}
    _extract_qbo_summary_totals((data.get("Rows") or {}).get("Row") or [], totals)
    income = totals.get("Income", 0.0)
    cogs = totals.get("COGS", 0.0)
    gross_profit = totals.get("GrossProfit", income - cogs)
    expenses = totals.get("Expenses", 0.0)
    net_income = totals.get("NetIncome", gross_profit - expenses)

    return {
        "success": True,
        "realm_id": realm_id,
        "currency": header.get("Currency", "USD"),
        "start_period": header.get("StartPeriod", ""),
        "end_period": header.get("EndPeriod", ""),
        "metrics": {
            "total_revenue": round(income, 2),
            "cogs": round(cogs, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_margin_pct": round((gross_profit / income) * 100, 2) if income else 0.0,
            "operating_expenses": round(expenses, 2),
            "net_income": round(net_income, 2),
            "net_margin_pct": round((net_income / income) * 100, 2) if income else 0.0,
        },
        "section_totals": totals,
    }


def tool_get_shopify_store_analytics(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch Shopify store order volume, GMV, AOV, and refund metrics via Admin REST API."""
    store = str(params.get("store_domain") or os.getenv("SHOPIFY_STORE_DOMAIN", "")).strip()
    token = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
    if not store or not token:
        return {
            "success": False,
            "error": "get_shopify_store_analytics is not configured: set SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN.",
        }

    period_days = max(1, min(365, int(params.get("period_days", 30))))
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=period_days)).isoformat()
    api_version = os.getenv("SHOPIFY_API_VERSION", "2024-10").strip()
    query = urllib.parse.urlencode({"status": "any", "limit": 250, "created_at_min": since})
    url = f"https://{store}/admin/api/{api_version}/orders.json?{query}"

    try:
        status, data = _get_json(url, headers={"X-Shopify-Access-Token": token})
    except ConnectionError as exc:
        return {"success": False, "error": str(exc)}

    if not (200 <= status < 300) or not isinstance(data, dict) or not isinstance(data.get("orders"), list):
        return {"success": False, "status_code": status, "error": f"Shopify API error: {data}"}

    orders = data["orders"]
    total_orders = len(orders)
    total_gmv = 0.0
    refunded_orders = 0
    currency = "USD"
    for order in orders:
        if not isinstance(order, dict):
            continue
        currency = str(order.get("currency") or currency)
        try:
            total_gmv += float(order.get("total_price") or 0.0)
        except (TypeError, ValueError):
            pass
        if str(order.get("financial_status") or "").lower() in {"refunded", "partially_refunded"}:
            refunded_orders += 1

    return {
        "success": True,
        "store_domain": store,
        "period_days": period_days,
        "currency": currency,
        "metrics": {
            "total_gmv": round(total_gmv, 2),
            "total_orders": total_orders,
            "average_order_value": round(total_gmv / total_orders, 2) if total_orders else 0.0,
            "refund_rate_pct": round((refunded_orders / total_orders) * 100, 2) if total_orders else 0.0,
        },
    }


def tool_query_snowflake_data(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a read-only SQL query against the Snowflake SQL REST API v2."""
    sql = str(params.get("sql") or "").strip()
    if not sql:
        return {"success": False, "error": "query_snowflake_data requires a non-empty 'sql' query."}
    if not _READONLY_SQL_RE.match(sql) or _MUTATING_SQL_RE.search(sql):
        return {
            "success": False,
            "error": "query_snowflake_data only allows read-only queries (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN).",
        }

    account = str(params.get("account") or os.getenv("SNOWFLAKE_ACCOUNT", "")).strip()
    token = os.getenv("SNOWFLAKE_TOKEN", "").strip()
    if not account or not token:
        return {
            "success": False,
            "error": "query_snowflake_data is not configured: set SNOWFLAKE_ACCOUNT and SNOWFLAKE_TOKEN.",
        }

    body: Dict[str, Any] = {
        "statement": sql,
        "timeout": int(params.get("timeout", 30)),
    }
    for env_key, param_key, body_key in (
        ("SNOWFLAKE_WAREHOUSE", "warehouse", "warehouse"),
        ("SNOWFLAKE_DATABASE", "database", "database"),
        ("SNOWFLAKE_SCHEMA", "schema", "schema"),
        ("SNOWFLAKE_ROLE", "role", "role"),
    ):
        val = str(params.get(param_key) or os.getenv(env_key, "")).strip()
        if val:
            body[body_key] = val

    url = f"https://{account}.snowflakecomputing.com/api/v2/statements"
    try:
        status, data = _post_json(
            url,
            body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Snowflake-Authorization-Token-Type": os.getenv("SNOWFLAKE_TOKEN_TYPE", "KEYPAIR_JWT"),
            },
        )
    except ConnectionError as exc:
        return {"success": False, "error": str(exc)}

    if not (200 <= status < 300) or not isinstance(data, dict):
        return {"success": False, "status_code": status, "error": f"Snowflake API error: {data}"}

    meta = data.get("resultSetMetaData") or {}
    row_type = meta.get("rowType") or []
    columns = [str(col.get("name") or f"col_{i}") for i, col in enumerate(row_type) if isinstance(col, dict)]
    rows = data.get("data") or []
    return {
        "success": True,
        "statement_handle": data.get("statementHandle", ""),
        "row_count": meta.get("numRows", len(rows)),
        "columns": columns,
        "rows": rows,
    }


_STUB_TOOLS: Dict[str, str] = {}

EXTENDED_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "send_slack_notification",
            "description": "Send a notification or Block Kit message to Slack (requires SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Target Slack channel (e.g. #trading-desk or channel ID)"},
                    "text": {"type": "string", "description": "Message text"},
                    "blocks": {"type": "array", "description": "Optional Slack Block Kit blocks", "items": {"type": "object"}},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_feishu_card",
            "description": "Send an interactive notification card to Feishu/Lark (requires FEISHU_WEBHOOK_URL or FEISHU_APP_ID + FEISHU_APP_SECRET).",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Card header title"},
                    "content": {"type": "string", "description": "Card markdown content"},
                    "receive_id": {"type": "string", "description": "Target open_id/chat_id when using OpenAPI credentials"},
                    "receive_id_type": {"type": "string", "description": "ID type (open_id, chat_id, user_id)"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_tradingview_alert",
            "description": "Parse and validate a TradingView webhook alert payload into a structured order intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_payload": {"type": "string", "description": "Raw JSON webhook payload string"},
                    "ticker": {"type": "string", "description": "Ticker symbol (e.g. NASDAQ:NVDA)"},
                    "action": {"type": "string", "description": "BUY, SELL, LONG, SHORT, or CLOSE"},
                    "price": {"type": "number", "description": "Trigger price"},
                    "quantity": {"type": "number", "description": "Order quantity"},
                    "timeframe": {"type": "string", "description": "Chart timeframe"},
                    "strategy": {"type": "string", "description": "Strategy name"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_quickbooks_pnl",
            "description": "Fetch Profit & Loss report from QuickBooks Online (requires QUICKBOOKS_REALM_ID and QUICKBOOKS_ACCESS_TOKEN).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                    "end_date": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                    "date_macro": {"type": "string", "description": "QuickBooks date macro (e.g. This Fiscal Quarter-to-date)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shopify_store_analytics",
            "description": "Fetch store GMV, order count, AOV, and refund rate from Shopify Admin API (requires SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN).",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_domain": {"type": "string", "description": "Shopify store domain (e.g. myshop.myshopify.com)"},
                    "period_days": {"type": "integer", "description": "Lookback window in days (1-365, default 30)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_snowflake_data",
            "description": "Execute a read-only SQL query against Snowflake SQL REST API v2 (requires SNOWFLAKE_ACCOUNT and SNOWFLAKE_TOKEN).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "Read-only SQL query (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN)"},
                    "warehouse": {"type": "string", "description": "Optional Snowflake warehouse override"},
                    "database": {"type": "string", "description": "Optional Snowflake database override"},
                    "schema": {"type": "string", "description": "Optional Snowflake schema override"},
                },
                "required": ["sql"],
            },
        },
    },
]


def stub_guard(name: str, service: str, handler):
    """Wrap an unimplemented tool so it reports its status instead of inventing one."""
    def _guarded(params: Dict[str, Any]) -> Dict[str, Any]:
        if os.getenv("ARIA_ALLOW_STUB_TOOLS", "").strip() in ("1", "true", "yes"):
            result = dict(handler(params) or {})
            result["stub"] = True
            result["warning"] = f"{name} returned canned data; {service} is not connected."
            return result
        return {
            "success": False,
            "error": (
                f"{name} is not implemented — it is a shape-only stub. "
                f"Connecting it requires {service}. "
                f"Do not report its result as if the action happened."
            ),
            "stub": True,
        }

    _guarded.__name__ = getattr(handler, "__name__", name)
    _guarded.__doc__ = getattr(handler, "__doc__", "")
    return _guarded


def register_extended_tools(registry_or_dict: Any, schemas: Optional[List[Dict[str, Any]]] = None) -> None:
    """Register all six real enterprise connector tools and their JSON schemas."""
    tools: Dict[str, Tuple[Any, str]] = {
        "send_slack_notification": (
            tool_send_slack_notification,
            "Send a notification to Slack via Webhook or Bot API",
        ),
        "push_feishu_card": (
            tool_push_feishu_card,
            "Send an interactive card to Feishu / Lark",
        ),
        "parse_tradingview_alert": (
            tool_parse_tradingview_alert,
            "Parse and validate a TradingView webhook alert payload",
        ),
        "get_quickbooks_pnl": (
            tool_get_quickbooks_pnl,
            "Fetch Profit & Loss report from QuickBooks Online API",
        ),
        "get_shopify_store_analytics": (
            tool_get_shopify_store_analytics,
            "Fetch store GMV, order count, and refund rate from Shopify Admin API",
        ),
        "query_snowflake_data": (
            tool_query_snowflake_data,
            "Execute read-only SQL query against Snowflake SQL REST API v2",
        ),
    }

    if hasattr(registry_or_dict, "register"):
        for name, entry in tools.items():
            registry_or_dict.register(name, entry[0])
    elif isinstance(registry_or_dict, dict):
        registry_or_dict.update(tools)

    if schemas is not None:
        existing = {s.get("function", {}).get("name") for s in schemas}
        for schema in EXTENDED_TOOL_SCHEMAS:
            if schema["function"]["name"] not in existing:
                schemas.append(schema)
