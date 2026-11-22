"""Skill manifests for model-facing workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from aria_code.packages.aria_core import CapabilityManifest, PermissionLevel, ServiceKind


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    tools: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    permissions: List[PermissionLevel] = field(default_factory=list)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name,
            kind=ServiceKind.SKILL,
            description=self.description,
            capabilities=[f"tool:{name}" for name in self.tools] + [f"agent:{name}" for name in self.agents],
            permissions=self.permissions,
            required_services=self.tools + self.agents,
            tags=["skill"],
        )


def builtin_skill_specs() -> List[SkillSpec]:
    return [
        SkillSpec(
            name="financial-research",
            description="Multi-source quote, fundamentals, technical analysis, and narrative report generation.",
            tools=["get_market_data", "get_market_history", "get_risk_metrics", "analyze_news"],
            agents=["technical", "fundamental", "macro", "risk", "synthesis"],
            permissions=[PermissionLevel.NETWORK],
        ),
        SkillSpec(
            name="equity-research-report",
            description=(
                "Evidence-backed equity report workflow with specialist agents, "
                "deterministic fallbacks, provenance, critique, and completion gates."
            ),
            tools=[
                "get_market_data",
                "get_market_history",
                "calculate_factors",
                "get_risk_metrics",
                "analyze_news",
            ],
            agents=[
                "technical",
                "fundamental",
                "macro",
                "risk",
                "news",
                "catalyst",
                "sector",
                "synthesis",
            ],
            permissions=[PermissionLevel.NETWORK, PermissionLevel.WORKSPACE_WRITE],
        ),
        SkillSpec(
            name="strategy-backtest",
            description="Generate and validate trading strategies with historical simulation and artifacts.",
            tools=["get_market_data"],
            agents=["technical", "risk"],
            permissions=[PermissionLevel.NETWORK, PermissionLevel.WORKSPACE_WRITE],
        ),
        SkillSpec(
            name="workspace-coding",
            description="Read, edit, and verify local project files with explicit workspace permissions.",
            tools=["read_file", "list_files", "search_code", "edit_file", "write_file", "run_command"],
            permissions=[PermissionLevel.READ_ONLY, PermissionLevel.WORKSPACE_WRITE],
        ),
        SkillSpec(
            name="broker-portfolio",
            description="Read broker account state and propose guarded orders with user approval.",
            tools=["broker_query", "broker_order"],
            permissions=[PermissionLevel.BROKER_READ, PermissionLevel.BROKER_TRADE],
        ),
        SkillSpec(
            name="financial-orchestration",
            description=(
                "Orchestrate artheras/financial-service MCP tools for symbol resolution, "
                "quotes, fundamentals, technicals, correlation matrices, stat-arb, and portfolio backtests."
            ),
            tools=[
                "arthera-finance/financial_resolve_symbol",
                "arthera-finance/financial_market_quote",
                "arthera-finance/financial_market_history",
                "arthera-finance/financial_market_fundamentals",
                "arthera-finance/financial_technical_indicators",
                "arthera-finance/financial_correlation_matrix",
                "arthera-finance/financial_stat_arb",
                "arthera-finance/financial_portfolio_backtest",
                "arthera-finance/financial_ohlcv_quality_audit",
                "arthera-finance/financial_strategy_backtest",
            ],
            permissions=[PermissionLevel.READ_ONLY],
        ),
        SkillSpec(
            name="erp-orchestration",
            description=(
                "Orchestrate artheras/erp-service MCP tools for 3PL warehouse analytics, "
                "inventory replenishment planning, carrier scorecards, and freight invoice audits."
            ),
            tools=[
                "erp-service/erp.warehouse.analyze",
                "erp-service/erp.warehouse.snapshot",
                "erp-service/erp.inventory.plan",
                "erp-service/erp.carriers.score",
                "erp-service/erp.freight.audit",
                "erp-service/erp.warehouse.task",
            ],
            permissions=[PermissionLevel.READ_ONLY],
        ),
        SkillSpec(
            name="healthcare-orchestration",
            description=(
                "Orchestrate artheras/healthcare-service MCP tools for biomedical literature, "
                "clinical trial evidence, and genomics lookups."
            ),
            tools=["aria-healthcare/search_literature", "aria-healthcare/clinical_trials"],
            permissions=[PermissionLevel.READ_ONLY],
        ),
    ]
