"""Market data and broker tools extracted from aria_cli.py.

Lazy-imports ``market_data_client`` and ``brokers`` so the module loads
even when those optional packages are absent.  The import guards mirror
aria_cli.py lines 134-189 so the same failure modes apply.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent  # aria-code/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aria_code.apps.cli.market_metadata import enrich_market_quote
from aria_code.packages.aria_core.paths import aria_home

logger = logging.getLogger(__name__)


def _quote_time_metadata(quote: dict) -> dict[str, str]:
    """Return provenance without calling an undated quote 'latest'."""
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for key in ("as_of", "timestamp", "quote_time", "last_trade_time", "date"):
        value = quote.get(key)
        if value not in (None, ""):
            return {"as_of": str(value), "retrieved_at": retrieved_at,
                    "time_quality": "provider_timestamp"}
    return {"retrieved_at": retrieved_at, "time_quality": "retrieval_time_only"}

# ── Optional: market data client ────────────────────────────────────────────
# Loading this client imports pandas, NumPy, and requests. The coding REPL does
# not need any of them until a market tool is actually invoked.
from importlib.util import find_spec

_HAS_MDC = find_spec("market_data_client") is not None


def _get_mdc():
    if not _HAS_MDC:
        return None
    from market_data_client import get_mdc
    return get_mdc()

# ── Unified data service (MDC primary + DataRouter fallback) ──────────────────
# DataService bridges MarketDataClient with the configured DataRouter, which
# honours ~/.aria/datasources.yaml (tushare / akshare / finnhub / fred / …).
# Lazily constructed so importing this module stays cheap.
_DATA_SERVICE = None


def _get_data_service():
    global _DATA_SERVICE
    if _DATA_SERVICE is None:
        from data_service import DataService
        _DATA_SERVICE = DataService()
    return _DATA_SERVICE

# ── Optional: broker integration ────────────────────────────────────────────
try:
    from brokers import (                                  # noqa: E402
        get_registry as _get_broker_registry,
        list_broker_configs as _list_broker_configs,
        BROKERS_CONFIG_PATH as _BROKERS_CONFIG_PATH,
    )
    _HAS_BROKERS = True
except ImportError:
    _HAS_BROKERS = False
    def _get_broker_registry(): return None   # type: ignore[return-value]
    def _list_broker_configs(): return []
    _BROKERS_CONFIG_PATH = None


def _get_finnhub_key() -> str:
    """Read Finnhub API key from env or ~/.arthera/providers.json."""
    val = os.getenv("FINNHUB_API_KEY", "")
    if val:
        return val
    providers_file = aria_home() / "providers.json"
    try:
        if providers_file.exists():
            raw = json.loads(providers_file.read_text(encoding="utf-8"))
            for section in ("llm", "data"):
                entry = raw.get(section, {}).get("finnhub", {})
                if entry.get("api_key"):
                    return entry["api_key"]
    except Exception:
        pass
    return ""


def _extract_symbol(params: dict) -> str:
    """Robustly extract and clean a stock/crypto symbol from tool params."""
    if not isinstance(params, dict):
        return ""
    val = params.get("symbol") or params.get("symbols") or params.get("ticker") or params.get("code") or params.get("stock")
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    elif isinstance(val, dict):
        val = val.get("symbol") or val.get("ticker") or val.get("code") or ""
    s = str(val or "").strip()
    if (s.startswith("[") and s.endswith("]")) or (s.startswith("(") and s.endswith(")")):
        s = s[1:-1].strip()
    return s.strip("'\"` ").upper()


def tool_get_market_data(params: dict) -> dict:
    """Fetch real-time quote + technical indicators for any stock/ETF/crypto.

    Supports A-shares (6-digit code), HK (.HK), US tickers, crypto.
    Primary source: MarketDataClient.  Fallback: Finnhub (US/global only).
    """
    symbol = _extract_symbol(params)
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    symbol_base = symbol.rsplit(".", 1)[0] if symbol.endswith((".SZ", ".SS", ".SH")) else symbol
    is_ashare_symbol = (
        symbol_base.isdigit() and len(symbol_base) == 6
    ) or (
        symbol_base.startswith(("SH", "SZ"))
        and symbol_base[2:].isdigit()
        and len(symbol_base[2:]) == 6
    )

    # ── 1. Quote ─────────────────────────────────────────────────────────────
    quote: dict = {"success": False, "error": "market data client unavailable"}
    if _HAS_MDC and _get_mdc is not None:
        import time as _t
        mdc = _get_mdc()
        for _att in range(3):
            try:
                quote = mdc.quote(symbol)
                if quote.get("success"):
                    break
                _e = str(quote.get("error", "")).lower()
                if ("rate" in _e or "429" in _e) and _att < 2:
                    _t.sleep(2 ** _att)
                    continue
                break
            except Exception as exc:
                _es = str(exc).lower()
                if ("rate" in _es or "429" in _es) and _att < 2:
                    _t.sleep(2 ** _att)
                    continue
                _raw = str(exc)
                if "Connection aborted" in _raw or "RemoteDisconnected" in _raw:
                    quote = {"success": False, "error": "网络连接中断，请稍后重试"}
                elif "Connection refused" in _raw:
                    quote = {"success": False, "error": "连接被拒绝，数据服务暂时不可用"}
                elif "timeout" in _raw.lower():
                    quote = {"success": False, "error": "连接超时，请稍后重试"}
                else:
                    quote = {"success": False, "error": _raw}
                break

    # Finnhub fallback for US/global symbols
    if not quote.get("success"):
        _fh_key = _get_finnhub_key()
        if _fh_key:
            try:
                import requests as _rq
                _r = _rq.get(
                    f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={_fh_key}",
                    timeout=6,
                )
                if _r.status_code == 200:
                    _fh = _r.json()
                    if _fh.get("c"):
                        quote = {
                            "success": True, "symbol": symbol,
                            "price": round(_fh["c"], 4),
                            "change_pct": round(float(_fh.get("dp") or 0), 4),
                            "high": round(_fh.get("h", 0), 4),
                            "low": round(_fh.get("l", 0), 4),
                            "currency": "USD", "provider": "finnhub",
                        }
            except Exception:
                pass

    # Unified DataService fallback — reaches the user's configured DataRouter
    # sources (tushare / akshare / finnhub / alpha_vantage / …) when both the
    # native MDC chain and Finnhub failed. Additive: only runs on failure.
    if not quote.get("success"):
        try:
            _res = _get_data_service().quote(symbol)
            if _res.success and _res.data:
                quote = dict(_res.data)
                quote.setdefault("success", True)
        except Exception:
            pass

    if not quote.get("success"):
        return {
            "success": False,
            "symbol": symbol,
            "market": "CN" if is_ashare_symbol else "GLOBAL",
            "provider_chain": quote.get("provider_chain") or (
                ["eastmoney", "akshare", "yfinance"] if is_ashare_symbol
                else ["yfinance", "finnhub"]
            ),
            "error": quote.get("error") or "行情数据源暂时不可用，请稍后重试。",
        }

    quote = enrich_market_quote(symbol, quote)
    result = {
        "success":    True,
        "symbol":     symbol,
        "name":       quote.get("name") or symbol,
        "price":      quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "high":       quote.get("high"),
        "low":        quote.get("low"),
        "volume":     quote.get("volume"),
        "market_cap": quote.get("market_cap"),
        "currency":   quote.get("currency") or "USD",
        "provider":   quote.get("provider") or "market_data_client",
        "provider_chain": quote.get("provider_chain") or [
            quote.get("provider") or "market_data_client"
        ],
    }
    result.update(_quote_time_metadata(quote))

    # ── 2. Technical indicators ───────────────────────────────────────────────
    ti: dict = {}
    if _HAS_MDC and _get_mdc is not None:
        try:
            ti = mdc.technical_indicators(symbol, days=120) or {}  # type: ignore[name-defined]
        except Exception:
            ti = {}

    if (not ti.get("success") or ti.get("rsi") is None) and not is_ashare_symbol:
        import yfinance as _yf
        import numpy as _np
        from datetime import date as _date, timedelta as _td
        _yf_sym = symbol
        if symbol.isdigit() and len(symbol) == 6:
            _yf_sym = symbol + (".SS" if symbol.startswith("6") else ".SZ")

        def _compute_ta(df) -> dict:
            _c = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
            _v = df["Volume"] if "Volume" in df.columns else None
            _d = _c.diff()
            _g = _d.clip(lower=0).rolling(14).mean()
            _l = (-_d.clip(upper=0)).rolling(14).mean()
            _rsi_val = float((100 - 100 / (1 + _g / _l.replace(0, _np.nan))).iloc[-1])
            _ema12 = _c.ewm(span=12).mean()
            _ema26 = _c.ewm(span=26).mean()
            _macd  = _ema12 - _ema26
            _mhist = float((_macd - _macd.ewm(span=9).mean()).iloc[-1])
            _ma20  = _c.rolling(20).mean()
            _std20 = _c.rolling(20).std()
            _ma60  = _c.rolling(60).mean() if len(_c) >= 60 else _ma20
            r = {
                "success":   True,
                "rsi":       round(_rsi_val, 2) if not _np.isnan(_rsi_val) else None,
                "macd_hist": round(_mhist, 4),
                "ma20":      round(float(_ma20.iloc[-1]), 2),
                "ma60":      round(float(_ma60.iloc[-1]), 2),
                "bb_upper":  round(float((_ma20 + 2 * _std20).iloc[-1]), 2),
                "bb_lower":  round(float((_ma20 - 2 * _std20).iloc[-1]), 2),
            }
            if _v is not None and result.get("volume") is None:
                _rv = _v.iloc[-1]
                if not _np.isnan(_rv):
                    result["volume"] = int(_rv)
            return r

        _df_ta = None
        try:
            _df_ta = _yf.Ticker(_yf_sym).history(period="6mo", auto_adjust=True)
            if _df_ta.empty:
                _df_ta = None
        except Exception:
            _df_ta = None

        if _df_ta is None or len(_df_ta) < 20:
            try:
                _start = (_date.today() - _td(days=185)).isoformat()
                _df_ta = _yf.download(
                    _yf_sym, start=_start, auto_adjust=True, progress=False, timeout=15
                )
                if hasattr(_df_ta.columns, "levels") and len(_df_ta.columns.levels) > 1:
                    _df_ta.columns = _df_ta.columns.droplevel(1)
                if _df_ta.empty:
                    _df_ta = None
            except Exception:
                _df_ta = None

        if _df_ta is not None and len(_df_ta) >= 20:
            try:
                ti = _compute_ta(_df_ta)
            except Exception:
                pass

    if ti.get("success"):
        for _k in ("rsi", "macd_hist", "ma20", "ma60", "bb_upper", "bb_lower"):
            if ti.get(_k) is not None:
                result[_k] = ti[_k]

    return result


def tool_get_market_history(params: dict) -> dict:
    """Fetch OHLC price history for any stock/ETF/index/crypto.

    Returns a *compact* summary (period stats + the most recent candles), NOT
    the full series, so the model can reason about trends without flooding the
    context window. Routes through the unified DataService: MarketDataClient
    first (Tushare for A-shares when configured, then Eastmoney/Sina/AKShare,
    yfinance for HK/US/global), then the configured DataRouter as fallback.

    Use this instead of writing ad-hoc akshare/tushare scripts: it handles
    symbol normalisation, source fallback, and is environment-independent.
    """
    symbol = _extract_symbol(params)
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        days = int(params.get("days", 120) or 120)
    except (TypeError, ValueError):
        days = 120
    days = max(5, min(days, 1000))
    interval = str(params.get("interval", "1d") or "1d").lower()

    # Prefer the unified DataService (MDC + DataRouter fallback, honours the
    # user's datasources.yaml). Fall back to MDC directly if it can't load.
    hist = None
    chain = None
    err = None
    try:
        res = _get_data_service().history(symbol, days=days, interval=interval)
        chain = res.provider_chain
        if res.errors:
            err = res.errors[0]
        if (res.data or {}).get("data"):
            hist = res.data
    except Exception as exc:
        err = str(exc)
        if _HAS_MDC and _get_mdc is not None:
            try:
                _h = _get_mdc().history(symbol, days=days, interval=interval)
                if _h.get("data"):
                    hist = _h
                    chain = _h.get("provider_chain")
                else:
                    err = _h.get("error") or err
            except Exception as exc2:
                err = str(exc2)

    if not hist or not hist.get("data"):
        return {
            "success": False,
            "symbol": symbol,
            "provider_chain": chain or ["eastmoney", "sina", "akshare", "yfinance"],
            "error": err or "历史行情数据源暂时不可用，请稍后重试。",
        }

    rows = hist["data"]  # ascending by date: [{date,open,high,low,close,volume},...]
    closes = [r["close"] for r in rows if r.get("close")]
    if not closes:
        return {"success": False, "symbol": symbol, "error": "history contained no usable closes"}

    def _ma(n: int):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    start_close = closes[0]
    end_close = closes[-1]
    period_high = max(r["high"] for r in rows if r.get("high"))
    period_low = min(r["low"] for r in rows if r.get("low"))
    vols = [r["volume"] for r in rows if r.get("volume")]

    # Only the most recent candles travel back to the model — the rest is
    # summarised. Keeps the payload small regardless of `days`.
    recent_n = min(30, len(rows))
    return {
        "success":     True,
        "symbol":      hist.get("symbol") or symbol,
        "name":        hist.get("name") or symbol,
        "provider":    hist.get("provider"),
        "interval":    interval,
        "total_points": len(rows),
        "summary": {
            "start_date":   rows[0].get("date"),
            "end_date":     rows[-1].get("date"),
            "start_close":  round(start_close, 4),
            "end_close":    round(end_close, 4),
            "change_pct":   round((end_close - start_close) / start_close * 100, 2) if start_close else None,
            "period_high":  round(period_high, 4),
            "period_low":   round(period_low, 4),
            "avg_volume":   int(sum(vols) / len(vols)) if vols else None,
            "ma5":          _ma(5),
            "ma20":         _ma(20),
            "ma60":         _ma(60),
        },
        "recent_candles": rows[-recent_n:],
    }


def tool_broker_query(params: dict) -> dict:
    """Query a connected broker account (read-only): balance, positions, orders."""
    if not _HAS_BROKERS:
        return {"success": False, "error": "brokers 模块未加载，请确认 brokers/ 目录存在"}

    query = str(params.get("query", "positions")).lower()
    bid   = params.get("broker_id", "")

    try:
        reg = _get_broker_registry()
        if bid:
            broker = reg.get(bid)
            if not broker:
                broker = reg.connect(bid)
        else:
            broker = reg.active()
            if not broker:
                broker = reg.connect_default()
            if not broker:
                cfgs = _list_broker_configs()
                if not cfgs:
                    return {
                        "success": False,
                        "error": (
                            "尚未配置任何券商。\n"
                            f"请编辑 {_BROKERS_CONFIG_PATH} 或使用 /broker add <type> 命令。"
                        ),
                    }
                return {
                    "success": False,
                    "error": "没有已连接的券商。请先运行 /broker connect <id>。",
                }

        if "account" in query or "balance" in query or "资金" in query or "余额" in query:
            acct = broker.account_info()
            return {
                "success": True, "query": "account",
                "broker": broker.label, "broker_type": broker.broker_type,
                "account_id": acct.masked_account, "currency": acct.currency,
                "total_assets": acct.total_assets, "cash": acct.cash,
                "market_value": acct.market_value, "frozen": acct.frozen,
                "pnl_today": acct.pnl_today, "pnl_total": acct.pnl_total,
            }

        if "position" in query or "持仓" in query or "portfolio" in query:
            positions = broker.positions()
            return {
                "success": True, "query": "positions",
                "broker": broker.label, "count": len(positions),
                "positions": [
                    {
                        "symbol": p.symbol, "name": p.name,
                        "quantity": p.quantity, "available": p.available_qty,
                        "cost": p.cost_price, "price": p.current_price,
                        "market_value": p.market_value,
                        "pnl": p.pnl, "pnl_pct": round(p.pnl_pct, 2),
                        "currency": p.currency,
                    }
                    for p in positions
                ],
            }

        if "order" in query or "订单" in query or "委托" in query:
            status = params.get("status", "all")
            orders = broker.orders(status=status, limit=int(params.get("limit", 20)))
            return {
                "success": True, "query": "orders",
                "broker": broker.label, "status_filter": status,
                "count": len(orders),
                "orders": [
                    {
                        "order_id": o.order_id, "symbol": o.symbol, "name": o.name,
                        "side": o.side, "type": o.order_type,
                        "quantity": o.quantity, "filled": o.filled_qty,
                        "price": o.price, "avg_price": o.avg_price,
                        "status": o.status, "time": o.created_at,
                        "currency": o.currency,
                    }
                    for o in orders
                ],
            }

        # Default: positions
        positions = broker.positions()
        return {
            "success": True, "query": "positions",
            "broker": broker.label, "count": len(positions),
            "positions": [
                {
                    "symbol": p.symbol, "name": p.name,
                    "quantity": p.quantity, "market_value": p.market_value,
                    "pnl": p.pnl, "pnl_pct": round(p.pnl_pct, 2),
                }
                for p in positions
            ],
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_broker_order(params: dict) -> dict:
    """Propose or execute a previewed order through the trading service layer."""
    if not _HAS_BROKERS:
        return {"success": False, "error": "brokers 模块未加载"}

    symbol     = str(params.get("symbol", "")).strip().upper()
    side       = str(params.get("side", "")).lower()
    qty        = params.get("quantity") or params.get("qty")
    price      = params.get("price")
    order_type = str(params.get("order_type", "limit")).lower()
    confirmed  = bool(params.get("confirmed", False))
    preview_id = str(params.get("preview_id", "") or params.get("order_preview_id", "")).strip()
    target_weight = params.get("target_weight")

    try:
        reg    = _get_broker_registry()
        broker = reg.active()
        if not broker:
            broker = reg.connect_default()
    except Exception as _e:
        logger.debug("broker lookup failed: %s", _e)
        broker = None

    if not broker:
        return {"success": False, "error": "无已连接账户，请先 /broker connect"}

    if confirmed:
        if not preview_id:
            return {
                "success": False,
                "confirmation_required": True,
                "error": "confirmed=true 需要 preview_id；请先生成订单预览。",
            }
        try:
            from brokers import execute_order_preview
            return execute_order_preview(broker, preview_id, confirmed=True)
        except Exception as e:
            return {"success": False, "error": str(e)}

    if not symbol:
        return {"success": False, "error": "symbol 是必填项"}
    if side not in ("buy", "sell"):
        return {"success": False, "error": "side 必须是 'buy' 或 'sell'"}
    if not qty:
        return {"success": False, "error": "quantity 是必填项"}
    try:
        qty = float(qty)
        if qty <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return {"success": False, "error": "quantity 必须是正数"}

    try:
        from brokers import OrderIntent, build_order_preview
        preview = build_order_preview(
            broker,
            OrderIntent(
                symbol=symbol,
                side=side,
                quantity=qty,
                price=float(price) if price is not None else None,
                order_type=order_type,
                target_weight=float(target_weight) if target_weight is not None else None,
                source=str(params.get("source", "order_preview") or "order_preview"),
                user_message=str(params.get("user_message", "") or ""),
                metadata={"tool": "broker_order"},
            ),
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    plan_data = preview.get("order_plan")
    plan_message = ""
    risk = (plan_data or {}).get("risk", {})
    if risk.get("violations"):
        plan_message = "\n".join(f"  - {v}" for v in risk.get("violations", []))
    elif risk.get("warnings"):
        plan_message = "\n".join(f"  - {w}" for w in risk.get("warnings", []))
    blockers = preview.get("execution_blockers") or []
    if blockers:
        plan_message = "\n".join(f"  - {v}" for v in blockers)

    if plan_data and plan_data.get("risk", {}).get("violations"):
        return {
            "success": False, "risk_rejected": True,
            "order_plan": plan_data,
            "preview_id": preview.get("preview_id"),
            "trade_preview": preview,
            "message": "订单计划未通过风控：\n" + plan_message,
        }

    _price_str = f"{float(price):.2f}" if price is not None else "市价"
    _side_cn   = "买入" if side == "buy" else "卖出"
    _risk_note = ""
    if plan_data:
        if risk.get("warnings"):
            _risk_note = "\n\n风控提示：\n" + plan_message
        elif blockers:
            _risk_note = "\n\n执行限制：\n" + plan_message
    return {
        "success": False,
        "confirmation_required": True,
        "preview_id": preview.get("preview_id"),
        "trade_preview": preview,
        "order_plan": plan_data,
        "order_preview": {
            "preview_id": preview.get("preview_id"),
            "mode": preview.get("mode"),
            "broker": preview.get("broker_label"),
            "symbol": symbol, "side": side, "side_cn": _side_cn,
            "qty": qty, "price": price, "price_display": _price_str,
            "order_type": order_type,
            "can_execute": preview.get("can_execute"),
        },
        "message": (
            f"⚠️ 请确认以下订单预览 `{preview.get('preview_id')}`：\n"
            f"  模式: {preview.get('mode')}  券商: {preview.get('broker_label')}\n"
            f"  {_side_cn} **{symbol}**  数量: {qty:,.4g}  价格: {_price_str}\n\n"
            "确认时必须携带 preview_id；其他任何回复取消。"
            f"{_risk_note}"
        ),
    }
