"""
aria_bots.telegram_bot — Lightweight Telegram Bot client for Aria Daemon.

Uses the Telegram Bot API directly via httpx (no heavy python-telegram-bot dep).
Supports long-polling updates and sending messages/documents back to users.

Commands handled:
  /price SYMBOL         — quick quote
  /report SYMBOL        — full analysis (async, returns text summary)
  /brief                — morning brief
  /alerts               — list active alerts
  /alert SYMBOL cond v  — add alert (e.g. /alert 600362 price_below 39.5)
  /screen               — hot A-share screener
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
