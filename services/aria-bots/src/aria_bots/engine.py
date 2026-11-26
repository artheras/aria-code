"""Run one chat turn on the Go engine (`aria-runtime stream`).

The bots used to shell out to aria_cli.py. Now a turn is

    aria-runtime stream --format=jsonl --quiet --no-banner --prompt=...

in a workspace of its own, read as JSONL until `turn.completed`. The engine
runs headless: a tool that needs approval is refused, because nobody in the
chat can answer the prompt. An operator grants named tools with
ARIA_BOT_ALLOW_TOOLS.

The logistics tools reach the engine as an MCP server the bots register
through ARIA_MCP_EXTRA_CONFIG, so the user's own mcp_servers.json is left
alone. A conversation bound to a shipper passes ARIA_OWNER_SCOPE down; the
engine hands its environment to the MCP server, and erp-service enforces it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional

from .erp_logistics import OWNER_SCOPE_ENV

ENGINE_ENV = "ARIA_RUNTIME"
MAX_PROMPT = 65536
LOGISTICS_SERVER = "aria-logistics"


def state_dir() -> Path:
    base = os.environ.get("ARIA_BOTS_HOME", "").strip()
    return Path(base).expanduser() if base else Path.home() / ".aria" / "bots"


def find_engine() -> Optional[str]:
    """ARIA_RUNTIME when it names an aria-runtime, else aria-runtime or aria-native on PATH."""
    configured = os.environ.get(ENGINE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.name.removesuffix(".exe").startswith("aria-runtime") and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    runtime = shutil.which("aria-runtime")
    if runtime:
        return runtime
    native_env = os.environ.get("ARIA_NATIVE_BINARY", "").strip()
    if native_env:
        npath = Path(native_env).expanduser()
        if npath.is_file() and os.access(npath, os.X_OK):
            return str(npath)
    return shutil.which("aria-native")


def mcp_config() -> Path:
    """The servers the bots add to every engine turn (written atomically when changed)."""
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    server = {
        "name": LOGISTICS_SERVER,
        "command": sys.executable,
        "args": ["-m", "aria_bots.erp_logistics"],
        "description": "Freight analysis, carrier scoring and inventory policy, scoped per shipper",
    }
    path = folder / "mcp_servers.json"
    rendered = json.dumps({"servers": [server]}, indent=2)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == rendered:
            return path
    except OSError:
        pass
    tmp = folder / f".mcp_servers.{os.getpid()}.tmp"
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(path)
    return path


def allowed_tools() -> list[str]:
    return [t.strip() for t in os.environ.get("ARIA_BOT_ALLOW_TOOLS", "").split(",") if t.strip()]


def with_history(prompt: str, history: Optional[Iterable[dict]]) -> str:
    """The engine takes one prompt; earlier messages go in front of it."""
    lines = []
    for item in history or []:
        role = "Assistant" if item.get("role") == "assistant" else "User"
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    if not lines:
        return prompt
    return "Earlier in this conversation:\n" + "\n".join(lines) + "\n\nNow answer:\n" + prompt


def command(engine: str, prompt: str) -> list[str]:
    argv = [engine, "stream", "--format=jsonl", "--quiet", "--no-banner", f"--prompt={prompt}"]
    tools = allowed_tools()
    if tools:
        argv += ["--allow-tools", ",".join(tools)]
    return argv


def environment(owner: Optional[str]) -> dict[str, str]:
    env = {**os.environ, "ARIA_BOT_MODE": "1", "ARIA_MCP_EXTRA_CONFIG": str(mcp_config())}
    env.pop(OWNER_SCOPE_ENV, None)
    if owner:
        env[OWNER_SCOPE_ENV] = owner
    env.setdefault("ARIA_MCP_ACTOR", f"aria-bot:{owner}" if owner else "aria-bot")
    return env


def record_bot_usage(owner: Optional[str], ok: bool, prompt_bytes: int) -> None:
    """Append a lightweight per-owner usage record to `<ARIA_BOTS_HOME>/usage.jsonl`."""
    try:
        folder = state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "owner": owner or "anonymous",
            "ok": ok,
            "prompt_bytes": prompt_bytes,
            "licensed": bool(os.environ.get("ARIA_LICENSE_KEY", "").strip()),
        }
        with (folder / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_answer(stdout: bytes) -> tuple[bool, str]:
    """(success, answer) from the engine's JSONL; deltas when no final text."""
    deltas: list[str] = []
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "answer.delta":
            deltas.append(str(event.get("text") or event.get("delta") or ""))
        elif kind == "turn.completed":
            answer = str(event.get("response") or "".join(deltas)).strip()
            return bool(event.get("success")), answer or str(event.get("error") or "")
    return False, "".join(deltas).strip()


async def run_turn(prompt: str, *, engine: str, owner: Optional[str] = None,
                   history: Optional[Iterable[dict]] = None, timeout: float = 120) -> str:
    """The answer text, or a message starting with ❌ / ⏱️ when there is none."""
    prompt = with_history(prompt, history)
    if len(prompt.encode("utf-8")) > MAX_PROMPT:
        return "❌ 消息太长，请缩短后重试。"
    workspace = state_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *command(engine, prompt),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace),
        env=environment(owner),
    )
    try:
        # One newline acknowledges the host; EOF after it, so nothing can wait on input.
        stdout, stderr = await asyncio.wait_for(proc.communicate(b"\n"), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"⏱️ 查询超时（>{int(timeout)}s），请简化问题后重试。"
    ok, answer = read_answer(stdout)
    succeeded = bool(answer and (ok or proc.returncode == 0))
    record_bot_usage(owner, succeeded, len(prompt.encode("utf-8")))
    if succeeded:
        return answer[:3000]
    detail = answer or stderr.decode("utf-8", errors="replace").strip()[-500:] or f"exit {proc.returncode}"
    return f"❌ Aria 引擎未能完成：{detail}"
