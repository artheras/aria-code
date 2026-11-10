"""
aria_bots.telegram_bot — Telegram client and the aria-telegram-bot command handler.

Uses the Telegram Bot API directly via httpx (no heavy python-telegram-bot dep).
Supports long-polling updates and sending messages/documents back to users.

Commands handled:
  /price SYMBOL         — quick quote
  /report SYMBOL        — full analysis (async, returns text summary)
  /brief                — morning brief
  /alerts               — list active alerts
  /alert SYMBOL cond v  — add alert (e.g. /alert 600362 price_below 39.5)
  /help                 — command list

Usage:
    from aria_bots.telegram_bot import TelegramBot
    bot = TelegramBot(token="...", allowed_ids={123456})
    await bot.start(command_handler)   # command_handler(cmd, args, chat_id) -> str
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional, Set

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: Optional[Set[int]] = None,
        poll_timeout: int = 30,
    ):
        self.token = token
        self.allowed_chat_ids = allowed_chat_ids or set()
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._running = False
        self._client: Optional[httpx.AsyncClient] = None

    def is_allowed(self, chat_id: int) -> bool:
        """Fails closed: an empty allowlist allows nobody."""
        return chat_id in self.allowed_chat_ids

    # ── Low-level API ─────────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return _API.format(token=self.token, method=method)

    async def _call(self, method: str, **kwargs: Any) -> Optional[dict]:
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.poll_timeout + 5)
            resp = await self._client.post(self._url(method), json=kwargs)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Telegram %s error: %s", method, data.get("description"))
                return None
            return data.get("result")
        except Exception as exc:
            logger.error("Telegram API call %s failed: %s", method, exc)
            return None

    # ── Sending ───────────────────────────────────────────────────────────────

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str = "Markdown"
    ) -> bool:
        # Telegram has a 4096-char message limit
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        result = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return result is not None

    async def send_long_message(self, chat_id: int, text: str) -> None:
        """Split and send messages that exceed Telegram's 4096-char limit."""
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            await self.send_message(chat_id, chunk)

    async def send_typing(self, chat_id: int) -> None:
        await self._call("sendChatAction", chat_id=chat_id, action="typing")

    async def send_document(
        self, chat_id: int, file_path: str, caption: str = ""
    ) -> bool:
        """Send a file as a document attachment."""
        try:
            import aiofiles
            url = self._url("sendDocument")
            async with aiofiles.open(file_path, "rb") as f:
                content = await f.read()
            import os
            filename = os.path.basename(file_path)
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=60)
            resp = await self._client.post(
                url,
                data={"chat_id": str(chat_id), "caption": caption},
                files={"document": (filename, content)},
            )
            return resp.json().get("ok", False)
        except ImportError:
            # Fallback: just send caption as text
            await self.send_message(chat_id, caption or "File ready (aiofiles not installed)")
            return False
        except Exception as exc:
            logger.error("send_document failed: %s", exc)
            return False

    # ── Polling loop ─────────────────────────────────────────────────────────

    async def get_updates(self) -> list[dict]:
        result = await self._call(
            "getUpdates",
            offset=self._offset,
            timeout=self.poll_timeout,
            allowed_updates=["message"],
        )
        return result or []

    async def start(
        self,
        command_handler: Callable[[str, str, int], Coroutine[Any, Any, str]],
    ) -> None:
        """
        Start long-polling. For each received message, parse the command and
        call command_handler(command, args, chat_id) → reply text.
        command_handler should be an async coroutine.
        """
        self._running = True
        logger.info("Telegram bot polling started")
        while self._running:
            try:
                updates = await self.get_updates()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Polling error: %s", exc)
                await asyncio.sleep(5)
                continue

            for update in updates:
                self._offset = max(self._offset, update["update_id"] + 1)
                msg = update.get("message", {})
                text = (msg.get("text") or "").strip()
                chat_id = msg.get("chat", {}).get("id")
                if not text or not chat_id:
                    continue

                # ACL check. Fails closed: an empty TELEGRAM_ALLOWED_IDS allows
                # nobody. It used to allow everybody — anyone who found the bot
                # could drive the daemon's commands.
                if not self.is_allowed(chat_id):
                    await self.send_message(
                        chat_id,
                        "⛔ 未授权。请将你的 Chat ID 添加到 `TELEGRAM_ALLOWED_IDS`。\n你的 ID: `" + str(chat_id) + "`",
                    )
                    continue

                asyncio.create_task(self._handle(text, chat_id, command_handler))

    async def _handle(
        self,
        text: str,
        chat_id: int,
        command_handler: Callable[[str, str, int], Coroutine[Any, Any, str]],
    ) -> None:
        # Parse "/command args" or plain text
        if text.startswith("/"):
            parts = text[1:].split(None, 1)
            cmd  = parts[0].lower().split("@")[0]  # strip @botname suffix
            args = parts[1] if len(parts) > 1 else ""
        else:
            cmd  = "chat"
            args = text

        await self.send_typing(chat_id)
        try:
            reply = await command_handler(cmd, args, chat_id)
        except Exception as exc:
            reply = f"⚠️ 执行出错: {exc}"
            logger.exception("command_handler error cmd=%s", cmd)

        if reply:
            await self.send_long_message(chat_id, reply)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_me(self) -> Optional[dict]:
        return await self._call("getMe")


# ── Commands (aria-telegram-bot) ─────────────────────────────────────────────

HELP = (
    "*Aria 命令列表*\n\n"
    "`/price SYMBOL` — 实时报价\n"
    "`/report SYMBOL` — 研报\n"
    "`/brief` — 今日晨报\n"
    "`/alert SYMBOL cond value` — 添加价格预警\n"
    "    条件: price\\_above / price\\_below / pct\\_change\\_above / pct\\_change\\_below\n"
    "`/alerts` — 查看预警列表\n"
    "`/status` — Daemon 运行状态\n"
    "\n直接发文字也可对话 Aria。"
)


async def handle_command(cmd: str, args: str, chat_id: int) -> str:
    """Answer one Telegram command: market commands go to aria-runtime daemon,
    everything else is a turn on the Go engine."""
    from . import engine, market

    args = args.strip()
    try:
        if cmd in ("help", "start"):
            return HELP
        if cmd in ("price", "p"):
            symbol = args.split()[0].upper() if args else ""
            if not symbol:
                return "用法: `/price AAPL` 或 `/price 600519.SS`"
            title, body, _ = market.price_card(await market.quote(symbol))
            return f"*{title}*\n" + body.replace("**", "*")
        if cmd in ("brief", "morning", "briefing"):
            title, body = await market.brief("morning-brief")
            return f"*{title}*\n{body}"
        if cmd == "alert":
            parts = args.split()
            if len(parts) != 3:
                return "用法: `/alert AAPL price_above 200`"
            symbol, condition, value = parts[0].upper(), parts[1], parts[2]
            await market.add_alert(symbol, condition, value)
            return f"✅ 预警已添加：*{symbol}* {condition} `{value}`"
        if cmd == "alerts":
            table = await market.list_alerts()
            return "暂无预警" if len(table.splitlines()) < 2 else f"```\n{table}\n```"
        if cmd == "status":
            return await market.status()
    except market.MarketError as exc:
        return f"⚠️ {exc}"

    runtime = engine.find_engine()
    if not runtime:
        return "⚠️ 未找到 aria-runtime：请安装，或用 ARIA_RUNTIME 指向它。"
    if cmd == "report":
        symbol = args.split()[0].upper() if args else ""
        if not symbol:
            return "用法: `/report AAPL` 或 `/report 600519.SS`"
        prompt = (f"为 {symbol} 写一份简明研报：最新行情、技术面、基本面要点、主要风险和结论。"
                  "只用工具取到的数据，注明来源与日期。")
        return await engine.run_turn(prompt, engine=runtime, timeout=240)
    if cmd == "chat":
        return await engine.run_turn(args, engine=runtime) if args else HELP
    return f"未知命令 `/{cmd}`。发送 `/help` 查看命令列表。"


def allowed_ids(raw: str) -> Set[int]:
    return {int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def main() -> int:
    """aria-telegram-bot: long-poll Telegram with TELEGRAM_BOT_TOKEN, answering
    only chats in TELEGRAM_ALLOWED_IDS."""
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [telegram] %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return 2
    ids = allowed_ids(os.environ.get("TELEGRAM_ALLOWED_IDS", ""))
    if not ids:
        logger.warning("TELEGRAM_ALLOWED_IDS is empty: every chat will be refused")
    bot = TelegramBot(token=token, allowed_chat_ids=ids)
    try:
        asyncio.run(bot.start(handle_command))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
