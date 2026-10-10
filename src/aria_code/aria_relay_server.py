#!/usr/bin/env python3
"""
aria_relay_server.py — Aria 中继服务器
=======================================
你（产品方）部署一次，所有用户共用。

架构:
  用户飞书消息
    → 飞书云 → POST /feishu/event
    → relay_server 查 feishu_user_id → 对应 WebSocket
    → relay_server 转发 payload 到用户本机
    → aria_relay_client.dispatch_event()
    → Aria LLM 回复
    → relay_server 收到 response
    → relay_server 调飞书 reply_message API
    → 飞书消息卡片展示给用户

WebSocket 注册流程:
  1. 用户本机 aria_relay_client 连接 wss://relay.yourdomain.com
  2. 发送: {"type": "register", "client_id": "aria-xxxxxxxxxxxx"}
  3. 用户在飞书向 Aria Bot 发: /bind ARIA-BIND-ARIA-XXXXXXXXXXXX
  4. 服务器记录: feishu_user_id → client_id
  5. 后续消息经 WebSocket 透传

依赖:
  pip install fastapi uvicorn websockets httpx

启动:
  python3 aria_relay_server.py
  # 或
  uvicorn aria_code.aria_relay_server:app --host 0.0.0.0 --port 8765

所需环境变量:
  FEISHU_APP_ID         飞书应用 App ID
  FEISHU_APP_SECRET     飞书应用 App Secret
  FEISHU_ENCRYPT_KEY    飞书事件订阅的 Encrypt Key（推荐）——启用请求头签名校验
  FEISHU_VERIFICATION_TOKEN
                        飞书 Verification Token（次选）——校验事件体里的 token
                        ⚠️ 二者至少配一个，否则 /feishu/event 一律拒绝。
                        仅本地联调可设 RELAY_ALLOW_UNVERIFIED_EVENTS=1 绕过。
                        ⚠️ 不支持飞书「加密推送」：Encrypt Key 只用于校验签名，事件体
                        不会被解密。开了加密推送，事件体只有 {"encrypt": ...}，所有消息
                        都会被静默忽略。飞书后台请保持加密推送关闭。
  RELAY_SECRET          部署级别的注册口令（可选）。每台电脑另有自己的令牌和绑定码，
                        见下方 client_credentials：没有它们，任何人都能冒充或绑定别人的电脑。
  RELAY_STORE           状态存储：sqlite（默认，DB_PATH）或 firestore。Cloud Run 上必须用
                        firestore——容器文件系统每次部署都会清空，SQLite 里的绑定和电脑凭证会丢。
  RELAY_FIRESTORE_PROJECT / RELAY_FIRESTORE_DATABASE  可选，默认当前项目和 (default) 数据库
  DB_PATH               SQLite 路径（默认 ./relay.db）
  MESSAGE_TIMEOUT       等待用户本机回复的超时秒数（默认 90）

事件应答:
  飞书约 3 秒内收不到应答就会重发事件。所以 /feishu/event 先应答 {"code": 0}，
  转发和等待本机回复放在应答之后执行；按 event_id（旧版事件为 uuid）去重，
  重发的事件只应答、不再转发。卡片按钮事件例外：飞书要用应答里的 toast/卡片，
  所以仍同步等待本机（最多 2.5 秒）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("aria.relay_server")

# ── Config ────────────────────────────────────────────────────────────────────

_FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
_FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
_RELAY_SECRET      = os.environ.get("RELAY_SECRET", "")
_FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
_FEISHU_ENCRYPT_KEY        = os.environ.get("FEISHU_ENCRYPT_KEY", "")
_ALLOW_UNVERIFIED_EVENTS   = os.environ.get(
    "RELAY_ALLOW_UNVERIFIED_EVENTS", "").strip().lower() in {"1", "true", "yes", "on"}
_DB_PATH           = os.environ.get("DB_PATH", "./relay.db")
_MSG_TIMEOUT       = int(os.environ.get("MESSAGE_TIMEOUT", "90"))
# Overridable so contract tests can point the relay at a fake Feishu.
_FEISHU_API        = os.environ.get("FEISHU_API_BASE", "https://open.feishu.cn/open-apis").rstrip("/")


# ── Persistent state (relay_store.py) ─────────────────────────────────────────
#
# SQLite by default; RELAY_STORE=firestore on Cloud Run, where the container
# filesystem — and with it a SQLite file — is discarded on every deploy.
#
# Each machine's credentials are recorded on its first registration (trust on
# first use), only as hashes: the token proves a later connection is the same
# machine; the bind code is the only thing /bind accepts. The bind code used to
# be the client_id, so anyone who learned one could bind their own Feishu
# account to someone else's machine.

_store_instance = None


def _store():
    global _store_instance
    if _store_instance is None:
        from aria_code.relay_store import open_store
        _store_instance = open_store()
    return _store_instance


def _ephemeral_store() -> bool:
    """SQLite inside a Cloud Run container is discarded with the container."""
    return bool(os.environ.get("K_SERVICE")) and _store().kind == "sqlite"


def get_db() -> sqlite3.Connection:
    """The SQLite connection, when that is the store (tests and local use)."""
    return _store().db


def _lookup_client(feishu_user_id: str) -> Optional[str]:
    return _store().client_for_user(feishu_user_id)


def _bind(feishu_user_id: str, client_id: str) -> None:
    _store().bind_user(feishu_user_id, client_id)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_bind_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _admit_client(client_id: str, token: str, bind_code: str) -> str:
    """"" when this registration may proceed, otherwise why not."""
    if len(token or "") < 32:
        return "this Aria version is too old for the relay; upgrade aria-code"
    if len(_normalise_bind_code(bind_code or "")) < 10:
        return "bind code missing; upgrade aria-code"
    store = _store()
    code_hash = _digest(_normalise_bind_code(bind_code))
    known = store.credentials(client_id)
    if known is None:
        if store.claim_credentials(client_id, _digest(token), code_hash):
            return ""
        known = store.credentials(client_id)   # lost a race for the same id
    if not known or not hmac.compare_digest(known["token_hash"], _digest(token)):
        return "client_id is registered to another installation"
    # The machine may rotate its bind code; the token proves it is the same one.
    if known["bind_code_hash"] != code_hash:
        store.set_bind_code(client_id, code_hash)
    return ""


def _client_for_bind_code(code: str) -> Optional[str]:
    normalised = _normalise_bind_code(code)
    if len(normalised) < 10:
        return None
    return _store().client_for_bind_code(_digest(normalised))


# ── WebSocket connection registry ─────────────────────────────────────────────

_connections: dict[str, WebSocket] = {}   # client_id → WebSocket
# request_id → (client_id it was sent to, Future). Only that client may answer:
# any connected client could otherwise resolve another machine's request.
_pending_responses: dict[str, tuple[str, asyncio.Future]] = {}


# ── Sending on a client's behalf ─────────────────────────────────────────────
#
# Until 2026-10-03 a client could not answer at all. The relay forwarded each
# event and replied to Feishu only when the client was offline; the client's
# bot then tried to reply itself, with app credentials that relay mode
# deliberately never gives it — so every answer was dropped without a trace.
#
# Now the client hands its message back over the WebSocket and the relay sends
# it. That turns the relay's bot identity into something any connected client
# can use, so a send is accepted only where the client has standing:
#   reply — to a message the relay forwarded to this client, within an hour;
#   send  — to a chat this client's bound user has spoken to the bot in;
#   at most _SEND_LIMIT messages per client per minute;
#   msg_type text or interactive, content at most 30 KB.

_FORWARD_TTL = 3600
_SEND_LIMIT = 60
_forwarded: dict[str, tuple[str, float]] = {}       # message_id → (client_id, expires)
_send_times: dict[str, list[float]] = {}             # client_id → recent send timestamps


def _remember_forward(client_id: str, message_id: str, chat_id: str) -> None:
    now = time.time()
    for mid in [m for m, (_, exp) in _forwarded.items() if exp < now]:
        _forwarded.pop(mid, None)
    if message_id:
        _forwarded[message_id] = (client_id, now + _FORWARD_TTL)
    if chat_id:
        _store().remember_chat(client_id, chat_id)


def _may_send(client_id: str, request: dict) -> str:
    """"" when the request is allowed, otherwise why not."""
    op, target = request.get("op"), str(request.get("target") or "")
    if request.get("msg_type") not in ("text", "interactive"):
        return "msg_type must be text or interactive"
    if len(str(request.get("content") or "")) > 30_000:
        return "content too large"
    if op == "reply":
        owner = _forwarded.get(target)
        if not owner or owner[0] != client_id or owner[1] < time.time():
            return "not a message forwarded to this client"
    elif op == "send":
        if request.get("receive_id_type", "chat_id") != "chat_id":
            return "send is only to chats"
        if not _store().has_chat(client_id, target):
            return "this client has no conversation in that chat"
    else:
        return "op must be reply or send"
    now = time.time()
    # Forget clients with no send in the last minute, so the table holds only
    # recent senders rather than every client that ever sent.
    for idle in [cid for cid, times in _send_times.items() if not times or times[-1] <= now - 60]:
        _send_times.pop(idle, None)
    recent = [t for t in _send_times.get(client_id, []) if t > now - 60]
    if len(recent) >= _SEND_LIMIT:
        return "rate limit"
    recent.append(now)
    _send_times[client_id] = recent
    return ""


async def _send_for_client(client_id: str, request: dict) -> dict:
    refused = _may_send(client_id, request)
    if refused:
        logger.warning("refused send for %s: %s", client_id, refused)
        return {"code": -1, "msg": f"relay refused: {refused}"}
    body = {"msg_type": request["msg_type"], "content": request["content"]}
    if request["op"] == "reply":
        url = f"{_FEISHU_API}/im/v1/messages/{request['target']}/reply"
    else:
        url = f"{_FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
        body["receive_id"] = request["target"]
    try:
        token = await _get_tenant_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
        result = resp.json()
    except Exception as exc:
        logger.warning("send for %s failed: %s", client_id, exc)
        return {"code": -1, "msg": "relay could not reach Feishu"}
    sent_id = ((result.get("data") or {}).get("message_id") or "")
    if result.get("code") == 0 and request["msg_type"] == "interactive" and sent_id:
        _store().remember_card(sent_id, client_id)
    return {"code": result.get("code"), "msg": result.get("msg", ""),
            "data": {"message_id": sent_id} if sent_id else {}}


def _card_origin(message_id: str) -> Optional[str]:
    return _store().card_origin(message_id) if message_id else None


# ── Feishu API helpers ────────────────────────────────────────────────────────

_feishu_token_cache: dict[str, Any] = {}


async def _get_tenant_token() -> str:
    now = time.time()
    if _feishu_token_cache.get("expires_at", 0) > now + 60:
        return _feishu_token_cache["token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_FEISHU_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": _FEISHU_APP_ID, "app_secret": _FEISHU_APP_SECRET},
            timeout=10,
        )
        data = resp.json()

    token = data.get("tenant_access_token", "") if isinstance(data, dict) else ""
    if resp.status_code != 200 or data.get("code", 0) != 0 or not token:
        # Not cached: an empty token kept for two hours made every send fail
        # until it expired. The next call tries again.
        raise RuntimeError(f"Feishu tenant token: HTTP {resp.status_code}, "
                           f"code {data.get('code') if isinstance(data, dict) else '?'}")
    expire = int(data.get("expire", 7200))
    _feishu_token_cache.update({"token": token, "expires_at": now + expire})
    return token


async def _reply_feishu(message_id: str, content: str, color: str = "blue") -> None:
    token = await _get_tenant_token()
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "Aria"},
                "template": color,
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{_FEISHU_API}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json=card,
            timeout=15,
        )


async def _send_feishu_text(open_id: str, text: str) -> None:
    token = await _get_tenant_token()
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{_FEISHU_API}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "open_id"},
            json={
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=15,
        )


# ── Route message to local aria instance ─────────────────────────────────────

async def _route_to_local(feishu_user_id: str, payload: dict) -> Optional[Any]:
    """Forward Feishu event to the user's connected local aria instance."""
    client_id = _lookup_client(feishu_user_id)
    if not client_id:
        return None
    message = (payload.get("event") or {}).get("message") or {}
    _remember_forward(client_id, message.get("message_id", ""), message.get("chat_id", ""))
    return await _route_to_client(client_id, payload)


async def _route_to_client(client_id: str, payload: dict, timeout: Optional[float] = None) -> Optional[Any]:
    """Send an event to one connected client and wait for its result."""
    ws = _connections.get(client_id)
    if not ws:
        return None

    req_id = f"req_{uuid.uuid4().hex[:10]}"
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_responses[req_id] = (client_id, future)

    try:
        await ws.send_text(json.dumps({
            "type": "message",
            "id": req_id,
            "payload": payload,
        }))
        result = await asyncio.wait_for(future, timeout=timeout or _MSG_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        logger.warning("Timeout waiting for response from client_id=%s", client_id)
        return {"error": "Aria 本机响应超时，请检查 aria_relay_client 是否在线"}
    finally:
        _pending_responses.pop(req_id, None)


def _resolve_response(client_id: str, message: dict) -> bool:
    """Hand a client's answer to the request waiting for it — if it was sent to that client."""
    owner, future = _pending_responses.get(message.get("id", ""), (None, None))
    if future is None or owner != client_id or future.done():
        return False
    future.set_result(message.get("result"))
    return True


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [relay] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Aria Relay Server started  db=%s", _DB_PATH)
    logger.info("Relay state store: %s", _store().kind)
    if _ephemeral_store():
        logger.warning("Running on Cloud Run with SQLite: bindings and machine credentials are "
                       "lost on every deploy. Set RELAY_STORE=firestore.")
    yield
    logger.info("Relay Server shutting down")


app = FastAPI(title="Aria Relay Server", lifespan=lifespan)


# ── WebSocket endpoint (users' local machines) ────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id: Optional[str] = None

    try:
        # First message must be register
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        msg = json.loads(raw)

        if msg.get("type") != "register":
            await websocket.send_text(json.dumps({"ok": False, "reason": "first message must be register"}))
            return

        client_id = msg.get("client_id", "")
        if not client_id:
            await websocket.send_text(json.dumps({"ok": False, "reason": "client_id required"}))
            return

        # Validate RELAY_SECRET if configured (constant-time: it is a secret)
        if _RELAY_SECRET and not hmac.compare_digest(str(msg.get("secret") or ""), _RELAY_SECRET):
            await websocket.send_text(json.dumps({"ok": False, "reason": "invalid secret"}))
            return

        refused = _admit_client(client_id, str(msg.get("token") or ""), str(msg.get("bind_code") or ""))
        if refused:
            logger.warning("refused registration for %s: %s", client_id, refused)
            await websocket.send_text(json.dumps({"ok": False, "reason": refused}))
            return

        # Register in-memory + mark as pending bind (if first time)
        _connections[client_id] = websocket

        await websocket.send_text(json.dumps({"ok": True, "client_id": client_id}))
        logger.info("Client connected: %s", client_id)

        # Message loop
        async for raw in websocket.iter_text():
            try:
                response_msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if response_msg.get("type") == "pong":
                continue

            if response_msg.get("type") == "send":
                async def _answer(request=response_msg, cid=client_id):
                    result = await _send_for_client(cid, request)
                    await websocket.send_text(json.dumps(
                        {"type": "send_result", "id": request.get("id", ""), "result": result}))
                asyncio.create_task(_answer())
                continue

            if response_msg.get("type") == "response":
                _resolve_response(client_id, response_msg)

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        logger.warning("Register timeout for new connection")
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        # A reconnect registers a new socket under the same client_id before
        # the old one is noticed as gone, and a refused registration never
        # registered at all: neither may remove the live connection.
        if client_id and _connections.get(client_id) is websocket:
            _connections.pop(client_id, None)
            logger.info("Client disconnected: %s", client_id)


# ── Feishu event endpoint ─────────────────────────────────────────────────────


# ── Feishu event authenticity ─────────────────────────────────────────────────
#
# 2026-08-19 修复一条完整的未授权账户劫持链。修复前 /feishu/event 对请求来源
# 不做任何校验，配合公开的 /status，四步即可劫持任意在线用户：
#
#   1. GET  /status         → 拿到全部在线 client_ids（该端点当时把它们直接列出）
#   2. POST /feishu/event   → 伪造事件，sender.open_id 填攻击者自己的，
#                             正文填 "/bind ARIA-BIND-<受害者 client_id>"
#   3. 服务端执行 _bind(攻击者, 受害者client_id)   → 绑定被改写
#   4. 攻击者此后发的消息都会被转发到受害者本机执行，结果回传给攻击者
#
# RELAY_SECRET 只校验 WebSocket 注册那一侧，HTTP 侧完全没有防护。
#
# 飞书提供两种官方校验手段，这里都支持：
#   - Verification Token：事件体里的 token 字段，与控制台配置值比对
#   - Encrypt Key：请求头 X-Lark-Signature = sha256(timestamp+nonce+key+body)
# 两者只要配置了任意一个就会被强制执行；都没配置时默认**拒绝**事件，
# 除非显式设置 RELAY_ALLOW_UNVERIFIED_EVENTS=1（仅供本地联调）。
# 默认拒绝而不是默认放行——这个端点的下游是用户本机，放行的代价太高。


def _verify_feishu_request(headers: dict, body: bytes, payload: dict) -> tuple[bool, str]:
    """返回 (是否可信, 拒绝原因)。"""
    if _FEISHU_ENCRYPT_KEY:
        timestamp = headers.get("x-lark-request-timestamp", "")
        nonce     = headers.get("x-lark-request-nonce", "")
        signature = headers.get("x-lark-signature", "")
        if not signature:
            return False, "missing X-Lark-Signature"
        digest = hashlib.sha256(
            (timestamp + nonce + _FEISHU_ENCRYPT_KEY).encode("utf-8") + body
        ).hexdigest()
        # 定长比较，避免按字节短路带来的时序侧信道
        if not hmac.compare_digest(digest, signature):
            return False, "signature mismatch"
        return True, ""

    if _FEISHU_VERIFICATION_TOKEN:
        token = payload.get("token") or payload.get("header", {}).get("token", "")
        if not hmac.compare_digest(str(token), _FEISHU_VERIFICATION_TOKEN):
            return False, "verification token mismatch"
        return True, ""

    if _ALLOW_UNVERIFIED_EVENTS:
        return True, ""

    return False, (
        "relay 未配置 FEISHU_ENCRYPT_KEY 或 FEISHU_VERIFICATION_TOKEN；"
        "未验签的事件一律拒绝（本地联调可设 RELAY_ALLOW_UNVERIFIED_EVENTS=1）"
    )


@app.post("/feishu/event")
async def feishu_event(request: Request, background: BackgroundTasks):
    """Feishu Developer Console → Event Subscription → Request URL: /feishu/event"""
    body = await request.body()
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # 先验签再做任何事。URL verification challenge 也要验——飞书在发
    # challenge 时同样带 token/签名，放它过去等于给攻击者一个免验探测点。
    trusted, reason = _verify_feishu_request(
        {k.lower(): v for k, v in request.headers.items()}, body, payload
    )
    if not trusted:
        logger.warning("拒绝未通过校验的 /feishu/event 请求: %s", reason)
        raise HTTPException(401, f"Unverified Feishu event: {reason}")

    # URL verification challenge
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    # A button press on a card. Routed to the client that posted the card —
    # it holds the approval and checks the presser against its approvers —
    # and answered synchronously, because Feishu shows the response's toast
    # and card to the presser and waits at most 3 s for it.
    if (payload.get("header") or {}).get("event_type") == "card.action.trigger":
        context = (payload.get("event") or {}).get("context") or {}
        origin = _card_origin(context.get("open_message_id", ""))
        if not origin:
            return {"toast": {"type": "error", "content": "这张卡片已失效。"}}
        result = await _route_to_client(origin, payload, timeout=2.5)
        if isinstance(result, dict) and ("toast" in result or "card" in result):
            return result
        return {"toast": {"type": "error", "content": "Aria 本机未及时响应，请稍后再试。"}}

    # Feishu retries an event it did not get an answer to within about 3 s.
    # Forwarding waits for the machine (up to MESSAGE_TIMEOUT), so the answer
    # goes back now and the work runs after it; a retry of an event already
    # taken is acknowledged and dropped instead of reaching the machine twice.
    if _seen_event(payload):
        return {"code": 0}
    background.add_task(_handle_message, payload)
    return {"code": 0}


_EVENT_TTL = 8 * 3600     # Feishu's last retry comes about six hours after the first try
_seen_events: dict[str, float] = {}   # event id → when it may be forgotten


def _seen_event(payload: dict) -> bool:
    """True when this event was already taken; records it otherwise."""
    event_id = str((payload.get("header") or {}).get("event_id") or payload.get("uuid") or "")
    if not event_id:
        return False
    now = time.time()
    for stale in [key for key, expires in _seen_events.items() if expires < now]:
        _seen_events.pop(stale, None)
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now + _EVENT_TTL
    return False


async def _handle_message(payload: dict) -> None:
    """A message event, after Feishu has had its answer: bind, or forward."""
    try:
        await _deliver(payload)
    except Exception:
        logger.exception("handling a Feishu event failed")


async def _deliver(payload: dict) -> None:
    # Extract sender + message_id
    event = payload.get("event", {})
    message = event.get("message", {})
    sender  = event.get("sender", {})
    feishu_user_id = sender.get("sender_id", {}).get("open_id", "")
    message_id     = message.get("message_id", "")

    if not feishu_user_id:
        return

    # Handle /bind command
    msg_type = message.get("message_type", "")
    if msg_type == "text":
        try:
            text_content = json.loads(message.get("content", "{}")).get("text", "").strip()
        except Exception:
            text_content = ""

        if text_content.upper().startswith("/BIND ") or text_content.upper().startswith("ARIA-BIND-"):
            raw_code = text_content.upper().replace("/BIND ", "").strip().replace("ARIA-BIND-", "")
            # Only the code generated and shown on the machine binds it. The
            # client_id is not accepted any more: it was the bind code, so
            # whoever learned one could route their messages to another
            # person's machine.
            client_id = _client_for_bind_code(raw_code)
            if not client_id:
                await _send_feishu_text(
                    feishu_user_id,
                    "❌ 绑定码无效。请在你电脑上运行 aria-code 的配置向导，使用它显示的绑定码。")
                return
            _bind(feishu_user_id, client_id)
            await _send_feishu_text(
                feishu_user_id,
                "✅ 绑定成功！你的 Aria 实例已连接。\n现在可以直接发消息与你的 Aria 交互了。"
            )
            return

    # Route to local aria instance
    result = await _route_to_local(feishu_user_id, payload)

    if result is None:
        # No binding found or client offline
        if not _lookup_client(feishu_user_id):
            await _send_feishu_text(
                feishu_user_id,
                "👋 你好！要开始使用 Aria，请：\n"
                "1. 在你的电脑上安装 Aria Code\n"
                "2. 运行 `python3 setup_wizard.py` 完成配置\n"
                "3. 发送绑定码绑定你的账户"
            )
        else:
            await _reply_feishu(
                message_id,
                "⚠️ Aria 本机未连接。请确保你的电脑上 `aria_relay_client.py` 正在运行。",
                color="yellow",
            )


# ── Status endpoint ───────────────────────────────────────────────────────────

@app.get("/status")
async def status(request: Request):
    """健康检查。**不再无条件列出 client_ids**——它就是 /bind 的绑定凭证，
    公开等于把劫持所需的最后一块拼图直接送出去（见 _verify_feishu_request
    上方记录的攻击链）。带正确 RELAY_SECRET 时才返回明细，否则只给计数。
    """
    body = {
        "connected_clients": len(_connections),
        "total_bindings": _store().binding_count(),
        "store": _store().kind,
        **({"store_warning": "sqlite on Cloud Run — bindings are lost on every deploy; "
                             "set RELAY_STORE=firestore"} if _ephemeral_store() else {}),
        "feishu_app_configured": bool(_FEISHU_APP_ID),
        "event_verification": (
            "encrypt_key" if _FEISHU_ENCRYPT_KEY
            else "verification_token" if _FEISHU_VERIFICATION_TOKEN
            else "DISABLED (unverified events allowed)" if _ALLOW_UNVERIFIED_EVENTS
            else "enforced (events rejected: no key configured)"
        ),
    }
    presented = request.headers.get("x-relay-secret", "")
    if _RELAY_SECRET and hmac.compare_digest(presented, _RELAY_SECRET):
        body["client_ids"] = list(_connections.keys())
    return body


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "aria_code.aria_relay_server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8765")),
        reload=False,
        log_level="info",
    )
