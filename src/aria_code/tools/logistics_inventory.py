"""Inventory policy for a 3PL: when to reorder, how much, and what is stuck.

Everything here is a standard, published formula, applied to the records the
caller supplies, with the formula and every threshold written into the result
so a client can recompute any number by hand:

    z            = Φ⁻¹(service level)
    safety stock = z · √(L·σ_d² + d̄²·σ_L²)        demand and lead-time variability
    reorder point= d̄·L + safety stock
    order-up-to  = reorder point + d̄·R              R = review period
    order qty    = order-up-to − inventory position, when position ≤ reorder point
    position     = on hand + on order − backordered

ABC ranks a shipper's SKUs by annual consumption value (Pareto 80/95), XYZ by
the coefficient of variation of daily demand (0.5 / 1.0). Movement status
comes from days since last movement (30 slow, 90 dead).

What it will not do is guess. A SKU with less demand history than
`min_history_days` gets no safety stock and no reorder point — it is reported
as insufficient history, because a policy computed from a handful of days
would be confidently wrong, and a reorder quantity is money.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from statistics import NormalDist
from typing import Any, Dict, List

from .logistics_io import load_records, number, series, text
from .logistics_tenancy import OWNER_FIELD, TenancyError, owner_params_schema, scope_records

DEFAULTS = {
    "service_level": 0.95,
    "review_period_days": 7.0,
    "min_history_days": 14,
    "slow_days": 30.0,
    "dead_days": 90.0,
    "excess_cover_days": 180.0,
}
ABC_BOUNDS = (0.80, 0.95)
XYZ_BOUNDS = (0.5, 1.0)


def _setting(params: Dict[str, Any], key: str) -> float:
    value = params.get(key, DEFAULTS[key])
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} must be finite and non-negative")
    return value


def _demand(record: Dict[str, Any], row: int) -> tuple[float, float, int] | None:
    """(mean, sample std, days of history), from a daily series or from aggregates."""
    history = series(record, "daily_demand", row)
    if history is not None:
        if len(history) < 2:
            return (history[0] if history else 0.0), 0.0, len(history)
        return statistics.fmean(history), statistics.stdev(history), len(history)
    mean = number(record, "demand_mean", row)
    if mean is None:
        return None
    std = number(record, "demand_std", row) or 0.0
    days = number(record, "demand_days", row)
    return mean, std, int(days) if days is not None else 0


def _abc(items: List[Dict[str, Any]]) -> None:
    """Assign ABC within each shipper's portfolio, by annual consumption value."""
    by_owner: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item["annual_value"] is not None:
            by_owner[item.get(OWNER_FIELD) or ""].append(item)
    for group in by_owner.values():
        total = sum(i["annual_value"] for i in group)
        if total <= 0:
            continue
        cumulative = 0.0
        for item in sorted(group, key=lambda i: (-i["annual_value"], i["sku"])):
            share_before = cumulative / total
            item["abc"] = "A" if share_before < ABC_BOUNDS[0] else "B" if share_before < ABC_BOUNDS[1] else "C"
            cumulative += item["annual_value"]


def tool_plan_inventory_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        records, source = load_records(params, "skus")
        records, scope = scope_records(records, params.get("owner_id"),
                                       all_owners=bool(params.get("all_owners")))

        service_level = _setting(params, "service_level")
        if not 0.5 <= service_level < 1:
            raise ValueError("service_level must be at least 0.5 and below 1")
        review = _setting(params, "review_period_days")
        min_history = int(_setting(params, "min_history_days"))
        slow_days = _setting(params, "slow_days")
        dead_days = _setting(params, "dead_days")
        excess_days = _setting(params, "excess_cover_days")
        z = NormalDist().inv_cdf(service_level)

        items: List[Dict[str, Any]] = []
        for row, record in enumerate(records, 1):
            sku = text(record, "sku", row, required=True)
            on_hand = number(record, "on_hand", row, required=True)
            on_order = number(record, "on_order", row) or 0.0
            backorder = number(record, "backorder", row) or 0.0
            lead = number(record, "lead_time_days", row, required=True, positive=True)
            lead_std = number(record, "lead_time_std_days", row) or 0.0
            unit_cost = number(record, "unit_cost", row)
            idle_days = number(record, "days_since_last_movement", row)
            demand = _demand(record, row)
            if demand is None:
                raise ValueError(f"Row {row}: daily_demand (or demand_mean) is required")
            mean, std, history_days = demand
            position = on_hand + on_order - backorder

            item: Dict[str, Any] = {
                OWNER_FIELD: text(record, OWNER_FIELD, row) or None,
                "warehouse_id": text(record, "warehouse_id", row) or None,
                "sku": sku,
                "on_hand": on_hand,
                "inventory_position": round(position, 2),
                "avg_daily_demand": round(mean, 4),
                "demand_std": round(std, 4),
                "history_days": history_days,
                "lead_time_days": lead,
                "safety_stock": None,
                "reorder_point": None,
                "order_up_to": None,
                "suggested_order_qty": 0,
                "days_of_cover": round(on_hand / mean, 1) if mean > 0 else None,
                "abc": None,
                "xyz": None,
                "movement": None,
                "flags": [],
                "annual_value": round(mean * 365 * unit_cost, 2) if unit_cost is not None else None,
            }

            if mean > 0:
                cv = std / mean
                item["xyz"] = "X" if cv <= XYZ_BOUNDS[0] else "Y" if cv <= XYZ_BOUNDS[1] else "Z"

            if idle_days is not None:
                item["movement"] = ("dead" if idle_days >= dead_days
                                    else "slow" if idle_days >= slow_days else "active")

            if mean == 0:
                item["action"] = "no_demand"
                if on_hand > 0:
                    item["flags"].append("stock held with no demand in the history")
            elif history_days < min_history:
                item["action"] = "insufficient_history"
                item["flags"].append(
                    f"{history_days} days of demand history; at least {min_history} are needed "
                    f"for a safety stock that means anything"
                )
            else:
                safety = z * math.sqrt(lead * std ** 2 + mean ** 2 * lead_std ** 2)
                reorder_point = mean * lead + safety
                order_up_to = reorder_point + mean * review
                # Rounded up: a fractional unit cannot be held, and rounding the
                # buffer down would quietly lower the service level.
                item["safety_stock"] = math.ceil(safety)
                item["reorder_point"] = math.ceil(reorder_point)
                item["order_up_to"] = math.ceil(order_up_to)
                if position <= reorder_point:
                    item["action"] = "reorder"
                    item["suggested_order_qty"] = max(0, math.ceil(order_up_to - position))
                else:
                    item["action"] = "ok"

            if item["days_of_cover"] is not None and item["days_of_cover"] > excess_days:
                item["flags"].append(f"{item['days_of_cover']:g} days of cover exceeds {excess_days:g}")
            items.append(item)

        _abc(items)

        order = {"reorder": 0, "insufficient_history": 1, "no_demand": 2, "ok": 3}
        items.sort(key=lambda i: (order[i["action"]],
                                  i["days_of_cover"] if i["days_of_cover"] is not None else math.inf,
                                  i["sku"]))

        counts = defaultdict(int)
        for item in items:
            counts[item["action"]] += 1
        dead = sum(1 for i in items if i["movement"] == "dead")
        slow = sum(1 for i in items if i["movement"] == "slow")

        assumptions = [
            f"service level {service_level:.1%} → z = {z:.3f}",
            "safety stock = z·√(L·σ_d² + d̄²·σ_L²); reorder point = d̄·L + safety stock",
            f"order-up-to = reorder point + d̄·{review:g} (review period, days)",
            "inventory position = on hand + on order − backordered",
            "safety stock, reorder point and order quantity rounded up to whole units",
            f"no policy below {min_history} days of demand history",
            f"ABC by annual consumption value within each shipper (A < {ABC_BOUNDS[0]:.0%}, B < {ABC_BOUNDS[1]:.0%} cumulative)",
            f"XYZ by demand CV (X ≤ {XYZ_BOUNDS[0]}, Y ≤ {XYZ_BOUNDS[1]}, Z above)",
            f"movement: slow ≥ {slow_days:g} days idle, dead ≥ {dead_days:g}",
            "capacity, MOQs, case packs and supplier terms are not modelled",
        ]
        if scope["mode"] == "all_owners":
            assumptions.insert(0, scope["marker"])

        summary = (
            f"{len(items)} {'SKU' if len(items) == 1 else 'SKUs'} analysed ({source}): {counts['reorder']} to reorder, "
            f"{counts['insufficient_history']} with too little history, {counts['no_demand']} "
            f"with no demand; {dead} dead and {slow} slow-moving."
        )
        return {
            "success": True,
            "data": {"scope": scope, "items": items, "counts": dict(counts),
                     "dead_stock": dead, "slow_stock": slow, "assumptions": assumptions},
            "source": source,
            "summary": summary,
        }
    except (OSError, ValueError, TypeError, TenancyError) as exc:
        return {"success": False, "error": str(exc)}


SCHEMA = {
    "name": "plan_inventory_policy",
    "description": (
        "Reorder points, safety stock, order quantities, ABC/XYZ classes and dead/slow stock "
        "for a shipper's SKUs, from supplied demand history and lead times. Formulas are "
        "stated in the result. Refuses to mix shippers' data unless told which one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV or JSON file of SKU records"},
            "skus": {"type": "array", "description": "SKU records: sku, on_hand, lead_time_days, daily_demand"},
            "service_level": {"type": "number", "description": "Target cycle service level, default 0.95"},
            "review_period_days": {"type": "number", "description": "Days between reviews, default 7"},
            **owner_params_schema(),
        },
    },
}
