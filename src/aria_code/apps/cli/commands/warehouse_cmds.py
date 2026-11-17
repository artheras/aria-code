"""Warehouse ERP command; globals are rebound by the legacy CLI bridge."""

from __future__ import annotations

import json

from aria_code.apps.cli.i18n import ui_text


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


def _inventory_headline(result: dict, zh: bool) -> str:
    """The first line of /inventory, in the UI language.

    The tool's summary is English; in a Chinese session the detail lines under
    it were Chinese and the headline above them was not. The formulas in the
    basis line stay as the tool writes them.
    """
    if not zh:
        return result["summary"]
    data, counts = result["data"], result["data"].get("counts", {})
    return (f"分析 {len(data['items'])} 个 SKU（{result.get('source', '')}）："
            f"{counts.get('reorder', 0)} 个需补货，{counts.get('insufficient_history', 0)} 个历史数据不足，"
            f"{counts.get('no_demand', 0)} 个无需求；呆滞 {data.get('dead_stock', 0)} 个，慢动 {data.get('slow_stock', 0)} 个。")


def _carriers_headline(result: dict, zh: bool) -> str:
    """The first line of /carriers, in the UI language (see _inventory_headline)."""
    if not zh:
        return result["summary"]
    import re

    data = result["data"]
    waybills = re.match(r"(\d+)", result.get("summary", ""))
    lanes = {c["lane"] for c in data.get("scorecard", [])}
    carriers = {c["carrier"] for c in data.get("scorecard", [])}
    return (f"{waybills.group(1) if waybills else '?'} 张运单，{len(lanes)} 条线路、{len(carriers)} 家承运商"
            f"（{result.get('source', '')}）；{len(data.get('anomalies', []))} 个异常待核实；"
            f"{len(data.get('savings', []))} 个节省机会，预计共 {data.get('estimated_total_saving', 0):,.2f}。")


def _labels(self) -> dict:
    """Labels in the UI language. The tools' formulas stay as they write them."""
    config = getattr(getattr(self, "terminal", None), "config", None) or {}
    lang = str(config.get("ui_lang", "en") or "en").lower()
    return _LABELS["zh" if lang.startswith("zh") else "en"]


def _abc_xyz(item: dict) -> str:
    # ABC needs a unit cost; without one it is unknown, which a bare "-X" hid.
    return f"ABC/XYZ {item['abc'] or '–'}/{item['xyz'] or '–'}"


def _emit(self, text: str, style: str = "") -> None:
    """One line of a report, wrapped under its own indent.

    The indent was typed as leading spaces, so in an 80-column terminal a
    wrapped line went back to column 0 ("cover · ABC/XYZ –/X" under the
    margin). It is plain text, not markup: a SKU or file name may hold "[".
    """
    if not self.context.has_rich:
        print(text)
        return
    from aria_code.ui.render.output import print_hanging

    body = text.lstrip(" ")
    print_hanging(self.context.console, text[: len(text) - len(body)], body, style)


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
        from aria_bots.erp_logistics import tool_plan_inventory_policy

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
        _emit(self, _inventory_headline(result, text is _LABELS["zh"]), "bold")
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
        from aria_bots.erp_logistics import tool_score_carriers

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
        _emit(self, _carriers_headline(result, text is _LABELS["zh"]), "bold")
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
