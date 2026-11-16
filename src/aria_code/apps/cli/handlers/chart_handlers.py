"""Stock chart analysis handlers extracted from aria_cli.py."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Callable

from aria_code.apps.cli.plotly_html import plotly_script_tag


def _normalise_history_frame(hist):
    """Return a clean OHLCV frame with title-case columns, or None."""
    if hist is None or getattr(hist, "empty", True):
        return None
    try:
        hist = hist.copy()
        if getattr(hist, "columns", None) is not None:
            try:
                import pandas as _pd
                if isinstance(hist.columns, _pd.MultiIndex):
                    hist.columns = hist.columns.droplevel(-1)
            except Exception:
                pass
            hist.columns = [str(c).title() for c in hist.columns]
        if "Close" not in hist.columns:
            return None
        hist = hist.dropna(subset=["Close"]).copy()
        return hist if not hist.empty else None
    except Exception:
        return None


def _normalise_chart_symbol(symbol: str) -> str:
    """Normalize common A-share inputs for chart providers."""
    raw = (symbol or "").strip().upper()
    if not raw:
        return raw
    if re.match(r"^\d{6}$", raw):
        return f"{raw}.SS" if raw.startswith(("6", "9")) else f"{raw}.SZ"
    m = re.match(r"^(?:SH|SZ)([036]\d{5}|68\d{4})$", raw)
    if m:
        code = m.group(1)
        return f"{code}.SS" if raw.startswith("SH") else f"{code}.SZ"
    if raw.endswith(".SH"):
        return raw[:-3] + ".SS"
    return raw


def _ashare_plain_symbol(symbol: str) -> str:
    """Return 6-digit A-share code for akshare, or empty string."""
    raw = (symbol or "").strip().upper()
    m = re.match(r"^(?:SH|SZ)?([036]\d{5}|68\d{4})(?:\.(?:SS|SH|SZ))?$", raw)
    return m.group(1) if m else ""


def _days_for_period(period: str) -> int:
    return {
        "1mo": 35, "1m": 35,
        "3mo": 100, "3m": 100,
        "6mo": 185, "6m": 185,
        "ytd": 370,
        "1y": 370,
        "2y": 740,
        "3y": 1100,
        "5y": 1830,
        "max": 7300,
    }.get((period or "1y").lower(), 370)


def _fetch_akshare_history_frame(symbol: str, period: str):
    """Fetch A-share history via akshare; returns (frame, currency, error)."""
    code = _ashare_plain_symbol(symbol)
    if not code:
        return None, None, "not an A-share symbol"
    try:
        import pandas as _pd
        import akshare as _ak
        from datetime import datetime as _dt, timedelta as _td

        end_date = _dt.now().strftime("%Y%m%d")
        start_date = (_dt.now() - _td(days=_days_for_period(period))).strftime("%Y%m%d")
        frame = _ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if frame is None or frame.empty:
            return None, None, "empty akshare result"
        frame = frame.rename(columns={
            "日期": "Date",
            "开盘": "Open",
            "最高": "High",
            "最低": "Low",
            "收盘": "Close",
            "成交量": "Volume",
            "成交额": "Turnover",
            "振幅": "Amplitude",
            "涨跌幅": "ChangePct",
            "涨跌额": "Change",
            "换手率": "TurnoverRate",
        })
        if "Date" in frame.columns:
            frame["Date"] = _pd.to_datetime(frame["Date"])
            frame = frame.set_index("Date")
        frame = frame.sort_index()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in frame.columns:
                frame[col] = _pd.to_numeric(frame[col], errors="coerce")
        frame = _normalise_history_frame(frame)
        return frame, "CNY", "" if frame is not None else "akshare frame missing Close"
    except Exception as exc:
        return None, None, str(exc)


def _fetch_mdc_history_frame(symbol: str, period: str, interval: str = "1d"):
    """Fetch history through the unified MarketDataClient provider chain."""
    code = _ashare_plain_symbol(symbol)
    if not code:
        return None, None, "", "not an A-share symbol"
    try:
        import pandas as _pd
        from aria_code.clients.market_data_client import get_mdc

        result = get_mdc().history(code, days=_days_for_period(period), interval=interval)
        if not result or not result.get("success"):
            return None, None, "", str((result or {}).get("error") or "empty MarketDataClient result")
        records = result.get("data") or []
        if not records:
            return None, None, "", "MarketDataClient returned no bars"
        frame = _pd.DataFrame(records).rename(columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        })
        if "Date" in frame.columns:
            frame["Date"] = _pd.to_datetime(frame["Date"])
            frame = frame.set_index("Date")
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in frame.columns:
                frame[col] = _pd.to_numeric(frame[col], errors="coerce")
        frame = _normalise_history_frame(frame)
        provider_chain = result.get("provider_chain") or [result.get("provider", "market_data_client")]
        provider = " → ".join(str(item) for item in provider_chain if item) or "market_data_client"
        return frame, "CNY", provider, "" if frame is not None else "MarketDataClient frame missing Close"
    except Exception as exc:
        return None, None, "", str(exc)


def _fetch_yahoo_chart_frame(symbol: str, period: str, interval: str = "1d"):
    """Fetch Yahoo chart API data without yfinance; returns (frame, currency, error)."""
    try:
        import pandas as _pd
        import requests as _req
        p2 = int(time.time())
        days_by_period = {
            "1mo": 35, "3mo": 100, "6mo": 185, "ytd": 370,
            "1y": 370, "2y": 740, "3y": 1100, "5y": 1830, "max": 7300,
        }
        p1 = p2 - days_by_period.get(period, 370) * 86400
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?period1={p1}&period2={p2}&interval={interval}&events=history"
            f"&includeAdjustedClose=true"
        )
        resp = _req.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        result = (resp.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None, None, "empty Yahoo Chart result"
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        timestamps = result.get("timestamp") or []
        frame = _pd.DataFrame({
            "Open": quote.get("open", []),
            "High": quote.get("high", []),
            "Low": quote.get("low", []),
            "Close": quote.get("close", []),
            "Volume": quote.get("volume", []),
        }, index=_pd.to_datetime(timestamps, unit="s"))
        frame = _normalise_history_frame(frame)
        meta = result.get("meta") or {}
        return frame, meta.get("currency"), ""
    except Exception as exc:
        return None, None, str(exc)


def _fmt_num(value, digits: int = 2, prefix: str = "") -> str:
    try:
        if value is None or (hasattr(value, "__class__") and str(value) == "nan"):
            return "N/A"
        return f"{prefix}{float(value):,.{digits}f}"
    except Exception:
        return "N/A"


def _fmt_int(value) -> str:
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return "N/A"


def _write_ta_png_artifact(record, hist, symbol: str, name: str, currency: str,
                           is_ashare: bool, ma20, ma60, rsi14, macd_v, macd_s_val) -> tuple[str, str]:
    """Write a compact PNG TA chart next to the HTML artifact when possible."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        import pandas as _pd
        import math as _math
    except Exception as exc:
        return "", f"needs matplotlib — pip install 'aria-code[charts]' ({exc})"

    try:
        png_path = record.path.with_suffix(".png")
        plot_df = hist.tail(180).copy()
        x = list(range(len(plot_df)))
        inc_color = "#dc2626" if is_ashare else "#16a34a"
        dec_color = "#16a34a" if is_ashare else "#dc2626"

        fig = plt.figure(figsize=(14, 9), dpi=150)
        gs = fig.add_gridspec(4, 1, height_ratios=[4.2, 1.0, 1.2, 1.2], hspace=0.08)
        ax_price = fig.add_subplot(gs[0])
        ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
        ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)
        ax_macd = fig.add_subplot(gs[3], sharex=ax_price)

        for i, row in enumerate(plot_df.itertuples()):
            open_v = float(getattr(row, "Open", getattr(row, "Close")))
            high_v = float(getattr(row, "High", getattr(row, "Close")))
            low_v = float(getattr(row, "Low", getattr(row, "Close")))
            close_v = float(getattr(row, "Close"))
            color = inc_color if close_v >= open_v else dec_color
            ax_price.vlines(i, low_v, high_v, color=color, linewidth=0.8)
            body_low = min(open_v, close_v)
            body_h = max(abs(close_v - open_v), max(close_v * 0.001, 0.01))
            ax_price.add_patch(Rectangle((i - 0.32, body_low), 0.64, body_h,
                                         facecolor=color, edgecolor=color, linewidth=0.6))

        for col, color, label, lw in (
            ("MA20", "#f59e0b", "MA20", 1.2),
            ("MA60", "#ef4444", "MA60", 1.2),
            ("BB_UP", "#6366f1", "BB upper", 0.8),
            ("BB_LO", "#6366f1", "BB lower", 0.8),
        ):
            if col in plot_df.columns:
                ax_price.plot(x, plot_df[col].astype(float), color=color, linewidth=lw,
                              linestyle="--" if col.startswith("BB_") else "-", label=label)

        if "Volume" in plot_df.columns:
            closes = plot_df["Close"].astype(float).tolist()
            vols = plot_df["Volume"].fillna(0).astype(float).tolist()
            colors = [inc_color if i == 0 or closes[i] >= closes[i - 1] else dec_color for i in range(len(closes))]
            ax_vol.bar(x, vols, color=colors, width=0.75, alpha=0.65)

        if "RSI14" in plot_df.columns:
            ax_rsi.plot(x, plot_df["RSI14"].astype(float), color="#8b5cf6", linewidth=1.1)
            ax_rsi.axhline(70, color=dec_color, linestyle=":", linewidth=0.8)
            ax_rsi.axhline(30, color=inc_color, linestyle=":", linewidth=0.8)
            ax_rsi.set_ylim(0, 100)

        if "MACD_HIS" in plot_df.columns:
            hist_vals = plot_df["MACD_HIS"].astype(float).tolist()
            colors = [inc_color if v >= 0 else dec_color for v in hist_vals]
            ax_macd.bar(x, hist_vals, color=colors, width=0.75, alpha=0.65)
        if "MACD" in plot_df.columns:
            ax_macd.plot(x, plot_df["MACD"].astype(float), color="#2563eb", linewidth=1.0, label="MACD")
        if "MACD_SIG" in plot_df.columns:
            ax_macd.plot(x, plot_df["MACD_SIG"].astype(float), color="#f59e0b", linewidth=1.0, linestyle="--", label="Signal")
        ax_macd.axhline(0, color="#94a3b8", linewidth=0.7)

        tick_step = max(1, len(plot_df) // 8)
        tick_pos = list(range(0, len(plot_df), tick_step))
        tick_labels = []
        for idx in tick_pos:
            raw = plot_df.index[idx]
            tick_labels.append(raw.strftime("%Y-%m-%d") if hasattr(raw, "strftime") else str(raw)[:10])
        ax_macd.set_xticks(tick_pos)
        ax_macd.set_xticklabels(tick_labels, rotation=25, ha="right", fontsize=8)
        for ax in (ax_price, ax_vol, ax_rsi, ax_macd):
            ax.grid(True, color="#e5e7eb", linewidth=0.6, alpha=0.7)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
        ax_price.legend(loc="upper left", fontsize=8, ncols=4)
        ax_price.set_ylabel(currency)
        ax_vol.set_ylabel("Volume")
        ax_rsi.set_ylabel("RSI")
        ax_macd.set_ylabel("MACD")
        title = f"{name} ({symbol}) TA"
        subtitle = f"MA20={ma20:.2f}" if ma20 else "MA20=—"
        if ma60:
            subtitle += f"  MA60={ma60:.2f}"
        if rsi14:
            subtitle += f"  RSI={rsi14:.1f}"
        if macd_v is not None and macd_s_val is not None:
            subtitle += f"  MACD={macd_v:.3f}/{macd_s_val:.3f}"
        fig.suptitle(f"{title}\n{subtitle}", fontsize=12)
        fig.savefig(png_path, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        if not png_path.exists() or png_path.stat().st_size <= 0:
            return "", "PNG 文件未生成"
        return str(png_path), ""
    except Exception as exc:
        try:
            plt.close("all")
        except Exception:
            pass
        return "", str(exc)


def _review_chart(symbol: str, last_close: float, high_52w: float, low_52w: float,
                  rsi14, ma20, ma60, bb_up, bb_lo, sup3, res3, n_bars: int) -> list[str]:
    """
    自审函数：检查图表数据质量，返回问题列表（空列表 = 通过）。
    在 cmd_chart 中调用，用于发现并反馈图表异常。
    """
    issues = []
    if last_close <= 0:
        issues.append("价格数据异常（收盘价 ≤ 0）")
    if n_bars < 20:
        issues.append(f"历史数据不足 20 根 K 线（仅 {n_bars} 根），指标不可靠")
    if rsi14 is None:
        issues.append("RSI 计算失败（数据可能不足 14 根）")
    elif not (0 < rsi14 < 100):
        issues.append(f"RSI 值异常: {rsi14:.1f}（应在 0-100 之间）")
    if ma20 and last_close > 0:
        if abs(ma20 / last_close - 1) > 0.5:
            issues.append(f"MA20 偏离价格超 50%，数据可能存在复权误差（MA20={ma20:.2f} 价格={last_close:.2f}）")
    if bb_up and bb_lo and bb_up <= bb_lo:
        issues.append("布林带上下轨计算倒置（BB_UP <= BB_LO）")
    if sup3 and min(sup3) >= last_close:
        issues.append("支撑位计算有误（支撑位不应高于现价）")
    if res3 and max(res3) <= last_close:
        issues.append("阻力位计算有误（阻力位不应低于现价）")
    price_range_pct = (high_52w - low_52w) / low_52w * 100 if low_52w > 0 else 0
    if price_range_pct > 1000:
        issues.append(f"52周价格波动超 1000%（{price_range_pct:.0f}%），可能存在股票分拆/复权问题")
    return issues


def handle_multi_stock_comparison_direct(symbols: list[str], period: str = "1y") -> dict:
    """Generate a normalized multi-symbol performance comparison chart."""
    try:
        import pandas as _pd
    except Exception as exc:
        return {"success": False, "error": f"缺少依赖: {exc}"}

    _PERIOD_MAP = {
        "1m": "1mo", "3m": "3mo", "6m": "6mo",
        "1y": "1y", "2y": "2y", "3y": "3y", "5y": "5y",
        "ytd": "ytd", "max": "max",
    }
    period = _PERIOD_MAP.get(str(period or "1y").lower(), str(period or "1y").lower())
    clean_symbols: list[str] = []
    for sym in symbols or []:
        normalized = _normalise_chart_symbol(str(sym or "").strip().upper())
        if normalized and normalized not in clean_symbols:
            clean_symbols.append(normalized)
    if len(clean_symbols) < 2:
        return {"success": False, "error": "至少需要两个标的才能生成对比图"}

    series: dict[str, _pd.Series] = {}
    raw_rows: dict[str, list[dict]] = {}
    providers: dict[str, str] = {}
    errors: dict[str, str] = {}

    for symbol in clean_symbols:
        hist = None
        currency = None
        provider = ""
        err = ""
        if _ashare_plain_symbol(symbol):
            hist, currency, provider, err = _fetch_mdc_history_frame(symbol, period, "1d")
        if hist is None or hist.empty:
            hist, currency, err = _fetch_yahoo_chart_frame(symbol, period, "1d")
            provider = "Yahoo Chart API" if hist is not None and not hist.empty else provider
        hist = _normalise_history_frame(hist)
        if hist is None or hist.empty or "Close" not in hist.columns:
            errors[symbol] = err or "empty history"
            continue
        closes = _pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if len(closes) < 5:
            errors[symbol] = "not enough close prices"
            continue
        series[symbol] = closes
        providers[symbol] = provider or "market data"
        rows = []
        for idx, row in hist.tail(370).iterrows():
            rows.append({
                "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                "open": None if _pd.isna(row.get("Open")) else float(row.get("Open")),
                "high": None if _pd.isna(row.get("High")) else float(row.get("High")),
                "low": None if _pd.isna(row.get("Low")) else float(row.get("Low")),
                "close": None if _pd.isna(row.get("Close")) else float(row.get("Close")),
                "volume": None if _pd.isna(row.get("Volume")) else float(row.get("Volume")),
                "currency": currency,
            })
        raw_rows[symbol] = rows

    if len(series) < 2:
        return {
            "success": False,
            "error": "可用历史数据不足，无法生成对比图",
            "errors": errors,
        }

    closes_df = _pd.concat(series, axis=1).dropna(how="all").ffill().dropna()
    normalized_df = closes_df.divide(closes_df.iloc[0]).multiply(100.0)
    returns_df = closes_df.pct_change().dropna()

    metrics = []
    for symbol in normalized_df.columns:
        norm = normalized_df[symbol].dropna()
        rets = returns_df[symbol].dropna() if symbol in returns_df else _pd.Series(dtype=float)
        drawdown = norm / norm.cummax() - 1.0
        metrics.append({
            "symbol": symbol,
            "total_return_pct": round(float(norm.iloc[-1] - 100.0), 2),
            "volatility_pct": round(float(rets.std() * (252 ** 0.5) * 100), 2) if len(rets) else 0.0,
            "max_drawdown_pct": round(float(drawdown.min() * 100), 2) if len(drawdown) else 0.0,
            "last_close": round(float(closes_df[symbol].dropna().iloc[-1]), 4),
        })
    metrics.sort(key=lambda row: row["total_return_pct"], reverse=True)

    dates = [idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx) for idx in normalized_df.index]
    traces = []
    for symbol in normalized_df.columns:
        traces.append({
            "x": dates,
            "y": [None if _pd.isna(v) else round(float(v), 4) for v in normalized_df[symbol].tolist()],
            "type": "scatter",
            "mode": "lines",
            "name": symbol,
            "line": {"width": 2},
        })

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", "_".join(normalized_df.columns))
    from aria_code.artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
    artifact = create_user_artifact("stock-charts", safe, f"{safe}_comparison", ".html")
    out_file = artifact.path
    metrics_rows = "".join(
        "<tr>"
        f"<td>{m['symbol']}</td>"
        f"<td>{m['total_return_pct']:+.2f}%</td>"
        f"<td>{m['volatility_pct']:.2f}%</td>"
        f"<td>{m['max_drawdown_pct']:.2f}%</td>"
        f"<td>{m['last_close']}</td>"
        "</tr>"
        for m in metrics
    )
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{' vs '.join(normalized_df.columns)} comparison</title>
  {plotly_script_tag()}
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px;color:#111827}}
    h1{{font-size:20px;margin:0 0 8px}}
    .meta{{color:#6b7280;margin-bottom:18px}}
    #chart{{height:620px}}
    table{{border-collapse:collapse;margin-top:18px;font-size:13px}}
    th,td{{border-bottom:1px solid #e5e7eb;padding:8px 12px;text-align:right}}
    th:first-child,td:first-child{{text-align:left}}
  </style>
</head>
<body>
  <h1>{' / '.join(normalized_df.columns)} 标准化收益对比</h1>
  <div class="meta">Period: {period} · base=100 · providers: {', '.join(sorted(set(providers.values())))}</div>
  <div id="chart"></div>
  <table>
    <thead><tr><th>Symbol</th><th>Total Return</th><th>Ann. Vol</th><th>Max Drawdown</th><th>Last Close</th></tr></thead>
    <tbody>{metrics_rows}</tbody>
  </table>
  <script>
    const traces = {json.dumps(traces, ensure_ascii=False)};
    Plotly.newPlot("chart", traces, {{
      margin: {{l: 48, r: 24, t: 18, b: 42}},
      hovermode: "x unified",
      yaxis: {{title: "Normalized value (base=100)", gridcolor: "#eef2f7"}},
      xaxis: {{type: "date", gridcolor: "#eef2f7"}},
      legend: {{orientation: "h", y: 1.08}},
      paper_bgcolor: "#fff",
      plot_bgcolor: "#fff"
    }}, {{responsive: true, displaylogo: false}});
  </script>
</body>
</html>"""
    out_file.write_text(html_doc, encoding="utf-8")
    write_artifact_metadata(artifact, {
        "kind": "stock_comparison_chart",
        "status": "complete",
        "symbols": list(normalized_df.columns),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "data": {"period": period, "provider_chain": sorted(set(providers.values())), "errors": errors},
        "metrics": metrics,
        "outputs": {"html": str(out_file)},
    })
    write_artifact_raw_data(artifact, {
        "symbols": list(normalized_df.columns),
        "prices": raw_rows,
        "normalized": {
            symbol: [
                {"date": date, "value": value}
                for date, value in zip(dates, traces[idx]["y"])
            ]
            for idx, symbol in enumerate(normalized_df.columns)
        },
    })
    return {
        "success": True,
        "chart_path": str(out_file),
        "symbols": list(normalized_df.columns),
        "metrics": metrics,
        "provider": ", ".join(sorted(set(providers.values()))),
        "errors": errors,
    }


def handle_stock_chart_analysis_direct(symbol: str, period: str = "1y") -> dict:
    """
    生成专业股票分析图表 (HTML)，并自审数据质量。
    四面板：K线+均线+布林带 / 成交量 / RSI(14) / MACD柱状图
    A股（.SS/.SZ）自动切换红涨绿跌配色。
    """
    import html as _html
    import math
    try:
        import pandas as _pd
    except Exception as exc:
        return {"success": False, "error": f"缺少依赖: {exc}"}
    try:
        import yfinance as _yf
    except Exception:
        _yf = None

    _PERIOD_MAP = {
        "1m": "1mo", "3m": "3mo", "6m": "6mo",
        "1y": "1y",  "2y": "2y",  "3y": "3y", "5y": "5y",
        "ytd": "ytd", "max": "max",
    }
    period = _PERIOD_MAP.get(period.lower(), period)
    symbol = _normalise_chart_symbol(symbol)

    # A股判断（影响K线颜色惯例）
    is_ashare = bool(_ashare_plain_symbol(symbol))

    # ── 获取历史数据 ────────────────────────────────────────────────────────────
    hist   = None
    provider = "Yahoo Finance"
    provider_currency = None
    err1   = ""
    mdc_err = ""
    ak_err = ""
    chart_err = ""
    ticker = None

    if is_ashare:
        hist, provider_currency, mdc_provider, mdc_err = _fetch_mdc_history_frame(symbol, period, "1d")
        if hist is not None and not hist.empty:
            provider = mdc_provider or "market_data_client"

    if is_ashare and (hist is None or hist.empty):
        hist, provider_currency, ak_err = _fetch_akshare_history_frame(symbol, period)
        if hist is not None and not hist.empty:
            provider = "akshare"

    if (hist is None or hist.empty) and _yf is not None:
        try:
            ticker = _yf.Ticker(symbol)
            hist = ticker.history(period=period, interval="1d", auto_adjust=True)
            hist = _normalise_history_frame(hist)
            if hist is not None and not hist.empty:
                provider = "Yahoo Finance"
        except Exception as exc:
            err1 = str(exc)

    if hist is None or hist.empty:
        hist, provider_currency, chart_err = _fetch_yahoo_chart_frame(symbol, period, "1d")
        if hist is None or hist.empty:
            return {
                "success": False,
                "error": (
                    f"无法获取 {symbol} 数据: "
                    f"MarketDataClient={mdc_err or 'skipped'}; "
                    f"akshare={ak_err or 'skipped'}; "
                    f"yfinance={err1 or ('missing' if _yf is None else 'empty')}; "
                    f"YahooChart={chart_err or 'empty'}"
                ),
            }
        provider = "Yahoo Chart API"

    if hist is None or hist.empty:
        return {"success": False, "error": f"无法获取 {symbol} 历史数据"}

    hist = hist.dropna(subset=["Close"]).copy()

    # ── 指标计算 ────────────────────────────────────────────────────────────────
    hist["MA20"]    = hist["Close"].rolling(20).mean()
    hist["MA60"]    = hist["Close"].rolling(60).mean()
    _std20          = hist["Close"].rolling(20).std()
    hist["BB_UP"]   = hist["MA20"] + 2 * _std20
    hist["BB_LO"]   = hist["MA20"] - 2 * _std20
    _delta          = hist["Close"].diff()
    _gain           = _delta.clip(lower=0).rolling(14).mean()
    _loss           = (-_delta.clip(upper=0)).rolling(14).mean()
    hist["RSI14"]   = 100 - (100 / (1 + _gain / _loss.replace(0, _pd.NA)))
    _ema12          = hist["Close"].ewm(span=12, adjust=False).mean()
    _ema26          = hist["Close"].ewm(span=26, adjust=False).mean()
    hist["MACD"]    = _ema12 - _ema26
    hist["MACD_SIG"]= hist["MACD"].ewm(span=9, adjust=False).mean()
    hist["MACD_HIS"]= hist["MACD"] - hist["MACD_SIG"]

    last       = hist.iloc[-1]
    last_close = float(last["Close"])
    high_52w   = float(hist["High"].max()) if "High" in hist.columns else float(hist["Close"].max())
    low_52w    = float(hist["Low"].min())  if "Low"  in hist.columns else float(hist["Close"].min())
    ma20       = float(last["MA20"])     if _pd.notna(last["MA20"])     else None
    ma60       = float(last["MA60"])     if _pd.notna(last["MA60"])     else None
    bb_up      = float(last["BB_UP"])    if _pd.notna(last["BB_UP"])    else None
    bb_lo      = float(last["BB_LO"])    if _pd.notna(last["BB_LO"])    else None
    rsi14      = float(last["RSI14"])    if _pd.notna(last["RSI14"])    else None
    macd_v     = float(last["MACD"])     if _pd.notna(last["MACD"])     else None
    macd_s_val = float(last["MACD_SIG"]) if _pd.notna(last["MACD_SIG"]) else None

    # ── 支撑/阻力（10棒摆动点，去重后取最近3个）──────────────────────────────
    _sup_lvls: list[float] = []
    _res_lvls: list[float] = []
    if "High" in hist.columns and "Low" in hist.columns and len(hist) >= 20:
        _win = 10  # 10棒窗口过滤噪音，比5棒更稳健
        _h   = hist["High"].values
        _l   = hist["Low"].values
        for i in range(_win, len(hist) - _win):
            if float(_h[i]) == float(max(_h[i - _win:i + _win + 1])):
                _res_lvls.append(float(_h[i]))
            if float(_l[i]) == float(min(_l[i - _win:i + _win + 1])):
                _sup_lvls.append(float(_l[i]))
    # MA 作为动态支撑/阻力
    if ma20:
        (_sup_lvls if last_close > ma20 else _res_lvls).append(ma20)
    if ma60:
        (_sup_lvls if last_close > ma60 else _res_lvls).append(ma60)
    # 布林带
    if bb_lo:
        _sup_lvls.append(bb_lo)
    if bb_up:
        _res_lvls.append(bb_up)
    sup3 = sorted(set(round(v, 2) for v in _sup_lvls if v < last_close), reverse=True)[:3]
    res3 = sorted(set(round(v, 2) for v in _res_lvls if v > last_close))[:3]

    # ── 基本面 ──────────────────────────────────────────────────────────────────
    info = {}
    if ticker is None and _yf is not None:
        try:
            ticker = _yf.Ticker(symbol)
        except Exception:
            ticker = None
    try:
        info = ticker.get_info() or {} if ticker is not None else {}
    except Exception:
        pass
    name       = info.get("longName") or info.get("shortName") or symbol
    currency   = info.get("currency") or provider_currency or ("CNY" if is_ashare else "USD")
    pe         = info.get("trailingPE")
    pb         = info.get("priceToBook")
    roe        = info.get("returnOnEquity")
    # A fraction, as _fv(div_yield, 100, pct=True) below expects. The fallback
    # was dividendYield, which yfinance gives already in percent: 100x too big.
    trailing   = info.get("trailingAnnualDividendYield")
    rate       = info.get("dividendRate")
    price_now  = info.get("regularMarketPrice") or info.get("currentPrice")
    div_yield  = trailing or (float(rate) / float(price_now) if rate and price_now else None)
    market_cap = info.get("marketCap")

    def _fv(v, mult=1.0, pct=False):
        if v is None or (isinstance(v, float) and (math.isnan(v) or v == 0)):
            return "—"
        x = float(v) * mult
        return f"{x:.2f}%" if pct else f"{x:,.2f}"

    def _mcap(v):
        if not v:
            return "—"
        if v >= 1e12: return f"{v/1e12:.2f}T"
        if v >= 1e9:  return f"{v/1e9:.1f}B"
        if v >= 1e8:  return f"{v/1e8:.1f}亿"
        return f"{v:,.0f}"

    trend    = ("偏多" if ma20 and ma60 and last_close > ma20 > ma60 else
                "偏空" if ma20 and ma60 and last_close < ma20 < ma60 else "震荡")
    rsi_view = ("超买" if rsi14 and rsi14 >= 70 else "超卖" if rsi14 and rsi14 <= 30 else "中性")
    momentum = "MACD↑多" if macd_v and macd_s_val and macd_v > macd_s_val else "MACD↓弱"

    # ── K线颜色惯例 ────────────────────────────────────────────────────────────
    # 中国A股：红涨绿跌  |  美股/港股/加密：绿涨红跌
    if is_ashare:
        inc_color = "#dc2626"   # 红 = 涨
        dec_color = "#16a34a"   # 绿 = 跌
        vol_up_c  = "rgba(220,38,38,0.75)"
        vol_dn_c  = "rgba(22,163,74,0.75)"
        macd_pos  = "rgba(220,38,38,0.75)"
        macd_neg  = "rgba(22,163,74,0.75)"
    else:
        inc_color = "#16a34a"   # 绿 = 涨
        dec_color = "#dc2626"   # 红 = 跌
        vol_up_c  = "rgba(22,163,74,0.75)"
        vol_dn_c  = "rgba(220,38,38,0.75)"
        macd_pos  = "rgba(22,163,74,0.75)"
        macd_neg  = "rgba(220,38,38,0.75)"

    # ── 序列化 ──────────────────────────────────────────────────────────────────
    def _ser(col):
        values = []
        for v in hist[col]:
            try:
                if v is None:
                    values.append(None)
                    continue
                if hasattr(v, "__class__") and str(v) in {"<NA>", "NaT"}:
                    values.append(None)
                    continue
                fv = float(v)
                if math.isnan(fv):
                    values.append(None)
                else:
                    values.append(round(fv, 4))
            except Exception:
                values.append(None)
        return json.dumps(values)

    def _ser_int(col):
        if col not in hist.columns:
            return "[]"
        return json.dumps([None if (v is None or (isinstance(v, float) and math.isnan(v)))
                           else int(float(v)) for v in hist[col]])

    x_dates = json.dumps([idx.strftime("%Y-%m-%d") for idx in hist.index])
    open_s  = _ser("Open")  if "Open"  in hist.columns else _ser("Close")
    high_s  = _ser("High")  if "High"  in hist.columns else _ser("Close")
    low_s   = _ser("Low")   if "Low"   in hist.columns else _ser("Close")
    close_s = _ser("Close")
    vol_s   = _ser_int("Volume")
    ma20_s  = _ser("MA20")
    ma60_s  = _ser("MA60")
    bbup_s  = _ser("BB_UP")
    bblo_s  = _ser("BB_LO")
    rsi_s   = _ser("RSI14")
    macd_s2 = _ser("MACD")
    macds_s = _ser("MACD_SIG")
    macdh_s = _ser("MACD_HIS")

    # 成交量/MACD颜色（JSON 串）
    closes = hist["Close"].values
    vol_colors = json.dumps([
        vol_up_c if (i > 0 and not math.isnan(float(closes[i])) and
                     float(closes[i]) >= float(closes[i-1])) else vol_dn_c
        for i in range(len(closes))
    ])
    macd_colors = json.dumps([
        macd_pos if (v is not None and not math.isnan(float(v)) and float(v) >= 0) else macd_neg
        for v in hist["MACD_HIS"].values
    ])

    # 支撑/阻力 shapes（只绘制在价格面板 y轴内）
    sup_shapes = "".join(
        f'{{type:"line",xref:"paper",x0:0,x1:1,yref:"y",y0:{v},y1:{v},'
        f'line:{{color:"#22c55e",width:1.2,dash:"dot"}}}},'
        for v in sup3
    )
    res_shapes = "".join(
        f'{{type:"line",xref:"paper",x0:0,x1:1,yref:"y",y0:{v},y1:{v},'
        f'line:{{color:"#f97316",width:1.2,dash:"dot"}}}},'
        for v in res3
    )

    # ── 自审 ────────────────────────────────────────────────────────────────────
    review_issues = _review_chart(
        symbol, last_close, high_52w, low_52w,
        rsi14, ma20, ma60, bb_up, bb_lo, sup3, res3, len(hist)
    )

    # ── 生成 HTML ────────────────────────────────────────────────────────────────
    safe_sym  = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
    from aria_code.artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
    _artifact = create_user_artifact("stock-charts", symbol, f"{safe_sym}_chart", ".html")
    out_file  = _artifact.path

    # 配色标注
    color_note = "红涨绿跌（A股惯例）" if is_ashare else "绿涨红跌（国际惯例）"
    rsi_color  = "red" if rsi14 and rsi14 >= 70 else ("green" if rsi14 and rsi14 <= 30 else "")
    cg = lambda ok: "green" if ok else "red"

    cards_html = f"""
  <div class="card"><div class="lbl">最新收盘</div><div class="val">{currency} {last_close:,.2f}</div></div>
  <div class="card"><div class="lbl">52周区间</div><div class="val small">{low_52w:,.2f} — {high_52w:,.2f}</div></div>
  <div class="card"><div class="lbl">MA20</div><div class="val {cg(ma20 and last_close>ma20)}">{f'{ma20:,.2f}' if ma20 else '—'}</div></div>
  <div class="card"><div class="lbl">MA60</div><div class="val {cg(ma60 and last_close>ma60)}">{f'{ma60:,.2f}' if ma60 else '—'}</div></div>
  <div class="card"><div class="lbl">布林上/下轨</div><div class="val small">{f'{bb_up:,.2f}' if bb_up else '—'} / {f'{bb_lo:,.2f}' if bb_lo else '—'}</div></div>
  <div class="card"><div class="lbl">RSI(14)</div><div class="val {rsi_color}">{f'{rsi14:.1f}' if rsi14 else '—'} {rsi_view}</div></div>
  <div class="card"><div class="lbl">趋势 / 动能</div><div class="val">{trend} · {momentum}</div></div>
  <div class="card"><div class="lbl">P/E</div><div class="val">{_fv(pe)}</div></div>
  <div class="card"><div class="lbl">P/B</div><div class="val">{_fv(pb)}</div></div>
  <div class="card"><div class="lbl">ROE</div><div class="val">{_fv(roe, 100, pct=True)}</div></div>
  <div class="card"><div class="lbl">股息率</div><div class="val">{_fv(div_yield, 100, pct=True)}</div></div>
  <div class="card"><div class="lbl">市值</div><div class="val">{_mcap(market_cap)}</div></div>"""
    if sup3:
        cards_html += f'\n  <div class="card sup"><div class="lbl">支撑位</div><div class="val small">{" / ".join(str(v) for v in sup3)}</div></div>'
    if res3:
        cards_html += f'\n  <div class="card res"><div class="lbl">阻力位</div><div class="val small">{" / ".join(str(v) for v in res3)}</div></div>'

    warn_html = ""
    if review_issues:
        warn_items = "".join(f"<li>{_html.escape(iss)}</li>" for iss in review_issues)
        warn_html = f'<div class="warn"><strong>⚠ 图表自审发现 {len(review_issues)} 个问题：</strong><ul>{warn_items}</ul></div>'

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(name)} ({_html.escape(symbol)}) 分析图表</title>
{plotly_script_tag()}
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;color:#17202a}}
  main{{max-width:1320px;margin:0 auto;padding:18px 20px}}
  h1{{margin:0 0 2px;font-size:21px;font-weight:700}}
  .meta{{color:#6b7280;font-size:11.5px;margin-bottom:12px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:7px;margin-bottom:12px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:9px 11px}}
  .card.sup{{border-left:3px solid #22c55e}} .card.res{{border-left:3px solid #f97316}}
  .lbl{{color:#6b7280;font-size:10.5px;font-weight:500;text-transform:uppercase;letter-spacing:.3px}}
  .val{{font-size:14px;font-weight:700;margin-top:2px}} .val.small{{font-size:11.5px;font-weight:600}}
  .green{{color:#16a34a}} .red{{color:#dc2626}}
  #chart{{background:#fff;border:1px solid #e5e7eb;border-radius:10px}}
  .footer{{color:#9ca3af;font-size:11px;margin-top:8px;text-align:center}}
  .warn{{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:12px;color:#92400e}}
  .warn ul{{margin:4px 0 0 16px;padding:0}}
</style>
</head>
<body>
<main>
<h1>{_html.escape(name)} <span style="font-weight:400;color:#6b7280">({_html.escape(symbol)})</span></h1>
<p class="meta">生成: {datetime.now():%Y-%m-%d %H:%M}  ·  数据: Yahoo Finance  ·  周期: {period}  ·  配色: {color_note}  ·  Aria Code</p>
{warn_html}<div class="cards">{cards_html}
</div>
<div id="chart"></div>
<p class="footer">⚠ 仅供参考，不构成投资建议。&nbsp; 绿虚线=支撑位 &nbsp;|&nbsp; 橙虚线=阻力位</p>
</main>
<script>
const x      = {x_dates};
const op     = {open_s};
const hi     = {high_s};
const lo     = {low_s};
const cl     = {close_s};
const vol    = {vol_s};
const volClr = {vol_colors};
const ma20   = {ma20_s};
const ma60   = {ma60_s};
const bbUp   = {bbup_s};
const bbLo   = {bblo_s};
const rsi    = {rsi_s};
const macd   = {macd_s2};
const macdSg = {macds_s};
const macdHi = {macdh_s};
const macdHiClr = {macd_colors};

const traces = [
  /* K线 */
  {{x,open:op,high:hi,low:lo,close:cl,type:"candlestick",name:"K线",
    increasing:{{line:{{color:"{inc_color}"}},fillcolor:"{inc_color}"}},
    decreasing:{{line:{{color:"{dec_color}"}},fillcolor:"{dec_color}"}},
    yaxis:"y",whiskerwidth:0.3}},
  /* 布林上轨 */
  {{x,y:bbUp,type:"scatter",mode:"lines",name:"BB上轨",
    line:{{color:"rgba(99,102,241,0.6)",width:1}},yaxis:"y"}},
  /* 布林下轨（填充，hover隐藏避免重复） */
  {{x,y:bbLo,type:"scatter",mode:"lines",name:"BB下轨",
    line:{{color:"rgba(99,102,241,0.6)",width:1}},
    fill:"tonexty",fillcolor:"rgba(99,102,241,0.07)",
    showlegend:false,hoverinfo:"skip",yaxis:"y"}},
  /* MA20 */
  {{x,y:ma20,type:"scatter",mode:"lines",name:"MA20",
    line:{{color:"#f59e0b",width:1.5}},yaxis:"y"}},
  /* MA60 */
  {{x,y:ma60,type:"scatter",mode:"lines",name:"MA60",
    line:{{color:"#ef4444",width:1.5,dash:"dot"}},yaxis:"y"}},
  /* 成交量 */
  {{x,y:vol,type:"bar",name:"成交量",marker:{{color:volClr}},yaxis:"y2",showlegend:false}},
  /* RSI */
  {{x,y:rsi,type:"scatter",mode:"lines",name:"RSI(14)",
    line:{{color:"#8b5cf6",width:1.5}},yaxis:"y3"}},
  /* MACD 柱 */
  {{x,y:macdHi,type:"bar",name:"MACD柱",marker:{{color:macdHiClr}},yaxis:"y4",showlegend:false}},
  /* MACD 线 */
  {{x,y:macd,type:"scatter",mode:"lines",name:"MACD",
    line:{{color:"#2563eb",width:1.5}},yaxis:"y4"}},
  /* Signal 线 */
  {{x,y:macdSg,type:"scatter",mode:"lines",name:"Signal",
    line:{{color:"#f59e0b",width:1.5,dash:"dot"}},yaxis:"y4"}}
];

const layout = {{
  height:820,
  /* 右边距加大：确保Y轴完整数字不被截断 */
  margin:{{l:8,r:80,t:14,b:28}},
  paper_bgcolor:"#fff",plot_bgcolor:"#fff",
  hovermode:"x unified",
  legend:{{orientation:"h",y:1.025,x:0,font:{{size:11}},bgcolor:"rgba(255,255,255,0.8)"}},
  xaxis:{{domain:[0,1],type:"date",rangeslider:{{visible:false}},
          gridcolor:"#f1f5f9",showgrid:true}},
  /* 面板分配：价格60% / 成交量11% / RSI10% / MACD11% */
  yaxis: {{domain:[0.25,1],   side:"right",gridcolor:"#f1f5f9",
           title:{{text:"价格 ({currency})",font:{{size:11}}}},tickfont:{{size:11}}}},
  yaxis2:{{domain:[0.145,0.22],side:"right",gridcolor:"#f1f5f9",
           showticklabels:false,title:""}},
  yaxis3:{{domain:[0.075,0.135],side:"right",range:[0,100],gridcolor:"#f1f5f9",
           title:{{text:"RSI",font:{{size:11}}}},tickfont:{{size:10}}}},
  yaxis4:{{domain:[0,0.065],  side:"right",gridcolor:"#f1f5f9",
           title:{{text:"MACD",font:{{size:11}}}},tickfont:{{size:10}}}},
  shapes:[
    {{type:"line",xref:"paper",x0:0,x1:1,yref:"y3",y0:70,y1:70,
      line:{{color:"rgba(220,38,38,0.6)",width:1,dash:"dot"}}}},
    {{type:"line",xref:"paper",x0:0,x1:1,yref:"y3",y0:30,y1:30,
      line:{{color:"rgba(22,163,74,0.6)",width:1,dash:"dot"}}}},
    {{type:"line",xref:"paper",x0:0,x1:1,yref:"y4",y0:0,y1:0,
      line:{{color:"#94a3b8",width:0.8}}}},
    {sup_shapes}{res_shapes}
  ]
}};
Plotly.newPlot("chart",traces,layout,{{responsive:true,displaylogo:false,
  modeBarButtonsToRemove:["autoScale2d","lasso2d","select2d"]}});
</script>
</body>
</html>"""

    out_file.write_text(html_doc, encoding="utf-8")

    png_path, png_error = _write_ta_png_artifact(
        _artifact, hist, symbol, name, currency, is_ashare,
        ma20, ma60, rsi14, macd_v, macd_s_val,
    )

    _raw_prices = []
    try:
        _raw_prices = hist.reset_index().tail(370).to_dict(orient="records")
    except Exception:
        _raw_prices = []
    write_artifact_metadata(_artifact, {
        "kind": "stock_chart",
        "status": "complete",
        "symbol": symbol,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "data": {
            "provider_chain": [provider],
            "rows": int(len(hist)),
            "panels": ["candlestick", "bollinger", "ma20", "ma60", "volume", "rsi14", "macd"],
            "color_convention": "ashare_red_up" if is_ashare else "western_green_up",
        },
        "review": {"issues": review_issues, "passed": len(review_issues) == 0},
        "outputs": {
            "html": str(out_file),
            "png": png_path,
            "png_error": png_error,
        },
        "metrics": {
            "last_close": last_close, "high_52w": high_52w, "low_52w": low_52w,
            "trend": trend, "rsi14": rsi14, "momentum": momentum,
            "support": sup3, "resistance": res3,
        },
    })
    write_artifact_raw_data(_artifact, {
        "symbol": symbol, "provider": provider, "info": info, "prices": _raw_prices,
    })
    return {
        "success":       True,
        "chart_path":    str(out_file),
        "png_path":      png_path,
        "png_error":     png_error,
        "response":      f"图表已生成：{out_file.name}",
        "symbol":        symbol,
        "name":          name,
        "last_close":    last_close,
        "trend":         trend,
        "rsi":           rsi14,
        "momentum":      momentum,
        "support":       sup3,
        "resistance":    res3,
        "review_issues": review_issues,
        "provider":      provider,
    }


def handle_stock_chart_analysis(
    message: str,
    *,
    is_chart_request: Callable[[str], bool],
    extract_symbol: Callable[[str], str],
) -> dict:
    """Deterministic path for stock analysis + chart requests.

    This avoids weak local models writing fake scripts or leaking pseudo tool
    calls. It fetches historical data, computes common indicators, writes a
    standalone HTML chart, and returns a concise Markdown analysis.
    """
    if not is_chart_request(message):
        return {"success": False, "error": "not_stock_chart_analysis"}

    symbol = extract_symbol(message) or "AAPL"
    period = "1y"
    interval = "1d"

    try:
        import html as _html
        import pandas as _pd
    except Exception as exc:
        return {
            "success": False,
            "error": f"缺少图表分析依赖：{exc}",
            "response": "当前环境缺少 `pandas`，无法生成股票图表。",
        }
    try:
        import yfinance as _yf
    except Exception:
        _yf = None

    symbol = _normalise_chart_symbol(symbol)
    provider = "Yahoo Finance"
    provider_currency = None
    ticker = None
    hist = None
    yahoo_error = ""
    chart_error = ""
    mdc_error = ""
    ak_error = ""

    if _ashare_plain_symbol(symbol):
        hist, provider_currency, mdc_provider, mdc_error = _fetch_mdc_history_frame(symbol, period, interval)
        if hist is not None and not hist.empty:
            provider = mdc_provider or "market_data_client"

    if _ashare_plain_symbol(symbol) and (hist is None or hist.empty):
        hist, provider_currency, ak_error = _fetch_akshare_history_frame(symbol, period)
        if hist is not None and not hist.empty:
            provider = "akshare"

    if (hist is None or hist.empty) and _yf is not None:
        try:
            ticker = _yf.Ticker(symbol)
            hist = ticker.history(period=period, interval=interval, auto_adjust=False)
            hist = _normalise_history_frame(hist)
            if hist is not None and not hist.empty:
                provider = "Yahoo Finance"
        except Exception as exc:
            hist = None
            yahoo_error = str(exc)
    elif _yf is None:
        yahoo_error = "yfinance missing"

    if hist is None or hist.empty:
        hist, provider_currency, chart_error = _fetch_yahoo_chart_frame(symbol, period, interval)
        if hist is not None and not hist.empty:
            provider = "Yahoo Chart API"

    if hist is None or hist.empty:
        try:
            stooq_symbol = symbol.lower()
            if "." not in stooq_symbol:
                stooq_symbol = f"{stooq_symbol}.us"
            url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
            hist = _pd.read_csv(url)
            if hist is not None and not hist.empty:
                hist["Date"] = _pd.to_datetime(hist["Date"])
                hist = hist.set_index("Date").sort_index().tail(260)
                hist = _normalise_history_frame(hist)
                provider = "Stooq"
        except Exception as exc:
            return {
                "success": False,
                "error": (
                    f"获取 {symbol} 历史行情失败："
                    f"MarketDataClient={mdc_error or 'skipped'}; "
                    f"akshare={ak_error or 'skipped'}; "
                    f"Yahoo={yahoo_error or 'empty'}; "
                    f"YahooChart={chart_error or 'empty'}; "
                    f"Stooq={exc}"
                ),
                "response": f"无法获取 {symbol} 历史行情，图表未生成。请稍后重试，或检查网络/数据源访问。",
            }

    if hist is None or hist.empty or "Close" not in hist.columns:
        return {
            "success": False,
            "error": (
                f"{symbol} 历史行情为空："
                f"MarketDataClient={mdc_error or 'skipped'}; "
                f"akshare={ak_error or 'skipped'}; "
                f"Yahoo={yahoo_error or 'empty'}; "
                f"YahooChart={chart_error or 'empty'}"
            ),
            "response": f"没有拿到 {symbol} 的可用历史行情，图表未生成。请稍后重试，或检查网络/数据源访问。",
        }

    hist = hist.dropna(subset=["Close"]).copy()
    hist["MA20"] = hist["Close"].rolling(20).mean()
    hist["MA50"] = hist["Close"].rolling(50).mean()
    hist["MA200"] = hist["Close"].rolling(200).mean()
    delta = hist["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, _pd.NA)
    hist["RSI14"] = 100 - (100 / (1 + rs))
    ema12 = hist["Close"].ewm(span=12, adjust=False).mean()
    ema26 = hist["Close"].ewm(span=26, adjust=False).mean()
    hist["MACD"] = ema12 - ema26
    hist["MACD_SIGNAL"] = hist["MACD"].ewm(span=9, adjust=False).mean()

    last = hist.iloc[-1]
    first_close = hist["Close"].iloc[0]
    last_close = float(last["Close"])
    ytd_like_return = (last_close / float(first_close) - 1) * 100 if first_close else 0
    ma20 = float(last["MA20"]) if _pd.notna(last["MA20"]) else None
    ma50 = float(last["MA50"]) if _pd.notna(last["MA50"]) else None
    ma200 = float(last["MA200"]) if _pd.notna(last["MA200"]) else None
    rsi14 = float(last["RSI14"]) if _pd.notna(last["RSI14"]) else None
    macd = float(last["MACD"]) if _pd.notna(last["MACD"]) else None
    macd_sig = float(last["MACD_SIGNAL"]) if _pd.notna(last["MACD_SIGNAL"]) else None
    high_52w = float(hist["High"].max()) if "High" in hist else float(hist["Close"].max())
    low_52w = float(hist["Low"].min()) if "Low" in hist else float(hist["Close"].min())

    info = {}
    try:
        if ticker is None and _yf is not None:
            ticker = _yf.Ticker(symbol)
        info = ticker.get_info() or {} if ticker is not None else {}
    except Exception:
        info = {}
    name = info.get("longName") or info.get("shortName") or symbol
    pe = info.get("trailingPE")
    market_cap = info.get("marketCap")
    currency = info.get("currency") or provider_currency or ("CNY" if _ashare_plain_symbol(symbol) else "USD")

    if ma20 and ma50 and last_close > ma20 > ma50:
        trend = "偏多"
    elif ma20 and ma50 and last_close < ma20 < ma50:
        trend = "偏空"
    else:
        trend = "震荡/中性"
    momentum = "MACD偏多" if macd is not None and macd_sig is not None and macd > macd_sig else "MACD偏弱"
    rsi_view = "超买" if rsi14 is not None and rsi14 >= 70 else ("超卖" if rsi14 is not None and rsi14 <= 30 else "中性")

    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
    from aria_code.artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
    _artifact = create_user_artifact("stock-charts", symbol, f"{safe_symbol}_analysis_chart", ".html")
    out_file = _artifact.path

    x = [idx.strftime("%Y-%m-%d") for idx in hist.index]
    close = [None if _pd.isna(v) else round(float(v), 4) for v in hist["Close"]]
    volume = [None if _pd.isna(v) else int(float(v)) for v in hist.get("Volume", _pd.Series(index=hist.index, dtype=float))]
    ma20_arr = [None if _pd.isna(v) else round(float(v), 4) for v in hist["MA20"]]
    ma50_arr = [None if _pd.isna(v) else round(float(v), 4) for v in hist["MA50"]]
    rsi_arr = [None if _pd.isna(v) else round(float(v), 4) for v in hist["RSI14"]]

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html.escape(symbol)} 股票分析图表</title>
  {plotly_script_tag()}
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #17202a; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 6px; font-size: 28px; }}
    .meta {{ color: #667085; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 18px 0; }}
    .metric {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; }}
    .label {{ color: #667085; font-size: 12px; }}
    .value {{ font-size: 18px; font-weight: 650; margin-top: 4px; }}
    #chart {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; }}
    .note {{ color: #667085; font-size: 13px; margin-top: 14px; }}
  </style>
</head>
<body>
<main>
  <h1>{_html.escape(name)} ({_html.escape(symbol)})</h1>
  <div class="meta">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 数据：{_html.escape(provider)} · 周期：{period}</div>
  <section class="grid">
    <div class="metric"><div class="label">最新收盘</div><div class="value">{currency} {_fmt_num(last_close)}</div></div>
    <div class="metric"><div class="label">近一年区间</div><div class="value">{_fmt_num(low_52w)} - {_fmt_num(high_52w)}</div></div>
    <div class="metric"><div class="label">MA20 / MA50</div><div class="value">{_fmt_num(ma20)} / {_fmt_num(ma50)}</div></div>
    <div class="metric"><div class="label">RSI14</div><div class="value">{_fmt_num(rsi14)}</div></div>
    <div class="metric"><div class="label">P/E</div><div class="value">{_fmt_num(pe)}</div></div>
    <div class="metric"><div class="label">成交量</div><div class="value">{_fmt_int(last.get("Volume"))}</div></div>
  </section>
  <div id="chart"></div>
  <p class="note">图表包含收盘价、MA20、MA50、成交量和 RSI14。该文件为本地 HTML，可直接在浏览器打开。</p>
</main>
<script>
const x = {json.dumps(x)};
const close = {json.dumps(close)};
const volume = {json.dumps(volume)};
const ma20 = {json.dumps(ma20_arr)};
const ma50 = {json.dumps(ma50_arr)};
const rsi = {json.dumps(rsi_arr)};
const data = [
  {{x, y: close, type: "scatter", mode: "lines", name: "Close", line: {{color: "#2563eb", width: 2}}, yaxis: "y"}},
  {{x, y: ma20, type: "scatter", mode: "lines", name: "MA20", line: {{color: "#f59e0b", width: 1.5}}, yaxis: "y"}},
  {{x, y: ma50, type: "scatter", mode: "lines", name: "MA50", line: {{color: "#10b981", width: 1.5}}, yaxis: "y"}},
  {{x, y: volume, type: "bar", name: "Volume", marker: {{color: "rgba(100,116,139,0.35)"}}, yaxis: "y2"}},
  {{x, y: rsi, type: "scatter", mode: "lines", name: "RSI14", line: {{color: "#dc2626", width: 1.5}}, yaxis: "y3"}}
];
const layout = {{
  height: 720,
  margin: {{l: 62, r: 30, t: 28, b: 42}},
  paper_bgcolor: "#fff",
  plot_bgcolor: "#fff",
  hovermode: "x unified",
  legend: {{orientation: "h", y: 1.04}},
  xaxis: {{domain: [0, 1], rangeslider: {{visible: false}}, gridcolor: "#eef2f7"}},
  yaxis: {{domain: [0.36, 1], title: "Price", gridcolor: "#eef2f7"}},
  yaxis2: {{domain: [0.18, 0.31], title: "Volume", gridcolor: "#eef2f7"}},
  yaxis3: {{domain: [0, 0.13], title: "RSI", range: [0, 100], gridcolor: "#eef2f7"}},
  shapes: [
    {{type: "line", xref: "paper", x0: 0, x1: 1, yref: "y3", y0: 70, y1: 70, line: {{color: "#ef4444", dash: "dot"}}}},
    {{type: "line", xref: "paper", x0: 0, x1: 1, yref: "y3", y0: 30, y1: 30, line: {{color: "#22c55e", dash: "dot"}}}}
  ]
}};
Plotly.newPlot("chart", data, layout, {{responsive: true, displaylogo: false}});
</script>
</body>
</html>
"""
    out_file.write_text(html_doc, encoding="utf-8")

    _raw_prices = []
    try:
        _raw_prices = hist.reset_index().tail(370).to_dict(orient="records")
    except Exception:
        _raw_prices = []
    write_artifact_metadata(_artifact, {
        "kind": "stock_chart_analysis",
        "status": "complete",
        "symbol": symbol,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "data": {
            "provider_chain": [provider],
            "rows": int(len(hist)),
            "missing_fields": [
                k for k, v in {
                    "ma20": ma20,
                    "ma50": ma50,
                    "ma200": ma200,
                    "rsi14": rsi14,
                    "macd": macd,
                    "macd_signal": macd_sig,
                    "pe": pe,
                    "market_cap": market_cap,
                }.items()
                if v is None
            ],
        },
        "metrics": {
            "last_close": last_close,
            "trend": trend,
            "rsi14": rsi14,
            "momentum": momentum,
            "ytd_like_return": ytd_like_return,
        },
    })
    write_artifact_raw_data(_artifact, {
        "symbol": symbol,
        "provider": provider,
        "info": info,
        "prices": _raw_prices,
    })

    market_cap_text = "—"
    if market_cap:
        market_cap_text = f"{currency} {market_cap / 1e12:.2f}T" if market_cap >= 1e12 else f"{currency} {market_cap / 1e9:.1f}B"

    response = (
        f"## {name} ({symbol}) 股票分析\n\n"
        f"已生成图表：[{out_file.name}]({out_file})\n\n"
        f"| 指标 | 数值 |\n"
        f"| --- | --- |\n"
        f"| 最新收盘 | {currency} {_fmt_num(last_close)} |\n"
        f"| 近一年涨跌幅 | {ytd_like_return:+.2f}% |\n"
        f"| 近一年高/低 | {_fmt_num(high_52w)} / {_fmt_num(low_52w)} |\n"
        f"| MA20 / MA50 / MA200 | {_fmt_num(ma20)} / {_fmt_num(ma50)} / {_fmt_num(ma200)} |\n"
        f"| RSI14 | {_fmt_num(rsi14)}（{rsi_view}） |\n"
        f"| MACD | {_fmt_num(macd)} / signal {_fmt_num(macd_sig)}（{momentum}） |\n"
        f"| P/E / 市值 | {_fmt_num(pe)} / {market_cap_text} |\n\n"
        f"**结论**：当前技术结构为 **{trend}**。"
        f"RSI 处于{rsi_view}区间，{momentum}。"
        f"若价格能稳定站上 MA20 和 MA50，短线结构会更健康；若跌破 MA50 或放量下行，需要降低仓位和预期。\n\n"
        f"**风险**：该分析基于 {provider} 历史行情和常用技术指标，不构成投资建议；财报、产品周期、利率和大盘风险都会影响股价。"
    )
    return {
        "success": True,
        "response": response,
        "provider": "deterministic",
        "tools_used": ["stock_chart", provider, "html_chart"],
        "chart_path": str(out_file),
        "symbol": symbol,
    }
