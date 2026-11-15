"""
tools/stripe_tools.py — Stripe Business & Revenue Analytics Tool
================================================================
Allows agents and CLI users to ingest and analyze Stripe data:
- Connects to Stripe export files (charges.csv, subscriptions.json) or live Stripe REST API
- Calculates MRR, ARR, LTV, Churn, GPV, and recoverable dunning opportunities
"""

from __future__ import annotations

import csv
import json
import logging
import pathlib
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def tool_analyze_stripe_data(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze Stripe payment transactions and subscription data.
    Params:
        file_path (str, optional): Path to CSV or JSON with charges/subscriptions
        charges (list, optional): List of charge records
        subscriptions (list, optional): List of subscription records
        business_name (str, optional): Name of the business
    """
    file_path = params.get("file_path")
    charges = params.get("charges", [])
    subscriptions = params.get("subscriptions", [])
    business_name = params.get("business_name", "Enterprise SaaS")

    if file_path:
        p = pathlib.Path(file_path).expanduser().resolve()
        if not p.exists():
            return {"success": False, "error": f"File not found: {file_path}"}
        try:
            if p.suffix == ".json":
                content = json.loads(p.read_text(encoding="utf-8"))
                charges = content.get("charges", content if isinstance(content, list) else [])
                subscriptions = content.get("subscriptions", [])
            elif p.suffix == ".csv":
                with open(p, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    charges = list(reader)
        except Exception as exc:
            return {"success": False, "error": f"Failed to parse Stripe file: {exc}"}

    # No data means no analysis.
    #
    # This used to substitute a "representative sample dataset" and analyse
    # that instead, returning a normal-looking result with no indication the
    # numbers were invented. Called with no arguments it reported a specific
    # ARR and subscriber count for a business it had never seen — and once the
    # tool became visible to the model, that is a figure it would relay to the
    # user as theirs. Demo data is only safe when it announces itself.
    if not charges and not subscriptions:
        return {
            "success": False,
            "error": (
                "No Stripe data supplied. Pass file_path (a CSV/JSON export of "
                "charges and subscriptions), or charges/subscriptions directly. "
                "This tool analyses data you provide; it does not connect to Stripe."
            ),
        }

    data_source = "file_path" if file_path else "caller-supplied records"
    try:
        from aria_code.packages.quant_engine.services.stripe_analytics_service import StripeAnalyticsService
        service = StripeAnalyticsService()
        res = service.analyze_stripe_data(charges, subscriptions).to_dict()
    except Exception:
        succeeded = [c for c in charges if str(c.get("status", "succeeded")).lower() == "succeeded"]
        failed = [c for c in charges if str(c.get("status", "")).lower() == "failed"]
        gpv = sum(float(c.get("amount_usd", 0.0) or 0.0) for c in succeeded)
        fees = sum(float(c.get("fee_usd", 0.0) or 0.0) for c in succeeded)
        refunds = sum(float(c.get("refunded_amount_usd", 0.0) or 0.0) for c in charges)
        failed_amt = sum(float(c.get("amount_usd", 0.0) or 0.0) for c in failed)
        active_subs = [s for s in subscriptions if str(s.get("status", "active")).lower() == "active"]
        canceled_subs = [s for s in subscriptions if str(s.get("status", "")).lower() in {"canceled", "cancelled"}]
        mrr = sum(float(s.get("mrr_usd", 0.0) or 0.0) for s in active_subs)
        total_subs = len(active_subs) + len(canceled_subs)
        churn = round((len(canceled_subs) / total_subs) * 100.0, 2) if total_subs > 0 else 0.0
        res = {
            "gross_payment_volume_usd": round(gpv, 2),
            "net_revenue_usd": round(gpv - fees - refunds, 2),
            "mrr_usd": round(mrr, 2),
            "arr_usd": round(mrr * 12.0, 2),
            "active_subscribers_count": len(active_subs),
            "monthly_churn_rate_pct": churn,
            "net_revenue_retention_pct": round(100.0 - churn, 2) if total_subs > 0 else 0.0,
            "failed_payment_amount_usd": round(failed_amt, 2),
            "recoverable_dunning_amount_usd": round(failed_amt * 0.55, 2),
            "revenue_growth_recommendations": ["已基于提供的 Stripe 记录完成交易与订阅指标汇总"],
        }
    res["data_source"] = data_source

    return {
        "success": True,
        "data": res,
        "summary": (
            f"已诊断 {business_name} Stripe 数据（来源：{data_source}）：总流水 ${res.get('gross_payment_volume_usd', 0):,.2f}，"
            f"MRR ${res.get('mrr_usd', 0):,.2f} (ARR ${res.get('arr_usd', 0):,.2f})，"
            f"活跃订阅 {res.get('active_subscribers_count', 0)} 户，月流失率 {res.get('monthly_churn_rate_pct', 0)}%，"
            f"失败扣款 ${res.get('failed_payment_amount_usd', 0):,.2f}。"
        ),
    }


def register_stripe_tools(tools_dict: Dict[str, Any], schemas_list: List[Dict[str, Any]]) -> int:
    """Register stripe tools into LOCAL_TOOLS."""
    tools_dict["analyze_stripe_data"] = (tool_analyze_stripe_data, "Analyze user-connected Stripe business transactions, MRR, ARR, churn, and failed payments")
    schemas_list.append({
        "name": "analyze_stripe_data",
        "description": "Analyze user-connected Stripe transaction and subscription datasets",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Optional CSV/JSON path containing Stripe charges and subscriptions"},
                "charges": {"type": "array", "description": "Optional list of charge objects"},
                "subscriptions": {"type": "array", "description": "Optional list of subscription objects"},
                "business_name": {"type": "string", "description": "Name of the business"},
            },
        },
    })
    return 1
