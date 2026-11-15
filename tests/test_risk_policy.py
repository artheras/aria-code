"""Tests for unified RiskPolicy schema and DataService Point-in-Time quality gate."""

from aria_code.data_service import DataServiceResult, validate_point_in_time
from aria_code.domain.risk_policy import (
    RiskPolicy,
    check_order_compliance,
    resolve_risk_policy,
)


def test_resolve_risk_policy_strictest_wins():
    account = RiskPolicy(
        name="account",
        max_position_weight=0.50,
        max_leverage=2.0,
        max_drawdown_pct=0.25,
        approval_required_above_weight=0.20,
        min_cash_reserve_pct=0.05,
        allow_short=True,
    )
    strategy = RiskPolicy(
        name="strategy",
        max_position_weight=0.40,
        max_leverage=1.5,
        max_drawdown_pct=0.15,
        approval_required_above_weight=0.15,
        min_cash_reserve_pct=0.10,
        allow_short=True,
    )
    execution = RiskPolicy(
        name="execution",
        max_position_weight=0.10,
        max_leverage=1.0,
        max_drawdown_pct=0.20,
        approval_required_above_weight=0.08,
        min_cash_reserve_pct=0.05,
        allow_short=False,
    )

    eff = resolve_risk_policy(account, strategy, execution)
    assert eff.max_position_weight == 0.10
    assert eff.max_leverage == 1.0
    assert eff.max_drawdown_pct == 0.15
    assert eff.approval_required_above_weight == 0.08
    assert eff.min_cash_reserve_pct == 0.10
    assert eff.allow_short is False


def test_check_order_compliance_violations_and_approval():
    policy = RiskPolicy(
        max_position_weight=0.20,
        max_leverage=1.5,
        max_drawdown_pct=0.15,
        approval_required_above_weight=0.10,
        min_cash_reserve_pct=0.05,
        allow_short=False,
    )

    ok = check_order_compliance(policy, target_weight=0.08, cash_reserve_pct=0.20)
    assert ok.allowed is True
    assert ok.requires_approval is False

    needs_approval = check_order_compliance(policy, target_weight=0.15, cash_reserve_pct=0.20)
    assert needs_approval.allowed is True
    assert needs_approval.requires_approval is True

    blocked = check_order_compliance(
        policy,
        target_weight=0.25,
        gross_leverage=2.0,
        current_drawdown_pct=0.16,
        cash_reserve_pct=0.02,
        is_short=True,
    )
    assert blocked.allowed is False
    assert len(blocked.violations) == 5


def test_validate_point_in_time_blocks_lookahead_and_bad_price():
    clean = DataServiceResult(
        kind="quote",
        symbol="AAPL",
        success=True,
        data={"price": 210.5},
        quality={"available_at": "2026-11-10T16:00:00Z", "status": "ok"},
        timestamp="2026-11-10T16:00:00Z",
    )
    ok = validate_point_in_time(clean, as_of="2026-11-11T00:00:00Z")
    assert ok["passed"] is True
    assert ok["quality_status"] == "verified"

    leaked = validate_point_in_time(clean, as_of="2026-11-09T00:00:00Z")
    assert leaked["passed"] is False
    assert leaked["quality_status"] == "rejected"
    assert any("lookahead" in reason for reason in leaked["reasons"])


def test_trading_preview_enforces_unified_risk_policy(tmp_path, monkeypatch):
    import aria_code.brokers.paper_broker as paper_mod
    import aria_code.brokers.trading as trading
    from aria_code.brokers.paper_broker import PaperBroker

    monkeypatch.setattr(paper_mod, "PAPER_LEDGER_PATH", tmp_path / "paper_ledger.json")
    monkeypatch.setattr(trading, "TRADE_PREVIEWS_PATH", tmp_path / "previews.json")
    monkeypatch.setattr(trading, "TRADE_AUDIT_PATH", tmp_path / "audit.jsonl")

    broker = PaperBroker(
        "paper_1",
        {
            "id": "paper_1",
            "type": "paper",
            "label": "Paper",
            "mode": "paper",
            "starting_cash": 100000.0,
            "currency": "USD",
            "max_drawdown_pct": 0.15,
            "max_daily_loss_pct": 0.03,
        },
    )
    broker.connect()
    intent = trading.OrderIntent(
        symbol="AAPL",
        side="buy",
        quantity=10,
        price=100.0,
        metadata={"current_drawdown_pct": 0.22, "cash_reserve_pct": 0.01},
    )
    preview = trading.build_order_preview(broker, intent)
    assert preview["can_execute"] is False
    assert preview["risk_compliance"]["allowed"] is False
    assert any("drawdown" in b for b in preview["execution_blockers"])
    assert any("Cash reserve" in b for b in preview["execution_blockers"])
