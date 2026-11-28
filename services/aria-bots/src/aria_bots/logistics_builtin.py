"""Built-in deterministic 3PL logistics engine for ``aria-bots``.

Used automatically when the external ``erp_service`` package is not installed,
preserving identical tool names, JSON contracts, shipper tenancy isolation
(``ARIA_OWNER_SCOPE``), and verifiable replenishment / freight-audit math.
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OWNER_SCOPE_ENV = "ARIA_OWNER_SCOPE"


class TenancyError(ValueError):
    """Raised when records violate single-shipper isolation rules."""


def owner_params_schema() -> Dict[str, Any]:
    return {
        "owner_id": {
            "type": "string",
            "description": "Shipper / owner identifier when analyzing multi-owner warehouse data.",
        },
        "all_owners": {
            "type": "boolean",
            "description": "Explicitly aggregate across all owners (refused when ARIA_OWNER_SCOPE is bound).",
        },
    }


CARRIER_SCHEMA: Dict[str, Any] = {
    "name": "score_carriers",
    "description": "Score carriers by lane, flag per-kg billing anomalies, and recommend cost-saving switches that preserve on-time reliability.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV, JSON, or XLSX waybill file path"},
            "waybills": {"type": "array", "description": "Inline waybill records"},
            **owner_params_schema(),
        },
    },
}

INVENTORY_SCHEMA: Dict[str, Any] = {
    "name": "plan_inventory_policy",
    "description": "Compute safety stock, reorder points, order quantities, ABC/XYZ classes, and thin-history/dead-stock flags per SKU.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV, JSON, or XLSX SKU inventory file path"},
            "skus": {"type": "array", "description": "Inline SKU records"},
            "service_level": {"type": "number", "description": "Target service level in (0.5, 0.9999), default 0.95"},
            "review_period_days": {"type": "number", "description": "Order-up-to review period in days, default 7"},
            **owner_params_schema(),
        },
    },
}


def load_records(params: Dict[str, Any], inline_key: str) -> Tuple[List[Dict[str, Any]], str]:
    inline = params.get(inline_key)
    if isinstance(inline, list):
        return [dict(r) for r in inline if isinstance(r, dict)], f"inline:{inline_key}"

    file_path = params.get("file_path")
    if not file_path:
        raise ValueError(f"Provide either 'file_path' or inline '{inline_key}' records.")
    path = Path(str(file_path))
    if not path.is_file():
        raise ValueError(f"File not found: {path}")

    ext = path.suffix.lower()
    if ext == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get(inline_key) or raw.get("records") or raw.get("items") or []
        if not isinstance(raw, list):
            raise ValueError("JSON file must contain a list of record objects.")
        return [dict(r) for r in raw if isinstance(r, dict)], path.name
    if ext == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
        return rows, path.name
    if ext in (".xlsx", ".xlsm"):
        import openpyxl  # optional in aria-bots[files]

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            iter_rows = ws.iter_rows(values_only=True)
            headers_raw = next(iter_rows, None)
            if not headers_raw:
                return [], path.name
            headers = [str(h or "").strip() for h in headers_raw]
            out: List[Dict[str, Any]] = []
            for row in iter_rows:
                if row is None or all(v is None or str(v).strip() == "" for v in row):
                    continue
                out.append({headers[i]: row[i] for i in range(min(len(headers), len(row))) if headers[i]})
            return out, path.name
        finally:
            wb.close()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(r) for r in reader], path.name


def scope_records(
    records: List[Dict[str, Any]],
    requested_owner: Optional[Any] = None,
    *,
    all_owners: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    if not records:
        raise ValueError("No records supplied.")

    bound_owner = os.environ.get(OWNER_SCOPE_ENV, "").strip()
    req_owner = str(requested_owner or "").strip()

    if bound_owner:
        if all_owners:
            raise TenancyError(
                f"Chat is bound to shipper '{bound_owner}'; cross-shipper all_owners queries are refused."
            )
        if req_owner and req_owner != bound_owner:
            raise TenancyError(
                f"Chat is bound to shipper '{bound_owner}'; cannot access shipper '{req_owner}'."
            )
        target = bound_owner
    else:
        target = req_owner

    has_owner_col = any("owner_id" in r or "shipper_id" in r for r in records)
    if not has_owner_col:
        if bound_owner:
            raise TenancyError(
                f"Chat is bound to shipper '{bound_owner}', but records lack an owner_id/shipper_id column."
            )
        return records, "unscoped"

    def _row_owner(row: Dict[str, Any]) -> str:
        val = row.get("owner_id")
        if val is None or str(val).strip() == "":
            val = row.get("shipper_id")
        return str(val or "").strip()

    missing = [idx for idx, r in enumerate(records, 1) if not _row_owner(r)]
    if missing:
        raise TenancyError(
            f"Refusing unattributed rows missing owner_id/shipper_id (first at row {missing[0]})."
        )

    owners_present = sorted({_row_owner(r) for r in records})
    if target:
        filtered = [r for r in records if _row_owner(r) == target]
        if not filtered:
            raise TenancyError(f"No records found for shipper '{target}'.")
        return filtered, f"owner:{target}"

    if len(owners_present) > 1 and not all_owners:
        raise TenancyError(
            f"Records span multiple shippers ({', '.join(owners_present)}); specify owner_id or set all_owners=true."
        )
    if len(owners_present) == 1:
        return records, f"owner:{owners_present[0]}"
    return records, "all_owners"


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or str(val).strip() == "":
        return default
    return float(val)


def _parse_on_time(val: Any) -> Optional[bool]:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on_time", "ontime"):
        return True
    if s in ("0", "false", "no", "n", "late", "delayed"):
        return False
    return None


def _audit_waybill_rows(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_lane: Dict[str, List[Dict[str, Any]]] = {}
    parsed: List[Dict[str, Any]] = []
    total_spend = 0.0
    on_time_known: List[bool] = []

    for idx, r in enumerate(records, 1):
        wb_no = str(r.get("waybill_no") or r.get("id") or f"ROW-{idx}").strip()
        origin = str(r.get("origin") or "UNK").strip().upper()
        dest = str(r.get("destination") or "UNK").strip().upper()
        lane = f"{origin}-{dest}"
        carrier = str(r.get("carrier") or "UNKNOWN").strip()
        weight = _to_float(r.get("billed_weight_kg") or r.get("weight_kg"), 0.0)
        cost = _to_float(r.get("total_cost") or r.get("cost"), 0.0)
        actual_w = _to_float(r.get("actual_weight_kg"), 0.0)
        ot = _parse_on_time(r.get("is_on_time"))
        rate = (cost / weight) if weight > 0 else None

        total_spend += cost
        if ot is not None:
            on_time_known.append(ot)

        entry = {
            "waybill_no": wb_no,
            "lane": lane,
            "carrier": carrier,
            "billed_weight_kg": weight,
            "actual_weight_kg": actual_w if actual_w > 0 else None,
            "total_cost": cost,
            "rate_per_kg": rate,
            "is_on_time": ot,
        }
        parsed.append(entry)
        by_lane.setdefault(lane, []).append(entry)

    anomalies: List[Dict[str, Any]] = []
    for lane, items in sorted(by_lane.items()):
        rates = [it["rate_per_kg"] for it in items if it["rate_per_kg"] is not None and it["rate_per_kg"] > 0]
        median_rate = statistics.median(rates) if rates else 0.0
        mad = statistics.median([abs(x - median_rate) for x in rates]) if len(rates) >= 3 else 0.0
        for it in items:
            reasons = []
            rate = it["rate_per_kg"]
            if rate is not None and median_rate > 0:
                ratio = rate / median_rate
                z_mad = ((rate - median_rate) / (1.4826 * mad)) if mad > 1e-9 else 0.0
                if ratio >= 1.8 and (len(rates) < 4 or z_mad >= 3.5 or ratio >= 2.0):
                    reasons.append(f"rate_per_kg {rate:.2f} vs lane median {median_rate:.2f} ({ratio:.2f}x)")
            if it["actual_weight_kg"] and it["billed_weight_kg"] > it["actual_weight_kg"] * 1.25:
                reasons.append(
                    f"billed_weight_kg {it['billed_weight_kg']} exceeds actual_weight_kg {it['actual_weight_kg']} by >25%"
                )
            if reasons:
                anomalies.append({
                    "waybill_no": it["waybill_no"],
                    "lane": lane,
                    "carrier": it["carrier"],
                    "total_cost": round(it["total_cost"], 2),
                    "billed_weight_kg": it["billed_weight_kg"],
                    "rate_per_kg": round(rate, 4) if rate is not None else None,
                    "lane_median_rate_per_kg": round(median_rate, 4),
                    "reason": "; ".join(reasons),
                })

    # Score carriers per lane (excluding flagged billing anomalies from normal rate comparison)
    flagged_set = {a["waybill_no"] for a in anomalies}
    carrier_lane_stats: List[Dict[str, Any]] = []
    savings: List[Dict[str, Any]] = []

    for lane, items in sorted(by_lane.items()):
        by_carrier: Dict[str, List[Dict[str, Any]]] = {}
        for it in items:
            by_carrier.setdefault(it["carrier"], []).append(it)

        lane_Map: Dict[str, Dict[str, Any]] = {}
        for carrier, c_items in sorted(by_carrier.items()):
            clean = [x for x in c_items if x["waybill_no"] not in flagged_set and x["rate_per_kg"] is not None]
            sample = clean if clean else [x for x in c_items if x["rate_per_kg"] is not None]
            w_sum = sum(x["billed_weight_kg"] for x in sample)
            c_sum = sum(x["total_cost"] for x in sample)
            avg_rate = (c_sum / w_sum) if w_sum > 0 else 0.0
            ot_vals = [x["is_on_time"] for x in c_items if x["is_on_time"] is not None]
            ot_rate = (sum(1 for v in ot_vals if v) / len(ot_vals)) if ot_vals else None
            stat = {
                "lane": lane,
                "carrier": carrier,
                "shipments": len(c_items),
                "total_spend": round(sum(x["total_cost"] for x in c_items), 2),
                "total_weight_kg": round(sum(x["billed_weight_kg"] for x in c_items), 2),
                "avg_rate_per_kg": round(avg_rate, 4),
                "on_time_rate": round(ot_rate, 4) if ot_rate is not None else None,
            }
            carrier_lane_stats.append(stat)
            lane_Map[carrier] = stat

        # Recommend switch only when alternative is meaningfully cheaper AND at least as reliable
        for from_c, cur in lane_Map.items():
            if cur["on_time_rate"] is None or cur["avg_rate_per_kg"] <= 0:
                continue
            best_alt: Optional[Dict[str, Any]] = None
            for to_c, alt in lane_Map.items():
                if to_c == from_c or alt["on_time_rate"] is None or alt["avg_rate_per_kg"] <= 0:
                    continue
                if alt["on_time_rate"] >= cur["on_time_rate"] and alt["avg_rate_per_kg"] < cur["avg_rate_per_kg"] * 0.95:
                    if best_alt is None or alt["avg_rate_per_kg"] < best_alt["avg_rate_per_kg"]:
                        best_alt = alt
            if best_alt is not None:
                est_saving = (cur["avg_rate_per_kg"] - best_alt["avg_rate_per_kg"]) * cur["total_weight_kg"]
                savings.append({
                    "lane": lane,
                    "from_carrier": from_c,
                    "to_carrier": best_alt["carrier"],
                    "from_rate_per_kg": cur["avg_rate_per_kg"],
                    "to_rate_per_kg": best_alt["avg_rate_per_kg"],
                    "from_on_time_rate": cur["on_time_rate"],
                    "to_on_time_rate": best_alt["on_time_rate"],
                    "estimated_savings": round(max(0.0, est_saving), 2),
                })

    overall_ot = round(sum(1 for v in on_time_known if v) * 100.0 / len(on_time_known), 2) if on_time_known else None
    return {
        "total_waybills": len(parsed),
        "total_freight_spend": round(total_spend, 2),
        "overall_on_time_rate": overall_ot,
        "billing_anomalies": anomalies,
        "anomalies": anomalies,
        "carriers": carrier_lane_stats,
        "savings": savings,
    }


def tool_analyze_logistics_data_builtin(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        records, source = load_records(params, "waybills")
        records, scope = scope_records(
            records,
            params.get("owner_id"),
            all_owners=bool(params.get("all_owners")),
        )
    except (TenancyError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": str(exc)}

    data = _audit_waybill_rows(records)
    data["data_source"] = source
    data["scope"] = scope
    rate = data["overall_on_time_rate"]
    rate_label = f"{rate}%" if rate is not None else "不可得（无准时状态记录）"
    return {
        "success": True,
        "source": str(params.get("file_path") or source),
        "summary": (
            f"已审计 {data['total_waybills']} 单运单（来源：{source}），"
            f"运费总计 ¥{data['total_freight_spend']:,.2f}，已知准时交付率 {rate_label}，"
            f"发现 {len(data['billing_anomalies'])} 笔需核实的计费异常。"
        ),
        "data": data,
    }


def tool_score_carriers(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        records, source = load_records(params, "waybills")
        records, scope = scope_records(
            records,
            params.get("owner_id"),
            all_owners=bool(params.get("all_owners")),
        )
    except (TenancyError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": str(exc)}

    data = _audit_waybill_rows(records)
    data["data_source"] = source
    data["scope"] = scope
    total_savings = round(sum(s["estimated_savings"] for s in data["savings"]), 2)
    data["total_estimated_savings"] = total_savings
    return {
        "success": True,
        "source": str(params.get("file_path") or source),
        "summary": (
            f"已评估 {len(data['carriers'])} 组线路承运商表现（{data['total_waybills']} 单），"
            f"识别 {len(data['anomalies'])} 笔计费异常与 {len(data['savings'])} 条不降准时率的可省钱切换路线（预计节省 ¥{total_savings:,.2f}）。"
        ),
        "data": data,
    }


def _parse_demand_series(raw: Any) -> List[float]:
    if isinstance(raw, list):
        return [float(x) for x in raw]
    text = str(raw or "").strip()
    if not text:
        return []
    parts = [p for p in text.replace(",", " ").split() if p]
    return [float(p) for p in parts]


def tool_plan_inventory_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        records, source = load_records(params, "skus")
        records, scope = scope_records(
            records,
            params.get("owner_id"),
            all_owners=bool(params.get("all_owners")),
        )
    except (TenancyError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": str(exc)}

    service_level = float(params.get("service_level") or 0.95)
    if not (0.5 < service_level < 0.9999):
        return {"success": False, "error": "service_level must be between 0.5 and 0.9999"}
    review_days = float(params.get("review_period_days") if params.get("review_period_days") is not None else 7.0)
    z = statistics.NormalDist().inv_cdf(service_level)

    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(records, 1):
        sku = str(row.get("sku") or f"SKU-{idx}").strip()
        on_hand = _to_float(row.get("on_hand"), 0.0)
        on_order = _to_float(row.get("on_order"), 0.0)
        backorder = _to_float(row.get("backorder"), 0.0)
        lead_time = _to_float(row.get("lead_time_days"), 0.0)
        lead_std = _to_float(row.get("lead_time_std_days"), 0.0)
        demand = _parse_demand_series(row.get("daily_demand"))
        position = on_hand + on_order - backorder

        if len(demand) < 14:
            items.append({
                "sku": sku,
                "days_of_history": len(demand),
                "inventory_position": round(position, 2),
                "action": "insufficient_history",
                "status": "insufficient_history",
                "safety_stock": None,
                "reorder_point": None,
                "suggested_order_qty": 0,
            })
            continue

        mean_d = statistics.fmean(demand)
        std_d = statistics.stdev(demand) if len(demand) > 1 else 0.0
        if mean_d <= 0 and std_d <= 0:
            items.append({
                "sku": sku,
                "days_of_history": len(demand),
                "mean_daily_demand": 0.0,
                "inventory_position": round(position, 2),
                "action": "dead_stock",
                "xyz_class": "Z",
                "safety_stock": 0.0,
                "reorder_point": 0.0,
                "suggested_order_qty": 0,
            })
            continue

        variance = lead_time * (std_d ** 2) + (mean_d ** 2) * (lead_std ** 2)
        safety_stock = z * math.sqrt(max(0.0, variance))
        reorder_point = mean_d * lead_time + safety_stock
        if position <= reorder_point:
            order_qty = max(0.0, reorder_point + mean_d * review_days - position)
            action = "reorder" if order_qty > 0 else "hold"
        else:
            order_qty = 0.0
            action = "hold"

        cv = (std_d / mean_d) if mean_d > 0 else float("inf")
        xyz = "X" if cv <= 0.5 else ("Y" if cv <= 1.0 else "Z")
        unit_cost = _to_float(row.get("unit_cost") or row.get("unit_price") or row.get("price"), 0.0)
        annual_value = (mean_d * 365.0 * unit_cost) if unit_cost > 0 else (mean_d * 365.0)

        items.append({
            "sku": sku,
            "days_of_history": len(demand),
            "mean_daily_demand": round(mean_d, 2),
            "demand_std": round(std_d, 2),
            "inventory_position": round(position, 2),
            "annual_consumption_value": round(annual_value, 2),
            "xyz_class": xyz,
            "action": action,
            "safety_stock": round(safety_stock, 1),
            "reorder_point": round(reorder_point, 1),
            "suggested_order_qty": round(order_qty, 1) if order_qty > 0 else 0,
        })

    # Compute ABC Pareto classification across active SKUs
    active_ranked = sorted(
        [it for it in items if it.get("annual_consumption_value", 0) > 0],
        key=lambda it: it["annual_consumption_value"],
        reverse=True,
    )
    total_val = sum(it["annual_consumption_value"] for it in active_ranked)
    if total_val > 0:
        cum = 0.0
        for it in active_ranked:
            cum += it["annual_consumption_value"]
            share = cum / total_val
            abc = "A" if share <= 0.80 else ("B" if share <= 0.95 else "C")
            it["abc_class"] = abc
            it["abc_xyz"] = f"{abc}{it.get('xyz_class', 'Z')}"

    return {
        "success": True,
        "source": str(params.get("file_path") or source),
        "formula": "safety_stock = z * sqrt(L * sigma_d^2 + d_bar^2 * sigma_L^2); reorder_point = d_bar * L + safety_stock; position = on_hand + on_order - backorder",
        "assumptions": {
            "service_level": service_level,
            "z_score": round(z, 4),
            "review_period_days": review_days,
            "min_history_days": 14,
            "scope": scope,
        },
        "data": {
            "scope": scope,
            "data_source": source,
            "items": items,
        },
    }


def _load_csv_or_inline(params: Dict[str, Any], path_key: str, inline_key: str) -> List[Dict[str, Any]]:
    inline = params.get(inline_key)
    if isinstance(inline, list):
        return [dict(r) for r in inline if isinstance(r, dict)]
    p = params.get(path_key)
    if not p:
        raise ValueError(f"Provide '{path_key}' or inline '{inline_key}'")
    path = Path(str(p))
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def tool_reconcile_ar_aging(params: Dict[str, Any]) -> Dict[str, Any]:
    """Compute AR aging buckets (current, 1-30, 31-60, 61-90, 90+) as of cutoff_date."""
    from datetime import date

    try:
        cutoff_str = str(params.get("cutoff_date") or "").strip()
        if not cutoff_str:
            raise ValueError("cutoff_date (YYYY-MM-DD) is required")
        cutoff = date.fromisoformat(cutoff_str)
        invoices = _load_csv_or_inline(params, "invoices_path", "invoices")
        payments = _load_csv_or_inline(params, "payments_path", "payments")
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}

    paid_by_inv: Dict[str, float] = {}
    for p in payments:
        p_date_raw = str(p.get("received_date") or p.get("payment_date") or p.get("date") or "").strip()
        if p_date_raw and date.fromisoformat(p_date_raw) > cutoff:
            continue
        inv_id = str(p.get("invoice_no") or p.get("invoice_id") or "").strip()
        amt = _to_float(p.get("amount"), 0.0)
        paid_by_inv[inv_id] = paid_by_inv.get(inv_id, 0.0) + amt

    buckets: Dict[str, Dict[str, float]] = {}
    for inv in invoices:
        issue_raw = str(inv.get("issue_date") or "").strip()
        if issue_raw and date.fromisoformat(issue_raw) > cutoff:
            continue
        inv_id = str(inv.get("invoice_no") or inv.get("invoice_id") or "").strip()
        cust = str(inv.get("customer") or "UNKNOWN").strip()
        amount = _to_float(inv.get("amount"), 0.0)
        outstanding = round(max(0.0, amount - paid_by_inv.get(inv_id, 0.0)), 2)
        if outstanding <= 0:
            continue
        due = date.fromisoformat(str(inv.get("due_date") or issue_raw).strip())
        overdue = (cutoff - due).days
        if overdue <= 0:
            b = "current"
        elif overdue <= 30:
            b = "1-30"
        elif overdue <= 60:
            b = "31-60"
        elif overdue <= 90:
            b = "61-90"
        else:
            b = "90+"
        row = buckets.setdefault(cust, {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0})
        row[b] = round(row[b] + outstanding, 2)

    return {"success": True, "cutoff_date": cutoff_str, "data": {"aging": buckets}}


def tool_reconcile_cycle_count(params: Dict[str, Any]) -> Dict[str, Any]:
    """Reconcile physical cycle counts (with UOM conversions) against system inventory."""
    try:
        system_rows = _load_csv_or_inline(params, "system_path", "system")
        count_rows = _load_csv_or_inline(params, "counts_path", "counts")
        uom_rows = _load_csv_or_inline(params, "uom_path", "uom")
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}

    uom_factor: Dict[Tuple[str, str], float] = {}
    for u in uom_rows:
        sku = str(u.get("sku") or "").strip()
        unit = str(u.get("uom") or u.get("unit") or "").strip().lower()
        factor = _to_float(u.get("eaches") or u.get("eaches_per_unit") or u.get("factor") or u.get("qty_per_uom"), 1.0)
        uom_factor[(sku, unit)] = factor

    sys_map: Dict[Tuple[str, str], int] = {}
    for r in system_rows:
        loc = str(r.get("location") or "").strip()
        sku = str(r.get("sku") or "").strip()
        qty = int(round(_to_float(r.get("qty_each") or r.get("qty") or r.get("system_qty") or r.get("on_hand"), 0.0)))
        sys_map[(loc, sku)] = sys_map.get((loc, sku), 0) + qty

    counted_locs = set()
    cnt_map: Dict[Tuple[str, str], int] = {}
    for r in count_rows:
        loc = str(r.get("location") or "").strip()
        sku = str(r.get("sku") or "").strip()
        unit = str(r.get("uom") or r.get("unit") or "each").strip().lower()
        raw_qty = _to_float(r.get("counted") or r.get("qty") or r.get("counted_qty"), 0.0)
        factor = 1.0 if unit in ("each", "ea", "pcs", "pc", "unit", "") else uom_factor.get((sku, unit), 1.0)
        eaches = int(round(raw_qty * factor))
        counted_locs.add(loc)
        cnt_map[(loc, sku)] = cnt_map.get((loc, sku), 0) + eaches

    all_keys = {k for k in sys_map if k[0] in counted_locs} | set(cnt_map.keys())
    variances = []
    for loc, sku in sorted(all_keys):
        sys_q = sys_map.get((loc, sku), 0)
        cnt_q = cnt_map.get((loc, sku), 0)
        if cnt_q != sys_q:
            variances.append({
                "location": loc,
                "sku": sku,
                "system": sys_q,
                "counted": cnt_q,
                "variance": cnt_q - sys_q,
            })
    return {"success": True, "data": {"variances": variances}}


def tool_convert_fx_invoices(params: Dict[str, Any]) -> Dict[str, Any]:
    """Convert foreign-currency invoices to EUR using latest rate on or before invoice date."""
    from datetime import date

    try:
        invoices = _load_csv_or_inline(params, "invoices_path", "invoices")
        rates = _load_csv_or_inline(params, "rates_path", "rates")
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}

    # rates.csv: date, pair (e.g. EURUSD) or currency columns
    rate_history: Dict[str, List[Tuple[date, float]]] = {}
    for r in rates:
        d = date.fromisoformat(str(r.get("date") or "").strip())
        if "pair" in r and "rate" in r:
            pair = str(r["pair"]).strip().upper()
            rate_history.setdefault(pair, []).append((d, _to_float(r["rate"])))
        else:
            for k, v in r.items():
                ku = str(k).strip().upper()
                if ku.startswith("EUR") and len(ku) == 6 and str(v or "").strip() != "":
                    rate_history.setdefault(ku, []).append((d, _to_float(v)))

    for pair in rate_history:
        rate_history[pair].sort(key=lambda x: x[0])

    out_inv: Dict[str, float] = {}
    total = 0.0
    for inv in invoices:
        inv_no = str(inv.get("invoice_no") or inv.get("invoice_id") or "").strip()
        cur = str(inv.get("currency") or "EUR").strip().upper()
        amt = _to_float(inv.get("amount"), 0.0)
        d = date.fromisoformat(str(inv.get("invoice_date") or inv.get("date") or "").strip())
        if cur == "EUR":
            eur = round(amt, 2)
        else:
            series = rate_history.get(f"EUR{cur}", [])
            eligible = [rt for dt, rt in series if dt <= d]
            if not eligible:
                return {"success": False, "error": f"No EUR{cur} rate on or before {d}"}
            eur = round(amt / eligible[-1], 2)
        out_inv[inv_no] = eur
        total += eur

    return {"success": True, "data": {"invoices": out_inv, "total_eur": round(total, 2)}}


def tool_calculate_customs_duty(params: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate EU customs duty per shipment with EUR 150 de minimis & longest HS prefix match."""
    try:
        lines = _load_csv_or_inline(params, "lines_path", "lines")
        duty_rates = _load_csv_or_inline(params, "duty_rates_path", "duty_rates")
        fx_rows = _load_csv_or_inline(params, "fx_path", "fx")
    except (ValueError, OSError) as exc:
        return {"success": False, "error": str(exc)}

    fx_map: Dict[str, float] = {"EUR": 1.0}
    for r in fx_rows:
        cur = str(r.get("currency") or "").strip().upper()
        if cur:
            fx_map[cur] = _to_float(r.get("eur_per_unit") or r.get("rate_to_eur") or r.get("rate"), 1.0)
        for k, v in r.items():
            ku = str(k).strip().upper()
            if ku.startswith("EUR") and len(ku) == 6 and str(v or "").strip() != "":
                fx_map[ku[3:]] = 1.0 / _to_float(v, 1.0)

    prefixes: List[Tuple[str, float]] = []
    for r in duty_rates:
        pref = str(r.get("hs_prefix") or r.get("prefix") or "").strip()
        rate = _to_float(r.get("duty_rate") or r.get("rate"), 0.0)
        if rate > 1.0:
            rate /= 100.0
        if pref:
            prefixes.append((pref, rate))

    def _match_rate(hs: str) -> float:
        best_len = -1
        best_rate = 0.0
        for pref, rate in prefixes:
            if hs.startswith(pref) and len(pref) > best_len:
                best_len = len(pref)
                best_rate = rate
        return best_rate

    shipments: Dict[str, List[Tuple[float, float]]] = {}
    for ln in lines:
        shp = str(ln.get("shipment_id") or ln.get("waybill_no") or "").strip()
        cur = str(ln.get("currency") or "EUR").strip().upper()
        val = _to_float(ln.get("value") or ln.get("customs_value"), 0.0)
        if cur != "EUR":
            # Support both EURxxx (units per EUR) and direct eur_per_unit
            if f"EUR{cur}" in ln:
                eur_val = val / _to_float(ln[f"EUR{cur}"], 1.0)
            else:
                rate = fx_map.get(cur)
                if rate is None:
                    return {"success": False, "error": f"Missing FX rate for {cur}"}
                eur_val = val * rate if rate < 2.0 and cur != "JPY" else val / rate
        else:
            eur_val = val
        hs = str(ln.get("hs_code") or "").strip()
        shipments.setdefault(shp, []).append((eur_val, _match_rate(hs)))

    duties: Dict[str, float] = {}
    for shp, items in sorted(shipments.items()):
        total_val = sum(v for v, _ in items)
        if total_val <= 150.0:
            duties[shp] = 0.0
        else:
            duties[shp] = round(sum(v * r for v, r in items), 2)

    return {"success": True, "data": {"duties": duties}}
