"""Market commands for the bots, answered by `aria-runtime daemon`.

The bots used to import the Python daemon's internals (its database path,
price fetcher and brief builders). Now they run the Go daemon's commands:

    aria-runtime daemon quote SYMBOL --json
    aria-runtime daemon once morning-brief --json
    aria-runtime daemon alert add SYMBOL CONDITION VALUE
    aria-runtime daemon alert list
    aria-runtime daemon status

Quotes come from financial-service; alerts land in the same daemon.db the
running daemon watches (ARIA_DAEMON_DIR, else ~/.aria).
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from . import engine

CONDITIONS = ("pct_change_above", "pct_change_below", "price_above", "price_below")


class MarketError(RuntimeError):
    """The daemon command failed; the message is safe to show in a chat."""


async def daemon(*args: str, timeout: float = 60) -> str:
    runtime = engine.find_engine()
    if not runtime:
        raise MarketError("未找到 aria-runtime：请安装，或用 ARIA_RUNTIME 指向它。")
    proc = await asyncio.create_subprocess_exec(
        runtime, "daemon", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MarketError(f"aria-runtime daemon {args[0]} 超时（>{int(timeout)}s）") from None
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-300:] or f"exit {proc.returncode}"
        raise MarketError(detail)
    return stdout.decode("utf-8", errors="replace").strip()


def _parse_daemon_json(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    raise MarketError("daemon 返回了无效的 JSON 数据")


async def quote(symbol: str) -> dict:
    """{symbol, price, currency, source[, change_pct]}."""
    return _parse_daemon_json(await daemon("quote", symbol, "--json"))


async def brief(command: str = "morning-brief") -> tuple[str, str]:
    """(title, body) of a brief: morning-brief, evening-brief or weekly-recap."""
    data = _parse_daemon_json(await daemon("once", command, "--json", timeout=120))
    return data["title"], data["body"]


async def add_alert(symbol: str, condition: str, value: str) -> str:
    if condition not in CONDITIONS:
        raise MarketError("条件必须是 " + " / ".join(CONDITIONS))
    return await daemon("alert", "add", symbol, condition, value)


async def list_alerts() -> str:
    return await daemon("alert", "list")


async def status() -> str:
    return await daemon("status")


def price_card(data: dict) -> tuple[str, str, str]:
    """(title, body, color) for a quote card. Red is up, as in Chinese markets."""
    symbol, price = data["symbol"], float(data["price"])
    mark = {"CNY": "¥", "RMB": "¥", "USD": "$", "HKD": "HK$"}.get(str(data.get("currency") or "").upper(), "")
    change: Optional[float] = data.get("change_pct")
    lines = [f"**当前价** {mark}{price:,.3f}"]
    if change is None:
        color, arrow = "blue", "📈"
    else:
        lines.append(f"**涨跌幅** {change:+.2f}%")
        color = "red" if change > 0 else "green" if change < 0 else "blue"
        arrow = "📈" if change >= 0 else "📉"
    if data.get("source"):
        lines.append(f"**来源** {data['source']}")
    return f"{arrow} {symbol}", "\n".join(lines), color
