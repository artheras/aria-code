"""Tests for tool_get_market_history — the compact OHLC history tool.

The tool must give the model usable history WITHOUT dumping the full series
into context (that is what blew the context window and cut tasks short before).
"""
import pathlib
import sys

import pytest

_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)


class _FakeMDC:
    def __init__(self, rows, success=True, provider="eastmoney"):
        self._rows = rows
        self._success = success
        self._provider = provider

    def history(self, symbol, days=120, interval="1d"):
        if not self._success:
            return {"success": False, "error": "数据源暂时不可用",
                    "provider_chain": ["eastmoney", "sina"]}
        return {"success": True, "symbol": symbol, "name": symbol,
                "data": self._rows, "provider": self._provider,
                "provider_chain": [self._provider], "count": len(self._rows)}


def _series(n):
    # ascending dates, rising close 100..100+n
    return [
        {"date": f"2026-01-{(i % 28) + 1:02d}", "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100 + i, "volume": 1000 + i}
        for i in range(n)
    ]


@pytest.fixture
def _patch_mdc(monkeypatch):
    import apps.cli.tools.market_tools as mt

    def _install(fake):
        # The tool now routes through DataService; wrap the fake MDC in a real
        # DataService (router disabled) so we exercise the actual code path.
        from data_service import DataService
        svc = DataService(market_client=fake, router=False)
        monkeypatch.setattr(mt, "_HAS_MDC", True, raising=False)
        monkeypatch.setattr(mt, "_get_mdc", lambda: fake, raising=False)
        monkeypatch.setattr(mt, "_get_data_service", lambda: svc, raising=False)
    return _install


def test_history_returns_compact_summary(_patch_mdc):
    from apps.cli.tools.market_tools import tool_get_market_history
    _patch_mdc(_FakeMDC(_series(252)))

    r = tool_get_market_history({"symbol": "600519", "days": 252})

    assert r["success"] is True
    assert r["total_points"] == 252
    # The full 252-row series must NOT be returned — only the recent tail.
    assert len(r["recent_candles"]) == 30
    s = r["summary"]
    assert s["start_close"] == 100.0
    assert s["end_close"] == 351.0
    assert s["period_high"] == 352.0   # high of last row = 101 + 251
    assert s["ma5"] is not None and s["ma20"] is not None and s["ma60"] is not None
    assert s["change_pct"] == pytest.approx((351 - 100) / 100 * 100, rel=1e-3)


def test_history_short_series_recent_capped_to_length(_patch_mdc):
    from apps.cli.tools.market_tools import tool_get_market_history
    _patch_mdc(_FakeMDC(_series(8)))

    r = tool_get_market_history({"symbol": "AAPL", "days": 30})

    assert r["success"] is True
    assert len(r["recent_candles"]) == 8
    # Not enough points for MA20/MA60 → None, but MA5 computable.
    assert r["summary"]["ma5"] is not None
    assert r["summary"]["ma20"] is None
    assert r["summary"]["ma60"] is None


def test_history_propagates_source_failure(_patch_mdc):
    from apps.cli.tools.market_tools import tool_get_market_history
    _patch_mdc(_FakeMDC([], success=False))

    r = tool_get_market_history({"symbol": "600519"})

    assert r["success"] is False
    assert "provider_chain" in r
    assert r["error"]


def test_history_requires_symbol():
    from apps.cli.tools.market_tools import tool_get_market_history
    r = tool_get_market_history({"symbol": "   "})
    assert r["success"] is False
    assert "symbol" in r["error"].lower()


def test_history_payload_stays_small(_patch_mdc):
    """Even for a huge lookback, the serialized payload must stay compact."""
    import json
    from apps.cli.tools.market_tools import tool_get_market_history
    _patch_mdc(_FakeMDC(_series(1000)))

    r = tool_get_market_history({"symbol": "600519", "days": 1000})
    size = len(json.dumps(r, ensure_ascii=False))
    assert r["success"] is True
    assert size < 6000, f"history payload too large: {size} bytes"


def test_renderer_registered_as_finance_tool():
    from ui.render.output import FINANCE_TOOL_NAMES
    assert "get_market_history" in FINANCE_TOOL_NAMES
