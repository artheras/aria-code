"""Trading service layer for paper/live execution.

All order execution flows through a preview id. Live trading is denied unless
the broker config explicitly enables ``allow_live_trade``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .config import BROKERS_CONFIG_PATH
from .planning import RiskRuleSet, StrategyIntent, plan_order, snapshot_from_broker


TRADE_PREVIEWS_PATH = BROKERS_CONFIG_PATH.parent / "trade_previews.json"
TRADE_AUDIT_PATH = BROKERS_CONFIG_PATH.parent / "trade_audit.jsonl"

_DRY_RUN_VALUES = {"1", "true", "yes", "on"}


def global_dry_run() -> bool:
    """Global trading kill-switch.

    When ``ARIA_DRY_RUN`` is truthy, ALL brokers are forced read-only regardless
    of their per-broker config — order previews are never executable. A single
    operational switch for demos, testing, or a risk-off freeze:
        export ARIA_DRY_RUN=1
    """
    return str(os.getenv("ARIA_DRY_RUN", "")).strip().lower() in _DRY_RUN_VALUES


@dataclass(frozen=True)
class TradingPolicy:
    mode: str = "read_only"  # read_only | paper | live
    allow_live_trade: bool = False
    require_confirm: bool = True
    max_single_position_weight: float = 0.20
    min_cash_reserve_weight: float = 0.02
    max_order_value_weight: float = 0.10
    allow_short: bool = False
    allow_fractional: bool = False
    max_drawdown_pct: float = 0.25
    max_daily_loss_pct: float = 0.05
    approval_required_above_weight: float = 0.15

    def rules(self) -> RiskRuleSet:
        return RiskRuleSet(
            max_single_position_weight=self.max_single_position_weight,
            min_cash_reserve_weight=self.min_cash_reserve_weight,
            max_order_value_weight=self.max_order_value_weight,
            allow_short=self.allow_short,
            allow_fractional=self.allow_fractional,
        )

    def to_risk_policy(self):
        from aria_code.domain.risk_policy import RiskPolicy

        return RiskPolicy(
            name=f"broker:{self.mode}",
            max_position_weight=self.max_single_position_weight,
            max_drawdown_pct=self.max_drawdown_pct,
            min_cash_reserve_pct=self.min_cash_reserve_weight,
            allow_short=self.allow_short,
            approval_required_above_weight=self.approval_required_above_weight,
        )


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: Optional[float] = None
    order_type: str = "limit"
    price: Optional[float] = None
    source: str = "manual"
    target_weight: Optional[float] = None
    user_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def resolve_trading_mode(config: Dict[str, Any], broker_type: str = "") -> str:
    if global_dry_run():
        return "read_only"
    explicit = str(config.get("mode", "") or "").lower()
    if explicit in {"read_only", "paper", "live"}:
        return explicit
    if broker_type == "paper" or config.get("paper") is True:
        return "paper"
    return "read_only"


def policy_from_config(config: Dict[str, Any], broker_type: str = "") -> TradingPolicy:
    mode = resolve_trading_mode(config, broker_type=broker_type)
    return TradingPolicy(
        mode=mode,
        allow_live_trade=bool(config.get("allow_live_trade", False)),
        require_confirm=bool(config.get("require_confirm", True)),
        max_single_position_weight=float(config.get("max_single_position_weight", 0.20) or 0.20),
        min_cash_reserve_weight=float(config.get("min_cash_reserve_weight", 0.02) or 0.02),
        max_order_value_weight=float(config.get("max_order_value_weight", 0.10) or 0.10),
        allow_short=bool(config.get("allow_short", False)),
        allow_fractional=bool(config.get("allow_fractional", False)),
        max_drawdown_pct=float(config.get("max_drawdown_pct", 0.25) or 0.25),
        max_daily_loss_pct=float(config.get("max_daily_loss_pct", 0.05) or 0.05),
        approval_required_above_weight=float(config.get("approval_required_above_weight", 0.15) or 0.15),
    )


def _load_previews() -> Dict[str, Any]:
    if not TRADE_PREVIEWS_PATH.exists():
        return {"previews": {}}
    try:
        data = json.loads(TRADE_PREVIEWS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("previews", {})
            return data
    except Exception:
        pass
    return {"previews": {}}


def _save_previews(data: Dict[str, Any]) -> None:
    TRADE_PREVIEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADE_PREVIEWS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit(event: Dict[str, Any]) -> None:
    TRADE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": int(time.time()), **event}
    with TRADE_AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _execution_blockers(policy: TradingPolicy, plan: Dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if global_dry_run():
        blockers.append("全局 dry-run 模式已启用 (ARIA_DRY_RUN)，所有下单被冻结")
    risk = plan.get("risk") or {}
    blockers.extend(str(item) for item in risk.get("violations") or [])
    if plan.get("action") in {"buy", "sell", "rebalance"} and not plan.get("estimated_order"):
        blockers.append("订单计划没有可执行订单，通常是缺少价格、数量或持仓")
    if policy.mode == "read_only":
        blockers.append("账户处于 read_only 模式，不能执行订单")
    if policy.mode == "live" and not policy.allow_live_trade:
        blockers.append("实盘账户未设置 allow_live_trade=true")
    return blockers


def build_order_preview(broker: Any, intent: OrderIntent) -> Dict[str, Any]:
    policy = policy_from_config(getattr(broker, "config", {}) or {}, getattr(broker, "broker_type", ""))
    snapshot = snapshot_from_broker(broker)
    strategy_intent = StrategyIntent(
        symbol=intent.symbol,
        action=intent.side,
        target_weight=intent.target_weight,
        reason=intent.user_message,
        source=intent.source,
        metadata=dict(intent.metadata),
    )
    planned = plan_order(
        snapshot,
        strategy_intent,
        price=float(intent.price) if intent.price is not None else None,
        quantity=float(intent.quantity) if intent.quantity is not None else None,
        order_type=intent.order_type,
        rules=policy.rules(),
    )
    from dataclasses import asdict
    from aria_code.domain.risk_policy import check_order_compliance

    plan_dict = planned.to_dict()
    preview_id = "tp_" + uuid.uuid4().hex[:12]
    meta = dict(intent.metadata or {})
    est = plan_dict.get("estimated_order") or {}
    projected_weight = float(
        intent.target_weight
        if intent.target_weight is not None
        else (est.get("projected_position_weight", 0.0) if isinstance(est, dict) else 0.0)
        or 0.0
    )
    compliance = check_order_compliance(
        policy.to_risk_policy(),
        target_weight=projected_weight,
        gross_leverage=float(meta.get("gross_leverage", 1.0) or 1.0),
        current_drawdown_pct=float(meta.get("current_drawdown_pct", 0.0) or 0.0),
        cash_reserve_pct=float(meta.get("cash_reserve_pct", 1.0) or 1.0),
        is_short=(intent.side.lower() == "short"),
    )
    blockers = _execution_blockers(policy, plan_dict)
    for violation in compliance.violations:
        if violation not in blockers:
            blockers.append(violation)
    preview = {
        "preview_id": preview_id,
        "created_at": int(time.time()),
        "status": "pending",
        "broker_id": getattr(broker, "broker_id", ""),
        "broker_label": getattr(broker, "label", ""),
        "broker_type": getattr(broker, "broker_type", ""),
        "mode": policy.mode,
        "allow_live_trade": policy.allow_live_trade,
        "require_confirm": policy.require_confirm,
        "intent": {
            "symbol": intent.symbol.upper(),
            "side": intent.side.lower(),
            "quantity": float(intent.quantity) if intent.quantity is not None else None,
            "order_type": intent.order_type.lower(),
            "price": intent.price,
            "source": intent.source,
        },
        "order_plan": plan_dict,
        "risk_compliance": asdict(compliance),
        "execution_blockers": blockers,
        "can_execute": not blockers,
        "audit_path": str(TRADE_AUDIT_PATH),
    }
    store = _load_previews()
    store.setdefault("previews", {})[preview_id] = preview
    _save_previews(store)
    _audit({"event": "trade_preview", "preview": preview})
    return preview


def load_order_preview(preview_id: str) -> Dict[str, Any] | None:
    return (_load_previews().get("previews") or {}).get(preview_id)


def list_order_previews(limit: int = 10) -> list[Dict[str, Any]]:
    rows = list((_load_previews().get("previews") or {}).values())
    rows.sort(key=lambda row: int(row.get("created_at", 0)), reverse=True)
    return rows[: max(0, int(limit or 10))]


def mark_preview_status(preview_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    store = _load_previews()
    preview = (store.get("previews") or {}).get(preview_id)
    if not preview:
        return
    preview["status"] = status
    preview["updated_at"] = int(time.time())
    if extra:
        preview.update(extra)
    _save_previews(store)


def execute_order_preview(
    broker: Any,
    preview_id: str,
    *,
    confirmed: bool = False,
    source: str = "terminal",
) -> Dict[str, Any]:
    """`source` is audit-trail metadata only, never a trust signal — it does
    not change what this function will or won't execute. It exists so the
    audit log can distinguish a human typing /trade confirm at the terminal
    keyboard ("terminal", the default) from the separate, explicitly-gated
    chat-confirm MCP path ("chat_mcp") — see packages/aria_mcp/server.py's
    _call_broker_confirm_order for that gate."""
    preview = load_order_preview(preview_id)
    if not preview:
        return {"success": False, "error": f"preview not found: {preview_id}"}
    if not confirmed:
        return {"success": False, "confirmation_required": True, "preview": preview}
    if preview.get("broker_id") != getattr(broker, "broker_id", ""):
        return {"success": False, "error": "preview broker does not match active broker", "preview": preview}

    policy = policy_from_config(getattr(broker, "config", {}) or {}, getattr(broker, "broker_type", ""))
    blockers = _execution_blockers(policy, preview.get("order_plan") or {})
    if blockers:
        mark_preview_status(preview_id, "rejected", {"execution_blockers": blockers})
        _audit({"event": "trade_rejected", "preview_id": preview_id, "blockers": blockers})
        return {"success": False, "risk_rejected": True, "execution_blockers": blockers, "preview": preview}

    intent = preview.get("intent") or {}
    planned_order = ((preview.get("order_plan") or {}).get("estimated_order") or {})
    if not planned_order:
        return {"success": False, "error": "preview has no executable order", "preview": preview}
    result = broker.place_order(
        symbol=str(planned_order.get("symbol") or intent.get("symbol", "")),
        side=str(planned_order.get("side") or intent.get("side", "")),
        quantity=float(planned_order.get("quantity", intent.get("quantity", 0.0)) or 0.0),
        order_type=str(planned_order.get("order_type") or intent.get("order_type", "limit")),
        price=float(planned_order.get("price", intent.get("price", 0.0)) or 0.0),
    )
    payload = {
        "success": bool(getattr(result, "success", False)),
        "order_id": getattr(result, "order_id", ""),
        "message": getattr(result, "message", ""),
        "broker": getattr(broker, "label", ""),
        "broker_id": getattr(broker, "broker_id", ""),
        "mode": policy.mode,
        "preview_id": preview_id,
        "symbol": str(planned_order.get("symbol") or intent.get("symbol", "")),
        "side": str(planned_order.get("side") or intent.get("side", "")),
        "qty": float(planned_order.get("quantity", intent.get("quantity", 0.0)) or 0.0),
        "order_plan": preview.get("order_plan"),
    }
    mark_preview_status(preview_id, "executed" if payload["success"] else "failed", {
        "result": payload,
    })
    _audit({"event": "trade_execute", "preview_id": preview_id, "result": payload, "source": source})
    return payload
