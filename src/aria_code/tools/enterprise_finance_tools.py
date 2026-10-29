"""Corporate financial metrics from actual, period-aligned statements."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List


def _number(data: dict, key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a finite number")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return value


def _ratio(numerator, denominator, scale=1):
    if numerator is None or denominator is None or denominator == 0:
        return None
    value = numerator / denominator * scale
    return round(value, 2) if math.isfinite(value) else None


def compute_financial_metrics(income: dict, balance: dict, cashflow: dict) -> dict:
    """Missing inputs stay unknown; zero denominators never become one."""
    revenue = _number(income, "revenue")
    net_income = _number(income, "net_income")
    assets = _number(balance, "total_assets")
    equity = _number(balance, "total_equity")
    current_assets = _number(balance, "current_assets")
    current_liabilities = _number(balance, "current_liabilities")
    inventory = _number(balance, "inventory")
    quick_assets = None if current_assets is None or inventory is None else current_assets - inventory
    return {
        "gross_margin_pct": _ratio(_number(income, "gross_profit"), revenue, 100),
        "net_margin_pct": _ratio(net_income, revenue, 100),
        "dupont": {
            "roe": _ratio(net_income, equity, 100),
            "net_profit_margin": _ratio(net_income, revenue, 100),
            "asset_turnover": _ratio(revenue, assets),
            "equity_multiplier": _ratio(assets, equity),
        },
        "working_capital": {
            "current_ratio": _ratio(current_assets, current_liabilities),
            "quick_ratio": _ratio(quick_assets, current_liabilities),
        },
        # The former three-term approximation is not a full Altman model.
        "altman_z_score": None,
        "solvency_risk": "UNKNOWN",
        "limitations": ["Unavailable metrics are null. No complete Altman model was evaluated."],
    }


def _fetch_yahoo_statements(symbol: str):
    import yfinance as yf
    from curl_cffi.requests import Session

    deadline = time.monotonic() + 20

    class BoundedSession(Session):
        def request(self, method, url, **kwargs):
            # yfinance passes timeout=30 explicitly, overriding a session default.
            requested = kwargs.get("timeout", 8)
            if isinstance(requested, (tuple, list)):
                requested = max(requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Financial statement source deadline exceeded")
            kwargs["timeout"] = min(float(requested or 8), 8, remaining)
            return super().request(method, url, **kwargs)

    with BoundedSession(impersonate="chrome") as session:
        ticker = yf.Ticker(symbol, session=session)
        inc_df = ticker.income_stmt
        bal_df = ticker.balance_sheet
        if time.monotonic() >= deadline:
            raise TimeoutError("Financial statement source deadline exceeded")
        if inc_df.empty or bal_df.empty:
            raise ValueError("empty_statements")
        periods = inc_df.columns.intersection(bal_df.columns)
        if periods.empty:
            raise ValueError("no_common_period")
        period = max(periods)
        inc, bal = inc_df[period], bal_df[period]
        def fields(series, mapping):
            values = {}
            for source, target in mapping.items():
                value = series.get(source)
                # pandas marks absent financial cells with NaN.
                if value is not None and math.isfinite(float(value)):
                    values[target] = float(value)
            return values
        income = fields(inc, {
            "Total Revenue": "revenue", "Cost Of Revenue": "cost_of_goods_sold",
            "Gross Profit": "gross_profit", "Operating Income": "operating_income", "Net Income": "net_income",
        })
        balance = fields(bal, {
            "Total Assets": "total_assets", "Stockholders Equity": "total_equity",
            "Current Assets": "current_assets", "Current Liabilities": "current_liabilities",
            "Inventory": "inventory", "Retained Earnings": "retained_earnings",
        })
        if not income:
            raise ValueError("empty_statements")
        return income, balance, str(period.date())


def tool_analyze_financial_statements(params: Dict[str, Any]) -> Dict[str, Any]:
    company_name = str(params.get("company_name") or "").strip().upper()
    data = params.get("financials") or {}
    if not isinstance(data, dict):
        return {"success": False, "error_code": "invalid_financials", "error": "financials must be an object"}
    if params.get("file_path"):
        return {"success": False, "error_code": "use_file_tool", "error": "Read the JSON file with read_file, then pass its contents as financials. This tool does not read arbitrary paths."}
    income, balance, cashflow = (data.get(k, {}) for k in ("income_statement", "balance_sheet", "cashflow"))
    if not all(isinstance(item, dict) for item in (income, balance, cashflow)):
        return {"success": False, "error_code": "invalid_financials", "error": "Statement sections must be objects"}
    source, period = "caller-supplied statements", None
    diagnosis = "missing_statements"
    if company_name and len(company_name) <= 10 and not income:
        if not params.get("network_enabled", True):
            diagnosis = "network_disabled"
        else:
            try:
                income, balance, period = _fetch_yahoo_statements(company_name)
                source = f"yfinance:{company_name}"
            except ImportError:
                diagnosis = "dependency_missing"
            except TimeoutError:
                diagnosis = "source_timeout"
            except ValueError as exc:
                diagnosis = str(exc) if str(exc) in {"empty_statements", "no_common_period"} else "source_error"
            except Exception:
                diagnosis = "source_unavailable"
    if not income:
        return {
            "success": False, "error_code": diagnosis, "source": f"yfinance:{company_name}",
            "error": f"No financial statements available for {company_name or 'this company'} ({diagnosis}). This tool does not estimate missing figures.",
            "next_actions": ["Continue other analysis using available market data; disclose missing financial statements.",
                             "Supply income_statement, balance_sheet and cashflow through financials.",
                             "Check Yahoo connectivity and yfinance dependencies; Google login does not authorize Yahoo data."],
        }
    try:
        metrics = compute_financial_metrics(income, balance, cashflow)
    except ValueError as exc:
        return {"success": False, "error_code": "invalid_financials", "error": str(exc)}
    metrics.update(company_name=company_name, data_source=source, period=period)
    def display(value):
        return "unavailable" if value is None else str(value)
    return {
        "success": True, "data": metrics,
        "summary": f"{company_name} financial metrics ({source}, period {period or 'caller supplied'}): gross margin {display(metrics['gross_margin_pct'])}%; net margin {display(metrics['net_margin_pct'])}%; ROE {display(metrics['dupont']['roe'])}%. Missing metrics are unavailable; solvency was not classified.",
    }


def register_enterprise_finance_tools(tools_dict: Dict[str, Any], schemas_list: List[Dict[str, Any]]) -> int:
    tools_dict["analyze_financial_statements"] = (tool_analyze_financial_statements, "Analyze company financial statements (P&L, Balance Sheet, Cash Flow) and Dupont decomposition")
    schemas_list.append({
        "name": "analyze_financial_statements",
        "description": "Analyze corporate financial statements, working capital cycle, and solvency. If company_name is a ticker (e.g. AAPL), it fetches real Yahoo Finance data.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Use read_file for JSON and pass financials; direct paths are not read"},
                "financials": {"type": "object", "description": "Optional dict containing income_statement, balance_sheet, cashflow"},
                "company_name": {"type": "string", "description": "Company name or ticker symbol (e.g. AAPL)"},
            },
        },
    })
    return 1

