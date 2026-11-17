"""Agent manifests built from the existing AgentRegistry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Type

from aria_code.agents.base import BaseAgent
from aria_code.agents.registry import get_registry
from aria_code.packages.aria_core import CapabilityManifest, PermissionLevel, ServiceKind


_CAPABILITIES_BY_AGENT = {
    "technical": ["market.history", "market.technical"],
    "fundamental": ["market.fundamentals", "filings"],
    "macro": ["macro.data", "news"],
    "risk": ["market.history", "portfolio.risk", "compliance.check"],
    "news": ["news"],
    "catalyst": ["news", "events"],
    "sector": ["market.sector"],
    "earnings": ["filings", "events"],
    "portfolio": ["portfolio"],
    "synthesis": ["reasoning.synthesis"],
    "debate": ["reasoning.debate"],
    "coder": ["code.write", "code.edit", "file.io"],
    "tester": ["code.test", "code.compile", "command.execute"],
    "debugger": ["code.debug", "code.self_heal", "traceback.analyze"],
    "strategist": ["quant.strategy", "factor.selection", "portfolio.allocation"],
}


@dataclass(frozen=True)
class AgentManifest:
    name: str
    description: str
    builtin: bool
    capabilities: List[str] = field(default_factory=list)
    permissions: List[PermissionLevel] = field(default_factory=lambda: [PermissionLevel.NETWORK])
    output_schema: Dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "required": ["agent", "symbol", "analysis", "confidence", "signal"],
    })

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name,
            kind=ServiceKind.AGENT,
            description=self.description,
            capabilities=self.capabilities,
            permissions=self.permissions,
            output_schema=self.output_schema,
            tags=["agent"],
        )


def manifest_from_agent_class(cls: Type[BaseAgent], *, builtin: bool = True) -> AgentManifest:
    name = getattr(cls, "name", cls.__name__).lower()
    return AgentManifest(
        name=name,
        description=getattr(cls, "description", ""),
        builtin=builtin,
        capabilities=_CAPABILITIES_BY_AGENT.get(name, ["agent"]),
    )


def list_agent_manifests() -> List[AgentManifest]:
    registry = get_registry()
    out: List[AgentManifest] = []
    for item in registry.list():
        cls = registry.get(item["name"])
        if cls:
            out.append(manifest_from_agent_class(cls, builtin=bool(item.get("builtin"))))
    return sorted(out, key=lambda item: item.name)
