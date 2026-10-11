"""Persistent terminal application service. The host owns the TTY.

Bidirectional UTF-8 JSONL, protocol 1: submit/cancel a turn, answer a menu or
input request, resume a session, and shut down. Inference, commands, task
isolation, risk assessment, tool approvals and persistence stay in the existing
ArtheraTerminal. Only its presentation/input adapters are replaced here.
"""
from __future__ import annotations

import argparse
import asyncio
import builtins
import getpass
import io
import json
import queue
import re
import sys
import threading
import uuid
from pathlib import Path

MAX_MESSAGE = 1_048_576
MAX_INPUT = 65_536
PROTOCOL = 1


def session_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("Invalid session ID")
    return value


class Output(io.TextIOBase):
    """Capture legacy command/Rich output; never write it on the protocol pipe."""
    def __init__(self, server):
        self.server = server

    def write(self, text):
        if text:
            # Bound an individual event even when a legacy command prints a file.
            for offset in range(0, len(text), 16_384):
                self.server.emit("output.delta", text=text[offset:offset + 16_384])
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class Server:
    def __init__(self, incoming, outgoing, options):
        self.incoming, self.outgoing, self.options = incoming, outgoing, options
        self.lock = threading.Lock()
        self.requests = queue.Queue(maxsize=32)
        self.dialog_lock = threading.Lock()
        self.dialog = None
        self.turn_id = None
        self.task = None
        self.loop = None
        self.stopping = threading.Event()
        self.terminal = None
        self.cli = None
        self.background = []
        self.tool_lock = threading.Lock()
        self.active_tools = 0
        self.cancel_requested = False

    def emit(self, kind, **fields):
        record = {"type": kind, "protocol": PROTOCOL, "turn_id": self.turn_id, **fields}
        encoded = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            raise ValueError("Application event exceeds 1 MiB")
        with self.lock:
            self.outgoing.write(encoded)
            self.outgoing.flush()

    def ask(self, kind, **fields):
        ident = uuid.uuid4().hex
        replies = queue.Queue(maxsize=1)
        with self.dialog_lock:
            self.dialog = (ident, replies)
        self.emit(kind, request_id=ident, **fields)
        try:
            value = replies.get()
            if value is None:
                raise KeyboardInterrupt
            return value
        finally:
            with self.dialog_lock:
                self.dialog = None
            self.emit("dialog.closed", request_id=ident)

    def input(self, prompt="", *, secret=False):
        value = self.ask("input.requested", prompt=str(prompt), secret=secret)
        text = value.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT:
            raise ValueError("Invalid input response")
        return text

    def select(self, options, selected=0, title="", **kwargs):
        # The existing approval function supplies its actual options. Returning
        # an index to it preserves deny lists, L4 ask-every-time, hooks, symbol
        # previews and the exact scope of session grants.
        choices = [[str(label), str(help_text)] for label, help_text in options]
        answer = self.ask("approval.requested", title=str(title or "Approval / selection"),
                          choices=choices, selected=selected,
                          shortcuts=kwargs.get("shortcuts", {}))
        chosen = answer.get("choice", -1)
        if type(chosen) is not int or not -1 <= chosen < len(choices):
            raise ValueError("Invalid menu response")
        return chosen

    def abort(self):
        with self.dialog_lock:
            if self.dialog:
                try:
                    self.dialog[1].put_nowait(None)
                except queue.Full:
                    pass
        if self.loop:
            def cancel():
                if self.terminal and self.terminal.cancel_event:
                    self.terminal.cancel_event.set()
                if self.task and not self.task.done() and not self.cancel_requested:
                    self.cancel_requested = True
                    self.task.cancel()
            self.loop.call_soon_threadsafe(cancel)

    def read_requests(self):
        try:
            while not self.stopping.is_set():
                raw = self.incoming.readline(MAX_MESSAGE + 1)
                if not raw:
                    break
                if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
                    self.emit("protocol.error", error="Request must be a JSONL line of at most 1 MiB")
                    break
                try:
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict) or type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL:
                        raise ValueError("Unsupported application protocol")
                    kind = value.get("type")
                    if kind == "dialog.respond":
                        with self.dialog_lock:
                            if not self.dialog or value.get("request_id") != self.dialog[0] or value.get("turn_id") != self.turn_id:
                                raise ValueError("Stale or unknown dialog")
                            self.dialog[1].put_nowait(value)
                        continue
                    if kind == "turn.cancel":
                        if value.get("turn_id") != self.turn_id or not self.turn_id:
                            raise ValueError("Stale or unknown turn")
                        self.abort()
                        continue
                    if kind == "shutdown":
                        break
                    self.requests.put_nowait(value)
                except (ValueError, UnicodeError, queue.Full) as error:
                    self.emit("protocol.error", error=str(error))
        finally:
            self.stopping.set()
            self.abort()
            # Wake the dispatcher even if the queue is full.
            try:
                self.requests.put_nowait({"type": "shutdown"})
            except queue.Full:
                pass

    def initialize(self):
        import aria_cli as cli
        from rich.console import Console
        from aria_code.apps.cli.runtime_consumer import TerminalRuntimeEventConsumer
        from aria_code.apps.cli.exec_events import ExecEvents

        self.cli = cli
        output = Output(self)
        # Console captures markup/tables without ANSI. Command modules get the
        # same console through AriaContext. Model-visible reasoning is never
        # sent as answer/output events by this consumer.
        sys.stdout = output
        sys.stderr = output
        cli.console = Console(file=output, force_terminal=False, color_system=None, width=100)
        cli.HAS_RICH = True
        builtins.input = self.input
        getpass.getpass = lambda prompt="Password: ", stream=None: self.input(prompt, secret=True)
        cli.console.input = lambda prompt="", **kw: self.input(prompt, secret=bool(kw.get("password")))
        cli._arrow_select = self.select
        cli._render_answer_block = lambda text, *a, **kw: self.emit("answer.replace", text=str(text))
        server = self

        class Events(ExecEvents):
            def __init__(self):
                self.streaming = True
            def emit(self, kind, **fields):
                server.emit(kind, **fields)

        events = Events()

        class Consumer(TerminalRuntimeEventConsumer):
            def __init__(self, **kwargs):
                kwargs.update(has_rich=False, action_view=None, print_tool_call=None,
                              print_tool_done=None, on_response_start=None,
                              set_robot_state=None)
                super().__init__(**kwargs)
            def on_token(self, token):
                self.first_token_received = True
                self.streamed_any = True
                self.token_count += 1
                self.response_text += token
                events.text_delta(token)
            def on_thinking(self, content):
                # No hidden reasoning in the protocol/transcript.
                self.thinking_tokens += 1
                self.set_phase(cli.TurnPhase.THINKING)
            def on_tool_call(self, tool, params):
                events.tool_started(tool, params)
            def on_tool_result(self, tool, result):
                events.tool_completed(tool, result)
            def on_status(self, state, message):
                events.status(state, message)
            def stop_live(self, discard=False):
                self.stop_spinner()

        cli.TerminalRuntimeEventConsumer = Consumer
        config = cli.load_config()
        config["_session_workspace_root"] = str(Path.cwd().resolve())
        config["_session_write_roots"] = [str(Path(p).expanduser().resolve()) for p in self.options.add_dir]
        config["_session_read_roots"] = [str(Path(p).expanduser().resolve()) for p in self.options.read_dir]
        if self.options.resume_last and not self.options.resume:
            self.options.resume = config.get("last_session_id")
            if not self.options.resume:
                raise ValueError("No last session to resume")
        resumed = self.load_session(self.options.resume) if self.options.resume else None
        config["last_session_id"] = self.options.resume or uuid.uuid4().hex[:8]
        if self.options.model:
            model = self.options.model
            if "/" in model and not model.startswith("http"):
                from aria_code.apps.cli.providers.chat_routing import normalize_provider_name, KNOWN_MODEL_PROVIDERS
                provider, model = model.split("/", 1)
                provider = normalize_provider_name(provider)
                if provider in KNOWN_MODEL_PROVIDERS:
                    config["backend_chat"] = False
                config.update(model=model, local_provider=provider,
                              local_mode=provider in {"ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom"})
            else:
                key = cli.resolve_model_key(model)
                config.update(model=cli.MODELS[key]["id"] if key in cli.MODELS else model, local_provider="ollama")
        if self.options.url:
            config["api_url"] = self.options.url
        if self.options.thinking:
            config["thinking_mode"] = "thinking"
        if self.options.local:
            config["local_mode"] = True
        cli._auto_approve_session = self.options.dangerously_skip_permissions
        cli._session_always_allow.update(t.strip() for t in self.options.allow_tools.split(",") if t.strip())
        self.terminal = self.make_terminal(config)
        if resumed:
            self.terminal.conversation = resumed["messages"]
        self.persist()
        self.remember_session()
        self.snapshot("session.ready")

    def make_terminal(self, config):
        terminal = self.cli.ArtheraTerminal(config)
        execute = terminal.tool_executor.execute_local
        def tracked(*args, **kwargs):
            with self.tool_lock:
                self.active_tools += 1
            try:
                return execute(*args, **kwargs)
            finally:
                with self.tool_lock:
                    self.active_tools -= 1
        terminal.tool_executor.execute_local = tracked
        return terminal

    def load_session(self, ident):
        # Validate before the constructor creates a JSONL log for this ID.
        ident = session_id(ident)
        data = self.cli.SessionManager(self.cli.SESSIONS_DIR).load_session(ident)
        if not data:
            from aria_code.apps.cli.session_jsonl import JsonlSessionStore
            data = JsonlSessionStore().load_session(ident)
        if not data:
            raise ValueError("Session not found")
        messages = data.get("messages")
        if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
            raise ValueError("Invalid session messages")
        return data

    def end_hooks(self):
        if self.cli._HAS_JSON_HOOKS:
            try:
                self.cli._fire_json_hook("SessionEnd", session_id=self.terminal.session_id,
                                         hooks=self.cli._JSON_HOOKS)
            except Exception:
                pass
        self.cli._run_event_hook("session_end", {"ARIA_SESSION": self.terminal.session_id})

    def switch_session(self, ident=None):
        data = self.load_session(ident) if ident is not None else None
        self.persist()
        self.end_hooks()
        config = dict(self.terminal.config, last_session_id=ident or uuid.uuid4().hex[:8])
        registry = self.terminal._mcp_registry
        self.cli._session_always_allow.clear()
        self.cli._session_command_prefixes.clear()
        self.cli._auto_approve_session = self.options.dangerously_skip_permissions
        self.cli._session_always_allow.update(t.strip() for t in self.options.allow_tools.split(",") if t.strip())
        # A fresh terminal also resets plans, images, loaded-project context,
        # telemetry, tool trace and pending artifacts. MCP connections survive.
        self.terminal = self.make_terminal(config)
        self.terminal._mcp_registry = registry
        if data:
            self.terminal.conversation = data["messages"]
        self.persist()
        self.remember_session()
        self.snapshot("session.ready")

    def remember_session(self):
        # Save only the pointer in the user configuration, not transient
        # project roots or launch-only model/permission overrides.
        config = self.cli.load_config()
        config["last_session_id"] = self.terminal.session_id
        self.cli.save_config(config)

    def snapshot(self, kind):
        from aria_code.apps.cli.native_branding import robot_rows
        t = self.terminal
        # A display snapshot is bounded independently of the model's context.
        messages = [{"role": m.get("role", ""), "content": str(m.get("content", ""))[:4096]}
                    for m in t.conversation[-40:]]
        self.emit(kind, session_id=t.session_id, messages=messages,
                  version=self.cli.__version__,
                  robot=robot_rows(hidden=self.options.no_banner or self.options.banner == "off"),
                  model=t.config.get("model", ""), provider=t.config.get("local_provider", ""),
                  workspace=str(Path.cwd()), commands=sorted(t.commands.commands),
                  permission=t.config.get("permission_mode", "workspace-write"),
                  network=bool(t.config.get("network_enabled", True)))

    def persist(self):
        t = self.terminal
        if t.config.get("auto_save_sessions", True):
            t.session_mgr.save_session(t.session_id, t.conversation)

    async def turn(self, value):
        t = self.terminal
        t._last_turn_envelope = None
        t._last_response = ""
        t.cancel_event = asyncio.Event()
        self.emit("turn.started", prompt=value["text"])
        outcome, error = "completed", ""
        try:
            prompt = value["text"]
            if t.commands.is_command(prompt.strip()):
                await t.commands.execute(prompt.strip())
            elif prompt.startswith("!"):
                # User-initiated shell commands still go through the executor's
                # workspace/command policy, rather than bypassing it with shell=True.
                from aria_code.runtime.approval import ApprovalDecision
                result = await asyncio.to_thread(t.tool_executor.execute_local, "run_command",
                                                  {"command": prompt[1:].strip()},
                                                  approval=ApprovalDecision.allow(policy=t.config.get("command_policy", "safe"), user_approved=True))
                self.emit("output.delta", text=json.dumps(result, ensure_ascii=False, default=str))
                t.conversation.append({"role": "user", "content": f"[shell {prompt}]\n{result}"})
            else:
                await t.send_message(prompt, route_text=True)
            envelope = t._last_turn_envelope
            if envelope:
                outcome = "cancelled" if envelope.cancelled else str(envelope.status)
                error = str(envelope.error or "")
            if t._last_response:
                self.emit("answer.replace", text=t._last_response)
        except (asyncio.CancelledError, KeyboardInterrupt):
            outcome = "cancelled"
        except (Exception, SystemExit) as exc:
            outcome, error = "failed", str(exc)
        finally:
            # Cancelling an asyncio Future cannot stop an executor thread.
            # Never accept a next turn while a tool from this one is still
            # running. The native host kills the worker/process group if this
            # boundary cannot settle within its cancellation grace period.
            while True:
                with self.tool_lock:
                    busy = self.active_tools
                if not busy:
                    break
                await asyncio.sleep(0.01)
            t._streaming = False
            t.cancel_event = None
            try:
                self.persist()
            except Exception as exc:
                outcome, error = "failed", f"Session save failed: {exc}"
            self.emit("turn.completed", status=outcome, error=error, session_id=t.session_id)
            self.turn_id = None
            self.snapshot("session.state")

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self.read_requests, daemon=True).start()
        self.initialize()
        # Start configured MCP connections without delaying the first frame.
        # Keep the same registration path used by the Python REPL.
        if self.cli._HAS_MCP:
            async def mcp():
                try:
                    from aria_code.mcp_client import MCPToolRegistry
                    registry = MCPToolRegistry()
                    self.terminal._mcp_registry = registry
                    results = await registry.start_all()
                    if results:
                        registry.register_into(self.cli.LOCAL_TOOLS, self.cli.LOCAL_TOOL_SCHEMAS)
                        self.cli._mcp_registry = registry
                except Exception:
                    pass
            self.background.append(asyncio.create_task(mcp()))
        while not self.stopping.is_set():
            value = await asyncio.to_thread(self.requests.get)
            kind = value.get("type")
            if kind == "shutdown":
                break
            try:
                if self.task and not self.task.done():
                    raise ValueError("A turn is already running")
                if kind == "turn.submit":
                    text = value.get("text")
                    ident = value.get("turn_id")
                    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_INPUT:
                        raise ValueError("Turn text must be 1..65536 UTF-8 bytes")
                    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                        raise ValueError("Invalid turn ID")
                    self.turn_id = ident
                    self.cancel_requested = False
                    self.task = asyncio.create_task(self.turn(value))
                elif kind == "session.resume":
                    self.switch_session(value.get("session_id", ""))
                elif kind == "session.new":
                    self.switch_session()
                else:
                    raise ValueError("Unknown application request")
            except (ValueError, OSError, TypeError) as exc:
                self.emit("protocol.error", error=str(exc))
        self.abort()
        if self.task:
            try:
                await self.task
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        self.persist()
        for task in self.background:
            task.cancel()
        if self.terminal._mcp_registry:
            await self.terminal._mcp_registry.stop_all()
        from aria_code.runtime.processes import stop_all
        await asyncio.to_thread(stop_all)
        self.end_hooks()
        self.emit("session.closed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--url")
    parser.add_argument("--resume")
    parser.add_argument("--resume-last", action="store_true")
    parser.add_argument("--no-banner", action="store_true")
    parser.add_argument("--banner", choices=["full", "compact", "off"])
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--read-dir", action="append", default=[])
    parser.add_argument("--allow-tools", default="")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    options = parser.parse_args()
    incoming, outgoing = sys.stdin.buffer, sys.stdout
    server = Server(incoming, outgoing, options)
    try:
        asyncio.run(server.serve())
    except (BrokenPipeError, KeyboardInterrupt):
        return
    except Exception as exc:
        server.emit("session.failed", error=str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
