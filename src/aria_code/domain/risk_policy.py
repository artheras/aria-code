"""Unified Risk Policy Schema and hierarchical resolution.

Reconciles account, strategy, and execution constraints under a single
strictest-wins rule so position limits (e.g. 50% / 40% / 15% / 10%) cannot
silently drift across modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RiskPolicy:
    """Canonical risk constraints at one governance layer."""

    name: str = "default"
    max_position_weight: float = 0.50       # upper bound per single position (0..1)
    max_leverage: float = 1.0               # gross leverage ceiling
    max_drawdown_pct: float = 0.20          # circuit-breaker drawdown (0..1)
    approval_required_above_weight: float = 0.15  # human approval threshold
    min_cash_reserve_pct: float = 0.05      # cash buffer floor (0..1)
    allow_short: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def resolve_risk_policy(*policies: Optional[RiskPolicy], name: str = "effective") -> RiskPolicy:
    """Merge layered policies using strictest-wins semantics."""
    active = [p for p in policies if p is not None]
    if not active:
        return RiskPolicy(name=name)
    return RiskPolicy(
        name=name,
        max_position_weight=min(p.max_position_weight for p in active),
        max_leverage=min(p.max_leverage for p in active),
        max_drawdown_pct=min(p.max_drawdown_pct for p in active),
        approval_required_above_weight=min(p.approval_required_above_weight for p in active),
        min_cash_reserve_pct=max(p.min_cash_reserve_pct for p in active),
        allow_short=all(p.allow_short for p in active),
    )


@dataclass
class RiskCheckOutcome:
    allowed: bool
    requires_approval: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    effective_policy: Dict[str, Any] = field(default_factory=dict)


def check_order_compliance(
    policy: RiskPolicy,
    *,
    target_weight: float,
    gross_leverage: float = 1.0,
    current_drawdown_pct: float = 0.0,
    cash_reserve_pct: float = 1.0,
    is_short: bool = False,
) -> RiskCheckOutcome:
    """Evaluate a proposed position/order against the resolved RiskPolicy."""
    violations: List[str] = []
    warnings: List[str] = []

    if is_short and not policy.allow_short:
        violations.append("Short selling is disabled by effective risk policy.")
    if target_weight > policy.max_position_weight:
        violations.append(
            f"Target weight {target_weight:.2%} exceeds max_position_weight {policy.max_position_weight:.2%}."
        )
    if gross_leverage > policy.max_leverage:
        violations.append(
            f"Gross leverage {gross_leverage:.2f}x exceeds max_leverage {policy.max_leverage:.2f}x."
        )
    if current_drawdown_pct >= policy.max_drawdown_pct:
        violations.append(
            f"Current drawdown {current_drawdown_pct:.2%} reached circuit-breaker {policy.max_drawdown_pct:.2%}."
        )
    if cash_reserve_pct < policy.min_cash_reserve_pct:
        violations.append(
            f"Cash reserve {cash_reserve_pct:.2%} is below min_cash_reserve_pct {policy.min_cash_reserve_pct:.2%}."
        )

    requires_approval = (not violations) and (target_weight > policy.approval_required_above_weight)
    if requires_approval:
        warnings.append(
            f"Target weight {target_weight:.2%} exceeds approval threshold {policy.approval_required_above_weight:.2%}."
        )

    return RiskCheckOutcome(
        allowed=not violations,
        requires_approval=requires_approval,
        violations=violations,
        warnings=warnings,
        effective_policy=policy.to_dict(),
    )
