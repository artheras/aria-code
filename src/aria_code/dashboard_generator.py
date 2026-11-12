"""
dashboard_generator.py — Bloomberg-style per-request dashboard HTML generator

Usage:
    python3 dashboard_generator.py [--open]

Integration: triggered via /dashboard command in aria_cli.py.

Data sources (all local, embedded at generation time — no runtime API calls):
  - ~/.arthera/portfolio.db  -> positions, trades, realized P&L
  - ~/.aria/daemon.db        -> active price alerts
  - aria_cli config          -> watchlist
  - MarketDataClient         -> market prices with provider fallback
  - artifacts.py             -> recently generated files
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from aria_code.packages.aria_core.paths import aria_home

_PORTFOLIO_DB = aria_home() / "portfolio.db"
_DAEMON_DB    = Path.home() / ".aria"    / "daemon.db"


# ── Data collection ────────────────────────────────────────────────────────────

def _fetch_prices(symbols: List[str]) -> Dict[str, Dict]:
    if not symbols:
        return {}
    result: Dict[str, Dict] = {}
    try:
        from market_data_client import MarketDataClient

        quotes = MarketDataClient().multi_quote(symbols).get("quotes") or {}
        for sym, quote in quotes.items():
            if not quote or not quote.get("success"):
                continue
            price = quote.get("price")
            prev = quote.get("prev_close") or quote.get("previous_close")
            pct = quote.get("change_percent")
            if pct is None and price is not None and prev:
                try:
                    pct = round((float(price) / float(prev) - 1) * 100, 2)
                except Exception:
                    pct = None
            result[sym] = {
                "price": round(float(price), 4) if price is not None else None,
                "prev_close": round(float(prev), 4) if prev is not None else None,
                "pct_change": pct,
                "name": quote.get("name") or sym,
                "provider": quote.get("provider") or quote.get("source") or "",
            }
    except Exception:
        pass
    return result


def _load_portfolio() -> Tuple[List[Dict], List[Dict]]:
    if not _PORTFOLIO_DB.exists():
        return [], []
    try:
        from aria_code.domain.portfolio_ledger import PortfolioLedger
        ledger    = PortfolioLedger()
        positions = ledger.get_positions()
        realized  = ledger.get_realized_pnl()
        return positions, realized
    except Exception:
        return [], []


def _load_alerts() -> List[Dict]:
    if not _DAEMON_DB.exists():
        return []
    try:
        with sqlite3.connect(_DAEMON_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, symbol, condition, value, trigger_count, active, created_at "
                "FROM alerts ORDER BY active DESC, created_at DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _load_recent_artifacts(limit: int = 10) -> List[Dict]:
    items: List[Dict] = []
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from artifacts import recent_artifacts_all
        for art in recent_artifacts_all(limit=limit):
            p = Path(str(art.get("path") or art.get("metadata_path") or "")).expanduser()
            if p.exists():
                items.append({
                    "name":     p.name,
                    "path":     str(p),
                    "category": str(art.get("kind") or art.get("category") or "artifact"),
                    "size_kb":  round(p.stat().st_size / 1024, 1),
                    "mtime":    datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    except Exception:
        return []
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def _market_overview_symbols() -> List[str]:
    return [
        "000001.SS", "399001.SZ", "399006.SZ", "000300.SS",
        "^GSPC", "^IXIC", "^DJI", "^VIX",
        "BTC-USD", "ETH-USD", "GC=F", "CNY=X",
    ]


_SYM_LABELS: Dict[str, str] = {
    "000001.SS": "上证指数",  "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",  "000300.SS": "沪深300",
    "^GSPC":     "S&P 500",   "^IXIC":     "NASDAQ",
    "^DJI":      "DOW JONES", "^VIX":      "VIX",
    "BTC-USD":   "BTC/USD",   "ETH-USD":   "ETH/USD",
    "GC=F":      "GOLD $/oz", "CNY=X":     "USD/CNY",
}


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _pct_cls(pct: Optional[float]) -> str:
    if pct is None:
        return "flat"
    return "up" if pct >= 0 else "down"


def _pct_str(pct: Optional[float]) -> str:
    if pct is None:
        return "--"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def _price_str(price: Optional[float], sym: str = "") -> str:
    if not price:
        return "--"
    if price >= 10_000:
        return f"{price:,.0f}"
    if price >= 1_000:
        return f"{price:,.2f}"
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}".rstrip("0").rstrip(".")
    return f"{price:.6f}".rstrip("0").rstrip(".")


def _quote_tiles(items: List[Dict]) -> str:
    parts = []
    for d in items:
        sym   = d["symbol"]
        label = d.get("label", sym)
        pct   = d.get("pct")
        price = d.get("price")
        cls   = _pct_cls(pct)
        arrow = "▲" if cls == "up" else "▼" if cls == "down" else ""
        parts.append(
            f'<div class="qt">'
            f'<div class="qt-sym">{sym}</div>'
            f'<div class="qt-name">{label}</div>'
            f'<div class="qt-price">{_price_str(price, sym)}</div>'
            f'<div class="qt-chg {cls}">{arrow} {_pct_str(pct)}</div>'
            f'</div>'
        )
    return "\n".join(parts)


def _positions_table(positions: List[Dict]) -> str:
    rows = []
    for p in positions:
        upnl = p.get("unrealized_pnl")
        upct = p.get("unrealized_pct")
        dpct = p.get("day_pct")
        cu = _pct_cls(upnl)
        cd = _pct_cls(dpct)
        price_str  = str(p.get("current_price") or "--")
        mktv_str   = f"{p.get('market_value'):,.0f}" if p.get("market_value") else "--"
        upnl_str   = ("+" if (upnl or 0) > 0 else "") + f"{upnl:,.0f}" if upnl is not None else "--"
        upct_str   = _pct_str(upct)
        dpct_str   = _pct_str(dpct)
        rows.append(
            f"<tr>"
            f'<td class="sym">{p["symbol"]}</td>'
            f'<td class="num">{p["net_qty"]:,}</td>'
            f'<td class="num">{p["avg_cost"]:.4f}</td>'
            f'<td class="num">{price_str}</td>'
            f'<td class="num {cd}">{dpct_str}</td>'
            f'<td class="num">{mktv_str}</td>'
            f'<td class="num {cu}">{upnl_str}</td>'
            f'<td class="num {cu}">{upct_str}</td>'
            f"</tr>"
        )
    return (
        '<table class="data-table">'
        "<thead><tr>"
        "<th>SYMBOL</th>"
        '<th class="r">QTY</th>'
        '<th class="r">AVG COST</th>'
        '<th class="r">PRICE</th>'
        '<th class="r">DAY CHG</th>'
        '<th class="r">MKT VALUE</th>'
        '<th class="r">UNREALIZED</th>'
        '<th class="r">RETURN %</th>'
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _alerts_table(alerts: List[Dict]) -> str:
    rows = []
    for a in alerts[:15]:
        cond  = (a.get("condition") or "").upper().replace("_", " ")
        astat = "ACTIVE" if a.get("active") else "OFF"
        bcls  = "badge-on" if a.get("active") else "badge-off"
        rows.append(
            f"<tr>"
            f'<td class="sym">{a.get("symbol", "")}</td>'
            f"<td>{cond}</td>"
            f'<td class="num">{a.get("value", "")}</td>'
            f'<td class="num dim">{a.get("trigger_count", 0)}x</td>'
            f'<td><span class="badge {bcls}">{astat}</span></td>'
            f"</tr>"
        )
    return (
        '<table class="data-table">'
        "<thead><tr>"
        "<th>SYMBOL</th><th>CONDITION</th>"
        '<th class="r">LEVEL</th>'
        '<th class="r">TRIGGERED</th>'
        "<th>STATUS</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _movers_table(items: List[Dict], limit: int = 8) -> str:
    ranked = sorted(
        [d for d in items if d.get("pct") is not None],
        key=lambda d: d.get("pct") or 0,
        reverse=True,
    )[:limit]
    rows = []
    for d in ranked:
        pct = d.get("pct")
        cls = _pct_cls(pct)
        rows.append(
            f"<tr>"
            f'<td class="sym">{d.get("symbol", "")}</td>'
            f'<td>{d.get("label", d.get("symbol", ""))}</td>'
            f'<td class="num">{_price_str(d.get("price"), d.get("symbol", ""))}</td>'
            f'<td class="num {cls}">{_pct_str(pct)}</td>'
            f"</tr>"
        )
    if not rows:
        return '<div style="color:var(--text-muted);font-size:12px;padding:14px 0">NO MOVERS</div>'
    return (
        '<table class="data-table">'
        "<thead><tr>"
        "<th>SYMBOL</th><th>NAME</th>"
        '<th class="r">PRICE</th>'
        '<th class="r">CHG%</th>'
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _artifacts_list(artifacts: List[Dict]) -> str:
    rows = []
    for a in artifacts:
        cat  = (a.get("category") or "").upper()
        rows.append(
            f"<tr>"
            f'<td class="sym" style="font-size:10px">{cat}</td>'
            f"<td><span style=\"color:var(--text-primary);font-size:12px\">{a['name']}</span></td>"
            f'<td class="num dim">{a.get("size_kb", 0)} KB</td>'
            f'<td class="dim">{a.get("mtime", "")}</td>'
            f'<td><a href="file://{a["path"]}" target="_blank" class="badge badge-off" style="text-decoration:none">OPEN</a></td>'
            f"</tr>"
        )
    return (
        '<table class="data-table">'
        "<thead><tr>"
        "<th>TYPE</th><th>FILE</th>"
        '<th class="r">SIZE</th>'
        "<th>MODIFIED</th><th></th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _metric_card(label: str, value: str, sub: str = "", cls: str = "") -> str:
    val_cls = f' class="{cls}"' if cls else ""
    return (
        '<div class="metric">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-val{val_cls}">{value}</div>'
        + (f'<div class="metric-sub">{sub}</div>' if sub else "")
        + "</div>"
    )


# ── Main generator ─────────────────────────────────────────────────────────────

def generate(
    watchlist:   Optional[List[str]] = None,
    config:      Optional[Dict]      = None,
    mode:        str                 = "full",
    output_path: Optional[Path]      = None,
) -> Path:
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
    stamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    watchlist = watchlist or (config or {}).get("watchlist") or ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]

    positions, realized  = _load_portfolio()
    alerts               = _load_alerts()
    artifacts            = _load_recent_artifacts()

    port_syms = [p["symbol"] for p in positions]
    all_syms  = list(dict.fromkeys(_market_overview_symbols() + watchlist + port_syms))
    prices    = _fetch_prices(all_syms)

    for pos in positions:
        q    = prices.get(pos["symbol"]) or {}
        price = q.get("price", 0)
        cost  = pos.get("avg_cost") or 0
        qty   = pos.get("net_qty", 0)
        pos["current_price"]  = price or None
        pos["market_value"]   = round(price * qty, 2) if price else None
        pos["unrealized_pnl"] = round((price - cost) * qty, 2) if price and cost else None
        pos["unrealized_pct"] = round((price / cost - 1) * 100, 2) if price and cost else None
        pos["day_pct"]        = q.get("pct_change")

    total_mktv    = sum(p.get("market_value") or 0 for p in positions)
    total_cost    = sum(p.get("cost_basis") or 0 for p in positions)
    total_unreal  = sum(p.get("unrealized_pnl") or 0 for p in positions)
    total_realized = sum(r.get("total_pnl", 0) for r in realized)

    market_data = [
        {"symbol": s, "label": _SYM_LABELS.get(s, s), "price": (prices.get(s) or {}).get("price"), "pct": (prices.get(s) or {}).get("pct_change")}
        for s in _market_overview_symbols()
    ]
    watchlist_data = [
        {"symbol": s, "label": s, "price": (prices.get(s) or {}).get("price"), "pct": (prices.get(s) or {}).get("pct_change")}
        for s in watchlist
    ]
    _seen_symbols = set()
    movers_data = []
    for item in market_data + watchlist_data:
        sym = item.get("symbol")
        if sym and sym in _seen_symbols:
            continue
        if sym:
            _seen_symbols.add(sym)
        movers_data.append(item)

    # ── Portfolio metrics ──────────────────────────────────────────────────────
    mktv_str    = f"{total_mktv:,.0f}" if total_mktv else "--"
    cost_str    = f"{total_cost:,.0f}" if total_cost else "--"
    unreal_cls  = "up" if total_unreal > 0 else "down" if total_unreal < 0 else ""
    unreal_str  = ("+" if total_unreal > 0 else "") + f"{total_unreal:,.0f}" if total_unreal else "--"
    real_cls    = "up" if total_realized > 0 else "down" if total_realized < 0 else ""
    real_str    = ("+" if total_realized > 0 else "") + f"{total_realized:,.0f}" if total_realized else "--"
    active_alerts = len([a for a in alerts if a.get("active")])

    positions_html = _positions_table(positions) if positions else (
        '<div style="color:var(--text-muted);font-size:12px;padding:14px 0">'
        'NO POSITIONS — add via /journal add buy SYMBOL QTY PRICE'
        '</div>'
    )
    alerts_html = _alerts_table(alerts) if alerts else (
        '<div style="color:var(--text-muted);font-size:12px;padding:14px 0">'
        'NO ALERTS — add via /alert add SYMBOL gt 200'
        '</div>'
    )
    artifacts_html = _artifacts_list(artifacts) if artifacts else (
        '<div style="color:var(--text-muted);font-size:12px;padding:14px 0">'
        'NO RECENT FILES — run /backtest or /report to generate'
        '</div>'
    )

    mode = (mode or "full").lower().strip()
    if mode not in {"full", "brief", "market", "portfolio"}:
        mode = "full"

    include_portfolio = mode in {"full", "portfolio"}
    include_market = mode in {"full", "market", "brief", "portfolio"}
    include_watchlist = mode in {"full", "market", "brief"}
    include_alerts = mode in {"full", "portfolio"}
    include_artifacts = mode in {"full", "portfolio"}
    include_movers = mode in {"brief", "market", "full"}
    mode_blurb = {
        "brief": "MORNING BRIEF — INDEXES, MOVERS, WATCHLIST",
        "market": "MARKET DASHBOARD — OVERVIEW, MOVERS, WATCHLIST",
        "portfolio": "PORTFOLIO DASHBOARD — POSITIONS, ALERTS, FILES",
        "full": "FULL TERMINAL — PORTFOLIO + MARKET + ALERTS + FILES",
    }.get(mode, "FULL TERMINAL")

    from apps.cli.prompts.ui import get_ui_css_base
    css = get_ui_css_base()

    html = f"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ARIA TERMINAL — {now_str}</title>
<style>
{css}
/* ── Dashboard-specific layout ── */
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--border); border: 1px solid var(--border); }}
.two-col > * {{ background: var(--bg-primary); }}
.col-inner {{ padding: 14px; }}
.no-pos {{ color: var(--text-muted); font-size: 12px; padding: 14px 0; font-family: var(--font-mono); }}
.data-source {{ font-size: 10px; color: var(--text-muted); font-family: var(--font-mono);
  margin-top: 8px; letter-spacing: 0.04em; }}
</style>
</head>
<body>

<!-- ── Header ── -->
<div class="topbar">
  <div class="topbar-brand">ARIA <span>TERMINAL</span></div>
  <div class="topbar-meta">GENERATED {now_str.upper()} &nbsp;·&nbsp; MODE: {mode.upper()} &nbsp;·&nbsp; DATA: MARKET DATA SERVICE + LOCAL DB &nbsp;·&nbsp; DELAYED/PROVIDER DEPENDENT</div>
</div>

<div class="section">
  <div class="sh">{mode_blurb}</div>
  <div class="metric-sub">Mode-specific layout keeps morning brief, market view, and portfolio view distinct.</div>
</div>

<!-- ── Portfolio Summary ── -->
{f'''<div class="section">
  <div class="sh">PORTFOLIO SUMMARY</div>
  <div class="grid g4" style="margin-bottom:1px">
    {_metric_card("MARKET VALUE", mktv_str)}
    {_metric_card("COST BASIS", cost_str)}
    {_metric_card("UNREALIZED P&L", unreal_str, sub="mark-to-market", cls=unreal_cls)}
    {_metric_card("REALIZED P&L", real_str, sub="all closed trades", cls=real_cls)}
  </div>
</div>''' if include_portfolio else ''}

<!-- ── Positions Table ── -->
{f'''<div class="section">
  <div class="sh">OPEN POSITIONS ({len(positions)})</div>
  {positions_html}
</div>''' if include_portfolio else ''}

<!-- ── Market Overview ── -->
{f'''<div class="section">
  <div class="sh">TOP MOVERS</div>
  {_movers_table(movers_data, limit=8)}
</div>''' if include_movers else ''}

{f'''<div class="section">
  <div class="sh">MARKET OVERVIEW — A-SHARE</div>
  <div class="grid g4" style="margin-bottom:1px">
    {_quote_tiles(market_data[:4])}
  </div>
</div>''' if include_market else ''}

{f'''<div class="section">
  <div class="sh">MARKET OVERVIEW — US EQUITY</div>
  <div class="grid g4" style="margin-bottom:1px">
    {_quote_tiles(market_data[4:8])}
  </div>
</div>''' if include_market else ''}

{f'''<div class="section">
  <div class="sh">CRYPTO / COMMODITY / FX</div>
  <div class="grid g4" style="margin-bottom:1px">
    {_quote_tiles(market_data[8:])}
  </div>
  <div class="data-source">PRICES VIA ARIA MARKET DATA ROUTER — PROVIDER ATTRIBUTED — NOT FOR TRADING</div>
</div>''' if include_market else ''}

<!-- ── Watchlist ── -->
{f'''<div class="section">
  <div class="sh">WATCHLIST ({len(watchlist)} SYMBOLS)</div>
  <div class="grid g{'6' if len(watchlist_data) > 4 else '4'}" style="margin-bottom:1px">
    {_quote_tiles(watchlist_data)}
  </div>
</div>''' if include_watchlist else ''}

<!-- ── Alerts + Artifacts ── -->
{f'''<div class="two-col">
  <div class="col-inner">
    <div class="sh">PRICE ALERTS ({active_alerts} ACTIVE)</div>
    {alerts_html}
  </div>
  <div class="col-inner">
    <div class="sh">RECENT GENERATED FILES</div>
    {artifacts_html}
  </div>
</div>''' if (include_alerts or include_artifacts) else ''}

</body></html>"""

    artifact = None
    if output_path is None:
        from artifacts import create_user_artifact

        artifact = create_user_artifact("dashboards", mode, f"aria_dashboard_{mode}", ".html")
        out = artifact.path
    else:
        out = output_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    if artifact is not None:
        try:
            from artifacts import write_artifact_metadata, write_artifact_raw_data

            write_artifact_metadata(artifact, {
                "kind": "dashboard",
                "status": "complete",
                "mode": mode,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "data": {
                    "watchlist": watchlist,
                    "market_symbols": _market_overview_symbols(),
                    "position_count": len(positions),
                    "alert_count": len(alerts),
                },
            })
            write_artifact_raw_data(artifact, {
                "market": market_data,
                "watchlist": watchlist_data,
                "positions": positions,
                "alerts": alerts,
            })
        except Exception:
            pass
    return out


def _open_in_browser(path: Path) -> None:
    try:
        sys_name = platform.system()
        if sys_name == "Darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys_name == "Windows":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def generate_and_open(
    watchlist: Optional[List[str]] = None,
    config:    Optional[Dict]      = None,
    mode:      str                 = "full",
) -> Path:
    out = generate(watchlist=watchlist, config=config, mode=mode)
    _open_in_browser(out)
    return out


if __name__ == "__main__":
    p = generate_and_open()
    print(f"Dashboard saved: {p}")
