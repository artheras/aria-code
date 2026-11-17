"""
tests/test_coder_tester_self_healing.py — Tests for Coder, Tester, and Self-Healing Engine
"""

import asyncio
import json
import pathlib
import sys
import tempfile
import pytest

from aria_code.agents.engineering.coder import CoderAgent
from aria_code.agents.engineering.tester import TesterAgent, TesterSelfHealingAgent
from aria_code.agents.registry import get_registry
from aria_code.runtime.self_healing import SelfHealingEngine, TracebackInfo


def test_agent_registry_discovery():
    registry = get_registry()
    coder_cls = registry.get("coder")
    tester_cls = registry.get("tester")
    debugger_cls = registry.get("debugger")

    assert coder_cls is not None
    assert tester_cls is not None
    assert debugger_cls is not None


# 85b4d36 ("feat(coder): empower coder agent to dynamically write code using LLM
# instead of a static template") removed CoderAgent.generate_backtest_script and
# _build_self_contained_python, and analyze() no longer returns a script_path at
# all — it drives a tool-calling LLM loop and reports prose. The two tests below
# assert the deleted contract. They are kept rather than deleted so the coverage
# gap stays visible: whoever owns the LLM-driven coder has to decide what the
# replacement contract is and what an offline test of it looks like.
_CODER_TEMPLATE_REMOVED = pytest.mark.skip(
    reason="CoderAgent's static script generator was removed in 85b4d36; "
           "these assert the pre-LLM contract and need rewriting against the new one"
)


@_CODER_TEMPLATE_REMOVED
def test_coder_agent_generate_self_contained_script():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = pathlib.Path(tmpdir)
        coder = CoderAgent(output_dir=out_dir)

        rule_tree = {
            "symbol": "NVDA",
            "strategy_name": "NVDA Dual Moving Average",
            "factors": [
                {"name": "SMA_Fast", "params": {"period": 10}},
                {"name": "SMA_Slow", "params": {"period": 30}},
            ],
            "risk_constraints": {
                "stop_loss_pct": 7.0,
                "slippage_pct": 0.05,
                "commission_pct": 0.05,
            },
        }

        script_path, code = coder.generate_backtest_script("NVDA", rule_tree=rule_tree)
        assert script_path.exists()
        assert "SYMBOL = \"NVDA\"" in code
        assert "FAST_PERIOD = 10" in code
        assert "SLOW_PERIOD = 30" in code
        assert "SLIPPAGE_PCT = 0.05" in code

        # Verify syntax
        engine = SelfHealingEngine()
        valid, err, line = engine.verify_syntax(script_path)
        assert valid is True
        assert err is None


def test_self_healing_engine_syntax_and_execution():
    """Clean script → runs first try, metrics and artifacts collected.

    Owns its fixture rather than asking CoderAgent for one: this is a
    SelfHealingEngine test, and routing it through the agent made it depend on
    the static generator that 85b4d36 removed (and now on a live LLM).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = pathlib.Path(tmpdir)
        script_path = out_dir / "strategy_aapl.py"
        # _extract_results reads <stem>_metrics.json and <stem>_backtest.png.
        script_path.write_text(
            '\n'.join([
                "import json, pathlib",
                "OUT = pathlib.Path(__file__).parent",
                'metrics = {"symbol": "AAPL", "total_return_pct": 12.5,',
                '           "sharpe_ratio": 1.1, "max_drawdown_pct": -4.2}',
                '(OUT / "strategy_aapl_metrics.json").write_text(json.dumps(metrics))',
                '(OUT / "strategy_aapl_backtest.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\n")',
                "print(json.dumps(metrics))",
            ]),
            encoding="utf-8",
        )

        engine = SelfHealingEngine(python_executable=sys.executable)
        valid, err, _line = engine.verify_syntax(script_path)
        assert valid is True
        assert err is None
        async def _run():
            res = await engine.execute_and_heal(script_path)
            assert res.success is True
            assert res.metrics.get("symbol") == "AAPL"
            assert "total_return_pct" in res.metrics
            assert "sharpe_ratio" in res.metrics
            assert "max_drawdown_pct" in res.metrics
            assert len(res.artifact_paths) >= 1

        asyncio.run(_run())


def test_self_healing_engine_patch_repair_on_zero_division():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = pathlib.Path(tmpdir) / "buggy_calc.py"
        buggy_code = """
import json
def calculate():
    x = 100
    y = 0
    sharpe = x / y
    return {"sharpe_ratio": sharpe, "symbol": "TEST"}

if __name__ == "__main__":
    res = calculate()
    print(json.dumps(res))
"""
        test_file.write_text(buggy_code.strip(), encoding="utf-8")

        engine = SelfHealingEngine(python_executable=sys.executable, max_retries=3)
        async def _run():
            res = await engine.execute_and_heal(test_file)
            assert res.success is True
            assert len(res.patches_applied) > 0
            assert res.metrics.get("symbol") == "TEST"

        asyncio.run(_run())



