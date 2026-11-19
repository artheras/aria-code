"""Warehouse ERP command stubs; globals are rebound by the legacy CLI bridge."""

from __future__ import annotations

import re


def _inventory_headline(result: dict, zh: bool) -> str:
    """The first line of /inventory, in the UI language."""
    if not zh:
        return result["summary"]
    data, counts = result["data"], result["data"].get("counts", {})
    return (
        f"分析 {len(data['items'])} 个 SKU（{result.get('source', '')}）："
        f"{counts.get('reorder', 0)} 个需补货，{counts.get('insufficient_history', 0)} 个历史数据不足，"
        f"{counts.get('no_demand', 0)} 个无需求；呆滞 {data.get('dead_stock', 0)} 个，慢动 {data.get('slow_stock', 0)} 个。"
    )


def _carriers_headline(result: dict, zh: bool) -> str:
    """The first line of /carriers, in the UI language."""
    if not zh:
        return result["summary"]
    data = result["data"]
    waybills = re.match(r"(\d+)", result.get("summary", ""))
    lanes = {c["lane"] for c in data.get("scorecard", [])}
    carriers = {c["carrier"] for c in data.get("scorecard", [])}
    return (
        f"{waybills.group(1) if waybills else '?'} 张运单，{len(lanes)} 条线路、{len(carriers)} 家承运商"
        f"（{result.get('source', '')}）；{len(data.get('anomalies', []))} 个异常待核实；"
        f"{len(data.get('savings', []))} 个节省机会，预计共 {data.get('estimated_total_saving', 0):,.2f}。"
    )


def _emit(self, text: str, style: str = "") -> None:
    if not self.context.has_rich:
        print(text)
        return
    from aria_code.ui.render.output import print_hanging

    body = text.lstrip(" ")
    print_hanging(self.context.console, text[: len(text) - len(body)], body, style)


class LogisticsCommandsMixin:
    """3PL inventory and carrier command stubs (moved to erp-service MCP)."""

    async def cmd_inventory(self, args: str):
        """3PL inventory policy moved to erp-service MCP."""
        _emit(self, "/inventory 已迁移至 erp-service MCP。", "dim")

    async def cmd_carriers(self, args: str):
        """3PL carrier scorecard moved to erp-service MCP."""
        _emit(self, "/carriers 已迁移至 erp-service MCP。", "dim")
