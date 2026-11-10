"""
market_data_client.py — Arthera unified real-time market data client.

Design principles
─────────────────
1. Proxy-bypassed  : uses requests.Session(trust_env=False) so Chinese data
   sources work even when HTTP_PROXY / HTTPS_PROXY is set (VPN / Clash).
2. Multi-source fallback chain:
     US/Global  → yfinance (primary)  → Alpha Vantage (if key set)
     A-shares   → Eastmoney push2 API → AKShare (historical)
     Crypto     → ccxt (binance/okx)  → yfinance fallback
3. Unified output schema — every function returns a consistent dict so callers
   don't care which data source actually served the data.
4. No blocking calls inside async context — use run_in_executor where needed.

Quick usage
───────────
    from market_data_client import MarketDataClient
    mdc = MarketDataClient()
    print(mdc.quote("NVDA"))
    print(mdc.quote("000001"))          # A-share
    print(mdc.quote("BTC/USDT"))        # crypto
    print(mdc.history("AAPL", days=30))
    print(mdc.indices())                # major global indices
    print(mdc.northbound_flow())        # 北向资金
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import numpy as np
    import pandas as pd
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] in {"numpy", "pandas"}:
        raise ModuleNotFoundError(
            "Local market tools require the optional finance extra. "
            "Install with: pip install 'aria-code[finance]<4'",
            name=exc.name,
        ) from None
    raise
import requests
from aria_code.packages.aria_core.paths import aria_home

logger = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("curl_cffi").setLevel(logging.CRITICAL)

_UNSET = object()  # sentinel: "not yet resolved" (distinct from a resolved None)

# 东方财富 API 公开默认令牌（非个人凭证，各开源项目通用值）
# 可通过环境变量覆盖：export EASTMONEY_UT=your_token
_EM_UT = os.environ.get("EASTMONEY_UT", "bd1d9ddb04089700cf9c27f6f7426281")


def _friendly_market_error(symbol: str, providers: List[str], detail: Any = "") -> str:
    """Return a user-facing market data error without leaking vendor internals.

    Naming the attempted public providers makes the fallback state actionable,
    while deliberately omitting raw exception details, request URLs and keys.
    """
    safe_symbol = str(symbol or "当前标的").strip()[:24] or "当前标的"
    labels = {
        "eastmoney": "Eastmoney",
        "tencent": "腾讯",
        "sina": "新浪",
        "akshare": "AKShare",
        "yfinance": "Yahoo Finance",
    }
    chain = " -> ".join(labels.get(str(provider).lower(), str(provider)) for provider in providers if provider)
    suffix = f"已尝试 {chain}。" if chain else ""
    return f"暂时无法获取 {safe_symbol} 的行情数据，请稍后重试。{suffix}"


def _is_valid_price(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False

# ── Simple in-process cache (TTL-based) ─────────────────────────────────────

class _Cache:
    def __init__(self):
        self._store: Dict[str, tuple] = {}  # key → (value, expire_ts)
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() < entry[1]:
                return entry[0]
            return None

    def set(self, key: str, value, ttl: int = 60):
        with self._lock:
            self._store[key] = (value, time.time() + ttl)

_cache = _Cache()


def _session() -> requests.Session:
    """Return a requests Session for Chinese financial APIs.

    Uses the system proxy (HTTP_PROXY / HTTPS_PROXY) when set — users outside
    China need a proxy/VPN to reach Eastmoney servers.  Previously trust_env=False
    was set here, which bypassed the proxy and caused connection failures for
    non-China IPs.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://finance.eastmoney.com/",
    })
    return s


def _session_no_proxy() -> requests.Session:
    """Return a requests Session that explicitly bypasses any system proxy.

    Use for globally-accessible endpoints (Yahoo Finance, Alpha Vantage) that
    should NOT go through a China-routing VPN.
    """
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    })
    return s


# ── Symbol classification ────────────────────────────────────────────────────

def _is_ashare(symbol: str) -> bool:
    s = symbol.strip().upper()
    if s.endswith((".SZ", ".SS", ".SH")):
        s = s.rsplit(".", 1)[0]
    digits = s.lstrip("SZ").lstrip("SH")
    return (
        (s.startswith(("60","00","30","68","83","87")) and s.isdigit() and len(s) == 6)
        or (s.startswith(("SH", "SZ")) and s[2:].isdigit() and len(s[2:]) == 6)
        or digits.isdigit() and len(digits) == 6
    )

def _is_crypto(symbol: str) -> bool:
    s = symbol.upper()
    return "/" in s or s.endswith(("USDT","BTC","ETH","BNB","-USD","-USDT"))

def _norm_crypto(symbol: str, quote: str = "USDT") -> str:
    """Normalise a crypto symbol to ccxt 'BASE/QUOTE' form.

    Fixes the rstrip bug: 'DOTUSDT'.rstrip('USDT') → 'DO' (strips chars, not
    the suffix). Here we strip the quote suffix exactly.
    """
    s = symbol.upper().strip()
    if "/" in s:
        return s
    if "-" in s:
        base, quote_part = s.split("-", 1)
        quote_norm = "USDT" if quote_part == "USD" else quote_part
        return f"{base}/{quote_norm}"
    for q in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q if q != 'USD' else 'USDT'}"
    return f"{s}/{quote}"

def _normalise_ashare(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith((".SZ", ".SS", ".SH")):
        s = s.rsplit(".", 1)[0]
    s = s.lstrip("SH").lstrip("SZ")
    s = s.lstrip("0") if s.startswith(("60","00","30","68","83","87")) else s
    return s.zfill(6)

def _ashare_secid(code: str, original: str = "") -> str:
    """Convert 6-digit code → Eastmoney secid (1.XXXXXX=沪 / 0.XXXXXX=深).

    原符号带交易所后缀时以后缀为准:上证指数族(000001.SS 上证综指/000300.SS
    沪深300/000905.SS 中证500…)与深市个股(000001.SZ 平安银行…)共享 000 前缀,
    仅凭前缀猜会把"上证指数"解析成平安银行——跨市场张冠李戴。
    """
    code = code.zfill(6)
    o = (original or "").strip().upper()
    if o.endswith((".SS", ".SH")):
        return f"1.{code}"   # 上交所(含上证指数族)
    if o.endswith(".SZ"):
        return f"0.{code}"   # 深交所
    if code.startswith(("60", "68", "83", "87")):
        return f"1.{code}"   # 上交所
    return f"0.{code}"       # 深交所


def _is_sh_index_family(code: str, original: str = "") -> bool:
    """上证指数族:.SS/.SH 后缀 + 000/880/999 前缀(个股 000xxx 只在深市)。"""
    o = (original or "").strip().upper()
    return o.endswith((".SS", ".SH")) and code.zfill(6).startswith(("000", "880", "999"))


def _yf_ashare_symbol(code: str, original: str = "") -> str:
    """裸 6 位码 → yfinance 符号;原符号带后缀时以后缀为准(同 secid 的撞码问题)。"""
    o = (original or "").strip().upper()
    if o.endswith((".SS", ".SH")):
        return code + ".SS"
    if o.endswith(".SZ"):
        return code + ".SZ"
    return code + (".SS" if code.startswith(("6", "68", "83", "87")) else ".SZ")


# 市场后缀 → 报价货币(兜底数据源 stooq/yfinance_download 不带货币字段时用;
# 与 NL 分析块的"货币单位:{currency}"直接挂钩,硬编码 USD 会让欧股/港股说错货币)
_SUFFIX_CURRENCY = {
    ".SS": "CNY", ".SZ": "CNY", ".SH": "CNY",
    ".HK": "HKD",
    ".T": "JPY",
    ".L": "GBP",
    ".DE": "EUR", ".PA": "EUR", ".AS": "EUR", ".MI": "EUR", ".MC": "EUR",
    ".KS": "KRW", ".KQ": "KRW",
    ".AX": "AUD",
    ".TO": "CAD", ".V": "CAD",
    ".SW": "CHF",
    ".SI": "SGD",
}


def _currency_for_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    for suffix, cur in _SUFFIX_CURRENCY.items():
        if s.endswith(suffix):
            return cur
    return "USD"


def _finnhub_style_symbol(symbol: str) -> bool:
    """Finnhub 免费档只覆盖美股普通代码;指数(^)/期货(=F)/外汇(=X)/带市场后缀
    的港欧日股必然失败——直接跳过省一次 6s 超时往返,由 yfinance 承接。"""
    s = str(symbol or "").strip().upper()
    if not s or s.startswith("^") or "=" in s:
        return False
    for suffix in _SUFFIX_CURRENCY:
        if s.endswith(suffix):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MarketDataClient
# ═══════════════════════════════════════════════════════════════════════════

class MarketDataClient:
    """Unified market data access with proxy bypass and multi-source fallback."""

    EM_QUOTE_URL   = "https://push2.eastmoney.com/api/qt/stock/get"
    EM_ULIST_URL   = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    EM_NORTHBOUND  = "https://push2.eastmoney.com/api/qt/kamt/get"
    EM_HIST_URL    = "https://push2.eastmoney.com/api/qt/stock/kline/get"
    EM_HOT_URL     = "https://push2.eastmoney.com/api/qt/clist/get"
    EM_LIMIT_URL   = "https://push2.eastmoney.com/api/qt/clist/get"

    # Eastmoney field map for stock quote
    _EM_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f169,f170,f171,f116,f117,f162,f167,f168"

    def __init__(self, alpha_vantage_key: str = ""):
        self._sess   = _session()
        self._sess_np = None  # lazy no-proxy session for proxy-bypass fallback
        self._av_key = alpha_vantage_key or os.getenv("ALPHA_VANTAGE_KEY", "")
        self._fh_key = self._load_finnhub_key()
        self._ts_source = _UNSET  # lazy Tushare source (None once resolved & unconfigured)

    def _tushare(self):
        """Lazily resolve the user's configured Tushare source.

        Returns a ``TushareSource`` only when TUSHARE_TOKEN is set (env or
        ~/.aria|.arthera/.env); otherwise ``None``. Users who never configured
        Tushare pay nothing — the A-share chain falls straight through to the
        free HTTP sources. When a token *is* present we honour it as the
        preferred A-share source (it is the user's explicit, reliable choice,
        especially behind the GFW where Eastmoney/AKShare can be flaky).
        """
        if self._ts_source is _UNSET:
            try:
                from datasources.sources.tushare_source import TushareSource
                src = TushareSource()
                self._ts_source = src if src.is_configured() else None
            except Exception as e:
                logger.debug("Tushare source unavailable: %s", e)
                self._ts_source = None
        return self._ts_source

    def _em_get_json(self, url: str, params: dict, timeout: int = 8):
        """GET JSON from an eastmoney endpoint, resilient to flaky hosts/proxy.

        Two failure modes are handled together:
          * A broken HTTP(S)_PROXY returns empty bodies / ProxyError → retry
            with a trust_env=False (no-proxy) session.
          * eastmoney's push2 cluster has many numbered hosts
            (N.push2.eastmoney.com) that go down individually → rotate hosts
            until one responds with valid JSON.
        """
        import re as _re
        # Build a small candidate host list (original + a couple numbered hosts).
        # Kept short so we fail fast and don't hammer eastmoney's rate limiter.
        m = _re.search(r"https?://([^/]+)(/.*)", url)
        if m and "push2.eastmoney.com" in m.group(1):
            path = m.group(2)
            _hosts = list(dict.fromkeys([m.group(1), "1.push2.eastmoney.com", "7.push2.eastmoney.com"]))
            _urls = [f"https://{h}{path}" for h in _hosts]
        else:
            _urls = [url]

        if self._sess_np is None:
            self._sess_np = _session_no_proxy()
        for candidate in _urls:
            for sess in (self._sess, self._sess_np):
                try:
                    r = sess.get(candidate, params=params, timeout=timeout)
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict) and data.get("data") is not None:
                        return data
                except Exception:
                    continue
        return None

    @staticmethod
    def _load_finnhub_key() -> str:
        """Read Finnhub API key from env var or ~/.arthera/providers.json."""
        key = os.getenv("FINNHUB_API_KEY", "") or os.getenv("FINNHUB_KEY", "")
        if key:
            return key
        try:
            p = aria_home() / "providers.json"
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                key = raw.get("data", {}).get("finnhub", {}).get("api_key", "")
                if key:
                    return key
        except Exception:
            pass
        return ""

    def _quote_finnhub(self, symbol: str) -> Dict[str, Any]:
        """Finnhub quote fallback — uses configured API key."""
        if not self._fh_key:
            return {"success": False, "error": "no finnhub key", "symbol": symbol}
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={symbol.upper()}&token={self._fh_key}"
            r   = self._sess.get(url, timeout=6)
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}", "symbol": symbol}
            d = r.json()
            price = float(d.get("c") or 0)
            if price <= 0:
                return {"success": False, "error": "price=0 from finnhub", "symbol": symbol}
            prev = float(d.get("pc") or price)
            chg_p = round(float(d.get("dp") or 0), 2)
            name = symbol
            mktcap = None
            currency = "USD"
            try:
                prof_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol.upper()}&token={self._fh_key}"
                pr = self._sess.get(prof_url, timeout=5).json()
                name = pr.get("name") or symbol
                mktcap = (float(pr.get("marketCapitalization") or 0) * 1e6) or None
                currency = pr.get("currency") or "USD"
            except Exception:
                pass
            # Finnhub /quote has no volume field — enrich from yfinance fast_info
            # so 成交量 isn't always 0 for US stocks (finnhub is primary here).
            _vol = int(d.get("v") or 0)
            if _vol == 0:
                try:
                    import yfinance as _yf
                    _fi = _yf.Ticker(symbol).fast_info
                    _vol = int(getattr(_fi, "last_volume", 0)
                               or getattr(_fi, "ten_day_average_volume", 0) or 0)
                except Exception:
                    _vol = 0
            return {
                "success":    True,
                "symbol":     symbol.upper(),
                "name":       name,
                "price":      price,
                "change":     round(price - prev, 4),
                "change_pct": chg_p,
                "volume":     _vol,
                "market_cap": mktcap,
                "high":       round(float(d.get("h") or 0), 2),
                "low":        round(float(d.get("l") or 0), 2),
                "open":       round(float(d.get("o") or 0), 4),
                "prev_close": round(prev, 4),
                "currency":   currency,
                "market":     "US",
                "provider":   "finnhub",
                "timestamp":  datetime.now().isoformat(),
            }
        except Exception as e:
            return {"success": False, "error": str(e), "symbol": symbol}

    def _history_finnhub(self, symbol: str, days: int, interval: str) -> Dict[str, Any]:
        """Finnhub candle history fallback."""
        if not self._fh_key:
            return {"success": False, "error": "no finnhub key", "symbol": symbol}
        resolution = "D" if interval in ("1d", "day", "daily") else "60"
        _end   = int(time.time())
        _start = int((datetime.now() - timedelta(days=days + 5)).timestamp())
        try:
            url = (f"https://finnhub.io/api/v1/stock/candle?symbol={symbol.upper()}"
                   f"&resolution={resolution}&from={_start}&to={_end}&token={self._fh_key}")
            r = self._sess.get(url, timeout=10)
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}", "symbol": symbol}
            d = r.json()
            if d.get("s") != "ok" or not d.get("c"):
                return {"success": False, "error": "no candle data", "symbol": symbol}
            records = [
                {
                    "date":   datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "open":   round(float(o), 4),
                    "high":   round(float(h), 4),
                    "low":    round(float(l), 4),
                    "close":  round(float(c), 4),
                    "volume": int(v),
                }
                for t, o, h, l, c, v in zip(
                    d["t"], d["o"], d["h"], d["l"], d["c"], d.get("v", [0]*len(d["c"]))
                )
            ]
            return {
                "success": True, "symbol": symbol.upper(),
                "data": records, "provider": "finnhub",
                "interval": interval, "count": len(records),
            }
        except Exception as e:
            return {"success": False, "error": str(e), "symbol": symbol}

    # ── Public API ───────────────────────────────────────────────────────────

    def quote(self, symbol: str) -> Dict[str, Any]:
        """Real-time quote for US stock / A-share / crypto / index.

        Returns unified dict:
          symbol, name, price, change, change_pct, volume, market_cap,
          high, low, open, prev_close, provider, timestamp
        """
        ckey = f"quote:{symbol}"
        cached = _cache.get(ckey)
        if cached:
            return cached

        if _is_ashare(symbol):
            result = self._quote_ashare(symbol)
        elif _is_crypto(symbol):
            result = self._quote_crypto(symbol)
        elif self._fh_key and _finnhub_style_symbol(symbol):
            # Finnhub is primary for US stocks — faster, no rate limits.
            # 指数/期货/外汇/带市场后缀的港欧日股不走 finnhub(免费档必失败,
            # 白付一次往返),直接 yfinance。
            result = self._quote_finnhub(symbol)
            if not result.get("success"):
                result = self._quote_yfinance(symbol)
        else:
            result = self._quote_yfinance(symbol)

        if result.get("success"):
            _cache.set(ckey, result, ttl=30)   # 30s cache for quotes
        return result

    def history(self, symbol: str, days: int = 252,
                interval: str = "1d") -> Dict[str, Any]:
        """OHLCV history as a list of dicts (sorted ascending by date).

        Returns: {success, symbol, data: [{date,open,high,low,close,volume},...],
                  provider}
        """
        ckey = f"hist:{symbol}:{days}:{interval}"
        cached = _cache.get(ckey)
        if cached:
            return cached

        if _is_ashare(symbol):
            result = self._history_ashare(symbol, days, interval)
        elif _is_crypto(symbol):
            result = self._history_crypto(symbol, days, interval)
        else:
            result = self._history_yfinance(symbol, days, interval)

        if result.get("success"):
            _cache.set(ckey, result, ttl=300)  # 5min cache for history
        elif "rate" in str(result.get("error", "")).lower():
            # Brief negative cache: stops every agent in a /team run from
            # re-hammering the same rate-limited symbol with its own backoff.
            _cache.set(ckey, result, ttl=20)
        return result

    def indices(self) -> Dict[str, Any]:
        """Real-time major global and Chinese indices."""
        ckey = "indices:global"
        cached = _cache.get(ckey)
        if cached:
            return cached
        result = self._fetch_indices()
        if result.get("success"):
            _cache.set(ckey, result, ttl=60)
        return result

    def northbound_flow(self) -> Dict[str, Any]:
        """北向资金 (沪股通+深股通) net buy/sell today."""
        ckey = "northbound"
        cached = _cache.get(ckey)
        if cached:
            return cached
        result = self._fetch_northbound()
        if result.get("success"):
            _cache.set(ckey, result, ttl=120)
        return result

    def hot_stocks(self, market: str = "cn", top_n: int = 20) -> Dict[str, Any]:
        """热门/活跃股票榜单."""
        ckey = f"hot:{market}:{top_n}"
        cached = _cache.get(ckey)
        if cached:
            return cached
        if market == "cn":
            result = self._fetch_hot_ashare(top_n)
        else:
            result = self._fetch_hot_us(top_n)
        if result.get("success"):
            _cache.set(ckey, result, ttl=120)
        return result

    def multi_quote(self, symbols: List[str]) -> Dict[str, Any]:
        """Batch quotes for multiple symbols."""
        results = {}
        for sym in symbols:
            r = self.quote(sym)
            results[sym] = r
        return {"success": True, "quotes": results}

    def technical_indicators(self, symbol: str, days: int = 120) -> Dict[str, Any]:
        """Compute RSI, MACD, Bollinger Bands, MA from history data."""
        hist = self.history(symbol, days=days)
        if not hist.get("success"):
            return hist
        try:
            df = pd.DataFrame(hist["data"])
            if df.empty:
                return {"success": False, "error": "empty history dataframe", "symbol": symbol}
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["high"]  = pd.to_numeric(df.get("high",  df["close"]), errors="coerce")
            df["low"]   = pd.to_numeric(df.get("low",   df["close"]), errors="coerce")
            df.dropna(subset=["close"], inplace=True)
            close = df["close"]
            n = len(close)
            if n < 2:
                return {"success": False, "error": f"insufficient data: {n} bars", "symbol": symbol}

            result: Dict[str, Any] = {
                "success": True,
                "symbol": symbol,
                "provider": "local_pandas",
                "data_provider": hist.get("provider"),
                "provider_chain": hist.get("provider_chain") or [hist.get("provider", "history")],
            }

            # Current price (always available if n >= 1)
            result["price"] = round(float(close.iloc[-1]), 4)

            # RSI(14) — needs at least 15 bars
            if n >= 15:
                delta = close.diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rs    = gain / loss.replace(0, np.nan)
                rsi_s = 100 - 100 / (1 + rs)
                rsi_v = rsi_s.iloc[-1]
                result["rsi"] = round(float(rsi_v), 2) if not np.isnan(rsi_v) else None

            # MACD(12,26,9) — needs at least 27 bars
            if n >= 27:
                ema12  = close.ewm(span=12).mean()
                ema26  = close.ewm(span=26).mean()
                macd_l = ema12 - ema26
                sig_l  = macd_l.ewm(span=9).mean()
                hist_m = macd_l - sig_l
                result["macd"]       = round(float(macd_l.iloc[-1]), 4)
                result["macd_signal"]= round(float(sig_l.iloc[-1]),  4)
                result["macd_hist"]  = round(float(hist_m.iloc[-1]), 4)

            # Bollinger Bands(20) — needs at least 20 bars
            if n >= 20:
                ma20  = close.rolling(20).mean()
                std20 = close.rolling(20).std()
                bb_u  = (ma20 + 2 * std20).iloc[-1]
                bb_l  = (ma20 - 2 * std20).iloc[-1]
                bb_m  = ma20.iloc[-1]
                if not any(np.isnan(v) for v in (bb_u, bb_l, bb_m)):
                    result["bb_upper"] = round(float(bb_u), 4)
                    result["bb_mid"]   = round(float(bb_m), 4)
                    result["bb_lower"] = round(float(bb_l), 4)
                    result["bb_position"] = round(
                        (result["price"] - float(bb_l)) /
                        max(float(bb_u - bb_l), 1e-9), 4
                    )

            # Moving averages
            for ma_n in [5, 10, 20, 60, 120]:
                if n >= ma_n:
                    v = close.rolling(ma_n).mean().iloc[-1]
                    if not np.isnan(v):
                        result[f"ma{ma_n}"] = round(float(v), 4)

            return result
        except Exception as e:
            return {"success": False, "error": str(e), "symbol": symbol}

    def fundamentals(self, symbol: str) -> Dict[str, Any]:
        """US stock fundamentals via yfinance."""
        if _is_ashare(symbol):
            return self._fundamentals_ashare(symbol)
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            info = t.info or {}
            return {
                "success":     True,
                "symbol":      symbol,
                "name":        info.get("longName",""),
                "sector":      info.get("sector",""),
                "industry":    info.get("industry",""),
                "market_cap":  info.get("marketCap"),
                "pe_ratio":    info.get("trailingPE"),
                "fwd_pe":      info.get("forwardPE"),
                "pb_ratio":    info.get("priceToBook"),
                "ps_ratio":    info.get("priceToSalesTrailing12Months"),
                "ev_ebitda":   info.get("enterpriseToEbitda"),
                # ROE / revenue growth — yfinance returns ratios (0.12), the
                # agent expects percent (12), so ×100. Fixes 基本面 数据不足.
                "roe":         (info["returnOnEquity"] * 100
                                if info.get("returnOnEquity") is not None else None),
                "revenue_growth": (info["revenueGrowth"] * 100
                                   if info.get("revenueGrowth") is not None else None),
                "revenue":     info.get("totalRevenue"),
                "net_income":  info.get("netIncomeToCommon"),
                "eps":         info.get("trailingEps"),
                "fwd_eps":     info.get("forwardEps"),
                "dividend_yield": info.get("dividendYield"),
                "beta":        info.get("beta"),
                "52w_high":    info.get("fiftyTwoWeekHigh"),
                "52w_low":     info.get("fiftyTwoWeekLow"),
                "analyst_target": info.get("targetMeanPrice"),
                "recommendation": info.get("recommendationKey"),
                "employees":   info.get("fullTimeEmployees"),
                "description": (info.get("longBusinessSummary","")[:300]
                                if info.get("longBusinessSummary") else ""),
                "provider": "yfinance",
            }
        except Exception as e:
            # Finnhub fundamentals fallback
            if self._fh_key:
                try:
                    m_url = (f"https://finnhub.io/api/v1/stock/metric?symbol={symbol.upper()}"
                             f"&metric=all&token={self._fh_key}")
                    m_r = self._sess.get(m_url, timeout=8)
                    if m_r.status_code == 200:
                        m = m_r.json().get("metric") or {}
                        p_url = (f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol.upper()}"
                                 f"&token={self._fh_key}")
                        p_r = self._sess.get(p_url, timeout=5)
                        prof = p_r.json() if p_r.status_code == 200 else {}
                        return {
                            "success":        True,
                            "symbol":         symbol,
                            "name":           prof.get("name", symbol),
                            "sector":         prof.get("gsector", ""),
                            "industry":       prof.get("gind", ""),
                            "market_cap":     (float(prof.get("marketCapitalization") or 0) * 1e6) or None,
                            "pe_ratio":       m.get("peTTM"),
                            "fwd_pe":         m.get("peExclExtraTTM"),
                            "pb_ratio":       m.get("pbAnnual") or m.get("pbQuarterly"),
                            "ev_ebitda":      m.get("currentEv/freeCashFlowAnnual"),
                            "dividend_yield": m.get("dividendYieldIndicatedAnnual"),
                            # ROE + revenue growth (finnhub metric=all has these) —
                            # fixes 基本面 ROE/营收增速 showing 数据不足.
                            "roe":            m.get("roeTTM") or m.get("roeRfy") or m.get("roeAnnual"),
                            "revenue_growth": (m.get("revenueGrowthTTMYoy")
                                               or m.get("revenueGrowthQuarterlyYoy")
                                               or m.get("revenueGrowth5Y")),
                            "52w_high":       m.get("52WeekHigh"),
                            "52w_low":        m.get("52WeekLow"),
                            "beta":           m.get("beta"),
                            "eps":            m.get("epsInclExtraItemsTTM"),
                            "provider":       "finnhub",
                        }
                except Exception:
                    pass
            return {"success": False, "error": str(e), "symbol": symbol}

    # ── US / Global (yfinance) ───────────────────────────────────────────────

    def _quote_yfinance(self, symbol: str) -> Dict[str, Any]:
        try:
            import yfinance as yf
        except Exception:
            yc = self._quote_yahoo_chart(symbol)
            if yc.get("success"):
                return yc
            stooq = self._quote_stooq(symbol)
            if stooq.get("success"):
                return stooq
            return {"success": False, "error": "yfinance unavailable", "symbol": symbol}

        def _attempt_fast_info():
            t  = yf.Ticker(symbol)
            fi = t.fast_info
            info = {}
            try:
                info = t.info or {}
            except Exception as _e:
                logger.debug("yfinance t.info slow/failed for %s: %s", symbol, _e)
            price = float(fi.last_price or 0)
            prev  = float(fi.previous_close or price)
            chg   = price - prev
            chg_p = chg / prev * 100 if prev else 0
            return {
                "success":    True,
                "symbol":     symbol.upper(),
                "name":       info.get("longName","") or info.get("shortName",""),
                "price":      round(price, 4),
                "change":     round(chg, 4),
                "change_pct": round(chg_p, 2),
                "volume":     int(fi.three_month_average_volume or 0),
                "market_cap": fi.market_cap,
                "high":       round(float(fi.day_high or 0), 2),
                "low":        round(float(fi.day_low  or 0), 2),
                "open":       round(float(fi.open     or 0), 4),
                "prev_close": round(prev, 4),
                "currency":   fi.currency or "USD",
                "market":     "US",
                "provider":   "yfinance",
                "timestamp":  datetime.now().isoformat(),
            }

        # Primary attempt
        try:
            return _attempt_fast_info()
        except Exception as e:
            err_text = str(e).lower()
            is_rate_limit = any(t in err_text for t in ("too many", "rate", "429", "429"))
            if not is_rate_limit:
                yc = self._quote_yahoo_chart(symbol)
                if yc.get("success"):
                    return yc
                stooq = self._quote_stooq(symbol)
                if stooq.get("success"):
                    return stooq
                return {"success": False, "error": str(e), "symbol": symbol}

        # Rate-limited: wait 3s then retry once
        logger.debug("yfinance rate-limited for %s, retrying in 3s…", symbol)
        time.sleep(3)
        try:
            return _attempt_fast_info()
        except Exception:
            pass

        # Final fallback: yf.download (different API endpoint, avoids rate limit)
        try:
            from datetime import date as _date, timedelta as _td
            df = yf.download(
                symbol,
                start=(_date.today() - _td(days=5)).isoformat(),
                end=_date.today().isoformat(),
                interval="1d", auto_adjust=True, progress=False, timeout=15,
            )
            if not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.droplevel(1) if len(df.columns.levels) > 1 else df.columns
                last = df.iloc[-1]
                price = round(float(last.get("Close", 0)), 4)
                prev_row = df.iloc[-2] if len(df) >= 2 else last
                prev  = round(float(prev_row.get("Close", price)), 4)
                chg   = round(price - prev, 4)
                chg_p = round(chg / prev * 100 if prev else 0, 2)
                return {
                    "success":    True,
                    "symbol":     symbol.upper(),
                    "name":       "",
                    "price":      price,
                    "change":     chg,
                    "change_pct": chg_p,
                    "volume":     int(last.get("Volume", 0)),
                    "market_cap": None,
                    "high":       round(float(last.get("High", 0)), 2),
                    "low":        round(float(last.get("Low",  0)), 2),
                    "open":       round(float(last.get("Open", 0)), 4),
                    "prev_close": prev,
                    "currency":   _currency_for_symbol(symbol),
                    "market":     "US",
                    "provider":   "yfinance_download",
                    "timestamp":  datetime.now().isoformat(),
                }
        except Exception as _dl_err:
            logger.debug("yfinance download fallback also failed for %s: %s", symbol, _dl_err)

        # Finnhub fallback when yfinance is completely exhausted
        if self._fh_key:
            fh = self._quote_finnhub(symbol)
            if fh.get("success"):
                return fh

        yc = self._quote_yahoo_chart(symbol)
        if yc.get("success"):
            return yc

        stooq = self._quote_stooq(symbol)
        if stooq.get("success"):
            return stooq

        return {"success": False, "error": "yfinance rate-limited or no data", "symbol": symbol}

    @staticmethod
    def _stooq_symbol(symbol: str) -> str:
        """Best-effort conversion from Yahoo-style tickers to Stooq tickers."""
        s = (symbol or "").strip().lower()
        if not s:
            return s
        if s.startswith("^") or "=" in s:
            return ""
        if "." not in s:
            return f"{s}.us"
        suffix_map = {
            "de": "de",
            "pa": "fr",
            "as": "nl",
            "mi": "it",
            "mc": "es",
            "ls": "pt",
            "sw": "ch",
            "l": "uk",
            "hk": "hk",
        }
        base, suffix = s.rsplit(".", 1)
        return f"{base}.{suffix_map.get(suffix, suffix)}"

    def _history_stooq(self, symbol: str, days: int, interval: str = "1d") -> Dict[str, Any]:
        if interval not in ("1d", "day", "daily"):
            return {"success": False, "error": "stooq only supports daily history", "symbol": symbol}
        stooq_symbol = self._stooq_symbol(symbol)
        if not stooq_symbol:
            return {"success": False, "error": "unsupported stooq symbol", "symbol": symbol}
        try:
            r = self._sess.get(
                "https://stooq.com/q/d/l/",
                params={"s": stooq_symbol, "i": "d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            text = getattr(r, "text", "")
            if not text and hasattr(r, "content"):
                text = r.content.decode("utf-8", errors="ignore")
            if not text or "No data" in text:
                raise ValueError("empty Stooq response")
            from io import StringIO
            df = pd.read_csv(StringIO(text))
            if df.empty or "Close" not in df.columns:
                raise ValueError("empty Stooq dataframe")
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            df = df.dropna(subset=["Date", "Close"]).sort_values("Date").tail(days + 5)
            records = []
            for _, row in df.iterrows():
                records.append({
                    "date":   str(row["Date"].date()),
                    "open":   round(float(row.get("Open", row.get("Close", 0))), 4),
                    "high":   round(float(row.get("High", row.get("Close", 0))), 4),
                    "low":    round(float(row.get("Low", row.get("Close", 0))), 4),
                    "close":  round(float(row.get("Close", 0)), 4),
                    "volume": int(float(row.get("Volume", 0) or 0)),
                })
            if not records:
                raise ValueError("empty Stooq records")
            return {
                "success": True,
                "symbol": symbol.upper(),
                "data": records,
                "provider": "stooq",
                "provider_chain": ["yfinance", "finnhub", "stooq"],
                "count": len(records),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "symbol": symbol}

    def _history_yahoo_chart(self, symbol: str, days: int, interval: str = "1d") -> Dict[str, Any]:
        """Direct Yahoo chart endpoint fallback independent of yfinance objects."""
        iv_map = {"1d": "1d", "1h": "1h", "15m": "15m", "5m": "5m"}
        iv = iv_map.get(interval, "1d")
        p2 = int(time.time())
        p1 = p2 - max(days + 5, 30) * 86400
        try:
            r = self._sess.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={
                    "period1": p1,
                    "period2": p2,
                    "interval": iv,
                    "events": "history",
                    "includeAdjustedClose": "true",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.yahoo.com/"},
                timeout=12,
            )
            data = r.json()
            result = (data.get("chart", {}).get("result") or [None])[0]
            if not result:
                raise ValueError("empty Yahoo chart result")
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            timestamps = result.get("timestamp") or []
            closes = quote.get("close") or []
            def _q_at(name: str, idx: int, fallback=0):
                values = quote.get(name) or []
                try:
                    value = values[idx]
                    return fallback if value is None else value
                except Exception:
                    return fallback
            records = []
            for idx, ts in enumerate(timestamps):
                try:
                    close = closes[idx]
                    if close is None:
                        continue
                    records.append({
                        "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                        "open": round(float(_q_at("open", idx, close) or close), 4),
                        "high": round(float(_q_at("high", idx, close) or close), 4),
                        "low": round(float(_q_at("low", idx, close) or close), 4),
                        "close": round(float(close), 4),
                        "volume": int(float(_q_at("volume", idx, 0) or 0)),
                    })
                except Exception:
                    continue
            if not records:
                raise ValueError("empty Yahoo chart records")
            return {
                "success": True,
                "symbol": symbol.upper(),
                "data": records,
                "provider": "yahoo_chart",
                "provider_chain": ["yfinance", "yahoo_chart"],
                "count": len(records),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "symbol": symbol}

    def _quote_stooq(self, symbol: str) -> Dict[str, Any]:
        hist = self._history_stooq(symbol, days=7, interval="1d")
        if not hist.get("success"):
            return hist
        records = hist.get("data") or []
        if not records:
            return {"success": False, "error": "empty Stooq quote records", "symbol": symbol}
        last = records[-1]
        prev = records[-2] if len(records) >= 2 else last
        price = float(last.get("close") or 0)
        prev_close = float(prev.get("close") or price)
        change = price - prev_close
        change_pct = change / prev_close * 100 if prev_close else 0
        return {
            "success": True,
            "symbol": symbol.upper(),
            "name": symbol.upper(),
            "price": round(price, 4),
            "change": round(change, 4),
            "change_pct": round(change_pct, 2),
            "volume": int(last.get("volume") or 0),
            "market_cap": None,
            "high": round(float(last.get("high") or price), 2),
            "low": round(float(last.get("low") or price), 2),
            "open": round(float(last.get("open") or price), 4),
            "prev_close": round(prev_close, 4),
            "currency": _currency_for_symbol(symbol),
            "market": "GLOBAL",
            "provider": "stooq",
            "provider_chain": ["yfinance", "finnhub", "stooq"],
            "timestamp": datetime.now().isoformat(),
        }

    def _quote_yahoo_chart(self, symbol: str) -> Dict[str, Any]:
        hist = self._history_yahoo_chart(symbol, days=7, interval="1d")
        if not hist.get("success"):
            return hist
        records = hist.get("data") or []
        if not records:
            return {"success": False, "error": "empty Yahoo chart quote records", "symbol": symbol}
        last = records[-1]
        prev = records[-2] if len(records) >= 2 else last
        price = float(last.get("close") or 0)
        prev_close = float(prev.get("close") or price)
        if price <= 0:
            return {"success": False, "error": "price=0 from Yahoo chart", "symbol": symbol}
        change = price - prev_close
        change_pct = change / prev_close * 100 if prev_close else 0
        meta_currency = ""
        return {
            "success": True,
            "symbol": symbol.upper(),
            "name": symbol.upper(),
            "price": round(price, 4),
            "change": round(change, 4),
            "change_pct": round(change_pct, 2),
            "volume": int(last.get("volume") or 0),
            "market_cap": None,
            "high": round(float(last.get("high") or price), 2),
            "low": round(float(last.get("low") or price), 2),
            "open": round(float(last.get("open") or price), 4),
            "prev_close": round(prev_close, 4),
            "currency": meta_currency or "USD",
            "market": "GLOBAL",
            "provider": "yahoo_chart",
            "provider_chain": ["yfinance", "yahoo_chart"],
            "timestamp": datetime.now().isoformat(),
        }

    def _history_yfinance(self, symbol: str, days: int, interval: str) -> Dict[str, Any]:
        try:
            import yfinance as yf
        except Exception:
            yc = self._history_yahoo_chart(symbol, days, interval)
            if yc.get("success"):
                return yc
            stooq = self._history_stooq(symbol, days, interval)
            if stooq.get("success"):
                return stooq
            return {"success": False, "error": "yfinance unavailable", "symbol": symbol}
        period_map = {1: "5d", 5: "5d", 30: "1mo", 60: "3mo",
                      90: "3mo", 120: "6mo", 180: "6mo",
                      252: "1y", 365: "1y", 730: "2y", 1260: "5y"}
        period = period_map.get(days) or f"{days}d"
        iv_map = {"1d": "1d", "1h": "1h", "15m": "15m", "5m": "5m"}
        iv = iv_map.get(interval, "1d")

        def _df_to_records(df) -> list:
            records = []
            for ts, row in df.iterrows():
                records.append({
                    "date":   str(ts.date()) if hasattr(ts, "date") else str(ts)[:10],
                    "open":   round(float(row.get("Open",  row.get("open",  0))), 4),
                    "high":   round(float(row.get("High",  row.get("high",  0))), 4),
                    "low":    round(float(row.get("Low",   row.get("low",   0))), 4),
                    "close":  round(float(row.get("Close", row.get("close", 0))), 4),
                    "volume": int(row.get("Volume", row.get("volume", 0))),
                })
            return records

        # Primary: Ticker.history() with bounded exponential backoff on rate
        # limits (1s, 2s) — gives the provider time to recover before falling
        # through to the download/finnhub fallbacks. Total ≤3s so it stays
        # well within per-agent timeouts.
        for _attempt in range(3):
            try:
                df = yf.Ticker(symbol).history(period=period, interval=iv, auto_adjust=True)
                if not df.empty:
                    records = _df_to_records(df)
                    return {"success": True, "symbol": symbol, "data": records,
                            "provider": "yfinance", "count": len(records)}
                break  # empty (not a rate limit) → go straight to fallback
            except Exception as _e:
                _err = str(_e).lower()
                _is_rl = any(t in _err for t in ("too many", "rate", "429"))
                if _is_rl and _attempt < 2:
                    logger.debug("yfinance history rate-limited for %s, backoff %ss",
                                 symbol, 2 ** _attempt)
                    time.sleep(1.0 * (2 ** _attempt))   # 1s, then 2s
                    continue
                logger.debug("yfinance history primary failed for %s: %s — trying download fallback", symbol, _e)
                break

        # Fallback: yf.download() uses a different API endpoint, more resilient to rate limits
        try:
            from datetime import date, timedelta as _td
            end_dt = date.today()
            start_dt = end_dt - _td(days=days + 5)
            df2 = yf.download(symbol, start=start_dt.isoformat(), end=end_dt.isoformat(),
                              interval=iv, auto_adjust=True, progress=False, timeout=15)
            if not df2.empty:
                # yf.download may return MultiIndex columns when single ticker
                if hasattr(df2.columns, "levels"):
                    df2.columns = df2.columns.droplevel(1) if len(df2.columns.levels) > 1 else df2.columns
                records = _df_to_records(df2)
                return {"success": True, "symbol": symbol, "data": records,
                        "provider": "yfinance_download", "count": len(records)}
        except Exception as _e:
            logger.debug("yfinance download fallback also failed for %s: %s", symbol, _e)

        # Finnhub candle fallback
        yc = self._history_yahoo_chart(symbol, days, interval)
        if yc.get("success"):
            return yc

        if self._fh_key:
            fh = self._history_finnhub(symbol, days, interval)
            if fh.get("success"):
                return fh

        stooq = self._history_stooq(symbol, days, interval)
        if stooq.get("success"):
            return stooq

        return {
            "success": False,
            "error": (
                "global history unavailable: "
                f"yfinance/yahoo_chart/finnhub/stooq failed; "
                f"yahoo_chart={yc.get('error')}; stooq={stooq.get('error')}"
            ),
            "symbol": symbol,
            "provider_chain": ["yfinance", "yahoo_chart", "finnhub", "stooq"],
        }

    # ── A-share (Eastmoney push2 API) ────────────────────────────────────────

    def _quote_ashare(self, symbol: str) -> Dict[str, Any]:
        """A股报价: 用户配置的 Tushare 优先（若有），再东方财富，yfinance 末级 fallback."""
        code = _normalise_ashare(symbol)
        errors: List[str] = []

        # ── 优先路径: 用户配置的 Tushare（仅当 TUSHARE_TOKEN 已设置）──────────
        # 上证指数族(000001.SS 等)跳过 Tushare:裸 6 位码会命中同码深市个股
        # (000001=平安银行),直接走东财(secid 已按后缀区分交易所)。
        _ts = None if _is_sh_index_family(code, symbol) else self._tushare()
        if _ts is not None:
            try:
                q = _ts.quote(code)
                if q is not None and _is_valid_price(float(q.price or 0)):
                    return {
                        "success":    True,
                        "symbol":     code,
                        "name":       q.name or code,
                        "price":      round(float(q.price), 4),
                        "change":     round(float(q.change or 0), 4),
                        "change_pct": round(float(q.change_pct or 0), 2),
                        "volume":     int(float(q.volume or 0)),
                        "currency":   "CNY",
                        "market":     "CN",
                        "provider":   "tushare",
                        "provider_chain": ["tushare"],
                        "timestamp":  q.timestamp or datetime.now().isoformat(),
                    }
            except Exception as ts_err:
                errors.append(f"tushare: {ts_err}")
                logger.debug("Tushare A-share quote failed %s: %s", code, ts_err)

        # ── 主路径: 东方财富 push2 API ─────────────────────────────────
        secid = _ashare_secid(code, original=symbol)
        try:
            _resp = self._em_get_json(self.EM_QUOTE_URL, {
                "secid":  secid,
                "fields": self._EM_FIELDS,
                "ut":     _EM_UT,
                "fltt": 2, "invt": 2,
            }, timeout=6)
            d = (_resp or {}).get("data", {}) or {}
            price = float(d.get("f43", 0))
            if not _is_valid_price(price):
                raise ValueError("empty Eastmoney quote")
            chg      = float(d.get("f169", 0))
            chg_pct  = float(d.get("f170", 0))
            prev     = round(price - chg, 4)  # f46=今开(open), not 昨收; derive from change
            return {
                "success":    True,
                "symbol":     code,
                "name":       d.get("f58", code),
                "price":      price,
                "change":     chg,
                "change_pct": chg_pct,
                # Eastmoney push2 quote fields use lots (手) for f47 and
                # CNY yuan for f48/f116 when fltt=2.  Normalize the public
                # contract to shares/yuan so every downstream renderer uses
                # the same units.
                "volume":     int(float(d.get("f47", 0) or 0) * 100),
                "turnover":   float(d.get("f48", 0)),
                "market_cap": float(d.get("f116", 0)),
                "volume_unit": "shares",
                "turnover_unit": "CNY",
                "market_cap_unit": "CNY",
                "high":       float(d.get("f44", 0)),
                "low":        float(d.get("f45", 0)),
                "open":       float(d.get("f46", 0)),
                "prev_close": prev,
                "currency":   "CNY",
                "market":     "CN",
                "provider":   "eastmoney",
                "provider_chain": ["eastmoney"],
                "timestamp":  datetime.now().isoformat(),
            }
        except Exception as em_err:
            errors.append(f"eastmoney: {em_err}")
            logger.debug("Eastmoney A-share failed %s: %s", code, em_err)

        # ── 备用路径 1: 腾讯行情 qt.gtimg.cn ─────────────────────────────────
        try:
            prefix = "sz" if code.startswith(("0", "3")) else "sh"
            r = self._sess.get(f"https://qt.gtimg.cn/q={prefix}{code}", timeout=6)
            raw = r.text.strip()
            if raw and "=" in raw:
                val = raw.split("=", 1)[1].strip().strip('"').strip("'")
                flds = val.split("~")
                if len(flds) > 10 and flds[3]:
                    price  = float(flds[3])
                    prev   = float(flds[4] or price)
                    chg    = price - prev
                    chg_p  = chg / prev * 100 if prev else 0
                    if _is_valid_price(price):
                        return {
                            "success":    True,
                            "symbol":     code,
                            "name":       flds[1] or code,
                            "price":      round(price, 4),
                            "change":     round(chg, 4),
                            "change_pct": round(chg_p, 2),
                            "volume":     int(float(flds[6] or 0)) * 100,
                            "high":       round(float(flds[33] if len(flds) > 33 and flds[33] else price), 2),
                            "low":        round(float(flds[34] if len(flds) > 34 and flds[34] else price), 2),
                            "open":       round(float(flds[5] or price), 4),
                            "prev_close": round(prev, 4),
                            "currency":   "CNY",
                            "market":     "CN",
                            "provider":   "tencent",
                            "provider_chain": ["eastmoney", "tencent"],
                            "timestamp":  datetime.now().isoformat(),
                        }
                raise ValueError(f"invalid tencent price: {flds[3] if len(flds) > 3 else 'N/A'}")
        except Exception as tx_err:
            errors.append(f"tencent: {tx_err}")
            logger.debug("Tencent A-share failed %s: %s", code, tx_err)

        # ── 备用路径 2: 新浪行情 hq.sinajs.cn ────────────────────────────────
        try:
            prefix = "sz" if code.startswith(("0", "3")) else "sh"
            r = self._sess.get(f"https://hq.sinajs.cn/list={prefix}{code}",
                               headers={"Referer": "https://finance.sina.com.cn/"},
                               timeout=6)
            raw = r.text.strip()
            if raw and "=" in raw:
                val = raw.split("=", 1)[1].strip().strip('"').strip("'")
                flds = val.split(",")
                if len(flds) > 9 and flds[3]:
                    price  = float(flds[3])
                    prev   = float(flds[2] or price)
                    chg    = price - prev
                    chg_p  = chg / prev * 100 if prev else 0
                    if _is_valid_price(price):
                        return {
                            "success":    True,
                            "symbol":     code,
                            "name":       flds[0] or code,
                            "price":      round(price, 4),
                            "change":     round(chg, 4),
                            "change_pct": round(chg_p, 2),
                            "volume":     int(float(flds[8] or 0)),
                            "turnover":   float(flds[9] or 0),
                            "high":       round(float(flds[4] or price), 2),
                            "low":        round(float(flds[5] or price), 2),
                            "open":       round(float(flds[1] or price), 4),
                            "prev_close": round(prev, 4),
                            "currency":   "CNY",
                            "market":     "CN",
                            "provider":   "sina",
                            "provider_chain": ["eastmoney", "tencent", "sina"],
                            "timestamp":  datetime.now().isoformat(),
                        }
                raise ValueError(f"invalid sina price: {flds[3] if len(flds) > 3 else 'N/A'}")
        except Exception as sina_err:
            errors.append(f"sina: {sina_err}")
            logger.debug("Sina A-share failed %s: %s", code, sina_err)

        # ── 备用路径 3: AKShare snapshot（如果本地安装）──────────────────────
        # AKShare uses its own requests sessions; clear proxy env vars so it
        # connects directly instead of routing through the China VPN (which
        # rejects AKShare's Eastmoney endpoints with ProxyError).
        try:
            import akshare as ak
            import os as _ak_os
            _ak_proxy_bk = {k: _ak_os.environ.pop(k, None)
                            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
            try:
                df = ak.stock_zh_a_spot_em()
            finally:
                for _k, _v in _ak_proxy_bk.items():
                    if _v is not None: _ak_os.environ[_k] = _v
            row = df[df["代码"].astype(str) == code]
            if row.empty:
                raise ValueError("empty AKShare quote")
            item = row.iloc[0]
            price = float(item.get("最新价", 0))
            if not _is_valid_price(price):
                raise ValueError("empty AKShare price")
            return {
                "success":    True,
                "symbol":     code,
                "name":       str(item.get("名称", code)),
                "price":      price,
                "change":     float(item.get("涨跌额", 0) or 0),
                "change_pct": float(item.get("涨跌幅", 0) or 0),
                "volume":     int(float(item.get("成交量", 0) or 0)),
                "turnover":   float(item.get("成交额", 0) or 0),
                "market_cap": float(item.get("总市值", 0) or 0),
                "high":       float(item.get("最高", 0) or 0),
                "low":        float(item.get("最低", 0) or 0),
                "open":       float(item.get("今开", 0) or 0),
                "prev_close": float(item.get("昨收", 0) or 0),
                "currency":   "CNY",
                "market":     "CN",
                "provider":   "akshare",
                "provider_chain": ["eastmoney", "tencent", "sina", "akshare"],
                "timestamp":  datetime.now().isoformat(),
            }
        except Exception as ak_err:
            errors.append(f"akshare: {ak_err}")
            logger.debug("AKShare A-share failed %s: %s", code, ak_err)

        # ── 末级 fallback: yfinance via Yahoo Finance（全球可访问，明确绕过代理）──
        # Yahoo Finance is accessible globally; bypass any China-routing proxy so
        # this fallback works even when the VPN/proxy is down.
        try:
            import yfinance as yf
            import os as _os
            yf_sym = _yf_ashare_symbol(code, symbol)
            # Temporarily clear proxy env vars so yfinance connects directly to Yahoo
            _proxy_backup = {k: _os.environ.pop(k, None)
                             for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
            try:
                t  = yf.Ticker(yf_sym)
                fi = t.fast_info
                price = float(fi.last_price or 0)
                if not _is_valid_price(price):
                    # fast_info may return None outside trading hours; use history
                    h = t.history(period="2d", auto_adjust=True)
                    if h.empty:
                        raise ValueError("empty yfinance history")
                    price = float(h["Close"].iloc[-1])
                    prev  = float(h["Close"].iloc[-2]) if len(h) >= 2 else price
                else:
                    prev = float(fi.previous_close or price)
            finally:
                for k, v in _proxy_backup.items():
                    if v is not None:
                        _os.environ[k] = v
            if not _is_valid_price(price):
                raise ValueError("empty yfinance quote")
            chg   = price - prev
            chg_p = chg / prev * 100 if prev else 0
            return {
                "success":    True,
                "symbol":     code,
                "name":       code,
                "price":      round(price, 4),
                "change":     round(chg, 4),
                "change_pct": round(chg_p, 2),
                "volume":     int(getattr(fi, "three_month_average_volume", None) or 0),
                "market_cap": getattr(fi, "market_cap", None),
                "high":       round(float(getattr(fi, "day_high",  None) or 0), 2),
                "low":        round(float(getattr(fi, "day_low",   None) or 0), 2),
                "open":       round(float(getattr(fi, "open",      None) or 0), 4),
                "prev_close": round(prev, 4),
                "currency":   "CNY",
                "market":     "CN",
                "provider":   "yfinance",
                "provider_chain": ["eastmoney", "tencent", "sina", "akshare", "yfinance"],
                "timestamp":  datetime.now().isoformat(),
            }
        except Exception as yf_err:
            errors.append(f"yfinance: {yf_err}")
            logger.debug("yfinance A-share failed %s: %s", code, yf_err)

        return {
            "success": False,
            "symbol": code,
            "market": "CN",
            "provider_chain": ["eastmoney", "tencent", "sina", "akshare", "yfinance"],
            "error": _friendly_market_error(
                code, ["Eastmoney", "腾讯", "新浪", "AKShare", "Yahoo Finance"],
                "; ".join(errors)
            ),
            "debug_error": "; ".join(errors),
        }

    def _history_ashare(self, symbol: str, days: int, interval: str) -> Dict[str, Any]:
        code  = _normalise_ashare(symbol)
        secid = _ashare_secid(code, original=symbol)
        errors: List[str] = []
        klt_map = {"1d": 101, "1w": 102, "1mo": 103, "1h": 60, "30m": 30}
        klt = klt_map.get(interval, 101)
        end_date   = datetime.now().strftime("%Y%m%d%H%M%S")

        # ── 优先路径: 用户配置的 Tushare（仅日线，仅当 TUSHARE_TOKEN 已设置）──
        _ts = self._tushare()
        if _ts is not None and interval == "1d":
            try:
                h = _ts.history(code, days=days)
                if h is not None and h.data is not None and not h.data.empty:
                    records = []
                    for idx, row in h.data.iterrows():
                        records.append({
                            "date":   idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10],
                            "open":   float(row.get("open",   0) or 0),
                            "high":   float(row.get("high",   0) or 0),
                            "low":    float(row.get("low",    0) or 0),
                            "close":  float(row.get("close",  0) or 0),
                            "volume": int(float(row.get("volume", 0) or 0)),
                        })
                    if records:
                        return {"success": True, "symbol": code, "name": code,
                                "data": records, "provider": "tushare",
                                "provider_chain": ["tushare"], "count": len(records)}
            except Exception as ts_err:
                errors.append(f"tushare: {ts_err}")
                logger.debug("Tushare history failed %s: %s", code, ts_err)

        # ── 主路径: 东方财富历史 K线（通过系统代理）──────────────────────────
        try:
            _resp = self._em_get_json(self.EM_HIST_URL, {
                "secid":   secid,
                "klt":     klt,
                "fqt":     1,       # 前复权
                "lmt":     days + 50,
                "end":     end_date,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
                "ut":      _EM_UT,
            }, timeout=10)
            raw = (_resp or {}).get("data", {}) or {}
            name = raw.get("name", code)
            klines = raw.get("klines", [])
            records = []
            for k in klines:
                parts = k.split(",")
                if len(parts) >= 6:
                    records.append({
                        "date":   parts[0],
                        "open":   float(parts[1]),
                        "close":  float(parts[2]),
                        "high":   float(parts[3]),
                        "low":    float(parts[4]),
                        "volume": int(float(parts[5])),
                    })
            if records:
                return {"success": True, "symbol": code, "name": name,
                        "data": records, "provider": "eastmoney",
                        "provider_chain": ["eastmoney"], "count": len(records)}
            raise ValueError("empty Eastmoney kline response")
        except Exception as em_err:
            errors.append(f"eastmoney: {em_err}")
            logger.debug("Eastmoney history failed %s: %s", code, em_err)

        # ── 备用 1: 新浪 K线（scale=240 = 日线，datalen ≈ days）────────────────
        try:
            import json as _json
            prefix = "sz" if code.startswith(("0", "3")) else "sh"
            datalen = min(max(days, 60), 1023)
            r = self._sess.get(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                params={"symbol": f"{prefix}{code}", "scale": 240, "ma": "no", "datalen": datalen},
                headers={"Referer": "https://finance.sina.com.cn/"},
                timeout=10,
            )
            raw_list = _json.loads(r.text) if r.status_code == 200 else []
            records = []
            for item in raw_list:
                records.append({
                    "date":   item["day"],
                    "open":   float(item.get("open",  0)),
                    "high":   float(item.get("high",  0)),
                    "low":    float(item.get("low",   0)),
                    "close":  float(item.get("close", 0)),
                    "volume": int(float(item.get("volume", 0))),
                })
            if records:
                return {"success": True, "symbol": code, "name": code,
                        "data": records, "provider": "sina",
                        "provider_chain": ["eastmoney", "sina"], "count": len(records)}
            raise ValueError("empty Sina kline response")
        except Exception as sina_err:
            errors.append(f"sina: {sina_err}")
            logger.debug("Sina history failed %s: %s", code, sina_err)

        # ── 备用 2: AKShare 历史数据（如果本地安装）──────────────────────────
        if interval == "1d":
            try:
                import akshare as ak
                import os as _ak_os
                start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
                end_day = datetime.now().strftime("%Y%m%d")
                _ak_proxy_bk = {k: _ak_os.environ.pop(k, None)
                                for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
                try:
                    df = ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=start_date,
                        end_date=end_day,
                        adjust="qfq",
                    )
                finally:
                    for _k, _v in _ak_proxy_bk.items():
                        if _v is not None:
                            _ak_os.environ[_k] = _v
                if df.empty:
                    raise ValueError("empty AKShare history")
                records = []
                for _, row in df.tail(days + 5).iterrows():
                    records.append({
                        "date":   str(row.get("日期", ""))[:10],
                        "open":   float(row.get("开盘", 0) or 0),
                        "high":   float(row.get("最高", 0) or 0),
                        "low":    float(row.get("最低", 0) or 0),
                        "close":  float(row.get("收盘", 0) or 0),
                        "volume": int(float(row.get("成交量", 0) or 0)),
                    })
                if records:
                    return {"success": True, "symbol": code, "name": code,
                            "data": records, "provider": "akshare",
                            "provider_chain": ["eastmoney", "sina", "akshare"],
                            "count": len(records)}
                raise ValueError("empty AKShare records")
            except Exception as ak_err:
                errors.append(f"akshare: {ak_err}")
                logger.debug("AKShare history failed %s: %s", code, ak_err)
        else:
            errors.append(f"akshare: unsupported interval {interval}")

        # ── 备用 3: yfinance Yahoo Finance（绕过代理，全球可访问）────────────────
        try:
            import yfinance as yf
            import os as _os
            yf_sym = _yf_ashare_symbol(code, symbol)
            period_map = {30: "1mo", 60: "3mo", 90: "3mo", 120: "6mo",
                          180: "6mo", 252: "1y", 365: "1y", 730: "2y"}
            period = period_map.get(days) or f"{days}d"
            iv_map = {"1d": "1d", "1h": "1h", "15m": "15m"}
            iv = iv_map.get(interval, "1d")

            _proxy_backup = {k: _os.environ.pop(k, None)
                             for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
            try:
                df = yf.Ticker(yf_sym).history(period=period, interval=iv, auto_adjust=True)
                if df.empty:
                    df = yf.download(yf_sym, period=period, interval=iv,
                                     auto_adjust=True, progress=False, timeout=15)
                    if hasattr(df.columns, "levels") and len(df.columns.levels) > 1:
                        df.columns = df.columns.droplevel(1)
            finally:
                for k, v in _proxy_backup.items():
                    if v is not None:
                        _os.environ[k] = v

            if df.empty:
                raise ValueError("empty yfinance dataframe")
            records = []
            for ts, row in df.iterrows():
                records.append({
                    "date":   str(ts.date()) if hasattr(ts, "date") else str(ts)[:10],
                    "open":   round(float(row.get("Open",  row.get("open",  0))), 4),
                    "high":   round(float(row.get("High",  row.get("high",  0))), 4),
                    "low":    round(float(row.get("Low",   row.get("low",   0))), 4),
                    "close":  round(float(row.get("Close", row.get("close", 0))), 4),
                    "volume": int(row.get("Volume", row.get("volume", 0))),
                })
            return {"success": True, "symbol": code, "name": code,
                    "data": records, "provider": "yfinance",
                    "provider_chain": ["eastmoney", "sina", "akshare", "yfinance"],
                    "count": len(records)}
        except Exception as yf_err:
            errors.append(f"yfinance: {yf_err}")
            logger.debug("yfinance history fallback failed %s: %s", code, yf_err)
            return {
                "success": False,
                "symbol": code,
                "market": "CN",
                "provider_chain": ["eastmoney", "sina", "akshare", "yfinance"],
                "error": _friendly_market_error(
                    code, ["Eastmoney", "新浪", "AKShare", "Yahoo Finance"],
                    "; ".join(errors),
                ),
                "debug_error": "; ".join(errors),
            }

    def _fundamentals_ashare(self, symbol: str) -> Dict[str, Any]:
        """A股基本面：东方财富个股资金流."""
        code = _normalise_ashare(symbol)
        # 通过 yfinance 尝试 (港股 / ADR)
        try:
            import yfinance as yf
            yf_sym = _yf_ashare_symbol(code, symbol)
            return self._quote_yfinance(yf_sym)
        except Exception as e:
            return {"success": False, "error": str(e), "symbol": symbol}

    # ── Crypto (ccxt) ────────────────────────────────────────────────────────

    def _quote_crypto(self, symbol: str) -> Dict[str, Any]:
        try:
            import ccxt
            sym = _norm_crypto(symbol)
            ex = ccxt.binance({"enableRateLimit": True,
                               "proxies": {"http": "", "https": ""}})
            ticker = ex.fetch_ticker(sym)
            price  = float(ticker["last"] or 0)
            prev   = float(ticker.get("previousClose") or ticker.get("open") or price)
            chg    = price - prev
            chg_p  = chg / prev * 100 if prev else 0
            return {
                "success":    True,
                "symbol":     sym,
                "price":      price,
                "change":     round(chg, 6),
                "change_pct": round(chg_p, 2),
                "volume":     float(ticker.get("baseVolume", 0)),
                "high":       float(ticker.get("high", 0) or 0),
                "low":        float(ticker.get("low", 0) or 0),
                "market_cap": None,
                "currency":   "USDT",
                "market":     "CRYPTO",
                "provider":   "ccxt/binance",
                "timestamp":  datetime.now().isoformat(),
            }
        except Exception as e:
            # fallback to yfinance
            yf_sym = symbol.replace("/","").replace("USDT","-USD")
            return self._quote_yfinance(yf_sym)

    def _history_crypto(self, symbol: str, days: int, interval: str) -> Dict[str, Any]:
        try:
            import ccxt
            sym = _norm_crypto(symbol)
            iv_map = {"1d":"1d","1h":"1h","15m":"15m","4h":"4h"}
            tf = iv_map.get(interval, "1d")
            limit = min(days, 1000)
            ex = ccxt.binance({"enableRateLimit": True,
                               "proxies": {"http": "", "https": ""}})
            ohlcv = ex.fetch_ohlcv(sym, timeframe=tf, limit=limit)
            records = [{"date":   datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                        "open":   c[1], "high": c[2], "low": c[3],
                        "close":  c[4], "volume": c[5]}
                       for c in ohlcv]
            return {"success": True, "symbol": sym, "data": records,
                    "provider": "ccxt/binance", "count": len(records)}
        except Exception as e:
            yf_sym = symbol.replace("/","").replace("USDT","-USD")
            return self._history_yfinance(yf_sym, days, interval)

    # ── Binance: funding rate + read-only account ──────────────────────────────

    def crypto_funding_rate(self, symbol: str, exchange: str = "binance") -> Dict[str, Any]:
        """Perpetual funding rate (read-only, no key). Falls back across
        exchanges so a geo-blocked Binance doesn't kill the lookup."""
        import ccxt
        sym = _norm_crypto(symbol) + ":USDT"   # ccxt linear-perp notation
        _last_err = ""
        for exch in [exchange] + [e for e in ("okx", "bybit", "gate") if e != exchange]:
            try:
                ex = getattr(ccxt, exch)({"enableRateLimit": True,
                                          "options": {"defaultType": "swap"},
                                          "proxies": {"http": "", "https": ""}})
                fr = ex.fetch_funding_rate(sym)
                return {
                    "success": True, "symbol": sym, "exchange": exch,
                    "funding_rate": fr.get("fundingRate"),
                    "funding_rate_pct": round((fr.get("fundingRate") or 0) * 100, 4),
                    "next_funding": fr.get("fundingDatetime"),
                    "mark_price": fr.get("markPrice"),
                    "provider": f"ccxt/{exch}",
                }
            except Exception as e:
                _last_err = str(e)
                continue
        return {"success": False, "error": _last_err, "symbol": symbol}

    def crypto_account(self, exchange: str = "binance") -> Dict[str, Any]:
        """READ-ONLY account balance via API key (no trading).

        Keys from env: BINANCE_API_KEY / BINANCE_SECRET (or <EXCHANGE>_API_KEY).
        This only reads balances — Aria never places crypto orders.
        """
        import os as _os
        key = _os.getenv(f"{exchange.upper()}_API_KEY") or _os.getenv("BINANCE_API_KEY")
        secret = _os.getenv(f"{exchange.upper()}_SECRET") or _os.getenv("BINANCE_SECRET")
        if not key or not secret:
            return {"success": False, "error": "no_api_key",
                    "hint": f"set {exchange.upper()}_API_KEY and {exchange.upper()}_SECRET (read-only key)"}
        try:
            import ccxt
            ex = getattr(ccxt, exchange)({
                "apiKey": key, "secret": secret,
                "enableRateLimit": True, "proxies": {"http": "", "https": ""},
            })
            bal = ex.fetch_balance()
            holdings = []
            for asset, amt in (bal.get("total") or {}).items():
                if amt and float(amt) > 0:
                    holdings.append({"asset": asset, "amount": float(amt),
                                     "free": float((bal.get("free") or {}).get(asset, 0)),
                                     "used": float((bal.get("used") or {}).get(asset, 0))})
            holdings.sort(key=lambda h: -h["amount"])
            return {"success": True, "exchange": exchange,
                    "holdings": holdings, "asset_count": len(holdings),
                    "provider": f"ccxt/{exchange}", "read_only": True}
        except Exception as e:
            return {"success": False, "error": str(e), "exchange": exchange}

    # ── Global indices ────────────────────────────────────────────────────────

    def _fetch_indices(self) -> Dict[str, Any]:
        indices = {}
        # A股指数 (东方财富)
        cn_secids = "1.000001,0.399001,0.399006,1.000016,1.000688"
        cn_names  = {"000001":"上证指数","399001":"深证成指",
                     "399006":"创业板指","000016":"上证50","000688":"科创50"}
        try:
            _resp = self._em_get_json(self.EM_ULIST_URL, {
                "fltt": 2, "invt": 2,
                "fields": "f1,f2,f3,f4,f12,f14",
                "secids": cn_secids,
                "ut": _EM_UT,
            }, timeout=8)
            _diff = (_resp or {}).get("data", {}).get("diff", []) or []
            if isinstance(_diff, dict):
                _diff = list(_diff.values())
            for item in _diff:
                code = item.get("f12","")
                indices[cn_names.get(code, code)] = {
                    "price":      round(float(item.get("f2",0)), 2),
                    "change_pct": round(float(item.get("f3",0)), 2),
                    "change":     round(float(item.get("f4",0)), 2),
                    "market":     "CN",
                }
        except Exception as e:
            logger.debug("CN indices error: %s", e)

        # Global indices (yfinance)
        global_map = {
            "^GSPC":  "S&P 500",
            "^IXIC":  "纳斯达克",
            "^DJI":   "道琼斯",
            "^HSI":   "恒生指数",
            "^N225":  "日经225",
            "^FTSE":  "富时100",
            "GC=F":   "黄金",
            "CL=F":   "原油WTI",
            "BTC-USD":"比特币",
        }
        try:
            import yfinance as yf
            tickers = yf.Tickers(" ".join(global_map.keys()))
            for sym, name in global_map.items():
                try:
                    fi    = tickers.tickers[sym].fast_info
                    price = float(fi.last_price or 0)
                    prev  = float(fi.previous_close or price)
                    chg_p = (price - prev) / prev * 100 if prev else 0
                    indices[name] = {
                        "price":      round(price, 2),
                        "change_pct": round(chg_p, 2),
                        "change":     round(price - prev, 4),
                        "market":     "US" if sym.startswith("^") else "COMMOD",
                    }
                except Exception as _e:
                    logger.debug("Global index fetch failed for %s: %s", sym, _e)
        except Exception as e:
            logger.debug("Global indices yfinance error: %s", e)

        return {"success": True, "indices": indices,
                "timestamp": datetime.now().isoformat()}

    # ── 北向资金 ────────────────────────────────────────────────────────────

    def _fetch_northbound(self) -> Dict[str, Any]:
        try:
            _resp = self._em_get_json(self.EM_NORTHBOUND, {
                "fields1": "f1,f2,f3,f4",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": 101, "lmt": 5,
                "ut": _EM_UT,
            }, timeout=8)
            data = (_resp or {}).get("data", {}) or {}
            sh   = data.get("s2n", {}) or {}   # 沪股通
            sz   = data.get("s3n", {}) or {}   # 深股通
            def _val(obj, key):
                try: return float(obj.get(key, 0)) / 1e8   # 元 → 亿
                except (KeyError, ValueError, TypeError): return 0.0
            sh_net = _val(sh, "f2")
            sz_net = _val(sz, "f2")
            total  = sh_net + sz_net
            return {
                "success":     True,
                "total_net":   round(total, 2),
                "sh_net":      round(sh_net, 2),
                "sz_net":      round(sz_net, 2),
                "unit":        "亿元",
                "direction":   "净流入" if total > 0 else "净流出",
                "provider":    "eastmoney",
                "timestamp":   datetime.now().isoformat(),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 热门股榜单 ────────────────────────────────────────────────────────────

    def _fetch_hot_ashare(self, top_n: int = 20) -> Dict[str, Any]:
        try:
            data = self._em_get_json(self.EM_HOT_URL, {
                "pn": 1, "pz": top_n, "po": 1, "np": 1,
                "ut": _EM_UT,
                "fltt": 2, "invt": 2, "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f62",
            })
            if not data:
                return {"success": False, "error": "eastmoney 无响应（网络或代理）"}
            items = (data.get("data") or {}).get("diff", []) or []
            if isinstance(items, dict):
                items = list(items.values())
            stocks = []
            for d in items[:top_n]:
                stocks.append({
                    "code":       d.get("f12",""),
                    "name":       d.get("f14",""),
                    "price":      round(float(d.get("f2",0)), 2),    # fltt=2 → already in ¥
                    "change_pct": round(float(d.get("f3",0)), 2),    # already in %
                    "volume":     int(d.get("f5",0)),
                    "turnover":   float(d.get("f6",0)),
                    "amplitude":  round(float(d.get("f7",0)), 2),    # already in %
                })
            return {"success": True, "market": "CN", "stocks": stocks,
                    "count": len(stocks), "provider": "eastmoney"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def screen_ashare(self, *, max_pe: float = 50, min_market_cap_yi: float = 0,
                      limit: int = 20, exclude_st: bool = True) -> Dict[str, Any]:
        """Screen A-shares via the eastmoney clist endpoint (host-rotating,
        proxy-resilient). Sorted by change% desc, then filtered by PE/market cap.

        Fetches a few pages (not all ~5000 stocks) so the request stays small
        and reliable. Fields: f2 price, f3 chg%, f8 turnover, f9 PE(dynamic),
        f12 code, f14 name, f20 total mktcap, f23 PB.
        """
        rows: List[Dict[str, Any]] = []
        for pn in range(1, 4):  # up to 3 pages × 100 = 300 movers
            data = self._em_get_json(self.EM_HOT_URL, {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "ut": _EM_UT,
                "fltt": 2, "invt": 2, "fid": "f3",  # sort by change% desc
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f2,f3,f8,f9,f12,f14,f20,f23",
            })
            if not data:
                break
            diff = (data.get("data") or {}).get("diff", []) or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            rows.extend(diff)
            if len(diff) < 100:
                break

        if not rows:
            return {"success": False, "error": "eastmoney 无响应（网络或代理）"}

        def _num(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        out: List[Dict[str, Any]] = []
        for d in rows:
            name = str(d.get("f14", ""))
            if exclude_st and ("ST" in name or "退" in name):
                continue
            price = _num(d.get("f2"))
            pe = _num(d.get("f9"))
            mktcap = _num(d.get("f20"))  # 元
            if price is None or price <= 0:
                continue
            if pe is not None and not (0.1 <= pe <= max_pe):
                continue
            if min_market_cap_yi > 0 and (mktcap is None or mktcap < min_market_cap_yi * 1e8):
                continue
            out.append({
                "code": str(d.get("f12", "")),
                "name": name,
                "price": round(price, 2),
                "change_pct": round(_num(d.get("f3")) or 0, 2),
                "pe_dynamic": round(pe, 1) if pe is not None else None,
                "pb": round(_num(d.get("f23")) or 0, 2),
                "turnover_rate": round(_num(d.get("f8")) or 0, 2),
                "market_cap_yi": round(mktcap / 1e8, 1) if mktcap else None,
            })
            if len(out) >= limit:
                break

        return {"success": True, "count": len(out), "stocks": out,
                "provider": "eastmoney"}

    def _fetch_hot_us(self, top_n: int = 10) -> Dict[str, Any]:
        """US most active stocks via yfinance screener."""
        watchlist = ["NVDA","AAPL","TSLA","MSFT","AMZN","META","GOOGL","AMD","INTC","PLTR"]
        results = []
        try:
            import yfinance as yf
            for sym in watchlist[:top_n]:
                try:
                    fi = yf.Ticker(sym).fast_info
                    p  = float(fi.last_price or 0)
                    prev = float(fi.previous_close or p)
                    chg_p = (p-prev)/prev*100 if prev else 0
                    results.append({"symbol": sym, "price": round(p,2),
                                    "change_pct": round(chg_p,2)})
                except Exception as _e:
                    logger.debug("Screener quote failed for %s: %s", sym, _e)
            return {"success": True, "market": "US", "stocks": results,
                    "count": len(results), "provider": "yfinance"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ── Module-level singleton ───────────────────────────────────────────────────

_mdc: Optional[MarketDataClient] = None

def get_mdc() -> MarketDataClient:
    global _mdc
    if _mdc is None:
        _mdc = MarketDataClient()
    return _mdc


# ── Convenience functions (module-level API) ─────────────────────────────────

def quote(symbol: str) -> Dict[str, Any]:
    return get_mdc().quote(symbol)

def history(symbol: str, days: int = 252, interval: str = "1d") -> Dict[str, Any]:
    return get_mdc().history(symbol, days=days, interval=interval)

def indices() -> Dict[str, Any]:
    return get_mdc().indices()

def northbound_flow() -> Dict[str, Any]:
    return get_mdc().northbound_flow()

def technical_indicators(symbol: str, days: int = 120) -> Dict[str, Any]:
    return get_mdc().technical_indicators(symbol, days=days)

def fundamentals(symbol: str) -> Dict[str, Any]:
    return get_mdc().fundamentals(symbol)

def hot_stocks(market: str = "cn", top_n: int = 20) -> Dict[str, Any]:
    return get_mdc().hot_stocks(market=market, top_n=top_n)

def screen_ashare(**kwargs) -> Dict[str, Any]:
    return get_mdc().screen_ashare(**kwargs)


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(json.dumps(quote(sym), indent=2, ensure_ascii=False, default=str))
