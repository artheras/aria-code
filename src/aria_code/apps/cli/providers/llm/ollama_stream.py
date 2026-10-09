"""stream_ollama — Ollama streaming function extracted from aria_cli.py.

Module globals are rebound to aria_cli's namespace by the function-rebind
shim in aria_cli.py after import, so all bare name references resolve correctly.
"""
from __future__ import annotations
import asyncio
import re
from datetime import datetime

try:
    from packages.aria_skills.loader import CORE_FILE_TOOLS as _CORE_FILE_TOOLS
except Exception:
    _CORE_FILE_TOOLS: frozenset[str] = frozenset()

# Imported rather than borrowed from aria_cli's globals: this function is also
# reached without aria_cli loaded (SDK, daemon), where the rebind never runs.
from .response_cache import (
    cache_get as _cache_get,
    cache_key as _cache_key,
    cache_set as _cache_set,
)

# ── names reclaimed from the borrow set ──────────────────────────────────────
# stream_ollama borrows 45 bare names from aria_cli's module globals. The
# rebind in aria_cli._rebind_stream_ollama MERGES rather than replaces —
# `dict(source.__globals__)` then `.update(globals())` — so anything defined
# here is the base of that merge and aria_cli still wins on conflicts. Adding a
# name here therefore cannot change the CLI path; it only gives the fallback
# path (SDK, daemon, any consumer that never imports aria_cli) a name it
# previously died on.
#
# Every name below was checked to be *the same object* aria_cli holds. That
# check is the whole point, not ceremony:
#
#   · _CONFIRM_TOOLS looks importable from apps.cli.tool_executor, but that
#     module's copy is a placeholder stub — an empty set() — while aria_cli's
#     is the real {'edit_file', 'multi_edit', ...}. Importing the obvious
#     source would have silently made every tool skip confirmation. Fail open.
#   · _apply_tool_approval, _confirm_tool_execution_decision and
#     _format_tool_summary resolve to different function objects in
#     tool_executor than in aria_cli, and they sit on the approval path.
#   · get_ariarc and build_tool_system_prompt differ only by import root
#     (`ariarc` vs `aria_code.ariarc`) — importing either here would add a
#     second module object rather than remove a borrow.
#   · _try_prefetch_market_data exists in handlers.market_handlers, but
#     aria_cli defines its own.
#
# tests/test_ollama_stream_imports.py enforces the identity rule, so this list
# cannot grow a diverging entry later.
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# market_detect owns it; deterministic only re-exports. Same object either
# way, but going direct keeps this module from pulling in deterministic
# (and, now, the domain packs behind it).
from aria_code.apps.cli.utils.market_detect import _is_broker_intent
from aria_code.apps.cli.helpers import detect_ollama_models_rich
from aria_code.apps.cli.model_catalog import (
    _HAS_MODEL_CAP,
    get_model_cfg,
    resolve_model_key,
)
from aria_code.apps.cli.prompts.coding import CODING_SYSTEM_PROMPT
from aria_code.apps.cli.tool_registry import LOCAL_TOOL_SCHEMAS
from aria_code.apps.cli.tools.market_tools import _HAS_MDC
from aria_code.ui.console import HAS_RICH, Panel, console

# Second batch. Same rule as above — each verified to be the object aria_cli
# holds, which is why _format_tool_summary is still absent: apps.cli.
# tool_executor's copy is a different function object from aria_cli's and sits
# on the tool-output path.
#
# The four intent predicates and the three message helpers were already
# extracted; aria_cli only aliases them, so importing the real modules here
# removes a borrow without moving any code. The three offline responders were
# genuinely still in aria_cli and moved to offline_responses.py — a provider's
# "backend unavailable" message belongs with the provider, and the fallback
# path could not previously build its own.
from aria_code.apps.cli.intent import (
    is_analysis_request as _is_analysis_request,
    is_coding_request as _is_coding_request,
    is_general_knowledge as _is_general_knowledge,
    is_sports_query as _is_sports_query,
)
from aria_code.apps.cli.message_processing import (
    compact_messages as _compact_messages,
    parse_text_tool_calls as _parse_text_tool_calls,
    strip_tool_call_tags as _strip_tool_call_tags,
)
from .offline_responses import (
    is_simple_greeting as _is_simple_greeting,
    offline_greeting_response as _offline_greeting_response,
    ollama_unavailable_result as _ollama_unavailable_result,
)

# Third batch. aria_cli held wrappers around all of these; converting those to
# aliases is what makes the import safe, because a wrapper is a different
# function object from the thing it wraps.
from aria_code.apps.cli.handlers.market_handlers import _try_prefetch_market_data
from aria_code.apps.cli.handlers.sports_handlers import (
    try_prefetch_sports_data as _try_prefetch_sports_data,
)
from aria_code.apps.cli.message_processing import (
    build_broker_context_block as _build_broker_context_block,
)
from aria_code.apps.cli.prompts.system_prompts import (
    build_analysis_prompt_lite as _build_analysis_prompt_lite,
    build_analysis_system_prompt as _build_analysis_system_prompt,
    build_coding_prompt_lite as _build_coding_prompt_lite,
    build_finance_prompt as _build_finance_prompt,
    build_prefetched_analysis_prompt as _build_prefetched_analysis_prompt,
)

# Not an import: build_analysis_system_prompt() stamps today's date at call
# time, so this is a snapshot, and aria_cli holds its own taken when *it* was
# imported. The two are equal strings on the same day but never the same
# object, which is why the identity guard lists this name as a documented
# value-equality exception rather than skipping it. On the CLI path aria_cli's
# snapshot wins the rebind merge anyway; this one exists so the fallback path
# has one at all.
ANALYSIS_SYSTEM_PROMPT = _build_analysis_system_prompt()


def _try_inject_file_paths(_message: str) -> str:
    """Legacy hook retained for the extracted stream implementation.

    Context references are resolved by the typed ``@`` reference service before
    provider dispatch. Silently reading path-like text here would bypass that
    service's validation and duplicate context, so the legacy hook is now a
    deliberate no-op.
    """
    return ""


def _recent_sports_quant_context(history: list, max_chars: int = 5000) -> str:
    """Return the latest sports quant block from chat history for follow-ups."""
    markers = (
        "【泊松模型量化预测",
        "【量化预测",
        "最可能比分",
        "可能比分",
    )
    for msg in reversed(history or []):
        content = str(msg.get("content", "")) if isinstance(msg, dict) else ""
        if not content:
            continue
        if "预期进球" in content and any(marker in content for marker in markers):
            return content[-max_chars:]
    return ""


_TOOL_NAME_ALIASES = {
    "get_stock_data": "get_market_data",
    "get_stock_price": "get_market_data",
    "fetch_stock_data": "get_market_data",
    "query_stock_data": "get_market_data",
    "get_stock_history": "get_market_history",
    "get_historical_data": "get_market_history",
    "fetch_market_history": "get_market_history",
}


def _response_cache_eligible(message: str, history: list, enable_tools: bool) -> bool:
    """Return whether a turn can safely reuse a prose-only model response."""
    return not enable_tools and not history and len(message) < 300


def _routing_message_for_turn(message: str, history: list) -> str:
    """Keep provider routing anchored to the user's original request.

    Runtime tool rounds pass a generated ``## Tool Results`` prompt as the
    current message. Classifying that synthetic prompt can switch a finance
    turn into coding and expose unrelated file/shell tools.
    """
    current = str(message or "")
    if not current.lstrip().startswith("## Tool Results"):
        return current
    for item in reversed(history or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", "")
        if not isinstance(content, str):
            continue
        if content.strip() and not content.lstrip().startswith("## Tool Results"):
            return content
    return current


def _normalize_requested_tool_call(
    tool_name: str,
    params: dict,
    available_names: set[str],
) -> tuple[str, dict, str]:
    """Map common model aliases and parameter names to registered tools."""
    requested_name = str(tool_name or "").strip()
    aliased_name = _TOOL_NAME_ALIASES.get(requested_name, requested_name)
    resolved_name = aliased_name if aliased_name in available_names else requested_name
    normalized_params = dict(params or {})

    # Normalize symbol parameter across all market tools
    if resolved_name in {"get_market_data", "get_market_history", "calculate_factors", "get_risk_metrics", "get_options_chain"}:
        sym = normalized_params.get("symbol")
        if not sym:
            for key in ("symbols", "ticker", "tickers", "code", "stock", "asset"):
                if normalized_params.get(key):
                    sym = normalized_params.pop(key)
                    break
        if isinstance(sym, (list, tuple)):
            sym = sym[0] if sym else ""
        elif isinstance(sym, dict):
            sym = sym.get("symbol") or sym.get("ticker") or sym.get("code") or ""
        sym_str = str(sym or "").strip()
        if (sym_str.startswith("[") and sym_str.endswith("]")) or (sym_str.startswith("(") and sym_str.endswith(")")):
            sym_str = sym_str[1:-1].strip()
        normalized_params["symbol"] = sym_str.strip("'\"` ").upper()

    # Normalize query parameter for web_search
    if resolved_name in {"web_search", "web_fetch"}:
        q = (
            normalized_params.get("query")
            or normalized_params.get("q")
            or normalized_params.get("search")
            or normalized_params.get("keyword")
            or normalized_params.get("keywords")
            or normalized_params.get("text")
            or normalized_params.get("symbol")
            or ""
        )
        if isinstance(q, (list, tuple)):
            q = " ".join(str(x) for x in q if x)
        normalized_params["query"] = str(q).strip().strip("'\"`")

    return resolved_name, normalized_params, requested_name


def _finance_tool_schema_allowed(
    schema: dict,
    allowed_names: set[str],
    explicit_mcp: bool = False,
) -> bool:
    """Allow local finance tools and approved finance tools exposed over MCP."""
    name = str(schema.get("function", {}).get("name", ""))
    if name in allowed_names:
        return True
    if not name.startswith("mcp__"):
        return False
    short_name = name.rsplit("__", 1)[-1]
    return explicit_mcp or short_name in allowed_names


def _skill_tool_schema_allowed(schema: dict, allowed_names: set[str]) -> bool:
    """Match a Skill policy against local and namespaced MCP tool schemas.

    Core file I/O tools (read_file, write_file, ...) are always allowed
    regardless of the skill's policy — see CORE_FILE_TOOLS for why.
    """
    name = str(schema.get("function", {}).get("name", ""))
    if name in _CORE_FILE_TOOLS or name in allowed_names:
        return True
    if name.startswith("mcp__"):
        short_name = name.rsplit("__", 1)[-1]
        return short_name in _CORE_FILE_TOOLS or short_name in allowed_names
    return False


async def stream_ollama(ollama_url: str, message: str, history: list,
                        model: str = "qwen2.5:7b",
                        on_token=None, on_thinking=None,
                        on_tool_call=None, on_tool_result=None,
                        cancel_event: asyncio.Event = None,
                        enable_tools: bool = True,
                        tool_schemas: list = None,
                        defer_tool_execution: bool = False,
                        system_override: str = None,
                        show_market_prefetch_status: bool = True) -> dict:
    """Stream chat via local Ollama with tool calling support (native + text-based)."""
    import aiohttp

    _intent_message = _routing_message_for_turn(message, history)

    # ── Response cache: skip Ollama for repeated stateless queries ───────────
    # Only cache when there is no conversation history (stateless), the query
    # is short (likely a simple quote/concept), and no tools are being called.
    # A cached prose answer cannot stand in for a tool-capable turn: returning
    # it here bypasses schema injection and the runtime tool loop entirely.
    _should_cache = _response_cache_eligible(message, history, enable_tools)
    if _should_cache:
        _ck = _cache_key(model, message)
        _cached = _cache_get(_ck)
        if _cached:
            if on_token:
                on_token(_cached)
            return {"success": True, "response": _cached,
                    "provider": "ollama_cache", "usage": {}}

    _models_probe, _ollama_err = detect_ollama_models_rich(ollama_url)
    if _ollama_err:
        if _is_simple_greeting(_intent_message):
            return _offline_greeting_response()
        return _ollama_unavailable_result(ollama_url, _ollama_err)

    # ── 模型自动解析：确保请求的模型在 Ollama 中存在 ─────────────────────────
    try:
        from local_llm_provider import resolve_model_async
        _resolved = await resolve_model_async(ollama_url, model)
        if _resolved != model:
            model = _resolved   # silently remap to available model
    except Exception:
        pass  # resolution failed — proceed with original model name

    # ── 模型分级守卫：小模型不能处理 coding/analysis/complex-finance 任务 ──────
    # 如果分配到的模型是 small/nano 级别，但任务需要代码生成、复杂分析或长文本，
    # 自动升级到 Ollama 中最优可用模型，防止低质量/模板化输出。
    try:
        from model_capability import get_model_capability, is_router_only, can_handle_coding
        _cap_check = get_model_capability(model)
        _task_needs_upgrade = (
            is_router_only(_cap_check)
            or (not can_handle_coding(_cap_check) and _is_coding_request(_intent_message))
            # Small (1-4B) models also struggle with complex finance questions:
            # they ignore detailed system prompts and output template garbage.
            # Upgrade when the question is non-trivial and the model is "small".
            # Use 8 as the minimum length threshold (works for both Chinese and English):
            # Chinese "比特币值得投资吗" = 9 chars, English "buy or sell?" = 12 chars.
            or (_cap_check.size_class == "small" and len(_intent_message) > 8
                and not _is_simple_greeting(_intent_message))
        )
        if _task_needs_upgrade and _models_probe:
            # 按优先级寻找可用的升级模型
            # NOTE: gpt-oss 排在 deepseek-v3.1 前面，因为 deepseek-v3.1:671b-cloud
            # 在 Ollama 实例中有时超时，而 gpt-oss:120b-cloud 响应稳定。
            _upgrade_prefixes = [
                "aria-sonata-3b", "qwen2.5-coder:7b", "qwen2.5-coder:3b",
                "qwen2.5:7b", "qwen2.5:3b", "llama3.2:3b", "mistral",
                # Cloud models registered in this Ollama instance (remote but available)
                "gpt-oss", "deepseek-v3.1",
            ]
            # _models_probe is a list of dicts: {"name": str, "size_label": str, ...}
            # Must extract "name" field — do NOT call .startswith() on the dict.
            _probe_names = [
                m["name"] if isinstance(m, dict) else m
                for m in _models_probe
            ]
            for _pref in _upgrade_prefixes:
                _candidate = next(
                    (m for m in _probe_names if m.startswith(_pref)), None
                )
                if _candidate and _candidate != model:
                    model = _candidate
                    break
    except Exception:
        pass

    # ── 五档路由：通过 Prelude 意图分类器（或关键词 fallback）决定 prompt ────
    # Always rebuild finance prompt to get today's date
    _finance_prompt = _build_finance_prompt(_intent_message)

    try:
        from intent_classifier import (
            classify_intent_async,
            INTENT_CODING, INTENT_ANALYSIS, INTENT_REALTIME,
            INTENT_GENERAL, INTENT_FINANCE,
        )
        _intent = await classify_intent_async(_intent_message, ollama_url)
    except Exception:
        # Fallback to legacy keyword detection if intent_classifier unavailable
        if _is_coding_request(_intent_message):
            _intent = "coding"
        elif _is_analysis_request(_intent_message):
            _intent = "analysis"
        elif _is_general_knowledge(_intent_message):
            _intent = "general"
        else:
            _intent = "finance"

    # The lightweight classifier can label stock-analysis prompts as generic
    # finance. Preserve the stronger deterministic signal so tool-capable
    # models receive the prompt that requires market data before analysis.
    if _is_analysis_request(_intent_message) and _intent in {"general", "finance"}:
        _intent = "analysis"

    _is_general = (_intent == "general")
    try:
        from apps.cli.intent_router import build_intent_route
        _route = build_intent_route(_intent_message)
    except Exception:
        _route = None

    # Resolve portable workflow metadata before tool schemas are selected. This
    # lets a verified Skill narrow the tools exposed to the model, rather than
    # treating its declared policy as prompt-only guidance.
    _is_tool_followup = message.lstrip().startswith("## Tool Results")
    _skill_activation = None
    try:
        from packages.aria_skills import activate_external_skills as _activate_external_skills

        _skill_activation = _activate_external_skills(
            _intent_message,
            record=not _is_tool_followup,
        )
    except Exception as _skill_exc:
        logger.debug("skill activation failed: %s", _skill_exc)

    # ── Context-aware tool schema filtering ───────────────────────────────────
    # Intent drives tool exposure, but cross-intent requests (e.g. "分析AAPL然后
    # 写一个回测策略") need BOTH market data tools AND coding tools.
    # Detect overlap by checking if the message contains signals from both domains.
    _CU_TOOL_NAMES = {"browser_navigate", "browser_screenshot",
                      "computer_screenshot", "computer_action"}
    _CODE_TOOL_NAMES = {"read_file", "write_file", "edit_file", "list_files",
                        "search_code", "run_command", "process", "github",
                        "glob", "notebook_read", "notebook_edit"}
    _FINANCE_TOOL_NAMES = {
        "get_market_data", "get_market_history", "calculate_factors",
        "get_risk_metrics", "analyze_news",
        "get_market_insights", "get_predictions", "peer_comparison",
        "web_search", "web_fetch",
        "research_report_assess",
        "research_run_create", "research_run_get", "research_run_transition",
        "research_run_attach_artifact", "research_run_record_quality",
        "research_run_events",
    }

    _available_tool_schemas = list(
        LOCAL_TOOL_SCHEMAS if tool_schemas is None else tool_schemas
    )
    _msg_low = _intent_message.lower()
    _explicit_tool_signal = any(k in _msg_low for k in (
        "调用工具", "使用工具", "用工具", "调用 agent", "使用 agent",
        "调用智能体", "使用智能体", "mcp", "tool call", "use tool",
        "use agent", "spawn agent",
    ))
    _explicit_agent_signal = any(k in _msg_low for k in (
        "agent", "智能体", "多代理", "多智能体", "研究团队", "subagent",
    ))
    _explicit_mcp_signal = "mcp" in _msg_low
    _finance_tool_names = set(_FINANCE_TOOL_NAMES)
    if any(k in _msg_low for k in ("加密", "比特币", "以太坊", "crypto", "btc", "eth")):
        _finance_tool_names.add("get_crypto_data")
    if any(k in _msg_low for k in ("外汇", "汇率", "forex", "fx", "usd/", "eur/")):
        _finance_tool_names.add("get_forex_data")
    if any(k in _msg_low for k in ("持仓", "账户", "下单", "broker", "portfolio", "position")):
        _finance_tool_names.update({"broker_query", "broker_order", "get_risk_metrics"})
    if any(k in _msg_low for k in ("期权", "option", "隐含波动率")):
        _finance_tool_names.add("get_options_chain")
    if _explicit_agent_signal:
        _finance_tool_names.update({
            "spawn_task", "task_status", "task_result", "task_list",
            "task_cancel", "update_todos",
        })
    _explicit_code_signal = bool(getattr(_route, "explicit_code", False)) if _route else any(k in _msg_low for k in (
        "代码", "脚本", "python", "程序", "实现", "开发", "修改文件",
        "写代码", "编写代码", "策略代码", "保存为.py", ".py",
        "script", "code", "program", "implement", "edit file", "write file",
    ))
    _is_visual_artifact_request = bool(getattr(_route, "visual_artifact", False)) if _route else any(k in _msg_low for k in (
        "图表", "走势图", "k线图", "k线", "chart", "plot", "dashboard", "看板", "report", "报告",
    ))
    _has_coding_signal = _explicit_code_signal or (
        not _is_visual_artifact_request
        and any(k in _msg_low for k in ("写", "回测", "backtest", "save", "file"))
    )
    _has_finance_signal = any(k in _msg_low for k in (
        "分析", "股票", "行情", "股价", "市场", "quantitative", "stock",
        "price", "market", "analyze", "analysis", "ticker",
    ))
    _is_cross_intent = _has_coding_signal and _has_finance_signal

    if _is_visual_artifact_request and not _explicit_code_signal:
        _excluded = _CU_TOOL_NAMES | _CODE_TOOL_NAMES
        _schemas_for_context = [
            s for s in _available_tool_schemas
            if s.get("function", {}).get("name") not in _excluded
        ]
    elif _intent in ("finance", "analysis", "realtime") and not _is_cross_intent:
        # Keep finance turns focused. MCP tools remain available because they
        # may provide institution-specific data, while file/code tools are
        # excluded so the model does not search the repository for a quote API.
        _schemas_for_context = [
            s for s in _available_tool_schemas
            if _finance_tool_schema_allowed(
                s,
                _finance_tool_names,
                explicit_mcp=_explicit_mcp_signal,
            )
        ]
    elif _is_cross_intent:
        # Cross-intent: expose both finance AND coding tools (minus CU/browser).
        # Intent hint injected into system prompt guides priority without hard exclusion.
        _schemas_for_context = [
            s for s in _available_tool_schemas
            if s.get("function", {}).get("name") not in _CU_TOOL_NAMES
        ]
    else:
        # Coding/general: all local tools except CU (browser/computer control)
        _schemas_for_context = [
            s for s in _available_tool_schemas
            if s.get("function", {}).get("name") not in _CU_TOOL_NAMES
        ]

    _skill_allowed_tools = {
        tool_name
        for skill in (_skill_activation.skills if _skill_activation else ())
        for tool_name in skill.policy.allowed_tools
    }
    if _skill_allowed_tools:
        _schemas_for_context = [
            schema for schema in _schemas_for_context
            if _skill_tool_schema_allowed(schema, _skill_allowed_tools)
        ]

    # ── Select prompt size based on model capability ─────────────────────────
    # Small / nano models (≤3B) cannot effectively use the full CODING_SYSTEM_PROMPT
    # (6000+ tokens of examples they mostly ignore).  Send a condensed version that
    # keeps the essential rules and the single complete working template.
    #
    # Analysis uses the full tool-aware prompt for capable models and the lite
    # prefetched-data prompt for small/text-only models.
    try:
        from model_capability import get_model_capability as _gmc
        _prompt_cap = _gmc(model)
        _model_size = _prompt_cap.size_class
        _model_supports_native_tools = bool(
            _prompt_cap.tool_calls and _prompt_cap.format == "ollama_native"
        )
    except Exception:
        _model_size = "medium"
        _model_supports_native_tools = False
    _use_lite_prompt = _model_size in ("nano", "small")

    if _intent == "coding":
        _base_prompt = _build_coding_prompt_lite(_intent_message) if _use_lite_prompt else CODING_SYSTEM_PROMPT
    elif _intent == "analysis":
        # Native-tool models must receive the prompt that explicitly requires
        # get_market_data. The lite prompt is the fallback for text-only/small
        # models and tells them to rely on deterministic prefetched data.
        _base_prompt = (
            ANALYSIS_SYSTEM_PROMPT
            if _model_supports_native_tools and not _use_lite_prompt
            else _build_analysis_prompt_lite(_intent_message)
        )
    elif _intent == "general":
        # 纯知识/概念问题：注入日期，但不注入工具 schema
        from datetime import datetime as _dt2
        _today = _dt2.now()
        _today_str = f"{_today.year:04d}年{_today.month:02d}月{_today.day:02d}日"
        _base_prompt = (
            f"你是 Aria，Arthera 的 AI 助手。今天是 {_today_str}。\n"
            "你的能力覆盖：金融量化分析、足球/体育赛事分析与预测（含泊松算法）、编程、通用知识问答。\n\n"
            "## 体育量化分析规则（重要）\n"
            "用户消息中可能包含两种特殊数据块：\n\n"
            "### 【比赛信息】块\n"
            "= football-data.org API 获取的真实赛程（比赛时间、状态、比分）。这是**事实**，不要质疑。\n\n"
            "### 【泊松模型量化预测】块\n"
            "= Aria 用 Dixon-Coles 泊松算法计算的量化结果，包含：\n"
            "  - 两队 FIFA 排名强度参数（进攻/防守）\n"
            "  - 预期进球数（λ值）\n"
            "  - 主胜/平局/客胜概率（%）\n"
            "  - 最可能比分及其概率\n"
            "  - 隐含赔率\n"
            "当收到此块时，你应当：\n"
            "1. **直接引用数字**（「算法显示加拿大胜率 42.4%」），不要重新计算或质疑\n"
            "2. **解释概率背后的逻辑**：FIFA 排名差距、进攻强度对比说明了什么\n"
            "3. **分析高频比分区间**：比如 1-0/1-1/2-1 集中说明比赛预计紧张胶着\n"
            "4. **补充战术/球员层面的定性分析**（这是你的训练知识，算法没有的部分）\n"
            "5. **绝对不要**说「我没有实时数据」「世界杯尚未开始」「不包含实时数据」「以上预测基于历史」——这些与上方数据矛盾\n"
            "6. **绝对不要**在末尾添加「若需最新数据请使用/football命令」之类的建议——用户已经有数据了\n\n"
            "## 通用规则\n"
            "- 使用 Markdown（**粗体**、## 标题、- 列表、表格）\n"
            "- 不要编造股价/汇率等金融数字\n"
            "- 简洁精准，用数据说话\n"
        )
    else:
        # realtime / finance: use full finance prompt with tool access
        _base_prompt = _finance_prompt

    # Project context injection: skip or condense for small/nano models.
    # A 1.5B model with a 4000-token README injected into its context will
    # either copy the README into its response or hallucinate beyond recovery.
    _small_model = _model_size in ("nano", "small")
    if not _is_general:
        if not _small_model and _PROJECT_CONTEXT:
            system_prompt = _base_prompt + _PROJECT_CONTEXT
        else:
            # For small models: skip the full README, only keep a 2-line summary
            _ctx_brief = ""
            if _PROJECT_CONTEXT:
                _first_lines = [l for l in _PROJECT_CONTEXT.split("\n") if l.strip()][:3]
                _ctx_brief = "\n# Context: " + " | ".join(_first_lines[:2]) + "\n"
            system_prompt = _base_prompt + _ctx_brief
    else:
        system_prompt = _base_prompt

    # Inject cross-intent hint so the model knows to use both tool sets in order
    if _is_cross_intent:
        _cross_hint = (
            "\n\n## Task Hint\n"
            "This request spans both **market analysis** and **code generation**. "
            "Suggested order: (1) fetch market data first, (2) use the data to write/run code. "
            "Use get_market_data for live prices, then write_file + run_command for scripts.\n"
        )
        system_prompt = system_prompt + _cross_hint

    # Prepend global user memory (user profile, project history, preferences)
    try:
        from memory_manager import MemoryManager as _MM
        _mem_block = _MM().load_context(max_chars=500)
        if _mem_block:
            system_prompt = _mem_block + "\n" + system_prompt
    except Exception:
        pass

    # Append ariarc project context if available — small models skip this
    if _HAS_ARIARC and not _is_general and not _small_model:
        try:
            _arc = get_ariarc()
            _arc_block = _arc.build_system_prompt_block()
            if _arc_block:
                # Hard-cap ariarc block at 800 chars to prevent context overflow
                _arc_short = _arc_block[:800] + ("…" if len(_arc_block) > 800 else "")
                system_prompt = system_prompt + "\n\n" + _arc_short
        except Exception:
            pass

    # Inject live broker context when user is asking about their own portfolio,
    # or when a broker is connected and the message is finance-related.
    if _is_broker_intent(_intent_message):
        _broker_ctx = _build_broker_context_block()
        if _broker_ctx:
            system_prompt = system_prompt + "\n\n" + _broker_ctx

    # Allow /file analyze and other commands to inject a specialist role override
    if system_override:
        system_prompt = system_override + "\n\n" + system_prompt

    url = f"{ollama_url}/api/chat"
    _mcfg = get_model_cfg(model)

    if _HAS_MODEL_CAP:
        _cap         = get_model_capability(model)
        _num_ctx     = _cap.context_window
        _temperature = _cap.temperature
    else:
        _num_ctx     = _mcfg.get("num_ctx", 16384)
        _temperature = _mcfg.get("temperature", 0.3)
    _max_tokens = _mcfg.get("max_tokens", min(_mcfg.get("num_ctx", 8192) // 4, 8192))
    _mkey = resolve_model_key(model)

    # ── 上下文硬截断：保留 80% 上下文给历史，防止溢出 ────────────────────
    # 用 1.5 chars/token（CJK 混合文本实际比率）而非英文假设的 4 chars/token
    _chars_per_tok = 1.5
    _ctx_chars_for_hist = int(_num_ctx * 0.80 * _chars_per_tok) - len(system_prompt) - len(message) - 512
    _ctx_chars_limit = max(_ctx_chars_for_hist, 1000)
    # 从最新历史往前选，确保总字符数不超限
    _trimmed_history: list = []
    _hist_chars = 0
    for _hm in reversed(history):
        _hm_len = len(_hm.get("content",""))
        if _hist_chars + _hm_len > _ctx_chars_limit:
            break
        _trimmed_history.insert(0, _hm)
        _hist_chars += _hm_len

    messages = [{"role": "system", "content": system_prompt}]
    for msg in _trimmed_history:
        prepared_msg = {
            "role": msg["role"],
            "content": msg.get("content", ""),
        }
        for key in ("tool_calls", "tool_call_id", "name"):
            if msg.get(key) is not None:
                prepared_msg[key] = msg[key]
        messages.append(prepared_msg)
    # Runtime adapters pass history and the current prompt separately. Legacy
    # callers may already have appended that exact prompt, so deduplicate by
    # content rather than by role; a tool-result message is also role=user and
    # must not cause the next agent instruction to be dropped.
    _last_is_current = bool(
        _trimmed_history
        and _trimmed_history[-1].get("role") == "user"
        and _trimmed_history[-1].get("content") == message
    )
    if not _last_is_current:
        messages.append({"role": "user", "content": message})

    # ── 工具注入：通识问答跳过，同时跳过无法可靠调用工具的小模型 ──────────
    # 判断模型是否具备工具调用能力（text_only / format不支持的都跳过）
    _model_can_use_tools = False
    if (
        _HAS_MODEL_CAP
        and enable_tools
        and _available_tool_schemas
        and (not _is_general or _explicit_tool_signal)
    ):
        _tool_cap = get_model_capability(model)
        # 只有明确支持工具且 context_window >= 8192 的模型才注入 tool schema
        _model_can_use_tools = (
            _tool_cap.format != "text_only"
            and _tool_cap.context_window >= 8192
        )
        if _model_can_use_tools:
            _tool_sys = build_tool_system_prompt(_schemas_for_context, model)
            if _tool_sys and messages:
                if messages[0].get("role") == "system":
                    messages[0]["content"] += _tool_sys
                else:
                    messages.insert(0, {"role": "system", "content": _tool_sys.strip()})

    # ── 实时数据预取：始终为分析/报价查询预取真实市场数据注入 prompt ──────────
    # 无论模型是否支持工具调用，都注入真实数据，防止模型生成占位符（$X.XX）
    # 策略：
    #   1. system prompt 替换为"数据已预取"专用 prompt
    #   2. 数据同时注入到用户消息开头（本地模型对最近的 user message 最敏感）
    _skip_market_prefetch = (
        _is_general
        or _is_visual_artifact_request
        or _model_can_use_tools
    )
    if _HAS_MDC and not _skip_market_prefetch:
        import time as _t_inj
        _t_inj_start = _t_inj.time()
        _market_inject = _try_prefetch_market_data(_intent_message, history)
        _t_inj_ms = int((_t_inj.time() - _t_inj_start) * 1000)
        if _market_inject:
            # 过程可见化：⏺/✓ 格式，与工具调用步骤保持一致
            import re as _re_inj
            _inj_m = _re_inj.search(r'## 📊 (\S+) 实时行情（来源：(\S+)）', _market_inject)
            if HAS_RICH and show_market_prefetch_status:
                _sym_label = _inj_m.group(1) if _inj_m else "market_data"
                _src_label = _inj_m.group(2) if _inj_m else "local"
                console.print(
                    f"\n  [#C08050]⏺[/#C08050]  [bold]market_data[/bold]"
                    f"  [dim]{_sym_label} · {_src_label}[/dim]"
                )
                console.print(
                    f"  [green]✓[/green]  [dim]实时行情已注入[/dim]"
                    f"  [dim]({_t_inj_ms}ms)[/dim]"
                )
            # Replace system prompt with data-first prompt.
            # Use nano variant for 1-3B models (no template placeholders).
            _is_nano_model = _use_lite_prompt or _model_size in ("nano", "small")
            _prefetched_sys = _build_prefetched_analysis_prompt(nano=_is_nano_model, user_message=_intent_message)
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = _prefetched_sys
            else:
                messages.insert(0, {"role": "system", "content": _prefetched_sys})
            # Prepend real data to the user message so the model sees it last
            # (most recent = highest attention weight for local models).
            _augmented_user = (
                _market_inject
                + "\n---\n"
                "上面是真实实时数据。请只使用这些具体数字作答，不要引用训练记忆中的历史价格。\n\n"
                + message
            )
            for _mi in reversed(messages):
                if _mi.get("role") == "user":
                    _mi["content"] = _augmented_user
                    break

    # Portable SKILL.md workflows are loaded after prompt/prefetch selection so
    # they survive the data-first prompt replacement used by local models.
    try:
        _skill_block = _skill_activation.prompt_block
        if _skill_block and messages:
            if messages[0].get("role") == "system":
                messages[0]["content"] += "\n\n" + _skill_block
            else:
                messages.insert(0, {"role": "system", "content": _skill_block})
            if not _is_tool_followup:
                for _active_skill in _skill_activation.skills:
                    if HAS_RICH:
                        console.print(
                            f"  [#C08050]⏺[/#C08050]  [bold]skill[/bold]  "
                            f"[dim]{_active_skill.qualified_name} · "
                            f"{_active_skill.integrity}[/dim]"
                        )
                    else:
                        print(
                            f"  skill  {_active_skill.qualified_name} · "
                            f"{_active_skill.integrity}"
                        )
    except (AttributeError, TypeError) as _skill_exc:
        logger.debug("skill prompt injection failed: %s", _skill_exc)

    # ── 体育赛事数据预取：sports query → inject live scores / WC data + Poisson ─
    if _is_general and _is_sports_query(_intent_message):
        _sports_ctx = _try_prefetch_sports_data(_intent_message)
        if not _sports_ctx:
            _sports_ctx = _recent_sports_quant_context(history)
        if _sports_ctx:
            _has_quant = (
                "泊松模型量化预测" in _sports_ctx
                or ("预期进球" in _sports_ctx and "可能比分" in _sports_ctx)
            )
            if HAS_RICH:
                _label = "[bold #50A0C0]sports_data+quant[/bold #50A0C0]" if _has_quant else "[bold #50A0C0]sports_data[/bold #50A0C0]"
                console.print(f"  {_label} [dim]赛事数据{'+ 泊松预测 ' if _has_quant else ''}已注入[/dim]")
                # Print Poisson block directly so user sees it even if LLM ignores context
                if _has_quant and not _ARIA_BOT_MODE:
                    console.print(Panel(
                        _sports_ctx,
                        title="[bold]⚽ 量化预测数据[/bold]",
                        border_style="cyan",
                        padding=(0, 1),
                    ))
            for _mi in reversed(messages):
                if _mi.get("role") == "user":
                    if _has_quant:
                        _injection_note = (
                            "\n---\n"
                            "## 数据说明\n"
                            "以上数据来自 football-data.org 实时 API + 泊松量化模型（Aria 本地计算）。\n"
                            f"【比赛信息】块 = API 真实赛程数据，获取日期为 {datetime.now().strftime('%Y-%m-%d')}。\n"
                            "【泊松模型量化预测】块 = Aria 使用 Dixon-Coles 泊松分布对此场比赛运行的算法结果。\n\n"
                            "## 你的任务\n"
                            "1. **直接引用预测数据中的概率数字**（如「加拿大胜率 42.4%」），不要说你没有数据\n"
                            "2. 解释为什么会有这样的概率分布（结合两队 FIFA 排名、进攻/防守强度）\n"
                            "3. 如果用户要“一个以上/多个/最准比分”，必须按 `top_scorelines` 概率降序列出候选比分和概率\n"
                            "4. 分析最可能的比分区间（高频比分说明比赛预计紧张/单方碾压）\n"
                            "5. 可以给出走势判断，但不要编造射正率、最近5场客场数据、历史交锋次数等输入数据之外的具体事实\n"
                            "6. 注意区分胜平负概率和准确比分概率：热门球队胜率最高，不代表最可能的单一比分一定是该队获胜\n"
                            "7. 不要否认上方已提供的赛程或模型数据；如需说明限制，具体指出缺失字段\n"
                            "8. **严禁**在回复末尾添加任何类似以下内容的免责声明：\n"
                            "   - 「以上预测基于历史...并不包含实时数据」\n"
                            "   - 「不包含实时数据或赛前最新信息」\n"
                            "   - 「若需赛前最新数据，请使用 /football 命令」\n"
                            "   上方已有实时 API 数据 + 量化模型，这些免责声明与数据矛盾，请勿添加。\n\n"
                        )
                    else:
                        _injection_note = (
                            "\n---\n"
                            "以上是从 football-data.org 获取的赛事数据。\n"
                            "请基于这些数据给出分析，若数据不完整可结合训练知识合理推断。\n\n"
                        )
                    _mi["content"] = _sports_ctx + _injection_note + message
                    break

    # ── 文件路径自动注入：若用户消息引用了本地文件，预读并注入内容 ────────────
    # 无论意图是什么，只要消息里有可读的文件路径就注入（coding / analysis 均有效）
    _file_inject = _try_inject_file_paths(_intent_message)
    if _file_inject:
        for _mi in reversed(messages):
            if _mi.get("role") == "user":
                _mi["content"] = _file_inject + _mi["content"]
                break

    # ── Token budget 分级策略 ────────────────────────────────────────────────
    # 小模型（<8K ctx）防止无限延伸；通识问答分两档：
    #   · 纯问候/一句话问题 → 200 tokens（快速）
    #   · 知识解释问题（"什么是X", "如何…"） → 1500 tokens（保证完整性）
    #   · 正常问题 → 模型 max_tokens 配置值
    _is_small_model = _HAS_MODEL_CAP and get_model_capability(model).context_window < 8192
    _is_greeting    = _is_simple_greeting(_intent_message)
    _wants_complete_output = any(k in _intent_message.lower() for k in (
        "完整", "完整输出", "完整给出", "全面", "详细", "不要中断", "不要截断",
        "complete", "full output", "comprehensive", "do not stop", "don't stop",
        "do not truncate", "end-to-end",
    ))

    if _is_greeting:
        # Reasoning-capable Ollama Cloud models can spend more than 200 tokens
        # in ``message.thinking`` before emitting the short visible greeting.
        _effective_max_tokens = 512 if (
            _HAS_MODEL_CAP and get_model_capability(model).thinking
        ) else 200
    elif _wants_complete_output:
        _complete_cap = max(2048, min(8192, int(_num_ctx * 0.45)))
        _effective_max_tokens = max(_max_tokens, _complete_cap)
    elif _is_general:
        _effective_max_tokens = max(1500, min(_max_tokens, 4096))   # 足够完整回答概念解释，不截断
    elif _use_lite_prompt:
        # Small/nano model: coding tasks need more room for complete scripts;
        # analysis/finance keep a tighter cap to prevent runaway echo generation.
        if _intent == "coding":
            _effective_max_tokens = 2000
        else:
            _effective_max_tokens = 512
    elif _is_small_model:
        _effective_max_tokens = min(_max_tokens, 2048)
    else:
        _effective_max_tokens = _max_tokens

    # 停止词：覆盖常见 hallucination 模式
    # 包含：英文求助模板、工具执行幻觉、"任务就绪"尾部幻觉（中文小模型常见）
    _stop_seqs = [
        # ── 英文求助/拒绝模板 ─────────────────────────────────────────────
        "I'm sorry, as an AI",
        "I'm sorry for any confusion",
        "I cannot perform",
        "I can't perform",
        "Do You Need Help",
        "Are There Specific Areas",
        "Let us brainstorm together",
        "AWAITING FEEDBACK",
        "Would love more context",
        "Please provide more details",
        "Could you please provide",
        "Without knowing those specifics",
        "os.system('pip install",
        "git clone https://github.com",
        "Let's download these libraries",
        # ── 中文"任务就绪"尾部幻觉（小模型在回答结束后常产生） ────────────
        "好的，我将开始执行任务",
        "好的，我已经准备好了要做的工作",
        "请告诉我您希望我在接下来做什么",
        "请问有什么我可以帮助您的吗",
        "请告诉我你需要什么帮助",
        "我会尽快为您完成这项任务",
        "如果您有任何其他问题，请随时告诉我",
        "如果你有其他问题，请随时提问",
        # ── 英文任务就绪幻觉 ─────────────────────────────────────────────
        "I'm ready to help with your next",
        "Let me know if you need anything else",
        "Is there anything else you'd like me to",
        "Feel free to ask if you have more questions",
        # ── 工具调用幻觉（声称已调用但实际没有 tool_call 事件）────────────
        "I have already called `get_market_data`",
        "I have already called `get_stock_price`",
        "I have already called get_market_data",
        "I have already fetched",
        "I have already retrieved",
        "我已经调用了",
        "我已调用工具",
        "我已经获取了最新数据",
        # ── 模板占位符输出（模型把 system prompt 模板当内容输出）────────────
        "${real_price_from_data",
        "${data['day_range']}",
        "{actual date today}",
        "{real price from data}",
        "List real recent headlines",
    ]

    payload = {
        "model": model, "messages": messages, "stream": True,
        "options": {
            "num_ctx":        _num_ctx,
            "temperature":    _temperature,
            "top_p":          0.9,
            "repeat_penalty": 1.4,
            "repeat_last_n":  256,
            "num_predict":    _effective_max_tokens,
        },
        "stop": _stop_seqs,
    }
    # Only inject native Ollama tools field for capable models.
    # Small (≤4K ctx) or text_only models must never receive tool schemas —
    # they produce malformed partial JSON that leaks into the output stream.
    if _model_can_use_tools:
        _cap2 = get_model_capability(model) if _HAS_MODEL_CAP else None
        if _cap2 and _cap2.tool_calls and _cap2.format == "ollama_native":
            payload["tools"] = _schemas_for_context

    full_response = ""
    response_segments = []
    tools_used = []
    tool_calls_pending = []
    _tool_call_counts = {}
    _tool_name_counts = {}
    _available_tool_names = {
        s.get("function", {}).get("name", "")
        for s in _schemas_for_context
        if s.get("function", {}).get("name")
    }
    max_tool_rounds = 25 if _wants_complete_output or _intent == "coding" else 12
    _server_retry_budget = 2
    _continuation_count = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0}
    _last_tool_had_error = False  # Track if previous tool failed
    _in_error_recovery = False    # Stays True until run_command succeeds (not reset by read_file)
    _nudge_count = 0  # Limit error recovery nudges
    _consecutive_reads = 0  # Track repeated read_file without fixing
    _last_failed_cmd = ""  # Track last failed run_command to detect repeats
    _consecutive_cmd_failures = 0  # Count consecutive failures of same command
    # Repetition loop detection — check every 80 chars (was 200, too slow for 200-token responses)
    _rep_check_interval = 80
    _rep_token_count = [0]     # mutable for closure
    _rep_cancelled = [False]   # signals loop to stop

    def _tool_signature(tool_name: str, params: dict) -> tuple[str, str]:
        try:
            payload = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = str(params or {})
        return tool_name, payload

    def _register_tool_call(tool_name: str, params: dict) -> dict:
        tool_name, normalized_params, requested_name = _normalize_requested_tool_call(
            tool_name,
            params,
            _available_tool_names,
        )
        call = {"tool": tool_name, "params": normalized_params}
        if requested_name != tool_name:
            call["_aria_requested_tool"] = requested_name
        sig = _tool_signature(tool_name, params)
        seen = _tool_call_counts.get(sig, 0)
        _tool_call_counts[sig] = seen + 1
        _tool_name_counts[tool_name] = _tool_name_counts.get(tool_name, 0) + 1
        # Market data calls are expensive and usually deterministic for a turn.
        # One successful/attempted call is enough; repeated identical calls are
        # almost always model looping.
        limit = 1 if tool_name in {
            "get_market_data", "get_crypto_data", "get_forex_data",
            "get_technical_indicators",
        } else 2
        total_limit = {
            "web_search": 3,
            "web_fetch": 4,
            "search_news": 3,
        }.get(tool_name)
        if seen >= limit:
            call["_aria_duplicate"] = True
            call["_aria_limit_reason"] = "duplicate parameters"
        elif total_limit is not None and _tool_name_counts[tool_name] > total_limit:
            call["_aria_duplicate"] = True
            call["_aria_limit_reason"] = f"turn budget exceeded ({total_limit})"
        return call

    def _check_repetition(text: str) -> bool:
        """Return True if the response is looping.

        Covers three patterns:
          A. Paragraph-level loop: same long block (50-400 chars) reappears
          B. Sentence-level tail loop: short sentence (15-50 chars) appears
             2+ times at the END — catches "好的，我已经准备好了" × 2 style tails
          C. Beginning-restart loop: model generates the full response, then
             starts again from the very beginning. Detects when the opening
             80 chars of the accumulated response reappear after the midpoint.
             This is the most common 1.5B model failure mode.
        """
        if len(text) < 100:
            return False

        # Pattern C: restart-from-beginning (fast path, checked first)
        if len(text) > 300:
            _opening = text[:80].strip()
            if _opening and len(_opening) >= 20:
                _after_half = text[len(text) // 2:]
                if _opening in _after_half:
                    return True

        tail = text[-4000:]

        # Pattern A: medium-to-large probe in trailing window
        for sub_len in (400, 250, 150, 80, 50):
            if len(tail) < sub_len * 2:
                continue
            probe = tail[-sub_len:].strip()
            if len(probe) < 20:
                continue
            if tail[:-sub_len].count(probe) >= 1:
                return True

        # Pattern B: short sentence repetition at tail (boilerplate hallucination)
        # Split by Chinese sentence-ending punctuation + newlines
        import re as _re2
        # Common non-looping phrases that legitimately repeat (disclaimers, transitions)
        _B_IGNORE = {
            "本内容不构成投资建议", "不构成投资建议", "请注意风险",
            "以上仅供参考", "仅供参考", "请以官方数据为准",
            "如有问题请咨询专业人士", "投资有风险，入市需谨慎",
            "好的", "当然", "当然可以", "明白了", "好的，我来",
        }
        sentences = [s.strip() for s in _re2.split(r'[。！？\n]+', tail) if s.strip()]
        if len(sentences) >= 4:
            # Check if any sentence in the last 3 also appears before it in the tail
            for sent in sentences[-3:]:
                if len(sent) < 15:          # raised from 10 → less hair-trigger
                    continue
                if sent in _B_IGNORE:
                    continue
                # Require 3+ occurrences (raised from 2) to reduce false positives
                if tail.count(sent) >= 3:
                    return True

        return False

    for tool_round in range(max_tool_rounds):
        # Context compaction: compress older messages if context too large
        if tool_round > 0:
            payload["messages"] = _compact_messages(payload["messages"], model_key=_mkey)

        full_response = ""
        tool_calls_this_round = []
        _done_reason = ""
        # Do not show provisional finance prose before this round is known to
        # be prose-only. Some local models append malformed function JSON after
        # a sentence, and streaming it first makes the protocol visible.
        _buffer_finance_prose = enable_tools and _intent in {"finance", "analysis", "realtime"}
        _buffered_tokens: list[str] = []

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(url, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        try:
                            _body = await resp.text()
                            _json = json.loads(_body) if _body.strip().startswith("{") else {}
                            _ollama_err = _json.get("error") or _body[:200]
                        except Exception:
                            _ollama_err = f"HTTP {resp.status}"
                        # Invalidate model cache so next call re-probes
                        try:
                            from local_llm_provider import _model_cache
                            _model_cache.clear()
                        except Exception:
                            pass
                        if resp.status >= 500 and _server_retry_budget > 0:
                            _server_retry_budget -= 1
                            if HAS_RICH:
                                console.print("  [yellow]模型服务暂时错误，已压缩上下文并重试…[/yellow]")
                            payload["messages"] = _compact_messages(payload["messages"], model_key=_mkey)
                            payload["messages"].append({
                                "role": "user",
                                "content": (
                                    "SYSTEM: The model server returned a transient 5xx error. "
                                    "Continue the current task from the available tool results. "
                                    "Do not restart or repeat completed work."
                                ),
                            })
                            await asyncio.sleep(1.0)
                            continue
                        return {"success": False, "error": f"Ollama {resp.status}: {_ollama_err}"}
                    async for line in resp.content:
                        if cancel_event and cancel_event.is_set():
                            return {"success": True, "response": full_response,
                                    "cancelled": True, "provider": "ollama", "usage": usage}
                        text = line.decode("utf-8", errors="ignore").strip()
                        if not text:
                            continue
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            continue

                        # Check for native tool calls from Ollama
                        msg = data.get("message", {})
                        reasoning = msg.get("thinking", "")
                        if reasoning:
                            if on_thinking:
                                on_thinking(reasoning)
                        if msg.get("tool_calls"):
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                tool_name = fn.get("name", "")
                                tool_args = fn.get("arguments", {})
                                if isinstance(tool_args, str):
                                    try:
                                        tool_args = json.loads(tool_args)
                                    except json.JSONDecodeError:
                                        tool_args = {}
                                call = _register_tool_call(tool_name, tool_args)
                                tool_calls_this_round.append(call)
                                if not call.get("_aria_duplicate"):
                                    tools_used.append(tool_name)
                                if on_tool_call and not call.get("_aria_duplicate"):
                                    on_tool_call(tool_name, tool_args)

                        if data.get("done"):
                            # Capture Ollama usage stats from final message
                            usage["prompt_tokens"] += data.get("prompt_eval_count", 0)
                            usage["completion_tokens"] += data.get("eval_count", 0)
                            _done_reason = str(data.get("done_reason") or data.get("stop_reason") or "")
                            break

                        token = msg.get("content", "")
                        if token:
                            full_response += token
                            # 重复检测：先检测再流出，避免重复内容流到用户终端
                            _rep_token_count[0] += len(token)
                            if _rep_token_count[0] >= _rep_check_interval:
                                _rep_token_count[0] = 0
                                if _check_repetition(full_response):
                                    # 定位重复起始点：找到最长不重复前缀
                                    _fr = full_response
                                    _cut = len(_fr) // 2
                                    # 尝试精确裁切：找重复开始的位置
                                    for _probe_len in (300, 200, 150, 100):
                                        if len(_fr) < _probe_len * 2:
                                            continue
                                        _probe = _fr[-_probe_len:]
                                        _pos = _fr[:-_probe_len].find(_probe)
                                        if _pos > 0:
                                            _cut = _pos
                                            break
                                    full_response = _fr[:_cut].rstrip()
                                    _rep_cancelled[0] = True
                                    if cancel_event:
                                        cancel_event.set()
                                    break
                            # 流出 token — 过滤条件：
                            # 1. 以 <tool_call 开头的 XML 工具调用（内部处理，不显示）
                            # 2. 以 { 开头的裸 JSON 工具调用（小模型幻觉，直接屏蔽）
                            _fr_lstrip = full_response.lstrip()
                            _looks_like_tool_json = (
                                _fr_lstrip.startswith("{")
                                and ('"name"' in full_response or '"function"' in full_response)
                                and '"arguments"' in full_response
                            )
                            # 3. 孤立的 ``` 围栏（未配对的代码块标记，过滤掉）
                            _stripped_tok = token.strip()
                            _is_orphan_fence = (
                                _stripped_tok.startswith("```")
                                and len(_stripped_tok) <= 6   # just ``` or ```py etc
                                and full_response.count("```") % 2 == 1   # unpaired
                            )
                            _has_internal_protocol = bool(
                                re.search(r"(?<![A-Za-z0-9_])\??brtc\b", full_response, re.IGNORECASE)
                            )
                            if not _fr_lstrip.startswith("<tool_call") \
                                    and not _looks_like_tool_json \
                                    and not _is_orphan_fence \
                                    and not _has_internal_protocol:
                                if _buffer_finance_prose:
                                    _buffered_tokens.append(token)
                                elif on_token:
                                    on_token(token)
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            if any(x in err_msg.lower() for x in ("cannot connect", "connect call failed", "connection refused", "errno 61")):
                return _ollama_unavailable_result(ollama_url, err_msg)
            return {"success": False, "error": f"Ollama: {err_msg}"}

        # Fallback: parse text-based tool calls if no native ones found
        if not tool_calls_this_round and full_response.strip():
            text_calls = _parse_text_tool_calls(full_response)
            if text_calls:
                tool_calls_this_round = [
                    _register_tool_call(tc["tool"], tc["params"])
                    for tc in text_calls
                ]
                for tc in tool_calls_this_round:
                    if not tc.get("_aria_duplicate"):
                        tools_used.append(tc["tool"])
                    if on_tool_call and not tc.get("_aria_duplicate"):
                        on_tool_call(tc["tool"], tc["params"])

        # If repetition was detected, truncate and return cleanly
        if _rep_cancelled[0]:
            # Remove the repeated tail — keep only the first clean portion
            lines = full_response.strip().splitlines()
            # Find where repetition started: keep up to the point where unique content ends
            seen_paragraphs = set()
            clean_lines = []
            for line in lines:
                key = line.strip()
                if key and len(key) > 20:
                    if key in seen_paragraphs:
                        break  # Hit a repeated paragraph — stop here
                    seen_paragraphs.add(key)
                clean_lines.append(line)
            full_response = "\n".join(clean_lines).rstrip()
            if on_token:
                # The repetition note is appended as a final token
                on_token("\n\n*[model stopped — repetition detected]*")
                full_response += "\n\n*[model stopped — repetition detected]*"
            return {
                "success": True, "response": full_response,
                "tools_used": tools_used, "sources": [],
                "tool_calls_pending": [], "usage": usage, "provider": "ollama",
            }

        # If no tool calls this round
        if not tool_calls_this_round:
            full_response = _strip_tool_call_tags(full_response)
            if _buffer_finance_prose and on_token and full_response:
                on_token(full_response)
            clean_text = full_response.strip().lower()

            if _done_reason in {"length", "num_predict"} and _continuation_count < 3:
                _continuation_count += 1
                response_segments.append(full_response.rstrip())
                payload["messages"].append({"role": "assistant", "content": full_response})
                payload["messages"].append({
                    "role": "user",
                    "content": (
                        "继续完成上一条回答，从刚才中断处接着写。"
                        "不要重写已经输出的内容，不要总结，直到任务完整结束。"
                    ),
                })
                if HAS_RICH:
                    from aria_code.apps.cli.tools.write_tools import _ui_zh
                    console.print(f"\n  [dim]{'继续输出未完成内容…' if _ui_zh() else 'Continuing where the answer was cut off…'}[/dim]\n")
                continue

            # Detect "intent without action" — model says it will do something
            # but didn't output a tool call
            _intent_words = [
                "let me", "i will", "i'll", "let's", "让我", "我会", "我将",
                "让我们", "我来", "接下来", "下面", "我们来", "我需要",
                "再次", "重新", "检查", "修复", "fix", "retry", "check",
            ]
            has_intent = any(w in clean_text for w in _intent_words)
            should_nudge = (_in_error_recovery or _last_tool_had_error or has_intent) and _nudge_count < 5

            if should_nudge and tool_round < max_tool_rounds - 1:
                _nudge_count += 1
                if _in_error_recovery:
                    nudge = (
                        "SYSTEM: You are in error recovery mode. The script FAILED and is NOT yet fixed. "
                        "You MUST call a tool NOW to fix it:\n"
                        "- If you already read the file: call edit_file to fix the specific error, or write_file to rewrite.\n"
                        "- If you haven't read it: call read_file first.\n"
                        "- After fixing: call run_command to retry.\n"
                        "Do NOT output text. Output ONLY a <tool_call>."
                    )
                elif _last_tool_had_error:
                    nudge = (
                        "SYSTEM: The previous step FAILED. Fix it NOW by calling a tool:\n"
                        "1. read_file to see the code.\n"
                        "2. edit_file or write_file to fix.\n"
                        "3. run_command to retry.\n"
                        "Output a <tool_call> NOW."
                    )
                else:
                    nudge = (
                        "SYSTEM: You said you would do something but did not call a tool. "
                        "Do NOT describe what you will do — just DO it. "
                        "Output a <tool_call> NOW to take the next action."
                    )
                payload["messages"].append({"role": "assistant", "content": full_response})
                payload["messages"].append({"role": "user", "content": nudge})
                continue

            # Truly done. Tokens were already streamed above; do not print the
            # accumulated response again or the terminal shows duplicate blocks.
            break

        # Tool calls present — suppress model text (tool UI provides feedback)
        # Large models may emit multiple write_file calls in one round (project scaffolding).
        # Destructive / interactive tools (run_command, edit_file) remain sequential.
        _MUST_SERIALIZE = {"run_command", "edit_file"}
        if len(tool_calls_this_round) > 1:
            _all_safe = all(tc["tool"] not in _MUST_SERIALIZE for tc in tool_calls_this_round)
            _is_large_mdl = _model_size in ("large",)
            if _is_large_mdl and _all_safe:
                tool_calls_this_round = tool_calls_this_round[:5]  # max 5 parallel writes
            else:
                tool_calls_this_round = tool_calls_this_round[:1]

        if defer_tool_execution:
            clean_text = _strip_tool_call_tags(full_response)
            return {
                "success": True,
                "response": clean_text,
                "tools_used": [],
                "sources": [],
                "tool_calls_pending": [
                    {"tool": tc["tool"], "params": tc["params"]}
                    for tc in tool_calls_this_round
                    if not tc.get("_aria_duplicate")
                ],
                "usage": usage,
                "provider": "ollama",
            }

        # Execute tool calls locally and feed results back
        clean_text = _strip_tool_call_tags(full_response)
        payload["messages"].append({"role": "assistant", "content": clean_text,
                                     "tool_calls": [{"function": {"name": tc["tool"], "arguments": tc["params"]}}
                                                     for tc in tool_calls_this_round]})

        ollama_cancelled = False
        for tc in tool_calls_this_round:
            # Check cancel between tools
            if cancel_event and cancel_event.is_set():
                ollama_cancelled = True
                break

            tool_name = tc["tool"]
            # Note: _print_tool_call already called by on_tool_call during streaming

            if tc.get("_aria_duplicate"):
                reason = tc.get("_aria_limit_reason") or "duplicate tool call"
                summary = (
                    f"SYSTEM: Tool call skipped ({reason}): {tool_name}. "
                    "Use the existing tool result already available in this turn. "
                    "Do not call this tool again in this turn; finish the answer from available evidence."
                )
                payload["messages"].append({
                    "role": "tool",
                    "content": summary,
                })
                continue

            # Ask user confirmation for destructive tools
            if tool_name in _CONFIRM_TOOLS:
                try:
                    approval = _confirm_tool_execution_decision(
                        tool_name,
                        tc["params"],
                        config_policy=_ACTIVE_COMMAND_POLICY[0],
                    )
                    _apply_tool_approval(tc["params"], approval)
                    if not approval.approved:
                        ollama_cancelled = True
                        if HAS_RICH:
                            console.print("\n  [dim]Cancelled[/dim]")
                        break
                    # Persist "Allow & set balanced" choice
                    if approval.upgrade_policy:
                        tc["params"].pop("_upgrade_policy", None)
                        _ACTIVE_COMMAND_POLICY[0] = "balanced"
                        if HAS_RICH:
                            console.print("  [dim]策略已升级为 balanced（本会话）[/dim]")
                except KeyboardInterrupt:
                    ollama_cancelled = True
                    break

            try:
                tool_t0 = time.time()
                # Inject current policy for run_command so post-approval execution
                # isn't re-blocked by the default "safe" policy
                if tool_name == "run_command" and "policy" not in tc["params"]:
                    tc["params"]["policy"] = _ACTIVE_COMMAND_POLICY[0]
                result = execute_local_tool(tool_name, tc["params"])
                tool_dt = time.time() - tool_t0
            except KeyboardInterrupt:
                ollama_cancelled = True
                break
            _print_tool_result(tool_name, result, tool_dt)

            summary = _format_tool_summary(tool_name, result)

            # Track if this tool had an error (for nudge logic)
            _last_tool_had_error = not result.get("success", False)
            if result.get("success") and tool_name == "run_command":
                exit_code = result.get("data", {}).get("exit_code", 0)
                _last_tool_had_error = (exit_code != 0)

            # Error recovery state machine
            if _last_tool_had_error:
                _in_error_recovery = True
                _consecutive_reads = 0
                # Detect repeated failed run_command (same command failing 2+ times)
                if tool_name == "run_command":
                    cmd_str = tc["params"].get("command", "")
                    if cmd_str == _last_failed_cmd:
                        _consecutive_cmd_failures += 1
                    else:
                        _last_failed_cmd = cmd_str
                        _consecutive_cmd_failures = 1
                    if _consecutive_cmd_failures >= 2:
                        summary += ("\n\nSYSTEM: You have run the SAME command and it FAILED again with the same error. "
                                    "STOP re-running it. You MUST fix the code first:\n"
                                    "1. read_file to see the script content\n"
                                    "2. edit_file to fix the specific error (or write_file to rewrite entirely)\n"
                                    "3. THEN run_command to retry.\n"
                                    "Do NOT run the same command again until you have fixed the code.")
            elif tool_name in ("read_file", "list_files", "search_code"):
                # Diagnostic tools do NOT exit error recovery
                _consecutive_reads += 1
                # If model read the file 2+ times without fixing, inject directive
                if _in_error_recovery and _consecutive_reads >= 2:
                    summary += ("\n\nSYSTEM: You have read this file multiple times without fixing it. "
                                "STOP reading. Use edit_file to fix the specific error, "
                                "or use write_file to rewrite the entire script. Then run_command to retry.")
            elif tool_name in ("edit_file", "write_file"):
                # Fix was applied — stay in recovery until run_command succeeds
                _consecutive_reads = 0
                _consecutive_cmd_failures = 0  # Reset — code was changed
                _last_failed_cmd = ""
            elif tool_name == "run_command" and not _last_tool_had_error:
                # run_command succeeded — exit error recovery
                _in_error_recovery = False
                _consecutive_reads = 0
                _consecutive_cmd_failures = 0
                _last_failed_cmd = ""
                _nudge_count = 0

            if on_tool_result:
                on_tool_result(tool_name, summary)

            # Feed tool result back to Ollama for next round
            payload["messages"].append({
                "role": "tool",
                "content": summary,
            })

        if ollama_cancelled:
            return {"success": True, "response": full_response,
                    "cancelled": True, "tools_used": tools_used,
                    "sources": [], "thinking": "", "provider": "ollama", "usage": usage}

        # Continue streaming with tool results in context
        if HAS_RICH:
            console.print()  # newline before next AI response

    # Write successful stateless response to cache for future reuse
    if response_segments:
        full_response = "\n".join(part for part in response_segments + [full_response] if part)

    if _should_cache and full_response and not tools_used:
        _cache_set(_ck, full_response)

    # ── Code-block executor fallback ─────────────────────────────────────────
    # Small models often ignore the <tool_call> instruction and write plain code
    # blocks instead.  When the intent is "coding" and the model produced a
    # Python block but zero tool calls, auto-extract the code and queue
    # write_file + run_command so the outer agentic loop executes it.
    _auto_tool_calls: list = []
    _allow_code_block_autorun = (
        _intent == "coding"
        and _has_coding_signal
        and (_explicit_code_signal or not _is_visual_artifact_request)
    )
    if _allow_code_block_autorun and not tools_used and full_response:
        import re as _re
        # Accept both complete (``` closed) and truncated (unclosed) code blocks
        _py_blocks = _re.findall(r"```python\n(.*?)```", full_response, _re.DOTALL)
        if not _py_blocks:
            # Fallback: grab everything after the opening fence (handles truncation)
            _m = _re.search(r"```python\n(.*)", full_response, _re.DOTALL)
            if _m:
                _py_blocks = [_m.group(1)]
        if _py_blocks:
            _code = _py_blocks[-1].strip()
            # Basic sanitisation: strip leading spaces from ticker assignments
            _code = _re.sub(
                r"""(ticker\s*=\s*['"])(\s+)([A-Z]{1,10})(['"])""",
                r"\1\3\4", _code
            )
            # Auto-add missing `import mplfinance as mpf` when mpf is used
            if "mpf." in _code and "import mplfinance" not in _code:
                _code = "import mplfinance as mpf\n" + _code
            # Auto-add `import matplotlib.pyplot as plt` when plt is used
            if "plt." in _code and "import matplotlib.pyplot as plt" not in _code:
                _code = (
                    "import matplotlib; matplotlib.use('Agg')\n"
                    "import matplotlib.pyplot as plt\n" + _code
                )
            # Try to extract user-specified filename from the original message
            _fname_match = _re.search(
                r'保存(?:到|为|成)?\s*([^\s，,。]+\.py)'
                r'|save\s+(?:to\s+|as\s+)?([^\s,]+\.py)'
                r'|(?:named?|called?|filename?)\s+([^\s,]+\.py)',
                _intent_message, _re.IGNORECASE
            )
            if _fname_match:
                _fname = next(g for g in _fname_match.groups() if g)
                # Strip any path prefix from the extracted name
                _fname = os.path.basename(_fname)
            else:
                _fname = f"aria_generated_{int(time.time())}.py"
            _fpath = f"~/Documents/Aria Code/generated/{_fname}"

            # Validate Python syntax before writing — prepend warning comment if broken
            import py_compile as _pyc
            import tempfile as _tf2
            try:
                with _tf2.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as _stmp:
                    _stmp.write(_code)
                    _stmp_path = _stmp.name
                _pyc.compile(_stmp_path, doraise=True)
                os.unlink(_stmp_path)
            except _pyc.PyCompileError as _pce:
                # Surface the error prominently; file is still saved so user can fix it
                _err_line = str(_pce).replace(str(_stmp_path), _fname)
                _code = f"# ⚠️ SYNTAX ERROR (fix before running):\n# {_err_line}\n\n" + _code
                try:
                    os.unlink(_stmp_path)
                except Exception:
                    pass
            except Exception:
                pass

            _auto_tool_calls = [
                {"tool": "write_file",  "params": {"path": _fpath, "content": _code}},
            ]
            import shlex as _shlex_auto
            _auto_tool_calls.append({
                "tool": "run_command",
                "params": {
                    "command": f"python3 {_shlex_auto.quote(os.path.expanduser(_fpath))}",
                    "timeout": 120,
                },
            })
            if on_tool_call:
                on_tool_call("write_file",  _auto_tool_calls[0]["params"])
                on_tool_call("run_command", _auto_tool_calls[1]["params"])

    if _auto_tool_calls:
        return {"success": True, "response": full_response,
                "tool_calls_pending": _auto_tool_calls,
                "tools_used": tools_used, "sources": [], "thinking": "",
                "provider": "ollama", "usage": usage}

    return {"success": True, "response": full_response,
            "tools_used": tools_used, "sources": [], "thinking": "", "provider": "ollama",
            "usage": usage}
