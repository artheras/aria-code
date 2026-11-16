import pathlib
import sys
from types import SimpleNamespace


_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)


class _YahooChartResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "chart": {
                "result": [{
                    "timestamp": [1781222400, 1781308800],
                    "meta": {"currency": "USD"},
                    "indicators": {
                        "quote": [{
                            "open": [186.0, 190.0],
                            "high": [192.0, 195.0],
                            "low": [184.0, 188.0],
                            "close": [191.8, 193.4],
                            "volume": [1200000, 1350000],
                        }]
                    },
                }]
            }
        }


def test_yahoo_chart_fallback_returns_clean_ohlcv(monkeypatch):
    import aria_code.apps.cli.handlers.chart_handlers as chart_handlers

    class FakeRequests:
        @staticmethod
        def get(*_args, **_kwargs):
            return _YahooChartResponse()

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)

    hist, currency, error = chart_handlers._fetch_yahoo_chart_frame("SPCX", "1y", "1d")

    assert error == ""
    assert currency == "USD"
    assert list(hist.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert hist.iloc[-1]["Close"] == 193.4


def test_normalise_history_frame_rejects_missing_close():
    import pandas as pd
    import aria_code.apps.cli.handlers.chart_handlers as chart_handlers

    hist = pd.DataFrame({"price": [1, 2, 3]})

    assert chart_handlers._normalise_history_frame(hist) is None


def test_ashare_chart_uses_akshare_when_yfinance_unavailable(monkeypatch, tmp_path):
    import pandas as pd
    import aria_code.apps.cli.handlers.chart_handlers as chart_handlers

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.setitem(
        sys.modules,
        "aria_code.clients.market_data_client",
        SimpleNamespace(get_mdc=lambda: SimpleNamespace(history=lambda *_a, **_k: {"success": False, "error": "mdc unavailable"})),
    )

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(*_args, **_kwargs):
            dates = pd.date_range("2025-01-01", periods=80, freq="D")
            base = [10 + i * 0.05 for i in range(len(dates))]
            return pd.DataFrame({
                "日期": dates.strftime("%Y-%m-%d"),
                "开盘": base,
                "最高": [v + 0.2 for v in base],
                "最低": [v - 0.2 for v in base],
                "收盘": [v + 0.1 for v in base],
                "成交量": [100000 + i for i in range(len(dates))],
            })

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)

    result = chart_handlers.handle_stock_chart_analysis_direct("601899.SS", "1y")

    assert result["success"] is True
    assert result["symbol"] == "601899.SS"
    assert result["provider"] == "akshare"
    assert str(tmp_path / "user-output" / "generated") in result["chart_path"]


def test_natural_language_ashare_chart_analysis_uses_akshare(monkeypatch, tmp_path):
    import pandas as pd
    import aria_code.apps.cli.handlers.chart_handlers as chart_handlers
    from aria_code.apps.cli.utils.market_detect import _extract_market_symbol, _is_stock_chart_analysis_request

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    monkeypatch.setitem(sys.modules, "yfinance", None)
    monkeypatch.setitem(
        sys.modules,
        "aria_code.clients.market_data_client",
        SimpleNamespace(get_mdc=lambda: SimpleNamespace(history=lambda *_a, **_k: {"success": False, "error": "mdc unavailable"})),
    )

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(*_args, **_kwargs):
            dates = pd.date_range("2025-01-01", periods=260, freq="D")
            base = [12 + i * 0.03 for i in range(len(dates))]
            return pd.DataFrame({
                "日期": dates.strftime("%Y-%m-%d"),
                "开盘": base,
                "最高": [v + 0.3 for v in base],
                "最低": [v - 0.3 for v in base],
                "收盘": [v + 0.12 for v in base],
                "成交量": [200000 + i for i in range(len(dates))],
            })

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)

    result = chart_handlers.handle_stock_chart_analysis(
        "生成紫金矿业近一年K线图和技术指标",
        is_chart_request=_is_stock_chart_analysis_request,
        extract_symbol=_extract_market_symbol,
    )

    assert result["success"] is True
    assert "601899.SS" in result["response"]
    assert result["tools_used"] == ["stock_chart", "akshare", "html_chart"]


def test_ashare_chart_prefers_market_data_client(monkeypatch, tmp_path):
    import aria_code.apps.cli.handlers.chart_handlers as chart_handlers

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    records = [
        {
            "date": f"2025-01-{(i % 28) + 1:02d}",
            "open": 10 + i * 0.05,
            "high": 10.3 + i * 0.05,
            "low": 9.8 + i * 0.05,
            "close": 10.1 + i * 0.05,
            "volume": 100000 + i,
        }
        for i in range(80)
    ]
    monkeypatch.setitem(
        sys.modules,
        "aria_code.clients.market_data_client",
        SimpleNamespace(
            get_mdc=lambda: SimpleNamespace(
                history=lambda *_a, **_k: {
                    "success": True,
                    "data": records,
                    "provider": "eastmoney",
                    "provider_chain": ["eastmoney"],
                }
            )
        ),
    )

    result = chart_handlers.handle_stock_chart_analysis_direct("601899", "1y")

    assert result["success"] is True
    assert result["symbol"] == "601899.SS"
    assert result["provider"] == "eastmoney"
    assert str(tmp_path / "user-output" / "generated") in result["chart_path"]
