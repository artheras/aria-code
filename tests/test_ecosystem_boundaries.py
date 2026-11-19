"""Deterministic architecture & ecosystem boundary guardrails ("Software Garden").

Every time vertical code is decoupled to an external MCP service or a security
invariant is established, codifying it here prevents future edits or agents from
silently re-introducing deleted modules, bloating thin stubs, or hardcoding
restricted MCP env flags.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "aria_code"
RUST_SRC = ROOT / "rust" / "cli" / "src"


def test_deleted_vertical_modules_stay_absent():
    removed = [
        SRC / "brokers" / "cn",
        SRC / "brokers" / "intl",
        SRC / "brokers" / "automation.py",
        SRC / "datasources" / "sources",
        SRC / "packages" / "quant_engine",
        SRC / "domain" / "rust_quant.py",
        SRC / "domain" / "portfolio_ledger.py",
        SRC / "domain" / "backtest_report.py",
        SRC / "domain" / "finance_formulas.py",
        SRC / "agents" / "portfolio_agent.py",
        SRC / "apps" / "cli" / "handlers" / "chart_handlers.py",
        SRC / "tools" / "broker_tools.py",
        SRC / "clients" / "aliyun_data_client.py",
    ]
    present = [
        str(p.relative_to(ROOT))
        for p in removed
        if (any(p.rglob("*.py")) if p.is_dir() else p.exists())
    ]
    assert not present, f"Deleted vertical modules re-appeared: {present}"


def test_decoupled_stubs_stay_under_line_budget():
    budgets = {
        SRC / "apps" / "cli" / "handlers" / "market_handlers.py": 60,
        SRC / "apps" / "cli" / "handlers" / "broker_handlers.py": 30,
        SRC / "ui" / "render" / "finance.py": 90,
        SRC / "aria_cli.py": 5200,
        SRC / "tools" / "local_finance_tools.py": 60,
        SRC / "report_generator.py": 300,
    }
    violations = []
    for path, limit in budgets.items():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > limit:
            violations.append(f"{path.relative_to(ROOT)}: {lines} > {limit} lines")
    assert not violations, "Module line budgets exceeded:\n" + "\n".join(violations)


def test_mcp_clients_never_hardcode_financial_network_or_broker_env():
    mcp_rs = (RUST_SRC / "engine" / "mcp.rs").read_text(encoding="utf-8")
    mcp_py = (SRC / "packages" / "aria_mcp" / "config.py").read_text(encoding="utf-8")

    # Non-comment lines must never insert/assign restricted financial-service env vars.
    for label, text, comment_prefix in [
        ("rust/cli/src/engine/mcp.rs", mcp_rs, "//"),
        ("packages/aria_mcp/config.py", mcp_py, "#"),
    ]:
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(comment_prefix) or "assert!" in stripped:
                continue
            if re.search(
                r'(insert|env.*:|\bFINANCIAL_SERVICE_(NETWORK|BROKERS_ENABLED)\b\s*[:=])',
                stripped,
            ) and (
                "FINANCIAL_SERVICE_NETWORK" in stripped
                or "FINANCIAL_SERVICE_BROKERS_ENABLED" in stripped
            ):
                raise AssertionError(
                    f"{label}:{lineno} hardcodes restricted financial-service env var: {stripped}"
                )
