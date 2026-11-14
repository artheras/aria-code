"""
data_service.py — unified market data facade
============================================

This module sits above MarketDataClient and datasources.DataRouter. It returns
one normalized shape for quotes, history, fundamentals, and technical signals
so report/backtest code does not need to know provider-specific schemas.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from aria_code.packages.aria_services.cache import DistributedCacheManager
except ImportError:
    try:
        from packages.aria_services.cache import DistributedCacheManager
    except ImportError:
        DistributedCacheManager = None

from aria_code.packages.aria_services.provider_health import (
    GLOBAL_PROVIDER_HEALTH,
    ProviderHealthRegistry,
    ProviderIssue,
    classify_provider_error,
)


def _dedupe(values: List[Any]) -> List[str]:
    return list(dict.fromkeys(str(v) for v in values if v not in (None, "", [], {})))


def _is_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _to_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _provider_chain(data: Dict[str, Any], fallback: str) -> List[str]:
    chain = data.get("provider_chain")
    if isinstance(chain, list):
        return _dedupe(chain)
    return _dedupe([data.get("provider"), data.get("source"), fallback])


def _provider_from_call(method: str, data: Dict[str, Any] | None = None) -> str:
    data = data or {}
    provider = data.get("provider") or data.get("source")
    if provider:
        return str(provider)
    return "market_data_client" if method in {"quote", "history", "fundamentals", "technical_indicators"} else method


def _data_with_success(data: Dict[str, Any], success: bool) -> Dict[str, Any]:
    """Return payload with its embedded success flag matching DataServiceResult."""
    if not data:
        return {}
    out = dict(data)
    out["success"] = bool(success)
    return out


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp() -> str:
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class DataServiceResult:
    kind: str
    symbol: str
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    provider_chain: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    source: str = ""
    stale: bool = False
    quality: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_timestamp)


@dataclass
class DataBundle:
    symbol: str
    quote: Dict[str, Any] = field(default_factory=dict)
    history: Dict[str, Any] = field(default_factory=dict)
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    technical: Dict[str, Any] = field(default_factory=dict)
    provider_chain: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    quality: Dict[str, Any] = field(default_factory=dict)
    status: str = "data_unavailable"
    timestamp: str = field(default_factory=_utc_timestamp)


class DataService:
    """Unified data entrypoint with cache, fallback, provenance and validation."""

    def __init__(
        self,
        market_client: Any = None,
        router: Any = None,
        ttl_seconds: int = 60,
        max_quote_age_seconds: int = 900,
        provider_health: ProviderHealthRegistry | None = None,
    ):
        if market_client is None:
            from market_data_client import MarketDataClient
            market_client = MarketDataClient()
        self._router_disabled = router is False
        if self._router_disabled:
            router = None
        if router is None:
            if not self._router_disabled:
                try:
                    from datasources.router import get_router
                    router = get_router()
                except Exception:
                    router = None
        self.market_client = market_client
        self.router = router
        self.ttl_seconds = ttl_seconds
        self.max_quote_age_seconds = max_quote_age_seconds
        self.provider_health = provider_health or GLOBAL_PROVIDER_HEALTH
        import os
        redis_url = os.environ.get("REDIS_URL", None)
        if DistributedCacheManager:
            self._cache_manager = DistributedCacheManager(redis_url=redis_url)
        else:
            self._cache_manager = None
            self._cache: Dict[Tuple[Any, ...], Tuple[float, DataServiceResult]] = {}
    def quote(self, symbol: str) -> DataServiceResult:
        return self._cached(("quote", symbol), lambda: self._quote_uncached(symbol))

    def history(self, symbol: str, days: int = 370, interval: str = "1d") -> DataServiceResult:
        return self._cached(("history", symbol, days, interval), lambda: self._history_uncached(symbol, days, interval))

    def fundamentals(self, symbol: str) -> DataServiceResult:
        return self._cached(("fundamentals", symbol), lambda: self._fundamentals_uncached(symbol))

    def technical_indicators(self, symbol: str, days: int = 120) -> DataServiceResult:
        return self._cached(("technical", symbol, days), lambda: self._technical_uncached(symbol, days))

    def bundle(self, symbol: str, history_days: int = 370, technical_days: int = 120) -> DataBundle:
        quote = self.quote(symbol)
        history = self.history(symbol, days=history_days)
        fundamentals = self.fundamentals(symbol)
        technical = self.technical_indicators(symbol, days=technical_days)

        provider_chain = _dedupe(
            quote.provider_chain + history.provider_chain + fundamentals.provider_chain + technical.provider_chain
        )
        warnings = quote.warnings + history.warnings + fundamentals.warnings + technical.warnings
        errors = quote.errors + history.errors + fundamentals.errors + technical.errors
        missing_fields = self._bundle_missing_fields(quote, history, fundamentals, technical)
        stale = quote.stale or history.stale or technical.stale

        core_success = quote.success and history.success and technical.success
        any_success = quote.success or history.success or fundamentals.success or technical.success
        status = "complete" if core_success and not missing_fields else "partial" if any_success else "data_unavailable"
        if stale and status == "complete":
            status = "stale"
        return DataBundle(
            symbol=symbol,
            quote=quote.data,
            history=history.data,
            fundamentals=fundamentals.data,
            technical=technical.data,
            provider_chain=provider_chain,
            warnings=warnings[:10],
            errors=errors[:10],
            missing_fields=missing_fields,
            quality={
                "status": status,
                "stale": stale,
                "providers": provider_chain,
                "provider_health": self.provider_health.snapshot(),
                "missing_fields": missing_fields,
                "warnings": warnings[:10],
                "errors": errors[:10],
            },
            status=status,
        )

    def _cached(self, key: Tuple[Any, ...], factory: Any) -> DataServiceResult:
        # Convert tuple key to string for Redis
        str_key = "|".join(str(k) for k in key)

        if self._cache_manager:
            import json
            cached = self._cache_manager.get(str_key)
            if cached:
                # If cached is dict (from redis json), we might need to cast it back to DataServiceResult
                # For simplicity, if we rely on pickle or raw dict, we assume the factory returns raw or dataclass
                # Since DataServiceResult is a dataclass, we could recreate it or just return it.
                if isinstance(cached, dict):
                    return DataServiceResult(**cached)
                return cached

            result = factory()
            # To cache it in redis, convert dataclass to dict if necessary
            val_to_cache = result.__dict__ if hasattr(result, '__dict__') else result
            self._cache_manager.set(str_key, val_to_cache, self.ttl_seconds)
            return result
        else:
            now = time.time()
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1]
            result = factory()
            self._cache[key] = (now, result)
            return result

    def _quote_uncached(self, symbol: str) -> DataServiceResult:
        warnings: List[str] = []
        data = self._call_market("quote", symbol, warnings)
        if not self._valid_quote(data):
            primary = data
            fallback = self._call_router("quote", symbol, warnings)
            data = fallback or primary
        return self._result("quote", symbol, data, warnings, required=["price"], validator=self._valid_quote)

    def _history_uncached(self, symbol: str, days: int, interval: str) -> DataServiceResult:
        warnings: List[str] = []
        data = self._call_market("history", symbol, warnings, days=days, interval=interval)
        if not self._valid_history(data):
            data = self._call_router("history", symbol, warnings, days=days, interval=interval)
        return self._result("history", symbol, data, warnings, required=["data"], validator=self._valid_history)

    def _fundamentals_uncached(self, symbol: str) -> DataServiceResult:
        warnings: List[str] = []
        data = self._call_market("fundamentals", symbol, warnings)
        if not self._valid_payload(data):
            data = self._call_router("fundamentals", symbol, warnings)
        return self._result("fundamentals", symbol, data, warnings, required=[])

    def _technical_uncached(self, symbol: str, days: int) -> DataServiceResult:
        warnings: List[str] = []
        data = self._call_market("technical_indicators", symbol, warnings, days=days)

        # Yahoo Finance v8 fallback — for US symbols when primary TA source is rate-limited
        # or returns insufficient data (newly-listed stocks, ETFs without MDC coverage)
        _sym = symbol.upper().strip()
        _is_us = not (_sym.endswith((".SZ", ".SS", ".SH", ".HK")) or _sym.isdigit())
        _needs_fallback = _is_us and (
            not data or
            data.get("success") is False or
            (data.get("success") and data.get("rsi") is None)
        )
        if _needs_fallback:
            try:
                import json as _jv8
                import urllib.request as _uv8
                import statistics as _sv8
                _url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{_sym}"
                    "?interval=1d&range=6mo"
                )
                _req = _uv8.Request(_url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "application/json",
                })
                with _uv8.urlopen(_req, timeout=10) as _r:
                    _raw = _jv8.loads(_r.read())
                _res  = _raw["chart"]["result"][0]
                _q    = _res["indicators"]["quote"][0]
                _c    = [x for x in _q.get("close", []) if x is not None]
                _v    = [x for x in _q.get("volume", []) if x is not None]
                if len(_c) >= 14:
                    def _ema(p, n):
                        k, r = 2/(n+1), [p[0]]
                        for x in p[1:]: r.append(x*k + r[-1]*(1-k))
                        return r
                    _d  = [_c[i]-_c[i-1] for i in range(1, len(_c))]
                    _g  = [max(x,0) for x in _d]; _l = [max(-x,0) for x in _d]
                    _ag = sum(_g[:14])/14;         _al = sum(_l[:14])/14
                    for i in range(14, len(_g)):
                        _ag = (_ag*13+_g[i])/14;  _al = (_al*13+_l[i])/14
                    _rsi = (100 - 100/(1+_ag/_al)) if _al else 100.0
                    n = len(_c)
                    _ma20 = sum(_c[-20:])/min(20, n)
                    _ma60 = sum(_c[-60:])/min(60, n) if n >= 14 else _ma20
                    _std  = _sv8.stdev(_c[-min(20,n):]) if n >= 2 else 0
                    _v8_data: Dict[str, Any] = {
                        "success": True,
                        "price":   round(_c[-1], 2),
                        "rsi":     round(_rsi, 2),
                        "ma20":    round(_ma20, 2),
                        "ma60":    round(_ma60, 2),
                        "bb_upper": round(_ma20 + 2*_std, 2),
                        "bb_mid":   round(_ma20, 2),
                        "bb_lower": round(_ma20 - 2*_std, 2),
                        "provider": "yahoo_v8",
                        "history_bars": n,
                    }
                    if n >= 26:
                        _e12 = _ema(_c, 12); _e26 = _ema(_c, 26)
                        _md  = [a-b for a,b in zip(_e12, _e26)]
                        _sg  = _ema(_md, 9)
                        _v8_data["macd"]        = round(_md[-1], 4)
                        _v8_data["macd_signal"] = round(_sg[-1], 4)
                        _v8_data["macd_hist"]   = round(_md[-1]-_sg[-1], 4)
                    if _v:
                        _v8_data["volume"] = int(_v[-1])
                    data = _v8_data
                    warnings.append(f"yahoo_v8 fallback ({n} bars)")
                elif len(_c) > 0:
                    # Too few bars for TA — still surface current price and bar count
                    data = {
                        "success": False,
                        "price":   round(_c[-1], 2),
                        "history_bars": len(_c),
                        "provider": "yahoo_v8",
                    }
                    if _v:
                        data["volume"] = int(_v[-1])
                    _bar_str = f"{len(_c)} 个交易日"
                    warnings.append(f"数据不足（仅 {_bar_str}）— 新上市标的，TA 指标需至少 14 日历史")
            except Exception:
                pass

        return self._result("technical", symbol, data, warnings, required=["rsi", "macd", "ma20"])

    def _call_market(self, method: str, symbol: str, warnings: List[str], **kwargs: Any) -> Dict[str, Any]:
        try:
            fn = getattr(self.market_client, method)
            data = _to_dict(fn(symbol, **kwargs))
            provider = _provider_from_call(method, data)
            if data.get("success") is False:
                issue = classify_provider_error(provider, data.get("error") or "unavailable")
                self.provider_health.mark_issue(issue)
                warnings.append(self._format_issue(method, issue))
            elif data:
                self.provider_health.mark_success(provider)
            return data
        except Exception as exc:
            issue = classify_provider_error("market_data_client", exc)
            self.provider_health.mark_issue(issue)
            warnings.append(self._format_issue(method, issue))
            return {}

    def _call_router(self, method: str, symbol: str, warnings: List[str], **kwargs: Any) -> Dict[str, Any]:
        if self._router_disabled:
            return {}
        if self.router is None:
            issue = classify_provider_error("data_router", "router unavailable")
            self.provider_health.mark_issue(issue)
            warnings.append(self._format_issue(method, issue))
            return {}
        try:
            fn = getattr(self.router, method)
            data = _to_dict(fn(symbol, **kwargs))
            if data:
                data.setdefault("success", True)
                data.setdefault("provider", data.get("source") or f"data_router.{method}")
                data.setdefault("provider_chain", _dedupe([data.get("provider"), data.get("source")]))
                self.provider_health.mark_success(str(data.get("provider")))
            return data
        except Exception as exc:
            issue = classify_provider_error("data_router", exc)
            self.provider_health.mark_issue(issue)
            warnings.append(self._format_issue(method, issue))
            return {}

    @staticmethod
    def _format_issue(method: str, issue: ProviderIssue) -> str:
        return f"{issue.provider}.{method}: {issue.category} — {issue.message}"

    def _result(
        self,
        kind: str,
        symbol: str,
        data: Dict[str, Any],
        warnings: List[str],
        required: List[str],
        validator: Any = None,
    ) -> DataServiceResult:
        validator = validator or self._valid_payload
        success = validator(data)
        missing = [key for key in required if not _is_present(data.get(key))]
        if kind == "quote" and "price" in required and not self._valid_quote(data):
            if "price" not in missing:
                missing.append("price")
        if data.get("success") is False:
            success = False
        payload = _data_with_success(data, success)
        source = str(data.get("provider") or data.get("source") or "")
        payload_ts = data.get("timestamp") or data.get("asof") or data.get("as_of")
        timestamp = str(payload_ts or _utc_timestamp())
        stale = self._is_stale(kind, payload_ts)
        errors = []
        if data.get("error"):
            errors.append(str(data.get("error")))
        status = "ok" if success and not missing and not stale else (
            "stale" if success and stale else "partial" if success else "unavailable"
        )
        return DataServiceResult(
            kind=kind,
            symbol=symbol,
            success=success,
            data=payload if payload else {},
            provider_chain=_provider_chain(data, kind) if data else [],
            warnings=warnings[:5],
            errors=errors[:5],
            missing_fields=missing,
            source=source,
            stale=stale,
            quality={
                "status": status,
                "stale": stale,
                "source": source,
                "providers": _provider_chain(data, kind) if data else [],
                "provider_health": self.provider_health.snapshot(),
                "missing_fields": missing,
                "warnings": warnings[:5],
                "errors": errors[:5],
            },
            timestamp=timestamp,
        )

    def _is_stale(self, kind: str, timestamp: Any) -> bool:
        if kind != "quote":
            return False
        dt = _parse_timestamp(timestamp)
        if dt is None:
            return False
        return (_utc_now() - dt).total_seconds() > self.max_quote_age_seconds

    @staticmethod
    def _valid_payload(data: Dict[str, Any]) -> bool:
        return bool(data) and data.get("success") is not False

    @staticmethod
    def _valid_quote(data: Dict[str, Any]) -> bool:
        if not data or data.get("success") is False:
            return False
        price = data.get("price")
        try:
            return price is not None and float(price) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _valid_history(data: Dict[str, Any]) -> bool:
        if not data or data.get("success") is False:
            return False
        rows = data.get("data")
        try:
            return rows is not None and len(rows) > 0
        except TypeError:
            return False

    @staticmethod
    def _bundle_missing_fields(
        quote: DataServiceResult,
        history: DataServiceResult,
        fundamentals: DataServiceResult,
        technical: DataServiceResult,
    ) -> List[str]:
        missing: List[str] = []
        if not quote.success or not _is_present(quote.data.get("price")):
            missing.append("price")
        if not history.success:
            missing.append("history")
        for key in ("pe_ratio", "pe_ttm", "pb_ratio", "pb", "roe"):
            if _is_present(fundamentals.data.get(key)):
                break
        else:
            missing.append("fundamentals")
        for key in ("rsi", "macd", "ma20"):
            if not _is_present(technical.data.get(key)):
                missing.append(key)
        return _dedupe(missing)
