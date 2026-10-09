"""Agent-loop orchestration helpers for Aria Code.

This module intentionally starts with pure, easily-tested primitives. The CLI
still owns UI prompts and provider calls, while the runtime owns the mechanical
shape of tool batching and follow-up construction.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncGenerator, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

from .acceptance import AcceptanceGate
from .approval import ApprovalDecision, apply_approval_decision
from .tool_executor import ToolExecutor
from .budget import BudgetTracker
from .delivery import DeliveryLedger

if TYPE_CHECKING:
    from .contract import ChangeContract
    from .review import ReviewGate


DEFAULT_SERIAL_TOOLS = {"write_file", "edit_file", "multi_edit", "run_command", "process"}

# Phrases the model uses to signal task completion
_DONE_PHRASES = frozenset([
    "task complete", "task is complete", "all done", "completed successfully",
    "here is the final", "here's the final", "analysis complete",
    "任务完成", "已完成", "分析完成", "操作完成", "已经完成", "以下是最终",
    "i have completed", "i've completed", "the task has been completed",
])


def detect_task_complete(response_text: str) -> bool:
    """Heuristic: did the AI signal task completion without requesting more tools?"""
    if not response_text:
        return False
    lower = response_text.lower()
    return any(phrase in lower for phrase in _DONE_PHRASES)


class LoopGuard:
    """Detect repeated identical *failing* tool calls and break the agent loop.

    Weaker local models (and sometimes cloud models) get stuck calling the
    exact same tool with the exact same arguments after it has already failed
    — e.g. reading a file that does not exist, or calling an unknown tool. This
    guard tracks failure signatures across rounds:

      * After ``soft_threshold`` identical failures it injects a directive
        telling the model to STOP repeating that call and try another approach.
      * After ``hard_threshold`` it signals the caller to break the loop so we
        don't burn the remaining tool rounds.

    A successful call clears that signature's counter.
    """

    def __init__(
        self,
        *,
        soft_threshold: int = 2,
        hard_threshold: int = 4,
        tool_soft_threshold: int = 3,
    ) -> None:
        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        # Tool-level counting (ANY params): catches the observed blind spot
        # where a tool kept failing with slightly different arguments — the
        # exact-signature counters never accumulated, so peer_comparison
        # failed 4x in one turn without a single guard directive. Advisory
        # only: exploration patterns (read_file over several candidate paths)
        # legitimately fail with varying params, so the hard break stays
        # signature-exact.
        self.tool_soft_threshold = tool_soft_threshold
        self._fail_counts: Dict[str, int] = {}
        self._tool_fail_counts: Dict[str, int] = {}
        self._tool_fail_sigs: Dict[str, set] = {}
        self._warned: set = set()
        self._tool_warned: set = set()
        self.should_break: bool = False

    @staticmethod
    def _signature(tool_name: str, params: dict) -> str:
        import hashlib
        import json
        try:
            key = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            key = str(params)
        digest = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:12]
        return f"{tool_name}::{digest}"

    @staticmethod
    def is_failure(result) -> bool:
        """Best-effort detection of a failed tool result (dict or summary string)."""
        if isinstance(result, dict):
            if result.get("success") is True:
                return False
            if result.get("success") is False:
                return True
            text = f"{result.get('error', '')} {result.get('output', '')}"
        else:
            text = str(result)
        low = text[:200].lower()
        return any(s in low for s in (
            "error", "unknown local tool", "unknown tool", "not found",
            "no such file", "failed", "traceback", "exception",
            "missing", "未找到", "失败", "错误",
        ))

    def record(self, tool_name: str, params: dict, result) -> "str | None":
        """Record one tool result. Return a directive string if a loop is detected."""
        sig = self._signature(tool_name, params)
        if not self.is_failure(result):
            self._fail_counts.pop(sig, None)
            self._warned.discard(sig)
            self._tool_fail_counts.pop(tool_name, None)
            self._tool_fail_sigs.pop(tool_name, None)
            self._tool_warned.discard(tool_name)
            return None

        self._fail_counts[sig] = self._fail_counts.get(sig, 0) + 1
        self._tool_fail_counts[tool_name] = self._tool_fail_counts.get(tool_name, 0) + 1
        self._tool_fail_sigs.setdefault(tool_name, set()).add(sig)
        count = self._fail_counts[sig]

        if count >= self.hard_threshold:
            self.should_break = True
            return (
                f"⛔ 已连续 {count} 次用相同参数调用 `{tool_name}` 且全部失败。"
                f"立即停止调用工具，基于现有信息直接回答用户，或说明卡在哪里、需要什么。"
            )
        if count >= self.soft_threshold and sig not in self._warned:
            self._warned.add(sig)
            return (
                f"⚠ 你已经用完全相同的参数调用了 `{tool_name}` {count} 次，每次都失败。"
                f"不要再用相同参数重试。请改变策略：换参数、换工具(如先用 list_files/search_code 定位)，"
                f"或基于已有结果继续。"
            )

        tool_count = self._tool_fail_counts.get(tool_name, 0)
        distinct_sigs = len(self._tool_fail_sigs.get(tool_name, ()))
        # Only when the arguments actually varied — identical-params repeats
        # belong exclusively to the signature counters above (which already
        # warned at soft and will hard-break), and this message says
        # "参数各不相同", which must be true when shown.
        if (
            tool_count >= self.tool_soft_threshold
            and distinct_sigs >= 2
            and tool_name not in self._tool_warned
        ):
            self._tool_warned.add(tool_name)
            return (
                f"⚠ 工具 `{tool_name}` 本回合已失败 {tool_count} 次（参数各不相同）。"
                f"它当前很可能不可用（网络/依赖问题）。不要继续重试该工具；"
                f"改用其他数据来源，或在回答中说明该工具不可用——"
                f"也不要在“下一步建议”里推荐它而不注明刚刚失败。"
            )
        return None


def split_tool_calls(
    pending: Sequence[dict],
    serial_tools: Iterable[str] = DEFAULT_SERIAL_TOOLS,
) -> Tuple[List[dict], List[dict]]:
    """Split tool calls into parallel-safe and serial batches.

    Beyond the static whitelist, detects data dependencies at runtime:
    if a later run_command references a file written/edited earlier in
    the same batch, it is moved to the serial queue so it runs after
    the write completes.
    """
    serial = set(serial_tools)
    parallel_batch: List[dict] = []
    serial_batch: List[dict] = []
    written_paths: set = set()

    for tc in pending:
        tool = tc.get("tool", "")
        params = tc.get("params", {})

        if tool in serial:
            # Track paths being written so dependents can detect the dependency
            for key in ("path", "file_path", "filename", "target"):
                p = params.get(key)
                if p:
                    written_paths.add(str(p))
            serial_batch.append(tc)
        elif tool == "run_command":
            cmd = str(params.get("command", ""))
            # If the command references a path currently being written → serial
            if written_paths and any(p in cmd for p in written_paths):
                serial_batch.append(tc)
            else:
                parallel_batch.append(tc)
        else:
            parallel_batch.append(tc)

    return parallel_batch, serial_batch


def collect_parallel_done(
    pending: Sequence[dict],
    parallel_results: Sequence[tuple],
    serial_tools: Iterable[str] = DEFAULT_SERIAL_TOOLS,
) -> Dict[int, dict]:
    """Map original pending indices to already-executed parallel results."""
    serial = set(serial_tools)
    done: Dict[int, dict] = {}
    for original_index, tool_call in enumerate(pending):
        if tool_call.get("tool") in serial:
            continue
        for result_tool_call, result in parallel_results:
            if result_tool_call is tool_call:
                done[original_index] = result
                break
    return done


RemoteToolRunner = Callable[[str, dict], Awaitable[dict]]
Hook = Callable[[str, str, dict, dict | None], None]
SummaryFormatter = Callable[[str, dict], str]
ApprovalCallback = Callable[[str, dict], Union[ApprovalDecision, Awaitable[ApprovalDecision]]]


@dataclass
class AgentTurnState:
    """Mutable state accumulated across one agent response turn."""

    provider: str = "aws"
    total_response: str = ""
    tools_used: List[str] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thinking_tokens": 0,
    })
    tool_time_total: float = 0.0
    # 可选的预算追踪器。默认 None —— 不传就完全保持既有行为。
    _budget: Optional["BudgetTracker"] = None

    def append_response(self, text: str | None) -> None:
        if text:
            self.total_response += text

    def apply_model_result(self, result: dict, fallback_response: str = "") -> None:
        self.append_response(result.get("response") or fallback_response)
        self.tools_used.extend(result.get("tools_used", []))
        self.sources.extend(result.get("sources", []))
        self.provider = result.get("provider", self.provider)
        self.add_usage(result.get("usage", {}))

    def add_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        thinking = int(usage.get("thinking_tokens", 0) or 0)
        self.usage["prompt_tokens"] += prompt
        self.usage["completion_tokens"] += completion
        self.usage["thinking_tokens"] += thinking

        # 预算记账挂在这里而不是循环体里：所有路径的 usage 都会经过
        # add_usage()，挂在唯一入口上就不会漏记某条分支。thinking token 计入
        # 输出侧——各家对它的计费都按输出档，漏掉会显著低估推理模型的花费。
        if self._budget is not None:
            self._budget.record(self.provider, prompt, completion + thinking)

    def add_tool_time(self, elapsed: float) -> None:
        self.tool_time_total += elapsed

    def reset_response(self) -> None:
        self.total_response = ""

    def final_text(self, fallback_response: str = "") -> str:
        return self.total_response or fallback_response

    def token_counts(self, *, token_count: int = 0, thinking_tokens: int = 0) -> Tuple[int, int, int, int]:
        prompt_t = self.usage.get("prompt_tokens", 0)
        completion_t = self.usage.get("completion_tokens", 0) or token_count
        think_t = self.usage.get("thinking_tokens", 0) or thinking_tokens
        return prompt_t, completion_t, think_t, prompt_t + completion_t + think_t

    def generation_time(self, elapsed: float) -> float:
        return elapsed - self.tool_time_total

    def unique_tools(self) -> List[str]:
        return list(dict.fromkeys(self.tools_used))

    def build_metadata(
        self,
        *,
        elapsed: float,
        token_count: int = 0,
        thinking_tokens: int = 0,
    ) -> "AgentTurnMetadata":
        prompt_t, completion_t, think_t, total_t = self.token_counts(
            token_count=token_count,
            thinking_tokens=thinking_tokens,
        )
        parts = [f"{elapsed:.1f}s"]
        gen_time = self.generation_time(elapsed)

        if total_t > 0:
            token_parts = []
            if prompt_t > 0:
                token_parts.append(f"in: {prompt_t:,}")
            if completion_t > 0:
                token_parts.append(f"out: {completion_t:,}")
            if think_t > 0:
                token_parts.append(f"think: {think_t:,}")
            parts.append(f"{total_t:,} tokens ({', '.join(token_parts)})")
            if completion_t > 0 and gen_time > 0.5:
                parts.append(f"{completion_t / gen_time:.0f} t/s")
        elif token_count > 0:
            parts.append(f"{token_count:,} tokens")
            if gen_time > 0.5:
                parts.append(f"{token_count / gen_time:.0f} t/s")

        if self.tool_time_total > 0:
            parts.append(f"tools: {self.tool_time_total:.1f}s")
        if self.provider != "aws":
            parts.append(self.provider)
        unique_tools = self.unique_tools()
        if unique_tools:
            parts.append(" ".join(unique_tools))

        # Turn-level cost — only for cloud providers with token data
        _is_cloud = self.provider not in ("ollama", "ollama_cache", "local", "")
        if _is_cloud and total_t > 0:
            _cost = (prompt_t * 0.14 + completion_t * 0.28 + think_t * 1.10) / 1_000_000
            if _cost >= 0.0001:
                parts.append(f"${_cost:.4f}")

        return AgentTurnMetadata(
            parts=parts,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            thinking_tokens=think_t,
            total_tokens=total_t,
            generation_time=gen_time,
            provider=self.provider,
            tools=unique_tools,
        )

    def build_result(
        self,
        *,
        elapsed: float,
        fallback_response: str = "",
        token_count: int = 0,
        thinking_tokens: int = 0,
        success: bool = True,
        cancelled: bool = False,
        error: str = "",
        acceptance: Optional[dict] = None,
        stop_reason: str = "completed",
        contract: Optional[dict] = None,
        delivery: Optional[dict] = None,
    ) -> "AgentTurnResult":
        metadata = self.build_metadata(
            elapsed=elapsed,
            token_count=token_count,
            thinking_tokens=thinking_tokens,
        )
        return AgentTurnResult(
            success=success,
            cancelled=cancelled,
            error=error,
            final_text=self.final_text(fallback_response),
            metadata=metadata,
            provider=metadata.provider,
            tools=metadata.tools,
            sources=list(self.sources),
            acceptance=acceptance,
            stop_reason=stop_reason,
            contract=contract,
            delivery=delivery,
        )

    def build_cancelled_result(
        self,
        *,
        elapsed: float,
        fallback_response: str = "",
        token_count: int = 0,
        thinking_tokens: int = 0,
    ) -> "AgentTurnResult":
        return self.build_result(
            elapsed=elapsed,
            fallback_response=fallback_response,
            token_count=token_count,
            thinking_tokens=thinking_tokens,
            success=True,
            cancelled=True,
            stop_reason="cancelled",
        )

    def build_error_result(
        self,
        error: str | None,
        *,
        elapsed: float,
        fallback_response: str = "",
        token_count: int = 0,
        thinking_tokens: int = 0,
    ) -> "AgentTurnResult":
        return self.build_result(
            elapsed=elapsed,
            fallback_response=fallback_response,
            token_count=token_count,
            thinking_tokens=thinking_tokens,
            success=False,
            cancelled=False,
            error=error or "Unknown error",
            stop_reason="failed",
        )


@dataclass(frozen=True)
class AgentTurnMetadata:
    """Display and accounting metadata for one completed agent turn."""

    parts: List[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    generation_time: float = 0.0
    provider: str = "aws"
    tools: List[str] = field(default_factory=list)

    def system_prompt_estimate(self, message: str) -> int:
        return max(0, self.prompt_tokens - len(message) // 3)


@dataclass(frozen=True)
class AgentTurnResult:
    """Structured result for a completed agent turn."""

    success: bool
    cancelled: bool
    error: str
    final_text: str
    metadata: AgentTurnMetadata
    provider: str = "aws"
    tools: List[str] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    # 验收证据。None = 本轮没有验收(只读回合,或没有可推断的检查命令);
    # 有值时 ``acceptance["verified"]`` 才是「做完了」这句话的凭据。
    acceptance: Optional[dict] = None
    stop_reason: str = "completed"
    # The change contract this turn ran under and what it refused, or None
    # when no contract applied.
    contract: Optional[dict] = None
    # The runtime's account of the turn (runtime/delivery.py): what changed,
    # what was verified, the risk, checkpoints. None when nothing happened
    # worth reporting.
    delivery: Optional[dict] = None

    @classmethod
    def cancelled_result(
        cls,
        *,
        metadata: AgentTurnMetadata | None = None,
        final_text: str = "",
    ) -> "AgentTurnResult":
        return cls(
            success=True,
            cancelled=True,
            error="",
            final_text=final_text,
            metadata=metadata or AgentTurnMetadata(parts=[]),
            stop_reason="cancelled",
        )

    @classmethod
    def error_result(
        cls,
        error: str,
        *,
        metadata: AgentTurnMetadata | None = None,
        final_text: str = "",
    ) -> "AgentTurnResult":
        return cls(
            success=False,
            cancelled=False,
            error=error,
            final_text=final_text,
            metadata=metadata or AgentTurnMetadata(parts=[]),
            stop_reason="failed",
        )

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stop_reason": self.stop_reason,
            "cancelled": self.cancelled,
            "error": self.error,
            "final_text": self.final_text,
            "provider": self.provider,
            "tools": list(self.tools),
            "sources": list(self.sources),
            "metadata": {
                "parts": list(self.metadata.parts),
                "prompt_tokens": self.metadata.prompt_tokens,
                "completion_tokens": self.metadata.completion_tokens,
                "thinking_tokens": self.metadata.thinking_tokens,
                "total_tokens": self.metadata.total_tokens,
                "generation_time": self.metadata.generation_time,
                "provider": self.metadata.provider,
                "tools": list(self.metadata.tools),
                # 只在真的验收过时才出现,免得每个只读回合都带一个空字段,
                # 让消费者误以为「没验收」和「验收失败」是同一件事。
                **({"acceptance": dict(self.acceptance)} if self.acceptance else {}),
            },
            "acceptance": dict(self.acceptance) if self.acceptance else None,
        }

    def to_envelope(self) -> "AgentTurnEnvelope":
        return AgentTurnEnvelope.from_result(self)


@dataclass(frozen=True)
class AgentTurnEnvelope:
    """Stable runtime envelope for CLI/API consumers."""

    status: str
    success: bool
    cancelled: bool
    error: str
    final_text: str
    provider: str
    tools: List[str]
    summary: str
    metadata: dict

    @classmethod
    def from_result(cls, result: AgentTurnResult) -> "AgentTurnEnvelope":
        return cls(
            status="ok" if result.success else "error",
            success=result.success,
            cancelled=result.cancelled,
            error=result.error,
            final_text=result.final_text,
            provider=result.provider,
            tools=list(result.tools),
            summary=" · ".join(result.metadata.parts),
            metadata=result.to_dict()["metadata"],
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "success": self.success,
            "cancelled": self.cancelled,
            "error": self.error,
            "final_text": self.final_text,
            "provider": self.provider,
            "tools": list(self.tools),
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AgentErrorPresentation:
    """User-facing error presentation for model/agent failures."""

    error: str
    lines: List[str]
    level: str = "error"
    use_generic_error_prefix: bool = False

    @classmethod
    def from_error(cls, error: str | None, *, lang: str = "zh") -> "AgentErrorPresentation":
        normalized = error or "Unknown error"
        lowered = normalized.lower()
        is_zh = str(lang or "zh").lower().startswith("zh")
        if "aria-4223" in lowered:
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    (
                        "当前请求需要实时、可验证的金融数据，但本轮没有取得有效工具结果。"
                        if is_zh else
                        "This request requires current verified financial data, but no usable tool result was obtained."
                    ),
                    (
                        "未生成市场结论。请检查数据源或运行 /doctor 后重试。"
                        if is_zh else
                        "No market conclusion was generated. Check data sources or run /doctor, then retry."
                    ),
                ],
            )
        if lowered.startswith("missing_api_key:"):
            provider = normalized.split(":", 1)[1] or "provider"
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    f"{provider} API Key 未配置。" if is_zh else f"{provider} API key is not configured.",
                    f"运行: /apikey set {provider} <key>" if is_zh else f"Run: /apikey set {provider} <key>",
                ],
            )
        # Gemini on Vertex AI, the default model, borrowing the gcloud login.
        # These reached the user raw: "Error: vertex_needs_project: set …".
        if lowered.startswith("vertex_needs_project"):
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "Gemini 通过 Google Cloud 调用，但尚未设置项目。" if is_zh else
                    "Gemini runs on Google Cloud, and no project is set.",
                    "设置项目：/config set gcp_project=<项目 ID>（或 gcloud config set project <ID>），"
                    "或用 /model 换一个模型。" if is_zh else
                    "Set one with /config set gcp_project=<id> (or gcloud config set project <id>), "
                    "or pick another model with /model.",
                ],
            )
        if lowered.startswith("vertex_not_logged_in"):
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "Gemini 通过 Google Cloud 调用，但 gcloud 尚未登录。" if is_zh else
                    "Gemini runs on Google Cloud, and gcloud is not signed in.",
                    "运行 gcloud auth login，或用 /model 换一个模型。" if is_zh else
                    "Run gcloud auth login, or pick another model with /model.",
                ],
            )
        if lowered.startswith("vertex_gcloud_failed"):
            detail = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    (f"gcloud 未能签发访问令牌（{detail}）。" if is_zh else
                     f"gcloud could not issue an access token ({detail}).") if detail else
                    ("gcloud 未能签发访问令牌。" if is_zh else "gcloud could not issue an access token."),
                    "运行 gcloud auth login 后重试，或运行 /doctor。" if is_zh else
                    "Run gcloud auth login and retry, or run /doctor.",
                ],
            )
        if "http 402" in lowered or "insufficient balance" in lowered:
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "Provider 余额不足，当前模型请求未执行。"
                    if is_zh else
                    "The provider balance is insufficient; the model request was not run."
                ],
            )
        if "http 401" in lowered or "http 403" in lowered:
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "Provider 身份验证失败，请检查 API Key 或登录状态。"
                    if is_zh else
                    "Provider authentication failed. Check the API key or sign-in state."
                ],
            )
        if "http 429" in lowered or "rate limit" in lowered:
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "Provider 请求频率受限，请稍后重试。"
                    if is_zh else
                    "The provider rate limit was reached. Try again shortly."
                ],
            )
        if normalized in ("no_cloud_provider", "no_provider"):
            return cls(
                error=normalized,
                level="warning",
                lines=(
                    [
                        "没有可用的 AI 模型",
                        "  尚未登录，Ollama 也没有运行。",
                        "  解决方案（任选其一）：",
                        "    • 登录即用:     /login  —— 在浏览器里登录或注册，模型由服务端提供，不需要自备 API Key",
                        "    • 本地离线:     ollama serve",
                        "    • 自备 Key:     /apikey set deepseek <your-key>",
                        "    • 导出环境变量: export DEEPSEEK_API_KEY=sk-...",
                    ]
                    if is_zh else
                    [
                        "No AI model is available.",
                        "  You are not signed in, and Ollama is offline.",
                        "  Choose one:",
                        "    • Sign in:  /login  — in the browser; the model is served for you, no API key needed",
                        "    • Offline:  ollama serve",
                        "    • Your own key: /apikey set deepseek <your-key>",
                    ]
                ),
            )
        if normalized == "all_providers_failed":
            return cls(
                error=normalized,
                level="warning",
                lines=[
                    "所有云端 Provider 均请求失败，请检查网络或 API Key 是否有效。"
                    if is_zh else
                    "All cloud providers failed. Check the network connection and API keys."
                ],
            )
        if normalized == "empty_response":
            return cls(
                error=normalized,
                # 597899e 把这里从 error 降为 warning（"minimalist" 的空响应 UI）。
                # 原注释主张相反——本轮已终止，视觉上应与普通提示区分——已随代码
                # 更正，免得文件继续自相矛盾。
                level="warning",
                lines=(
                    [
                        "模型连续返回空响应（已自动重试 1 次），本轮停止。",
                        "可换个说法重试；若持续出现，请使用 /health 检查当前模型与 Provider。",
                    ]
                    if is_zh else
                    [
                        "The model returned no visible response twice (auto-retried once), so this turn was stopped.",
                        "Try rephrasing. If it persists, run /health to check the model and provider.",
                    ]
                ),
            )
        return cls(
            error=normalized,
            # Also lowered by 597899e. Note this is the catch-all branch: an
            # unexpected failure now renders with the same weight as a rate-limit
            # notice. Deliberate per that commit, but worth revisiting — the
            # dataclass default below it is still "error".
            level="warning",
            lines=[f"Error: {normalized}"],
            use_generic_error_prefix=True,
        )


@dataclass
class ToolBatchState:
    """Mutable state for one model-requested batch of tool calls."""

    tool_results: List[dict] = field(default_factory=list)
    elapsed_total: float = 0.0
    cancelled: bool = False

    def add_result(
        self,
        tool_name: str,
        result: dict,
        formatter: SummaryFormatter,
        *,
        elapsed: float = 0.0,
    ) -> dict:
        self.elapsed_total += elapsed
        return record_tool_result(self.tool_results, tool_name, result, formatter)

    def cancel(self) -> None:
        self.cancelled = True

    def build_next_turn(self, total_response: str) -> Tuple[dict, dict, str]:
        return build_next_turn_messages(total_response, self.tool_results)


@dataclass(frozen=True)
class ToolCallTask:
    """One ordered tool call in a model-requested turn."""

    index: int
    tool_call: dict
    parallel_result: dict | None = None

    @property
    def tool_name(self) -> str:
        return self.tool_call.get("tool", "")

    @property
    def params(self) -> dict:
        return self.tool_call.get("params", {})

    @property
    def has_parallel_result(self) -> bool:
        return self.parallel_result is not None

    def progress_label(self, total: int) -> str:
        if total > 1:
            return f"  [{self.index + 1}/{total}] Running {self.tool_name}..."
        return f"  Running {self.tool_name}..."


@dataclass
class ToolTurnPlan:
    """Runtime plan for executing one pending tool-call turn."""

    pending: Sequence[dict]
    parallel_done: Dict[int, dict] = field(default_factory=dict)
    batch: ToolBatchState = field(default_factory=ToolBatchState)

    def tasks(self) -> List[ToolCallTask]:
        return [
            ToolCallTask(
                index=index,
                tool_call=tool_call,
                parallel_result=self.parallel_done.get(index),
            )
            for index, tool_call in enumerate(self.pending)
        ]


@dataclass(frozen=True)
class ToolExecutionActivity:
    """One executed tool activity in a model-requested batch."""

    tool: str
    result: dict
    elapsed: float
    params: dict
    from_parallel: bool = False


@dataclass(frozen=True)
class ToolExecutionTurnResult:
    """Structured result from one pending tool-call turn."""

    batch: ToolBatchState
    activities: List[ToolExecutionActivity]
    assistant_message: dict
    tool_messages: List[dict]
    user_message: dict
    followup: str
    guard_directives: List[str] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.batch.cancelled


async def run_parallel_tools(
    pending: Sequence[dict],
    tool_executor: ToolExecutor,
    *,
    remote_runner: RemoteToolRunner | None = None,
    hook: Hook | None = None,
    serial_tools: Iterable[str] = DEFAULT_SERIAL_TOOLS,
) -> Dict[int, dict]:
    """Execute parallel-safe pending tools and return results by original index."""
    parallel_batch, _ = split_tool_calls(pending, serial_tools)

    async def _exec_one(tool_call: dict) -> tuple:
        tool_name = tool_call.get("tool", "")
        tool_params = tool_call.get("params", {})
        if tool_name in tool_executor.local_tools:
            result = await tool_executor.execute(tool_name, tool_params)
        elif remote_runner is not None:
            if hook is not None:
                hook("pre_tool", tool_name, tool_params, None)
            try:
                result = await remote_runner(tool_name, tool_params)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            if hook is not None:
                hook("post_tool", tool_name, tool_params, result)
        else:
            result = {"success": False, "error": f"Unknown tool: {tool_name}"}
        return tool_call, result

    parallel_results: List[tuple] = []
    if parallel_batch:
        gathered = await asyncio.gather(
            *[_exec_one(tool_call) for tool_call in parallel_batch],
            return_exceptions=True,
        )
        for item in gathered:
            if isinstance(item, Exception):
                parallel_results.append((None, {"success": False, "error": str(item)}))
            else:
                parallel_results.append(item)
    return collect_parallel_done(pending, parallel_results, serial_tools)


async def run_serial_tool(
    tool_name: str,
    tool_params: dict,
    tool_executor: ToolExecutor,
    *,
    remote_runner: RemoteToolRunner | None = None,
    hook: Hook | None = None,
    approval: ApprovalDecision | None = None,
) -> Tuple[dict, float]:
    """Execute one tool call and return (result, elapsed_seconds)."""
    started = time.time()
    if tool_name in tool_executor.local_tools:
        result = tool_executor.execute_local(tool_name, tool_params, approval=approval)
    elif remote_runner is not None:
        if hook is not None:
            hook("pre_tool", tool_name, tool_params, None)
        try:
            result = await remote_runner(tool_name, tool_params)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        if hook is not None:
            hook("post_tool", tool_name, tool_params, result)
    else:
        result = {"success": False, "error": f"Unknown tool: {tool_name}"}
    return result, time.time() - started


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def execute_tool_turn(
    pending: Sequence[dict],
    *,
    total_response: str,
    tool_executor: ToolExecutor,
    formatter: SummaryFormatter,
    remote_runner: RemoteToolRunner | None = None,
    hook: Hook | None = None,
    cancel_event: asyncio.Event | None = None,
    confirm_tools: Iterable[str] = (),
    approval_callback: ApprovalCallback | None = None,
    approval_applier: Callable[[dict, ApprovalDecision], dict] = apply_approval_decision,
    loop_guard: LoopGuard | None = None,
    serial_tools: Iterable[str] = DEFAULT_SERIAL_TOOLS,
    contract: "ChangeContract | None" = None,
) -> ToolExecutionTurnResult:
    """Execute one model-requested tool batch and prepare the next turn.

    This is the reusable runtime boundary for the agent tool loop.  Callers
    provide UI-specific approval and rendering callbacks; this function owns
    batching, permission application, execution, loop-guard retry directives,
    and construction of the assistant/user follow-up messages.
    """

    confirm = set(confirm_tools)
    # Keep the model-facing call transcript free of runtime-only approval
    # fields that may be injected into the executable parameter dictionary.
    pending_transcript = [
        {
            "tool": str(tool_call.get("tool", "")),
            "params": dict(tool_call.get("params", {}) or {}),
            # Opaque provider tokens (Gemini call ids / thought signatures)
            # that must be replayed with the call in the next request.
            **{
                key: tool_call[key]
                for key in ("call_id", "thought_signature")
                if tool_call.get(key)
            },
        }
        for tool_call in pending
    ]
    # The contract is checked before anything runs — parallel-safe calls run
    # first, all at once — and a refused call never reaches the executor: its
    # refusal stands in for its result, so the model learns which rule held.
    refused: Dict[int, dict] = {}
    if contract is not None:
        for index, tool_call in enumerate(pending):
            verdict = contract.check(str(tool_call.get("tool", "")), tool_call.get("params", {}) or {})
            if not verdict.allowed:
                refused[index] = verdict.as_tool_result(str(tool_call.get("tool", "")))
    runnable_indices = [index for index in range(len(pending)) if index not in refused]
    ran_in_parallel = await run_parallel_tools(
        [pending[index] for index in runnable_indices],
        tool_executor,
        remote_runner=remote_runner,
        hook=hook,
        serial_tools=serial_tools,
    )
    parallel_done = {runnable_indices[i]: result for i, result in ran_in_parallel.items()}
    parallel_done.update(refused)
    tool_turn = ToolTurnPlan(pending=pending, parallel_done=parallel_done)
    tool_batch = tool_turn.batch
    activities: List[ToolExecutionActivity] = []

    for task in tool_turn.tasks():
        if cancel_event and cancel_event.is_set():
            tool_batch.cancel()
            break

        tool_name = task.tool_name
        tool_params = task.params

        if task.has_parallel_result:
            tr = task.parallel_result or {}
            tool_batch.add_result(tool_name, tr, formatter)
            activities.append(ToolExecutionActivity(
                tool=tool_name,
                result=tr,
                elapsed=0.0,
                params=tool_params,
                from_parallel=True,
            ))
            continue

        approval = None
        if tool_name in confirm:
            if approval_callback is None:
                tool_batch.cancel()
                break
            decision = await _maybe_await(approval_callback(tool_name, tool_params))
            if decision is None:
                decision = ApprovalDecision.deny("approval unavailable")
            if not decision.approved and decision.feedback:
                # "No, do this instead": the call is skipped and the turn goes
                # on with the user's words as its result, as Codex does.
                declined = decision.as_declined_result()
                tool_batch.add_result(tool_name, declined, formatter)
                activities.append(ToolExecutionActivity(
                    tool=tool_name, result=declined, elapsed=0.0, params=tool_params))
                continue
            if not decision.approved:
                tool_batch.cancel()
                break
            approval_applier(tool_params, decision)
            approval = decision

        tr, tool_elapsed = await run_serial_tool(
            tool_name,
            tool_params,
            tool_executor,
            remote_runner=remote_runner,
            hook=hook,
            approval=approval,
        )
        tool_batch.add_result(tool_name, tr, formatter, elapsed=tool_elapsed)
        activities.append(ToolExecutionActivity(
            tool=tool_name,
            result=tr,
            elapsed=tool_elapsed,
            params=tool_params,
        ))

    guard_directives: List[str] = []
    if loop_guard is not None:
        for activity in activities:
            directive = loop_guard.record(activity.tool, activity.params, activity.result)
            if directive:
                guard_directives.append(directive)

    assistant_message, user_message, followup = tool_batch.build_next_turn(total_response)
    assistant_message["tool_calls"] = [
        {
            "function": {
                "name": str(tool_call.get("tool", "")),
                "arguments": dict(tool_call.get("params", {}) or {}),
            },
            **({"id": tool_call["call_id"]} if tool_call.get("call_id") else {}),
            **(
                {"thought_signature": tool_call["thought_signature"]}
                if tool_call.get("thought_signature")
                else {}
            ),
        }
        for tool_call in pending_transcript
        if tool_call.get("tool")
    ]
    tool_messages = [
        {
            "role": "tool",
            "content": str(item.get("result", "")),
            "name": str(item.get("tool", "")),
        }
        for item in tool_batch.tool_results
    ]
    if guard_directives:
        guard_text = "\n\n".join(guard_directives)
        if isinstance(user_message.get("content"), str):
            user_message["content"] += f"\n\n{guard_text}"
        elif isinstance(user_message.get("content"), list):
            user_message["content"].append({"type": "text", "text": guard_text})
        followup += f"\n\n{guard_text}"

    return ToolExecutionTurnResult(
        batch=tool_batch,
        activities=activities,
        assistant_message=assistant_message,
        tool_messages=tool_messages,
        user_message=user_message,
        followup=followup,
        guard_directives=guard_directives,
    )


# Cap each tool result so a single large output (a long pip-install log, a big
# data dump, a verbose traceback) cannot blow past the model's context window.
# Without this, one oversized result is appended verbatim, the next provider
# call exceeds num_ctx, the prompt is truncated, and the model loses the task
# mid-run. Head+tail keeps the actionable parts (what ran / the final error)
# and drops the noisy middle.
_MAX_TOOL_RESULT_CHARS = 6000


def _truncate_tool_result(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n… [已截断 {omitted:,} 字符 — 输出过长，仅保留首尾以保护上下文] …\n\n"
        + text[-tail:]
    )


_MAX_TEXT_TOOL_CALL_RETRIES = 2

# What a reply looks like when the model wrote tool calls instead of making
# them: a raw call tag, Gemini's "default_api:" call syntax, or an imitation of
# the "## Tool Results" block build_tool_followup produces.
_TEXT_TOOL_CALL = re.compile(
    r"<tool_call>|\bdefault_api[:.]\w|^#{2,3} ?Tool Results\b|^### \[\w+\] (?:✓ Success|❌ Error)",
    re.MULTILINE,
)

TEXT_TOOL_CALL_DIRECTIVE = (
    "Your last reply wrote tool calls (or tool results) as text. None of them "
    "were executed and no files were changed. Do not write calls or results "
    "out as text: call the tools through function calling, and only report "
    "results you actually received."
)


_CODE_SPAN = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)


def looks_like_text_tool_calls(text: str) -> bool:
    """True when a reply with no real tool calls contains written-out ones.

    Code spans are ignored, so an answer that quotes these formats — this
    being a coding agent, a repo can well contain them — is not mistaken
    for one.
    """
    return bool(text) and bool(_TEXT_TOOL_CALL.search(_CODE_SPAN.sub("", text)))


def build_tool_followup(tool_results: Sequence[dict]) -> str:
    """Build a structured follow-up message from tool results.

    Each result block is labelled with its tool name and a success/error
    status so the model can clearly distinguish outcomes and respond
    appropriately to failures rather than silently ignoring them. Each result
    is size-capped (see ``_truncate_tool_result``) so a single huge output
    cannot overflow the context window and cut the task short.
    """
    if not tool_results:
        return "No tool results. Continue with what you know or ask the user for clarification."

    blocks: List[str] = []
    error_tools: List[str] = []

    for item in tool_results:
        tool = item.get("tool", "unknown")
        result = item.get("result", "")
        result_str = _truncate_tool_result(str(result))

        is_error = (
            result_str.startswith("Error") or
            result_str.startswith("❌") or
            "error" in result_str[:80].lower() or
            "traceback" in result_str[:200].lower() or
            "exception" in result_str[:200].lower()
        )
        status = "❌ Error" if is_error else "✓ Success"
        if is_error:
            error_tools.append(tool)
        blocks.append(f"### [{tool}] {status}\n{result_str}")

    followup = "## Tool Results\n\n" + "\n\n---\n\n".join(blocks)

    if error_tools:
        healing_directives = []
        import re as _re_heal
        for item in tool_results:
            if item.get("tool") == "run_command":
                res_text = str(item.get("result", ""))
                if "Traceback" in res_text or "Error:" in res_text:
                    m_line = _re_heal.search(r'File ["\']([^"\']+)["\'], line (\d+)', res_text)
                    if m_line:
                        fpath, lineno = m_line.group(1), m_line.group(2)
                        healing_directives.append(
                            f"📌 [Self-Healing Directive] Error located at `{fpath}` line {lineno}. "
                            f"Use `edit_file` to patch this specific line/block rather than rewriting the entire file. "
                            f"Then re-run `run_command` to verify the fix."
                        )

        followup += (
            f"\n\n⚠ Tool(s) returned errors: {', '.join(error_tools)}. "
            "Read the error carefully. Options: (1) use read_file / search_code to diagnose, "
            "(2) use edit_file to fix the issue and retry run_command, "
            "(3) try a different approach. "
            "Do NOT give up silently — explain what failed and what you tried."
        )
        if healing_directives:
            followup += "\n\n" + "\n".join(healing_directives)
    else:
        followup += (
            "\n\nAll tools completed successfully. "
            "If the task is now complete, provide your final response. "
            "If additional steps are needed, continue using tools.\n\n"
            "Please continue your analysis using these results."
        )

    return followup


def record_tool_result(
    tool_results: List[dict],
    tool_name: str,
    result: dict,
    formatter: SummaryFormatter,
) -> dict:
    """Append one tool result summary and return the appended record."""
    summary = formatter(tool_name, result)
    record = {"tool": tool_name, "result": summary}
    tool_results.append(record)
    return record


def build_next_turn_messages(total_response: str, tool_results: Sequence[dict]) -> Tuple[dict, dict, str]:
    """Build assistant/user messages and follow-up text for the next agent turn.

    When a screenshot tool stored an image in computer_use_tools._PENDING_VISION_IMAGE,
    the user message content becomes a multipart list so vision models can see the image.
    """
    followup = build_tool_followup(tool_results)
    assistant_message = {"role": "assistant", "content": total_response}

    # Check for a pending screenshot from computer_screenshot / browser_screenshot
    vision_b64: "str | None" = None
    try:
        from aria_code.computer_use_tools import pop_pending_vision_image
        vision_b64 = pop_pending_vision_image()
    except ImportError:
        pass

    if vision_b64:
        user_content: "str | list" = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{vision_b64}"},
            },
            {"type": "text", "text": followup},
        ]
    else:
        user_content = followup

    user_message = {"role": "user", "content": user_content}
    return assistant_message, user_message, followup


# ── AgentEvent typed union ────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentEventToken:
    """A text token streamed from the model."""
    text: str


@dataclass(frozen=True)
class AgentEventThinking:
    """A thinking/reasoning token from extended-thinking models."""
    content: str


@dataclass(frozen=True)
class AgentEventToolCall:
    """Model requested a tool call (before execution)."""
    tool: str
    params: dict


@dataclass(frozen=True)
class AgentEventToolResult:
    """One tool has finished executing."""
    tool: str
    result: dict
    elapsed: float


@dataclass(frozen=True)
class AgentEventStatus:
    """Informational status change (e.g. provider fallback)."""
    state: str
    message: str


@dataclass(frozen=True)
class AgentEventComplete:
    """Agent loop finished normally. Carries the full turn result."""
    result: "AgentTurnResult"


@dataclass(frozen=True)
class AgentEventCancelled:
    """Agent loop was cancelled by the user."""
    partial_text: str


@dataclass(frozen=True)
class AgentEventError:
    """Agent loop encountered an unrecoverable error."""
    error: str


AgentEvent = Union[
    AgentEventToken,
    AgentEventThinking,
    AgentEventToolCall,
    AgentEventToolResult,
    AgentEventStatus,
    AgentEventComplete,
    AgentEventCancelled,
    AgentEventError,
]


# ── AgentOptions ──────────────────────────────────────────────────────────────

@dataclass
class AgentOptions:
    """Tunable parameters for one run_agent() invocation."""

    max_rounds: int = 30
    serial_tools: FrozenSet[str] = field(
        default_factory=lambda: frozenset(DEFAULT_SERIAL_TOOLS)
    )
    tool_schemas: List[dict] = field(default_factory=list)
    # Interactive tool approval (UI adapters inject these; None = execute
    # without prompting, matching the historical embedded-loop default only
    # for callers that never wire an approval UI).
    confirm_tools: FrozenSet[str] = field(default_factory=frozenset)
    approval_callback: Optional[ApprovalCallback] = None
    approval_applier: Optional[Callable[[dict, "ApprovalDecision"], dict]] = None
    requires_evidence: bool = False
    grounding_tools: FrozenSet[str] = field(default_factory=frozenset)
    evidence_already_grounded: bool = False
    # 花费上限。None = 不追踪（保持既有行为，调用方不传就跟以前一模一样）。
    # 传入一个 BudgetTracker 后，循环会在**每轮开始前**检查，超限则停止并把
    # 原因写进 result["budget_paused"]，而不是抛异常——抛异常会让调用方拿不到
    # 已经产出的中间结果。
    budget: Optional["BudgetTracker"] = None
    # 验收闸门。None = 不验收（保持既有行为）。传入一个 AcceptanceGate 后，
    # 只要本轮真的改写了磁盘上的文件，模型宣称完成时循环会先跑一遍推断出的
    # 检查命令；红了就把失败输出回灌给模型继续修，绿了才让这一轮结束。
    acceptance: Optional["AcceptanceGate"] = None
    # Change contract. None = no task bounds beyond the permission mode. With
    # one, the model is shown it before the first round and the runtime
    # refuses every tool call that breaks it (see runtime/contract.py).
    contract: Optional["ChangeContract"] = None
    # Independent review. None = none. With a ReviewGate, a turn that changed
    # files is read by a fresh-context reviewer (goal, contract, checks, diff —
    # not this transcript) once its checks are green; blocking findings go
    # back to the model like red checks do (see runtime/review.py).
    review: Optional["ReviewGate"] = None


# ── run_agent() ───────────────────────────────────────────────────────────────

async def run_agent(
    prompt: str,
    history: list,
    *,
    provider_fn: Callable,
    tool_executor: ToolExecutor,
    options: Optional["AgentOptions"] = None,
    remote_runner: Optional[RemoteToolRunner] = None,
    on_token: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, dict], None]] = None,
    on_tool_result: Optional[Callable[[str, dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    hook: Optional[Hook] = None,
    cancel_event: Optional[asyncio.Event] = None,
    tool_result_formatter: Optional[SummaryFormatter] = None,
) -> AsyncGenerator["AgentEvent", None]:
    """Provider-agnostic multi-round agent loop.

    Yields ``AgentEvent`` objects so every caller (REPL, bot, API) can
    handle UI in its own way without duplicating round-management logic.

    Parameters
    ----------
    prompt:
        The user's message for this turn.
    history:
        Conversation history **before** the current prompt.
    provider_fn:
        Async callable ``(message, history, on_token, on_thinking,
        on_tool_call, cancel_event) -> dict``.  Must return the same
        result dict that ``stream_ollama`` / ``stream_chat`` return.
    tool_executor:
        Local tool registry.
    options:
        Tunable loop parameters (max_rounds, serial_tools, …).
    remote_runner:
        Optional async callable for tools not in ``tool_executor``.
    on_token / on_thinking / on_tool_call / on_tool_result / on_status:
        Pass-through callbacks forwarded to ``provider_fn`` so callers
        that already set up streaming callbacks don't need to change.
    hook:
        Pre/post-tool hook fired around each tool execution.
    cancel_event:
        asyncio.Event; when set the loop exits at the next safe point.
    tool_result_formatter:
        Formats a tool result dict into a summary string.  Defaults to
        ``str(result.get('output', result))``.
    """
    opts = options or AgentOptions()
    _formatter: SummaryFormatter = tool_result_formatter or (
        lambda _tool, res: str(res.get("output", res))
    )
    _serial = set(opts.serial_tools)

    turn_state = AgentTurnState(provider="unknown", _budget=opts.budget)
    start_time = time.time()
    current_message = prompt
    if opts.requires_evidence:
        available_grounding_tools = ", ".join(sorted(opts.grounding_tools)[:20]) or "(none)"
        current_message = (
            "[Grounding requirement]\n"
            "This request requires current, auditable financial evidence. Call at least "
            "one applicable grounding tool before producing a market conclusion. If the "
            "tool fails or data is unavailable, report that limitation and do not infer "
            "prices, metrics, recommendations, or forecasts.\n"
            f"Grounding tools: {available_grounding_tools}\n\n"
            f"[User request]\n{prompt}"
        )
    if opts.contract is not None:
        request = current_message if opts.requires_evidence else f"[User request]\n{prompt}"
        current_message = f"{opts.contract.prompt_block()}\n\n{request}"
    contract_refusals: List[dict] = []
    reviewed_diff = ""
    ledger = DeliveryLedger(root=str(
        getattr(opts.acceptance, "root", None) or getattr(opts.contract, "root", None) or "") or None)
    token_count = 0
    thinking_tokens = 0
    result: dict = {}
    loop_guard = LoopGuard()
    grounded_results = 1 if opts.evidence_already_grounded else 0
    stop_reason = "max_rounds"
    text_tool_call_retries = 0

    for round_num in range(opts.max_rounds):
        # ── 预算闸门 ─────────────────────────────────────────────────────────
        # 放在发起调用**之前**：花完才发现超支，每次都会多花一轮，而这一轮
        # 恰恰可能是塞了满上下文的那一轮。
        if opts.budget is not None and not opts.budget.should_continue(
            projected_usd=opts.budget.projected_next_round_usd()
        ):
            # 本模块不持有 logger（全靠 yield 事件对外汇报），所以暂停原因
            # 只写进 result，由调用方决定怎么呈现——CLI 会打印，后台任务会
            # 记进 task ledger。
            reason = opts.budget.paused_reason or "budget exhausted"
            result["budget_paused"] = reason
            result["budget_paused_round"] = round_num + 1
            # 走既有的 hook 通道对外汇报，不新造一条：CLI 已经把 hook 接到
            # RuntimeTrace，事件会自动进入 /trace 和跨会话的 run_store，
            # 花费停在哪一轮、当时用量多少事后可查。
            if hook is not None:
                hook("budget_paused", "budget", {
                    "round": round_num + 1,
                    "reason": reason,
                    "spent_usd": round(opts.budget.state.spent_usd, 6),
                    "total_tokens": opts.budget.state.total_tokens,
                    "per_provider": dict(opts.budget.state.per_provider),
                }, None)
            result["budget_summary"] = opts.budget.summary()
            stop_reason = "budget_exhausted"
            yield AgentEventStatus(state=stop_reason, message=reason)
            break

        if opts.budget is not None:
            opts.budget.record_round()

        # ── Provider call ────────────────────────────────────────────────────
        response_text = ""
        _round_tokens = 0

        def _wrap_on_token(tok: str) -> None:
            nonlocal response_text, token_count, _round_tokens
            response_text += tok
            _round_tokens += 1
            token_count += 1
            if on_token is not None and (not opts.requires_evidence or grounded_results > 0):
                on_token(tok)

        def _wrap_on_thinking(content: str) -> None:
            nonlocal thinking_tokens
            thinking_tokens += 1
            if on_thinking is not None:
                on_thinking(content)

        def _wrap_on_tool_call(tool: str, params: dict) -> None:
            if on_tool_call is not None:
                on_tool_call(tool, params)

        try:
            result = await provider_fn(
                current_message,
                history,
                on_token=_wrap_on_token,
                on_thinking=_wrap_on_thinking,
                on_tool_call=_wrap_on_tool_call,
                on_tool_result=on_tool_result,
                on_status=on_status,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            yield AgentEventError(error=str(exc))
            return

        if result.get("cancelled"):
            turn_state.append_response(response_text)
            yield AgentEventCancelled(partial_text=turn_state.total_response)
            return

        if not result.get("success"):
            yield AgentEventError(error=result.get("error", "Unknown error"))
            return

        turn_state.apply_model_result(result, response_text)

        pending = result.get("tool_calls_pending", [])
        if not pending and looks_like_text_tool_calls(result.get("response") or response_text):
            # The model wrote its calls (and often their "results") as prose:
            # nothing ran. Ending here reported a task as done — sometimes with
            # tests "passing" — while no file had been touched. Say so and
            # give it the round back; if it keeps doing it, end incomplete.
            text_tool_call_retries += 1
            if text_tool_call_retries > _MAX_TEXT_TOOL_CALL_RETRIES:
                stop_reason = "text_tool_calls"
                yield AgentEventStatus(
                    state=stop_reason,
                    message="Model kept writing tool calls as text; task remains incomplete",
                )
                turn_state.append_response(
                    "\n\nThe model kept writing tool calls as text instead of "
                    "calling the tools; none of them were executed."
                )
                break
            yield AgentEventStatus(
                state="text_tool_calls",
                message="Model wrote tool calls as text; asking it to call them",
            )
            history = list(history) + [
                {"role": "user", "content": current_message},
                {"role": "assistant", "content": turn_state.total_response},
            ]
            current_message = TEXT_TOOL_CALL_DIRECTIVE
            turn_state.reset_response()
            continue
        if not pending:
            # ── 验收闸门 ─────────────────────────────────────────────────────
            # 模型不再要工具 = 它认为做完了。这是唯一一个「宣称完成」的出口,
            # 所以检查必须挂在这里:挂在写文件之后太早(改到一半必然是红的),
            # 挂在循环结束之后太晚(那时已经没有轮次可以拿来修了)。
            if opts.acceptance is not None and opts.acceptance.should_run():
                report = await opts.acceptance.run()
                if report is not None:
                    yield AgentEventStatus(
                        state="acceptance_passed" if report.passed else "acceptance_failed",
                        message=report.headline(),
                    )
                    if hook is not None:
                        hook("acceptance", "verify", report.summary(), None)
                    if report.ran and not report.passed:
                        # 把失败输出当成下一轮的用户消息回灌。走和工具结果
                        # 完全相同的通道,模型不需要认识一种新的消息类型。
                        history = list(history) + [
                            {"role": "user", "content": current_message},
                            {"role": "assistant", "content": turn_state.total_response},
                        ]
                        current_message = report.repair_directive()
                        turn_state.reset_response()
                        continue
            # ── Independent review ───────────────────────────────────────────
            # After the checks, never instead of them: a red run is repaired
            # first, and reviewing code that does not pass its own tests spends
            # a model call to learn what the tests already said.
            if opts.review is not None:
                diff = ledger.diff_text()
                acceptance_state = opts.acceptance.summary() if opts.acceptance is not None else {}
                if acceptance_state.get("verified") is not False and opts.review.should_run(
                        changed=ledger.changed, reviewed_diff=reviewed_diff, diff=diff):
                    last_checks = (acceptance_state.get("reports") or [{}])[-1].get("checks") or []
                    review = await opts.review.run(
                        goal=prompt,
                        diff=diff,
                        checks=last_checks,
                        contract=opts.contract.render() if opts.contract is not None else "",
                    )
                    reviewed_diff = diff
                    yield AgentEventStatus(
                        state={"pass": "review_passed", "blocking": "review_blocking"}.get(
                            review.verdict, "review_error"),
                        message=review.headline(),
                    )
                    if hook is not None:
                        hook("review", "review", review.as_dict(), None)
                    if review.blocking and opts.review.can_repair():
                        history = list(history) + [
                            {"role": "user", "content": current_message},
                            {"role": "assistant", "content": turn_state.total_response},
                        ]
                        current_message = review.repair_directive()
                        turn_state.reset_response()
                        continue
            if opts.requires_evidence and grounded_results == 0:
                yield AgentEventStatus(
                    state="evidence_required",
                    message="No usable financial evidence tool result was obtained",
                )
                yield AgentEventError(
                    error=(
                        "[ARIA-4223] Current verified financial data is required, "
                        "but no usable evidence tool result was obtained."
                    )
                )
                return
            stop_reason = "completed"
            break
        for tool_call in pending:
            yield AgentEventToolCall(
                tool=tool_call.get("tool", ""),
                params=tool_call.get("params", {}),
            )

        # Warn caller on final round
        if round_num == opts.max_rounds - 1:
            yield AgentEventStatus(
                state="max_rounds",
                message=f"Max rounds ({opts.max_rounds}) reached",
            )
            break

        # ── Tool execution ───────────────────────────────────────────────────
        tool_turn_result = await execute_tool_turn(
            pending,
            total_response=turn_state.total_response,
            tool_executor=tool_executor,
            formatter=_formatter,
            remote_runner=remote_runner,
            hook=hook,
            cancel_event=cancel_event,
            loop_guard=loop_guard,
            serial_tools=_serial,
            confirm_tools=opts.confirm_tools,
            approval_callback=opts.approval_callback,
            approval_applier=opts.approval_applier or apply_approval_decision,
            contract=opts.contract,
        )

        for activity in tool_turn_result.activities:
            turn_state.tools_used.append(activity.tool)
            ledger.record(activity.tool, activity.params, activity.result)
            refusal = (activity.result or {}).get("contract_violation")
            if refusal:
                contract_refusals.append(dict(refusal))
                yield AgentEventStatus(
                    state="contract_blocked",
                    message=f"{activity.tool} refused by the change contract: {refusal.get('reason', '')}",
                )
            if opts.acceptance is not None:
                opts.acceptance.record_tool(activity.tool, activity.result)
            canonical_tool = str(activity.tool).rsplit("__", 1)[-1]
            allowed_tools = {
                str(name).rsplit("__", 1)[-1]
                for name in opts.grounding_tools
            }
            result_payload = dict(activity.result or {})
            usable_payload = {
                key: value
                for key, value in result_payload.items()
                if key not in {"success", "cached", "elapsed_ms"}
            }
            if (
                canonical_tool in allowed_tools
                and result_payload.get("success") is not False
                and not result_payload.get("error")
                and any(value not in (None, "", [], {}) for value in usable_payload.values())
            ):
                grounded_results += 1
            yield AgentEventToolResult(
                tool=activity.tool,
                result=activity.result,
                elapsed=activity.elapsed,
            )

        turn_state.add_tool_time(tool_turn_result.batch.elapsed_total)
        if tool_turn_result.cancelled:
            yield AgentEventCancelled(partial_text=turn_state.total_response)
            return
        if tool_turn_result.guard_directives:
            yield AgentEventStatus(
                state="loop_guard",
                message="Repeated failing tool call detected",
            )

        history = list(history) + [
            {"role": "user", "content": current_message},
            tool_turn_result.assistant_message,
            *tool_turn_result.tool_messages,
        ]
        current_message = tool_turn_result.followup
        turn_state.reset_response()

        if loop_guard.should_break:
            stop_reason = "loop_guard"
            yield AgentEventStatus(
                state=stop_reason, message="Repeated failing tool calls; task remains incomplete"
            )
            turn_state.append_response(
                "\n\nRepeated failing tool calls were detected and the agent stopped retrying."
            )
            break

    # ── Build final result ───────────────────────────────────────────────────
    elapsed = time.time() - start_time
    acceptance_summary = (
        opts.acceptance.summary()
        if opts.acceptance is not None and opts.acceptance.reports else None
    )
    if stop_reason == "completed" and acceptance_summary and acceptance_summary.get("verified") is False:
        stop_reason = "checks_failed"
    contract_summary = (
        {
            "goal": opts.contract.goal,
            "source": opts.contract.source,
            "text": opts.contract.render(),
            "refused": contract_refusals,
        }
        if opts.contract is not None else None
    )
    delivery = ledger.report(
        acceptance=acceptance_summary,
        contract=contract_summary,
        review=opts.review.summary() if opts.review is not None else None,
        stop_reason=stop_reason,
    )
    turn_result = turn_state.build_result(
        elapsed=elapsed,
        success=stop_reason == "completed",
        error="" if stop_reason == "completed" else stop_reason,
        stop_reason=stop_reason,
        fallback_response=result.get("response", ""),
        token_count=token_count,
        thinking_tokens=thinking_tokens,
        acceptance=acceptance_summary,
        contract=contract_summary,
        delivery=delivery.as_dict() if delivery.worth_showing else None,
    )
    yield AgentEventComplete(result=turn_result)
