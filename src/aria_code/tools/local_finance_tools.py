"""
local_finance_tools.py — Fully-offline financial tool implementations for Aria Code.

Purpose
-------
When running in local_mode (no Arthera backend), this module provides drop-in
replacements for every ARIA_TOOLS entry using open-source data libraries:

  A股 data    → akshare
  US/Global   → yfinance
  Crypto      → ccxt
  Backtesting → vectorbt (or pandas fallback)
  Technical   → pandas_ta (or ta-lib if installed)
  Risk        → scipy / numpy

Each tool follows the same contract as the remote Aria tools:
  handler(params: dict) -> dict  (always returns a dict, never raises)

Registration
------------
Call ``register_local_finance_tools(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)`` at
startup to extend the CLI's tool registry.  The function is idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from importlib import import_module
from importlib.util import find_spec
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aria_code.packages.aria_core.paths import aria_home


class _LazyModule:
    """Load a finance dependency only when its tool is invoked."""

    def __init__(self, name: str):
        self.name = name
        self.module = None

    def __getattr__(self, attribute: str):
        if self.module is None:
            self.module = import_module(self.name)
        return getattr(self.module, attribute)


np = _LazyModule("numpy")
pd = _LazyModule("pandas")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alibaba Cloud data client (optional — degrades gracefully when offline)
# ---------------------------------------------------------------------------

try:
    from aria_code.aliyun_data_client import (
        AliyunDataClient,
        cloud_get_quote_sync,
        cloud_get_history_sync,
        cloud_get_factors_sync,
        cloud_get_ai_signal_sync,
    )
    _HAS_CLOUD = True
    logger.debug("Alibaba Cloud data client loaded ✓")
except ImportError:
    _HAS_CLOUD = False
    logger.debug("aliyun_data_client not found — cloud features disabled")

# ---------------------------------------------------------------------------
# Optional dependency guards
# ---------------------------------------------------------------------------

_HAS_YF = find_spec("yfinance") is not None
_HAS_AK = find_spec("akshare") is not None
yf = _LazyModule("yfinance")
ak = _LazyModule("akshare")


def _ak_retry(fn, *args, _tries: int = 3, _delay: float = 0.8, **kwargs):
    """Call an akshare function with retries, bypassing a broken proxy.

    Two transient failure modes are handled:
      1. akshare hits numbered eastmoney hosts (NN.push2.eastmoney.com) that go
         down individually — a retry usually lands on a healthy host.
      2. A misconfigured HTTP(S)_PROXY raises ProxyError even though the source
         is directly reachable. On a proxy/connection error we retry with the
         proxy env vars temporarily cleared (akshare uses requests trust_env),
         then restore them so the rest of the app is unaffected.
    """
    import os as _os
    import time as _t
    _PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
    last_exc = None
    for _i in range(_tries):
        _saved = {}
        _bypass = _i > 0  # first attempt honours the proxy; retries bypass it
        if _bypass:
            for _v in _PROXY_VARS:
                if _v in _os.environ:
                    _saved[_v] = _os.environ.pop(_v)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — akshare raises many types
            last_exc = exc
            if _i < _tries - 1:
                _t.sleep(_delay)
        finally:
            for _v, _val in _saved.items():
                _os.environ[_v] = _val
    raise last_exc

_HAS_CCXT = find_spec("ccxt") is not None
ccxt = _LazyModule("ccxt")

_HAS_TA = find_spec("pandas_ta") is not None

_HAS_VBT = find_spec("vectorbt") is not None
vbt = _LazyModule("vectorbt")

_HAS_SCIPY = find_spec("scipy") is not None
sp_stats = _LazyModule("scipy.stats")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _safe(fn, params, *args, **kwargs):
    """Wrap a tool handler so it never raises — returns error dict instead."""
    try:
        return fn(params, *args, **kwargs)
    except Exception as exc:
        logger.debug("Local finance tool error: %s", traceback.format_exc())
        return {"success": False, "error": str(exc), "traceback": traceback.format_exc()[-500:]}


def _parse_date(s: Optional[str], default_days_back: int = 365) -> str:
    if s:
        return s
    return (datetime.today() - timedelta(days=default_days_back)).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.today().strftime("%Y-%m-%d")


def _is_ashare(symbol: str) -> bool:
    s = symbol.strip().lower()
    return (
        s.startswith("sh") or s.startswith("sz")
        or (len(s) == 6 and s.isdigit())
        or s.endswith(".ss") or s.endswith(".sz")
    )


def _normalise_ashare(symbol: str) -> str:
    s = symbol.strip().lower().replace(".ss", "").replace(".sz", "")
    s = s.replace("sh", "").replace("sz", "")
    if len(s) == 6 and s.isdigit():
        prefix = "sh" if s.startswith("6") else "sz"
        return prefix + s
    return s


def _get_pandas_ta():
    if not _HAS_TA:
        return None
    try:
        import pandas_ta as ta
    except Exception:
        return None
    return ta


# ---------------------------------------------------------------------------
# 1. get_market_data
# ---------------------------------------------------------------------------

def _get_market_data(params: dict) -> dict:
    symbol   = params.get("symbol", "AAPL").upper()
    period   = params.get("period", "1y")        # yfinance period
    interval = params.get("interval", "1d")
    start    = params.get("start")
    end      = params.get("end", _today())

    if _is_ashare(symbol):
        return _get_ashare_data(symbol, start or _parse_date(None, 365), end)

    # ── Try Alibaba Cloud first (for US/global symbols) ────────────────
    if _HAS_CLOUD:
        try:
            cloud_q = cloud_get_quote_sync(symbol)
            if cloud_q and cloud_q.get("price"):
                return {
                    "success":      True,
                    "symbol":       symbol,
                    "latest_close": round(float(cloud_q.get("price", 0)), 4),
                    "change_pct":   round(float(cloud_q.get("change_percent", 0)), 3),
                    "volume":       int(cloud_q.get("volume", 0)),
                    "high":         round(float(cloud_q.get("high", 0)), 4),
                    "low":          round(float(cloud_q.get("low", 0)), 4),
                    "open":         round(float(cloud_q.get("open", 0)), 4),
                    "name":         cloud_q.get("name", symbol),
                    "market":       cloud_q.get("market", "US"),
                    "provider":     "aliyun_cloud",
                }
        except Exception as exc:
            logger.debug("Cloud quote failed for %s: %s — falling back to yfinance", symbol, exc)

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: pip install yfinance"}

    try:
        tkr  = yf.Ticker(symbol)
        if start:
            hist = tkr.history(start=start, end=end, interval=interval)
        else:
            hist = tkr.history(period=period, interval=interval)

        if hist.empty:
            return {"success": False, "error": f"No data for {symbol}"}

        info        = tkr.fast_info
        latest      = hist.iloc[-1]
        prev        = hist.iloc[-2] if len(hist) > 1 else latest
        chg         = (latest["Close"] - prev["Close"]) / prev["Close"] * 100

        return {
            "success":       True,
            "symbol":        symbol,
            "latest_close":  round(float(latest["Close"]), 4),
            "change_pct":    round(float(chg), 3),
            "volume":        int(latest["Volume"]),
            "high_52w":      round(float(info.year_high),  4) if hasattr(info, "year_high")  else None,
            "low_52w":       round(float(info.year_low),   4) if hasattr(info, "year_low")   else None,
            "market_cap":    getattr(info, "market_cap", None),
            "currency":      getattr(info, "currency", "USD"),
            "bars":          len(hist),
            "history_tail":  _df_tail(hist, 5),
            "provider":      "yfinance",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _get_ashare_data(symbol: str, start: str, end: str) -> dict:
    # ── Try Alibaba Cloud akshare_data_server first ────────────────────
    if _HAS_CLOUD:
        try:
            cloud_hist = cloud_get_history_sync(symbol, start=start, end=end)
            if cloud_hist and cloud_hist.get("data"):
                rows = cloud_hist["data"]
                if rows:
                    latest   = rows[-1]
                    prev     = rows[-2] if len(rows) > 1 else latest
                    cl, pc   = float(latest.get("close", 0)), float(prev.get("close", 0) or 0.0001)
                    chg      = (cl - pc) / pc * 100
                    # Build history_tail
                    tail = [
                        {k: v for k, v in r.items() if k in ("date", "open", "high", "low", "close", "volume")}
                        for r in rows[-5:]
                    ]
                    return {
                        "success":       True,
                        "symbol":        symbol,
                        "latest_close":  round(cl, 3),
                        "change_pct":    round(chg, 3),
                        "volume":        int(latest.get("volume", 0)),
                        "bars":          len(rows),
                        "history_tail":  tail,
                        "provider":      "aliyun_data",
                    }
        except Exception as exc:
            logger.debug("Cloud history failed for %s: %s — falling back to akshare", symbol, exc)

    if not _HAS_AK:
        return {"success": False, "error": "akshare not installed: pip install akshare"}

    norm = _normalise_ashare(symbol)
    code = norm[2:]  # strip sh/sz prefix
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start.replace("-", ""),
                                 end_date=end.replace("-", ""),
                                 adjust="qfq")
        if df is None or df.empty:
            return {"success": False, "error": f"No A-share data for {symbol}"}

        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover_rate",
        })
        latest = df.iloc[-1]
        prev   = df.iloc[-2] if len(df) > 1 else latest
        chg    = (latest["close"] - prev["close"]) / prev["close"] * 100

        return {
            "success":       True,
            "symbol":        symbol,
            "latest_close":  round(float(latest["close"]), 3),
            "change_pct":    round(float(chg), 3),
            "volume":        int(latest["volume"]),
            "turnover_rate": float(latest.get("turnover_rate", 0) or 0),
            "bars":          len(df),
            "history_tail":  _df_tail(df.rename(columns={"close": "Close", "volume": "Volume"}), 5),
            "provider":      "akshare",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 2. get_crypto_data
# ---------------------------------------------------------------------------

def _get_crypto_data(params: dict) -> dict:
    symbol   = params.get("symbol", "BTC/USDT").upper().replace("-", "/")
    exchange = params.get("exchange", "binance")
    timeframe = params.get("timeframe", "1d")
    limit    = int(params.get("limit", 100))

    if not _HAS_CCXT:
        # Fallback to yfinance for common crypto tickers
        if _HAS_YF:
            yf_sym = symbol.replace("/", "-") + ("" if symbol.endswith("USD") else "")
            return _get_market_data({"symbol": yf_sym, "period": "3mo"})
        return {"success": False, "error": "ccxt not installed: pip install ccxt"}

    def _yf_crypto_fallback(reason: str) -> dict:
        """Fall back to yfinance when the exchange is unreachable (region block, etc.)."""
        if not _HAS_YF:
            return {"success": False, "error": reason}
        # BTC/USDT → BTC-USD ; strip stablecoin quote to USD for yfinance
        base = symbol.split("/")[0]
        yf_sym = f"{base}-USD"
        res = _get_market_data({"symbol": yf_sym, "period": "3mo"})
        if res.get("success"):
            res["provider"] = "yfinance (ccxt fallback)"
            res["note"] = f"{exchange} 不可用，已回退 yfinance: {reason[:60]}"
        return res

    try:
        ex_class = getattr(ccxt, exchange.lower(), None)
        if ex_class is None:
            return {"success": False, "error": f"Unknown exchange: {exchange}"}
        ex = ex_class({"enableRateLimit": True})
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as net_exc:
            # Network / region block (e.g. Binance 451) → yfinance fallback
            return _yf_crypto_fallback(str(net_exc))
        if not ohlcv:
            return _yf_crypto_fallback("Empty OHLCV data")

        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["ts"], unit="ms")
        latest   = df.iloc[-1]
        prev     = df.iloc[-2]
        chg      = (latest["close"] - prev["close"]) / prev["close"] * 100
        vol_avg  = df["volume"].tail(20).mean()

        # Ticker for bid/ask
        try:
            ticker = ex.fetch_ticker(symbol)
        except Exception:
            ticker = {}

        return {
            "success":       True,
            "symbol":        symbol,
            "exchange":      exchange,
            "latest_close":  round(float(latest["close"]), 6),
            "change_pct_24h": round(float(chg), 3),
            "volume_24h":    float(latest["volume"]),
            "volume_avg_20d": round(float(vol_avg), 2),
            "bid":           ticker.get("bid"),
            "ask":           ticker.get("ask"),
            "bars":          len(df),
            "provider":      "ccxt",
        }
    except Exception as exc:
        return _yf_crypto_fallback(str(exc))


# ---------------------------------------------------------------------------
# 3. get_forex_data
# ---------------------------------------------------------------------------

def _get_forex_data(params: dict) -> dict:
    pair   = params.get("pair", "EURUSD=X")
    period = params.get("period", "3mo")
    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}
    # Normalise to yfinance format
    p = pair.replace("/", "").upper()
    if not p.endswith("=X"):
        p += "=X"
    return _get_market_data({"symbol": p, "period": period})


# ---------------------------------------------------------------------------
# 4. calculate_factors
# ---------------------------------------------------------------------------

def _calculate_factors(params: dict) -> dict:
    symbol = params.get("symbol", "AAPL")
    period = params.get("period", "1y")

    # ── Try Alibaba Cloud enhanced factor engine first ─────────────────
    if _HAS_CLOUD:
        try:
            cloud_factors = cloud_get_factors_sync(symbol)
            if cloud_factors and cloud_factors.get("success"):
                cloud_factors["provider"] = "aliyun_cloud"
                return cloud_factors
        except Exception as exc:
            logger.debug("Cloud factors failed for %s: %s — computing locally", symbol, exc)

    if _is_ashare(symbol) and _HAS_AK:
        data_result = _get_ashare_data(
            symbol,
            _parse_date(None, 365),
            _today(),
        )
    elif _HAS_YF:
        data_result = _get_market_data({"symbol": symbol, "period": period})
    else:
        return {"success": False, "error": "No data source available"}

    if not data_result.get("success"):
        return data_result

    # Re-fetch full history for factor computation
    if _is_ashare(symbol) and _HAS_AK:
        norm = _normalise_ashare(symbol)
        code = norm[2:]
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=_parse_date(None, 365).replace("-", ""),
                                 end_date=_today().replace("-", ""),
                                 adjust="qfq")
        df = df.rename(columns={"收盘": "Close", "成交量": "Volume",
                                 "开盘": "Open", "最高": "High", "最低": "Low"})
    else:
        tkr = yf.Ticker(symbol)
        df  = tkr.history(period=period)

    if df is None or len(df) < 20:
        return {"success": False, "error": "Insufficient history for factors"}

    close  = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    ret    = close.pct_change().dropna()

    factors: Dict[str, Any] = {"symbol": symbol}

    # ── Price momentum ─────────────────────────────────────────────────────
    for n in (5, 10, 20, 60, 120):
        if len(close) > n:
            factors[f"return_{n}d"] = round(float(close.pct_change(n).iloc[-1]), 5)

    # ── Moving averages ────────────────────────────────────────────────────
    for n in (5, 10, 20, 60, 120, 200):
        if len(close) >= n:
            ma = close.rolling(n).mean().iloc[-1]
            factors[f"ma_{n}"] = round(float(ma), 4)
            factors[f"ma_{n}_gap"] = round(float(close.iloc[-1] / ma - 1), 5)

    # ── Volatility ─────────────────────────────────────────────────────────
    for n in (10, 20, 60):
        if len(ret) >= n:
            factors[f"volatility_{n}d"] = round(float(ret.tail(n).std() * np.sqrt(252)), 5)

    # ── Volume ─────────────────────────────────────────────────────────────
    if len(volume) >= 20:
        vol_ma20 = volume.rolling(20).mean().iloc[-1]
        factors["volume_ratio_20d"] = round(float(volume.iloc[-1] / vol_ma20), 3) if vol_ma20 > 0 else None

    # ── RSI ────────────────────────────────────────────────────────────────
    # pandas_ta when it imports; otherwise the same 14-day RSI by hand. It is
    # installed-but-broken more often than absent (numba has no wheel for a
    # new Python; the native binary leaves it out), and that used to report
    # rsi_14 = None rather than falling back.
    ta = _get_pandas_ta() if len(close) >= 14 else None
    if ta is not None:
        rsi = ta.rsi(close, length=14)
        if rsi is not None and not rsi.empty:
            factors["rsi_14"] = round(float(rsi.iloc[-1]), 2)
        else:
            factors["rsi_14"] = None
    else:
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi_v = (100 - 100 / (1 + rs)).iloc[-1]
        factors["rsi_14"] = round(float(rsi_v), 2) if not np.isnan(rsi_v) else None

    # ── MACD ───────────────────────────────────────────────────────────────
    if len(close) >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        sig   = macd.ewm(span=9, adjust=False).mean()
        factors["macd"]          = round(float(macd.iloc[-1]), 5)
        factors["macd_signal"]   = round(float(sig.iloc[-1]),  5)
        factors["macd_hist"]     = round(float((macd - sig).iloc[-1]), 5)

    # ── Bollinger Bands ────────────────────────────────────────────────────
    if len(close) >= 20:
        bb_ma  = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_up  = bb_ma + 2 * bb_std
        bb_lo  = bb_ma - 2 * bb_std
        prc    = close.iloc[-1]
        factors["bb_position"] = round(float((prc - bb_lo.iloc[-1]) /
                                              (bb_up.iloc[-1] - bb_lo.iloc[-1] + 1e-8)), 4)
        factors["bb_upper"] = round(float(bb_up.iloc[-1]), 4)
        factors["bb_lower"] = round(float(bb_lo.iloc[-1]), 4)

    # ── Beta vs market ─────────────────────────────────────────────────────
    if _HAS_YF and not _is_ashare(symbol) and len(ret) >= 60:
        try:
            bench = yf.Ticker("SPY").history(period=period)["Close"].pct_change().dropna()
            aligned = pd.DataFrame({"asset": ret, "bench": bench}).dropna().tail(60)
            if len(aligned) >= 30:
                beta_v = float(np.cov(aligned["asset"], aligned["bench"])[0, 1] /
                                np.var(aligned["bench"]))
                factors["beta_60d"] = round(beta_v, 3)
        except Exception:
            pass

    # ── Trend score ────────────────────────────────────────────────────────
    trend = 0
    for ma_key, w in [("ma_5_gap", 0.15), ("ma_20_gap", 0.35), ("ma_60_gap", 0.50)]:
        v = factors.get(ma_key, 0.0) or 0.0
        trend += w * (1 if v > 0 else -1)
    factors["trend_score"] = round(trend, 3)

    factors["provider"] = "local"
    return {"success": True, **factors}


# ---------------------------------------------------------------------------
# 5. backtest_strategy
# ---------------------------------------------------------------------------

def _backtest_strategy(params: dict) -> dict:
    """
    Simple backtest engine.  Supports:
      strategy = "sma_cross" | "rsi_mean_revert" | "momentum" | "buy_hold"
    """
    symbol   = params.get("symbol", "AAPL")
    strategy = params.get("strategy", "sma_cross").lower().replace(" ", "_").replace("-", "_")
    start    = params.get("start", _parse_date(None, 3 * 365))
    end      = params.get("end", _today())
    fast     = int(params.get("fast_period", 20))
    slow     = int(params.get("slow_period", 60))
    rsi_lo   = float(params.get("rsi_oversold", 30))
    rsi_hi   = float(params.get("rsi_overbought", 70))
    mom_n    = int(params.get("momentum_period", 20))

    # Fetch data
    if _is_ashare(symbol) and _HAS_AK:
        norm = _normalise_ashare(symbol)
        code = norm[2:]
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=start.replace("-", ""),
                                 end_date=end.replace("-", ""),
                                 adjust="qfq")
        df = df.rename(columns={"收盘": "Close", "成交量": "Volume",
                                 "日期": "Date", "开盘": "Open",
                                 "最高": "High", "最低": "Low"})
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
    elif _HAS_YF:
        df = yf.Ticker(symbol).history(start=start, end=end)
    else:
        return {"success": False, "error": "No data source available"}

    if df is None or len(df) < max(slow, 60):
        return {"success": False, "error": f"Insufficient data: {len(df) if df is not None else 0} bars"}

    close = df["Close"].astype(float)

    # ── Signal generation ─────────────────────────────────────────────────
    if strategy in ("sma_cross", "ma_cross"):
        ma_f = close.rolling(fast).mean()
        ma_s = close.rolling(slow).mean()
        signal = (ma_f > ma_s).astype(int)

    elif strategy in ("rsi_mean_revert", "rsi"):
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        signal = pd.Series(0, index=rsi.index)
        in_pos = False
        for i in range(len(rsi)):
            r = rsi.iloc[i]
            if np.isnan(r):
                signal.iloc[i] = 0
            elif r < rsi_lo and not in_pos:
                in_pos = True
                signal.iloc[i] = 1
            elif r > rsi_hi and in_pos:
                in_pos = False
                signal.iloc[i] = 0
            else:
                signal.iloc[i] = int(in_pos)

    elif strategy in ("momentum", "mom"):
        ret_n  = close.pct_change(mom_n)
        signal = (ret_n > 0).astype(int)

    else:  # buy_hold
        signal = pd.Series(1, index=close.index)

    # ── Vectorbt backtest (if available) ─────────────────────────────────
    if _HAS_VBT:
        try:
            pf = vbt.Portfolio.from_signals(
                close, signal.shift(1).fillna(0) == 1,
                signal.shift(1).fillna(0) == 0,
                freq="D",
            )
            stats = pf.stats()
            return {
                "success":          True,
                "symbol":           symbol,
                "strategy":         strategy,
                "start":            str(df.index[0].date()),
                "end":              str(df.index[-1].date()),
                "bars":             len(df),
                "total_return":     round(float(stats.get("Total Return [%]", 0) / 100), 4),
                "annual_return":    round(float(stats.get("Annualized Return [%]", 0) / 100), 4),
                "sharpe_ratio":     round(float(stats.get("Sharpe Ratio", 0) or 0), 3),
                "sortino_ratio":    round(float(stats.get("Sortino Ratio", 0) or 0), 3),
                "max_drawdown":     round(float(stats.get("Max Drawdown [%]", 0) / 100), 4),
                "win_rate":         round(float(stats.get("Win Rate [%]", 0) / 100), 3),
                "total_trades":     int(stats.get("Total Trades", 0) or 0),
                "provider":         "vectorbt",
            }
        except Exception as exc:
            logger.debug("vectorbt failed, falling back to manual: %s", exc)

    # ── Manual pandas backtest fallback ──────────────────────────────────
    sig      = signal.shift(1).fillna(0)
    ret      = close.pct_change().fillna(0)
    port_ret = (ret * sig).fillna(0)
    equity   = (1 + port_ret).cumprod()

    total_r   = float(equity.iloc[-1] - 1)
    n_years   = max((df.index[-1] - df.index[0]).days / 365.25, 0.01)
    annual_r  = float((1 + total_r) ** (1 / n_years) - 1)
    rf        = 0.04  # risk-free rate
    excess    = port_ret - rf / 252
    sharpe    = float(excess.mean() / port_ret.std() * np.sqrt(252)) if port_ret.std() > 0 else 0.0
    neg_ret   = port_ret[port_ret < 0]
    sortino   = float(excess.mean() / neg_ret.std() * np.sqrt(252)) if len(neg_ret) > 0 and neg_ret.std() > 0 else 0.0

    # Max drawdown
    peak    = equity.cummax()
    dd      = (equity - peak) / peak
    max_dd  = float(dd.min())

    # Trade stats
    trades     = (sig.diff() != 0) & (sig == 1)
    exits      = (sig.diff() != 0) & (sig == 0)
    n_trades   = int(trades.sum())
    trade_rets = []
    entry_date = None
    entry_price = None
    for date, row in sig.items():
        if row == 1 and entry_price is None:
            entry_price = float(close.loc[date])
            entry_date  = date
        elif row == 0 and entry_price is not None:
            trade_rets.append(float(close.loc[date]) / entry_price - 1)
            entry_price = None

    win_rate = sum(1 for r in trade_rets if r > 0) / len(trade_rets) if trade_rets else 0.0

    # Benchmark: buy-and-hold
    bh_ret  = float(close.iloc[-1] / close.iloc[0] - 1)

    return {
        "success":          True,
        "symbol":           symbol,
        "strategy":         strategy,
        "start":            str(df.index[0].date()),
        "end":              str(df.index[-1].date()),
        "bars":             len(df),
        "total_return":     round(total_r, 4),
        "annual_return":    round(annual_r, 4),
        "sharpe_ratio":     round(sharpe, 3),
        "sortino_ratio":    round(sortino, 3),
        "max_drawdown":     round(max_dd, 4),
        "win_rate":         round(win_rate, 3),
        "total_trades":     n_trades,
        "benchmark_return": round(bh_ret, 4),
        "alpha":            round(annual_r - bh_ret / n_years, 4),
        "provider":         "pandas",
    }


# ---------------------------------------------------------------------------
# 6. get_risk_metrics
# ---------------------------------------------------------------------------

def _get_risk_metrics(params: dict) -> dict:
    symbol     = params.get("symbol", "AAPL")
    period     = params.get("period", "1y")
    conf_level = float(params.get("confidence", 0.95))

    if _is_ashare(symbol) and _HAS_AK:
        data = _get_ashare_data(symbol, _parse_date(None, 365), _today())
        if not data.get("success"):
            return data
        norm = _normalise_ashare(symbol)
        code = norm[2:]
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                 start_date=_parse_date(None, 365).replace("-", ""),
                                 end_date=_today().replace("-", ""),
                                 adjust="qfq")
        df = df.rename(columns={"收盘": "Close"})
        close = df["Close"].astype(float)
    elif _HAS_YF:
        close = yf.Ticker(symbol).history(period=period)["Close"].astype(float)
    else:
        return {"success": False, "error": "No data source"}

    if close is None or len(close) < 30:
        return {"success": False, "error": "Insufficient data"}

    ret  = close.pct_change().dropna()
    mu   = float(ret.mean())
    sig  = float(ret.std())

    # VaR (parametric)
    if _HAS_SCIPY:
        var_daily = float(-sp_stats.norm.ppf(1 - conf_level, mu, sig))
    else:
        var_daily = float(-np.percentile(ret, (1 - conf_level) * 100))

    var_monthly = var_daily * np.sqrt(21)

    # CVaR (Expected Shortfall)
    losses = -ret
    cvar   = float(losses[losses >= var_daily].mean())

    # Max drawdown
    equity = (1 + ret).cumprod()
    peak   = equity.cummax()
    dd     = (equity - peak) / peak
    max_dd = float(dd.min())

    # Calmar
    annual_ret = mu * 252
    calmar     = annual_ret / abs(max_dd) if max_dd != 0 else 0.0

    # Downside deviation (Sortino denominator)
    neg_ret     = ret[ret < 0]
    down_dev    = float(neg_ret.std() * np.sqrt(252)) if len(neg_ret) > 0 else 0.0

    # Skewness / kurtosis
    skew_v = float(ret.skew()) if _HAS_SCIPY else 0.0
    kurt_v = float(ret.kurtosis()) if _HAS_SCIPY else 0.0

    return {
        "success":             True,
        "symbol":              symbol,
        "confidence_level":    conf_level,
        "var_daily":           round(var_daily, 5),
        "var_monthly":         round(float(var_monthly), 5),
        "cvar_daily":          round(cvar, 5),
        "max_drawdown":        round(max_dd, 5),
        "annual_volatility":   round(float(sig * np.sqrt(252)), 5),
        "annual_return":       round(float(annual_ret), 5),
        "sharpe_ratio":        round(float((annual_ret - 0.04) / (sig * np.sqrt(252))), 3) if sig > 0 else 0.0,
        "calmar_ratio":        round(float(calmar), 3),
        "downside_deviation":  round(down_dev, 5),
        "skewness":            round(skew_v, 3),
        "kurtosis":            round(kurt_v, 3),
        "provider":            "local",
    }


# ---------------------------------------------------------------------------
# 7. optimize_positions
# ---------------------------------------------------------------------------

def _optimize_positions(params: dict) -> dict:
    symbols  = params.get("symbols", ["AAPL", "MSFT", "GOOGL"])
    period   = params.get("period", "1y")
    method   = params.get("method", "max_sharpe")  # max_sharpe | min_var | equal_weight
    rf       = float(params.get("risk_free_rate", 0.04))

    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]

    # Fetch returns
    prices = {}
    for sym in symbols:
        if _is_ashare(sym) and _HAS_AK:
            norm = _normalise_ashare(sym)
            code = norm[2:]
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                     start_date=_parse_date(None, 365).replace("-", ""),
                                     end_date=_today().replace("-", ""),
                                     adjust="qfq")
            df = df.rename(columns={"收盘": "Close"})
            prices[sym] = df["Close"].astype(float).values
        elif _HAS_YF:
            prices[sym] = yf.Ticker(sym).history(period=period)["Close"].astype(float).values

    if not prices:
        return {"success": False, "error": "Could not fetch prices"}

    # Align length
    min_len = min(len(v) for v in prices.values())
    ret_mat = np.column_stack([prices[s][-min_len:] for s in symbols])
    ret_mat = np.diff(np.log(ret_mat), axis=0)

    mu_vec  = ret_mat.mean(axis=0) * 252
    cov_mat = np.cov(ret_mat.T) * 252
    n       = len(symbols)

    if method == "equal_weight":
        weights = np.ones(n) / n
    elif method == "min_var":
        # Analytical min-variance
        try:
            inv_cov = np.linalg.inv(cov_mat + 1e-8 * np.eye(n))
            ones    = np.ones(n)
            w       = inv_cov @ ones
            weights = w / w.sum()
        except np.linalg.LinAlgError:
            weights = np.ones(n) / n
    else:  # max_sharpe — gradient-free grid search
        best_sharpe = -np.inf
        weights     = np.ones(n) / n
        rng         = np.random.default_rng(42)
        for _ in range(10000):
            w_try  = rng.dirichlet(np.ones(n))
            p_ret  = float(w_try @ mu_vec)
            p_vol  = float(np.sqrt(w_try @ cov_mat @ w_try))
            sharpe = (p_ret - rf) / p_vol if p_vol > 0 else -np.inf
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                weights     = w_try

    # Portfolio metrics
    p_ret  = float(weights @ mu_vec)
    p_vol  = float(np.sqrt(weights @ cov_mat @ weights))
    sharpe = (p_ret - rf) / p_vol if p_vol > 0 else 0.0

    return {
        "success":          True,
        "method":           method,
        "symbols":          symbols,
        "weights":          {sym: round(float(w), 4) for sym, w in zip(symbols, weights)},
        "portfolio_return": round(p_ret, 4),
        "portfolio_vol":    round(p_vol, 4),
        "sharpe_ratio":     round(sharpe, 3),
        "provider":         "local",
    }


# ---------------------------------------------------------------------------
# 8. get_sector_performance (A股 + US)
# ---------------------------------------------------------------------------

def _get_sector_performance(params: dict) -> dict:
    market = params.get("market", "cn").lower()  # cn | us

    if market == "cn" and _HAS_AK:
        try:
            df = _ak_retry(ak.stock_board_industry_name_em)
            if df is None or df.empty:
                raise ValueError("empty sector data")
            df = df.rename(columns={
                "板块名称": "sector", "最新价": "price",
                "涨跌幅": "change_pct", "成交额": "amount",
                "上涨家数": "rising", "下跌家数": "falling",
            })
            top    = df.nlargest(5,  "change_pct")[["sector", "change_pct"]].to_dict("records")
            bottom = df.nsmallest(5, "change_pct")[["sector", "change_pct"]].to_dict("records")
            return {
                "success":   True,
                "market":    "cn",
                "date":      _today(),
                "top_sectors":    top,
                "bottom_sectors": bottom,
                "total_sectors":  len(df),
                "provider":  "akshare",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    elif _HAS_YF:
        sector_etfs = {
            "Technology":    "XLK",  "Healthcare":    "XLV",
            "Financials":    "XLF",  "Consumer Disc": "XLY",
            "Industrials":   "XLI",  "Energy":        "XLE",
            "Utilities":     "XLU",  "Real Estate":   "XLRE",
            "Materials":     "XLB",  "Comm Services": "XLC",
            "Consumer Staples": "XLP",
        }
        perf = []
        for name, etf in sector_etfs.items():
            try:
                h = yf.Ticker(etf).history(period="5d")
                if len(h) >= 2:
                    chg = (h["Close"].iloc[-1] - h["Close"].iloc[-2]) / h["Close"].iloc[-2] * 100
                    perf.append({"sector": name, "etf": etf, "change_pct": round(float(chg), 2)})
            except Exception:
                pass
        perf.sort(key=lambda x: x["change_pct"], reverse=True)
        return {
            "success":       True,
            "market":        "us",
            "date":          _today(),
            "sectors":       perf,
            "top_sectors":   perf[:3],
            "bottom_sectors": perf[-3:],
            "provider":      "yfinance",
        }

    return {"success": False, "error": "akshare / yfinance not available: 运行 pip install akshare yfinance 或 /install akshare yfinance"}


# ---------------------------------------------------------------------------
# 9. A股 northbound fund flow (北向资金)
# ---------------------------------------------------------------------------

def _get_northbound_flow(params: dict) -> dict:
    if not _HAS_AK:
        return {"success": False, "error": "akshare not installed: 运行 pip install akshare 或 /install akshare"}
    try:
        # stock_hsgt_fund_flow_summary_em returns today's 沪深港通 summary.
        # 成交净买额 is already in 亿元 — no further scaling needed.
        df = _ak_retry(ak.stock_hsgt_fund_flow_summary_em)
        north = df[df["资金方向"] == "北向"]
        if north.empty:
            return {"success": False, "error": "No northbound data in response"}
        sh_flow = float(north[north["板块"] == "沪股通"]["成交净买额"].sum())
        sz_flow = float(north[north["板块"] == "深股通"]["成交净买额"].sum())
        total   = round(sh_flow + sz_flow, 2)
        return {
            "success":           True,
            "latest_net_buy_yi": total,
            "sh_net_buy_yi":     round(sh_flow, 2),
            "sz_net_buy_yi":     round(sz_flow, 2),
            "total_net_buy_yi":  total,
            "trend":             "inflow" if total > 0 else "outflow",
            "provider":          "akshare",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 10. screen_ashare — 选股筛选器
# ---------------------------------------------------------------------------

def _screen_ashare(params: dict) -> dict:
    """Screen A-share stocks by fundamental & technical criteria."""
    min_roe         = float(params.get("min_roe", 10))
    max_pe          = float(params.get("max_pe", 50))
    min_revenue_gr  = float(params.get("min_revenue_growth", 10))
    min_market_cap  = float(params.get("min_market_cap_yi", 0))    # 亿元
    limit           = int(params.get("limit", 20))

    # Primary: direct eastmoney clist (host-rotating, proxy-resilient, small
    # paged query). Far more reliable than akshare's full-market spot endpoint.
    try:
        from aria_code.market_data_client import screen_ashare as _em_screen
        _em = _em_screen(max_pe=max_pe, min_market_cap_yi=min_market_cap, limit=limit)
        if _em.get("success") and _em.get("stocks"):
            _em["criteria"] = params
            return _em
    except Exception:
        pass  # fall through to akshare

    if not _HAS_AK:
        return {"success": False, "error": "akshare not installed: 运行 pip install akshare 或 /install akshare"}

    try:
        # A股实时行情
        df = _ak_retry(ak.stock_zh_a_spot_em)
        if df is None or df.empty:
            return {"success": False, "error": "No spot data"}

        df = df.rename(columns={
            "代码": "code", "名称": "name", "最新价": "price",
            "涨跌幅": "change_pct", "总市值": "market_cap",
            "市盈率-动态": "pe_dynamic", "市净率": "pb",
            "成交量": "volume", "换手率": "turnover_rate",
        })

        # Basic filters
        df = df[~df["name"].str.contains("ST|退", na=False)]
        df = df[df["price"].notna() & (df["price"] > 0)]

        if "pe_dynamic" in df.columns:
            df = df[df["pe_dynamic"].between(0.1, max_pe, inclusive="both")]

        if "market_cap" in df.columns and min_market_cap > 0:
            df = df[df["market_cap"] >= min_market_cap * 1e8]

        # Score on momentum
        if "change_pct" in df.columns:
            df["score"] = df["change_pct"].fillna(0)
            df = df.nlargest(limit, "score")

        cols = ["code", "name", "price", "change_pct", "pe_dynamic", "pb",
                "market_cap", "turnover_rate"]
        cols = [c for c in cols if c in df.columns]
        result_df = df[cols].head(limit)

        # Format market_cap to 亿元
        if "market_cap" in result_df.columns:
            result_df = result_df.copy()
            result_df["market_cap_yi"] = (result_df["market_cap"] / 1e8).round(1)

        stocks = result_df.to_dict("records")
        return {
            "success":      True,
            "count":        len(stocks),
            "stocks":       stocks,
            "criteria":     params,
            "provider":     "akshare",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 11. get_limit_up_pool — 涨停板池
# ---------------------------------------------------------------------------

def _get_limit_up_pool(params: dict) -> dict:
    date = params.get("date", _today())
    if not _HAS_AK:
        return {"success": False, "error": "akshare not installed: 运行 pip install akshare 或 /install akshare"}
    try:
        date_str = date.replace("-", "")
        df = _ak_retry(ak.stock_zt_pool_em, date=date_str)
        if df is None or df.empty:
            return {"success": True, "count": 0, "stocks": [], "date": date}

        df = df.rename(columns={
            "代码": "code", "名称": "name",
            "涨停统计": "limit_streak", "连续涨停": "consecutive",
            "首次封板时间": "first_lock_time", "涨停类型": "limit_type",
        })
        cols = [c for c in ["code", "name", "limit_streak", "consecutive",
                             "first_lock_time", "limit_type"] if c in df.columns]
        return {
            "success":  True,
            "date":     date,
            "count":    len(df),
            "stocks":   df[cols].head(50).to_dict("records"),
            "provider": "akshare",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 12. get_market_indices
# ---------------------------------------------------------------------------

def _get_market_indices(params: dict) -> dict:
    indices = {
        # US
        "S&P 500": "^GSPC", "NASDAQ": "^IXIC", "Dow Jones": "^DJI",
        "VIX": "^VIX", "Russell 2000": "^RUT",
        # CN
        "上证综指": "000001.SS", "深证成指": "399001.SZ", "创业板": "399006.SZ",
        # Global
        "Nikkei 225": "^N225", "FTSE 100": "^FTSE", "DAX": "^GDAXI",
        "Hang Seng": "^HSI",
        # Commodities
        "Gold": "GC=F", "Crude Oil": "CL=F", "Bitcoin": "BTC-USD",
    }

    if not _HAS_YF:
        if _HAS_AK:
            # A股 indices only
            try:
                df = ak.stock_zh_index_spot_em()
                return {"success": True, "indices": df.head(10).to_dict("records"), "provider": "akshare"}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    results = []
    for name, ticker in indices.items():
        try:
            h = yf.Ticker(ticker).history(period="2d")
            if len(h) >= 1:
                latest = float(h["Close"].iloc[-1])
                chg    = (float(h["Close"].iloc[-1]) - float(h["Close"].iloc[0])) / float(h["Close"].iloc[0]) * 100 if len(h) >= 2 else 0.0
                results.append({"name": name, "ticker": ticker,
                                 "price": round(latest, 2), "change_pct": round(chg, 2)})
        except Exception:
            pass

    return {"success": True, "indices": results, "date": _today(), "provider": "yfinance"}


# ---------------------------------------------------------------------------
# 13. analyze_news (local sentiment via keyword scoring)
# ---------------------------------------------------------------------------

def _get_data_key(service: str) -> str:
    """Read a data service API key from env var or ~/.arthera/providers.json."""
    _DATA_ENV = {
        "finnhub":      "FINNHUB_API_KEY",
        "newsapi":      "NEWS_API_KEY",
        "brave":        "BRAVE_SEARCH_API_KEY",
        "alphavantage": "ALPHA_VANTAGE_API_KEY",
        "coingecko":    "COINGECKO_API_KEY",
        "twelvedata":   "TWELVEDATA_API_KEY",
    }
    env_var = _DATA_ENV.get(service, "")
    if env_var:
        val = os.getenv(env_var, "")
        if val:
            return val
    # Fall back to providers.json
    try:
        import pathlib as _pl
        import json as _json
        pf = aria_home() / "providers.json"
        if pf.exists():
            raw = _json.loads(pf.read_text(encoding="utf-8"))
            entry = raw.get("data", {}).get(service, {})
            if entry.get("api_key"):
                return entry["api_key"]
    except Exception:
        pass
    return ""


def _fetch_news_finnhub(symbol: str, limit: int) -> list:
    """Fetch stock news from Finnhub API."""
    key = _get_data_key("finnhub")
    if not key:
        return []
    try:
        import requests as _req
        import datetime as _dt
        end_dt   = _dt.date.today().isoformat()
        start_dt = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
        url = (f"https://finnhub.io/api/v1/company-news"
               f"?symbol={symbol}&from={start_dt}&to={end_dt}&token={key}")
        resp = _req.get(url, timeout=8)
        if resp.status_code == 200:
            items = resp.json()[:limit]
            return [{"title": a.get("headline", ""), "source": a.get("source", ""),
                     "time": str(a.get("datetime", "")), "url": a.get("url", "")}
                    for a in items]
    except Exception:
        pass
    return []


def _fetch_news_newsapi(query: str, limit: int) -> list:
    """Fetch news from NewsAPI.org."""
    key = _get_data_key("newsapi")
    if not key:
        return []
    try:
        import requests as _req
        url = (f"https://newsapi.org/v2/everything"
               f"?q={query}&language=en&sortBy=publishedAt&pageSize={limit}&apiKey={key}")
        resp = _req.get(url, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            return [{"title": a.get("title", ""), "source": a.get("source", {}).get("name", ""),
                     "time": a.get("publishedAt", "")[:10], "url": a.get("url", "")}
                    for a in data.get("articles", [])[:limit]]
    except Exception:
        pass
    return []


def _analyze_news(params: dict) -> dict:
    symbol  = params.get("symbol", "")
    query   = params.get("query", params.get("topic", symbol))
    topic   = query or symbol
    limit   = int(params.get("limit", 5))

    # ── 1. A股 news via akshare (no key needed) ───────────────────────────────
    if _HAS_AK and topic and _is_ashare(topic):
        try:
            norm = _normalise_ashare(topic) if _is_ashare(topic) else topic
            code = norm[2:] if len(norm) > 2 else norm
            df   = ak.stock_news_em(symbol=code)
            if df is not None and not df.empty:
                df = df.head(limit)
                news_list = []
                for _, row in df.iterrows():
                    title = str(row.get("新闻标题", row.get("title", "")))
                    score = _score_sentiment(title)
                    news_list.append({
                        "title":     title,
                        "time":      str(row.get("发布时间", "")),
                        "sentiment": "positive" if score > 0 else ("negative" if score < 0 else "neutral"),
                        "score":     score,
                    })
                avg_score = sum(n["score"] for n in news_list) / len(news_list) if news_list else 0
                return {
                    "success": True, "symbol": topic, "news": news_list,
                    "overall_sentiment": "positive" if avg_score > 0.1 else ("negative" if avg_score < -0.1 else "neutral"),
                    "avg_score": round(avg_score, 3), "provider": "akshare",
                }
        except Exception as exc:
            pass  # fall through to other providers

    # ── 2. Finnhub (if key available) ────────────────────────────────────────
    if symbol and not _is_ashare(symbol):
        articles = _fetch_news_finnhub(symbol.upper(), limit)
        if articles:
            news_list = []
            for a in articles:
                score = _score_sentiment(a["title"])
                news_list.append({**a, "sentiment": "positive" if score > 0 else ("negative" if score < 0 else "neutral"), "score": score})
            avg_score = sum(n["score"] for n in news_list) / len(news_list) if news_list else 0
            return {
                "success": True, "symbol": symbol, "news": news_list,
                "overall_sentiment": "positive" if avg_score > 0.1 else ("negative" if avg_score < -0.1 else "neutral"),
                "avg_score": round(avg_score, 3), "provider": "finnhub",
            }

    # ── 3. NewsAPI (if key available) ─────────────────────────────────────────
    search_query = topic or symbol or "market"
    articles = _fetch_news_newsapi(search_query, limit)
    if articles:
        news_list = []
        for a in articles:
            score = _score_sentiment(a["title"])
            news_list.append({**a, "sentiment": "positive" if score > 0 else ("negative" if score < 0 else "neutral"), "score": score})
        avg_score = sum(n["score"] for n in news_list) / len(news_list) if news_list else 0
        return {
            "success": True, "symbol": topic, "news": news_list,
            "overall_sentiment": "positive" if avg_score > 0.1 else ("negative" if avg_score < -0.1 else "neutral"),
            "avg_score": round(avg_score, 3), "provider": "newsapi",
        }

    # ── 4. yfinance news (free, no key, US/HK/global stocks) ─────────────────
    yf_sym = symbol.upper() if symbol else ""
    if yf_sym and not _is_ashare(yf_sym):
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_sym)
            raw_news = ticker.news or []
            news_list = []
            for item in raw_news[:limit]:
                content = item.get("content", {})
                title = (
                    content.get("title")
                    or item.get("title", "")
                )
                pub = (
                    content.get("pubDate")
                    or item.get("providerPublishTime", "")
                )
                url = (
                    content.get("canonicalUrl", {}).get("url")
                    or item.get("link", "")
                )
                provider = (
                    content.get("provider", {}).get("displayName")
                    or item.get("publisher", "")
                )
                if not title:
                    continue
                score = _score_sentiment(title)
                news_list.append({
                    "title":     title,
                    "time":      str(pub),
                    "url":       url,
                    "publisher": provider,
                    "sentiment": "positive" if score > 0 else ("negative" if score < 0 else "neutral"),
                    "score":     score,
                })
            if news_list:
                avg_score = sum(n["score"] for n in news_list) / len(news_list)
                return {
                    "success": True, "symbol": yf_sym, "news": news_list,
                    "overall_sentiment": "positive" if avg_score > 0.1 else ("negative" if avg_score < -0.1 else "neutral"),
                    "avg_score": round(avg_score, 3), "provider": "yfinance",
                }
        except Exception:
            pass

    # ── 5. web_search fallback — search "[symbol] news" ──────────────────────
    ws_query = f"{topic or symbol} stock news latest" if (topic or symbol) else ""
    if ws_query:
        ws_result = _web_search({"query": ws_query, "max_results": limit})
        if ws_result.get("success") and ws_result.get("results"):
            news_list = []
            for item in ws_result["results"]:
                title = item.get("title", "")
                score = _score_sentiment(title)
                news_list.append({
                    "title":     title,
                    "url":       item.get("url", ""),
                    "publisher": item.get("source", ""),
                    "sentiment": "positive" if score > 0 else ("negative" if score < 0 else "neutral"),
                    "score":     score,
                })
            if news_list:
                avg_score = sum(n["score"] for n in news_list) / len(news_list)
                return {
                    "success": True, "symbol": topic or symbol, "news": news_list,
                    "overall_sentiment": "positive" if avg_score > 0.1 else ("negative" if avg_score < -0.1 else "neutral"),
                    "avg_score": round(avg_score, 3), "provider": ws_result.get("provider", "web_search"),
                }

    # ── 6. No data available ──────────────────────────────────────────────────
    tip = "配置数据服务 key: /apikey set finnhub <key> 或 /apikey set newsapi <key>；或设置 BRAVE_SEARCH_API_KEY 启用网页搜索"
    return {"success": False, "error": tip}


def _score_sentiment(text: str) -> float:
    """Keyword-based sentiment scorer (0.0 = neutral, +1 = very positive, -1 = very negative)."""
    pos = ["上涨", "涨停", "突破", "创新高", "利好", "增长", "盈利", "bull", "beat", "growth", "record", "profit", "rally", "buy", "upgrade"]
    neg = ["下跌", "跌停", "亏损", "利空", "减少", "违规", "被罚", "风险", "bear", "miss", "loss", "decline", "sell", "downgrade", "fraud"]
    t   = text.lower()
    score = sum(1 for w in pos if w in t) - sum(1 for w in neg if w in t)
    return float(max(-1, min(1, score * 0.25)))


# ---------------------------------------------------------------------------
# 14. get_commodities_data — gold, silver, oil, gas, copper, wheat, etc.
# ---------------------------------------------------------------------------

# Common commodity keywords → yfinance futures tickers
_COMMODITY_MAP: Dict[str, str] = {
    # Precious metals
    "gold":       "GC=F",   "silver":     "SI=F",   "platinum":   "PL=F",   "palladium":  "PA=F",
    # Energy
    "oil":        "CL=F",   "crude":      "CL=F",   "crude oil":  "CL=F",
    "brent":      "BZ=F",   "natural gas": "NG=F",  "natgas":     "NG=F",   "gas":        "NG=F",
    "gasoline":   "RB=F",   "heating oil": "HO=F",
    # Base metals
    "copper":     "HG=F",   "aluminum":   "ALI=F",  "nickel":     "NI=F",   "zinc":       "ZNC=F",
    # Agricultural
    "wheat":      "ZW=F",   "corn":       "ZC=F",   "soybean":    "ZS=F",   "soybeans":   "ZS=F",
    "coffee":     "KC=F",   "cocoa":      "CC=F",   "sugar":      "SB=F",   "cotton":     "CT=F",
    "rice":       "ZR=F",   "oats":       "ZO=F",
    # Livestock
    "cattle":     "LE=F",   "hogs":       "HE=F",
    # Softs / other
    "lumber":     "LBS=F",  "rubber":     "rubber", "iron ore":   "TIO=F",
    # China-traded (A股 futures) — map to closest international proxy
    "螺纹钢":     "HG=F",   "铁矿石":     "TIO=F",  "铜":         "HG=F",
    "黄金":       "GC=F",   "白银":       "SI=F",   "原油":       "CL=F",   "天然气":     "NG=F",
}


def _get_commodities_data(params: dict) -> dict:
    """Commodity spot/futures price lookup via yfinance."""
    commodity = str(params.get("commodity", "gold")).strip().lower()

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: pip install yfinance"}

    # Resolve ticker
    ticker = _COMMODITY_MAP.get(commodity)
    if ticker is None:
        # Try direct uppercase (user may have passed ticker like GC=F)
        ticker = commodity.upper()

    try:
        tkr  = yf.Ticker(ticker)
        hist = tkr.history(period="5d")
        if hist.empty:
            # Fallback: try without =F suffix
            alt = commodity.upper().replace("=F", "") + "=F"
            hist = yf.Ticker(alt).history(period="5d")
            if hist.empty:
                return {
                    "success": False,
                    "error":   f"No data for commodity '{commodity}' (ticker={ticker}). "
                               "Try using the yfinance ticker directly, e.g. GC=F for gold.",
                }
            ticker = alt

        latest   = float(hist["Close"].iloc[-1])
        prev     = float(hist["Close"].iloc[-2]) if len(hist) > 1 else latest
        chg      = (latest - prev) / prev * 100 if prev else 0.0
        high_5d  = float(hist["High"].max())
        low_5d   = float(hist["Low"].min())
        vol_5d   = float(hist["Volume"].mean())

        # Longer term context
        hist_1y  = tkr.history(period="1y")
        high_52w = float(hist_1y["High"].max())  if not hist_1y.empty else None
        low_52w  = float(hist_1y["Low"].min())   if not hist_1y.empty else None
        ret_1y   = float((hist_1y["Close"].iloc[-1] / hist_1y["Close"].iloc[0] - 1)) \
                   if len(hist_1y) > 1 else None

        info      = tkr.fast_info
        currency  = getattr(info, "currency", "USD")

        return {
            "success":       True,
            "commodity":     commodity,
            "ticker":        ticker,
            "latest_price":  round(latest, 4),
            "change_pct":    round(chg, 3),
            "currency":      currency,
            "high_5d":       round(high_5d, 4),
            "low_5d":        round(low_5d, 4),
            "volume_5d_avg": int(vol_5d),
            "high_52w":      round(high_52w, 4) if high_52w else None,
            "low_52w":       round(low_52w, 4) if low_52w else None,
            "return_1y":     round(ret_1y, 4) if ret_1y is not None else None,
            "pct_from_52w_high": round((latest / high_52w - 1), 4) if high_52w else None,
            "provider":      "yfinance",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 15. get_futures_data — generic futures via yfinance
# ---------------------------------------------------------------------------

def _get_futures_data(params: dict) -> dict:
    """Futures contract data (equity index futures, VIX, etc.)"""
    contract = str(params.get("contract", params.get("symbol", "ES=F"))).strip().upper()

    # Common index futures shortcuts
    _FUTURES_MAP = {
        "SP500": "ES=F", "SPX": "ES=F", "S&P": "ES=F",
        "NQ": "NQ=F", "NASDAQ": "NQ=F",
        "DOW": "YM=F", "DJIA": "YM=F",
        "RUSSELL": "RTY=F", "RUT": "RTY=F",
        "VIX": "^VIX",
        "NIKKEI": "NK=F", "DAX": "FDAX=F",
        "HSI": "HSI=F",
    }
    ticker = _FUTURES_MAP.get(contract, contract)
    if not ticker.endswith("=F") and not ticker.startswith("^"):
        ticker = ticker + "=F"

    return _get_market_data({"symbol": ticker, "period": "5d"})


# ---------------------------------------------------------------------------
# 15b. get_bonds_data (US Treasury yields via yfinance)
# ---------------------------------------------------------------------------

def _get_bonds_data(params: dict) -> dict:
    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}
    tickers = {
        "2Y": "^IRX", "5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX",
    }
    results = {}
    for tenor, sym in tickers.items():
        try:
            h = yf.Ticker(sym).history(period="5d")
            if not h.empty:
                results[tenor] = round(float(h["Close"].iloc[-1]), 3)
        except Exception:
            pass
    if not results:
        return {"success": False, "error": "Could not fetch yield data"}
    # Yield curve shape
    if "2Y" in results and "10Y" in results:
        results["10Y_2Y_spread"] = round(results["10Y"] - results["2Y"], 3)
        results["curve_shape"]   = "normal" if results["10Y_2Y_spread"] > 0 else "inverted"
    return {"success": True, "yields": results, "provider": "yfinance"}


# ---------------------------------------------------------------------------
# 16. get_ai_signal — Alibaba Cloud DeepSeek-powered signal
# ---------------------------------------------------------------------------

def _get_ai_signal(params: dict) -> dict:
    """
    AI trading signal from the Alibaba Cloud quant backend.
    Returns: action (BUY/SELL/HOLD), confidence, reasoning, stop_loss, take_profit.
    Falls back to calculate_factors when cloud is unavailable.
    """
    symbol = params.get("symbol", "600519")
    market = params.get("market", "CN" if _is_ashare(symbol) else "US")

    if _HAS_CLOUD:
        try:
            result = cloud_get_ai_signal_sync(symbol, market=market)
            if result and result.get("action"):
                return {"success": True, **result, "provider": "aliyun_cloud"}
        except Exception as exc:
            logger.debug("Cloud AI signal failed: %s", exc)

    # Local fallback: derive a simple signal from factors
    factors = _calculate_factors({"symbol": symbol})
    if not factors.get("success"):
        return factors

    rsi     = factors.get("rsi_14", 50)
    macd_h  = factors.get("macd_hist", 0) or 0
    trend   = factors.get("trend_score", 0) or 0
    vol_r   = factors.get("volume_ratio_20d", 1.0) or 1.0

    score = 0
    if rsi is not None:
        score += (0.4 if rsi < 40 else -0.4 if rsi > 70 else 0)
    score += (0.3 if macd_h > 0 else -0.3 if macd_h < 0 else 0)
    score += trend * 0.3

    action     = "BUY" if score > 0.3 else "SELL" if score < -0.3 else "HOLD"
    confidence = min(abs(score), 1.0)

    return {
        "success":    True,
        "symbol":     symbol,
        "action":     action,
        "confidence": round(confidence, 3),
        "reasoning":  f"RSI={rsi}, MACD_hist={macd_h:.5f}, trend={trend:.3f}, vol_ratio={vol_r:.2f}",
        "stop_loss":  None,
        "take_profit": None,
        "provider":   "local_fallback",
    }


# ---------------------------------------------------------------------------
# 17. get_market_insights — AI narrative analysis
# ---------------------------------------------------------------------------

def _get_market_insights(params: dict) -> dict:
    """
    Narrative market insights from Alibaba Cloud AI service.
    symbols: list of stock codes or comma-separated string.
    """
    raw = params.get("symbols", params.get("symbol", "sh600519"))
    if isinstance(raw, str):
        symbols = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    else:
        symbols = list(raw)
    market = params.get("market", "CN")

    if _HAS_CLOUD:
        try:
            from aria_code.aliyun_data_client import run_async
            result = run_async(AliyunDataClient.get().get_market_insights(symbols, market=market))
            if result:
                return {"success": True, **result, "provider": "aliyun_cloud"}
        except Exception as exc:
            logger.debug("Cloud market insights failed: %s", exc)

    # Local fallback: multi-stock factor summary
    summaries = []
    for sym in symbols[:5]:
        f = _calculate_factors({"symbol": sym})
        if f.get("success"):
            summaries.append({
                "symbol":      sym,
                "rsi_14":      f.get("rsi_14"),
                "trend_score": f.get("trend_score"),
                "macd_hist":   f.get("macd_hist"),
                "vol_ratio":   f.get("volume_ratio_20d"),
            })
    if not summaries:
        return {"success": False, "error": "Could not compute local factors"}

    return {
        "success":   True,
        "symbols":   symbols,
        "summaries": summaries,
        "note":      "Cloud AI not available — showing local factor summary",
        "provider":  "local_fallback",
    }


# ---------------------------------------------------------------------------
# 18. get_predictions — ML model predictions from cloud
# ---------------------------------------------------------------------------

def _get_predictions(params: dict) -> dict:
    """
    ML-powered stock return predictions from Alibaba Cloud QuantEngine.
    Falls back to momentum signal locally.
    """
    raw = params.get("symbols", params.get("symbol", "sh600519"))
    if isinstance(raw, str):
        symbols = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    else:
        symbols = list(raw)
    days   = int(params.get("prediction_days", 5))
    market = params.get("market", "CN")

    if _HAS_CLOUD:
        try:
            from aria_code.aliyun_data_client import run_async
            result = run_async(AliyunDataClient.get().get_predictions(symbols, prediction_days=days, market=market))
            if result and result.get("predictions"):
                return {"success": True, **result, "provider": "aliyun_cloud"}
        except Exception as exc:
            logger.debug("Cloud predictions failed: %s", exc)

    # Local fallback: simple momentum prediction
    preds = []
    for sym in symbols[:5]:
        f = _calculate_factors({"symbol": sym})
        if f.get("success"):
            r5  = f.get("return_5d", 0) or 0
            r20 = f.get("return_20d", 0) or 0
            predicted_return = round((r5 * 0.4 + r20 * 0.6) * (days / 20), 4)
            preds.append({
                "symbol":           sym,
                "predicted_return": predicted_return,
                "confidence":       0.5,
                "method":           "momentum",
            })

    if not preds:
        return {"success": False, "error": "No data for predictions"}

    return {
        "success":         True,
        "predictions":     preds,
        "prediction_days": days,
        "provider":        "local_fallback",
    }


# ---------------------------------------------------------------------------
# 19. cloud_backtest — advanced ML-powered backtest via Alibaba Cloud
# ---------------------------------------------------------------------------

def _cloud_backtest(params: dict) -> dict:
    """
    Full ML-powered backtest via Alibaba Cloud QuantEngine.
    Falls back to local pandas backtest when cloud is unavailable.
    """
    raw = params.get("symbols", params.get("symbol", "sh600519"))
    if isinstance(raw, str):
        symbols = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    else:
        symbols = list(raw)

    strategy_cfg = params.get("strategy_config", {
        "model_type":              params.get("model_type", "lightgbm"),
        "backtest_period_months":  params.get("months", 12),
        "rebalance_freq":          params.get("rebalance_freq", "weekly"),
        "top_k":                   params.get("top_k", 3),
        "use_enhanced_factors":    True,
        "use_dynamic_position":    True,
    })
    start  = params.get("start", "")
    end    = params.get("end", "")
    market = params.get("market", "CN")

    if _HAS_CLOUD:
        try:
            from aria_code.aliyun_data_client import run_async
            result = run_async(
                AliyunDataClient.get().run_backtest(
                    symbols, strategy_cfg,
                    start_date=start, end_date=end, market=market,
                )
            )
            if result and result.get("status") in ("completed", "running"):
                out = {"success": True, "provider": "aliyun_cloud"}
                r = result.get("result") or {}
                perf = r.get("performance") or {}
                out.update({
                    "backtest_id":     result.get("backtest_id"),
                    "status":          result.get("status"),
                    "total_return":    perf.get("total_return"),
                    "annual_return":   perf.get("annualized_return"),
                    "sharpe_ratio":    perf.get("sharpe_ratio"),
                    "max_drawdown":    perf.get("max_drawdown"),
                    "win_rate":        perf.get("win_rate"),
                    "total_trades":    perf.get("total_trades"),
                    "equity_curve":    r.get("equity_curve"),
                })
                return out
        except Exception as exc:
            logger.debug("Cloud backtest failed: %s", exc)

    # Local fallback: run simple pandas backtest for first symbol
    return _backtest_strategy({
        "symbol":   symbols[0] if symbols else "sh600519",
        "strategy": params.get("strategy", "sma_cross"),
        "start":    start or _parse_date(None, 730),
        "end":      end   or _today(),
    })


# ---------------------------------------------------------------------------
# Helper: format dataframe tail for display
# ---------------------------------------------------------------------------

def _df_tail(df: pd.DataFrame, n: int = 5) -> List[Dict]:
    cols = [c for c in ["date", "Close", "Open", "High", "Low", "Volume"]
            if c in df.columns]
    sub  = df[cols].tail(n)
    records = []
    for _, row in sub.iterrows():
        rec = {}
        for c in cols:
            v = row[c]
            try:
                if isinstance(v, (float, np.floating)):
                    rec[c] = round(float(v), 4)
                elif hasattr(v, "item"):
                    rec[c] = v.item()
                else:
                    rec[c] = str(v)
            except Exception:
                rec[c] = str(v)
        records.append(rec)
    return records



# OpenAI/Ollama tool schemas for local finance tools
LOCAL_FINANCE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": "Get stock/ETF quotes and OHLCV history. Supports US stocks (AAPL), A-share (600519 or sh600519), ETFs, indices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":   {"type": "string", "description": "Ticker symbol, e.g. AAPL, sh600519, BTC-USD"},
                    "period":   {"type": "string", "description": "yfinance period: 1d 5d 1mo 3mo 6mo 1y 2y 5y ytd max"},
                    "interval": {"type": "string", "description": "Bar interval: 1m 5m 15m 30m 1h 1d 1wk 1mo"},
                    "start":    {"type": "string", "description": "Start date YYYY-MM-DD (overrides period)"},
                    "end":      {"type": "string", "description": "End date YYYY-MM-DD"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_data",
            "description": "Cryptocurrency OHLCV data. Uses ccxt if available, else yfinance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":    {"type": "string", "description": "Pair, e.g. BTC/USDT or ETH/USDT"},
                    "exchange":  {"type": "string", "description": "Exchange name (binance, okx, bybit, coinbase)"},
                    "timeframe": {"type": "string", "description": "Candle timeframe: 1m 5m 1h 4h 1d"},
                    "limit":     {"type": "integer", "description": "Number of bars"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_factors",
            "description": "Compute technical and quantitative factors: RSI, MACD, Bollinger, MA gaps, volatility, trend score, beta.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol"},
                    "period": {"type": "string", "description": "Lookback period (yfinance format)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "backtest_strategy",
            "description": "Run a strategy backtest. Returns Sharpe, max drawdown, win rate, trade count, alpha vs benchmark.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":          {"type": "string"},
                    "strategy":        {"type": "string", "description": "sma_cross | rsi_mean_revert | momentum | buy_hold"},
                    "start":           {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "end":             {"type": "string", "description": "End date YYYY-MM-DD"},
                    "fast_period":     {"type": "integer", "description": "Fast MA period (default 20)"},
                    "slow_period":     {"type": "integer", "description": "Slow MA period (default 60)"},
                    "rsi_oversold":    {"type": "number",  "description": "RSI oversold threshold (default 30)"},
                    "rsi_overbought":  {"type": "number",  "description": "RSI overbought threshold (default 70)"},
                    "momentum_period": {"type": "integer", "description": "Momentum lookback bars (default 20)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_portfolio_backtest",
            "description": (
                "Event-driven, multi-asset backtest. Unlike backtest_strategy (single-symbol "
                "templates), this runs a strategy across a basket with next-open fills (no "
                "look-ahead), commission+slippage, and an equal-weight buy-and-hold benchmark. "
                "Returns total/annual return, Sharpe, max drawdown, win rate, trade count, and "
                "alpha vs buy-hold. Use for portfolio/multi-symbol strategy evaluation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols":       {"type": "string", "description": "One or more tickers, space/comma separated (e.g. 'AAPL MSFT 600519')"},
                    "strategy":      {"type": "string", "description": "sma_cross | buy_hold (default sma_cross)"},
                    "days":          {"type": "integer", "description": "Lookback window in days (default 365)"},
                    "fast":          {"type": "integer", "description": "Fast MA period for sma_cross (default 20)"},
                    "slow":          {"type": "integer", "description": "Slow MA period for sma_cross (default 60)"},
                    "starting_cash": {"type": "number",  "description": "Initial capital (default 100000)"},
                    "commission":    {"type": "number",  "description": "Per-trade commission rate (default 0.0005)"},
                    "slippage":      {"type": "number",  "description": "Per-trade slippage rate (default 0.0005)"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_metrics",
            "description": "Portfolio / stock risk metrics: VaR, CVaR, drawdown, Sharpe, Sortino, Calmar, skewness.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":     {"type": "string"},
                    "period":     {"type": "string", "description": "Data lookback (default 1y)"},
                    "confidence": {"type": "number",  "description": "VaR confidence level (default 0.95)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_positions",
            "description": "Portfolio weight optimisation using Markowitz / max-Sharpe / min-variance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols":          {"type": "array",  "items": {"type": "string"}, "description": "List of ticker symbols"},
                    "method":           {"type": "string", "description": "max_sharpe | min_var | equal_weight"},
                    "period":           {"type": "string", "description": "History period (default 1y)"},
                    "risk_free_rate":   {"type": "number", "description": "Annual risk-free rate (default 0.04)"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_performance",
            "description": "Sector performance ranking. market='cn' uses akshare industry data; market='us' uses SPDR ETFs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "market": {"type": "string", "description": "cn | us (default cn)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_northbound_flow",
            "description": "北向资金 (Shanghai-HK / Shenzhen-HK Connect) net buy amount in 亿元 via akshare.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of trading days to show (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screen_ashare",
            "description": "A股选股筛选器. Filters by PE, market cap, ST/退市 exclusion, momentum ranking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_pe":              {"type": "number",  "description": "Max dynamic PE ratio (default 50)"},
                    "min_market_cap_yi":   {"type": "number",  "description": "Min market cap in 亿元 (default 0)"},
                    "limit":               {"type": "integer", "description": "Max stocks to return (default 20)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_limit_up_pool",
            "description": "Today's A股 limit-up stock pool (涨停板) via akshare. Includes consecutive limit-up count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date YYYY-MM-DD (default today)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_indices",
            "description": "Major global market indices: S&P 500, NASDAQ, 上证综指, Nikkei, Gold, BTC, etc.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_news",
            "description": "Fetch and sentiment-score recent news for a stock or topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code or company name"},
                    "limit":  {"type": "integer", "description": "Number of articles (default 5)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bonds_data",
            "description": "US Treasury yield curve (2Y, 5Y, 10Y, 30Y) and 10Y-2Y spread.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_commodities_data",
            "description": (
                "Commodity spot/futures price, 52-week range and 1-year return. "
                "Supports: gold, silver, oil, crude, brent, natgas, copper, wheat, corn, soybean, coffee, "
                "cotton, cattle, lumber, 黄金, 原油, 铜, etc. "
                "Also accepts yfinance tickers directly (e.g. GC=F, CL=F, NG=F)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commodity": {
                        "type": "string",
                        "description": "Commodity name (gold, oil, copper…) or yfinance ticker (GC=F)"
                    },
                },
                "required": ["commodity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_futures_data",
            "description": "Equity index and other futures: S&P (ES=F), NASDAQ (NQ=F), Nikkei, VIX, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contract": {
                        "type": "string",
                        "description": "Futures contract name or ticker: SP500, NQ, DOW, VIX, ES=F, NQ=F…"
                    },
                },
                "required": ["contract"],
            },
        },
    },
    # ── New cloud-backed tools ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_ai_signal",
            "description": (
                "AI-powered trading signal (BUY/SELL/HOLD) with confidence, reasoning, stop-loss and "
                "take-profit levels. Uses Alibaba Cloud DeepSeek + QuantEngine. "
                "Falls back to local factor-based signal when cloud unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code, e.g. sh600519, AAPL"},
                    "market": {"type": "string", "description": "CN | US (auto-detected from symbol)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_insights",
            "description": (
                "AI narrative market insights for a basket of stocks. Returns sentiment, key risks, "
                "opportunities and macro context. Powered by Alibaba Cloud AI."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of stock codes, e.g. ['sh600519', 'sz000858']"
                    },
                    "market": {"type": "string", "description": "CN | US (default CN)"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_predictions",
            "description": (
                "ML model return predictions for a list of stocks. "
                "Uses Alibaba Cloud LightGBM/XGBoost ensemble. "
                "Returns predicted_return and confidence for each symbol."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of stock codes"
                    },
                    "prediction_days": {"type": "integer", "description": "Forecast horizon in days (default 5)"},
                    "market": {"type": "string", "description": "CN | US (default CN)"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cloud_backtest",
            "description": (
                "Full ML-powered backtest on Alibaba Cloud QuantEngine. "
                "Supports rebalance_freq (daily/weekly/monthly), dynamic position sizing, "
                "model_type (lightgbm/xgboost/ensemble). Falls back to local pandas backtest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of stock codes for backtest universe"
                    },
                    "model_type":      {"type": "string",  "description": "lightgbm | xgboost | ensemble (default lightgbm)"},
                    "months":          {"type": "integer", "description": "Backtest period in months (default 12)"},
                    "rebalance_freq":  {"type": "string",  "description": "daily | weekly | monthly (default weekly)"},
                    "top_k":           {"type": "integer", "description": "Top-K stocks to hold per period (default 3)"},
                    "start":           {"type": "string",  "description": "Start date YYYY-MM-DD"},
                    "end":             {"type": "string",  "description": "End date YYYY-MM-DD"},
                    "market":          {"type": "string",  "description": "CN | US (default CN)"},
                },
                "required": [],
            },
        },
    },
    # ── web_search ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information. "
                "USE THIS PROACTIVELY when the user asks about: recent news, latest earnings, "
                "new IPO stocks or unverified tickers, price targets, analyst upgrades/downgrades, "
                "M&A deals, regulatory decisions, macro events, or anything that may have changed "
                "after your training cutoff. Do NOT rely on training data for current events — "
                "always search first. Chain with web_fetch to read full articles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string",  "description": "Search query, include ticker and topic, e.g. 'AAPL earnings Q1 2026'"},
                    "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    # ── web_fetch ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL and return the page text. Use after web_search to read full article content, "
                "SEC filings, earnings reports, or any webpage. Automatically strips HTML tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":      {"type": "string",  "description": "Full URL to fetch"},
                    "timeout":  {"type": "integer", "description": "Timeout seconds (default 15)"},
                },
                "required": ["url"],
            },
        },
    },
    # ── get_forex_data ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_forex_data",
            "description": "Get exchange rate data for currency pairs (e.g. USD/CNY, EUR/USD, USD/JPY). Returns OHLCV and current rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair":   {"type": "string", "description": "Currency pair e.g. USDCNY=X, EURUSD=X, USDJPY=X"},
                    "period": {"type": "string", "description": "1d | 5d | 1mo | 3mo | 1y (default 1mo)"},
                },
                "required": ["pair"],
            },
        },
    },
    # ── get_options_chain ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_options_chain",
            "description": "Retrieve options chain for a US stock: calls & puts with strike, expiry, IV, delta, volume, OI. Use for options strategy analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":  {"type": "string",  "description": "US stock ticker e.g. AAPL, TSLA, NVDA"},
                    "expiry":  {"type": "string",  "description": "Expiration date YYYY-MM-DD or leave blank for nearest"},
                    "option_type": {"type": "string", "description": "call | put | both (default both)"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── peer_comparison ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "peer_comparison",
            "description": "Compare a stock against sector peers on valuation (PE/PB), profitability (ROE), market cap. Automatically selects peers if not specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string",      "description": "Target stock ticker"},
                    "peers":  {"type": "array", "items": {"type": "string"}, "description": "Peer tickers (optional, auto-selected if omitted)"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── piotroski_fscore ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "piotroski_fscore",
            "description": "Calculate Piotroski F-Score (0-9) for financial health assessment. Score ≥7 = strong, ≤2 = weak. Use for fundamental stock screening.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── altman_zscore ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "altman_zscore",
            "description": "Calculate Altman Z-Score for bankruptcy risk. Z>2.99 = safe, 1.81-2.99 = grey zone, <1.81 = distress. Use when user asks about company financial risk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── calculate_ichimoku ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "calculate_ichimoku",
            "description": "Calculate Ichimoku Cloud indicators (Tenkan, Kijun, Senkou A/B, Chikou). Provides cloud support/resistance levels and trend signals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock or crypto ticker"},
                    "period": {"type": "string", "description": "Price history period: 3mo | 6mo | 1y (default 6mo)"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── get_fear_greed_index ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_fear_greed_index",
            "description": "Get CNN Fear & Greed Index (0-100) for overall market sentiment. Use when user asks about market sentiment, risk appetite, or broad market mood.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ── get_funding_rates ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_funding_rates",
            "description": "Get perpetual futures funding rates for crypto assets (BTC, ETH, etc.) from major exchanges. Positive rate = longs pay shorts (bullish); negative = shorts pay longs (bearish).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Crypto symbol e.g. BTC, ETH, SOL"},
                },
                "required": ["symbol"],
            },
        },
    },
    # ── get_funding_rates_compare ─────────────────────────────────────────────
    # The handler has been in LOCAL_FINANCE_TOOL_REGISTRY all along with no
    # schema beside it, so it was registered, callable, and invisible: the
    # model was never told it exists.
    {
        "type": "function",
        "function": {
            "name": "get_funding_rates_compare",
            "description": (
                "Compare perpetual funding rates for the same assets across Binance, OKX "
                "and Bybit in one call. Use this to spot cross-exchange arbitrage: a spread "
                "wider than about 0.02% between venues is worth attention. Requires ccxt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Perpetual pairs, e.g. [\"BTC/USDT\", \"ETH/USDT\"]. Defaults to BTC, ETH and SOL.",
                    },
                },
                "required": [],
            },
        },
    },
    # ── walk_forward_backtest ─────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "walk_forward_backtest",
            "description": "Run walk-forward validation backtest: train on rolling windows then test out-of-sample to avoid overfitting. More robust than simple backtest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":        {"type": "string", "description": "Stock ticker"},
                    "strategy":      {"type": "string", "description": "Strategy name: sma_cross | rsi_reversal | macd_trend"},
                    "train_months":  {"type": "integer", "description": "Training window in months (default 12)"},
                    "test_months":   {"type": "integer", "description": "Test window in months (default 3)"},
                },
                "required": ["symbol", "strategy"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 19. Piotroski F-Score  (基本面质量评分, 0–9)
# ---------------------------------------------------------------------------

def _piotroski_fscore(params: dict) -> dict:
    """
    Piotroski F-Score: 9项二元信号综合判断财务质量。
    ≥7 = 高质量(做多信号), ≤3 = 低质量(做空信号), 4-6 = 中性。
    """
    symbol = params.get("symbol", "AAPL")
    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    try:
        tkr  = yf.Ticker(symbol)
        info = tkr.info or {}
        bs_annual   = tkr.balance_sheet    if hasattr(tkr, "balance_sheet")    else None
        is_annual   = tkr.income_stmt      if hasattr(tkr, "income_stmt")      else None
        cf_annual   = tkr.cashflow         if hasattr(tkr, "cashflow")         else None

        def _get(df, row, col=0):
            try:
                if df is None or df.empty: return None
                matches = [r for r in df.index if row.lower() in str(r).lower()]
                if not matches: return None
                val = df.loc[matches[0]].iloc[col]
                return float(val) if val is not None and str(val) not in ("nan","None") else None
            except Exception:
                return None

        scores: Dict[str, Any] = {}

        # ── Profitability (4 signals) ──────────────────────────────────────
        roa = info.get("returnOnAssets") or _get(is_annual, "Net Income")
        scores["F1_ROA_positive"]     = int((roa or 0) > 0)

        cfo = _get(cf_annual, "Operating Cash Flow") or _get(cf_annual, "Total Cash From Operating")
        scores["F2_CFO_positive"]     = int((cfo or 0) > 0)

        # ROA change (current vs prior year)
        net_inc_cur  = _get(is_annual, "Net Income", 0)
        net_inc_prev = _get(is_annual, "Net Income", 1)
        ta_cur       = _get(bs_annual, "Total Assets", 0)
        ta_prev      = _get(bs_annual, "Total Assets", 1)
        roa_cur  = (net_inc_cur  / ta_cur)  if (ta_cur  and net_inc_cur  is not None) else None
        roa_prev = (net_inc_prev / ta_prev) if (ta_prev and net_inc_prev is not None) else None
        scores["F3_ROA_increasing"]   = int(roa_cur > roa_prev) if (roa_cur is not None and roa_prev is not None) else 0

        # Accruals: CFO > ROA × Total Assets
        scores["F4_CFO_gt_ROA"]       = int((cfo or 0) > (roa or 0) * (ta_cur or 1))

        # ── Leverage, Liquidity (3 signals) ───────────────────────────────
        ltd_cur  = _get(bs_annual, "Long Term Debt", 0)
        ltd_prev = _get(bs_annual, "Long Term Debt", 1)
        scores["F5_Leverage_lower"]   = int((ltd_cur or 0) < (ltd_prev or 0)) if ltd_prev is not None else 0

        ca_cur   = _get(bs_annual, "Current Assets", 0)
        ca_prev  = _get(bs_annual, "Current Assets", 1)
        cl_cur   = _get(bs_annual, "Current Liabilities", 0) or _get(bs_annual, "Total Current Liabilities", 0)
        cl_prev  = _get(bs_annual, "Current Liabilities", 1) or _get(bs_annual, "Total Current Liabilities", 1)
        cr_cur   = (ca_cur  / cl_cur)  if (cl_cur  and ca_cur)  else None
        cr_prev  = (ca_prev / cl_prev) if (cl_prev and ca_prev) else None
        scores["F6_CurrentRatio_up"]  = int(cr_cur > cr_prev) if (cr_cur and cr_prev) else 0

        # No dilution: shares outstanding not increasing
        shares_cur  = info.get("sharesOutstanding") or _get(bs_annual, "Ordinary Shares Number", 0)
        shares_prev = _get(bs_annual, "Ordinary Shares Number", 1)
        scores["F7_NoDilution"]       = int((shares_cur or 1) <= (shares_prev or 1)) if shares_prev else 1

        # ── Operating Efficiency (2 signals) ──────────────────────────────
        rev_cur  = _get(is_annual, "Total Revenue", 0)
        rev_prev = _get(is_annual, "Total Revenue", 1)
        gp_cur   = _get(is_annual, "Gross Profit", 0)
        gp_prev  = _get(is_annual, "Gross Profit", 1)
        gm_cur   = (gp_cur  / rev_cur)  if (rev_cur  and gp_cur)  else None
        gm_prev  = (gp_prev / rev_prev) if (rev_prev and gp_prev) else None
        scores["F8_GrossMargin_up"]   = int(gm_cur > gm_prev) if (gm_cur is not None and gm_prev is not None) else 0

        at_cur  = (rev_cur  / ta_cur)  if (ta_cur  and rev_cur)  else None
        at_prev = (rev_prev / ta_prev) if (ta_prev and rev_prev) else None
        scores["F9_AssetTurnover_up"] = int(at_cur > at_prev) if (at_cur is not None and at_prev is not None) else 0

        fscore = sum(scores.values())

        if fscore >= 7:
            verdict, color = "高质量 — 做多信号", "bullish"
        elif fscore <= 3:
            verdict, color = "低质量 — 做空信号", "bearish"
        else:
            verdict, color = "中性", "neutral"

        return {
            "success":  True,
            "symbol":   symbol,
            "f_score":  fscore,
            "verdict":  verdict,
            "signal":   color,
            "scores":   scores,
            "provider": "yfinance",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 20. Altman Z-Score  (破产风险预测)
# ---------------------------------------------------------------------------

def _altman_zscore(params: dict) -> dict:
    """
    Altman Z''-Score（适合非制造业，使用 4 变量版）。
    Z > 2.6 = 安全区，1.1–2.6 = 灰色区，< 1.1 = 破产风险。
    """
    symbol = params.get("symbol", "AAPL")
    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    try:
        tkr  = yf.Ticker(symbol)
        info = tkr.info or {}
        bs   = tkr.balance_sheet if hasattr(tkr, "balance_sheet") else None
        is_  = tkr.income_stmt   if hasattr(tkr, "income_stmt")   else None

        def _g(df, row, col=0):
            try:
                if df is None or df.empty: return None
                m = [r for r in df.index if row.lower() in str(r).lower()]
                if not m: return None
                v = df.loc[m[0]].iloc[col]
                return float(v) if str(v) not in ("nan","None","") else None
            except Exception:
                return None

        ta      = _g(bs, "Total Assets") or info.get("totalAssets")
        tl      = (_g(bs, "Total Liabilities Net Minority Interest") or
                   _g(bs, "Total Liabilities"))
        ca      = _g(bs, "Current Assets")
        cl      = (_g(bs, "Total Current Liabilities") or _g(bs, "Current Liabilities"))
        re      = _g(bs, "Retained Earnings")
        ebit    = _g(is_, "EBIT") or _g(is_, "Operating Income")
        revenue = _g(is_, "Total Revenue")
        market_cap = info.get("marketCap")
        bv_equity   = info.get("bookValue", 0) or 0
        shares_out  = info.get("sharesOutstanding", 0) or 0
        book_equity = bv_equity * shares_out

        if not ta or ta == 0:
            return {"success": False, "error": "无法获取总资产数据"}

        # Working capital / Total Assets  (X1)
        wc = (ca or 0) - (cl or 0)
        x1 = wc / ta

        # Retained Earnings / Total Assets  (X2)
        x2 = (re or 0) / ta

        # EBIT / Total Assets  (X3)
        x3 = (ebit or 0) / ta

        # Book/Market Value of Equity / Total Liabilities  (X4 — Z'' uses book)
        bv = book_equity or (market_cap or 0)
        x4 = bv / (tl or 1)

        # Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
        z = round(z, 3)

        if z > 2.6:
            zone = "安全区"
            risk = "low"
        elif z > 1.1:
            zone = "灰色区（不确定）"
            risk = "medium"
        else:
            zone = "破产风险区"
            risk = "high"

        return {
            "success":  True,
            "symbol":   symbol,
            "z_score":  z,
            "zone":     zone,
            "risk":     risk,
            "components": {
                "X1_working_capital_ratio": round(x1, 4),
                "X2_retained_earnings_ratio": round(x2, 4),
                "X3_ebit_ratio": round(x3, 4),
                "X4_equity_to_debt": round(x4, 4),
            },
            "formula":  "Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4",
            "provider": "yfinance",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 21. Options Chain  (期权链)
# ---------------------------------------------------------------------------

def _get_options_chain(params: dict) -> dict:
    """
    获取股票期权链（via yfinance）。
    返回最近到期日的 calls + puts 列表。
    """
    symbol  = str(params.get("symbol", "AAPL")).strip().upper()
    expiry  = params.get("expiry", "")      # "YYYY-MM-DD" or "" = nearest
    opt_type = params.get("type", "both").lower()  # "calls" | "puts" | "both"
    limit   = int(params.get("limit", 15))

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    try:
        tkr = yf.Ticker(symbol)
        dates = tkr.options
        if not dates:
            return {"success": False, "error": f"{symbol} 无可用期权数据"}

        if expiry and expiry in dates:
            exp = expiry
        else:
            exp = dates[0]  # nearest expiry

        chain = tkr.option_chain(exp)
        price = (tkr.info or {}).get("regularMarketPrice") or (tkr.info or {}).get("currentPrice") or 0

        def _fmt(df):
            if df is None or df.empty:
                return []
            cols = [c for c in ["strike","lastPrice","bid","ask","volume","openInterest",
                                 "impliedVolatility","inTheMoney"] if c in df.columns]
            df = df[cols].head(limit)
            rows = df.to_dict("records")
            for r in rows:
                iv = r.get("impliedVolatility")
                r["iv_pct"] = round(iv * 100, 1) if iv else None
            return rows

        result: Dict[str, Any] = {
            "success":     True,
            "symbol":      symbol,
            "price":       price,
            "expiry":      exp,
            "all_expiries": list(dates[:6]),
            "provider":    "yfinance",
        }
        if opt_type in ("calls", "both"):
            result["calls"] = _fmt(chain.calls)
        if opt_type in ("puts", "both"):
            result["puts"]  = _fmt(chain.puts)

        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 22. Ichimoku Cloud  (一目均衡表)
# ---------------------------------------------------------------------------

def _calculate_ichimoku(params: dict) -> dict:
    """
    一目均衡表指标计算。
    返回 Tenkan-sen(转换线), Kijun-sen(基准线), Senkou Span A/B(先行带),
    Chikou(迟行线), 以及云层厚度与当前信号。
    """
    symbol = str(params.get("symbol", "AAPL")).strip().upper()
    period = params.get("period", "6mo")

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    try:
        df = yf.Ticker(symbol).history(period=period)
        if df is None or len(df) < 52:
            return {"success": False, "error": "历史数据不足（至少需要 52 天）"}

        high = df["High"].astype(float)
        low  = df["Low"].astype(float)
        close= df["Close"].astype(float)

        def _mid(h, l, n):
            return (h.rolling(n).max() + l.rolling(n).min()) / 2

        tenkan  = _mid(high, low, 9)
        kijun   = _mid(high, low, 26)
        senkou_a = ((tenkan + kijun) / 2).shift(26)
        senkou_b = _mid(high, low, 52).shift(26)
        chikou  = close.shift(-26)

        t  = round(float(tenkan.iloc[-1]), 3)
        k  = round(float(kijun.iloc[-1]), 3)
        sa = round(float(senkou_a.iloc[-1]), 3) if not pd.isna(senkou_a.iloc[-1]) else None
        sb = round(float(senkou_b.iloc[-1]), 3) if not pd.isna(senkou_b.iloc[-1]) else None
        c  = round(float(close.iloc[-1]), 3)
        ck = round(float(chikou.iloc[-53]) if len(chikou) > 53 else float(chikou.iloc[0]), 3)

        # Signal
        above_cloud = sa is not None and sb is not None and c > max(sa, sb)
        below_cloud = sa is not None and sb is not None and c < min(sa, sb)
        bullish_tk  = t > k
        cloud_color = "绿云(多)" if (sa and sb and sa > sb) else "红云(空)"

        if above_cloud and bullish_tk:
            signal = "强势多头"
        elif above_cloud:
            signal = "偏多（价格在云上方）"
        elif below_cloud and not bullish_tk:
            signal = "强势空头"
        elif below_cloud:
            signal = "偏空（价格在云下方）"
        else:
            signal = "震荡（价格在云内）"

        return {
            "success":     True,
            "symbol":      symbol,
            "price":       c,
            "tenkan":      t,
            "kijun":       k,
            "senkou_a":    sa,
            "senkou_b":    sb,
            "chikou":      ck,
            "cloud_color": cloud_color,
            "cloud_thickness": round(abs((sa or 0) - (sb or 0)), 3),
            "signal":      signal,
            "above_cloud": above_cloud,
            "below_cloud": below_cloud,
            "tk_cross":    "金叉(多)" if bullish_tk else "死叉(空)",
            "provider":    "yfinance",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 23. Crypto Fear & Greed Index  + Funding Rates
# ---------------------------------------------------------------------------

def _get_fear_greed_index(params: dict) -> dict:
    """加密货币恐惧贪婪指数（来源: alternative.me，无需 API Key）。"""
    try:
        import urllib.request
        import json as _json
        with urllib.request.urlopen(
            "https://api.alternative.me/fng/?limit=7&format=json", timeout=6
        ) as resp:
            data = _json.loads(resp.read().decode())
        items = data.get("data", [])
        if not items:
            return {"success": False, "error": "No data returned"}
        latest   = items[0]
        value    = int(latest.get("value", 0))
        label_en = latest.get("value_classification", "")
        label_cn_map = {
            "Extreme Fear": "极度恐惧",
            "Fear":         "恐惧",
            "Neutral":      "中性",
            "Greed":        "贪婪",
            "Extreme Greed":"极度贪婪",
        }
        label_cn = label_cn_map.get(label_en, label_en)
        history  = [{"date": i.get("timestamp",""), "value": int(i.get("value",0)),
                     "label": i.get("value_classification","")} for i in items]
        return {
            "success":  True,
            "value":    value,
            "label":    label_cn,
            "label_en": label_en,
            "history":  history,
            "signal":   "做空" if value >= 75 else "做多" if value <= 25 else "中性",
            "provider": "alternative.me",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_funding_rates(params: dict) -> dict:
    """
    获取永续合约资金费率 (via ccxt)。
    支持 binance, okx, bybit 等主流交易所。
    高正费率 → 多头过多 → 看空信号；负费率 → 空头过多 → 看多信号。
    """
    exchange_id = params.get("exchange", "binance").lower()
    symbols = params.get("symbols", ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.replace(",", " ").split()]

    try:
        import ccxt as _ccxt
    except ImportError:
        return {"success": False, "error": "ccxt 未安装: pip install ccxt"}

    # Try exchanges in order; fall back if load_markets fails (network/region issues)
    _all_exchanges = ["binance", "okx", "bybit"]
    _exchange_fallback = [exchange_id] + [e for e in _all_exchanges if e != exchange_id]

    for _exid in _exchange_fallback:
        try:
            exchange_cls = getattr(_ccxt, _exid, None)
            if not exchange_cls:
                continue
            ex = exchange_cls({"options": {"defaultType": "future"}})
            try:
                ex.load_markets()
            except Exception as _lm_err:
                _lm_msg = str(_lm_err)
                if len(_lm_msg) > 100 or "http" in _lm_msg or "GET" in _lm_msg or "POST" in _lm_msg:
                    _lm_msg = f"无法连接 {_exid} 期货市场（网络或区域限制）"
                if _exid == _exchange_fallback[-1]:
                    return {"success": False, "error": f"{_exid}: {_lm_msg}"}
                continue

            results = []
            for sym in symbols:
                try:
                    fi = ex.fetch_funding_rate(sym)
                    rate = fi.get("fundingRate") or fi.get("funding_rate") or 0
                    next_time = fi.get("fundingDatetime") or fi.get("nextFundingDatetime") or ""
                    annualized = round(float(rate) * 3 * 365 * 100, 2)  # 8h intervals
                    results.append({
                        "symbol":       sym,
                        "rate":         round(float(rate) * 100, 4),
                        "rate_pct":     f"{float(rate)*100:.4f}%",
                        "annualized":   f"{annualized:.1f}%",
                        "next_funding": str(next_time)[:16],
                        "signal":       "空" if float(rate) > 0.0005 else "多" if float(rate) < -0.0001 else "中性",
                    })
                except Exception:
                    pass

            if not results:
                if _exid == _exchange_fallback[-1]:
                    return {"success": False, "error": f"已尝试 {', '.join(_exchange_fallback)}，均未能获取资金费率数据"}
                continue

            avg_rate = sum(r["rate"] for r in results) / len(results)
            return {
                "success":     True,
                "exchange":    _exid,
                "rates":       results,
                "avg_rate":    round(avg_rate, 4),
                "market_bias": "多头过热(偏空)" if avg_rate > 0.05 else "空头过多(偏多)" if avg_rate < -0.01 else "均衡",
                "provider":    "ccxt",
            }
        except Exception as e:
            _err_msg = str(e)
            if len(_err_msg) > 100 or "http" in _err_msg or "GET" in _err_msg or "POST" in _err_msg:
                _err_msg = f"无法连接 {_exid}（网络或区域限制）"
            if _exid == _exchange_fallback[-1]:
                return {"success": False, "error": _err_msg}

    return {"success": False, "error": "所有备用交易所均连接失败"}


def _get_funding_rates_compare(params: dict) -> dict:
    """
    并行查询 binance / okx / bybit，返回三所资金费率横向对比。
    用于发现跨所套利机会（同一标的费率差 > 0.02% 值得关注）。
    """
    symbols = params.get("symbols", ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.replace(",", " ").split()]

    try:
        import ccxt as _ccxt  # noqa: F401
    except ImportError:
        return {"success": False, "error": "ccxt 未安装: pip install ccxt"}

    import concurrent.futures as _fut

    _exchanges = ["binance", "okx", "bybit"]

    def _fetch(exid):
        return exid, _get_funding_rates({"exchange": exid, "symbols": symbols})

    ex_results: dict = {}
    with _fut.ThreadPoolExecutor(max_workers=3) as pool:
        for exid, r in pool.map(_fetch, _exchanges):
            ex_results[exid] = r

    comparison = []
    for sym in symbols:
        row: dict = {"symbol": sym}
        for exid in _exchanges:
            r = ex_results.get(exid, {})
            if r.get("success"):
                match = next((x for x in r.get("rates", []) if x["symbol"] == sym), None)
                if match:
                    row[exid] = match
        if len(row) > 1:
            comparison.append(row)

    if not comparison:
        return {"success": False, "error": "三所均无数据，请检查网络或 VPN"}

    # Find max cross-exchange spread per symbol
    spreads = []
    for row in comparison:
        rates = [row[e]["rate"] for e in _exchanges if e in row]
        if len(rates) >= 2:
            spreads.append(round(max(rates) - min(rates), 4))

    max_spread = max(spreads) if spreads else 0.0
    arb_note = (
        "⚠ 套利机会：最大价差 > 0.02%" if max_spread > 0.02
        else "价差正常，无明显套利空间"
    )

    return {
        "success":    True,
        "comparison": comparison,
        "exchanges":  _exchanges,
        "max_spread": max_spread,
        "arb_note":   arb_note,
        "provider":   "ccxt_compare",
    }


# ---------------------------------------------------------------------------
# 24. Walk-Forward Backtest  (滚动验证)
# ---------------------------------------------------------------------------

def _walk_forward_backtest(params: dict) -> dict:
    """
    Walk-Forward 滚动回测：将历史分成 N 个窗口，每窗口 in-sample 优化、
    out-of-sample 验证，评估策略真实泛化能力。
    """
    symbol   = params.get("symbol", "AAPL")
    strategy = params.get("strategy", "sma_crossover")
    periods  = int(params.get("periods", 5))      # number of WF windows
    train_r  = float(params.get("train_ratio", 0.7))
    period   = params.get("period", "5y")

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    try:
        import numpy as np
        tkr = yf.Ticker(symbol)
        df  = tkr.history(period=period)
        if df is None or len(df) < 200:
            return {"success": False, "error": "历史数据不足（需要至少 200 天）"}

        close = df["Close"].astype(float).values
        n     = len(close)
        window_size = n // periods

        window_results = []
        for i in range(periods):
            start = i * window_size
            end   = start + window_size if i < periods - 1 else n
            split = start + int((end - start) * train_r)

            train = close[start:split]
            test  = close[split:end]

            if len(test) < 20:
                continue

            # Simple parameter: SMA crossover with different windows
            best_sharpe = -np.inf
            best_fast, best_slow = 10, 30

            if strategy in ("sma_crossover", "ma_crossover"):
                for fast in (5, 10, 15, 20):
                    for slow in (20, 30, 40, 60):
                        if fast >= slow or len(train) <= slow:
                            continue
                        sig = np.where(
                            np.convolve(train, np.ones(fast)/fast, mode="valid")
                            [-(len(train)-slow+1):] >
                            np.convolve(train, np.ones(slow)/slow, mode="valid"),
                            1, 0
                        )
                        if len(sig) < 2: continue
                        rets = np.diff(train[-len(sig):]) / train[-len(sig):-1]
                        strat_rets = rets * sig[:-1]
                        sr = (np.mean(strat_rets) / (np.std(strat_rets) + 1e-8)) * np.sqrt(252)
                        if sr > best_sharpe:
                            best_sharpe, best_fast, best_slow = sr, fast, slow

            # Out-of-sample evaluation with best params
            if len(test) <= best_slow:
                continue
            fast_ma = np.convolve(test, np.ones(best_fast)/best_fast, mode="valid")
            slow_ma = np.convolve(test, np.ones(best_slow)/best_slow, mode="valid")
            n_sig   = min(len(fast_ma), len(slow_ma))
            signals = np.where(fast_ma[-n_sig:] > slow_ma[-n_sig:], 1, 0)
            rets_test = np.diff(test[-n_sig:]) / test[-n_sig:-1]
            strat_rets_oos = rets_test * signals[:-1]
            bh_rets        = rets_test

            oos_total  = float(np.prod(1 + strat_rets_oos) - 1)
            bh_total   = float(np.prod(1 + bh_rets) - 1)
            oos_sharpe = float(np.mean(strat_rets_oos) / (np.std(strat_rets_oos) + 1e-8)) * np.sqrt(252)
            dd_vals    = np.maximum.accumulate(np.cumprod(1 + strat_rets_oos)) - np.cumprod(1 + strat_rets_oos)
            max_dd     = float(np.max(dd_vals) / np.maximum.accumulate(np.cumprod(1 + strat_rets_oos))[-1])

            window_results.append({
                "window":         i + 1,
                "train_bars":     split - start,
                "test_bars":      end - split,
                "best_fast":      best_fast,
                "best_slow":      best_slow,
                "oos_return":     round(oos_total, 4),
                "bh_return":      round(bh_total, 4),
                "oos_sharpe":     round(oos_sharpe, 3),
                "max_drawdown":   round(max_dd, 4),
                "alpha":          round(oos_total - bh_total, 4),
            })

        if not window_results:
            return {"success": False, "error": "回测窗口计算失败"}

        avg_oos_ret   = sum(w["oos_return"] for w in window_results) / len(window_results)
        avg_sharpe    = sum(w["oos_sharpe"] for w in window_results) / len(window_results)
        avg_alpha     = sum(w["alpha"] for w in window_results) / len(window_results)
        pct_win       = sum(1 for w in window_results if w["oos_return"] > 0) / len(window_results)

        verdict = (
            "策略泛化能力强" if avg_alpha > 0.02 and avg_sharpe > 0.5
            else "策略泛化能力中等" if avg_alpha > 0
            else "策略泛化能力弱（过拟合风险）"
        )

        return {
            "success":           True,
            "symbol":            symbol,
            "strategy":          strategy,
            "windows":           window_results,
            "avg_oos_return":    round(avg_oos_ret, 4),
            "avg_sharpe":        round(avg_sharpe, 3),
            "avg_alpha":         round(avg_alpha, 4),
            "win_rate_windows":  round(pct_win, 2),
            "verdict":           verdict,
            "provider":          "local",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 25. Peer Comparison  (同行对比)
# ---------------------------------------------------------------------------

def _peer_comparison(params: dict) -> dict:
    """
    同行估值与表现对比。
    返回 PE/PB/ROE/YTD收益/股息率/市值 横向对比表。
    """
    symbol = str(params.get("symbol", "AAPL")).strip().upper()
    peers  = params.get("peers", [])  # list of ticker strings
    if isinstance(peers, str):
        peers = [p.strip().upper() for p in peers.replace(",", " ").split() if p.strip()]

    # Auto-suggest peers from yfinance sector info if not provided
    if not peers and _HAS_YF:
        try:
            info = yf.Ticker(symbol).info or {}
            # yfinance doesn't give peers directly; use sector to build manual map
            _SECTOR_PEERS = {
                "Technology":   ["AAPL","MSFT","GOOGL","META","NVDA","AMZN"],
                "Financials":   ["JPM","BAC","GS","MS","WFC","C"],
                "Healthcare":   ["JNJ","LLY","ABBV","MRK","PFE","UNH"],
                "Consumer Cyclical": ["AMZN","TSLA","HD","MCD","NKE","SBUX"],
                "Energy":       ["XOM","CVX","COP","SLB","EOG","OXY"],
            }
            sector = info.get("sector", "")
            default_list = _SECTOR_PEERS.get(sector, ["SPY"])
            peers = [p for p in default_list if p != symbol][:5]
        except Exception:
            pass

    if not peers:
        return {"success": False, "error": "请提供 peers 参数，如: peers=['MSFT','GOOGL','META']"}

    all_symbols = [symbol] + [p for p in peers if p != symbol]

    if not _HAS_YF:
        return {"success": False, "error": "yfinance not installed: 运行 pip install yfinance 或 /install yfinance"}

    rows = []
    for sym in all_symbols[:8]:
        try:
            info = yf.Ticker(sym).info or {}
            price  = info.get("regularMarketPrice") or info.get("currentPrice") or 0
            prev   = info.get("regularMarketPreviousClose") or price
            pe     = info.get("trailingPE") or info.get("forwardPE")
            pb     = info.get("priceToBook")
            roe    = info.get("returnOnEquity")
            dy     = info.get("dividendYield")
            mc     = info.get("marketCap")
            ytd    = (price / prev - 1) if prev else None
            rows.append({
                "symbol":    sym,
                "name":      (info.get("shortName") or sym)[:12],
                "price":     round(price, 2),
                "pe":        round(pe, 1)  if pe  else None,
                "pb":        round(pb, 2)  if pb  else None,
                "roe_pct":   round(roe * 100, 1) if roe else None,
                "div_yield": round(dy * 100, 2)  if dy  else None,
                "market_cap_b": round(mc / 1e9, 1) if mc else None,
                "is_target": sym == symbol,
            })
        except Exception:
            pass

    if not rows:
        return {"success": False, "error": "无法获取对比数据"}

    # Relative rankings
    pe_vals  = [r["pe"]   for r in rows if r["pe"] is not None]
    pb_vals  = [r["pb"]   for r in rows if r["pb"] is not None]
    roe_vals = [r["roe_pct"] for r in rows if r["roe_pct"] is not None]

    target_row = next((r for r in rows if r["is_target"]), rows[0])
    analysis = []
    if target_row.get("pe") and pe_vals:
        med_pe = sorted(pe_vals)[len(pe_vals)//2]
        vs = "高估" if target_row["pe"] > med_pe * 1.2 else "低估" if target_row["pe"] < med_pe * 0.8 else "合理"
        analysis.append(f"PE {target_row['pe']:.1f}x vs 同行中位数 {med_pe:.1f}x → {vs}")
    if target_row.get("roe_pct") and roe_vals:
        avg_roe = sum(roe_vals) / len(roe_vals)
        vs = "优于同行" if target_row["roe_pct"] > avg_roe else "低于同行"
        analysis.append(f"ROE {target_row['roe_pct']:.1f}% vs 同行均值 {avg_roe:.1f}% → {vs}")

    return {
        "success":   True,
        "symbol":    symbol,
        "peers":     peers,
        "table":     rows,
        "analysis":  analysis,
        "provider":  "yfinance",
    }


def _resolve_search_key(env_var: str, provider: str) -> str:
    """Resolve a search-API key: env var first, then ~/.arthera/providers.json.

    Keys added via `/apikey set <provider> <key>` are stored in providers.json
    (data section), not exported as env vars — so a web search must check both
    or it silently falls through to DuckDuckGo despite a configured key.
    """
    val = os.getenv(env_var, "")
    if val:
        return val
    try:
        import json as _json
        import pathlib as _pl
        for _loc in ("~/.arthera/providers.json", "~/.aria/providers.json"):
            p = _pl.Path(_loc).expanduser()
            if not p.exists():
                continue
            raw = _json.loads(p.read_text(encoding="utf-8"))
            for section in ("data", "llm"):
                entry = raw.get(section, {}).get(provider.lower(), {})
                if isinstance(entry, dict) and entry.get("api_key"):
                    return entry["api_key"]
                if isinstance(entry, str) and entry:
                    return entry
    except Exception:
        pass
    return ""


def _web_search(params: dict) -> dict:
    """Web search: Brave → Tavily → DuckDuckGo fallback chain."""
    if not isinstance(params, dict):
        params = {"query": str(params)}
    query = (
        params.get("query")
        or params.get("q")
        or params.get("search")
        or params.get("keyword")
        or params.get("keywords")
        or params.get("prompt")
        or params.get("text")
        or params.get("symbol")
        or params.get("ticker")
        or ""
    )
    if isinstance(query, (list, tuple)):
        query = " ".join(str(x) for x in query if x)
    elif isinstance(query, dict):
        query = " ".join(str(v) for v in query.values() if v)
    query = str(query).strip().strip("'\"`")
    if not query:
        # Fallback to general market news query if empty
        query = "stock market news"

    num = min(int(params.get("num_results", params.get("max_results", 5)) or 5), 10)

    # ── 1. Brave Search API ───────────────────────────────────────────────────
    brave_key = _resolve_search_key("BRAVE_SEARCH_API_KEY", "brave")
    if brave_key:
        try:
            import urllib.request as _req
            import urllib.parse as _parse
            import gzip as _gzip
            # search_lang must be a valid Brave code. "zh" is INVALID (→ HTTP 422);
            # use "zh-hans" for Chinese queries and omit it otherwise (auto-detect).
            _q_params = {"q": query, "count": num, "safesearch": "moderate"}
            if any("一" <= _c <= "鿿" for _c in query):
                _q_params["search_lang"] = "zh-hans"
            url = "https://api.search.brave.com/res/v1/web/search?" + _parse.urlencode(_q_params)
            req = _req.Request(url, headers={
                "Accept":               "application/json",
                "Accept-Encoding":      "gzip",
                "X-Subscription-Token": brave_key,
            })
            with _req.urlopen(req, timeout=10) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = _gzip.decompress(raw)
                data = json.loads(raw)
            results = []
            for item in data.get("web", {}).get("results", [])[:num]:
                results.append({
                    "title":   item.get("title", ""),
                    "url":     item.get("url", ""),
                    "snippet": item.get("description", ""),
                })
            if results:
                return {"success": True, "query": query, "results": results, "provider": "brave"}
        except Exception as e:
            # Decompress gzip error bodies so the log is readable, not garbage
            _detail = str(e)
            try:
                import urllib.error as _uerr
                if isinstance(e, _uerr.HTTPError):
                    _body = e.read()
                    try:
                        import gzip as _gz2
                        if e.headers.get("Content-Encoding") == "gzip":
                            _body = _gz2.decompress(_body)
                    except Exception:
                        pass
                    _detail = f"HTTP {e.code}: {_body.decode('utf-8', 'ignore')[:200]}"
            except Exception:
                pass
            logger.debug("Brave search failed: %s; trying next provider", _detail)

    # ── 2. Tavily API (designed for AI agents, generous free tier) ───────────
    tavily_key = _resolve_search_key("TAVILY_API_KEY", "tavily")
    if tavily_key:
        try:
            import urllib.request as _req2
            import urllib.parse as _parse2
            req2 = _req2.Request(
                "https://api.tavily.com/search",
                data=json.dumps({"api_key": tavily_key, "query": query, "max_results": num, "search_depth": "basic"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with _req2.urlopen(req2, timeout=10) as r2:
                data2 = json.loads(r2.read())
            results = [
                {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")[:300]}
                for item in data2.get("results", [])[:num]
            ]
            if results:
                return {"success": True, "query": query, "results": results, "provider": "tavily"}
        except Exception as e:
            logger.debug("Tavily search failed: %s", e)

    # ── 3. Google Programmable Search (Custom Search JSON API) ───────────────
    # GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_ENGINE_ID were documented in
    # .env.example but never read by any code path, so configuring them did
    # nothing and the chain fell through to rate-limited DuckDuckGo.
    google_key = _resolve_search_key("GOOGLE_SEARCH_API_KEY", "google")
    google_cx = _resolve_search_key("GOOGLE_SEARCH_ENGINE_ID", "google_cx")
    if google_key and google_cx:
        try:
            import urllib.request as _req3
            import urllib.parse as _parse3

            _g_params = {"key": google_key, "cx": google_cx, "q": query, "num": num}
            if any("\u4e00" <= _c <= "\u9fff" for _c in query):
                _g_params["lr"] = "lang_zh-CN"
            url3 = "https://www.googleapis.com/customsearch/v1?" + _parse3.urlencode(_g_params)
            with _req3.urlopen(url3, timeout=10) as r3:
                data3 = json.loads(r3.read())
            results = [
                {
                    "title":   item.get("title", ""),
                    "url":     item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in (data3.get("items") or [])[:num]
            ]
            if results:
                return {"success": True, "query": query, "results": results, "provider": "google"}
        except Exception as e:
            logger.debug("Google Programmable Search failed: %s; trying next provider", e)

    # ── 4. DuckDuckGo (free, no key, but rate-limited) ────────────────────────
    try:
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore[no-redef]
        results = []
        for item in DDGS().text(query, max_results=num):
            results.append({
                "title":   item.get("title", ""),
                "url":     item.get("href", ""),
                "snippet": item.get("body", ""),
            })
        if results:
            return {"success": True, "query": query, "results": results, "provider": "duckduckgo"}
        return {
            "success": False,
            "query":   query,
            "results": [],
            "error":   (
                "DuckDuckGo returned no results (rate-limited). "
                "推荐配置: BRAVE_SEARCH_API_KEY (免费2000次/月)、TAVILY_API_KEY (AI专用, 免费1000次/月) "
                "或 GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_ENGINE_ID (免费100次/天)"
            ),
        }
    except ImportError:
        pass
    except Exception as e:
        logger.debug("duckduckgo_search failed: %s", e)

    return {
        "success": False,
        "query":   query,
        "results": [],
        "error":   (
            "无可用搜索服务。推荐配置:\n"
            "  BRAVE_SEARCH_API_KEY — https://brave.com/search/api/ (免费2000次/月)\n"
            "  TAVILY_API_KEY       — https://tavily.com (AI专用, 免费1000次/月)\n"
            "  GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_ENGINE_ID\n"
            "                       — https://developers.google.com/custom-search (免费100次/天)\n"
            "  或安装: pip install duckduckgo-search"
        ),
    }


def _run_portfolio_backtest(params: dict) -> dict:
    """Event-driven, multi-asset backtest via backtest_engine.

    Unlike backtest_strategy (single-symbol templates), this runs a Strategy over
    a portfolio of symbols with next-open fills (no look-ahead), commission +
    slippage, and an equal-weight buy-hold benchmark. Returns a compact result.
    """
    raw = params.get("symbols") or params.get("symbol") or ""
    if isinstance(raw, str):
        symbols = [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]
    else:
        symbols = [str(s).strip().upper() for s in (raw or []) if str(s).strip()]
    if not symbols:
        return {"success": False, "error": "symbols is required"}

    try:
        from aria_code.backtest_engine import BacktestEngine, get_strategy, load_bars
    except Exception as exc:
        return {"success": False, "error": f"backtest engine unavailable: {exc}"}

    strategy_name = str(params.get("strategy", "sma_cross") or "sma_cross")
    skw: dict = {}
    for k in ("fast", "slow"):
        if params.get(k) is not None:
            try:
                skw[k] = int(params[k])
            except (TypeError, ValueError):
                pass
    if params.get("weight") is not None:
        try:
            skw["weight"] = float(params["weight"])
        except (TypeError, ValueError):
            pass
    try:
        strategy = get_strategy(strategy_name, **skw)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        days = max(30, min(int(params.get("days", 365) or 365), 2000))
    except (TypeError, ValueError):
        days = 365

    data, missing = {}, []
    for s in symbols:
        try:
            bars = load_bars(s, days=days)
        except Exception:
            bars = []
        (data.__setitem__(s, bars) if bars else missing.append(s))
    if not data:
        return {"success": False, "error": "无法获取历史数据，请稍后重试",
                "symbols": symbols, "missing": missing}

    def _f(key, default):
        try:
            return float(params.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    eng = BacktestEngine(starting_cash=_f("starting_cash", 100000.0),
                         commission=_f("commission", 0.0005),
                         slippage=_f("slippage", 0.0005))
    res = eng.run(data, strategy)

    ec = res.equity_curve
    step = max(1, len(ec) // 20)
    sample = ec[::step]
    if ec and sample[-1] is not ec[-1]:
        sample.append(ec[-1])
    return {
        "success": True,
        "strategy": res.strategy,
        "symbols": res.symbols,
        "missing": missing,
        "starting_cash": res.starting_cash,
        "metrics": res.metrics,
        "benchmark": res.benchmark,
        "alpha_vs_buyhold": round(res.metrics["total_return"] - res.benchmark.get("total_return", 0.0), 6),
        "n_points": len(ec),
        "equity_sample": sample,
        "recent_trades": res.trades[-10:],
    }


# ---------------------------------------------------------------------------
# Tool registry  (must be after all function definitions)
# ---------------------------------------------------------------------------

LOCAL_FINANCE_TOOL_REGISTRY: Dict[str, Tuple] = {
    # ── Market data (cloud → local fallback) ─────────────────────────────
    "get_market_data":        (_get_market_data,        "Stock/ETF quotes and OHLCV history (A股/US/global, cloud-backed)"),
    "get_crypto_data":        (_get_crypto_data,        "Cryptocurrency OHLCV and ticker data via ccxt/yfinance"),
    "get_forex_data":         (_get_forex_data,         "Foreign exchange rates (yfinance)"),
    "get_commodities_data":   (_get_commodities_data,   "Commodity futures prices: gold, oil, copper, wheat, etc. (yfinance)"),
    "get_futures_data":       (_get_futures_data,       "Equity index futures: S&P, NASDAQ, VIX, Nikkei, etc. (yfinance)"),
    # ── Factor & signal (cloud → local fallback) ─────────────────────────
    "calculate_factors":      (_calculate_factors,      "Technical factors: RSI, MACD, MA gaps, volatility, momentum, trend score (cloud-enhanced)"),
    "get_ai_signal":          (_get_ai_signal,          "AI trading signal: BUY/SELL/HOLD with confidence and reasoning (Alibaba Cloud DeepSeek)"),
    "get_market_insights":    (_get_market_insights,    "AI narrative market insights for a basket of symbols (Alibaba Cloud)"),
    "get_predictions":        (_get_predictions,        "ML-powered 5/10-day return predictions (Alibaba Cloud LightGBM/XGBoost)"),
    # ── Backtest (cloud ML → local pandas fallback) ───────────────────────
    "backtest_strategy":      (_backtest_strategy,      "Run a local pandas backtest: sma_cross | rsi_mean_revert | momentum | buy_hold"),
    "cloud_backtest":         (_cloud_backtest,         "Full ML-powered backtest via Alibaba Cloud QuantEngine (rebalance freq, dynamic position)"),
    # ── Risk & portfolio ──────────────────────────────────────────────────
    "get_risk_metrics":       (_get_risk_metrics,       "VaR, CVaR, max drawdown, Sharpe, Calmar, skew, kurtosis"),
    "optimize_positions":     (_optimize_positions,     "Portfolio optimisation: max_sharpe | min_var | equal_weight"),
    # ── A股 data services ─────────────────────────────────────────────────
    "get_sector_performance": (_get_sector_performance, "Sector performance ranking (A股 industry / US SPDR ETFs)"),
    "get_northbound_flow":    (_get_northbound_flow,    "北向资金 (沪深港通) net buy flow via akshare"),
    "screen_ashare":          (_screen_ashare,          "A股选股筛选: PE, ROE, market cap, momentum"),
    "get_limit_up_pool":      (_get_limit_up_pool,      "A股涨停板池 (today's limit-up stocks via akshare)"),
    "get_market_indices":     (_get_market_indices,     "Global market indices: US, CN, EU, crypto, commodities"),
    # ── News & macro ──────────────────────────────────────────────────────
    "analyze_news":           (_analyze_news,           "News sentiment analysis (A股 via akshare)"),
    "get_bonds_data":         (_get_bonds_data,         "US Treasury yield curve (yfinance)"),
    # ── Quality scores ────────────────────────────────────────────────────
    "piotroski_fscore":       (_piotroski_fscore,       "Piotroski F-Score (0-9): 财务质量评分，≥7 高质量做多，≤3 低质量做空"),
    "altman_zscore":          (_altman_zscore,          "Altman Z''-Score: 企业破产风险预测，>2.6安全，<1.1高风险"),
    # ── Options ───────────────────────────────────────────────────────────
    "get_options_chain":      (_get_options_chain,      "期权链: 获取股票的 calls/puts，含行权价、隐含波动率、未平仓量"),
    # ── Technical indicators ──────────────────────────────────────────────
    "calculate_ichimoku":     (_calculate_ichimoku,     "一目均衡表 Ichimoku Cloud: 转换线/基准线/先行带/迟行线，含信号判断"),
    # ── Crypto ────────────────────────────────────────────────────────────
    "get_fear_greed_index":   (_get_fear_greed_index,   "加密恐惧贪婪指数 (0-100)，>75极度贪婪/做空信号，<25极度恐惧/做多信号"),
    "get_funding_rates":      (_get_funding_rates,      "永续合约资金费率 (ccxt): 高正费率=多头过热，负费率=空头过多"),
    "get_funding_rates_compare": (_get_funding_rates_compare, "三所费率横向对比 (binance/okx/bybit): 并行查询，发现跨所套利机会"),
    # ── Portfolio / backtesting ───────────────────────────────────────────
    "walk_forward_backtest":  (_walk_forward_backtest,  "Walk-Forward 滚动回测：N个窗口验证策略泛化能力，避免过拟合"),
    "run_portfolio_backtest": (_run_portfolio_backtest, "事件驱动多资产回测：next-open 成交(无未来函数)+成本+买入持有基准，同一策略可驱动实盘"),
    "peer_comparison":        (_peer_comparison,        "同行对比: PE/PB/ROE/市值/股息率横向比较，自动识别同行业股票"),
    # ── Web search ────────────────────────────────────────────────────────
    "web_search":             (_web_search,             "Web search via Brave Search API or DuckDuckGo fallback; set BRAVE_SEARCH_API_KEY for higher quota"),
}

# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_local_finance_tools(
    tool_registry: Dict,
    schema_registry: List,
) -> int:
    """
    Add local finance tools to the CLI's LOCAL_TOOLS and LOCAL_TOOL_SCHEMAS.

    Only registers tools whose names are NOT already present (never overwrites
    existing tools, so remote Aria tools take precedence when backend is up).

    Returns number of tools newly registered.
    """
    added = 0
    for name, (handler, description) in LOCAL_FINANCE_TOOL_REGISTRY.items():
        if name not in tool_registry:
            tool_registry[name] = (
                lambda p, h=handler: _safe(h, p),
                description,
            )
            added += 1

    existing_schema_names = {
        s.get("function", {}).get("name") for s in schema_registry
    }
    for schema in LOCAL_FINANCE_TOOL_SCHEMAS:
        sname = schema.get("function", {}).get("name", "")
        if sname and sname not in existing_schema_names:
            schema_registry.append(schema)

    return added

    return added
