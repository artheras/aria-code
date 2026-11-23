"""Functional parity of the native domain binary against the current engine.

Only the test oracle uses Python; the program under test is the Go executable.
CI supplies ARIA_GO_RUNTIME_BINARY after building it from the checkout.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import os
import random
import subprocess

import pytest

try:
    from arthera_finance.backtest_engine import Bar, BacktestEngine, get_strategy
except ImportError:
    pytest.skip(
        "arthera_finance.backtest_engine moved to artheras/financial-service; install it to run Go parity oracle",
        allow_module_level=True,
    )


def cases():
    def series(values, opens=None, missing=()):
        return [dict(date=(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(),
                     open=opens[i] if opens else close, high=close, low=close,
                     close=close, volume=1000)
                for i, close in enumerate(values) if i not in missing]

    common = dict(starting_cash=10000.0, commission=0.0, slippage=0.0)
    yield "next-open-gap", dict(common, data={"AAA": series([10, 20], [10, 20])}, strategy="buy_hold")
    yield "exact-profit", dict(common, data={"AAA": series([10, 12, 12], [10, 10, 12])}, strategy="buy_hold")
    yield "fees-slippage", dict(common, commission=0.01, slippage=0.01, data={"AAA": series([10, 12, 12], [10, 10, 12])}, strategy="buy_hold")
    yield "sma-sell", dict(common, data={"AAA": series([10, 10, 10, 12, 14, 16, 10, 8, 7, 6])}, strategy="sma_cross", fast=2, slow=3)
    yield "missing-days", dict(common, data={"ZZZ": series([10, 12, 11, 9, 10], missing=(2,)), "AAA": series([20, 21, 18, 20, 22], missing=(1,))}, strategy="buy_hold")
    yield "empty", dict(common, data={}, strategy="buy_hold")
    yield "one-bar", dict(common, data={"AAA": series([10])}, strategy="buy_hold")
    yield "empty-series", dict(common, data={"ZZZ": [], "AAA": series([10, 11, 12])}, strategy="buy_hold")
    rng = random.Random(731)
    for i in range(40):
        data = {}
        for symbol in ("ZZZ", "AAA", "MID")[:1 + i % 3]:
            closes, opens = [], []
            last = rng.uniform(50, 150)
            for _ in range(35):
                opens.append(round(last * rng.uniform(0.98, 1.02), 4))
                last = round(last * rng.uniform(0.97, 1.03), 4)
                closes.append(last)
            data[symbol] = series(closes, opens, missing=(8, 19) if symbol == "AAA" else ())
        yield f"deterministic-{i}", dict(common, data=data, commission=0.0005, slippage=0.0005,
                                         strategy="sma_cross" if i % 2 else "buy_hold", fast=3, slow=8)


def expected(request):
    data = {s: [Bar(**b) for b in bars] for s, bars in request["data"].items()}
    engine = BacktestEngine(**{k: request[k] for k in ("starting_cash", "commission", "slippage", "periods_per_year") if k in request})
    parameters = {k: request[k] for k in ("fast", "slow", "weight") if k in request and (k == "weight" or request["strategy"] == "sma_cross")}
    return dataclasses.asdict(engine.run(data, get_strategy(request["strategy"], **parameters)))


def same_result(got, want, path="result"):
    if isinstance(want, dict):
        assert isinstance(got, dict) and got.keys() == want.keys(), path
        for key in want:
            same_result(got[key], want[key], f"{path}.{key}")
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), path
        for index, (actual, reference) in enumerate(zip(got, want)):
            same_result(actual, reference, f"{path}[{index}]")
    elif isinstance(want, (int, float)):
        assert math.isclose(got, want, rel_tol=1e-12, abs_tol=1e-8), (path, got, want)
    else:
        assert got == want, path


@pytest.mark.parametrize("name,case", list(cases()))
def test_native_backtest_matches_existing_business_results(tmp_path, name, case):
    binary = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not binary:
        pytest.skip("Native Go runtime is built by the native-runtime CI job")
    source = tmp_path / "bars.json"
    source.write_text(json.dumps(case), encoding="utf-8")
    process = subprocess.run([binary, "backtest", "--input", str(source)], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert process.returncode == 0, (name, process.stderr)
    same_result(json.loads(process.stdout), expected(case))
