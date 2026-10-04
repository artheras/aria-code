"""Warehouse ERP command; globals are rebound by the legacy CLI bridge."""

from __future__ import annotations

import json


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class WarehouseCommandsMixin:
    """Read-only warehouse ERP analysis commands."""

    async def cmd_warehouse(self, args: str):
        """Run `/warehouse <warehouse_id> [--json]` against the configured ERP."""
        parts = args.strip().split()
        as_json = "--json" in parts
        identifiers = [part for part in parts if not part.startswith("--")]
        if len(identifiers) != 1:
            message = "用法: /warehouse <仓库编号> [--json]，例如 /warehouse WH-CN-01"
            self.context.console.print(f"[yellow]{message}[/yellow]") if self.context.has_rich else print(message)
            return

        from agents.warehouse.workflow import run_warehouse_analysis
        from clients.warehouse_erp_client import WarehouseERPConfigurationError, WarehouseERPRequestError

        try:
            result, snapshot = await run_warehouse_analysis(identifiers[0])
        except (WarehouseERPConfigurationError, WarehouseERPRequestError) as exc:
            message = str(exc)
            self.context.console.print(f"[red]{message}[/red]") if self.context.has_rich else print(message)
            return

        payload = {
            "warehouse_id": identifiers[0],
            "signal": result.final_signal,
            "confidence": result.confidence,
            "agents": [item.to_dict() for item in result.results],
            "snapshot_counts": {key: len(snapshot.get(key, [])) for key in ("connectors", "inbounds", "skus", "locations")},
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        lines = [
            f"仓库 {identifiers[0]} · {result.final_signal} · 置信度 {result.confidence:.0%}",
            f"货代 {payload['snapshot_counts']['connectors']} · 入库单 {payload['snapshot_counts']['inbounds']} · "
            f"SKU {payload['snapshot_counts']['skus']} · 库位 {payload['snapshot_counts']['locations']}",
        ]
        for item in result.results:
            summary = item.key_points[0] if item.key_points else item.analysis
            lines.append(f"- {item.agent}: {item.signal} — {summary}")
        output = "\n".join(lines)
        self.context.console.print(output) if self.context.has_rich else print(output)


def _parse_logistics_args(args: str, usage: str) -> dict | None:
    """`<file> [--owner ID | --all-owners] [--json] [--key value]` → params, or None on misuse.

    shlex, so a path with spaces can be quoted. The shipper flags map straight
    onto the tools' owner_id / all_owners: a command never decides on its own
    to mix two shippers' data.
    """
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None
    params: dict = {"_json": False}
    positional = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--json":
            params["_json"] = True
        elif token == "--all-owners":
            params["all_owners"] = True
        elif token in ("--owner", "--service-level", "--review-days", "--min-shipments"):
            if index + 1 >= len(tokens):
                return None
            value = tokens[index + 1]
            key = {"--owner": "owner_id", "--service-level": "service_level",
                   "--review-days": "review_period_days", "--min-shipments": "min_shipments"}[token]
            params[key] = value
            index += 1
        elif token.startswith("--"):
            return None
        else:
            positional.append(token)
        index += 1
    if len(positional) != 1:
        return None
    params["file_path"] = positional[0]
    return params


_LABELS = {
    "en": {
        "inventory_usage": "Usage: /inventory <skus.csv|json> [--owner SHIPPER | --all-owners] "
                           "[--service-level 0.95] [--review-days 7] [--json]",
        "carriers_usage": "Usage: /carriers <waybills.csv|json> [--owner SHIPPER | --all-owners] "
                          "[--min-shipments 20] [--json]",
        "reorder": "  Reorder {sku}: order {qty} · reorder point {rop} · safety stock {ss} · {cover} days of cover · {cls}",
        "dead": "  Dead stock {sku}: {on_hand:g} on hand",
        "slow": "  Slow-moving {sku}: {on_hand:g} on hand",
        "basis": "Basis: ",
        "sep": "; ",
        "too_few": "too few shipments",
        "on_time": "{rate:.0%} (lower bound {low:.0%})",
        "no_on_time": "no on-time data",
        "carrier": "  {lane} {rank} {carrier}: {n} shipments · on time {on_time} · {cost}",
        "saving": "  Save {amount:,.2f}: {lane}, move {src} → {dst} ({diff:.2f}/kg cheaper)",
        "check": "  Check {waybill} ({carrier}): {detail}",
    },
    "zh": {
        "inventory_usage": "用法: /inventory <skus.csv|json> [--owner 货主ID | --all-owners] "
                           "[--service-level 0.95] [--review-days 7] [--json]",
        "carriers_usage": "用法: /carriers <waybills.csv|json> [--owner 货主ID | --all-owners] "
                          "[--min-shipments 20] [--json]",
        "reorder": "  补货 {sku}: 建议 {qty} 件 · 补货点 {rop} · 安全库存 {ss} · 可用 {cover} 天 · {cls}",
        "dead": "  呆滞 {sku}: 在库 {on_hand:g}",
        "slow": "  慢动 {sku}: 在库 {on_hand:g}",
        "basis": "依据: ",
        "sep": "；",
        "too_few": "样本不足",
        "on_time": "{rate:.0%}（下界 {low:.0%}）",
        "no_on_time": "无准时记录",
        "carrier": "  {lane} {rank} {carrier}: {n} 单 · 准时 {on_time} · {cost}",
        "saving": "  可节省 {amount:,.2f}: {lane} 由 {src} 转 {dst}（每 kg 低 {diff:.2f}）",
        "check": "  核实 {waybill} ({carrier}): {detail}",
    },
}


def _labels(self) -> dict:
    """Labels in the UI language. The tools' summaries and formulas stay English."""
    config = getattr(getattr(self, "terminal", None), "config", None) or {}
    lang = str(config.get("ui_lang", "en") or "en").lower()
    return _LABELS["zh" if lang.startswith("zh") else "en"]


def _abc_xyz(item: dict) -> str:
    # ABC needs a unit cost; without one it is unknown, which a bare "-X" hid.
    return f"ABC/XYZ {item['abc'] or '–'}/{item['xyz'] or '–'}"


def _emit(self, text: str, style: str = "") -> None:
    if self.context.has_rich and style:
        self.context.console.print(f"[{style}]{text}[/{style}]")
    elif self.context.has_rich:
        self.context.console.print(text)
    else:
        print(text)


class LogisticsCommandsMixin:
    """3PL inventory and carrier analyses, run directly on a CSV/JSON file.

    These are direct commands rather than only model tools because the
    logistics pack, by design, activates only on verifiable identifiers (a
    waybill or container number with a valid check digit). "Work out my
    reorder points" carries neither, so the model would not be offered the
    tool; a 3PL operator needs a way in that does not depend on that.
    """

    async def cmd_inventory(self, args: str):
        """Reorder points, safety stock, ABC/XYZ and dead stock for one shipper."""
        text = _labels(self)
        params = _parse_logistics_args(args, text["inventory_usage"])
        if params is None:
            _emit(self, text["inventory_usage"], "yellow")
            return
        from aria_code.tools.logistics_inventory import tool_plan_inventory_policy

        as_json = params.pop("_json")
        result = tool_plan_inventory_policy(params)
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if not result["success"]:
            _emit(self, result["error"], "red")
            return
        data = result["data"]
        if not data["scope"]["client_facing"]:
            _emit(self, data["scope"]["marker"], "bold red")
        _emit(self, result["summary"], "bold")
        for item in data["items"]:
            if item["action"] != "reorder":
                continue
            _emit(self, text["reorder"].format(
                sku=item["sku"], qty=item["suggested_order_qty"], rop=item["reorder_point"],
                ss=item["safety_stock"], cover=item["days_of_cover"], cls=_abc_xyz(item)))
        for item in data["items"]:
            if item["movement"] in ("dead", "slow"):
                _emit(self, text[item["movement"]].format(sku=item["sku"], on_hand=item["on_hand"]), "yellow")
        _emit(self, text["basis"] + text["sep"].join(data["assumptions"]), "dim")

    async def cmd_carriers(self, args: str):
        """Carrier scorecard by lane, cost anomalies and like-for-like savings."""
        text = _labels(self)
        params = _parse_logistics_args(args, text["carriers_usage"])
        if params is None:
            _emit(self, text["carriers_usage"], "yellow")
            return
        from aria_code.tools.logistics_carriers import tool_score_carriers

        as_json = params.pop("_json")
        result = tool_score_carriers(params)
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if not result["success"]:
            _emit(self, result["error"], "red")
            return
        data = result["data"]
        if not data["scope"]["client_facing"]:
            _emit(self, data["scope"]["marker"], "bold red")
        _emit(self, result["summary"], "bold")
        for entry in data["scorecard"]:
            rank = f"#{entry['rank_in_lane']}" if entry["rank_in_lane"] else text["too_few"]
            on_time = (text["on_time"].format(rate=entry["on_time_rate"], low=entry["on_time_lower_bound"])
                       if entry["on_time_rate"] is not None else text["no_on_time"])
            cost = f"{entry['median_cost_per_kg']:.2f}/kg" if entry["median_cost_per_kg"] is not None else "-"
            _emit(self, text["carrier"].format(lane=entry["lane"], rank=rank, carrier=entry["carrier"],
                                               n=entry["shipments"], on_time=on_time, cost=cost))
        for saving in data["savings"]:
            _emit(self, text["saving"].format(amount=saving["estimated_saving"], lane=saving["lane"],
                                              src=saving["from_carrier"], dst=saving["to_carrier"],
                                              diff=saving["rate_difference_per_kg"]), "green")
        for anomaly in data["anomalies"]:
            _emit(self, text["check"].format(waybill=anomaly["waybill_no"], carrier=anomaly["carrier"],
                                             detail=anomaly["detail"]), "yellow")
        _emit(self, text["basis"] + text["sep"].join(data["assumptions"]), "dim")
