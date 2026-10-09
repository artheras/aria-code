#!/usr/bin/env python3
"""
aria_daemon.py — Aria always-on background daemon.

Responsibilities:
  1. Price alert watchdog  — checks SQLite alerts every 30 s
  2. APScheduler cron jobs — morning brief, market scan, custom schedules
  3. Telegram bot          — bidirectional command channel
  4. Webhook job executor  — processes jobs queued by FastAPI /webhook/trigger
  5. APNs push delivery    — fires on alert trigger or scheduled job completion

Start manually:   python3 aria_daemon.py [--debug]
Install daemon:   python3 aria_daemon.py --install   (macOS LaunchAgent)
Uninstall:        python3 aria_daemon.py --uninstall

Config via env vars (or ~/.aria/.env):
  TELEGRAM_BOT_TOKEN        — Telegram bot token from @BotFather
  TELEGRAM_ALLOWED_IDS      — comma-separated chat IDs (e.g. "123456,789012")
  APNS_KEY_ID               — Apple Developer key ID (10-char string)
  APNS_TEAM_ID              — Apple Developer team ID
  APNS_BUNDLE_ID            — App bundle ID (default: com.arthera.app)
  APNS_SANDBOX              — "true" for sandbox / TestFlight (default: true)
  APNS_AUTH_KEY_P8          — .p8 key content, or place file at ~/.aria/apns.p8
  WEBHOOK_TOKEN             — Static token for /api/v1/webhook/trigger
  ARIA_WEBHOOK_HOST         — Daemon webhook host (default: 127.0.0.1)
  ARIA_WEBHOOK_PORT         — Daemon webhook port (default: 8765)
  (the FastAPI backend URL is the `api_url` config key, not an env var —
   set it with `/config set api_url=...`)
  ARIA_CODE_DIR             — Path to aria-code directory
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Logging ───────────────────────────────────────────────────────────────────

# Importing this module creates the log file and daemon.db and loads .env into
# os.environ (aria_feishu_bot relies on that), so the directory can be moved:
# the test suite points it at a scratch directory instead of the user's own.
_ARIA_DIR = Path(os.environ.get("ARIA_DAEMON_DIR") or (Path.home() / ".aria")).expanduser()
_LOG_DIR = _ARIA_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_DIR / "daemon.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("aria.daemon")

# ── Paths & config ────────────────────────────────────────────────────────────

_DB_PATH   = _ARIA_DIR / "daemon.db"
_PID_FILE  = _ARIA_DIR / "daemon.pid"
_ENV_FILE  = _ARIA_DIR / ".env"

_ARIA_CODE_DIR = Path(os.environ.get("ARIA_CODE_DIR", Path(__file__).parent))
if str(_ARIA_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_ARIA_CODE_DIR))


def _load_env() -> None:
    """Load ~/.aria/.env into os.environ if it exists."""
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()


# ── DB bootstrap ───────────────────────────────────────────────────────────────

def _init_db() -> None:
    _ARIA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS device_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                platform TEXT DEFAULT 'ios',
                user_id TEXT,
                bundle_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                condition TEXT NOT NULL,
                value REAL NOT NULL,
                message TEXT,
                notify_push INTEGER DEFAULT 1,
                notify_telegram INTEGER DEFAULT 1,
                notify_email TEXT,
                once INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                trigger_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                triggered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                name TEXT,
                cron_expr TEXT NOT NULL,
                command TEXT NOT NULL,
                symbols TEXT DEFAULT '[]',
                channels TEXT DEFAULT '["ios","telegram"]',
                language TEXT DEFAULT 'zh',
                user_id TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                last_run TEXT,
                next_run TEXT
            );
            CREATE TABLE IF NOT EXISTS webhook_jobs (
                id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                source TEXT DEFAULT 'external',
                status TEXT DEFAULT 'pending',
                result TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                started_at TEXT,
                done_at TEXT
            );
            CREATE TABLE IF NOT EXISTS push_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT,
                device_token TEXT,
                title TEXT,
                body TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        conn.commit()


_init_db()


# ── Price fetch (lightweight, no VPN proxy) ────────────────────────────────────

async def _fetch_price(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Fast price lookup. Returns (current_price, prev_close) for alert evaluation."""
    import math
    try:
        import yfinance as yf
        if symbol.isdigit() and len(symbol) == 6:
            yfn = symbol + (".SS" if symbol.startswith(("6", "5")) else ".SZ")
        else:
            yfn = symbol
        ticker = yf.Ticker(yfn)
        info = ticker.fast_info
        raw_price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        raw_prev  = getattr(info, "previous_close", None)
        # yfinance can return float('nan') — treat as None
        price      = float(raw_price)  if raw_price  is not None and not math.isnan(float(raw_price))  else None
        prev_close = float(raw_prev)   if raw_prev   is not None and not math.isnan(float(raw_prev))   else None
        return (price, prev_close)
    except Exception as exc:
        logger.debug("_fetch_price %s: %s", symbol, exc)
        return (None, None)


# ── Push notifications ────────────────────────────────────────────────────────

async def _push_alert(title: str, body: str, extra: Optional[dict] = None, alert_id: Optional[str] = None) -> None:
    """Try to push via APNs; log any failure but never raise."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "Arthera" / "apps" / "api" / "src"))
        from services.apns_service import push_to_all
        sent = await push_to_all(title, body, extra, alert_id)
        logger.info("APNs push: %d device(s) notified for alert_id=%s", sent, alert_id)
    except ImportError:
        logger.debug("apns_service not reachable — skipping push")


# ── Telegram push (one-shot, no polling) ─────────────────────────────────────

async def _telegram_push(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_IDS", "")
    if not token or not allowed_raw:
        return
    chat_ids = [int(x.strip()) for x in allowed_raw.split(",") if x.strip().isdigit()]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            for cid in chat_ids:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                )
    except Exception as exc:
        logger.warning("_telegram_push failed: %s", exc)


# ── Feishu push (webhook card) ────────────────────────────────────────────────

async def _feishu_push(title: str, body: str) -> None:
    """POST an interactive card to a Feishu group webhook (FEISHU_WEBHOOK_URL)."""
    url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not url:
        return
    color = "red"   if any(w in title for w in ("预警", "Alert", "熔断", "ERROR")) else \
            "green" if any(w in title for w in ("晨报", "完成", "Brief"))         else "blue"
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body[:2000]}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "Aria Daemon · " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")},
                ]},
            ],
        },
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=card)
            if resp.status_code >= 400:
                logger.warning("_feishu_push HTTP %s: %s", resp.status_code, resp.text[:120])
    except Exception as exc:
        logger.warning("_feishu_push failed: %s", exc)


# ── Alert watchdog ────────────────────────────────────────────────────────────

async def _alert_watchdog() -> None:
    """Check all active alerts against live prices every 30 seconds."""
    logger.info("Alert watchdog started")
    while True:
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE active=1"
                ).fetchall()
            alerts = [dict(r) for r in rows]

            # Group by symbol to minimise API calls
            by_symbol: dict[str, list[dict]] = {}
            for a in alerts:
                by_symbol.setdefault(a["symbol"], []).append(a)

            for symbol, sym_alerts in by_symbol.items():
                price, prev_close = await _fetch_price(symbol)
                if price is None:
                    continue
                for alert in sym_alerts:
                    await _check_alert(alert, price, prev_close)

        except Exception as exc:
            logger.error("Alert watchdog error: %s", exc)

        await asyncio.sleep(30)


async def _check_alert(alert: dict, price: float, prev_close: Optional[float] = None) -> None:
    cond = alert["condition"]
    val  = float(alert["value"])

    # pct_change 条件: prev_close=None 时跳过而非永远不触发
    if cond in ("pct_change_above", "pct_change_below") and (not prev_close or prev_close <= 0):
        logger.debug("Alert %s: prev_close unavailable, skipping pct_change check", alert["id"])
        return

    pct_chg = ((price - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else None
    fired = (
        (cond == "price_above"       and price > val) or
        (cond == "price_below"       and price < val) or
        (cond == "pct_change_above"  and pct_chg is not None and pct_chg > val) or
        (cond == "pct_change_below"  and pct_chg is not None and pct_chg < val)
    )
    if not fired:
        return

    symbol  = alert["symbol"]
    pct_str = f" ({pct_chg:+.2f}%)" if pct_chg is not None else ""
    message = alert.get("message") or f"{symbol} {cond.replace('_', ' ')} {val}"
    title   = "🔔 ARIA 价格预警"
    body    = f"{symbol} ¥{price:.2f}{pct_str} — {message}"

    logger.info("Alert triggered: %s | %s @ %.4f", alert["id"], symbol, price)

    # Update DB
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            "UPDATE alerts SET trigger_count=trigger_count+1, triggered_at=datetime('now')"
            + (", active=0" if alert.get("once", 1) else "")
            + " WHERE id=?",
            (alert["id"],),
        )
        conn.commit()

    extra = {"symbol": symbol, "price": price, "signal": "ALERT"}

    if alert.get("notify_push", 1):
        await _push_alert(title, body, extra, alert["id"])

    if alert.get("notify_telegram", 1):
        await _telegram_push(f"*{title}*\n{body}")
    await _feishu_push(title, body)
    _system_notify(title, body)

    # 触发后自动运行轻量分析（technical + risk），结果推送给用户
    asyncio.create_task(_auto_analyze_alert(symbol, price, pct_str))


async def _auto_analyze_alert(symbol: str, price: float, pct_str: str) -> None:
    """Run a lightweight technical+risk analysis after an alert fires and push the result."""
    try:
        sys.path.insert(0, str(_ARIA_CODE_DIR))
        from agents.team import AgentTeam
        team = AgentTeam(agent_names=["technical", "risk"])
        result = await asyncio.wait_for(team.run(symbol), timeout=45.0)
        signal  = result.signal or "N/A"
        conf    = f"{result.confidence:.0%}" if result.confidence else "?"
        points  = "\n".join(f"  • {p}" for p in (result.key_points or [])[:3])
        body = (
            f"🤖 *{symbol}* 预警后快速分析\n"
            f"现价 {price:.4f}{pct_str}\n\n"
            f"Signal: *{signal}*  置信度: {conf}\n"
            f"{points}"
        )
        await _telegram_push(body)
        await _feishu_push(f"🤖 {symbol} 预警分析", body.replace("*", "**"))
    except asyncio.TimeoutError:
        logger.warning("_auto_analyze_alert %s: timeout", symbol)
    except Exception as exc:
        logger.debug("_auto_analyze_alert %s: %s", symbol, exc)


# ── Telegram command handler ─────────────────────────────────────────────────

async def _telegram_command(cmd: str, args: str, chat_id: int) -> str:
    """Route Telegram bot commands to Aria functions."""
    args = args.strip()
    logger.info("Telegram cmd=/%s args=%s chat=%d", cmd, args[:40], chat_id)

    if cmd == "help":
        return (
            "*Aria 命令列表*\n\n"
            "`/price SYMBOL` — 实时报价\n"
            "`/report SYMBOL` — 深度分析研报\n"
            "`/brief` — 今日晨报\n"
            "`/screen` — 热门 A 股筛选\n"
            "`/alert SYMBOL cond value` — 添加价格预警\n"
            "    条件: price\\_above / price\\_below / pct\\_change\\_above\n"
            "`/alerts` — 查看预警列表\n"
            "`/status` — Daemon 运行状态\n"
            "\n直接发文字也可对话 Aria。"
        )

    if cmd in ("price", "p"):
        symbol = args.upper() or "SPY"
        price, _ = await _fetch_price(symbol)
        if price:
            return f"*{symbol}* 当前价格: `¥{price:.4f}`" if len(symbol) == 6 and symbol.isdigit() else f"*{symbol}* ${price:.4f}"
        return f"⚠️ 无法获取 {symbol} 价格（市场可能已关闭）"

    if cmd == "report":
        symbol = args.upper()
        if not symbol:
            return "用法: `/report AAPL` 或 `/report 600519`"
        return await _run_report(symbol)

    if cmd in ("brief", "morning", "briefing"):
        return await _run_morning_brief()

    if cmd == "screen":
        return await _run_screener()

    if cmd == "alert":
        return await _handle_alert_add(args, chat_id)

    if cmd == "alerts":
        return _list_alerts()

    if cmd == "status":
        return _daemon_status()

    if cmd == "chat":
        # Natural language fallback
        return await _run_chat(args)

    return f"未知命令 `/{cmd}`。发送 `/help` 查看命令列表。"


async def _run_report(symbol: str) -> str:
    """Generate a quick text summary report."""
    try:
        price, _pc = await _fetch_price(symbol)
        price_str = f"¥{price:.2f}" if price and len(symbol) == 6 and symbol.isdigit() else (f"${price:.2f}" if price else "N/A")

        # Try to import aria-code market data for indicators
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "market_data_client",
                _ARIA_CODE_DIR / "market_data_client.py",
            )
            mdc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mdc)
            client = mdc.MarketDataClient()
            quote = await asyncio.wait_for(client.quote(symbol), timeout=8.0)
            name  = quote.get("name", symbol)
            chg   = quote.get("change_pct", 0)
            vol   = quote.get("volume", 0)
            chg_str = f"{chg:+.2f}%" if chg else ""
        except Exception:
            name, chg_str, vol = symbol, "", 0

        lines = [
            f"*{name}* `{symbol}` 快速分析",
            "",
            f"💰 价格: `{price_str}` {chg_str}",
            f"📊 成交量: `{vol:,}`" if vol else "",
            "",
            f"⚠️ 深度研报请在 Terminal 使用 `/report {symbol}` 命令。",
            "此处为轻量版摘要。",
        ]
        return "\n".join(l for l in lines if l is not None)

    except Exception as exc:
        logger.error("_run_report %s: %s", symbol, exc)
        return f"⚠️ 分析 {symbol} 时出错: {exc}"


def _maybe_auto_execute(preview_result: dict) -> list[str]:
    """Opt-in controlled auto-execution. Default OFF → draft-only.

    Only executes when the broker config turns on ``automation.enabled`` AND every
    gate in brokers.automation passes (dry-run off, risk passed, value/rate limits).
    """
    broker_id = preview_result.get("broker_id") or ""
    preview_id = preview_result.get("preview_id") or ""
    if not (broker_id and preview_id):
        return ["Webhook 只生成预览，不会自动执行实盘/仿盘订单。"]
    try:
        from brokers.config import get_broker_config
        from brokers.automation import AutoExecutePolicy, run_auto_execute
        from brokers.registry import get_registry

        policy = AutoExecutePolicy.from_config(get_broker_config(broker_id) or {})
        if not policy.enabled:
            return ["Webhook 只生成预览，不会自动执行（automation.enabled=false）。"]
        reg = get_registry()
        broker = reg.get(broker_id) or reg.ensure(broker_id)
        result = run_auto_execute(broker, preview_id, policy)
        if result.get("auto_executed"):
            return [f"✅ 已按自动交易策略执行 (order_id: `{result.get('order_id', '')}`)"]
        return ["⏸ 自动执行已跳过: " + "; ".join(str(r) for r in (result.get("reasons") or [])[:3])]
    except Exception as exc:
        logger.warning("auto-execute check failed: %s", exc)
        return [f"自动执行检查失败，保留为预览: `{exc}`"]



def gateway_config() -> dict:
    """The config the daemon hands to the runtime gateway.

    This used to be a dict literal pinned to Ollama:

        {"model": os.environ.get("ARIA_DAEMON_MODEL", "qwen2.5:7b"),
         "ollama_url": ..., "local_provider": "ollama"}

    so the daemon never saw the user's configuration. Someone running Aria on
    a Vertex/Gemini model had every alert analysed by a local qwen2.5:7b, and
    someone with no Ollama at all had the gateway raise on every alert, fall
    back to the quick summary, and say so only at log level info.

    The gateway itself is fully config-driven — analyze_alert_via_gateway
    passes model/api_url/ollama_url straight through — so it was only ever the
    daemon fabricating this.

    Loaded per call rather than cached: the daemon is long-lived, and a config
    change should reach the next alert without a restart. The two env vars stay
    as operator overrides (ARIA_DAEMON_MODEL is in this module's env table).
    """
    from apps.cli.bootstrap import default_config
    from apps.cli.config_paths import resolve_paths
    from packages.aria_services.settings import SettingsService

    paths = resolve_paths()
    cfg = SettingsService(
        config_dir=paths.config_dir,
        config_file=paths.config_file,
        sessions_dir=paths.sessions_dir,
        defaults=default_config(),
    ).load()

    if os.environ.get("ARIA_DAEMON_MODEL"):
        cfg["model"] = os.environ["ARIA_DAEMON_MODEL"]
    if os.environ.get("OLLAMA_URL"):
        cfg["ollama_url"] = os.environ["OLLAMA_URL"]
    return cfg

async def _run_tradingview_alert(payload: dict) -> str:
    """Handle a TradingView alert webhook job."""
    try:
        from apps.cli.tradingview_bridge import build_tradingview_order_preview, parse_tradingview_alert
        alert = parse_tradingview_alert(payload)
    except Exception as exc:
        raise ValueError(f"TradingView alert 解析失败: {exc}") from exc

    symbol = str(alert.get("symbol") or "").upper()
    if not symbol:
        raise ValueError("TradingView alert 缺少 symbol/ticker")

    action = str(alert.get("action") or "ALERT").upper()
    price = alert.get("price")
    price_line = f"Price: `{price}`" if price not in (None, "") else "Price: `N/A`"
    msg = str(alert.get("message") or "").strip()
    header = [
        f"*TradingView Alert* `{symbol}`",
        f"Action: *{action}*",
        price_line,
    ]
    if msg:
        header.append(f"Message: {msg[:300]}")

    preview_lines: list[str] = []
    try:
        preview_result = build_tradingview_order_preview(payload)
        if preview_result.get("trade_preview_created"):
            blockers = preview_result.get("execution_blockers") or []
            preview_lines.extend([
                "",
                "*Trade Preview*",
                f"preview_id: `{preview_result.get('preview_id')}`",
                f"Mode: `{preview_result.get('mode')}`  Broker: `{preview_result.get('broker_label')}`",
                f"Can execute after confirmation: `{bool(preview_result.get('can_execute'))}`",
                f"Confirm: `{preview_result.get('confirm_command')}`",
            ])
            if blockers:
                preview_lines.append("Blockers: " + "; ".join(str(item) for item in blockers[:4]))
            preview_lines.extend(_maybe_auto_execute(preview_result))
        elif preview_result.get("success"):
            reason = preview_result.get("reason", "no_trade_preview")
            preview_lines.extend(["", "*Trade Preview*", f"未生成订单预览: `{reason}`"])
            if preview_result.get("hint"):
                preview_lines.append(str(preview_result["hint"]))
        else:
            preview_lines.extend(["", "*Trade Preview*", f"生成失败: `{preview_result.get('error')}`"])
    except Exception as exc:
        logger.warning("TradingView trade preview failed: %s", exc)
        preview_lines.extend(["", "*Trade Preview*", f"生成失败: `{exc}`"])

    # Channels contract: analysis goes through the shared runtime gateway
    # (tool-less turn) instead of poking CLI internals; the legacy quick
    # summary stays as the fallback when no model backend is reachable.
    analysis = ""
    try:
        from apps.channels.intake import analyze_alert_via_gateway
        from apps.channels.tradingview import task_prompt
        analysis = await asyncio.wait_for(
            analyze_alert_via_gateway(task_prompt(alert), gateway_config()), timeout=90.0
        )
    except Exception as exc:
        # warning, not info: this is a silent capability downgrade — the alert
        # still gets answered, but by the legacy summary rather than the model.
        logger.warning("gateway analysis unavailable (%s) — using quick summary", exc)
    if not analysis:
        analysis = await _run_report(symbol)
    return "\n".join(header + preview_lines) + "\n\n" + analysis


async def _run_morning_brief() -> str:
    """Enriched morning market brief — A-share + US + crypto + FX."""
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"*🌅 Aria 晨报  {now_str}*\n"]

        # ── A-share indices ────────────────────────────────────────────────
        cn_indices = [
            ("上证指数", "000001.SS"), ("深证成指", "399001.SZ"),
            ("创业板",   "399006.SZ"), ("沪深300", "000300.SS"),
        ]
        lines.append("*🇨🇳 A股指数*")
        for name, sym in cn_indices:
            p, pc = await _fetch_price(sym)
            if p:
                arrow = "📈" if (pc or 0) >= 0 else "📉"
                pct_str = f" {pc:+.2f}%" if pc is not None else ""
                lines.append(f"  {arrow} {name}: `¥{p:.2f}`{pct_str}")
            else:
                lines.append(f"  — {name}: N/A")

        # ── US markets ────────────────────────────────────────────────────
        us_indices = [
            ("S&P 500", "^GSPC"), ("纳斯达克", "^IXIC"),
            ("道琼斯",  "^DJI"),  ("VIX恐慌", "^VIX"),
        ]
        lines.append("\n*🇺🇸 美股指数*")
        for name, sym in us_indices:
            p, pc = await _fetch_price(sym)
            if p:
                arrow = "📈" if (pc or 0) >= 0 else "📉"
                pct_str = f" {pc:+.2f}%" if pc is not None else ""
                lines.append(f"  {arrow} {name}: `{p:.2f}`{pct_str}")
            else:
                lines.append(f"  — {name}: N/A")

        # ── Crypto ────────────────────────────────────────────────────────
        crypto_pairs = [("BTC", "BTC-USD"), ("ETH", "ETH-USD"), ("黄金", "GC=F")]
        lines.append("\n*₿ 加密 & 大宗*")
        for name, sym in crypto_pairs:
            p, pc = await _fetch_price(sym)
            if p:
                pct_str = f" {pc:+.2f}%" if pc is not None else ""
                lines.append(f"  {name}: `${p:,.2f}`{pct_str}")
            else:
                lines.append(f"  {name}: N/A")

        # ── FX ───────────────────────────────────────────────────────────
        fx_pairs = [("USD/CNY", "CNY=X"), ("USD/JPY", "JPY=X")]
        lines.append("\n*💱 汇率*")
        for name, sym in fx_pairs:
            p, _ = await _fetch_price(sym)
            lines.append(f"  {name}: `{p:.4f}`" if p else f"  {name}: N/A")

        # ── Portfolio P&L summary ─────────────────────────────────────────
        pnl = await _get_portfolio_daily_pnl()
        if pnl:
            lines.append("\n*💼 持仓日内 P&L*")
            lines.append(pnl)

        lines.append("\n_数据来自 yfinance，仅供参考_")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("morning brief error: %s", exc)
        return f"⚠️ 晨报生成失败: {exc}"


async def _run_evening_brief() -> str:
    """Post-market A-share evening summary."""
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"*🌆 Aria 收盘简报  {now_str}*\n"]

        cn_indices = [
            ("上证指数", "000001.SS"), ("深证成指", "399001.SZ"),
            ("创业板",   "399006.SZ"), ("沪深300", "000300.SS"),
        ]
        lines.append("*今日收盘*")
        for name, sym in cn_indices:
            p, pc = await _fetch_price(sym)
            if p:
                arrow = "📈" if (pc or 0) >= 0 else "📉"
                pct_str = f" {pc:+.2f}%" if pc is not None else ""
                lines.append(f"  {arrow} {name}: `¥{p:.2f}`{pct_str}")

        pnl = await _get_portfolio_daily_pnl()
        if pnl:
            lines.append(f"\n*💼 今日持仓损益*\n{pnl}")

        lines.append("\n_数据来自 yfinance · 请以实盘数据为准_")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ 收盘简报失败: {exc}"


async def _run_weekly_recap() -> str:
    """Friday end-of-week performance summary."""
    try:
        now_str = datetime.now().strftime("%Y 第%W周")
        lines = [f"*📅 Aria 周报  {now_str}*\n"]

        us_etfs = [("S&P500", "SPY"), ("纳指", "QQQ"), ("罗素", "IWM"), ("黄金", "GLD")]
        lines.append("*本周美股 ETF 表现*")
        for name, sym in us_etfs:
            try:
                import yfinance as _yf
                df = _yf.download(sym, period="5d", progress=False, auto_adjust=True)
                if not df.empty and len(df) >= 2:
                    if hasattr(df.columns, 'levels'):
                        df.columns = df.columns.droplevel(1)
                    w_ret = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
                    arrow = "📈" if w_ret >= 0 else "📉"
                    lines.append(f"  {arrow} {name}: `{w_ret:+.2f}%`")
            except Exception:
                lines.append(f"  — {name}: N/A")

        cn_indices = [("上证", "000001.SS"), ("沪深300", "000300.SS"), ("创业板", "399006.SZ")]
        lines.append("\n*本周A股指数*")
        for name, sym in cn_indices:
            try:
                import yfinance as _yf
                df = _yf.download(sym, period="5d", progress=False, auto_adjust=True)
                if not df.empty and len(df) >= 2:
                    if hasattr(df.columns, 'levels'):
                        df.columns = df.columns.droplevel(1)
                    w_ret = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
                    arrow = "📈" if w_ret >= 0 else "📉"
                    lines.append(f"  {arrow} {name}: `{w_ret:+.2f}%`")
            except Exception:
                lines.append(f"  — {name}: N/A")

        lines.append("\n_数据来自 yfinance，仅供参考_")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ 周报生成失败: {exc}"


async def _get_portfolio_daily_pnl() -> str:
    """Read portfolio positions from DB and compute daily P&L."""
    try:
        portfolio_db = _ARIA_DIR / "portfolio.db"
        if not portfolio_db.exists():
            return ""
        with sqlite3.connect(portfolio_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, quantity, avg_cost FROM positions WHERE quantity > 0 LIMIT 10"
            ).fetchall()
        if not rows:
            return ""
        total_pnl = 0.0
        position_lines = []
        for r in rows:
            sym = r["symbol"]
            qty = r["quantity"]
            cost = r["avg_cost"]
            price, pct = await _fetch_price(sym)
            if price and cost and qty:
                day_pnl = (price - cost) * qty
                total_pnl += day_pnl
                arrow = "📈" if day_pnl >= 0 else "📉"
                position_lines.append(
                    f"  {arrow} {sym}: ¥{price:.2f}  日盈亏 `{day_pnl:+.2f}`"
                )
        if not position_lines:
            return ""
        result = "\n".join(position_lines)
        result += f"\n  合计: `{total_pnl:+.2f}`"
        return result
    except Exception as exc:
        logger.debug("portfolio pnl error: %s", exc)
        return ""


async def _portfolio_loss_watchdog() -> None:
    """Check portfolio positions every 5 min; push alert if any position drops > threshold."""
    LOSS_THRESHOLD_PCT = float(os.environ.get("ARIA_LOSS_ALERT_PCT", "5.0"))
    logger.info("Portfolio loss watchdog started (threshold: %.1f%%)", LOSS_THRESHOLD_PCT)
    while True:
        try:
            portfolio_db = _ARIA_DIR / "portfolio.db"
            if portfolio_db.exists():
                with sqlite3.connect(portfolio_db) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT symbol, quantity, avg_cost FROM positions WHERE quantity > 0"
                    ).fetchall()
                for r in rows:
                    sym = r["symbol"]
                    qty = float(r["quantity"])
                    cost = float(r["avg_cost"] or 0)
                    if qty <= 0 or cost <= 0:
                        continue
                    price, pct = await _fetch_price(sym)
                    if price and pct is not None and pct <= -LOSS_THRESHOLD_PCT:
                        day_loss = (price - cost) * qty
                        title = f"⚠️ {sym} 跌幅预警"
                        body = (
                            f"{sym} 当前价 ¥{price:.2f}，"
                            f"日跌幅 {pct:.2f}%，"
                            f"持仓损益 {day_loss:+.2f} 元"
                        )
                        logger.info("Portfolio loss alert: %s %.2f%%", sym, pct)
                        await _push_alert(title, body)
                        await _telegram_push(f"*{title}*\n{body}")
                        await _feishu_push(title, body)
        except Exception as exc:
            logger.debug("portfolio watchdog error: %s", exc)
        await asyncio.sleep(300)


def _system_notify(title: str, body: str) -> None:
    """Fire a native OS desktop notification (non-blocking, best-effort)."""
    try:
        import platform as _pl
        if _pl.system() == "Darwin":
            import subprocess as _sp
            safe_title = title.replace('"', '\\"').replace("'", "\\'")
            safe_body = body.replace('"', '\\"').replace("'", "\\'")
            _sp.Popen(
                ["osascript", "-e",
                 f'display notification "{safe_body}" with title "{safe_title}" subtitle "Aria Code"'],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
        elif _pl.system() == "Windows":
            try:
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(title, body, duration=6, threaded=True)
            except ImportError:
                pass
    except Exception:
        pass


async def _run_screener() -> str:
    """Quick hot-stock screener using market_data_client if available."""
    try:
        spec = __import__("importlib.util").util.spec_from_file_location(
            "market_data_client", _ARIA_CODE_DIR / "market_data_client.py"
        )
        mdc = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(mdc)
        client = mdc.MarketDataClient()
        result = await asyncio.wait_for(client.hot_ashare(limit=8), timeout=10.0)
        stocks = result if isinstance(result, list) else []
        if not stocks:
            return "⚠️ 暂时无法获取行情数据（市场未开市或数据源异常）"
        lines = ["*🔥 A股热门*\n"]
        for s in stocks[:8]:
            sym  = s.get("symbol", "")
            name = s.get("name", sym)
            p    = s.get("price", 0)
            chg  = s.get("change_pct", 0)
            lines.append(f"  `{sym}` {name}  ¥{p:.2f}  {chg:+.2f}%")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ 筛选器出错: {exc}"


async def _handle_alert_add(args: str, chat_id: int) -> str:
    """Parse and store an alert from Telegram. Format: SYMBOL cond value"""
    parts = args.split()
    if len(parts) < 3:
        return (
            "用法: `/alert SYMBOL condition value`\n"
            "示例: `/alert 600362 price_below 39.5`\n"
            "条件: `price_above` / `price_below`"
        )
    symbol, cond, val_str = parts[0].upper(), parts[1].lower(), parts[2]
    valid_conds = {"price_above", "price_below", "pct_change_above", "pct_change_below"}
    if cond not in valid_conds:
        return f"⚠️ 无效条件 `{cond}`。可用: {', '.join(valid_conds)}"
    try:
        val = float(val_str)
    except ValueError:
        return f"⚠️ 无效数值: `{val_str}`"

    import uuid
    alert_id = str(uuid.uuid4())[:8]
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO alerts (id, symbol, condition, value, message, notify_push, notify_telegram) "
            "VALUES (?,?,?,?,?,1,1)",
            (alert_id, symbol, cond, val, f"{symbol} {cond.replace('_',' ')} {val}"),
        )
        conn.commit()
    return f"✅ 预警已设置 `[{alert_id}]`\n{symbol} {cond.replace('_', ' ')} `{val}`"


def _list_alerts() -> str:
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, symbol, condition, value, active, trigger_count FROM alerts ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
    if not rows:
        return "📭 暂无预警设置"
    lines = ["*预警列表*\n"]
    for r in rows:
        status = "🟢" if r["active"] else "⚫"
        lines.append(f"{status} `{r['id']}` {r['symbol']} {r['condition'].replace('_',' ')} `{r['value']}` (触发{r['trigger_count']}次)")
    return "\n".join(lines)


def _daemon_status() -> str:
    pid = os.getpid()
    with sqlite3.connect(_DB_PATH) as conn:
        alert_count  = conn.execute("SELECT COUNT(*) FROM alerts WHERE active=1").fetchone()[0]
        sched_count  = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0]
        device_count = conn.execute("SELECT COUNT(*) FROM device_tokens").fetchone()[0]
        job_count    = conn.execute("SELECT COUNT(*) FROM webhook_jobs WHERE status='pending'").fetchone()[0]
    return (
        f"*🤖 Aria Daemon 状态*\n"
        f"  PID: `{pid}`\n"
        f"  活跃预警: `{alert_count}`\n"
        f"  定时任务: `{sched_count}`\n"
        f"  推送设备: `{device_count}`\n"
        f"  待处理 Webhook: `{job_count}`\n"
        f"  时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )


async def _run_chat(text: str) -> str:
    """Simple chat fallback using aria-code LLM if configured."""
    return f"💬 _收到你的消息: \"{text[:80]}\"_\n\n请在 Terminal 启动 `/aria` 获得完整对话体验。"


# ── Webhook job executor ─────────────────────────────────────────────────────

async def _webhook_executor() -> None:
    """Poll webhook_jobs table for pending jobs and execute them."""
    logger.info("Webhook executor started")
    while True:
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM webhook_jobs WHERE status='pending' ORDER BY created_at LIMIT 5"
                ).fetchall()
            for row in rows:
                asyncio.create_task(_execute_job(dict(row)))
        except Exception as exc:
            logger.error("Webhook executor error: %s", exc)
        await asyncio.sleep(2)


async def _webhook_http_server() -> None:
    """Small local HTTP endpoint for TradingView webhooks."""
    try:
        from aiohttp import web
        from apps.cli.tradingview_bridge import enqueue_tradingview_alert
    except ImportError as exc:
        logger.warning("Webhook HTTP server disabled: %s", exc)
        return

    token = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET") or os.environ.get("WEBHOOK_TOKEN", "")
    host = os.environ.get("ARIA_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("ARIA_WEBHOOK_PORT", "8765"))

    def _authorized(request) -> bool:
        if not token:
            return True
        supplied = (
            request.headers.get("X-Aria-Token")
            or request.headers.get("X-TradingView-Secret")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.query.get("token")
            or request.query.get("secret")
        )
        return supplied == token

    async def _handle_tradingview(request):
        # Open-mode guard (channels contract): with neither an endpoint token
        # nor ARIA_WEBHOOK_SECRET configured, only loopback clients may submit —
        # an open endpoint reachable off-host would accept injected alerts.
        try:
            from apps.channels.intake import should_refuse_open_intake
            secret_configured = bool(os.environ.get("ARIA_WEBHOOK_SECRET", "").strip())
            if should_refuse_open_intake(
                getattr(request, "remote", None),
                token_configured=bool(token),
                secret_configured=secret_configured,
            ):
                return web.json_response(
                    {"success": False,
                     "error": "open-mode intake refused for non-local client; set ARIA_WEBHOOK_SECRET or WEBHOOK_TOKEN"},
                    status=403,
                )
        except ImportError:
            pass
        if not _authorized(request):
            return web.json_response({"success": False, "error": "unauthorized"}, status=401)
        text = await request.text()
        try:
            import json as _json
            payload = _json.loads(text) if text.strip() else {}
        except Exception:
            payload = {"message": text}
        if request.query.get("channels") and isinstance(payload, dict):
            import json as _json
            payload["channels"] = _json.dumps(
                [item.strip() for item in request.query["channels"].split(",") if item.strip()]
            )
        if isinstance(payload, dict):
            for key in ("broker_id", "quantity", "qty", "target_weight", "order_type"):
                if request.query.get(key):
                    payload[key] = request.query[key]
        result = enqueue_tradingview_alert(payload, db_path=_DB_PATH)
        status = 202 if result.get("success") else 400
        return web.json_response(result, status=status)

    app = web.Application()
    app.router.add_post("/api/v1/webhook/tradingview", _handle_tradingview)
    app.router.add_post("/api/v1/tradingview/webhook", _handle_tradingview)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("TradingView webhook listening on http://%s:%d/api/v1/webhook/tradingview", host, port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def _execute_job(job: dict) -> None:
    job_id  = job["id"]
    command = job["command"]
    try:
        import json as _json
        payload = _json.loads(job.get("payload") or "{}")
    except Exception:
        payload = {}

    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            "UPDATE webhook_jobs SET status='running', started_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        conn.commit()

    try:
        if command.startswith("chat:"):
            result = await _run_chat(command[5:])
        elif command.startswith("report ") or command.startswith("/report "):
            sym = command.split()[-1].upper()
            result = await _run_report(sym)
        elif command == "tradingview_alert":
            result = await _run_tradingview_alert(payload)
        elif command in ("morning-brief", "/morning-brief", "brief"):
            result = await _run_morning_brief()
        elif command in ("screen", "/screen"):
            result = await _run_screener()
        else:
            raise ValueError(f"未知 Webhook 命令: {command!r}（支持: chat: / report / morning-brief / screen）")

        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE webhook_jobs SET status='done', result=?, done_at=datetime('now') WHERE id=?",
                (result[:2000], job_id),
            )
            conn.commit()

        # If channels include telegram, send result
        channels = []
        try:
            import json as _j
            raw_channels = payload.get("channels", "[]")
            if isinstance(raw_channels, list):
                channels = raw_channels
            else:
                channels = _j.loads(raw_channels or "[]")
        except Exception:
            pass
        if command == "tradingview_alert" and not channels:
            channels = ["telegram", "feishu"]
        if "telegram" in channels:
            await _telegram_push(result)
        if "feishu" in channels:
            await _feishu_push("⏰ Webhook 任务完成", result[:2000])

    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE webhook_jobs SET status='error', result=?, done_at=datetime('now') WHERE id=?",
                (str(exc)[:500], job_id),
            )
            conn.commit()


# ── Shipper digest ───────────────────────────────────────────────────────────

async def _run_shipper_digests() -> None:
    """Push each bound shipper group its digest (apps/channels/digest.py)."""
    try:
        from apps.channels.conversation import ConversationStore
        from apps.channels.digest import run_digests

        senders = {}
        import aria_feishu_bot
        # With the app's credentials (self-hosted), or through the relay client
        # this daemon runs in relay mode — the bot knows which it has.
        if aria_feishu_bot.can_send():
            senders["feishu"] = aria_feishu_bot.send_digest_card
        report = await run_digests(ConversationStore(), senders)
        for line in report:
            logger.info("shipper digest %s: %s", line["conversation"], line["status"])
    except Exception as exc:
        logger.error("shipper digest job failed: %s", exc)


# ── APScheduler cron ─────────────────────────────────────────────────────────

def _start_scheduler() -> None:
    """Load schedules from DB and register with APScheduler."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1"
            ).fetchall()

        for row in rows:
            s = dict(row)
            try:
                trigger = CronTrigger.from_crontab(s["cron_expr"], timezone="Asia/Shanghai")
                scheduler.add_job(
                    _run_scheduled_job,
                    trigger=trigger,
                    args=[s],
                    id=s["id"],
                    name=s.get("name") or s["command"],
                    replace_existing=True,
                    misfire_grace_time=300,
                )
                logger.info("Scheduled: %s [%s]", s.get("name", s["id"]), s["cron_expr"])
            except Exception as exc:
                logger.error("Failed to schedule %s: %s", s["id"], exc)

        # Default scheduled jobs (always register regardless of user schedules)
        # A-share pre-market brief: 08:55 weekdays (before 09:30 open)
        scheduler.add_job(
            _run_morning_brief_and_push,
            CronTrigger.from_crontab("55 8 * * 1-5", timezone="Asia/Shanghai"),
            id="default_morning_brief",
            name="A股开盘前晨报",
            replace_existing=True,
            misfire_grace_time=600,
        )
        # US pre-market brief: 21:00 weekdays CN time (US market opens 21:30 CN)
        scheduler.add_job(
            _run_morning_brief_and_push,
            CronTrigger.from_crontab("0 21 * * 1-5", timezone="Asia/Shanghai"),
            id="default_us_brief",
            name="美股开盘前简报",
            replace_existing=True,
            misfire_grace_time=600,
        )
        # A-share post-market evening brief: 15:35 weekdays
        scheduler.add_job(
            _run_evening_brief_and_push,
            CronTrigger.from_crontab("35 15 * * 1-5", timezone="Asia/Shanghai"),
            id="default_evening_brief",
            name="A股收盘晚报",
            replace_existing=True,
            misfire_grace_time=600,
        )
        # Weekly recap: Friday 16:30 CN time
        scheduler.add_job(
            _run_weekly_recap_and_push,
            CronTrigger.from_crontab("30 16 * * 5", timezone="Asia/Shanghai"),
            id="default_weekly_recap",
            name="周报",
            replace_existing=True,
            misfire_grace_time=1800,
        )
        logger.info("Registered 4 default scheduled jobs (morning / US / evening / weekly)")

        # Shipper digest: each group bound to a shipper gets what needs
        # attention. Only when the operator has said where the data is.
        if os.environ.get("ARIA_SHIPPER_FEEDS", "").strip():
            cron = os.environ.get("ARIA_DIGEST_CRON", "30 8 * * 1-6").strip()
            scheduler.add_job(
                _run_shipper_digests,
                CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
                id="default_shipper_digest",
                name="货主库存与运费提醒",
                replace_existing=True,
                misfire_grace_time=1800,
            )
            logger.info("Registered shipper digest [%s]", cron)

        scheduler.start()
        logger.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))
        return scheduler
    except ImportError:
        logger.warning("APScheduler not installed — cron disabled. pip install apscheduler")
        return None


async def _run_scheduled_job(schedule: dict) -> None:
    logger.info("Running scheduled job: %s", schedule.get("name", schedule["id"]))
    import json as _j
    channels = _j.loads(schedule.get("channels") or '["ios","telegram"]')
    cmd = schedule["command"]
    result = ""
    try:
        if cmd in ("morning-brief", "morning"):
            result = await _run_morning_brief()
        elif cmd in ("evening-brief", "evening"):
            result = await _run_evening_brief()
        elif cmd in ("weekly-recap", "weekly"):
            result = await _run_weekly_recap()
        elif cmd == "screen":
            result = await _run_screener()
        elif cmd.startswith("report "):
            result = await _run_report(cmd.split()[-1].upper())
        elif cmd.startswith("custom:"):
            result = f"Custom job: {cmd[7:]}"
        else:
            raise ValueError(
                f"未知定时命令: {cmd!r}（支持: morning-brief / evening-brief / weekly-recap / screen / report <symbol> / custom:<payload>）"
            )
    except Exception as exc:
        result = f"⚠️ 定时任务 [{cmd}] 失败: {exc}"

    if "telegram" in channels:
        await _telegram_push(f"⏰ *定时任务完成*\n{result}")
    if "feishu" in channels:
        await _feishu_push(f"⏰ 定时任务：{cmd}", result[:2000])
    if "ios" in channels:
        await _push_alert(
            "Aria 定时任务",
            result[:150],
            {"command": cmd},
        )

    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            "UPDATE schedules SET last_run=datetime('now') WHERE id=?",
            (schedule["id"],),
        )
        conn.commit()


async def _run_morning_brief_and_push() -> None:
    brief = await _run_morning_brief()
    await _telegram_push(brief)
    await _feishu_push("📊 Aria 晨报", brief[:2000])
    await _push_alert("Aria 晨报", brief[:150])
    _system_notify("Aria 晨报", brief[:120])


async def _run_evening_brief_and_push() -> None:
    brief = await _run_evening_brief()
    await _telegram_push(brief)
    await _feishu_push("🌆 Aria 收盘简报", brief[:2000])
    await _push_alert("Aria 收盘", brief[:150])
    _system_notify("Aria 收盘简报", brief[:120])


async def _run_weekly_recap_and_push() -> None:
    recap = await _run_weekly_recap()
    await _telegram_push(recap)
    await _feishu_push("📅 Aria 周报", recap[:2000])
    await _push_alert("Aria 周报", recap[:150])
    _system_notify("Aria 周报", recap[:120])


# ── PID management ────────────────────────────────────────────────────────────

def _write_pid() -> None:
    _ARIA_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Install / uninstall as macOS LaunchAgent ──────────────────────────────────

def _install_launchagent() -> None:
    python  = sys.executable
    script  = str(Path(__file__).resolve())
    log_out = str(_LOG_DIR / "daemon.log")
    log_err = str(_LOG_DIR / "daemon.err")
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.aria.daemon.plist"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aria.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:{str(Path(python).parent)}</string>
        <key>ARIA_CODE_DIR</key>
        <string>{str(_ARIA_CODE_DIR)}</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    os.system(f"launchctl unload '{plist_path}' 2>/dev/null; launchctl load -w '{plist_path}'")
    print(f"✅ Aria Daemon installed as LaunchAgent: {plist_path}")
    print(f"   Logs: {log_out}")
    print(f"   To uninstall: python3 {script} --uninstall")


def _uninstall_launchagent() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.aria.daemon.plist"
    if plist_path.exists():
        os.system(f"launchctl unload '{plist_path}' 2>/dev/null")
        plist_path.unlink()
        print("✅ Aria Daemon LaunchAgent removed")
    else:
        print("ℹ️  LaunchAgent not installed")
    _remove_pid()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("═" * 55)
    logger.info(" Aria Daemon starting  PID=%d", os.getpid())
    logger.info("═" * 55)
    _write_pid()

    loop = asyncio.get_event_loop()
    # Graceful shutdown on SIGINT / SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    tasks = [
        asyncio.create_task(_alert_watchdog(), name="alert_watchdog"),
        asyncio.create_task(_webhook_executor(), name="webhook_executor"),
        asyncio.create_task(_webhook_http_server(), name="webhook_http_server"),
        asyncio.create_task(_portfolio_loss_watchdog(), name="portfolio_watchdog"),
    ]

    scheduler = _start_scheduler()

    # Start Telegram bot if token configured
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tg_token:
        from aria_telegram_bot import TelegramBot
        allowed_raw = os.environ.get("TELEGRAM_ALLOWED_IDS", "")
        allowed_ids = set(
            int(x.strip()) for x in allowed_raw.split(",") if x.strip().isdigit()
        )
        bot = TelegramBot(token=tg_token, allowed_chat_ids=allowed_ids)
        me = await bot.get_me()
        if me:
            logger.info("Telegram bot: @%s", me.get("username", "?"))
            await _telegram_push("🤖 *Aria Daemon 已启动*\n发送 `/help` 查看命令。")
        tasks.append(asyncio.create_task(bot.start(_telegram_command), name="telegram_bot"))
    else:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")

    # Start Feishu relay client if relay mode configured
    relay_url       = os.environ.get("ARIA_RELAY_URL", "")
    relay_client_id = os.environ.get("ARIA_RELAY_CLIENT_ID", "")
    relay_mode      = os.environ.get("ARIA_RELAY_MODE", "")
    if relay_url and relay_client_id and relay_mode == "relay":
        try:
            from aria_relay_client import _connect_and_serve as _relay_serve
            tasks.append(asyncio.create_task(_relay_serve(), name="feishu_relay"))
            logger.info("Feishu relay client started → %s (client=%s)", relay_url, relay_client_id)
        except ImportError:
            logger.warning("aria_relay_client.py not found — Feishu relay disabled")

    logger.info("All workers started. Daemon running.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        _remove_pid()
        if scheduler:
            scheduler.shutdown(wait=False)
        logger.info("Aria Daemon stopped")


async def _shutdown() -> None:
    logger.info("Shutdown signal received")
    for task in asyncio.all_tasks():
        if task.get_name() not in ("main_task",):
            task.cancel()


if __name__ == "__main__":
    if "--install" in sys.argv:
        _install_launchagent()
    elif "--uninstall" in sys.argv:
        _uninstall_launchagent()
    elif "--status" in sys.argv:
        pid_file = _ARIA_DIR / "daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                print(f"✅ Aria Daemon is running (PID {pid})")
            except ProcessLookupError:
                print("⚠️  PID file exists but process not running")
        else:
            print("⚫ Aria Daemon is not running")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
