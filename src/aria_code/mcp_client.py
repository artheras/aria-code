"""
mcp_client.py — MCP (Model Context Protocol) client for Aria Code.

Connects to any MCP server via stdio transport and exposes its tools as
first-class LOCAL_TOOLS in the Aria Code tool loop.

Config file: ~/.arthera/mcp_servers.json
Example::

    {
      "servers": [
        {
          "name": "quant_engine",
          "command": "python3",
          "args": ["/path/to/mcp_server.py"],
          "env": {"PYTHONPATH": "/path/to/project"},
          "description": "Arthera quant engine MCP tools"
        },
        {
          "name": "filesystem",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/projects"],
          "description": "Filesystem access"
        },
        {
          "name": "apify",
          "command": "npx",
          "args": ["-y", "@apify/actors-mcp-server@latest", "--tools", "actors,docs,apify/rag-web-browser"],
          "env": {"APIFY_TOKEN": "${APIFY_TOKEN}"},
          "enabled": false,
          "description": "Approved Apify web research Actors (enable only after token and data-use review)"
        }
      ]
    }

Usage::

    registry = MCPToolRegistry()
    await registry.start_all()
    registry.register_into(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)

    # Tools are now callable as regular LOCAL_TOOLS entries
    result = await registry.call_tool("quant_engine/backtest_strategy", {"symbol": "AAPL"})
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from aria_code.packages.aria_mcp.circuit import ServerCircuit
from aria_code.packages.aria_core.paths import aria_home

logger = logging.getLogger(__name__)

MCP_CONFIG_PATH = aria_home() / "mcp_servers.json"

# MCP JSON-RPC protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"

# Marks a call that failed because the server could not be reached at all
# (timeout, closed pipe, exited process). Only these, and the explicit
# server-side "could not produce a result" codes below, count toward the
# per-server circuit breaker; a tool refusing its input does not.
TRANSPORT_FAILURE = "transport"
SERVER_UNAVAILABLE_CODES = frozenset({"source_unavailable", "source_timeout"})


def model_safe_tool_name(server_name: str, tool_name: str) -> str:
    """Return an OpenAI/Ollama-compatible qualified MCP function name."""
    server = re.sub(r"[^A-Za-z0-9_-]+", "_", str(server_name)).strip("_") or "server"
    tool = re.sub(r"[^A-Za-z0-9_-]+", "_", str(tool_name)).strip("_") or "tool"
    return f"mcp__{server}__{tool}"


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _make_request(method: str, params: Any = None, req_id: int = 1) -> bytes:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


def _make_notification(method: str, params: Any = None) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


# ---------------------------------------------------------------------------
# MCPServer — one subprocess connection
# ---------------------------------------------------------------------------

class MCPServer:
    """
    Manages a single MCP server subprocess (stdio transport).

    Lifecycle: start() → list_tools() → call_tool(…) → stop()
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
        description: str = "",
        timeout: float = 30.0,
    ):
        self.name        = name
        self.command     = command
        self.args        = args
        self.env         = env or {}
        self.description = description
        self.timeout     = timeout

        self._proc:    Optional[asyncio.subprocess.Process] = None
        self._req_id:  int  = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._tools:   List[Dict[str, Any]] = []
        self._running  = False

    # ── internal ───────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, data: bytes):
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _reader_loop(self):
        """Background task: read lines from server stdout and dispatch to futures."""
        if not self._proc or not self._proc.stdout:
            return
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("[MCP:%s] Non-JSON: %s", self.name, line[:100])
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            return  # stop() cancelled us; nothing is waiting on an answer
        except Exception as exc:
            logger.debug("[MCP:%s] Reader loop error: %s", self.name, exc)
        # stdout closed: the server has exited. Nothing can answer a request that
        # is still waiting, so fail those now. Without this, a server that
        # refuses to start (a missing environment variable, say) is waited on
        # for the whole timeout, 30 s at every launch, and the reason it gave on
        # stderr is never shown.
        await self._fail_pending()

    async def _fail_pending(self):
        reason = await self._exit_reason()
        for rid, fut in list(self._pending.items()):
            self._pending.pop(rid, None)
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server {self.name!r} exited before answering{reason}"))

    async def _exit_reason(self) -> str:
        """Exit code and the server's own last words, for the error message."""
        proc = self._proc
        if proc is None:
            return ""
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            code = None
        said = ""
        try:
            if proc.stderr is not None:
                said = (await asyncio.wait_for(proc.stderr.read(2000), timeout=1)).decode("utf-8", errors="ignore").strip()
        except Exception:
            pass
        said = " ".join(said.split())[:300]
        detail = f" (exit {code})" if code is not None else ""
        return f"{detail}: {said}" if said else detail

    async def _request(self, method: str, params: Any = None) -> Any:
        if self._proc is not None and self._proc.returncode is not None:
            raise RuntimeError(f"MCP server {self.name!r} has exited (exit {self._proc.returncode})")
        rid = self._next_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send(_make_request(method, params, rid))
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(rid, None)
            raise RuntimeError(f"MCP server {self.name!r} closed its input: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"MCP {self.name}/{method} timed out after {self.timeout}s")

    # ── public API ─────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Start the server subprocess and perform MCP handshake."""
        if self._running:
            return True
        try:
            env = {**os.environ, **self.env}
            self._proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            logger.warning("[MCP:%s] Command not found: %s", self.name, exc)
            return False
        except Exception as exc:
            logger.warning("[MCP:%s] Failed to start: %s", self.name, exc)
            return False

        self._reader_task = asyncio.create_task(self._reader_loop())

        # MCP initialize handshake
        try:
            await self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities":    {"tools": {}},
                "clientInfo":      {"name": "aria-code", "version": "3.0"},
            })
            await self._send(_make_notification("notifications/initialized"))
        except Exception as exc:
            logger.warning("[MCP:%s] Handshake failed: %s", self.name, exc)
            await self.stop()
            return False

        # Discover tools
        await self._refresh_tools()
        self._running = True
        logger.info("[MCP:%s] Started with %d tools", self.name, len(self._tools))
        return True

    async def _refresh_tools(self):
        try:
            result   = await self._request("tools/list")
            self._tools = result.get("tools", []) if result else []
        except Exception as exc:
            logger.debug("[MCP:%s] tools/list failed: %s", self.name, exc)
            self._tools = []

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool on this server.  Returns {"success": bool, "result": …}."""
        if not self._running:
            return {"success": False, "error": f"MCP server {self.name!r} not running",
                    "failure_kind": TRANSPORT_FAILURE}
        # Strip server prefix if present (e.g. "quant_engine/backtest_strategy")
        short_name = tool_name.split("/")[-1]
        try:
            result = await self._request("tools/call", {
                "name":      short_name,
                "arguments": arguments,
            })
            if result is None:
                return {"success": True, "result": None}
            if not isinstance(result, dict):
                return {"success": False, "error": "Invalid MCP tool result"}
            if "isError" in result and not isinstance(result["isError"], bool):
                return {"success": False, "error": "Invalid MCP tool error flag"}
            if result.get("isError"):
                error_content = result.get("content", [])
                detail = "\n".join(str(item.get("text", "")) for item in error_content
                                   if isinstance(item, dict)) if isinstance(error_content, list) else ""
                return {"success": False, "error_code": "mcp_tool_error",
                        "error": detail[:4000] or "MCP tool reported an error"}
            # MCP returns {content: [{type, text}]} or plain value
            content = result.get("content", result)
            if isinstance(content, list):
                parts = [c.get("text", "") if isinstance(c, dict) else str(c) for c in content]
                text  = "\n".join(parts)
                try:
                    parsed = json.loads(text)
                except Exception:
                    return {"success": True, "result": text}
                if isinstance(parsed, dict):
                    # Only this client decides what was a transport failure;
                    # a server cannot claim one (or hide one) via its payload.
                    parsed.pop("failure_kind", None)
                    return {"success": True, **parsed}
                return {"success": True, "result": parsed}
            return {"success": True, "result": content}
        except Exception as exc:
            # Timeout, closed pipe, exited process: the server itself failed.
            return {"success": False, "error": str(exc), "failure_kind": TRANSPORT_FAILURE}

    @property
    def is_alive(self) -> bool:
        """Process exists and has not exited (handshake state tracked separately)."""
        return self._proc is not None and self._proc.returncode is None

    async def restart(self) -> bool:
        """Tear down and re-launch the subprocess (reconnect policy)."""
        await self.stop()
        return await self.start()

    async def stop(self):
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.0)
            except Exception:
                pass
            self._reader_task = None
        if self._proc:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


# ---------------------------------------------------------------------------
# MCPToolRegistry — manages multiple servers + tool registration
# ---------------------------------------------------------------------------

class MCPToolRegistry:
    """
    Loads MCP server config from ``~/.arthera/mcp_servers.json``, starts
    each server, discovers tools, and registers them in the CLI's tool loop.

    Internal naming convention: ``{server_name}/{tool_name}``.
    Model-facing schemas use ``mcp__{server_name}__{tool_name}`` because tool
    APIs reject slashes in function names.
    This avoids collisions when two servers expose a tool with the same name.
    """

    def __init__(self, config_path: pathlib.Path = MCP_CONFIG_PATH):
        self.config_path = config_path
        self._servers:   Dict[str, MCPServer] = {}
        self._tool_map:  Dict[str, Tuple[str, str]] = {}  # qualified_name → (server_name, tool_name)
        self._circuits:  Dict[str, "ServerCircuit"] = {}  # per-server failure isolation
        self._loaded     = False
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    # ── config ─────────────────────────────────────────────────────────────

    def load_config(self) -> List[Dict[str, Any]]:
        if not self.config_path.exists():
            return []
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            return data.get("servers", [])
        except Exception as exc:
            logger.warning("MCP config load failed: %s", exc)
            return []

    def save_example_config(self):
        """Write an example mcp_servers.json if none exists."""
        if self.config_path.exists():
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        example = {
            "servers": [
                {
                    "name":        "quant_engine",
                    "command":     "python3",
                    "args":        ["path/to/mcp_server.py"],
                    "env":         {"PYTHONPATH": "path/to/project"},
                    "description": "Your Arthera quant engine (backtest, signals, factors)"
                },
                {
                    "name": "apify",
                    "command": "npx",
                    "args": ["-y", "@apify/actors-mcp-server@latest", "--tools", "actors,docs,apify/rag-web-browser"],
                    "env": {"APIFY_TOKEN": "${APIFY_TOKEN}"},
                    "enabled": False,
                    "description": "Apify web research (review Actor terms and enable explicitly)"
                }
            ]
        }
        with open(self.config_path, "w") as f:
            json.dump(example, f, indent=2, ensure_ascii=False)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start_all(self) -> Dict[str, bool]:
        """Start all configured MCP servers.  Returns {name: started_ok}."""
        self._event_loop = asyncio.get_running_loop()
        server_configs = self.load_config()
        results = {}
        for cfg in server_configs:
            name    = cfg.get("name", "unnamed")
            command = cfg.get("command", "")
            args    = cfg.get("args", [])
            env     = cfg.get("env", {})
            desc    = cfg.get("description", "")
            if not command:
                continue
            if not cfg.get("enabled", True):
                logger.debug("MCP server %r disabled in config, skipping", name)
                continue
            # expand ${VAR} placeholders in args and env values
            def _expand(s: str) -> str:
                import re as _re
                return _re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), s)
            args = [_expand(a) for a in args]
            env  = {k: _expand(v) for k, v in env.items() if v}
            srv = MCPServer(name=name, command=command, args=args, env=env, description=desc)
            ok  = await srv.start()
            if ok:
                self._servers[name] = srv
                for tool in srv.tools:
                    qualified = f"{name}/{tool['name']}"
                    self._tool_map[qualified] = (name, tool["name"])
            results[name] = ok
        self._loaded = True
        return results

    async def stop_all(self):
        for srv in self._servers.values():
            await srv.stop()
        self._servers.clear()
        self._tool_map.clear()
        self._loaded = False
        self._event_loop = None

    # ── tool info ──────────────────────────────────────────────────────────

    def all_tools(self) -> List[Dict[str, Any]]:
        """Return all discovered tools with qualified names."""
        result = []
        for srv_name, srv in self._servers.items():
            for tool in srv.tools:
                t = dict(tool)
                t["qualified_name"] = f"{srv_name}/{tool['name']}"
                t["server"] = srv_name
                result.append(t)
        return result

    # ── call ───────────────────────────────────────────────────────────────

    def _circuit_for(self, srv_name: str) -> ServerCircuit:
        circuit = self._circuits.get(srv_name)
        if circuit is None:
            circuit = self._circuits[srv_name] = ServerCircuit()
        return circuit

    async def call_tool(self, qualified_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if "/" in qualified_name:
            srv_name, tool_name = qualified_name.split("/", 1)
        else:
            # Try to find by short name
            srv_name, tool_name = self._resolve_short_name(qualified_name)

        srv = self._servers.get(srv_name)
        if srv is None:
            return {"success": False, "error": f"MCP server {srv_name!r} not found"}

        # ToolExecutor adds its own context to every call (_run_id,
        # _session_id, _skip_confirm, ...) for local tools to read. An MCP
        # server only knows the parameters in its schema, and a strict one
        # rejects the whole call over an extra keyword, so they stop here.
        arguments = {k: v for k, v in (arguments or {}).items() if not str(k).startswith("_")}

        # Per-server failure isolation: a wedged/dead server fails fast instead
        # of costing every call its full request timeout for the whole session.
        circuit = self._circuit_for(srv_name)
        if not circuit.allow_call():
            return {
                "success": False,
                "error": (
                    f"MCP server {srv_name!r} temporarily disabled after repeated "
                    f"failures ({circuit.describe()}). It will be probed again "
                    f"automatically; /mcp reload {srv_name} forces a restart now."
                ),
                "circuit": circuit.state,
            }

        # Reconnect policy: if the subprocess died, use this (probe) call to
        # relaunch it before dispatching.
        if not srv.is_alive:
            logger.info("[MCP:%s] Process dead — attempting restart", srv_name)
            if not await srv.restart():
                circuit.record_failure()
                return {
                    "success": False,
                    "error": f"MCP server {srv_name!r} is down and restart failed",
                    "circuit": circuit.state,
                }

        result = await srv.call_tool(tool_name, arguments)

        # 上游限流(429/too many requests):短退避后重试一次。
        # 与 market_data_client 的报价路径同策略,MCP 工具在此统一收口。
        if not result.get("success") and self._looks_rate_limited(result):
            logger.warning(
                "[MCP:%s] %s rate-limited upstream; retrying once in 3s",
                srv_name, tool_name,
            )
            await asyncio.sleep(3.0)
            result = await srv.call_tool(tool_name, arguments)

        if result.get("success"):
            circuit.record_success()
        elif self._looks_rate_limited(result):
            # 限流是上游数据源配额问题,不是本服务器故障——不计入熔断,
            # 否则一波限流会把整个服务器禁用,殃及无关工具。错误仍如实上抛。
            pass
        elif self._counts_as_server_failure(result):
            circuit.record_failure()
        else:
            # A tool-level refusal (invalid_input, network_disabled,
            # broker_access_disabled, an MCP isError result, ...) means the
            # server is up and answered. Counting those opened the circuit
            # after three bad arguments and disabled every tool on the server.
            # The server answered, so it is healthy as far as the breaker cares.
            circuit.record_success()
        return result

    @staticmethod
    def _counts_as_server_failure(result: Dict[str, Any]) -> bool:
        """True when a failed call says the server, not the request, is broken."""
        if result.get("failure_kind") == TRANSPORT_FAILURE:
            return True
        return str(result.get("error_code") or "") in SERVER_UNAVAILABLE_CODES

    @staticmethod
    def _looks_rate_limited(result: Dict[str, Any]) -> bool:
        text = str(result.get("error") or result.get("result") or "").lower()
        return any(t in text for t in ("rate limit", "rate-limit", "rate limited", "too many requests", "429"))

    async def reload_server(self, srv_name: str) -> bool:
        """Force stop/start of one server and reset its circuit (reload policy)."""
        srv = self._servers.get(srv_name)
        if srv is None:
            return False
        ok = await srv.restart()
        if ok:
            self._circuit_for(srv_name).record_success()
            self._tool_map = {q: st for q, st in self._tool_map.items() if st[0] != srv_name}
            for tool in srv.tools:
                self._tool_map[f"{srv_name}/{tool['name']}"] = (srv_name, tool["name"])
        return ok

    def _resolve_short_name(self, name: str) -> Tuple[str, str]:
        for qname, (srv, tool) in self._tool_map.items():
            if tool == name:
                return srv, tool
        return "unknown", name

    # ── registration ───────────────────────────────────────────────────────

    def register_into(
        self,
        tool_registry: Dict,
        schema_registry: List,
        overwrite: bool = False,
    ) -> int:
        """
        Add all MCP tools into the CLI's LOCAL_TOOLS and LOCAL_TOOL_SCHEMAS.

        Model-facing tool names use ``mcp__server__tool`` while handlers retain
        the MCP server's internal ``server/tool`` qualified name.

        Returns number of tools registered.
        """
        import asyncio as _asyncio

        added = 0
        existing_names = set(tool_registry.keys())

        for srv_name, srv in self._servers.items():
            for tool in srv.tools:
                qname = f"{srv_name}/{tool['name']}"
                model_name = model_safe_tool_name(srv_name, tool["name"])
                if model_name in existing_names and not overwrite:
                    continue

                # ToolExecutor invokes synchronous handlers in a worker thread.
                # Route the coroutine back to the event loop that owns the MCP
                # subprocess streams; asyncio.run() in the worker creates a
                # different loop and fails with cross-loop Future errors.
                def _make_sync_handler(qn: str) -> Callable:
                    def _handler(params: dict) -> dict:
                        try:
                            current_loop = _asyncio.get_running_loop()
                        except RuntimeError:
                            current_loop = None
                        owner_loop = self._event_loop
                        if owner_loop and owner_loop.is_running():
                            if current_loop is owner_loop:
                                return {
                                    "success": False,
                                    "error": "MCP sync handler called on its owner event loop; use ToolExecutor.execute",
                                }
                            future = _asyncio.run_coroutine_threadsafe(
                                self.call_tool(qn, params), owner_loop
                            )
                            return future.result(timeout=60)
                        return _asyncio.run(self.call_tool(qn, params))
                    return _handler

                tool_registry[model_name] = (
                    _make_sync_handler(qname),
                    tool.get("description", f"MCP tool from {srv_name}"),
                )
                existing_names.add(model_name)
                added += 1

                # Build schema
                input_schema = tool.get("inputSchema") or tool.get("parameters") or {
                    "type": "object", "properties": {}, "required": []
                }
                schema = {
                    "type": "function",
                    "function": {
                        "name":        model_name,
                        "description": f"[{srv_name}] {tool.get('description', '')}",
                        "parameters":  input_schema,
                    },
                }
                # Replace in place rather than append. The handler dict above
                # is keyed by name so re-registering is idempotent there, but
                # the schema list is not — reconnecting MCP (which core_cmds
                # does with overwrite=True) appended a second copy of every
                # tool. Most providers tolerate that and silently spend the
                # context; Vertex rejects the whole request with "Duplicate
                # function declaration found".
                for index, existing in enumerate(schema_registry):
                    if (existing.get("function") or existing).get("name") == model_name:
                        schema_registry[index] = schema
                        break
                else:
                    schema_registry.append(schema)

        return added

    # ── async call tool (for use inside async context) ─────────────────────

    async def call_tool_async(
        self,
        qualified_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self.call_tool(qualified_name, arguments)

    # ── status ─────────────────────────────────────────────────────────────

    def status(self) -> List[Dict[str, Any]]:
        return [
            {
                "name":        name,
                "running":     srv._running,
                "alive":       srv.is_alive,
                "circuit":     self._circuit_for(name).describe(),
                "tool_count":  len(srv.tools),
                "description": srv.description,
                "tools":       [t["name"] for t in srv.tools],
            }
            for name, srv in self._servers.items()
        ]


# ---------------------------------------------------------------------------
# Module-level singleton (lazy-initialized)
# ---------------------------------------------------------------------------

_registry: Optional[MCPToolRegistry] = None


def get_registry() -> MCPToolRegistry:
    global _registry
    if _registry is None:
        _registry = MCPToolRegistry()
    return _registry


async def init_mcp(tool_registry: Dict, schema_registry: List) -> Dict[str, bool]:
    """
    Convenience function: load config, start all servers, register tools.
    Call once at startup.

    Returns {server_name: started_ok} dict.
    """
    reg     = get_registry()
    results = await reg.start_all()
    n       = reg.register_into(tool_registry, schema_registry)
    if n > 0:
        logger.info("MCP: registered %d tools from %d servers", n, len(results))
    return results
