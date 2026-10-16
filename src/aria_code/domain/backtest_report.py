"""Local real-data strategy backtest and self-contained HTML rendering."""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from aria_code.artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
from aria_code.data_service import DataService
from aria_code.domain import performance_stats as stats


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    strategy: str = "momentum"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 100000.0
    fast_period: int = 20
    slow_period: int = 60
    momentum_period: int = 20


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _history_days(start_date: Optional[str], end_date: Optional[str]) -> int:
    end = _parse_date(end_date) or date.today()
    start = _parse_date(start_date)
    if not start:
        return 365
    return max((end - start).days + 10, 90)


def _clean_history_rows(rows: Iterable[Dict[str, Any]], start_date: Optional[str], end_date: Optional[str]) -> List[Dict[str, Any]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    cleaned: List[Dict[str, Any]] = []
    for row in rows or []:
        row_date = _parse_date(row.get("date") or row.get("Date"))
        close = _as_float(row.get("close", row.get("Close")))
        if not row_date or close is None or close <= 0:
            continue
        if start and row_date < start:
            continue
        if end and row_date > end:
            continue
        cleaned.append(
            {
                "date": row_date.isoformat(),
                "close": close,
                "open": _as_float(row.get("open", row.get("Open"))),
                "high": _as_float(row.get("high", row.get("High"))),
                "low": _as_float(row.get("low", row.get("Low"))),
                "volume": _as_float(row.get("volume", row.get("Volume"))),
            }
        )
    cleaned.sort(key=lambda x: x["date"])
    return cleaned


def _sma(values: Sequence[float], window: int) -> List[Optional[float]]:
    if window <= 1:
        return [float(v) for v in values]
    out: List[Optional[float]] = []
    rolling = 0.0
    for i, value in enumerate(values):
        rolling += value
        if i >= window:
            rolling -= values[i - window]
        out.append(rolling / window if i + 1 >= window else None)
    return out


# Every name accepted, mapped to the strategy it runs.
STRATEGY_ALIASES = {
    "buy_hold": "buy_hold", "buyhold": "buy_hold", "hold": "buy_hold", "buy_and_hold": "buy_hold",
    "sma_cross": "sma_cross", "ma_cross": "sma_cross", "moving_average": "sma_cross", "sma": "sma_cross",
    "sma_crossover": "sma_cross", "ma_crossover": "sma_cross",
    "momentum": "momentum", "mom": "momentum",
}


def normalize_strategy(name: str) -> Optional[str]:
    """The strategy a name runs, or None if there is no such strategy."""
    return STRATEGY_ALIASES.get((name or "momentum").lower().replace("-", "_").replace(" ", "_"))


def _signals(strategy: str, closes: Sequence[float], fast: int, slow: int, momentum_period: int) -> List[int]:
    known = normalize_strategy(strategy)
    if known is None:
        # Any other name used to fall through to buy-and-hold and be reported
        # under the name asked for: "/backtest sma AAPL" was buy-and-hold.
        raise ValueError(f"Unknown strategy '{strategy}'. Available: momentum, sma_cross (sma), buy_hold")
    strategy = known
    n = len(closes)
    if strategy == "buy_hold":
        return [1] * n
    if strategy == "sma_cross":
        fast_ma = _sma(closes, max(2, fast))
        slow_ma = _sma(closes, max(max(3, slow), fast + 1))
        return [1 if f is not None and s is not None and f > s else 0 for f, s in zip(fast_ma, slow_ma)]
    period = max(2, momentum_period)
    return [1 if i >= period and closes[i] > closes[i - period] else 0 for i in range(n)]


def run_backtest_from_history(history: Sequence[Dict[str, Any]], config: BacktestConfig) -> Dict[str, Any]:
    rows = _clean_history_rows(history, config.start_date, config.end_date)
    min_bars = max(30, min(max(config.slow_period, config.momentum_period) + 2, 80))
    if len(rows) < min_bars:
        return {
            "success": False,
            "symbol": config.symbol,
            "error": f"历史行情不足：需要至少 {min_bars} 根K线，当前 {len(rows)} 根",
        }

    dates = [str(r["date"]) for r in rows]
    closes = [float(r["close"]) for r in rows]
    volumes = [_as_float(r.get("volume")) for r in rows]
    valid_volumes = [v for v in volumes if v is not None and v >= 0]
    try:
        signals = _signals(config.strategy, closes, config.fast_period, config.slow_period, config.momentum_period)
    except ValueError as exc:
        return {"success": False, "symbol": config.symbol, "strategy": config.strategy, "error": str(exc)}

    initial = float(config.initial_capital or 100000.0)
    equity = [initial]
    benchmark = [initial]
    daily_strategy_returns = [0.0]
    daily_benchmark_returns = [0.0]
    trades = 0
    previous_position = 0
    trade_results: List[float] = []   # return of each round trip
    entry_equity = None

    for i in range(1, len(closes)):
        day_return = closes[i] / closes[i - 1] - 1.0
        position = signals[i - 1]  # shift one day to avoid look-ahead bias
        if position == 1 and previous_position == 0:
            trades += 1
            entry_equity = equity[-1]
        elif position == 0 and previous_position == 1 and entry_equity:
            trade_results.append(equity[-1] / entry_equity - 1.0)
            entry_equity = None
        previous_position = position
        strategy_return = day_return * position
        daily_strategy_returns.append(strategy_return)
        daily_benchmark_returns.append(day_return)
        equity.append(equity[-1] * (1.0 + strategy_return))
        benchmark.append(initial * closes[i] / closes[0])

    total_return = equity[-1] / initial - 1.0
    benchmark_return = benchmark[-1] / initial - 1.0
    rets = daily_strategy_returns[1:]
    years = stats.span_years(dates[0], dates[-1])
    if years is None:
        # Undatable rows: count bars as days, the long-standing fallback.
        years = max(len(rows) / stats.DAYS_PER_YEAR, stats.MIN_YEARS)
        periods_per_year = float(stats.FALLBACK_PERIODS_PER_YEAR)
    else:
        periods_per_year = len(rets) / years
    annualized_return = stats.annualized_return(total_return, years)
    volatility = stats.stddev(rets) * math.sqrt(periods_per_year)
    sharpe = stats.sharpe(rets, periods_per_year)
    if entry_equity:
        trade_results.append(equity[-1] / entry_equity - 1.0)   # still open: marked at the last close
    # Winning trades over trades. This was the share of up days while holding,
    # so one buy-and-hold "trade" showed a 52.5% win rate.
    win_rate = sum(1 for r in trade_results if r > 0) / len(trade_results) if trade_results else 0.0

    curve = [
        {
            "date": d,
            "strategy": round(e, 4),
            "benchmark": round(b, 4),
            "close": round(c, 4),
            "position": int(sig),
        }
        for d, e, b, c, sig in zip(dates, equity, benchmark, closes, signals)
    ]

    return {
        "success": True,
        "symbol": config.symbol.upper(),
        "strategy": config.strategy,
        "start": dates[0],
        "end": dates[-1],
        "bars": len(rows),
        "initial_capital": initial,
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized_return, 6),
        "annual_return": round(annualized_return, 6),
        "benchmark_return": round(benchmark_return, 6),
        "buy_hold_return": round(benchmark_return, 6),
        "alpha": round(total_return - benchmark_return, 6),
        "max_drawdown": round(stats.max_drawdown(equity), 6),
        "benchmark_max_drawdown": round(stats.max_drawdown(benchmark), 6),
        "annual_volatility": round(volatility, 6),
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "total_trades": trades,
        "volume_summary": {
            "last": round(valid_volumes[-1], 2) if valid_volumes else None,
            "average": round(sum(valid_volumes) / len(valid_volumes), 2) if valid_volumes else None,
            "min": round(min(valid_volumes), 2) if valid_volumes else None,
            "max": round(max(valid_volumes), 2) if valid_volumes else None,
            "coverage": round(len(valid_volumes) / len(rows), 4) if rows else 0.0,
        },
        "equity_curve": curve,
    }


def _fmt_pct(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number * 100:+.2f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:,.{digits}f}"


def _points(values: Sequence[float], width: int, height: int, pad: int) -> str:
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    if math.isclose(lo, hi):
        lo *= 0.99
        hi *= 1.01
    usable_w = max(width - pad * 2, 1)
    usable_h = max(height - pad * 2, 1)
    pts = []
    for i, value in enumerate(values):
        x = pad + usable_w * (i / max(len(values) - 1, 1))
        y = pad + usable_h * (1 - (value - lo) / (hi - lo))
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def render_backtest_html(result: Dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    curve = result.get("equity_curve") or []
    strategy_values = [_as_float(p.get("strategy")) or 0.0 for p in curve if isinstance(p, dict)]
    benchmark_values = [_as_float(p.get("benchmark")) or 0.0 for p in curve if isinstance(p, dict)]
    width, height, pad = 920, 360, 34
    strategy_points = _points(strategy_values, width, height, pad)
    benchmark_points = _points(benchmark_values, width, height, pad)
    start = html.escape(str(result.get("start", "")))
    end = html.escape(str(result.get("end", "")))
    symbol = html.escape(str(result.get("symbol", "")))
    strategy = html.escape(str(result.get("strategy", "")))
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bars = int(result.get("bars") or len(curve))
    latest_strategy = strategy_values[-1] if strategy_values else 0.0
    latest_benchmark = benchmark_values[-1] if benchmark_values else 0.0
    provider_chain = [str(p) for p in (result.get("provider_chain") or []) if p]
    missing_fields = [str(p) for p in (result.get("missing_fields") or []) if p]
    data_status = html.escape(str(result.get("data_status") or "unknown"))
    data_provider = html.escape(str(result.get("data_provider") or "history"))
    data_updated_at = html.escape(str(result.get("data_updated_at") or ""))
    source_text = html.escape(" → ".join(provider_chain) if provider_chain else data_provider)
    missing_text = html.escape(", ".join(missing_fields) if missing_fields else "none")

    metrics = [
        ("策略收益", _fmt_pct(result.get("total_return"))),
        ("买入持有", _fmt_pct(result.get("benchmark_return"))),
        ("超额收益", _fmt_pct(result.get("alpha"))),
        ("年化收益", _fmt_pct(result.get("annualized_return"))),
        ("最大回撤", _fmt_pct(result.get("max_drawdown"))),
        ("夏普比率", _fmt_num(result.get("sharpe_ratio"), 2)),
        ("胜率", _fmt_pct(result.get("win_rate"))),
        ("交易次数", str(result.get("total_trades", 0))),
    ]
    metric_html = "\n".join(
        f"<div class=\"metric\"><span>{html.escape(k)}</span><strong>{html.escape(v)}</strong></div>"
        for k, v in metrics
    )

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{symbol} {strategy} Backtest</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #f7f7f4;
  --panel: #ffffff;
  --text: #1f1f1f;
  --muted: #787878;
  --line: #d8d4ce;
  --accent: #b8794b;
  --bench: #6d6d6d;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #181818;
    --panel: #222222;
    --text: #eeeeee;
    --muted: #9a9a9a;
    --line: #3a3a3a;
    --accent: #d08a52;
    --bench: #aaaaaa;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  line-height: 1.45;
}}
main {{ max-width: 1080px; margin: 0 auto; padding: 28px 20px 40px; }}
.top {{ display: flex; justify-content: space-between; gap: 16px; align-items: baseline; margin-bottom: 18px; }}
h1 {{ font-size: 24px; margin: 0; letter-spacing: 0; }}
.sub {{ color: var(--muted); font-size: 14px; }}
.panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; margin-top: 14px; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
.metric {{ border-top: 1px solid var(--line); padding-top: 10px; min-height: 58px; }}
.metric span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
.metric strong {{ font-size: 18px; }}
.legend {{ display: flex; gap: 18px; color: var(--muted); font-size: 13px; margin-top: 12px; }}
.legend i {{ display: inline-block; width: 18px; height: 3px; vertical-align: middle; margin-right: 6px; }}
.provenance {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; font-size: 13px; }}
.provenance div {{ border-top: 1px solid var(--line); padding-top: 8px; }}
.provenance span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 3px; }}
svg {{ width: 100%; height: auto; display: block; }}
.foot {{ color: var(--muted); font-size: 12px; margin-top: 14px; }}
</style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>{symbol} · {strategy} 策略回测</h1>
      <div class="sub">{start} → {end} · {bars} bars · real historical data</div>
    </div>
    <div class="sub">Aria Code · {html.escape(created_at)}</div>
  </div>
  <section class="panel metrics">
    {metric_html}
  </section>
  <section class="panel">
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="equity curve">
      <rect x="0" y="0" width="{width}" height="{height}" fill="transparent"/>
      <line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="var(--line)"/>
      <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" stroke="var(--line)"/>
      <polyline points="{benchmark_points}" fill="none" stroke="var(--bench)" stroke-width="2" stroke-dasharray="6 6"/>
      <polyline points="{strategy_points}" fill="none" stroke="var(--accent)" stroke-width="3"/>
      <text x="{pad}" y="{pad - 10}" fill="var(--muted)" font-size="12">策略权益 {html.escape(_fmt_num(latest_strategy, 0))}</text>
      <text x="{width - pad - 210}" y="{pad - 10}" fill="var(--muted)" font-size="12">基准权益 {html.escape(_fmt_num(latest_benchmark, 0))}</text>
    </svg>
    <div class="legend">
      <span><i style="background:var(--accent)"></i>Strategy equity</span>
      <span><i style="background:var(--bench)"></i>Buy & hold</span>
    </div>
  </section>
  <section class="panel provenance">
    <div><span>Data status</span>{data_status}</div>
    <div><span>Provider chain</span>{source_text}</div>
    <div><span>Missing fields</span>{missing_text}</div>
    <div><span>Updated at</span>{data_updated_at or html.escape(created_at)}</div>
  </section>
  <div class="foot">本报告为本地生成的历史模拟，不构成投资建议。信号按前一交易日收盘后生效，避免未来函数。</div>
</main>
</body>
</html>
"""
    output_path.write_text(html_doc, encoding="utf-8")
    return output_path


def generate_backtest_report(
    config: BacktestConfig,
    output_dir: Optional[Path] = None,
    market_client: Optional[Any] = None,
) -> Dict[str, Any]:
    data_service = DataService(market_client=market_client) if market_client is not None else DataService()
    days = _history_days(config.start_date, config.end_date)
    hist_result = data_service.history(config.symbol, days=days, interval="1d")
    hist = hist_result.data
    if not hist_result.success:
        return {
            "success": False,
            "symbol": config.symbol,
            "strategy": config.strategy,
            "error": hist.get("friendly_error") or hist.get("error") or "历史行情获取失败",
            "provider_chain": hist_result.provider_chain,
            "missing_fields": hist_result.missing_fields,
            "data_status": "data_unavailable",
            "data_warnings": hist_result.warnings,
        }

    result = run_backtest_from_history(hist.get("data") or [], config)
    result["data_provider"] = hist_result.source or hist.get("provider")
    result["provider_chain"] = hist_result.provider_chain or hist.get("provider_chain") or [hist.get("provider", "history")]
    result["missing_fields"] = hist_result.missing_fields
    result["data_warnings"] = hist_result.warnings
    result["data_status"] = "complete" if hist_result.success and not hist_result.missing_fields else "partial"
    result["data_updated_at"] = hist_result.timestamp
    if not result.get("success"):
        return result

    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.symbol.upper()).strip("_") or "SYMBOL"
    safe_strategy = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.strategy.lower()).strip("_") or "strategy"
    ts_dt = datetime.now()
    if output_dir:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        output_path = root / f"{safe_symbol}_{safe_strategy}_{ts_dt.strftime('%Y%m%d_%H%M%S')}.html"
        artifact = None
    else:
        artifact = create_user_artifact(
            "strategies/backtests",
            config.symbol,
            f"{safe_symbol}_{safe_strategy}_backtest",
            ".html",
            timestamp=ts_dt,
        )
        output_path = artifact.path
    render_backtest_html(result, output_path)
    if artifact:
        write_artifact_metadata(artifact, {
            "kind": "strategy_backtest",
            "status": "complete",
            "symbol": config.symbol,
            "strategy": config.strategy,
            "created_at": ts_dt.isoformat(timespec="seconds"),
            "data": {
                "provider": result.get("data_provider"),
                "provider_chain": result.get("provider_chain"),
                "missing_fields": result.get("missing_fields"),
                "status": result.get("data_status"),
                "updated_at": result.get("data_updated_at"),
                "rows": len(hist.get("data") or []),
            },
            "config": config.__dict__,
            "metrics": {
                key: result.get(key)
                for key in ("total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "trades")
            },
        })
        write_artifact_raw_data(artifact, {
            "symbol": config.symbol,
            "strategy": config.strategy,
            "history": hist.get("data") or [],
            "data": {
                "provider": result.get("data_provider"),
                "provider_chain": result.get("provider_chain"),
                "missing_fields": result.get("missing_fields"),
                "status": result.get("data_status"),
                "warnings": result.get("data_warnings"),
                "updated_at": result.get("data_updated_at"),
            },
            "result": result,
        })
    result["report_path"] = str(output_path)
    result["provider"] = "local_backtest"
    return result
