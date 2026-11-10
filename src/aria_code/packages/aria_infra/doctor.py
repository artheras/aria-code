"""Health checks for Aria Code package bridges."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List

from aria_code.packages.aria_infra.arthera import ArtheraPackageMap


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class PackageDoctorReport:
    status: str
    checks: List[CheckResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


def _overall_status(checks: List[CheckResult]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


def _status_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def build_package_doctor_report(
    *,
    arthera: ArtheraPackageMap,
    mcp_status: Dict[str, Any],
    tool_count: int,
    manifest_can_export: bool,
    manifest_path: Path,
    services: Iterable[Any] = (),
    required_services: Iterable[str] = (),
    architecture_layers: Iterable[Any] = (),
    required_architecture_layers: Iterable[str] = (),
    provider_health: Iterable[Dict[str, Any]] = (),
) -> PackageDoctorReport:
    """Build package bridge health checks without doing I/O."""

    checks: List[CheckResult] = []
    checks.append(CheckResult(
        "product_identity",
        "ok",
        "Aria Code is registered as an Arthera product.",
    ))

    service_names = {
        getattr(service, "name", "")
        for service in services
        if getattr(service, "name", "")
    }
    missing_required = sorted(set(required_services) - service_names)
    if service_names and not missing_required:
        checks.append(CheckResult(
            "service_boundaries",
            "ok",
            f"{len(service_names)} service manifests registered.",
        ))
    elif missing_required:
        checks.append(CheckResult(
            "service_boundaries",
            "fail",
            f"Missing required services: {', '.join(missing_required)}.",
            "Register service specs in packages.aria_services.",
        ))
    else:
        checks.append(CheckResult(
            "service_boundaries",
            "warn",
            "No service manifests registered yet.",
            "Add Gateway/Data/Reports/Brokers service specs.",
        ))

    architecture_rows = list(architecture_layers or [])
    required_layer_names = set(required_architecture_layers or [])
    if architecture_rows or required_layer_names:
        layer_names = {
            getattr(layer, "name", "")
            for layer in architecture_rows
            if getattr(layer, "name", "")
        }
        missing_layers = sorted(required_layer_names - layer_names)
        if missing_layers:
            checks.append(CheckResult(
                "architecture_layers",
                "fail",
                f"Missing architecture layers: {', '.join(missing_layers)}.",
                "Update packages.aria_core.architecture.",
            ))
        else:
            incomplete = [
                getattr(layer, "name", "")
                for layer in architecture_rows
                if _status_value(getattr(layer, "status", "")).lower() != "done"
            ]
            if incomplete:
                checks.append(CheckResult(
                    "architecture_layers",
                    "warn",
                    f"{len(layer_names)} layers registered; incomplete: {', '.join(incomplete[:6])}.",
                    "Prioritize context, settings, runtime, safety, and channel extraction.",
                ))
            else:
                checks.append(CheckResult(
                    "architecture_layers",
                    "ok",
                    f"{len(layer_names)} architecture layers complete.",
                ))

    provider_rows = list(provider_health or [])
    if not provider_rows:
        checks.append(CheckResult(
            "data_provider_health",
            "warn",
            "No provider calls recorded in this session.",
            "Run /quote, /ta, /analyze, or /report first.",
        ))
    else:
        unhealthy = [
            row for row in provider_rows
            if row.get("status") not in ("ok", "", None)
        ]
        if unhealthy:
            checks.append(CheckResult(
                "data_provider_health",
                "warn",
                "; ".join(
                    f"{row.get('provider')}={row.get('status')}"
                    for row in unhealthy[:5]
                ),
                "Wait for cooldown, retry later, or switch provider/API key.",
            ))
        else:
            checks.append(CheckResult(
                "data_provider_health",
                "ok",
                f"{len(provider_rows)} providers healthy.",
            ))

    if arthera.available:
        checks.append(CheckResult(
            "arthera_packages",
            "ok",
            f"Found {len(arthera.packages)} package groups at {arthera.root}.",
        ))
    else:
        checks.append(CheckResult(
            "arthera_packages",
            "warn",
            f"Not found at {arthera.root}." if arthera.root else "Optional private backend is not configured.",
            "Use /finance setup for the standalone financial-service; set ARTHERA_ROOT only for an explicit legacy backend bridge.",
        ))

    if mcp_status.get("configured"):
        checks.append(CheckResult(
            "mcp_config",
            "ok",
            f"Configured in {mcp_status.get('config_path')}.",
        ))
    else:
        checks.append(CheckResult(
            "mcp_config",
            "warn",
            f"No arthera_quant_engine server in {mcp_status.get('config_path')}.",
            "Run /packages connect arthera.",
        ))

    if mcp_status.get("server_file_exists"):
        checks.append(CheckResult(
            "mcp_server_file",
            "ok",
            str(mcp_status.get("server_path") or ""),
        ))
    else:
        checks.append(CheckResult(
            "mcp_server_file",
            "warn",
            str(mcp_status.get("server_path") or "server path unavailable"),
            "Verify <ARTHERA_ROOT>/packages/quant_engine/mcp_server.py exists (set ARTHERA_ROOT env var).",
        ))

    if mcp_status.get("running"):
        checks.append(CheckResult(
            "mcp_runtime",
            "ok",
            f"Running with {mcp_status.get('tool_count', 0)} tools.",
        ))
    else:
        checks.append(CheckResult(
            "mcp_runtime",
            "warn",
            "Arthera MCP server is not running.",
            "Run /mcp reload or /packages connect arthera --reload.",
        ))

    if tool_count > 0:
        checks.append(CheckResult(
            "mcp_tool_manifests",
            "ok",
            f"{tool_count} Arthera MCP tools mapped to Aria manifests.",
        ))
    else:
        checks.append(CheckResult(
            "mcp_tool_manifests",
            "warn",
            "No Arthera MCP tools mapped yet.",
            "Start the MCP server, then run /packages tools arthera.",
        ))

    checks.append(CheckResult(
        "manifest_export",
        "ok" if manifest_can_export else "fail",
        str(manifest_path),
        "" if manifest_can_export else "Check artifact directory permissions.",
    ))

    return PackageDoctorReport(status=_overall_status(checks), checks=checks)
