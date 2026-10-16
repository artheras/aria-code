#!/usr/bin/env python3
"""Regenerate the Go backtest parity fixtures from the Python engine.

go/runtime/testdata/backtest-parity.json holds 48 cases, each an input and the
result the Python engine produced for it. The Go test asserts Go's output is
identical, so Python is the oracle and Go follows it. When the engine's
arithmetic changes on purpose, the fixtures have to be regenerated from it —
this is how, so the file is never edited by hand and never drifts from what
Python actually does.

    python scripts/regen_backtest_parity.py --check    # fail if out of date
    python scripts/regen_backtest_parity.py --write    # rewrite the fixtures
"""
from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aria_code.domain.backtest_engine import _STRATEGIES, Bar, BacktestEngine, get_strategy  # noqa: E402

FIXTURES = ROOT / "go" / "runtime" / "testdata" / "backtest-parity.json"


def run_case(case_input: dict) -> dict:
    """What the Python engine returns for one fixture input, in the fixture's shape."""
    data = {
        symbol: [Bar(date=b["date"], open=b["open"], high=b["high"], low=b["low"],
                     close=b["close"], volume=b.get("volume", 0.0)) for b in bars]
        for symbol, bars in case_input["data"].items()
    }
    engine_args = {}
    for key in ("starting_cash", "commission", "slippage", "periods_per_year"):
        if case_input.get(key) is not None:
            engine_args[key] = case_input[key]
    # Go ignores arguments a strategy does not take (a buy-and-hold fixture may
    # still carry fast/slow); Python's constructors reject them, so pass only
    # what each one accepts.
    accepted = set(inspect.signature(_STRATEGIES[case_input["strategy"].lower()].__init__).parameters)
    strategy_args = {k: case_input[k] for k in ("fast", "slow", "weight")
                     if case_input.get(k) is not None and k in accepted}
    result = BacktestEngine(**engine_args).run(data, get_strategy(case_input["strategy"], **strategy_args))
    return {
        "strategy": result.strategy,
        "symbols": result.symbols,
        "starting_cash": result.starting_cash,
        "equity_curve": result.equity_curve,
        "trades": result.trades,
        "metrics": result.metrics,
        "benchmark": result.benchmark,
    }


def regenerate() -> list[dict]:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return [{**case, "expected": run_case(case["input"])} for case in fixtures]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()

    current = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fresh = regenerate()
    stale = [a["name"] for a, b in zip(current, fresh) if a["expected"] != b["expected"]]
    if args.check:
        if stale:
            print(f"{len(stale)} of {len(current)} fixtures differ from the Python engine: {', '.join(stale[:8])}")
            return 1
        print(f"all {len(current)} fixtures match the Python engine")
        return 0
    FIXTURES.write_text(json.dumps(fresh, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"rewrote {len(fresh)} fixtures; {len(stale)} changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
