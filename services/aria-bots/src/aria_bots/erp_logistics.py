"""Logistics calls for the bots. The calculations live in erp-service.

This module keeps two host rules the chat needs and erp-service's own MCP
server does not:

- ``file_path`` is readable only inside the configured data folder. With no
  folder, a chat cannot name a file on the host.
- Tool names stay ``analyze_logistics_data``, ``score_carriers`` and
  ``plan_inventory_policy``. Shipper isolation (``ARIA_OWNER_SCOPE``) is
  enforced inside erp-service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TextIO

OWNER_SCOPE_ENV = "ARIA_OWNER_SCOPE"
PROTOCOL_VERSION = "2024-11-05"
DATA_DIR_ENV = "ARIA_LOGISTICS_DATA_DIR"
_MISSING = "Logistics calculations moved to erp-service. Install erp-service and put it on PYTHONPATH."

_FALLBACK_SCHEMAS = (
    {
        "name": "analyze_logistics_data",
        "description": "Audit freight waybills. 运费审计由 erp-service 计算。",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
            "waybills": {"type": "array"},
        }},
    },
    {
        "name": "score_carriers",
        "description": "Score carriers by lane. 承运商评分由 erp-service 计算。",
        "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
    },
    {
        "name": "plan_inventory_policy",
        "description": "Plan inventory reorder points. 库存策略由 erp-service 计算。",
        "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
    },
)


def _implementations() -> Dict[str, tuple[Callable[[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]]:
    try:
        from erp_service.logistics.carriers import SCHEMA as carrier_schema
        from erp_service.logistics.carriers import tool_score_carriers as ext_score_carriers
        from erp_service.logistics.inventory import SCHEMA as inventory_schema
        from erp_service.logistics.inventory import tool_plan_inventory_policy as ext_plan_inventory
        from erp_service.logistics.tenancy import owner_params_schema

        freight_schema = {
            "name": "analyze_logistics_data",
            "description": "Analyze actual waybills from supplied records, a CSV/JSON file, or an existing local database",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "CSV or JSON waybill file path"},
                    "waybills": {"type": "array", "description": "Waybill records with total_cost"},
                    **owner_params_schema(),
                },
            },
        }
        return {
            "analyze_logistics_data": (tool_analyze_logistics_data, freight_schema),
            "score_carriers": (ext_score_carriers, carrier_schema),
            "plan_inventory_policy": (ext_plan_inventory, inventory_schema),
        }
    except ImportError:
        from . import logistics_builtin as builtin

        freight_schema = {
            "name": "analyze_logistics_data",
            "description": "Analyze actual waybills from supplied records or a CSV/JSON/XLSX file, scoped per shipper",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "CSV, JSON, or XLSX waybill file path"},
                    "waybills": {"type": "array", "description": "Waybill records with total_cost"},
                    **builtin.owner_params_schema(),
                },
            },
        }
        return {
            "analyze_logistics_data": (builtin.tool_analyze_logistics_data_builtin, freight_schema),
            "score_carriers": (builtin.tool_score_carriers, builtin.CARRIER_SCHEMA),
            "plan_inventory_policy": (builtin.tool_plan_inventory_policy, builtin.INVENTORY_SCHEMA),
            "reconcile_ar_aging": (
                builtin.tool_reconcile_ar_aging,
                {
                    "name": "reconcile_ar_aging",
                    "description": "Compute accounts-receivable aging buckets (current, 1-30, 31-60, 61-90, 90+) as of cutoff_date.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cutoff_date": {"type": "string", "description": "Cutoff date YYYY-MM-DD"},
                            "invoices_path": {"type": "string"},
                            "payments_path": {"type": "string"},
                        },
                        "required": ["cutoff_date"],
                    },
                },
            ),
            "reconcile_cycle_count": (
                builtin.tool_reconcile_cycle_count,
                {
                    "name": "reconcile_cycle_count",
                    "description": "Reconcile warehouse cycle counts with UOM conversions against system stock.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "system_path": {"type": "string"},
                            "counts_path": {"type": "string"},
                            "uom_path": {"type": "string"},
                        },
                    },
                },
            ),
            "convert_fx_invoices": (
                builtin.tool_convert_fx_invoices,
                {
                    "name": "convert_fx_invoices",
                    "description": "Convert foreign-currency invoices to EUR using latest rate on or before invoice date.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "invoices_path": {"type": "string"},
                            "rates_path": {"type": "string"},
                        },
                    },
                },
            ),
            "calculate_customs_duty": (
                builtin.tool_calculate_customs_duty,
                {
                    "name": "calculate_customs_duty",
                    "description": "Calculate EU customs duty per shipment with EUR 150 de minimis threshold and longest HS prefix match.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lines_path": {"type": "string"},
                            "duty_rates_path": {"type": "string"},
                            "fx_path": {"type": "string"},
                        },
                    },
                },
            ),
        }


def tool_plan_inventory_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    return _implementations()["plan_inventory_policy"][0](params)


def tool_score_carriers(params: Dict[str, Any]) -> Dict[str, Any]:
    return _implementations()["score_carriers"][0](params)


def tool_analyze_logistics_data(params: Dict[str, Any]) -> Dict[str, Any]:
    """erp-service freight math, after this host's shipper check (with built-in fallback)."""
    try:
        from erp_service.freight import tool_analyze_logistics_data as impl
        from erp_service.logistics.records import load_records
        from erp_service.logistics.tenancy import TenancyError, scope_records
    except ImportError:
        from .logistics_builtin import tool_analyze_logistics_data_builtin
        return tool_analyze_logistics_data_builtin(params)

    try:
        records, source = load_records(params, "waybills")
        records, scope = scope_records(
            records,
            params.get("owner_id"),
            all_owners=bool(params.get("all_owners")),
        )
    except (TenancyError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": str(exc)}
    result = impl({"waybills": records})
    if not result.get("success"):
        return result
    data = result.setdefault("data", {})
    data["data_source"] = source
    data["scope"] = scope
    file_path = params.get("file_path")
    result["source"] = str(file_path) if file_path else source
    rate = data.get("overall_on_time_rate")
    rate_label = f"{rate}%" if rate is not None else "不可得（无准时状态记录）"
    anomalies = data.get("billing_anomalies") or []
    spend = data.get("total_freight_spend") or 0
    result["summary"] = (
        f"已审计 {data.get('total_waybills', len(records))} 单运单（来源：{source}），"
        f"运费总计 ¥{spend:,.2f}，已知准时交付率 {rate_label}，"
        f"发现 {len(anomalies)} 笔需核实的计费重量异常。"
    )
    return result


def register_logistics_tools(tools_dict: Dict[str, Any], schemas_list: list) -> int:
    """Register the three erp-service analyses under their existing tool names."""
    tools = _implementations()
    for name, (fn, schema) in tools.items():
        tools_dict[name] = (fn, schema.get("description", name))
        schemas_list.append(schema)
    return len(tools)


def tool_list() -> list[Dict[str, Any]]:
    try:
        schemas = [schema for _, schema in _implementations().values()]
    except ImportError:
        schemas = list(_FALLBACK_SCHEMAS)
    return [
        {
            "name": schema["name"],
            "description": schema["description"],
            "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        }
        for schema in schemas
    ]


def confine(arguments: Dict[str, Any], data_dir: Optional[Path]) -> Dict[str, Any]:
    """Resolve file_path inside the data folder, or refuse it."""
    file_path = arguments.get("file_path")
    if not file_path:
        return arguments
    if data_dir is None:
        raise ValueError(f"Files are not available here; set {DATA_DIR_ENV} or pass records inline")
    root = data_dir.resolve()
    candidate = Path(str(file_path)).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if path != root and root not in path.parents:
        raise ValueError("file_path must be inside the logistics data folder")
    return {**arguments, "file_path": str(path)}


def call_tool(name: str, arguments: Any, data_dir: Optional[Path]) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        arguments = confine(arguments, data_dir)
    except ValueError as exc:
        result = {"success": False, "error": str(exc)}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": True}
    try:
        tools = _implementations()
    except ImportError:
        return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": _MISSING})}], "isError": True}
    entry = tools.get(name)
    if entry is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    try:
        result = entry[0](arguments)
    except ValueError as exc:
        result = {"success": False, "error": str(exc)}
    except Exception as exc:  # a tool bug must not take the server down
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    failed = isinstance(result, dict) and result.get("success") is False
    text = json.dumps(result, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def handle(message: Dict[str, Any], data_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    if "id" not in message:
        return None
    reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
    params = message.get("params") or {}
    if method == "initialize":
        scope = os.environ.get(OWNER_SCOPE_ENV, "").strip()
        reply["result"] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aria-logistics", "version": "0.1.0"},
            "instructions": "Logistics analyses scoped to one shipper. Calculations run in erp-service."
            if scope else "Logistics analyses run in erp-service; data spanning shippers needs owner_id or all_owners.",
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": tool_list()}
    elif method == "tools/call":
        reply["result"] = call_tool(str(params.get("name", "")), params.get("arguments"), data_dir)
    else:
        reply["error"] = {"code": -32601, "message": f"Method not found: {method}"}
    return reply


def serve(stdin: TextIO, stdout: TextIO, data_dir: Optional[Path]) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            reply = handle(message, data_dir) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="aria-logistics-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get(DATA_DIR_ENV) or None,
                        help=f"folder file_path is read from (default ${DATA_DIR_ENV}; none: inline records only)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    serve(sys.stdin, sys.stdout, data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
