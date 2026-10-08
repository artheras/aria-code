"""CLI consumers for runtime/model events.

The runtime core emits typed events and asks for approval decisions.  This
module keeps terminal-specific rendering and prompts at the CLI adapter layer
instead of letting them spread through the agent loop.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from aria_code.runtime import (
    AgentEventStatus,
    AgentEventThinking,
    AgentEventToken,
    AgentEventToolCall,
    AgentEventToolResult,
    ApprovalDecision,
)


_REPETITION_MARKER = "*[model stopped — repetition detected]*"
_REPETITION_NOTICE = (
    "\n\n> 已检测到模型开始重复输出，已自动停止展开。"
    "上方结果仍然有效；如需继续，请指定要补充的部分。"
)


def _redact_activity_text(value: Any, limit: int = 90) -> str:
    """One line, secrets masked — for anything echoed to the screen or a log.

    Shell commands are now shown on their step line, so this also covers
    "Authorization: Bearer …", credentials in URLs and the usual key shapes.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|passwd|secret)(\s*[=:]\s*|\s+)[^\s]+",
        r"\1\2***",
        text,
    )
    text = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", text)
    text = re.sub(r"://[^/\s:@]+:[^@\s]+@", "://***@", text)
    text = re.sub(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{12,}|\bAIza[0-9A-Za-z_-]{20,}"
                  r"|\bxox[abposr]-[A-Za-z0-9-]{10,}", "***", text)
    return text[:limit]


# How long the runtime measured a tool call to take, carried with its result.
MEASURED_ELAPSED_KEY = "_elapsed_s"


def with_measured_elapsed(result: Any, elapsed: Any) -> Any:
    if isinstance(result, dict) and isinstance(elapsed, (int, float)):
        return {**result, MEASURED_ELAPSED_KEY: float(elapsed)}
    return result


def approval_subject(tool_name: str, params: dict[str, Any]) -> str:
    """What an answered approval leaves behind: the file, or the command with secrets masked."""
    if tool_name == "run_command":
        return _redact_activity_text(params.get("command", ""), limit=80)
    for key in ("path", "file_path"):
        if params.get(key):
            return Path(str(params[key])).name
    return tool_name.replace("_", " ")


def _tool_activity_hint(tool: str, params: dict[str, Any]) -> str:
    """Return one safe, useful argument for a compact activity row."""
    if not isinstance(params, dict):
        return ""
    for key in ("path", "file_path", "filename", "file"):
        if params.get(key):
            return Path(str(params[key])).name[:90]
    for key in ("symbol", "ticker", "query", "q", "command", "url", "task"):
        if params.get(key):
            return _redact_activity_text(params[key])
    title = params.get("title")
    return _redact_activity_text(title) if title else ""


class TurnPhase(str, Enum):
    """User-visible lifecycle for one assistant turn."""

    CONNECTING = "connecting"
    THINKING = "thinking"
    STREAMING = "streaming"
    TOOL = "tool"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class TurnLifecycle:
    phase: TurnPhase = TurnPhase.CONNECTING
    started_at: float = field(default_factory=time.time)
    first_visible_at: float | None = None
    ended_at: float | None = None

    def transition(self, phase: TurnPhase) -> None:
        self.phase = phase
        now = time.time()
        if phase is TurnPhase.STREAMING and self.first_visible_at is None:
            self.first_visible_at = now
        if phase in {TurnPhase.DONE, TurnPhase.ERROR, TurnPhase.CANCELLED}:
            self.ended_at = now


class TerminalRuntimeEventConsumer:
    """Consume runtime/provider events and render them to a terminal."""

    def __init__(
        self,
        *,
        terminal: Any,
        console: Any,
        has_rich: bool,
        markdown_cls: type | None,
        live_cls: type | None,
        strip_latex: Callable[[str], str],
        set_robot_state: Callable[[Any], None] | None = None,
        streaming_state: Any = None,
        print_tool_call: Callable[[str, dict], None] | None = None,
        print_tool_done: Callable[..., None] | None = None,
        fallback_from: str = "local",
        live_update_interval: float = 0.08,
        ui_lang: str = "en",
        on_response_start: Callable[[], None] | None = None,
        on_phase_change: Callable[[TurnPhase], None] | None = None,
    ) -> None:
        self.terminal = terminal
        self.console = console
        self.has_rich = has_rich
        self.markdown_cls = markdown_cls
        self.live_cls = live_cls
        self.strip_latex = strip_latex
        self.set_robot_state = set_robot_state
        self.streaming_state = streaming_state
        self.print_tool_call = print_tool_call
        self.print_tool_done = print_tool_done
        self.fallback_from = fallback_from
        self.live_update_interval = live_update_interval
        self.ui_lang = ui_lang or "en"
        self.on_response_start = on_response_start
        self.on_phase_change = on_phase_change
        self.lifecycle = TurnLifecycle()

        self.response_text = ""
        self.streamed_any = False
        self.token_count = 0
        self.thinking_tokens = 0
        self.thinking_shown = False
        self.thinking_start: float | None = None
        self.thinking_finished = False
        self.thinking_preview_buf: list[str] = []
        self.thinking_full_buf: list[str] = []
        self.tool_start_times: dict[str, list[float]] = {}
        self.tool_params: dict[str, list[dict[str, Any]]] = {}
        self.repetition_stopped = False
        self.repetition_notice_printed = False
        self.response_started = False

        self.live_display = None
        self.spinner = None
        self.first_token_received_ref = [False]
        self.token_start_time: float | None = None
        self.last_live_update = 0.0
        self.use_plain_print_ref = [False]
        self.use_batch_render_ref = [False]
        self.latex_buf_ref = [""]
        self.in_latex_ref = [False]

    @property
    def first_token_received(self) -> bool:
        return self.first_token_received_ref[0]

    @first_token_received.setter
    def first_token_received(self, value: bool) -> None:
        self.first_token_received_ref[0] = value

    @property
    def use_plain_print(self) -> bool:
        return self.use_plain_print_ref[0]

    @use_plain_print.setter
    def use_plain_print(self, value: bool) -> None:
        self.use_plain_print_ref[0] = value

    @property
    def use_batch_render(self) -> bool:
        return self.use_batch_render_ref[0]

    @use_batch_render.setter
    def use_batch_render(self, value: bool) -> None:
        self.use_batch_render_ref[0] = value

    @property
    def latex_buf(self) -> str:
        return self.latex_buf_ref[0]

    @latex_buf.setter
    def latex_buf(self, value: str) -> None:
        self.latex_buf_ref[0] = value

    @property
    def in_latex(self) -> bool:
        return self.in_latex_ref[0]

    @in_latex.setter
    def in_latex(self, value: bool) -> None:
        self.in_latex_ref[0] = value

    def start_spinner(self) -> None:
        self.set_phase(TurnPhase.THINKING)
        if self.has_rich and self.spinner is None and not self.first_token_received:
            label = "思考中" if self.ui_lang.lower().startswith("zh") else "Thinking"
            cancel = "esc 取消" if self.ui_lang.lower().startswith("zh") else "esc cancel"
            self.spinner = self.console.status(
                f"[dim]{label}… [/dim][dim italic]{cancel}[/dim italic]",
                spinner="dots",
                spinner_style="dim",
            )
            self.spinner.__enter__()

    def set_phase(self, phase: TurnPhase) -> None:
        if self.lifecycle.phase is phase:
            return
        self.lifecycle.transition(phase)
        if self.on_phase_change is not None:
            self.on_phase_change(phase)

    def ensure_response_started(self) -> None:
        """Render the response label only when visible answer text exists."""
        if self.response_started:
            return
        self._finish_thinking()
        self.stop_spinner()
        self.response_started = True
        if self.on_response_start is not None:
            self.on_response_start()

    def finish(self, phase: TurnPhase = TurnPhase.DONE) -> None:
        self._finish_thinking()
        self.stop_spinner()
        self.set_phase(phase)

    def stop_spinner(self) -> None:
        if self.spinner is not None:
            try:
                self.spinner.__exit__(None, None, None)
            except Exception:
                pass
            self.spinner = None

    def stop_live(self, discard: bool = False) -> None:
        self.stop_spinner()
        if self.live_display:
            try:
                if discard:
                    try:
                        from rich.text import Text as _RichText

                        self.live_display.update(_RichText(""))
                        self.live_display.refresh()
                    except Exception:
                        pass
                self.live_display.stop()
            except Exception:
                pass
            self.live_display = None
        elif self.first_token_received and not discard and not self.use_batch_render:
            print(flush=True)

    def set_batch_render_mode(self, enabled: bool = True) -> None:
        self.use_plain_print = enabled
        self.use_batch_render = enabled

    def reset_stream_state(self) -> None:
        self.response_text = ""
        self.streamed_any = False
        self.token_count = 0
        self.first_token_received = False
        self.token_start_time = None
        self.latex_buf = ""
        self.in_latex = False

    def flush_latex_buf(self) -> str:
        raw = self.latex_buf
        self.latex_buf = ""
        self.in_latex = False
        return self.strip_latex(raw) if raw.strip() else raw

    def _show_repetition_notice(self) -> None:
        if self.repetition_notice_printed or self.use_batch_render:
            return
        self.repetition_notice_printed = True
        if self.live_display and self.has_rich and self.markdown_cls is not None:
            clean_text = self.response_text.split(_REPETITION_MARKER, 1)[0].rstrip()
            self.live_display.update(self.markdown_cls(self.strip_latex(clean_text + _REPETITION_NOTICE)))
            self.live_display.refresh()
            return
        print(_REPETITION_NOTICE, end="", flush=True)

    def finalize_text(self, final_text: str) -> str:
        if self.in_latex and self.latex_buf:
            leftover = self.flush_latex_buf()
            final_text = (final_text or "") + leftover
            if self.use_plain_print and not self.use_batch_render:
                print(leftover, end="", flush=True)
        return final_text

    def _finish_thinking(self) -> None:
        if not self.thinking_shown or self.thinking_finished:
            return
        self.thinking_finished = True
        self.stop_spinner()
        elapsed_t = time.time() - self.thinking_start if self.thinking_start else 0
        self.terminal._last_thinking = "".join(self.thinking_full_buf).strip()
        is_zh = self.ui_lang.lower().startswith("zh")
        t_info = f"思考 {elapsed_t:.1f}s" if is_zh else f"Thought for {elapsed_t:.1f}s"
        if self.thinking_tokens > 0:
            t_info += f" · {self.thinking_tokens:,} tokens"
        expand = "展开" if is_zh else "expand"
        ctrlo = f"  [dim]· Ctrl+O {expand}[/dim]" if self.terminal._last_thinking else ""
        if self.has_rich:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self.console.print(f"  [dim]✻[/dim] [dim]{t_info}[/dim]{ctrlo}")
            if self.terminal.config.get("thinking_preview") and self.thinking_preview_buf:
                preview_text = "".join(self.thinking_preview_buf)[:280].strip()
                if len("".join(self.thinking_preview_buf)) > 280:
                    preview_text += "…"
                self.console.print(f"  [dim italic]{preview_text}[/dim italic]")
        else:
            print(f"\r  ✻ {t_info}")

    def on_token(self, token: str) -> None:
        if "<|im_start|>" in token or "<|im_end|>" in token:
            token = token.replace("<|im_start|>", "").replace("<|im_end|>", "")
            if not token.strip():
                return

        meta_artifacts = (
            "(注释：",
            "（注释：",
            "(提示：",
            "（提示：",
            "请使用实际注入的数据",
            "请使用实际数据",
            "实际注入的数据",
            "[system]",
            "[/system]",
            "[INST]",
            "[/INST]",
        )
        if any(a in token for a in meta_artifacts):
            token = re.sub(
                r"\(注[释释]：[^)）]*[)）]|（注[释释]：[^)）]*[)）]"
                r"|\(提示：[^)）]*[)）]|（提示：[^)）]*[)）]"
                r"|请使用实际(?:注入的)?数据[^。\n]*"
                r"|\[/?(?:system|INST)\]",
                "",
                token,
            )
            if not token.strip():
                return

        if not self.first_token_received and not token.strip():
            self.response_text += token
            self.streamed_any = True
            self.token_count += 1
            return

        if not self.first_token_received:
            self.first_token_received = True
            self.token_start_time = time.time()
            self.set_phase(TurnPhase.STREAMING)
            if self.set_robot_state is not None:
                self.set_robot_state(self.streaming_state)
            if not self.use_batch_render:
                self.ensure_response_started()

        if _REPETITION_MARKER in token:
            if self.use_batch_render:
                self.response_text += token
                self.streamed_any = True
                self.token_count += 1
                self.repetition_stopped = True
                return
            before, _, _after = token.partition(_REPETITION_MARKER)
            if before:
                self.on_token(before)
            self.response_text += _REPETITION_MARKER
            self.streamed_any = True
            self.token_count += 1
            self.repetition_stopped = True
            self._show_repetition_notice()
            return

        self._finish_thinking()

        if self.use_batch_render:
            self.response_text += token
            self.streamed_any = True
            self.token_count += 1
            return

        open_delims = (r"\(", r"\[", "$$")
        close_delims = (r"\)", r"\]", "$$")
        if not self.in_latex:
            if any(d in token for d in open_delims):
                self.in_latex = True
                self.latex_buf = token
                tail = token
                for od, cd in zip(open_delims, close_delims):
                    if od in tail:
                        after = tail[tail.index(od) + len(od):]
                        if cd in after:
                            token = self.flush_latex_buf()
                            break
                else:
                    self.response_text += self.latex_buf
                    self.streamed_any = True
                    self.token_count += 1
                    return
            else:
                token = self.strip_latex(token)
        else:
            self.latex_buf += token
            if any(d in token for d in close_delims):
                token = self.flush_latex_buf()
            else:
                self.response_text += token
                self.streamed_any = True
                self.token_count += 1
                return

        self.response_text += token
        self.streamed_any = True
        self.token_count += 1
        can_live = (
            self.has_rich
            and not self.use_plain_print
            and getattr(self.console, "is_terminal", False)
            and not getattr(self.console, "is_dumb_terminal", True)
            and self.markdown_cls is not None
            and self.live_cls is not None
        )
        if can_live:
            now = time.time()
            if self.live_display is not None and now - self.last_live_update < self.live_update_interval:
                return
            md = self.markdown_cls(self.strip_latex(self.response_text))
            if self.live_display is None:
                self.live_display = self.live_cls(
                    md,
                    console=self.console,
                    refresh_per_second=12,
                    vertical_overflow="visible",
                )
                self.live_display.start()
                self.last_live_update = now
            elif now - self.last_live_update >= self.live_update_interval:
                self.live_display.update(md)
                self.last_live_update = now
        else:
            print(token, end="", flush=True)

    def on_thinking(self, content: str) -> None:
        self.set_phase(TurnPhase.THINKING)
        if self.thinking_finished:
            # 工具调用后的新一段思考:重置段状态,否则上一段的 finished 闩锁
            # 让本段永远无法 finalize,"思考中 …(N tokens)"活行被冻结在滚动区
            # (即多条思考行并存的根因)。full_buf 不清,Ctrl+O 展开保留全部段落。
            self.thinking_finished = False
            self.thinking_shown = False
            self.thinking_tokens = 0
            self.thinking_preview_buf = []
        if not self.thinking_shown:
            self.stop_spinner()
            self.thinking_start = time.time()
            self.thinking_shown = True
        self.thinking_tokens += 1
        if self.thinking_tokens % 30 == 1:
            elapsed = time.time() - self.thinking_start
            label = "思考中" if self.ui_lang.lower().startswith("zh") else "Thinking"
            # 与 finalize 行(思考 Xs · N tokens)同格式,避免括号/点分隔两种样式并存
            sys.stdout.write(
                f"\r  \033[2m✻\033[0m \033[2m{label}  {elapsed:.1f}s"
                f"  ·  {self.thinking_tokens} tokens\033[0m    "
            )
            sys.stdout.flush()
        if len("".join(self.thinking_preview_buf)) < 300:
            self.thinking_preview_buf.append(content)
        if len("".join(self.thinking_full_buf)) < 8000:
            self.thinking_full_buf.append(content)

    def on_tool_call(self, tool: str, params: dict) -> None:
        self.set_phase(TurnPhase.TOOL)
        self._finish_thinking()

        # Cursor/Windsurf 风格的实时加载动画
        # One spinner at a time. /team's analysts call tools in parallel; the
        # second spinner replaced the first in this one slot, the first was
        # never stopped, and after the report the CLI sat on "Running
        # run_factor_research" with no prompt.
        self.close_tool_spinner()
        if self.has_rich and hasattr(self, 'console'):
            hint = _tool_activity_hint(tool, params)
            verb = "调用工具" if str(self.ui_lang).lower().startswith("zh") else "Running"
            label = f"[bold cyan]{verb}[/bold cyan] [green]{tool}[/green]"
            if hint:
                label += f" [dim]({hint})[/dim]"
            self.tool_spinner = self.console.status(label, spinner="dots12", spinner_style="cyan")
            self.tool_spinner.__enter__()

        if self.print_tool_call is not None:
            self.print_tool_call(tool, params if isinstance(params, dict) else {})

        self.tool_start_times.setdefault(tool, []).append(time.time())
        safe_params = dict(params) if isinstance(params, dict) else {}
        self.tool_params.setdefault(tool, []).append(safe_params)

    def close_tool_spinner(self) -> None:
        """Stop the tool spinner, if one is running."""
        spinner = getattr(self, "tool_spinner", None)
        if spinner is not None:
            try:
                spinner.__exit__(None, None, None)
            except Exception:
                pass
        self.tool_spinner = None

    def on_tool_result(self, tool: str, summary: Any) -> None:
        # 关闭工具加载动画
        self.close_tool_spinner()

        starts = self.tool_start_times.get(tool) or []
        started_at = starts.pop(0) if starts else time.time()
        if not starts:
            self.tool_start_times.pop(tool, None)
        elapsed_ms = int((time.time() - started_at) * 1000)
        # The runtime times each call itself. Measuring from the announcement
        # counted the person reading an approval, and results of a batch
        # arrive together — "✓ writing file test_fx.py 13.9s" for a 9ms write.
        if isinstance(summary, dict) and isinstance(summary.get(MEASURED_ELAPSED_KEY), (int, float)):
            elapsed_ms = int(summary[MEASURED_ELAPSED_KEY] * 1000)
        params_queue = self.tool_params.get(tool) or []
        params = params_queue.pop(0) if params_queue else {}
        if not params_queue:
            self.tool_params.pop(tool, None)
        ok = not (isinstance(summary, dict) and not summary.get("success", True))
        if self.print_tool_done is not None:
            # Surface the failure reason on the ✗ line — a red cross with no
            # explanation leaves both the user and the reviewer blind to WHY
            # (observed in the channels drill: peer_comparison failed 4× with
            # nothing on screen).
            detail = ""
            if not ok and isinstance(summary, dict):
                detail = str(summary.get("error") or "")[:90]
            elif ok:
                # Name what was done: two bare "✓ writing file" lines in a row
                # did not say which file each was.
                detail = _tool_activity_hint(tool, params)
            self.print_tool_done(tool, elapsed_ms, success=ok, summary=detail)

        ts = time.strftime("%H:%M:%S")
        icon = "✓" if ok else "✗"
        hint = _tool_activity_hint(tool, params)
        duration = f"{elapsed_ms}ms" if elapsed_ms < 1000 else f"{elapsed_ms / 1000:.1f}s"
        entry = f"{ts}  {icon} {tool}"
        if hint:
            entry += f"  {hint}"
        entry += f"  · {duration}"
        if not ok and isinstance(summary, dict):
            error = _redact_activity_text(summary.get("error"), limit=100)
            if error:
                entry += f"  · {error}"
        self.terminal._transcript_log.append(entry)
        if len(self.terminal._transcript_log) > 100:
            self.terminal._transcript_log = self.terminal._transcript_log[-100:]

        if tool in ("TaskCreate", "TaskUpdate") and isinstance(summary, dict):
            tid = summary.get("id") or summary.get("task_id")
            title = summary.get("title", "")
            status = summary.get("status", "pending")
            if tid:
                existing = next((t for t in self.terminal._task_list if t.get("id") == tid), None)
                if existing:
                    existing["status"] = status
                    if title:
                        existing["title"] = title
                else:
                    self.terminal._task_list.append({"id": tid, "title": title, "status": status})

    def on_status(self, state: str, message: str) -> None:
        if state in ("acceptance_passed", "acceptance_failed"):
            # The gate's verdict is the one piece of evidence behind "done", so
            # it is printed rather than left in the turn result: a user who
            # cannot see that the check went red will read the model's summary
            # as if it had gone green.
            passed = state == "acceptance_passed"
            if self.has_rich and self.console is not None:
                colour = "green" if passed else "yellow"
                self.console.print(f"  [{colour}]{'✓' if passed else '✗'} {message}[/{colour}]")
            else:
                print(f"  {'✓' if passed else '✗'} {message}")
            return
        if state in {"max_rounds", "budget_exhausted", "loop_guard", "checks_failed"}:
            if self.has_rich and self.console is not None:
                self.console.print(f"  [yellow]Task incomplete: {message}[/yellow]")
            else:
                print(f"  Task incomplete: {message}")
            return
        if state != "fallback":
            return
        match = re.search(r"(?:from\s+)?(\w+)\s*(?:→|->|to)\s*(\w+)", message or "", re.I)
        if match:
            from_provider, to_provider = match.group(1), match.group(2)
        else:
            from_provider, to_provider = self.fallback_from, "cloud"
        from aria_code.ui.render.output import print_fallback_toast

        print_fallback_toast(
            from_provider,
            to_provider,
            message or "",
            console=self.console,
            has_rich=self.has_rich,
        )

    def handle_runtime_event(self, event: Any) -> None:
        if isinstance(event, AgentEventToken):
            self.on_token(event.text)
        elif isinstance(event, AgentEventThinking):
            self.on_thinking(event.content)
        elif isinstance(event, AgentEventToolCall):
            self.on_tool_call(event.tool, event.params)
        elif isinstance(event, AgentEventToolResult):
            self.on_tool_result(event.tool, with_measured_elapsed(event.result, getattr(event, "elapsed", None)))
        elif isinstance(event, AgentEventStatus):
            self.on_status(event.state, event.message)


class TerminalApprovalEventConsumer:
    """Terminal-side approval prompt consumer for runtime tool execution."""

    def __init__(
        self,
        *,
        terminal: Any,
        console: Any,
        has_rich: bool,
        confirm_decision: Callable[..., ApprovalDecision],
        apply_decision: Callable[[dict, ApprovalDecision], dict],
        save_config: Callable[[dict], None],
    ) -> None:
        self.terminal = terminal
        self.console = console
        self.has_rich = has_rich
        self.confirm_decision = confirm_decision
        self.apply_decision = apply_decision
        self.save_config = save_config

    async def approve(
        self,
        tool_name: str,
        tool_params: dict,
        *,
        stop_before_prompt: Callable[[], None] | None = None,
    ) -> ApprovalDecision:
        if stop_before_prompt is not None:
            stop_before_prompt()
        try:
            approval = self.confirm_decision(
                tool_name,
                tool_params,
                config_policy=self.terminal.config.get("command_policy", "safe"),
            )
        except KeyboardInterrupt:
            approval = ApprovalDecision.deny("KeyboardInterrupt")

        self.terminal._record_feedback(
            "tool_accept" if approval.approved else "tool_reject",
            tool_name,
        )
        if not approval.approved:
            from aria_code.ui.render.output import print_tool_blocked

            zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
            print_tool_blocked(tool_name, "用户取消" if zh else "declined", console=self.console,
                               has_rich=self.has_rich)
        return approval

    def apply(self, tool_params: dict, approval: ApprovalDecision) -> dict:
        self.apply_decision(tool_params, approval)
        if approval.upgrade_policy:
            tool_params.pop("_upgrade_policy", None)
            self.terminal.config["command_policy"] = "balanced"
            try:
                self.save_config(self.terminal.config)
                if self.has_rich:
                    zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
                    self.console.print("  [dim]策略已升级为 balanced 并保存[/dim]" if zh else
                                       "  [dim]Policy set to balanced and saved[/dim]")
            except Exception:
                pass
        return tool_params


__all__ = ["TerminalApprovalEventConsumer", "TerminalRuntimeEventConsumer"]
