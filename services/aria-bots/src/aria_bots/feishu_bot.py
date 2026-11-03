"""
aria_bots.feishu_bot — Feishu (Lark) 多模态 AI 机器人
======================================================
OpenClaw 同款设计：任意输入（文字/语音/图片/文件）→ Aria AI → 卡片回复

两种运行模式：
  1. 嵌入 FastAPI（由 feishu_routes.py 调用）
  2. 独立运行  — python3 -m aria_bots.feishu_bot [port]

飞书端配置：
  1. 飞书开发者后台 → 创建自建应用
  2. 事件订阅 → Request URL: http://<host>/api/v1/feishu/event
  3. 权限：im:message / im:message:send_as_bot
  4. ~/.aria/.env 填写：
       FEISHU_APP_ID=cli_xxx        FEISHU_APP_SECRET=xxx
       ANTHROPIC_API_KEY=xxx        # 图片理解 / LLM
       OPENAI_API_KEY=xxx           # Whisper 语音转文字（可选）
       FEISHU_ALLOWED_USER_IDS=ou_xxx,ou_yyy   # 必填：留空 = 谁都不能用
       FEISHU_ENCRYPT_KEY=xxx 或 FEISHU_VERIFICATION_TOKEN=xxx   # 独立运行时必填其一
       ARIA_BOT_ALLOW_TOOLS=write_file      # 可选：机器人可免审批使用的工具

安全边界（2026-10-03）：
  - 白名单为空时拒绝所有人。之前留空 = 不限制，而机器人调用 aria 时自动批准
    全部工具，于是任何能给它发消息的人都能在这台机器上跑 shell 命令。
  - 机器人不再自动批准需要确认的工具（run_command / write_file / edit_file /
    multi_edit）；没有人能在飞书里点「同意」，这些调用会被拒绝。确实需要的，
    由管理员用 ARIA_BOT_ALLOW_TOOLS 逐个列出。
  - 独立运行时校验飞书事件签名；未配置 Encrypt Key 或 Verification Token 时
    拒绝一切事件。之前任何能访问端口的人都能伪造发件人。

支持的消息类型：
  📝 文字（非命令）→ Aria LLM 自然语言回答
  🎤 语音          → Whisper 转文字 → Aria LLM
  🖼️  图片          → 视觉 LLM 分析（Claude / GPT-4V）
  📄 文件          → 自动解析 PDF/Excel/代码 → Aria LLM 总结

结构化命令（/command）：
  /price AAPL           /brief
  /report NVDA          /run /price TSLA   (调用 aria CLI -p 模式)
  /alert add SYM cond v  /alerts         /status   /help
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional

from aria_bots.channels.conversation import (
    ChannelTurn,
    ConversationStore,
    InboundMessage,
    handle_owner_command,
    prepare_turn,
    should_respond,
)
from aria_bots.logistics.tenancy import OWNER_SCOPE_ENV
from aria_bots import engine as _engine
from aria_bots import market as _market

logger = logging.getLogger(__name__)

# Strip ANSI escape codes from aria CLI output
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Lines that are tool/UI artifacts and must be stripped from bot replies
_BOT_NOISE_RE = re.compile(
    r"^\s*(?:"
    # ── diff / table lines (ASCII pipe AND Unicode box-drawing │ U+2502) ────
    r"[│|][+\- \d]"                    # │298  │+ code  |- old  | context
    r"|[│|]\s*$"                        # │      │  (empty cell borders)
    r"|[┌┐└┘├┤┬┴┼─╌╍╴╶╷╸╹]"          # box corners / connectors
    # ── timing artifacts ────────────────────────────────────────────────────
    r"|└\s*\d+[\.,]\d+s?\b"
    r"|[└─]{1,3}\s*\d+[\.,]\d+\s*s"
    # ── tool call / result bullets ──────────────────────────────────────────
    r"|  [●└■▸]"
    r"|  L \d"
    # ── permission / confirmation dialog ────────────────────────────────────
    r"|[›❯>]\s*\d+\."                  # › 1. Yes  ❯ 1. Yes  > 1. Yes
    r"|\d+\.\s+Yes"                    # 1. Yes / 2. Yes, allow all
    r"|\d+\.\s+No"                     # 3. No
    r"|Enter number"                   # "Enter number (or Enter to keep current):"
    r"|Cancelled"
    # ── leftover Rich markup tags ────────────────────────────────────────────
    r"|\[/?(?:cyan|dim|bold|red|green|yellow|blue|magenta|white|grey|reset)\]"
    # ── horizontal rules ────────────────────────────────────────────────────
    r"|\s*[━─═]{4,}\s*$"
    r")"
)

# Inline timing / markup to strip from within a line
_INLINE_TIMING_RE = re.compile(r"\s*[└─]{1,2}\s*\d+[\.,]\d+\s*s\b")
_INLINE_RICH_TAG_RE = re.compile(r"\[/?(?:cyan|dim|bold|red|green|yellow|blue|magenta|white|grey|reset)\]")

def _find_aria_code_dir() -> Path:
    """Where aria_cli.py lives: the installed aria_code package, else beside this file."""
    import importlib.util

    spec = importlib.util.find_spec("aria_code")
    if spec and spec.submodule_search_locations:
        return Path(list(spec.submodule_search_locations)[0])
    return Path(__file__).parent


_ARIA_CODE_DIR = _find_aria_code_dir()


# ── Feishu API endpoints ───────────────────────────────────────────────────────

_FEISHU_API = "https://open.feishu.cn/open-apis"

# ── Token cache (tenant_access_token, expires ~2h) ────────────────────────────
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


async def _get_access_token() -> Optional[str]:
    """Fetch/cache tenant_access_token via app credentials."""
    app_id     = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return None

    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_FEISHU_API}/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
            token = data.get("tenant_access_token")
            expire = int(data.get("expire", 7200))
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + expire
            return token
    except Exception as exc:
        logger.warning("Feishu token fetch failed: %s", exc)
        return None


# ── Send message helpers ───────────────────────────────────────────────────────

# Set by the relay client while it is connected. In relay mode this machine has
# no Feishu app credentials — by design, the app secret stays on the relay — so
# every message goes back over the WebSocket and the relay sends it. Before
# 2026-10-03 nothing did this, and relay mode silently dropped every answer.
_RELAY_SEND = None


def set_relay_sender(sender) -> None:
    """Route sends through the relay: sender(request) -> Feishu-style result dict."""
    global _RELAY_SEND
    _RELAY_SEND = sender


def can_send() -> bool:
    """Whether this process can deliver a message at all."""
    has_app = bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"))
    return has_app or _RELAY_SEND is not None


async def _send_message(op: str, target: str, msg_type: str, content: str,
                        receive_id_type: str = "chat_id") -> Optional[dict]:
    """The one place a message leaves: directly with app credentials, else via the relay.

    op "reply" answers message `target`; op "send" posts to chat `target`.
    """
    token = await _get_access_token()
    if token:
        if op == "reply":
            return await _feishu_post(f"{_FEISHU_API}/im/v1/messages/{target}/reply", token,
                                      {"msg_type": msg_type, "content": content})
        return await _feishu_post(f"{_FEISHU_API}/im/v1/messages?receive_id_type={receive_id_type}",
                                  token, {"receive_id": target, "msg_type": msg_type, "content": content})
    if _RELAY_SEND is not None:
        try:
            return await _RELAY_SEND({"op": op, "target": target, "msg_type": msg_type,
                                      "content": content, "receive_id_type": receive_id_type})
        except Exception as exc:
            logger.warning("relay send failed: %s", exc)
            return None
    logger.error("cannot send: no Feishu app credentials and no relay connection")
    return None

async def _feishu_post(url: str, token: str, payload: dict) -> Optional[dict]:
    """POST to Feishu API; log the response code on error."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json=payload,
            )
        data = resp.json()
        code = data.get("code", 0)
        if code != 0:
            logger.error("Feishu API error %s: %s  url=%s  msg_id in payload=%s",
                         code, data.get("msg", ""), url.split("/")[-3:],
                         payload.get("receive_id", "—"))
        else:
            logger.debug("Feishu API ok: %s", url.split("/")[-2:])
        return data
    except Exception as exc:
        logger.warning("Feishu POST failed: %s", exc)
        return None


async def reply_text(message_id: str, text: str) -> None:
    """Reply to a Feishu message with plain text (auto-truncated at 3000 chars)."""
    if not message_id:
        logger.error("reply_text: empty message_id — cannot reply")
        return
    logger.info("reply_text → message_id=%s len=%d", message_id, len(text))
    await _send_message("reply", message_id, "text", json.dumps({"text": text[:3000]}))


async def reply_card(message_id: str, title: str, body: str,
                     color: str = "blue", footer: str = "") -> None:
    """Reply with an interactive card (title + Markdown body)."""
    if not message_id:
        logger.error("reply_card: empty message_id — cannot reply")
        return
    elements = _build_card_elements(body, footer)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }
    logger.info("reply_card → message_id=%s title=%s", message_id, title[:40])
    result = await _send_message("reply", message_id, "interactive", json.dumps(card))
    # If card failed, fall back to plain text
    if result and result.get("code") != 0:
        logger.info("reply_card: card failed (code %s), falling back to text", result.get("code"))
        await reply_text(message_id, f"【{title}】\n{body}")


async def reply_or_send(message_id: str, chat_id: str,
                        title: str, body: str,
                        color: str = "blue", footer: str = "") -> None:
    """Try reply by message_id first; if that fails, send a new message to chat_id."""
    if message_id:
        result = await _reply_card_raw(message_id, title, body, color, footer)
        if result is not None and result.get("code", 0) == 0:
            return
        logger.warning("reply failed (code=%s), falling back to send_card_to_chat",
                       result.get("code") if result else "no response")
    if chat_id:
        await send_card_to_chat(chat_id, title, body, color)
    else:
        logger.error("reply_or_send: both message_id and chat_id empty, cannot send")


def _build_card_elements(body: str, footer: str = "") -> list:
    """Split body into visual sections for richer card layout."""
    import re as _re
    # Split on markdown `---` dividers or `##`/`###` section headers
    _section_re = _re.compile(r'(?m)^[-─]{3,}\s*$')
    raw_sections = _section_re.split(body.strip())
    elements: list = []
    for i, sec in enumerate(raw_sections):
        sec = sec.strip()
        if not sec:
            continue
        # Detect if section starts with a ## header and peel it off
        _hdr_m = _re.match(r'^#{1,3}\s+(.+)\n', sec)
        if _hdr_m:
            hdr_text = _hdr_m.group(1).strip()
            sec_body = sec[_hdr_m.end():].strip()
            elements.append({"tag": "markdown", "content": f"**{hdr_text}**"})
            if sec_body:
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": sec_body[:900]}})
        else:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": sec[:900]}})
        if i < len(raw_sections) - 1:
            elements.append({"tag": "hr"})

    if not elements:
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body[:2000]}}]

    if footer:
        elements += [{"tag": "hr"}, {"tag": "note", "elements": [
            {"tag": "plain_text", "content": footer}
        ]}]
    return elements


async def _reply_card_raw(message_id: str, title: str, body: str,
                          color: str = "blue", footer: str = "") -> Optional[dict]:
    """Reply with a card; return raw API response dict."""
    elements = _build_card_elements(body, footer)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }
    return await _send_message("reply", message_id, "interactive", json.dumps(card))


async def send_card_to_chat(chat_id: str, title: str, body: str,
                            color: str = "blue", receive_id_type: str = "chat_id") -> bool:
    """Send a new card message to a chat (group or user). True if Feishu accepted it.

    The content is the card itself. It was wrapped as {"card": ...} — the
    custom-webhook format, which /im/v1/messages rejects — and the response
    was never read, so every proactive send failed without a trace. Nothing
    scheduled depended on it until the shipper digest.
    """
    elements = _build_card_elements(body)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }
    result = await _send_message("send", chat_id, "interactive", json.dumps(card), receive_id_type) or {}
    if result.get("code") != 0:
        logger.warning("send_card_to_chat rejected: code=%s msg=%s", result.get("code"), result.get("msg"))
        return False
    return True


# ── Command router ─────────────────────────────────────────────────────────────

async def _handle_command(cmd: str, message_id: str, sender_id: str, chat_id: str = "") -> None:
    """Parse a command string and reply with structured card."""
    parts = cmd.strip().split()
    if not parts:
        return
    verb = parts[0].lstrip("/").lower()

    if verb == "help":
        body = (
            "**💬 直接发消息** — 自然语言提问，Aria AI 直接回答\n"
            "**🎤 语音消息** — 自动转文字后 AI 分析\n"
            "**🖼️ 图片** — 自动识别图表/截图内容\n"
            "**📄 文件** — PDF/Excel/Word/代码 自动解析后 AI 总结\n\n"
            "**结构化命令：**\n"
            "`/price <symbol>` — 实时价格（支持 A 股 6 位代码）\n"
            "`/brief` — 今日晨报摘要\n"
            "`/report <symbol>` — 个股研报（异步推送）\n"
            "`/team <symbol>` — 🤖 多Agent研究（宏观+基本面+技术+风控）\n"
            "`/run <aria命令>` — 执行任意 Aria 命令，如 `/run /corr AAPL TSLA`\n"
            "`/alert add <symbol> <cond> <value>` — 添加价格预警\n"
            "　　条件: `price_above` `price_below` `pct_change_above` `pct_change_below`\n"
            "`/alerts` — 查看所有预警\n"
            "`/status` — Daemon 运行状态\n"
            "`/help` — 显示此帮助"
        )
        await reply_card(message_id, "📖 Aria 帮助", body, "blue")

    elif verb == "price":
        symbol = parts[1].upper() if len(parts) > 1 else ""
        if not symbol:
            await reply_text(message_id, "用法: /price <symbol>，例如 /price AAPL 或 /price 600036")
            return
        await reply_card(message_id, f"🔄 查询 {symbol}…", "正在获取实时行情，请稍候…", "blue")
        try:
            title, body, color = _market.price_card(await _market.quote(symbol))
            await reply_card(message_id, title, body, color)
        except Exception as exc:
            await reply_card(message_id, f"❌ {symbol}", f"查询失败: {str(exc)[:300]}", "red")

    elif verb == "brief":
        await reply_card(message_id, "⏳ 生成晨报…", "正在获取市场数据，请稍候…", "blue")
        try:
            title, brief = await _market.brief("morning-brief")
            await reply_card(message_id, title, brief[:2000], "green",
                             footer="Aria Code · 实时市场分析")
        except Exception as exc:
            await reply_card(message_id, "❌ 晨报生成失败", str(exc)[:300], "red")

    elif verb == "report":
        symbol = parts[1].upper() if len(parts) > 1 else ""
        if not symbol:
            await reply_text(message_id, "用法: /report <symbol>，例如 /report NVDA")
            return
        await reply_card(message_id, f"⏳ 生成 {symbol} 研报…",
                         "正在进行多维度分析，通常需要 30-60 秒，完成后推送结果。", "blue")
        asyncio.create_task(_async_report(symbol, message_id))

    elif verb == "alert":
        if len(parts) >= 5 and parts[1].lower() == "add":
            sym, cond, val = parts[2].upper(), parts[3], parts[4]
            if cond not in _market.CONDITIONS:
                await reply_card(message_id, "❌ 无效条件",
                                 "支持的条件：\n" + "\n".join(f"• `{c}`" for c in _market.CONDITIONS), "red")
                return
            try:
                await _market.add_alert(sym, cond, val)
            except Exception as exc:
                await reply_card(message_id, "❌ 预警添加失败", str(exc)[:300], "red")
                return
            await reply_card(message_id, "✅ 预警已添加",
                             f"**{sym}** 当 {cond.replace('_',' ')} `{val}` 时触发通知", "green")
        else:
            await reply_text(message_id, "用法: /alert add <symbol> <cond> <value>")

    elif verb == "alerts":
        try:
            table = await _market.list_alerts()
        except Exception as exc:
            await reply_card(message_id, "❌ 预警读取失败", str(exc)[:300], "red")
            return
        rows = table.splitlines()[1:]
        body = "暂无预警" if not rows else "```\n" + table[:1900] + "\n```"
        await reply_card(message_id, f"📋 预警（{len(rows)} 条）", body, "blue")

    elif verb == "status":
        try:
            text = await _market.status()
        except Exception as exc:
            await reply_card(message_id, "📡 Aria Daemon 状态", str(exc)[:300], "red")
            return
        running = "not running" not in text.splitlines()[0] if text else False
        await reply_card(message_id, "📡 Aria Daemon 状态", text[:2000],
                         "green" if running else "red")
    elif verb == "run":
        # /run <aria-command>  e.g. /run /price AAPL  /run /corr AAPL TSLA NVDA
        sub_cmd = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if not sub_cmd:
            await reply_text(message_id, "用法: /run <aria命令>，例如 `/run /price AAPL`")
            return
        await reply_card(message_id, f"⚙️ 执行: {sub_cmd[:60]}", "正在运行，请稍候…", "blue")
        asyncio.create_task(_async_run_aria(sub_cmd, message_id))

    else:
        await reply_card(message_id, "❓ 未知命令",
                         f"不认识 `/{verb}`，发送 `/help` 查看全部命令", "yellow")


async def _async_report(symbol: str, message_id: str) -> None:
    """Background task: a research note from the engine (financial-service tools)."""
    runtime = _engine.find_engine()
    if not runtime:
        await reply_card(message_id, f"❌ {symbol} 研报失败", "未找到 aria-runtime", "red")
        return
    prompt = (f"为 {symbol} 写一份简明研报：最新行情、技术面（趋势、RSI、MACD）、"
              "基本面要点、主要风险，以及结论。只用工具取到的数据，注明来源与日期。")
    result = await _engine.run_turn(prompt, engine=runtime, owner=_TURN_OWNER.get(), timeout=240)
    failed = result.startswith(("❌", "⏱️"))
    await reply_card(message_id, f"{'❌' if failed else '📄'} {symbol} 研报{'失败' if failed else '完成'}",
                     result[:2000], "red" if failed else "turquoise", footer="Aria Code · 研报")


async def _async_run_aria(cmd: str, message_id: str) -> None:
    """Background task: run aria CLI command and reply with result."""
    result = await _query_aria_llm(cmd, timeout=120)
    color = "red" if result.startswith("❌") or result.startswith("⏱️") else "turquoise"
    await reply_card(message_id, "✅ Aria 执行完成", result[:2000], color,
                     footer=f"命令: {cmd[:80]}")


# ── Multimodal helpers ────────────────────────────────────────────────────────

def _is_allowed_user(*sender_ids: str) -> bool:
    """Whether any of the sender's ids (user_id, open_id) is on the allowlist.

    Fails closed: an empty FEISHU_ALLOWED_USER_IDS allows nobody. It used to
    allow everybody, and the bot runs aria with tools — so an unconfigured
    deployment answered anyone who could message it.
    """
    raw = os.environ.get("FEISHU_ALLOWED_USER_IDS", "")
    allowed = {u.strip() for u in raw.split(",") if u.strip()}
    return any(i in allowed for i in sender_ids if i)


def _bot_cli_flags() -> list[str]:
    """Extra aria CLI flags for bot-run commands: only an explicit tool allowlist.

    Bot mode no longer approves every tool. A tool that needs confirmation is
    refused, because nobody in the chat can answer the prompt; an operator who
    needs one names it in ARIA_BOT_ALLOW_TOOLS.
    """
    tools = [t.strip() for t in os.environ.get("ARIA_BOT_ALLOW_TOOLS", "").split(",") if t.strip()]
    return ["--allow-tools", ",".join(tools)] if tools else []


async def _query_aria_direct(text: str, timeout: int = 90, history: Optional[list] = None) -> str:
    """
    Query the LLM directly via providers/llm/registry.py — no subprocess, no tool use.
    Used for conversational NL queries where we want a clean text answer.
    Falls back to _query_aria_llm on ImportError.

    With the Go engine installed, the turn runs there instead.
    """
    runtime = _engine.find_engine()
    if runtime:
        return await _engine.run_turn(text, engine=runtime, owner=_TURN_OWNER.get(),
                                      history=history, timeout=timeout)
    try:
        import sys as _sys
        _sys.path.insert(0, str(_ARIA_CODE_DIR))
        from providers.llm.registry import stream_cloud_fallback
        import asyncio as _aio

        collected: list[str] = []
        result = await _aio.wait_for(
            stream_cloud_fallback(text, history=list(history or []), on_token=collected.append),
            timeout=timeout,
        )
        if result.get("success") and collected:
            return "".join(collected).strip()
        if result.get("response"):
            return result["response"].strip()
        return "❌ LLM 返回空响应，请检查 API Key 配置。"
    except ImportError:
        pass  # fall through to subprocess
    except Exception as _exc:
        logger.warning("_query_aria_direct failed: %s", _exc)
    return await _query_aria_llm(text, timeout=timeout)


async def _query_aria_llm(text: str, timeout: int = 120) -> str:
    """
    Run a natural language query through aria-code's LLM via CLI -p mode.
    Returns plain text output (ANSI stripped, tool noise filtered).
    Use _query_aria_direct() for conversational queries — this is for
    slash commands (/brief, /team, etc.) that need the full CLI context.
    """
    runtime = _engine.find_engine()
    if runtime:
        return await _engine.run_turn(text, engine=runtime, owner=_TURN_OWNER.get(), timeout=timeout)
    # No Go engine: the Python CLI, while aria-code still ships one.
    aria_cli = _ARIA_CODE_DIR / "aria_cli.py"
    if not aria_cli.exists():
        return "❌ 未找到 Aria 引擎：请安装 aria-runtime，或用 ARIA_RUNTIME 指向它。"
    try:
        # ARIA_BOT_MODE=1: plain output for a chat card. It no longer approves
        # tools; see _bot_cli_flags.
        bot_env = {**os.environ, "ARIA_BOT_MODE": "1"}
        # A conversation bound to a shipper confines every tool in this run to
        # that shipper; the logistics tools enforce it (logistics_tenancy).
        bot_env.pop(OWNER_SCOPE_ENV, None)
        if _TURN_OWNER.get():
            bot_env[OWNER_SCOPE_ENV] = _TURN_OWNER.get()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(aria_cli), "-p", text, *_bot_cli_flags(),
            stdin=asyncio.subprocess.DEVNULL,   # no interactive prompts
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_ARIA_CODE_DIR),
            env=bot_env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        raw = stdout.decode("utf-8", errors="replace")
        # Strip ANSI codes, filter noise lines, collapse blank runs
        clean_lines = []
        blank_run = 0
        for line in _ANSI_RE.sub("", raw).splitlines():
            if _BOT_NOISE_RE.match(line):
                continue
            line = _INLINE_TIMING_RE.sub("", line)
            line = _INLINE_RICH_TAG_RE.sub("", line)
            if not line.strip():
                blank_run += 1
                if blank_run > 1:   # collapse consecutive blank lines to one
                    continue
            else:
                blank_run = 0
            clean_lines.append(line)
        clean = "\n".join(clean_lines).strip()
        return clean[:3000] if clean else (stderr.decode()[:500] or "（无输出）")
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return f"⏱️ 查询超时（>{timeout}s），请简化问题后重试。"
    except Exception as exc:
        return f"❌ aria LLM 调用失败: {exc}"


async def _download_feishu_resource(message_id: str, resource_key: str,
                                    rtype: str = "file") -> Optional[bytes]:
    """Download image / audio / file from Feishu message."""
    token = await _get_access_token()
    if not token:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{_FEISHU_API}/im/v1/messages/{message_id}/resources/{resource_key}",
                params={"type": rtype},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.content
    except Exception as exc:
        logger.warning("_download_feishu_resource failed: %s", exc)
    return None


async def _transcribe_voice(audio_bytes: bytes) -> str:
    """Speech-to-text: tries OpenAI Whisper API, then local faster-whisper."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            import httpx
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            with open(tmp, "rb") as audio_file:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data={"model": "whisper-1"},
                        files={"file": ("voice.ogg", audio_file, "audio/ogg")},
                    )
                    data = resp.json()
                    return data.get("text", "（识别结果为空）")
        except Exception as exc:
            logger.warning("OpenAI Whisper failed: %s", exc)

    # Fallback: local faster-whisper / openai-whisper
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        segments, _ = model.transcribe(tmp, language="zh")
        return " ".join(seg.text for seg in segments).strip() or "（识别结果为空）"
    except ImportError:
        pass

    return "❌ 语音转文字需要配置 OPENAI_API_KEY，或安装 faster-whisper (`pip install faster-whisper`)"


async def _analyze_image(image_bytes: bytes, caption: str = "") -> str:
    """Analyze image via Claude vision (ANTHROPIC_API_KEY) or GPT-4V (OPENAI_API_KEY)."""
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "请详细分析这张图片的内容。如果是图表、K线图或截图，请做专业解读并给出结论。"

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 1500,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image", "source": {
                                    "type": "base64", "media_type": "image/jpeg", "data": b64}},
                                {"type": "text", "text": prompt},
                            ],
                        }],
                    },
                )
                data = resp.json()
                return data["content"][0]["text"]
        except Exception as exc:
            logger.warning("Claude vision failed: %s", exc)

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    json={
                        "model": "gpt-4o",
                        "max_tokens": 1500,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64}"}},
                                {"type": "text", "text": prompt},
                            ],
                        }],
                    },
                )
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("GPT-4V failed: %s", exc)

    return "❌ 图片分析需要配置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY"


async def _analyze_file(file_bytes: bytes, filename: str) -> str:
    """Extract text from a file and query Aria LLM for analysis."""
    ext = Path(filename).suffix.lower()
    text_content = ""

    if ext == ".pdf":
        try:
            import pdfplumber
            import io
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages[:20]]
                text_content = "\n".join(pages)[:8000]
        except ImportError:
            try:
                import pypdf
                import io
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                text_content = "\n".join(
                    p.extract_text() or "" for p in reader.pages[:20]
                )[:8000]
            except Exception as exc:
                return f"❌ PDF 解析失败: {exc}"

    elif ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            import io
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
            rows = []
            for sheet in wb.sheetnames[:3]:
                ws = wb[sheet]
                for row in list(ws.iter_rows(values_only=True))[:50]:
                    rows.append("\t".join(str(c) for c in row if c is not None))
            text_content = f"[Excel: {filename}]\n" + "\n".join(rows)[:6000]
        except Exception as exc:
            return f"❌ Excel 解析失败: {exc}"

    elif ext in (".docx",):
        try:
            import docx
            import io
            doc = docx.Document(io.BytesIO(file_bytes))
            text_content = "\n".join(p.text for p in doc.paragraphs)[:8000]
        except Exception as exc:
            return f"❌ Word 解析失败: {exc}"

    elif ext in (".py", ".js", ".ts", ".go", ".java", ".cpp", ".c", ".rs",
                 ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".toml"):
        try:
            text_content = file_bytes.decode("utf-8", errors="replace")[:8000]
        except Exception:
            return "❌ 文件编码无法识别"

    else:
        try:
            text_content = file_bytes.decode("utf-8", errors="replace")[:4000]
        except Exception:
            return f"❌ 不支持的文件类型: {ext}"

    if not text_content.strip():
        return "❌ 文件内容为空或无法提取文本"

    query = (
        f"请分析以下文件（{filename}）的内容，给出主要信息、关键数据和洞察总结：\n\n"
        f"```\n{text_content[:6000]}\n```"
    )
    return await _query_aria_llm(query, timeout=120)


# ── Event verifier ────────────────────────────────────────────────────────────

def verify_feishu_request(headers: Dict[str, str], body: bytes, payload: Dict[str, Any]) -> tuple[bool, str]:
    """(trusted, reason) for an event received directly from Feishu.

    The same two checks the relay applies (go/relay: relay.verify; formerly the Python relay's _verify_feishu_request),
    kept separate because the relay ships without this module's dependencies:
      - Encrypt Key: X-Lark-Signature = sha256(timestamp + nonce + key + body)
      - Verification Token: the token field in the event body
    With neither configured every event is refused, unless
    FEISHU_ALLOW_UNVERIFIED_EVENTS=1 is set for local testing.

    This replaces verify_feishu_signature, which compared the digest with an
    empty string and was never called: the standalone server dispatched any
    POST, so anyone who could reach the port chose the sender — including one
    on the allowlist.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    encrypt_key = os.environ.get("FEISHU_ENCRYPT_KEY", "")
    token = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
    if encrypt_key:
        signature = headers.get("x-lark-signature", "")
        if not signature:
            return False, "missing X-Lark-Signature"
        digest = hashlib.sha256(
            (headers.get("x-lark-request-timestamp", "") + headers.get("x-lark-request-nonce", "")
             + encrypt_key).encode("utf-8") + body
        ).hexdigest()
        if not hmac.compare_digest(digest, signature):
            return False, "signature mismatch"
        return True, ""
    if token:
        sent = payload.get("token") or (payload.get("header") or {}).get("token", "")
        if not hmac.compare_digest(str(sent), token):
            return False, "verification token mismatch"
        return True, ""
    if os.environ.get("FEISHU_ALLOW_UNVERIFIED_EVENTS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True, ""
    return False, ("neither FEISHU_ENCRYPT_KEY nor FEISHU_VERIFICATION_TOKEN is set; "
                   "unverified events are refused")


# ── Conversation layer (apps/channels/conversation.py) ───────────────────────

# The shipper the current conversation is bound to, for every task it spawns.
_TURN_OWNER: ContextVar[Optional[str]] = ContextVar("aria_feishu_turn_owner", default=None)
_STORE: Optional[ConversationStore] = None
_MENTION_TOKEN = re.compile(r"@_user_\d+")


def _conversation_store() -> ConversationStore:
    global _STORE
    if _STORE is None or str(_STORE.path) != str(_conversation_db_path()):
        _STORE = ConversationStore(_conversation_db_path())
    return _STORE


def _conversation_db_path() -> Path:
    from aria_bots.channels.conversation import default_db_path
    return default_db_path()


def _strip_mentions(text: str) -> str:
    """Feishu writes each @ as a placeholder (@_user_1); the model needs the words."""
    return re.sub(r"\s+", " ", _MENTION_TOKEN.sub(" ", text or "")).strip()


def _mentions_bot(message: Dict[str, Any]) -> bool:
    """Whether a group message addresses this bot.

    With FEISHU_BOT_OPEN_ID set, the bot's own id must be among the mentions.
    Without it, any mention counts — correct for an app without the
    read-all-group-messages permission, which receives only @-mentions of
    itself; set the id if the app has that permission.
    """
    mentions = message.get("mentions") or []
    own_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()
    if own_id:
        return any((m.get("id") or {}).get("open_id") == own_id for m in mentions)
    return bool(mentions)


def feishu_inbound(event: Dict[str, Any], text: str) -> InboundMessage:
    """A Feishu im.message.receive_v1 event as a platform-neutral message."""
    message = event.get("message") or {}
    sender = (event.get("sender") or {}).get("sender_id") or {}
    open_id = sender.get("open_id", "")
    user_id = sender.get("user_id", "")
    direct = message.get("chat_type") == "p2p"
    return InboundMessage(
        channel="feishu",
        conversation_id=message.get("chat_id", ""),
        kind="direct" if direct else "group",
        sender_id=open_id or user_id,
        sender_aliases=(user_id,) if open_id and user_id else (),
        text=text,
        message_id=message.get("message_id", ""),
        mentions_bot=direct or _mentions_bot(message),
        reply_to=message.get("parent_id", ""),
    )


async def _send_digest_now(conversation: str, message_id: str) -> None:
    """/digest: this group's shipper digest, now, as a reply."""
    from aria_bots.channels.digest import build_digest, load_feeds

    owner = _conversation_store().owner_for(conversation)
    if not owner:
        await reply_text(message_id, "本群未绑定货主，没有可发送的简报。管理员可发送 /owner <货主ID> 绑定。")
        return
    try:
        digest = build_digest(owner, load_feeds())
    except Exception as exc:
        logger.warning("digest for %s failed: %s", owner, exc)
        digest = None
    if digest is None or digest.problems and not digest.worth_sending:
        # The details are for the operator's log, not the client's group.
        await reply_text(message_id, "暂时无法生成简报，已记录到运维日志。")
        return
    if not digest.worth_sending:
        await reply_text(message_id, f"{owner}：今天没有需要处理的补货、呆滞或运费异常。")
        return
    await reply_card(message_id, digest.title, digest.body, "orange")


# ── Approvals (apps/channels/approvals.py) ────────────────────────────────────

def _approval_card(approval) -> Dict[str, Any]:
    """The request as a card: buttons while pending, the outcome once decided."""
    from aria_bots.channels.approvals import PENDING, status_label

    elements = _build_card_elements(approval.summary)
    if approval.status == PENDING:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
            f"审批编号 {approval.id[:8]} · 24 小时内有效 · 批准后生成采购单草稿文件，不会自动下单"}]})
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "批准"},
             "value": {"aria_approval": approval.id, "decision": "approve"}},
            {"tag": "button", "type": "danger", "text": {"tag": "plain_text", "content": "驳回"},
             "value": {"aria_approval": approval.id, "decision": "reject"}},
        ]})
        template = "orange"
    else:
        outcome = f"{status_label(approval.status)} · {approval.decided_by}"
        # The file name, not the host path; a failure's details stay in the
        # approval record and the log, not in the client's group.
        if approval.status == "approved" and approval.result:
            outcome += f"\n已生成：{Path(approval.result).name}"
        elif approval.status == "failed":
            outcome += "\n详情已记录在审批记录中"
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": outcome}]})
        template = "green" if approval.status == "approved" else "grey"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"🧾 {approval.owner_id} 采购单草稿"},
                   "template": template},
        "elements": elements,
    }


async def _request_reorder(inbound: InboundMessage, message_id: str) -> None:
    """/补货: propose a purchase-order draft from the reorder list, for approval."""
    from aria_bots.channels.approvals import ApprovalStore, propose_reorder
    from aria_bots.channels.digest import feeds_for, load_feeds

    owner = _conversation_store().owner_for(inbound.key)
    if not owner:
        await reply_text(message_id, "本群未绑定货主，无法发起补货审批。管理员可发送 /owner <货主ID> 绑定。")
        return
    try:
        inventory = feeds_for(owner, load_feeds()).get("inventory")
        if not inventory:
            raise ValueError(f"no inventory feed configured for {owner}")
        proposal = propose_reorder(owner, inventory)
    except Exception as exc:
        logger.warning("reorder proposal for %s failed: %s", owner, exc)
        await reply_text(message_id, "暂时无法生成补货建议，已记录到运维日志。")
        return
    if proposal is None:
        await reply_text(message_id, f"{owner}：当前没有需要补货的 SKU。")
        return
    summary, payload = proposal
    approval = ApprovalStore(_conversation_db_path()).request(
        conversation=inbound.key, owner_id=owner, kind="purchase_order_draft", payload=payload,
        summary=f"**建议订货**\n{summary}\n\n数量未计入起订量和箱规，请审批人核对。",
        requested_by=f"feishu:{inbound.sender_id}",
    )
    await _send_message("reply", message_id, "interactive", json.dumps(_approval_card(approval)))


async def _handle_card_action(event: Dict[str, Any], *, authorized_by_binding: bool = False) -> Dict[str, Any]:
    """An approve/reject press. The response's toast and card are what the presser sees."""
    from aria_bots.channels.approvals import ApprovalStore, can_decide

    operator = event.get("operator") or {}
    ids = tuple(i for i in (operator.get("open_id", ""), operator.get("user_id", "")) if i)
    value = (event.get("action") or {}).get("value") or {}
    chat_id = (event.get("context") or {}).get("open_chat_id", "")
    approval_id = str(value.get("aria_approval", ""))
    if not approval_id:
        return {}

    def toast(kind: str, text: str, card=None) -> Dict[str, Any]:
        out: Dict[str, Any] = {"toast": {"type": kind, "content": text}}
        if card is not None:
            out["card"] = {"type": "raw", "data": card}
        return out

    if not authorized_by_binding and not _is_allowed_user(*ids):
        return toast("error", "你还没有使用 Aria 的权限。")
    conversation = f"feishu:{chat_id}"
    decision = ApprovalStore(_conversation_db_path()).decide(
        approval_id,
        conversation=conversation,
        current_owner=_conversation_store().owner_for(conversation),
        decided_by=f"feishu:{ids[0] if ids else ''}",
        approve=value.get("decision") == "approve",
        authorised=can_decide("feishu", ids),
    )
    card = _approval_card(decision.approval) if decision.approval is not None else None
    return toast("success" if decision.ok else "error", decision.message, card)


async def send_digest_card(chat_id: str, title: str, body: str) -> bool:
    """The Feishu sender for apps.channels.digest.run_digests."""
    return await send_card_to_chat(chat_id, title, body, "orange")


# ── Main event dispatcher (called by feishu_routes.py or standalone) ──────────

async def dispatch_event(raw: Dict[str, Any], *, authorized_by_binding: bool = False) -> Dict[str, Any]:
    """
    Handle one Feishu event payload.

    authorized_by_binding: the relay client passes True. The relay forwards an
    event only to the machine its sender bound with a code shown on that
    machine, so the binding is the authorization; a local allowlist would add
    nothing, since the relay knows the bound id. What limits a compromised
    relay is that bot-run commands cannot use confirmation-required tools.
    Supports: text / audio / image / file / post (富文本)
    Returns a dict to be sent as JSON response (HTTP 200 required by Feishu).
    """
    # 1. URL verification challenge
    if "challenge" in raw:
        return {"challenge": raw["challenge"]}

    header     = raw.get("header", {})
    event      = raw.get("event", {})
    event_type = header.get("event_type") or raw.get("type", "")

    # A press on an approval card's button (callback schema 2.0).
    if event_type == "card.action.trigger":
        return await _handle_card_action(event, authorized_by_binding=authorized_by_binding)

    if event_type not in ("im.message.receive_v1", "message"):
        return {"code": 0}

    msg      = event.get("message") or {}
    msg_id   = msg.get("message_id", "")
    msg_type = msg.get("message_type", "text")
    sender   = event.get("sender", {}).get("sender_id", {})
    user_id  = sender.get("user_id", "") or sender.get("open_id", "")
    chat_id  = msg.get("chat_id", "")

    logger.info("dispatch_event: type=%s msg_id=%s msg_type=%s user=%s chat=%s",
                event_type, msg_id, msg_type, user_id, chat_id)

    if not msg_id:
        logger.error("dispatch_event: msg_id is EMPTY — event structure may differ: %s",
                     json.dumps(raw, ensure_ascii=False)[:600])
        return {"code": 0}

    open_id = sender.get("open_id", "")
    if not authorized_by_binding and not _is_allowed_user(sender.get("user_id", ""), open_id):
        logger.warning("Blocked sender user_id=%s open_id=%s (not in FEISHU_ALLOWED_USER_IDS)",
                       sender.get("user_id", ""), open_id)
        # Say why only in a direct chat: in a group the bot would answer every
        # message from everyone not on the list.
        if msg.get("chat_type") == "p2p":
            await reply_text(
                msg_id,
                "⛔ 你还没有使用 Aria 的权限。请把下面的 ID 发给管理员，"
                f"加入 FEISHU_ALLOWED_USER_IDS：\n{open_id or sender.get('user_id', '')}",
            )
        return {"code": 0}

    content_raw = msg.get("content", "{}")
    try:
        content = json.loads(content_raw)
    except Exception:
        content = {}

    # Groups: only what is addressed to the bot. Feishu delivers just the
    # @-mentions unless the app holds the read-all-group-messages permission;
    # with it, this is what stops the bot answering every message in the room.
    # Images, audio and files cannot @ anyone, so in a group they are ignored.
    if msg.get("chat_type") != "p2p" and msg_type not in ("text", "post"):
        return {"code": 0}

    # Attachments are downloaded with the app's credentials, which relay mode
    # does not have on this machine; say so instead of failing silently.
    if msg_type in ("audio", "image", "file") and not (
            os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET")):
        await reply_text(msg_id, "中继模式下暂不支持图片、语音和文件，请直接发送文字。")
        return {"code": 0}

    # ── Text message ──────────────────────────────────────────────────────────
    if msg_type == "text":
        text = _strip_mentions(content.get("text", ""))
        if not text:
            return {"code": 0}
        inbound = feishu_inbound(event, text)
        if not should_respond(inbound):
            return {"code": 0}

        store = _conversation_store()
        owner_reply = handle_owner_command(inbound, store)
        if owner_reply is not None:
            await reply_text(msg_id, owner_reply)
            return {"code": 0}
        if text.strip().lower() in ("/digest", "/简报"):
            asyncio.create_task(_send_digest_now(inbound.key, msg_id))
            return {"code": 0}
        if text.strip().lower() in ("/reorder", "/补货"):
            asyncio.create_task(_request_reorder(inbound, msg_id))
            return {"code": 0}

        # A task copies the context it is created in, so every aria subprocess
        # the tasks below start runs under this conversation's shipper scope.
        # Reset afterwards: dispatch_event runs in its caller's context.
        scope_token = _TURN_OWNER.set(store.owner_for(inbound.key))
        try:
            if text.startswith("/"):
                logger.info("Feishu /cmd from %s: %s", user_id, text[:80])
                asyncio.create_task(_handle_command(text, msg_id, user_id, chat_id))
            else:
                # Free-form natural language → Aria LLM, with this conversation's context
                logger.info("Feishu NL query from %s: %s", user_id, text[:80])
                turn = prepare_turn(inbound, store)
                asyncio.create_task(_handle_nl_query(text, msg_id, chat_id, turn=turn,
                                                     conversation=inbound.key))
        finally:
            _TURN_OWNER.reset(scope_token)

    # ── Voice / Audio ─────────────────────────────────────────────────────────
    elif msg_type == "audio":
        file_key = content.get("file_key", "")
        logger.info("Feishu audio from %s, key=%s", user_id, file_key)
        asyncio.create_task(_handle_audio(file_key, msg_id))

    # ── Image ────────────────────────────────────────────────────────────────
    elif msg_type == "image":
        image_key = content.get("image_key", "")
        logger.info("Feishu image from %s, key=%s", user_id, image_key)
        asyncio.create_task(_handle_image(image_key, msg_id))

    # ── File attachment ───────────────────────────────────────────────────────
    elif msg_type == "file":
        file_key  = content.get("file_key", "")
        file_name = content.get("file_name", "attachment")
        logger.info("Feishu file from %s: %s", user_id, file_name)
        asyncio.create_task(_handle_file(file_key, file_name, msg_id))

    # ── Rich text (post) — extract plain text ─────────────────────────────────
    elif msg_type == "post":
        try:
            # post content: {"zh_cn": {"title":"...","content":[[{"tag":"text","text":"..."},...]]}}
            lang_content = content.get("zh_cn") or content.get("en_us") or {}
            title = lang_content.get("title", "")
            paras = lang_content.get("content", [])
            texts = []
            for para in paras:
                for seg in para:
                    if seg.get("tag") == "text":
                        texts.append(seg.get("text", ""))
            plain = title + ("\n" if title else "") + " ".join(texts)
            if plain.strip():
                asyncio.create_task(_handle_nl_query(plain.strip(), msg_id))
        except Exception as exc:
            logger.warning("post parse error: %s", exc)

    return {"code": 0}


# ── Message type handlers (run as background tasks) ───────────────────────────

_MARKET_BRIEF_TRIGGERS = frozenset({
    "行情查询", "市场行情", "股市行情", "今日行情", "行情", "大盘", "大盘行情",
    "市场概况", "今日市场", "A股行情", "港股行情", "美股行情", "晨报",
    "market", "market overview", "stock market", "markets today",
})


# ── 常用 A股/港股 中文名 → 股票代码（用于 NL 解析）──────────────────────────
_CN_COMPANY_TICKER: dict[str, str] = {
    # 银行
    "工商银行": "601398", "工行": "601398",
    "建设银行": "601939", "建行": "601939",
    "农业银行": "601288", "农行": "601288",
    "中国银行": "601988", "中行": "601988",
    "招商银行": "600036", "招行": "600036",
    "平安银行": "000001",
    "兴业银行": "601166",
    "浦发银行": "600000",
    "光大银行": "601818",
    # 券商/保险
    "中信证券": "600030",
    "海通证券": "600837",
    "中国平安": "601318", "平安": "601318",
    "中国人寿": "601628",
    # 能源/化工
    "中国石油": "601857", "中石油": "601857",
    "中国石化": "600028", "中石化": "600028",
    "中国神华": "601088",
    # 有色金属
    "江西铜业": "600362",
    "紫金矿业": "601899",
    "中金黄金": "600489",
    "山东黄金": "600547",
    "洛阳钼业": "603993",
    "铜陵有色": "000630",
    "中国铝业": "601600",
    "南方铜业": "SCCO",
    "自由港": "FCX", "自由港麦克莫兰": "FCX",
    # 消费
    "贵州茅台": "600519", "茅台": "600519",
    "五粮液": "000858",
    "洋河股份": "002304",
    "海天味业": "603288",
    "伊利股份": "600887",
    "格力电器": "000651", "格力": "000651",
    "美的集团": "000333", "美的": "000333",
    "海尔智家": "600690",
    # 科技/互联网
    "腾讯": "0700", "腾讯控股": "0700",
    "阿里巴巴": "BABA", "阿里": "BABA",
    "京东": "JD",
    "百度": "BIDU",
    "比亚迪": "002594",
    "宁德时代": "300750", "宁德": "300750",
    "中芯国际": "688981",
    "海康威视": "002415",
    # 地产
    "万科": "000002", "万科A": "000002",
    "碧桂园": "2007",
    "恒大": "3333",
    # 医药
    "恒瑞医药": "600276",
    "迈瑞医疗": "300760",
    # 全球大宗商品矿企
    "必和必拓": "BHP", "必拓": "BHP",
    "力拓": "RIO", "力拓集团": "RIO",
    "淡水河谷": "VALE",
    "嘉能可": "GLEN.L",
}

def _resolve_cn_company(text: str) -> str:
    """
    Replace Chinese company names in text with their ticker symbols.
    E.g. "江西铜业的走势" → "600362的走势"
    Returns modified text; if nothing matched, returns original.
    """
    result = text
    for cn_name, ticker in _CN_COMPANY_TICKER.items():
        if cn_name in result:
            result = result.replace(cn_name, ticker)
    return result


async def _handle_nl_query(text: str, message_id: str, chat_id: str = "", *,
                           turn: Optional[ChannelTurn] = None, conversation: str = "") -> None:
    """Route free-form natural language to Aria LLM and reply."""
    _low = text.strip().lower()
    _orig = text.strip()

    # Fast-path: generic market brief request — the daemon's brief, no model turn
    if _low in _MARKET_BRIEF_TRIGGERS or (
        any(k in _low for k in ("行情查询", "市场行情", "大盘今日", "今日大盘")) and
        not any(c.isalpha() and c.upper() == c for c in text)  # no uppercase ticker
    ):
        await reply_or_send(message_id, chat_id, "📊 获取市场概况…", "正在抓取主要指数数据…", "blue")
        try:
            title, snapshot = await _market.brief("morning-brief")
            await reply_or_send(message_id, chat_id, title, snapshot[:2000], "turquoise",
                                footer="Aria Code · financial-service")
        except Exception as _e:
            await reply_or_send(message_id, chat_id, "❌ 行情获取失败", str(_e)[:200], "red")
        return

    # Chinese company name → ticker resolution before sending to LLM
    resolved = _resolve_cn_company(_orig)
    if resolved != _orig:
        # Found at least one CN name; also annotate so LLM has context
        text = resolved + f"  (原文: {_orig})"

    await reply_or_send(message_id, chat_id, "🤔 思考中…", f"> {_orig[:120]}", "blue")
    # Use direct LLM call (no subprocess, no tool execution) for conversational queries.
    # With a turn, the model sees this conversation's recent messages and, when
    # the conversation is bound to a shipper, which one.
    if turn is not None:
        prompt = turn.prompt if resolved == _orig else f"{turn.prompt}\n({text})"
        result = await _query_aria_direct(prompt, timeout=120, history=turn.history)
        if conversation and not result.startswith("❌"):
            _conversation_store().record(conversation, "assistant", result[:2000])
    else:
        result = await _query_aria_direct(text, timeout=120)
    color = "red" if result.startswith("❌") else "green"
    await reply_or_send(message_id, chat_id, "💡 Aria 回答", result[:2000], color,
                        footer="Aria Code · AI 分析")


async def _handle_audio(file_key: str, message_id: str) -> None:
    """Download voice → transcribe → query Aria LLM."""
    await reply_card(message_id, "🎤 正在转写语音…", "下载中，请稍候…", "blue")
    audio_bytes = await _download_feishu_resource(message_id, file_key, rtype="file")
    if not audio_bytes:
        await reply_card(message_id, "❌ 语音下载失败", "无法获取语音文件", "red")
        return
    text = await _transcribe_voice(audio_bytes)
    if text.startswith("❌"):
        await reply_card(message_id, "❌ 语音转文字失败", text, "red")
        return
    await reply_card(message_id, "🎤 识别结果", f"**语音内容：**\n{text}\n\n---\n正在分析…", "blue")
    result = await _query_aria_llm(text, timeout=120)
    await reply_card(message_id, "💡 Aria 回答", result[:2000], "green",
                     footer=f"语音转文字: {text[:60]}…")


async def _handle_image(image_key: str, message_id: str) -> None:
    """Download image → visual LLM analysis."""
    await reply_card(message_id, "🖼️ 分析图片中…", "正在下载并识别，请稍候…", "blue")
    img_bytes = await _download_feishu_resource(message_id, image_key, rtype="image")
    if not img_bytes:
        await reply_card(message_id, "❌ 图片下载失败", "无法获取图片", "red")
        return
    result = await _analyze_image(img_bytes)
    await reply_card(message_id, "🖼️ 图片分析", result[:2000], "turquoise",
                     footer="Aria Code · 视觉 AI")


async def _handle_file(file_key: str, filename: str, message_id: str) -> None:
    """Download file → parse → Aria LLM analysis."""
    await reply_card(message_id, f"📄 解析文件：{filename}",
                     "正在下载并解析，请稍候…", "blue")
    file_bytes = await _download_feishu_resource(message_id, file_key, rtype="file")
    if not file_bytes:
        await reply_card(message_id, "❌ 文件下载失败", "无法获取文件", "red")
        return
    result = await _analyze_file(file_bytes, filename)
    await reply_card(message_id, f"📄 {filename} 分析完成", result[:2000], "turquoise",
                     footer="Aria Code · 文件智能解析")


# ── Standalone HTTP server (for testing without FastAPI) ──────────────────────

async def _standalone_handle(request):
    """POST /feishu/event for the standalone server: verify, then dispatch."""
    from aiohttp import web

    raw = await request.read()
    try:
        body = json.loads(raw)
    except Exception:
        return web.json_response({"code": 1, "msg": "bad json"}, status=400)
    # Before anything else, the URL-verification challenge included: it
    # carries the token too, and answering it unverified is a free probe.
    trusted, reason = verify_feishu_request(dict(request.headers), raw, body)
    if not trusted:
        logger.warning("refused an unverified Feishu event: %s", reason)
        return web.json_response({"code": 1, "msg": "unverified event"}, status=401)
    return web.json_response(await dispatch_event(body))


def _standalone_app():
    from aiohttp import web

    app = web.Application()
    app.router.add_post("/feishu/event", _standalone_handle)
    app.router.add_post("/api/v1/feishu/event", _standalone_handle)
    return app


async def _standalone_server(host: str = "0.0.0.0", port: int = 8888) -> None:
    """Minimal aiohttp-based server for standalone Feishu event reception."""
    try:
        from aiohttp import web
    except ImportError:
        logger.error("aiohttp not installed. pip install aiohttp")
        return

    app = _standalone_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Feishu standalone server listening on http://%s:%d/feishu/event", host, port)
    logger.info("Configure this URL in Feishu Developer Console → Event Subscription")
    await asyncio.Event().wait()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [feishu] %(levelname)s %(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    asyncio.run(_standalone_server(port=port))
