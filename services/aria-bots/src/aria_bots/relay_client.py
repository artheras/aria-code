#!/usr/bin/env python3
"""
aria_bots.relay_client — 连接 Aria 中继服务器的 WebSocket 客户端
================================================================
本机运行，把中继服务器转发来的飞书消息交给 aria_feishu_bot 处理，
并把 LLM 回复返回给中继服务器，再由服务器推送到飞书。

启动方式:
  python3 aria_relay_client.py              # 单次连接（断线自动重连）
  python3 aria_relay_client.py --once       # 调试：收到第一条消息后退出

所需环境变量（读取 ~/.aria/.env）:
  ARIA_RELAY_URL        中继地址，配置向导默认填 Arthera 运营的中继（见 OFFICIAL_RELAY_URL）
  ARIA_RELAY_CLIENT_ID  setup_wizard 生成的 12 位 hex id
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("aria.relay_client")

# ── 加载 ~/.aria/.env ──────────────────────────────────────────────────────

def _load_env() -> None:
    env_file = Path.home() / ".aria" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                if k not in os.environ:
                    os.environ[k] = v.strip()


_load_env()

# ── This machine's relay credentials ──────────────────────────────────────────
#
# The relay records both on first registration (as hashes): the token proves a
# later connection is this machine, and the bind code is what a Feishu user
# sends to bind to it. Both stay in ~/.aria/.env. Before 2026-10-04 there were
# neither: the client_id was the only identity and also the bind code, so
# anyone who learned it could impersonate the machine or bind their own Feishu
# account to it.

_BIND_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # nothing to misread: no 0/O, 1/I


def new_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


def new_bind_code() -> str:
    import secrets
    return "".join(secrets.choice(_BIND_ALPHABET) for _ in range(12))


def format_bind_code(code: str) -> str:
    """ABCDEFGHJKLM → ARIA-BIND-ABCD-EFGH-JKLM, the form a person types."""
    code = "".join(ch for ch in code.upper() if ch.isalnum())
    return "ARIA-BIND-" + "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def ensure_credentials(env_file: Path = Path.home() / ".aria" / ".env") -> tuple[str, str]:
    """This machine's (token, bind code), created and saved on first use."""
    token = os.environ.get("ARIA_RELAY_CLIENT_TOKEN", "").strip()
    code = os.environ.get("ARIA_RELAY_BIND_CODE", "").strip()
    fresh = {}
    if len(token) < 32:
        token = fresh["ARIA_RELAY_CLIENT_TOKEN"] = new_token()
    if len(code) < 10:
        code = fresh["ARIA_RELAY_BIND_CODE"] = new_bind_code()
    if fresh:
        env_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with env_file.open("a", encoding="utf-8") as handle:
            for key, value in fresh.items():
                handle.write(f"{key}={value}\n")
        env_file.chmod(0o600)
        os.environ.update(fresh)
        if "ARIA_RELAY_BIND_CODE" in fresh:
            logger.info("New relay bind code — send this to the Aria bot in Feishu: /bind %s",
                        format_bind_code(code))
    return token, code


# The relay Arthera runs. Offered as the setup wizard's default, never used as
# a silent fallback.
OFFICIAL_RELAY_URL = "wss://aria-code-741336310848.europe-west2.run.app/ws"

# The default used to be wss://relay.aria.ai. aria.ai belongs to an unrelated
# party (registered 2017); the subdomain does not resolve today, but its owner
# could create it at any time, and every client left on the default would then
# send its machine token, bind code and Feishu traffic there. Anyone who ran
# the wizard before 2026-10-04 has it saved in ~/.aria/.env, so it is refused
# by name, not merely no longer suggested.
_UNOWNED_RELAY_HOSTS = {"relay.aria.ai"}


def relay_url_problem(url: str) -> str:
    """"" when the client may connect to `url`, otherwise why not."""
    from urllib.parse import urlparse

    if not url:
        return "ARIA_RELAY_URL is not set — run the setup wizard (relay mode) to configure it"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _UNOWNED_RELAY_HOSTS or host.endswith(".aria.ai") or host == "aria.ai":
        return (f"{host} is not run by Arthera; run the setup wizard again to switch to "
                f"{OFFICIAL_RELAY_URL}")
    if parsed.scheme == "wss" and host:
        return ""
    if parsed.scheme == "ws" and host in ("localhost", "127.0.0.1", "::1"):
        return ""
    return "ARIA_RELAY_URL must be wss:// — this machine's token is sent when it connects (ws:// only to localhost)"


def default_relay_url(current: str) -> str:
    """What the setup wizard offers: the saved URL, unless it is unusable."""
    current = (current or "").strip()
    return current if current and not relay_url_problem(current) else OFFICIAL_RELAY_URL


_RELAY_URL = os.environ.get("ARIA_RELAY_URL", "").strip()
_CLIENT_ID = os.environ.get("ARIA_RELAY_CLIENT_ID", "")
_RECONNECT_DELAY_MAX = 60   # seconds
_RECONNECT_DELAY_BASE = 3


# ── 本地 aria_feishu_bot import ───────────────────────────────────────────────

def _get_feishu_bot():
    try:
        from aria_bots import feishu_bot
        return feishu_bot
    except ImportError as e:
        logger.warning("feishu_bot not importable: %s", e)
        return None


# ── Message handler ───────────────────────────────────────────────────────────

async def _handle_message(raw_msg: dict, ws) -> None:
    """
    Server sends:
      {"type": "message", "id": "req_xxx", "payload": <feishu_event_dict>}

    We reply:
      {"type": "response", "id": "req_xxx", "result": <any>}
    """
    req_id  = raw_msg.get("id", "")
    payload = raw_msg.get("payload", {})

    bot = _get_feishu_bot()
    if bot is None:
        result = {"error": "feishu_bot unavailable"}
    else:
        try:
            # The relay routes an event only to the machine its sender bound
            # with a code shown here, so that binding is the authorization.
            result = await bot.dispatch_event(payload, authorized_by_binding=True)
        except Exception as e:
            logger.exception("dispatch_event error")
            result = {"error": str(e)[:300]}

    reply = json.dumps({"type": "response", "id": req_id, "result": result})
    await ws.send(reply)


def register_frame(client_id: str, token: str, bind_code: str) -> dict:
    frame = {"type": "register", "client_id": client_id, "token": token, "bind_code": bind_code}
    secret = os.environ.get("ARIA_RELAY_SECRET", "").strip()
    if secret:
        frame["secret"] = secret   # the deployment-wide gate, when the relay sets one
    return frame


# ── Sending through the relay ─────────────────────────────────────────────────
#
# This machine has no Feishu app credentials in relay mode, so the bot hands
# each outgoing message to the relay, which sends it — and refuses anything
# that is not a reply to a message it forwarded here, or a post to a chat this
# user has spoken to the bot in.

_pending_sends: dict[str, asyncio.Future] = {}
_SEND_TIMEOUT = 20


def _relay_sender(ws):
    async def send(request: dict) -> dict:
        import uuid
        send_id = f"send_{uuid.uuid4().hex[:10]}"
        future = asyncio.get_running_loop().create_future()
        _pending_sends[send_id] = future
        try:
            await ws.send(json.dumps({"type": "send", "id": send_id, **request}))
            return await asyncio.wait_for(future, timeout=_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            return {"code": -1, "msg": "relay did not confirm the send"}
        finally:
            _pending_sends.pop(send_id, None)
    return send


def _settle_send(msg: dict) -> None:
    future = _pending_sends.get(msg.get("id", ""))
    if future is not None and not future.done():
        future.set_result(msg.get("result") or {})


# ── Main loop ─────────────────────────────────────────────────────────────────

async def _connect_and_serve(once: bool = False) -> None:
    try:
        import websockets  # type: ignore
    except ImportError:
        logger.error("websockets package not installed — run: pip install websockets")
        sys.exit(1)

    problem = relay_url_problem(_RELAY_URL)
    if problem:
        logger.error("Not connecting to the relay: %s", problem)
        sys.exit(1)

    if not _CLIENT_ID:
        logger.error(
            "ARIA_RELAY_CLIENT_ID is not set. "
            "Run setup_wizard.py to generate your client ID."
        )
        sys.exit(1)

    delay = _RECONNECT_DELAY_BASE
    while True:
        try:
            logger.info("Connecting to %s (client_id=%s)", _RELAY_URL, _CLIENT_ID)
            async with websockets.connect(
                _RELAY_URL,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                # Register with server
                token, bind_code = ensure_credentials()
                await ws.send(json.dumps(register_frame(_CLIENT_ID, token, bind_code)))
                ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                ack = json.loads(ack_raw)
                if not ack.get("ok"):
                    logger.error("Registration rejected: %s", ack.get("reason", "unknown"))
                    if "another installation" in str(ack.get("reason", "")):
                        logger.error("This client_id belongs to a different machine on the relay. "
                                     "Run the setup wizard to create a new one.")
                    await asyncio.sleep(delay)
                    continue

                logger.info("Registered. Waiting for messages…")
                delay = _RECONNECT_DELAY_BASE  # reset on success
                bot = _get_feishu_bot()
                if bot is not None and hasattr(bot, "set_relay_sender"):
                    bot.set_relay_sender(_relay_sender(ws))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from relay: %r", raw[:100])
                        continue

                    if msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                        continue

                    if msg.get("type") == "send_result":
                        _settle_send(msg)
                        continue

                    if msg.get("type") == "message":
                        asyncio.create_task(_handle_message(msg, ws))

                    if once:
                        return

        except (OSError, ConnectionRefusedError) as e:
            logger.warning("Connection failed: %s — retry in %ds", e, delay)
        except asyncio.CancelledError:
            logger.info("Relay client cancelled")
            return
        except Exception as e:
            logger.warning("Relay error: %s — retry in %ds", e, delay)

        # Disconnected: sends must not go to a closed socket while we retry.
        bot = _get_feishu_bot()
        if bot is not None and hasattr(bot, "set_relay_sender"):
            bot.set_relay_sender(None)
        await asyncio.sleep(delay)
        delay = min(delay * 2, _RECONNECT_DELAY_MAX)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Aria 中继客户端")
    parser.add_argument("--once", action="store_true", help="接收一条消息后退出（调试用）")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [aria-relay] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(_connect_and_serve(once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
