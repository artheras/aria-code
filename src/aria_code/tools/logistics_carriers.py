"""Carrier scorecards, freight anomalies and savings for a 3PL, by lane.

A raw on-time percentage ranks a carrier with 1 of 1 deliveries on time above
one with 98 of 100, which is backwards. Carriers here are ranked by the lower
bound of the Wilson score interval for their on-time rate, which is what the
data actually supports: a carrier earns a high rank by being on time *often*,
not by being on time once.

    Wilson lower bound = (p̂ + z²/2n − z·√(p̂(1−p̂)/n + z²/4n²)) / (1 + z²/n)

Freight anomalies use the modified z-score of cost per kg within a lane —
median and median absolute deviation, so one outlier cannot hide itself by
inflating the mean it is compared against (Iglewicz & Hoaglin, |Mᵢ| > 3.5):

    Mᵢ = 0.6745 · (xᵢ − median) / MAD
    Mᵢ = (xᵢ − median) / (1.253314 · MeanAD)     when MAD = 0

The second form matters: on a contracted lane most shipments share one rate,
which makes MAD zero, and those are the lanes where an overcharge is plainest.

A savings opportunity is reported only when, on the same lane, an alternative
carrier is both cheaper per kg *and* at least as reliable by the same lower
bound, with enough shipments on both sides to say so. The estimate is volume
× rate difference and nothing more; contract terms, surcharges, minimums and
capacity are not modelled and the result says so.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from statistics import NormalDist
from typing import Any, Dict, List

from .logistics_io import flag, load_records, number, text
from .logistics_tenancy import TenancyError, owner_params_schema, scope_records

DEFAULTS = {"confidence": 0.95, "min_shipments": 20}
ANOMALY_THRESHOLD = 3.5
OVERBILLED_WEIGHT_RATIO = 1.2
MIN_LANE_ROWS_FOR_OUTLIERS = 5


def wilson_lower_bound(successes: int, trials: int, confidence: float = 0.95) -> float | None:
    if trials <= 0:
        return None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (centre - margin) / denominator


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _lane(record: Dict[str, Any], row: int) -> str:
    origin = text(record, "origin", row)
    destination = text(record, "destination", row)
    return f"{origin}→{destination}" if origin and destination else "(all lanes)"


def tool_score_carriers(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        records, source = load_records(params, "waybills")
        records, scope = scope_records(records, params.get("owner_id"),
                                       all_owners=bool(params.get("all_owners")))
        confidence = float(params.get("confidence", DEFAULTS["confidence"]))
        if not 0.5 <= confidence < 1:
            raise ValueError("confidence must be at least 0.5 and below 1")
        min_shipments = int(params.get("min_shipments", DEFAULTS["min_shipments"]))
        if min_shipments < 1:
            raise ValueError("min_shipments must be at least 1")

        cells: Dict[tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
            "shipments": 0, "total_cost": 0.0, "total_kg": 0.0, "cost_per_kg": [],
            "on_time_known": 0, "on_time": 0, "exception_known": 0, "exceptions": 0,
        })
        lane_rates: Dict[str, List[tuple[float, Dict[str, Any], str]]] = defaultdict(list)
        anomalies: List[Dict[str, Any]] = []

        for row, record in enumerate(records, 1):
            carrier = text(record, "carrier", row, required=True)
            cost = number(record, "total_cost", row, required=True)
            billed = number(record, "billed_weight_kg", row)
            actual = number(record, "actual_weight_kg", row)
            on_time = flag(record, "is_on_time", row)
            exception = flag(record, "has_exception", row)
            lane = _lane(record, row)
            waybill = text(record, "waybill_no", row) or f"row {row}"

            cell = cells[(lane, carrier)]
            cell["shipments"] += 1
            cell["total_cost"] += cost
            weight = billed if billed else actual
            if weight:
                cell["total_kg"] += weight
                rate = cost / weight
                cell["cost_per_kg"].append(rate)
                lane_rates[lane].append((rate, {"waybill_no": waybill, "carrier": carrier}, carrier))
            if on_time is not None:
                cell["on_time_known"] += 1
                cell["on_time"] += int(on_time)
            if exception is not None:
                cell["exception_known"] += 1
                cell["exceptions"] += int(exception)
            if actual and billed and billed > actual * OVERBILLED_WEIGHT_RATIO:
                anomalies.append({
                    "waybill_no": waybill, "carrier": carrier, "lane": lane,
                    "kind": "billed_weight",
                    "detail": f"billed {billed:g} kg vs actual {actual:g} kg "
                              f"(> {OVERBILLED_WEIGHT_RATIO - 1:.0%} over); check dimensional weight before disputing",
                })

        # Cost-per-kg outliers within each lane, expensive side only.
        for lane, rows in lane_rates.items():
            if len(rows) < MIN_LANE_ROWS_FOR_OUTLIERS:
                continue
            rates = [r for r, _, _ in rows]
            median = statistics.median(rates)
            deviations = [abs(r - median) for r in rates]
            mad = statistics.median(deviations)
            # When more than half the shipments share one rate — the normal
            # case on a contracted lane — MAD is 0 and the modified z-score
            # is undefined. Skipping the lane there would let a 6x overcharge
            # through on exactly the lanes where one is easiest to see. Fall
            # back to the mean absolute deviation, scaled so the threshold
            # means the same thing (1.253314 · MeanAD estimates σ).
            if mad > 0:
                scale = mad / 0.6745
            else:
                mean_ad = statistics.fmean(deviations)
                if mean_ad == 0:
                    continue  # every shipment on the lane has the same rate
                scale = 1.253314 * mean_ad
            for rate, meta, carrier in rows:
                score = (rate - median) / scale
                if score > ANOMALY_THRESHOLD:
                    anomalies.append({
                        **meta, "lane": lane, "kind": "cost_per_kg",
                        "detail": f"{rate:.2f}/kg vs lane median {median:.2f}/kg (modified z {score:.1f})",
                    })

        scorecard: List[Dict[str, Any]] = []
        for (lane, carrier), cell in cells.items():
            lower = wilson_lower_bound(cell["on_time"], cell["on_time_known"], confidence)
            scorecard.append({
                "lane": lane,
                "carrier": carrier,
                "shipments": cell["shipments"],
                "on_time_rate": round(cell["on_time"] / cell["on_time_known"], 4) if cell["on_time_known"] else None,
                "on_time_lower_bound": round(lower, 4) if lower is not None else None,
                "on_time_sample": cell["on_time_known"],
                "median_cost_per_kg": round(statistics.median(cell["cost_per_kg"]), 4) if cell["cost_per_kg"] else None,
                "total_cost": round(cell["total_cost"], 2),
                "total_kg": round(cell["total_kg"], 2),
                "exception_rate": round(cell["exceptions"] / cell["exception_known"], 4) if cell["exception_known"] else None,
                "rankable": cell["shipments"] >= min_shipments and lower is not None,
            })

        # Rank within each lane: reliability first, then price.
        for lane in {c["lane"] for c in scorecard}:
            ranked = sorted(
                (c for c in scorecard if c["lane"] == lane and c["rankable"]),
                key=lambda c: (-c["on_time_lower_bound"],
                               c["median_cost_per_kg"] if c["median_cost_per_kg"] is not None else math.inf,
                               c["carrier"]),
            )
            for position, entry in enumerate(ranked, 1):
                entry["rank_in_lane"] = position
        for entry in scorecard:
            entry.setdefault("rank_in_lane", None)

        savings: List[Dict[str, Any]] = []
        for lane in {c["lane"] for c in scorecard}:
            eligible = [c for c in scorecard if c["lane"] == lane and c["rankable"]
                        and c["median_cost_per_kg"] is not None and c["total_kg"] > 0]
            for current in eligible:
                for alternative in eligible:
                    if alternative is current:
                        continue
                    if (alternative["median_cost_per_kg"] < current["median_cost_per_kg"]
                            and alternative["on_time_lower_bound"] >= current["on_time_lower_bound"]):
                        estimate = current["total_kg"] * (current["median_cost_per_kg"] - alternative["median_cost_per_kg"])
                        savings.append({
                            "lane": lane,
                            "from_carrier": current["carrier"],
                            "to_carrier": alternative["carrier"],
                            "volume_kg": current["total_kg"],
                            "rate_difference_per_kg": round(current["median_cost_per_kg"] - alternative["median_cost_per_kg"], 4),
                            "estimated_saving": round(estimate, 2),
                        })
        # Keep the best alternative per (lane, from_carrier).
        best: Dict[tuple[str, str], Dict[str, Any]] = {}
        for item in savings:
            key = (item["lane"], item["from_carrier"])
            if key not in best or item["estimated_saving"] > best[key]["estimated_saving"]:
                best[key] = item
        savings = sorted(best.values(), key=lambda s: -s["estimated_saving"])

        scorecard.sort(key=lambda c: (c["lane"], c["rank_in_lane"] or math.inf, c["carrier"]))
        assumptions = [
            f"carriers ranked within each lane by the {confidence:.0%} Wilson lower bound of on-time rate, then median cost/kg",
            f"only carrier-lane pairs with ≥ {min_shipments} shipments are ranked or used for savings",
            "cost per kg uses billed weight where present, otherwise actual weight",
            f"cost/kg anomaly: modified z-score > {ANOMALY_THRESHOLD} within the lane (lanes with ≥ {MIN_LANE_ROWS_FOR_OUTLIERS} weighed shipments); "
            "MAD-based, falling back to 1.253314 × mean absolute deviation when MAD is 0",
            f"weight anomaly: billed weight > {OVERBILLED_WEIGHT_RATIO:g} × actual",
            "savings require the alternative to be cheaper per kg and at least as reliable; "
            "estimate = volume × rate difference, excluding contract terms, surcharges, minimums and capacity",
        ]
        if scope["mode"] == "all_owners":
            assumptions.insert(0, scope["marker"])

        total_saving = round(sum(s["estimated_saving"] for s in savings), 2)
        summary = (
            f"{len(records)} waybills across {len({c['lane'] for c in scorecard})} lanes and "
            f"{len({c['carrier'] for c in scorecard})} carriers ({source}); {_count(len(anomalies), 'anomaly', 'anomalies')} "
            f"to check; {_count(len(savings), 'savings opportunity', 'savings opportunities')}, "
            f"estimated {total_saving:,.2f} in total."
        )
        return {
            "success": True,
            "data": {"scope": scope, "scorecard": scorecard, "anomalies": anomalies,
                     "savings": savings, "estimated_total_saving": total_saving,
                     "assumptions": assumptions},
            "source": source,
            "summary": summary,
        }
    except (OSError, ValueError, TypeError, TenancyError) as exc:
        return {"success": False, "error": str(exc)}


SCHEMA = {
    "name": "score_carriers",
    "description": (
        "Carrier scorecard by lane (on-time rate with a confidence lower bound, median cost/kg, "
        "exception rate), freight cost anomalies and like-for-like savings, from supplied "
        "waybills. Refuses to mix shippers' data unless told which one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "CSV or JSON file of waybills"},
            "waybills": {"type": "array", "description": "Waybills: carrier, total_cost, origin, destination, billed_weight_kg, is_on_time"},
            "min_shipments": {"type": "integer", "description": "Minimum shipments for a carrier-lane to be ranked, default 20"},
            **owner_params_schema(),
        },
    },
}
