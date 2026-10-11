#!/usr/bin/env python3
# ruff: noqa: E501
"""
Aria Code v3.0 — Claude Code 风格的量化投资终端 + 编程代理

Features:
  - SSE 流式 AI 对话 + Ollama 本地回退 (支持工具调用)
  - 本地工具系统: read_file, write_file, edit_file, list_files, search_code, run_command
  - Agentic 工具循环: AI 自动读取→分析→编辑→执行 (最多 8 轮)
  - 22 个远程 Aria 工具 + 6 个本地工具 + 15 个 Skills + 30 个 Slash 命令
  - Tab 补全, Rich Syntax 高亮, ESC 取消流式, Ctrl+D 退出
  - 会话管理 (保存/加载/恢复/导出)
  - 用户认证 + 上下文注入 + 反馈机制

Usage:
    aria-code                                          # 交互式 REPL（推荐）
    aria-code --resume                                 # 恢复上次会话
    aria-code -p "分析AAPL的技术面"                      # 单次查询
    aria-code quote AAPL MSFT                          # 快速报价
    python3 apps/cli/aria_cli.py                         # 交互式 REPL
    python3 apps/cli/aria_cli.py --resume                # 恢复上次会话
    python3 apps/cli/aria_cli.py -p "分析AAPL的技术面"     # 单次查询
    python3 apps/cli/aria_cli.py -p "写一个动量策略"       # AI 自动生成代码并保存
    python3 apps/cli/aria_cli.py quote AAPL MSFT          # 快速报价
    python3 apps/cli/aria_cli.py backtest momentum SPY    # 策略回测
    python3 apps/cli/aria_cli.py -p "AAPL PE" --json     # JSON 输出
"""

# Defined in aria_code/_version.py so the version can be read without
# importing the whole CLI. Re-exported here; aria_cli.__version__ still works.
from aria_code._version import __version__  # noqa: F401

# Fast path before loading agent, finance, and terminal modules.
import sys as _early_sys
if __name__ == "__main__":
    from aria_code.apps.cli.native_entry import dispatch_worker, maybe_native
    dispatch_worker()
    maybe_native()
if _early_sys.argv[1:2] == ["update"]:
    from aria_code.apps.cli.updater import main as _update_main
    raise SystemExit(_update_main(_early_sys.argv[2:]))
if _early_sys.argv[1:2] == ["health"]:
    from aria_code.apps.cli.model_probe import main as _health_main
    raise SystemExit(_health_main(_early_sys.argv[2:]))
if _early_sys.argv[1:] in (["--version"], ["-V"]):
    print(f"aria-code {__version__}")
    raise SystemExit(0)

from aria_code.apps.cli.commands.core_cmds import CoreCommandsMixin
# Stateless helpers now live in apps/cli/helpers.py; re-exported here so
# aria_cli's own callers keep working unchanged.
from aria_code.packages.aria_core.secure_file import write_secret_json
from aria_code.apps.cli.tool_registry import (  # noqa: F401 — re-exported
    ARIA_TOOLS,
    LOCAL_TOOLS,
    LOCAL_TOOL_SCHEMAS,
)
from aria_code.apps.cli.provider_keys import (  # noqa: F401 — re-exported
    get_provider_key as _get_provider_key,
    load_data_keys as _load_data_keys,
)
from aria_code.apps.cli.helpers import (  # noqa: F401
    detect_ollama_models,
    detect_ollama_models_rich,
    _load_project_context,
    _display_value,
    _chart_display_label,
    _chart_period_from_ta_days,
    _copy_text_to_clipboard,
    _reveal_path_in_finder,
    _open_path_or_url,
    _extract_code_block,
    _clean_tool_error_message,
    _fuzzy_match,
    _is_ashare_symbol,
    format_sparkline,
)
import sys

# Windows consoles default to a legacy codepage (cp1252 etc.), not UTF-8 —
# and this CLI's own --help text, examples, and output are intentionally
# bilingual (Chinese + English; see the module docstring above). Any of that
# text reaching stdout/stderr without this crashes with UnicodeEncodeError
# (caught by CI's install-smoke-test: `aria --help` on windows-latest).
# reconfigure() is a no-op in practice on POSIX, where stdout is already
# UTF-8, so this is safe to run unconditionally on every platform.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import os
import asyncio
import json
import argparse
try:
    # readline is Unix-only in the standard library — it does not exist on
    # Windows Python at all (CI's install-smoke-test caught this: every
    # Windows install crashed on startup with "ModuleNotFoundError: No
    # module named 'readline'", regardless of prompt_toolkit being the
    # preferred/primary input backend). The one place this module is
    # actually used (readline.* calls further down) already prefers
    # prompt_toolkit and wraps the readline fallback in its own
    # try/except, so leaving `readline` unset here degrades correctly —
    # the crash was purely from this unguarded top-level import running
    # before any of that fallback logic got a chance to execute.
    import readline
except ImportError:
    readline = None
import logging
import time
from aria_code.ui.render.output import file_uri as _file_uri
import shlex
import pathlib
import uuid
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from brokers.base import AccountInfo

from aria_code.apps.cli.plotly_html import plotly_script_tag
from aria_code.apps.cli.bootstrap import (
    default_config,
    disable_broken_proxy as _disable_broken_proxy,
    initialize_cli_environment,
    load_aria_env as _load_aria_env,
    runtime_paths,
)
from aria_code.apps.cli.config_store import load_cli_config, save_cli_config
from aria_code.apps.cli.lifecycle_hooks import run_event_hook

initialize_cli_environment()

from aria_code.change_store import ChangeConflictError, GLOBAL_CHANGE_STORE
from aria_code.safety import evaluate_command_policy
from aria_code.plan_utils import parse_plan_steps
from aria_code.privacy import FeedbackRecord, FeedbackStore, PrivacySettings
from aria_code.apps.cli.session_store import SessionManager
from aria_code.apps.cli.turn_planning import (
    estimate_context_tokens,
    is_complex_task,
    post_turn_context_decision,
    round_budget_for,
    should_decompose,
)
from aria_code.apps.cli.prompt_assembly import build_base_message, should_prepend_file_tool_hint, with_ml_signal_prefix
from aria_code.runtime import (
    AgentErrorPresentation,
    AgentTurnEnvelope,
    AgentTurnState,
    ApprovalDecision,
    RuntimeTrace,
    ToolExecutor,
    apply_approval_decision,
)
try:
    # RunStatus/RunStore land with the durable run-state store (runtime/run_store.py,
    # runtime/run_state.py) — still landing separately. Degrade gracefully rather than
    # fail the whole module import: _run_store stays None, every _transition_runtime_run/
    # _begin_runtime_run call already no-ops when that's the case (see ArtheraTerminal).
    from runtime import RunStatus, RunStore
except ImportError:
    RunStore = None

    class RunStatus:  # type: ignore[no-redef]  # pragma: no cover - fallback until runtime/run_state.py lands
        PLANNING = RUNNING = WAITING_APPROVAL = VERIFYING = "unavailable"
        SUCCEEDED = FAILED = CANCELLED = INTERRUPTED = "unavailable"
from aria_code.runtime.tool_policy import check_tool_policy
from aria_code.apps.cli.plan_mode import PlanModeState
from aria_code.workspace import VerificationPlanner, WorkspaceFiles, WorkspaceSecurity
from aria_code.apps.cli.commands.catalog import VISIBLE_SLASH_COMMANDS
from aria_code.apps.cli.commands.market_context import build_analyze_context, build_analyze_prompt
from aria_code.apps.cli.market_hours import market_session
from aria_code.apps.cli.commands.market import (
    parse_analysis_args,
    parse_symbols,
    parse_technical_args,
    route_top_level_text,
    sanitize_chart_symbol_args,
    try_top_level_route,
)
from aria_code.apps.cli.providers.base import AriaSSEProvider, ConfiguredProvider, OllamaProvider
from aria_code.apps.cli.preflight import build_intent_preflight, format_preflight_plain
from aria_code.apps.cli.runtime_consumer import (
    TerminalApprovalEventConsumer,
    TerminalRuntimeEventConsumer,
    TurnPhase,
)
from aria_code.packages.aria_sdk.streaming import stream_provider_result
# broker_cmds / model_cmds / workspace_cmds 的 mixin 方法裸名调用 aria_home()。
# 它们各自模块顶部也 import 了它，但 _rebind_mixin_globals 把方法的 __globals__
# 整个换成本模块的，那层导入在运行期不参与解析——只有这里这一份算数。
from aria_code.packages.aria_core.paths import aria_home
from aria_code.ui.render.market import print_quote_result, print_ta_result
from aria_code.apps.cli.commands.report import (
    all_agents_failed,
    build_markdown_report_prompt,
    export_report_pdf,
    generate_html_report,
    parse_report_args,
    report_agent_health,
    report_agent_names,
    report_file_size_kb,
    save_markdown_report,
    update_report_index,
)
from aria_code.apps.cli.commands.team import (
    parse_team_args,
    resolve_team_symbols,
    run_deep_cli,
    run_team_analysis,
    save_team_report,
    team_agent_names,
)
from aria_code.ui.render.team import (
    VERDICT_STYLE,
    build_team_table_rows,
    calc_column_widths,
    render_team_rows_plain,
    render_team_table,
    render_verdict_banner,
    team_mode_label,
)
from aria_code.ui.render.finance import (
    render_finance_result,
    render_macro_result,
    render_cb_rates,
    render_econ_calendar,
    render_options_chain,
    render_quality_scores,
    render_ichimoku,
    render_fear_greed,
    render_funding_rates,
    render_peer_comparison,
    render_house_price,
    render_reits_list,
    render_rental_yield,
    render_property_val,
    render_multi_city,
    render_asset_score,
    render_corr_matrix,
    render_portfolio_bt,
    render_sql_result,
    render_alerts,
)
from aria_code.apps.cli.direct import dispatch_direct_command, is_watchable_direct_command
from aria_code.apps.cli.tools.system_tools import (
    tool_ask_user as _src_ask_user,
    tool_run_command as _src_run_command,
    tool_web_fetch   as _src_web_fetch,
    tool_github      as _src_github,
)
from aria_code.apps.cli.tools.file_tools import (  # noqa: E402
    tool_lsp_hover as _tool_lsp_hover,
    tool_lsp_definition as _tool_lsp_definition,
    tool_lsp_references as _tool_lsp_references,
)
from aria_code.apps.cli.tools.notebook_tools import (
    tool_glob          as _src_glob,
    tool_notebook_read as _src_notebook_read,
    tool_notebook_edit as _src_notebook_edit,
)
from aria_code.apps.cli.tools.file_tools import (
    tool_read_file   as _src_read_file,
    tool_list_files  as _src_list_files,
    tool_search_code as _src_search_code,
)
from aria_code.apps.cli.tools.market_tools import (
    tool_get_market_data    as _src_get_market_data,
    tool_get_market_history as _src_get_market_history,
    tool_broker_query       as _src_broker_query,
    tool_broker_order       as _src_broker_order,
)
from aria_code.apps.cli.handlers.broker_handlers import handle_broker_query as _src_handle_broker_query
from aria_code.apps.cli.handlers.realty_handlers import handle_realty_query as _src_handle_realty_query
from aria_code.apps.cli.handlers.strategy_advice import handle_strategy_advice as _src_strategy_advice
from aria_code.apps.cli.handlers.chart_handlers import (
    handle_stock_chart_analysis_direct as _src_chart_analysis_direct,
    handle_stock_chart_analysis        as _src_chart_analysis,
)
from aria_code.apps.cli.utils.market_detect import (  # noqa: F401 — re-exported
    _re_sym, _STOCK_PATTERN,
    _CRYPTO_WORDS, _COMPANY_TO_TICKER,
    _BROKER_INTENT_KW, _is_broker_intent,
    _is_broker_guide_intent, _is_broker_setup_intent, _detect_broker_type,
    _FINANCIAL_TERMS_BLOCKLIST,
    _is_blocked_market_symbol_candidate,
    _extract_market_symbol, _extract_market_symbols, _extract_symbol_from_history,
    _is_stock_chart_analysis_request,
    _UNRESOLVED_CO_INDICATORS, _has_unresolved_company_mention,
    _REALTY_QUERY_KEYWORDS, _CN_CITIES, _INTL_CITIES, _STOCK_ONLY_MARKET_WORDS,
    _is_realty_query,
    _is_market_snapshot_request,
    _format_compact_market_cap, _market_snapshot_trend,
)

from aria_code.apps.cli.commands.broker_cmds import BrokerCommandsMixin
from aria_code.apps.cli.commands.canvas_cmds import CanvasCommandsMixin
from aria_code.apps.cli.commands.backtest_cmds import (
    BacktestCommandsMixin,
    format_backtest_data_error,  # mixin 方法裸名调用，见下方 _rebind_mixin_globals
    _print_sparkline,
)
from aria_code.apps.cli.commands.analysis_cmds import AnalysisCommandsMixin
from aria_code.apps.cli.commands.ashare_prediction_cmds import (
    ASharePredictionCommandsMixin,
    build_prediction_service,
    fetch_live_ashare_universe,
    load_universe_file,
    parse_ashare_predict_args,
    prediction_freshness,
)
from aria_code.apps.cli.commands.data_cmds import DataCommandsMixin
from aria_code.apps.cli.commands.ops_cmds import OpsCommandsMixin
from aria_code.apps.cli.commands.diagnostic_cmds import DiagnosticCommandsMixin
from aria_code.apps.cli.commands.diagnostic_ops_cmds import (
    DiagnosticOpsCommandsMixin,
    format_architecture_report,  # 同上
)
from aria_code.apps.cli.commands.ui_cmds import UiCommandsMixin
from aria_code.apps.cli.commands.session_ux_cmds import SessionUxCommandsMixin
from aria_code.apps.cli.commands.auth_cmds import AuthCommandsMixin
from aria_code.apps.cli.commands.file_cmds import FileCommandsMixin
from aria_code.apps.cli.commands.fx_commodity_cmds import FxCommodityCommandsMixin
from aria_code.apps.cli.commands.finance_service_cmds import FinanceServiceCommandsMixin
from aria_code.apps.cli.commands.orchestrator_cmds import OrchestratorCommandsMixin
from aria_code.apps.cli.commands.workflow_cmds import WorkflowCommandsMixin
from aria_code.apps.cli.commands.business_workflow_cmds import BusinessWorkflowCommandsMixin
from aria_code.apps.cli.commands.warehouse_cmds import LogisticsCommandsMixin, WarehouseCommandsMixin
from aria_code.apps.cli.commands.session_cmds import SessionCommandsMixin
from aria_code.apps.cli.session_export import build_session_export_payload  # 同上
from aria_code.apps.cli.commands.workspace_cmds import WorkspaceCommandsMixin
from aria_code.apps.cli.commands.model_cmds import ModelCommandsMixin
from aria_code.apps.cli.commands.market_cmds import (
    MarketCommandsMixin,
    _fetch_public_news_fallback,
)
from aria_code.apps.cli.commands.portfolio_cmds import PortfolioCommandsMixin
from aria_code.apps.cli.commands.pdf_export_cmds import PdfExportCommandsMixin
from aria_code.apps.cli.handlers.market_handlers import (
    _try_prefetch_market_data  as _src_prefetch_market_data,
    _try_handle_multi_market_snapshot  as _src_multi_snapshot,
    _try_handle_market_snapshot_analysis  as _src_market_snapshot_analysis,
    _try_handle_market_overview  as _src_market_overview,
)


# ── New modules: local LLM provider stack, finance tools, MCP, ariarc ──────
try:
    from model_capability import (
        get_model_capability, build_tool_system_prompt,
        RECOMMENDED_FINANCE_MODELS, parse_tool_calls_from_response as _parse_model_tool_calls,
    )
    _HAS_MODEL_CAP = True
except ImportError:
    _HAS_MODEL_CAP = False

try:
    from local_finance_tools import register_local_finance_tools
    _HAS_LOCAL_FINANCE = True
except ImportError:
    _HAS_LOCAL_FINANCE = False

# Market data imports pandas and NumPy; coding-only sessions do not need it.
from importlib.util import find_spec as _find_spec
_HAS_MDC = _find_spec("market_data_client") is not None
_MDC = None


def _get_mdc():
    if not _HAS_MDC:
        return None
    from market_data_client import get_mdc
    return get_mdc()

# (legacy financial_agents fallback removed — the agents/ package is the sole path)

try:
    from strategy_vault import get_vault as _get_vault, ai_review_strategy as _ai_review
    _HAS_VAULT = True
except ImportError:
    _HAS_VAULT = False

try:
    from mcp_client import MCPToolRegistry, MCP_CONFIG_PATH, init_mcp as _init_mcp
    _HAS_MCP = True
    _mcp_registry: Optional["MCPToolRegistry"] = None
except ImportError:
    _HAS_MCP = False
    _mcp_registry = None

try:
    from ariarc import AriaRC, get_ariarc, reload_ariarc
    _HAS_ARIARC = True
except ImportError:
    _HAS_ARIARC = False

try:
    from brokers import (
        get_registry as _get_broker_registry,
        list_broker_configs as _list_broker_configs,
        get_broker_config as _get_broker_cfg,
        add_broker_config as _add_broker_cfg,
        remove_broker_config as _remove_broker_cfg,
        set_default_broker as _set_default_broker,
        validate_broker_config as _validate_broker_cfg,
        supported_broker_types as _supported_broker_types,
        get_config_template as _get_broker_template,
        BROKERS_CONFIG_PATH as _BROKERS_CONFIG_PATH,
    )
    _HAS_BROKERS = True
except ImportError:
    _HAS_BROKERS = False
    def _get_broker_registry(): return None   # type: ignore
    def _list_broker_configs(): return []      # type: ignore
    _BROKERS_CONFIG_PATH = None

try:
    from plugin_loader import register_plugin_tools, find_plugin_file, PluginWatcher
    _HAS_PLUGIN = True
    _plugin_watcher: Optional["PluginWatcher"] = None
except ImportError:
    _HAS_PLUGIN = False
    _plugin_watcher = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)
# Library internals are not the user's business: on a fresh install the first
# /ta printed "WARNING:matplotlib.font_manager:Matplotlib is building the font
# cache; this may take a moment." into the middle of its output.
logging.getLogger("matplotlib").setLevel(logging.ERROR)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("curl_cffi").setLevel(logging.CRITICAL)

# ============================================================================
# Rich Console (graceful fallback to ANSI if not installed)
# ============================================================================

# ── UI layer — console, flags, ESC watcher ────────────────────────────────────
from aria_code.ui.console import (
    console, HAS_RICH, HAS_PT, _SYNTAX_THEME, make_markdown,
    _EscWatcher, _esc_watcher, _HAS_TERMIOS,
)
from aria_code.ui.robot import RobotState, set_robot_state
# Rich re-exports (used directly in this file)
if HAS_RICH:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text
    from rich.status import Status
    from rich.syntax import Syntax
    from rich.panel import Panel
    from rich.rule import Rule
    from rich import box as rich_box
else:
    rich_box = None
# prompt_toolkit re-exports
if HAS_PT:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings as _PTKeyBindings
# termios — already imported inside ui.console; alias for local use
if _HAS_TERMIOS:
    import termios, tty, select as _select


from aria_code.ui.picker import arrow_select as _arrow_select, run_picker_in_thread as _run_picker_in_thread



# ============================================================================
# Configuration & Persistent Memory
# ============================================================================
_PATHS = runtime_paths()
CONFIG_DIR = _PATHS.config_dir
CONFIG_FILE = _PATHS.config_file
HISTORY_FILE = _PATHS.history_file
SESSIONS_DIR = _PATHS.sessions_dir
PROVIDERS_FILE = _PATHS.providers_file  # Cloud API keys (Open Interpreter style)

# ── Cloud Provider key map ───────────────────────────────────────────────────
# Maps provider short name → environment variable name for API key.
from aria_code.apps.cli.constants import _PROVIDER_KEY_MAP, _DATA_KEY_MAP, _DATA_SIGNUP_URLS, _LLM_SIGNUP_URLS

# Default base URLs for cloud providers (OpenAI-compatible unless noted)
# _PROVIDER_BASE_URLS 已移到 apps/cli/provider_endpoints.py（28 个端点）。
# 这里 re-import 回本模块命名空间：多处裸名引用依赖它在 aria_cli 的 globals 里。
from aria_code.apps.cli.provider_endpoints import _PROVIDER_BASE_URLS








# One-line description for each provider (shown in picker)
_PROVIDER_DESC: Dict[str, str] = {
    # ── 国际 LLM ──────────────────────────────────────────────────────
    "deepseek":    "DeepSeek-V3/R1  强推理·中文优秀·价格极低",
    "openai":      "GPT-4.5 / o3 / o4-mini / o3-pro  最广泛兼容·持续更新",
    "anthropic":   "Claude Sonnet 4 / Opus 4  长文档·代码·分析",
    "groq":        "Llama3/Qwen  超高速推理 (免费额度)",
    "together":    "开源模型聚合  Llama/Qwen/DeepSeek 等 100+ 模型",
    "google":      "Gemini 3.8 Flash / 3.5 Flash / 3.5 Flash-Lite  多模态·超长上下文",
    "xai":         "Grok-3 / Grok-3-Fast  实时网络数据·Twitter整合",
    "mistral":     "Mistral Large / Codestral  欧洲顶级·代码生成强",
    "cohere":      "Command R+  RAG检索增强·企业文档处理",
    "perplexity":  "Sonar  实时联网搜索·研究报告",
    # ── 国内 LLM ──────────────────────────────────────────────────────
    "dashscope":   "通义千问 Max/Long/Turbo  阿里云·国内访问稳定",
    "siliconflow": "Qwen/GLM/DeepSeek  国内多模型聚合·价格低",
    "moonshot":    "Kimi  超长上下文 128K  中文理解·长文档",
    "zhipu":       "GLM-4-Plus / GLM-4-Flash  中文推理·代码生成",
    "baidu":       "ERNIE 4.5 Turbo  百度文心·国内最强中文",
    "bytedance":   "Doubao / 豆包  字节跳动·多模态·免费额度大",
    "minimax":     "MiniMax-Text-01  百万Token上下文",
    "stepfun":     "Step-2-16K  阶跃星辰·复杂推理",
    "01ai":        "Yi-Large / Yi-Vision  零一万物·中英双语",
    # ── Data ──────────────────────────────────────────────────────────
    "finnhub":     "实时美股行情+新闻  免费 60次/min",
    "alphavantage":"美股历史数据+技术指标  免费 25次/day",
    "polygon":     "美股全量数据+期权链  免费层可用",
    "fmp":         "财务报表+估值数据  免费层",
    "twelvedata":  "全球行情  A股/港股/美股  免费 800次/day",
    "newsapi":     "全球新闻聚合  免费 100次/day",
    "coingecko":   "加密货币行情+项目数据  基础免费",
    "tavily":      "AI搜索引擎  1000次/month 免费",
    "brave":       "网页搜索  2000次/month 免费",
}

# Detailed guide: where to get key + what it unlocks (shown in Panel before input)
_PROVIDER_GUIDE: Dict[str, str] = {
    "deepseek": (
        "1. 打开 platform.deepseek.com/api_keys\n"
        "2. 注册/登录 → 点击「创建 API Key」\n"
        "3. 复制 sk-xxxxxxxx 格式的密钥\n\n"
        "解锁: DeepSeek-V3 (最强中文推理) · DeepSeek-R1 (CoT思维链)\n"
        "价格: V3 约 ¥1/百万 token，远低于 GPT-4o"
    ),
    "openai": (
        "1. 打开 platform.openai.com/api-keys\n"
        "2. 登录 → 「Create new secret key」\n"
        "3. 复制 sk-proj-xxxxxxxx 格式密钥\n\n"
        "解锁: GPT-4o / GPT-4o-mini / o1 / o1-mini\n"
        "注意: 需绑定付款方式才能使用 GPT-4o"
    ),
    "anthropic": (
        "1. 打开 console.anthropic.com/settings/keys\n"
        "2. 登录 → 「Create Key」\n"
        "3. 复制 sk-ant-xxxxxxxx 格式密钥\n\n"
        "解锁: Claude Sonnet 4 · Claude Opus · Claude Haiku\n"
        "优势: 200K上下文·长文档分析·代码审查最强"
    ),
    "groq": (
        "1. 打开 console.groq.com/keys\n"
        "2. 登录 → 「Create API Key」\n"
        "3. 复制 gsk_xxxxxxxx 格式密钥\n\n"
        "解锁: Llama3-70B · Mixtral · Gemma\n"
        "优势: 每秒 500+ tokens，目前最快的免费推理"
    ),
    "together": (
        "1. 打开 api.together.ai/settings/api-keys\n"
        "2. 注册 → 「Create API Key」\n"
        "3. 复制 xxxxxxxx 格式密钥\n\n"
        "解锁: Llama3/Qwen/DeepSeek/Yi 等 100+ 开源模型\n"
        "新用户赠 $5 免费额度"
    ),
    "dashscope": (
        "1. 打开 dashscope.console.aliyun.com/apiKey\n"
        "2. 用阿里云账号登录\n"
        "3. 点击「创建新的 API-KEY」\n\n"
        "解锁: 通义千问2.5 / 通义千问Max / 通义千问Long\n"
        "优势: 国内访问无需代理，中文理解优秀"
    ),
    "siliconflow": (
        "1. 打开 cloud.siliconflow.cn/account/ak\n"
        "2. 注册/登录 → 「创建 API Key」\n"
        "3. 复制密钥\n\n"
        "解锁: Qwen2.5 / GLM-4 / DeepSeek / Yi 等\n"
        "新用户赠 14元免费额度，国内直连"
    ),
    "moonshot": (
        "1. 打开 platform.moonshot.cn/console/api-keys\n"
        "2. 注册/登录 → 「新建 API Key」\n"
        "3. 复制 sk-xxxxxxxx 格式密钥\n\n"
        "解锁: Kimi (moonshot-v1-8k/32k/128k)\n"
        "优势: 128K超长上下文，处理长文档首选"
    ),
    "zhipu": (
        "1. 打开 open.bigmodel.cn/usercenter/apikeys\n"
        "2. 注册/登录 → 「添加新的 API Key」\n"
        "3. 复制密钥\n\n"
        "解锁: GLM-4 / GLM-4-Flash / GLM-4V (多模态)\n"
        "GLM-4-Flash 速度快，新用户有免费额度"
    ),
    # Data services
    "finnhub": (
        "1. 打开 finnhub.io/register 注册\n"
        "2. 进入 Dashboard → 复制 API Key\n\n"
        "解锁: 美股实时报价 · 公司新闻 · 基本面数据\n"
        "免费额度: 60次/分钟，足够个人使用"
    ),
    "alphavantage": (
        "1. 打开 alphavantage.co/support/#api-key\n"
        "2. 填写邮箱 → 即时获取 Key\n\n"
        "解锁: 美股日K/周K历史数据 · RSI/MACD等技术指标\n"
        "免费额度: 25次/天，500次/月"
    ),
    "polygon": (
        "1. 打开 polygon.io/signup 注册\n"
        "2. Dashboard → API Keys → 复制 Key\n\n"
        "解锁: 美股实时+历史 · 期权链 · 新闻\n"
        "免费层: 延迟15分钟数据，基础期权数据"
    ),
    "fmp": (
        "1. 打开 financialmodelingprep.com/register\n"
        "2. 注册 → Dashboard → 复制 API Key\n\n"
        "解锁: 财报(资产负债表/利润表) · PE/PB等估值\n"
        "免费层: 250次/天，历史财报数据"
    ),
    "twelvedata": (
        "1. 打开 twelvedata.com/register 注册\n"
        "2. Dashboard → API Keys → 复制\n\n"
        "解锁: 全球行情 (A股/港股/美股/加密/外汇)\n"
        "免费额度: 800次/天，支持日K历史数据"
    ),
    "newsapi": (
        "1. 打开 newsapi.org/register 注册\n"
        "2. 注册后即显示 API Key\n\n"
        "解锁: 全球新闻聚合 · 按股票名称搜索相关新闻\n"
        "免费层: 100次/天（仅限开发者模式）"
    ),
    "coingecko": (
        "1. 打开 coingecko.com/en/api 注册\n"
        "2. 选择 Demo 计划 (免费) → 生成 Key\n\n"
        "解锁: 加密货币实时价格 · 历史数据 · 项目信息\n"
        "Demo Key: 30次/分钟，基础行情足够"
    ),
    "tavily": (
        "1. 打开 app.tavily.com 注册\n"
        "2. 控制台 → 复制 API Key (tvly-xxxxxxxx)\n\n"
        "解锁: AI优化的网页搜索，返回结构化摘要\n"
        "免费额度: 1000次/月"
    ),
    "brave": (
        "1. 打开 api.search.brave.com/app/keys\n"
        "2. 注册 → 「Add Key」→ 选择 Free 计划\n\n"
        "解锁: 网页搜索 (无追踪，隐私优先)\n"
        "免费额度: 2000次/月"
    ),
    # ── 新增国际 Provider ──────────────────────────────────────────────
    "google": (
        "1. 打开 aistudio.google.com/app/apikey\n"
        "2. 用 Google 账号登录 → 「Create API key」\n"
        "3. 复制 AIzaSy... 格式的密钥\n\n"
        "解锁: Gemini 3.8 Flash · Gemini 3.5 Flash · 多模态视觉\n"
        "用法: /model google/gemini-3.5-flash\n"
        "免费额度以 Google AI Studio 当前公布为准"
    ),
    "gemini": (
        "同 google provider，填入 Google AI Studio 的 API Key\n\n"
        "推荐模型:\n"
        "  gemini-3.5-flash       — 稳定版，适合生产（默认）\n"
        "  gemini-3.8-flash       — 最新最强，迭代较快\n"
        "  gemini-3.5-flash-lite  — 更快更省\n\n"
        "用法: /model gemini/gemini-3.5-flash"
    ),
    "xai": (
        "1. 打开 console.x.ai → 注册/登录\n"
        "2. 创建 API Key (xai-...)\n\n"
        "解锁: Grok-3 · Grok-3-Fast · Grok-3-Mini (推理)\n"
        "优势: 实时访问 Twitter/X 数据，最新新闻事件感知\n"
        "用法: /model xai/grok-3\n"
        "价格: Grok-3 $3/M tokens，Fast $5/M tokens"
    ),
    "grok": (
        "同 xai provider，填入 xAI Console 的 API Key\n\n"
        "推荐模型:\n"
        "  grok-3           — 旗舰推理\n"
        "  grok-3-fast      — 高速版\n"
        "  grok-3-mini      — 轻量思考模型\n\n"
        "用法: /model grok/grok-3-fast"
    ),
    "mistral": (
        "1. 打开 console.mistral.ai → 注册\n"
        "2. 「API Keys」→ 「Create new key」\n"
        "3. 复制密钥\n\n"
        "解锁: Mistral Large 2 · Mistral Small · Codestral (代码)\n"
        "用法: /model mistral/mistral-large-latest\n"
        "优势: 欧洲 GDPR 合规，Codestral 为代码生成最强之一\n"
        "免费额度: 新用户有试用额度"
    ),
    "cohere": (
        "1. 打开 dashboard.cohere.com → 注册\n"
        "2. 「API Keys」→ 复制 Trial key\n\n"
        "解锁: Command R+ · Command R · Embed · Rerank\n"
        "用法: /model cohere/command-r-plus\n"
        "优势: RAG 检索增强最强，企业文档处理首选\n"
        "Trial Key: 免费可用，速率限制较低"
    ),
    "perplexity": (
        "1. 打开 perplexity.ai/settings/api → 注册\n"
        "2. 「Generate」→ 复制 pplx-... 密钥\n\n"
        "解锁: sonar · sonar-pro · sonar-reasoning (联网推理)\n"
        "用法: /model perplexity/sonar-pro\n"
        "优势: 实时联网，自动引用来源，研究报告首选\n"
        "价格: sonar $1/M tokens，sonar-pro $3/M tokens"
    ),
    # ── 新增国内 Provider ──────────────────────────────────────────────
    "baidu": (
        "1. 打开 qianfan.cloud.baidu.com → 用百度账号登录\n"
        "2. 「用户中心」→「Access Token」→ 记录 Key 和 Secret\n\n"
        "解锁: ERNIE 4.5 Turbo · ERNIE Speed · ERNIE-Lite\n"
        "用法: /model baidu/ernie-4.5-turbo-128k\n"
        "优势: 国内最强中文理解，百度搜索知识整合\n"
        "免费额度: ERNIE Speed/Lite 大量免费 Token"
    ),
    "ernie": (
        "同 baidu provider，填入百度千帆平台的 Access Key\n\n"
        "推荐模型:\n"
        "  ernie-4.5-turbo-128k  — 旗舰，128K 上下文\n"
        "  ernie-speed-128k      — 高速，大量免费\n"
        "  ernie-lite-8k         — 轻量，免费额度最大\n\n"
        "用法: /model ernie/ernie-4.5-turbo-128k"
    ),
    "bytedance": (
        "1. 打开 ark.volcengine.com → 注册字节跳动账号\n"
        "2. 「API Key 管理」→ 创建 API Key\n"
        "3. 同时需要创建「推理接入点」获取 endpoint-id\n\n"
        "解锁: Doubao-1.5-Pro · Doubao-1.5-Lite · Doubao Vision\n"
        "用法: /model bytedance/<endpoint-id>\n"
        "优势: 字节跳动首选，多模态，免费额度很大\n"
        "新用户: 500万免费 Token"
    ),
    "doubao": (
        "同 bytedance provider，填入火山方舟 API Key\n\n"
        "推荐模型 (需先在控制台创建接入点):\n"
        "  doubao-1.5-pro-32k     — 旗舰\n"
        "  doubao-1.5-lite-32k    — 轻量快速\n"
        "  doubao-pro-vision-32k  — 多模态\n\n"
        "用法: /model doubao/<你的endpoint-id>"
    ),
    "minimax": (
        "1. 打开 platform.minimaxi.com → 注册\n"
        "2. 「接口密钥」→ 生成 API Key\n\n"
        "解锁: MiniMax-Text-01 (百万 Token 上下文!)\n"
        "用法: /model minimax/MiniMax-Text-01\n"
        "优势: 100万 Token 超长上下文，超长文档/代码库分析首选\n"
        "价格: 约 ¥1/百万 Token"
    ),
    "stepfun": (
        "1. 打开 platform.stepfun.com → 注册\n"
        "2. 「接口密钥」→ 创建 API Key\n\n"
        "解锁: step-2-16k · step-2-mini · step-1v-32k (视觉)\n"
        "用法: /model stepfun/step-2-16k\n"
        "优势: 阶跃星辰，数理逻辑和推理能力突出\n"
        "新用户: 有免费额度"
    ),
    "01ai": (
        "1. 打开 platform.lingyiwanwu.com → 注册\n"
        "2. 「API Keys」→ 创建密钥\n\n"
        "解锁: yi-large · yi-medium · yi-vision\n"
        "用法: /model 01ai/yi-large\n"
        "优势: 零一万物，中英双语均衡，视觉理解能力强"
    ),
    "yi": (
        "同 01ai provider，填入零一万物平台的 API Key\n\n"
        "推荐模型:\n"
        "  yi-large         — 旗舰推理\n"
        "  yi-medium        — 速度/质量均衡\n"
        "  yi-vision        — 图像理解\n\n"
        "用法: /model yi/yi-large"
    ),
}


# _test_api_key 已移到 apps/cli/provider_endpoints.py（178 行）。
# 必须用 _rebind_module_function_globals 而非普通 import：model_cmds.py 那个
# mixin 用裸名调用它，靠 _rebind_mixin_globals 把 __globals__ 指向本模块。
# 该重绑调用放在文件靠后处（_rebind_module_function_globals 定义之后）。


def _load_providers_json() -> Dict[str, Any]:
    """Load providers.json from the Aria config dir and return the 'llm' section.

    Returns an empty dict if the file doesn't exist or is malformed.
    """
    try:
        if PROVIDERS_FILE.exists():
            data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
            return data.get("llm", data) if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_providers_json(llm_section: Dict[str, Any]) -> None:
    """Persist LLM provider API keys to providers.json in the Aria config dir."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing: Dict = {}
    if PROVIDERS_FILE.exists():
        try:
            existing = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing["llm"] = llm_section
    write_secret_json(PROVIDERS_FILE, existing)


def _save_data_key(service: str, key: str) -> None:
    """Persist a data service API key to providers.json under 'data' section."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing: Dict = {}
    if PROVIDERS_FILE.exists():
        try:
            existing = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    data_section = existing.get("data", {})
    data_section[service] = {"api_key": key}
    existing["data"] = data_section
    write_secret_json(PROVIDERS_FILE, existing)





DEFAULT_CONFIG = default_config()

# Module-level write/command policies — updated whenever config is loaded/changed.
# Used by standalone tool functions without terminal access.
_ACTIVE_WRITE_POLICY = ["desktop_only"]  # list so closures can mutate it
_ACTIVE_COMMAND_POLICY = ["safe"]
_ACTIVE_PERMISSION_MODE = ["workspace-write"]
_PERMISSION_CYCLE = ["read-only", "workspace-write", "full-access"]
_ACTIVE_NETWORK_ENABLED = [True]
_ACTIVE_LSP_AUTOCHECK = [False]  # opt-in: run LSP diagnostics after each edit
# Approval by risk (safety.risk.approval_requirement): "manual" asks as before;
# "risk" runs calls at or below the auto-approve level without a prompt.
_ACTIVE_APPROVAL_MODE = ["manual"]
_ACTIVE_AUTO_APPROVE_LEVEL = [1]
# True only while a chat turn renders tools as action cells: run_command then
# leaves its exit line and output tail to the cell instead of printing them.
_ACTION_VIEW_ACTIVE = [False]


def _sync_write_policy(config: dict):
    """Sync module-level write/command policies from config dict."""
    _ACTIVE_WRITE_POLICY[0] = config.get("write_policy", "desktop_only")
    _ACTIVE_COMMAND_POLICY[0] = config.get("command_policy", "safe")
    _ACTIVE_PERMISSION_MODE[0] = config.get("permission_mode", "workspace-write")
    _ACTIVE_NETWORK_ENABLED[0] = bool(config.get("network_enabled", True))
    _ACTIVE_LSP_AUTOCHECK[0] = bool(config.get("lsp_autocheck", False))
    _ACTIVE_APPROVAL_MODE[0] = config.get("approval_mode", "manual")
    try:
        _ACTIVE_AUTO_APPROVE_LEVEL[0] = int(config.get("auto_approve_level", 1))
    except (TypeError, ValueError):
        _ACTIVE_AUTO_APPROVE_LEVEL[0] = 1


def _run_event_hook(event: str, env_extra: dict = None):
    """Compatibility wrapper for legacy call sites."""
    run_event_hook(event, config_dir=CONFIG_DIR, env_extra=env_extra)


def load_config() -> dict:
    return load_cli_config(_PATHS, DEFAULT_CONFIG, sync_policy=_sync_write_policy)


def save_config(cfg: dict):
    save_cli_config(_PATHS, cfg)


# ============================================================================
# Aria Tool Executor — calls /api/aria/execute-tool
# ============================================================================

# Populated in place — the container is owned by apps/cli/tool_registry.py.
# Rebinding it here instead would leave every reader over there with an
# empty registry and no error.
ARIA_TOOLS.extend([
    ("get_market_data",         "Stock quotes, prices, chart data"),
    ("get_crypto_data",         "Cryptocurrency market data"),
    ("get_forex_data",          "Foreign exchange rates"),
    ("get_commodities_data",    "Commodities prices (gold, oil, etc.)"),
    ("get_futures_data",        "Futures contract data"),
    ("get_bonds_data",          "Bond yields and data"),
    ("backtest_strategy",       "Run strategy backtesting"),
    ("calculate_factors",       "Calculate quantitative factors"),
    ("get_alpha158_factors",    "Alpha158 factor set"),
    ("get_risk_metrics",        "Risk metrics and VaR"),
    ("optimize_positions",      "Portfolio optimization"),
    ("stress_test_strategy",    "Strategy stress testing"),
    ("check_strategy_compliance", "Strategy compliance check"),
    ("recommend_strategy",      "AI strategy recommendation"),
    ("analyze_news",            "News sentiment analysis"),
    ("web_search",              "Web search for research"),
    ("get_world_bank_reports",  "World Bank economic reports"),
    ("generate_chart",          "Generate chart visualization"),
    ("generate_report",         "Generate analysis report"),
    ("assess_portfolio_risk",   "Portfolio risk assessment"),
    ("get_sector_performance",  "Sector performance heatmap"),
    ("get_market_indices",      "Global market indices"),
])


# ============================================================================
# Models Registry — like Claude Code model picker
# ============================================================================

# MODELS / MODEL_ALIASES / _MODEL_FALLBACK_PREFIXES 已移到
# apps/cli/model_catalog.py（纯数据，约 390 行）。同 skills_catalog，普通
# import 即可满足 mixin 的裸名引用。
from aria_code.apps.cli.model_catalog import (
    MODELS, MODEL_ALIASES, _MODEL_FALLBACK_PREFIXES,
    get_model_cfg, resolve_model_key,  # noqa: F401 — re-exported for existing callers
    is_provider_qualified, pick_best_installed_model,
)


# The selection logic moved to model_catalog beside the prefix table it reads —
# it is pure, and it was untestable here because importing this module pulls in
# the whole interactive CLI. The old body ended in `sorted(installed)[0]`; see
# model_catalog for what that did to a configured cloud model.
_pick_best_installed_model = pick_best_installed_model






# ── Response cache for stateless queries (TTL = 60s) ─────────────────────────
# Avoids sending the same market/concept query to Ollama multiple times
# in rapid succession (e.g., user retries or tab-completion tests).
#
# The implementation moved to apps/cli/providers/llm/response_cache.py so
# stream_ollama can import it instead of borrowing it from this module's
# globals — see that file's docstring. The private aliases stay because the
# globals-rebind below hands this namespace to the other extracted helpers.
from aria_code.apps.cli.providers.llm.response_cache import (
    RESPONSE_CACHE as _RESPONSE_CACHE,
    RESPONSE_CACHE_TTL as _RESPONSE_CACHE_TTL,
    cache_get as _cache_get,
    cache_key as _cache_key,
    cache_set as _cache_set,
)

# Implementations live in apps/cli/providers/llm/offline_responses.py, with
# the provider whose failure mode they describe. Re-exported under the private
# names because stream_ollama borrows them from this module's globals.
from aria_code.apps.cli.providers.llm.offline_responses import (  # noqa: E402
    is_simple_greeting as _is_simple_greeting,
    offline_greeting_response as _offline_greeting_response,
    ollama_unavailable_result as _ollama_unavailable_result,
)





THINKING_MODES = {
    "auto":     {"label": "Auto",     "description": "Let Aria decide when to think deeply"},
    "instant":  {"label": "Instant",  "description": "Fast responses, no extended thinking"},
    "thinking": {"label": "Thinking", "description": "Always show reasoning chain"},
}


# ============================================================================
# Skills System — Claude Code-style expandable prompt templates
# ============================================================================

# SKILLS 定义已移到 apps/cli/skills_catalog.py（纯数据，264 行）。
# 普通 import 即可：名字进入本模块命名空间，core_cmds / diagnostic_cmds 等
# mixin 的裸名引用照常解析，不需要 _rebind_module_function_globals（那是给
# 函数 __globals__ 用的，数据不涉及）。
from aria_code.apps.cli.skills_catalog import SKILLS



# ============================================================================
# Local Tool System — Claude Code-style file operations
# ============================================================================

import subprocess
import glob as glob_module
import re
import re as re_module
import difflib


def _is_safe_path(resolved: pathlib.Path) -> bool:
    """Return True if the resolved path is inside an allowed root directory.

    Allowed roots: home directory, /tmp, /var/folders (macOS temp).
    Blocks: /etc, /sys, /proc, /dev, and any path that resolves through a
    symlink to outside those roots (symlink traversal prevention).
    """
    return WorkspaceSecurity().is_safe_path(resolved)


from aria_code.apps.cli.tool_executor import *







































# Local tool registry: name → (handler, description, for display)
# Populated in place — the container is owned by apps/cli/tool_registry.py.
# Rebinding it here instead would leave every reader over there with an
# empty registry and no error.
from aria_code.runtime.processes import tool_process as _tool_process

LOCAL_TOOLS.update({
    # ── Core file tools ──────────────────────────────────────────────────────
    "read_file":      (_tool_read_file,      "Read a file's contents"),
    "analyze_file":   (_tool_analyze_file,   "Parse & analyze a local document/image (pdf/docx/xlsx/csv/json/image/…); images go to the vision model"),
    "write_file":     (_tool_write_file,     "Create or overwrite a file"),
    "edit_file":      (_tool_edit_file,      "Edit a file (find & replace)"),
    "multi_edit":     (_tool_multi_edit,     "Apply multiple find/replace edits to one file atomically"),
    "list_files":     (_tool_list_files,     "List files in a directory"),
    "list_dir":       (_tool_list_files,     "List files in a directory (alias for list_files)"),
    "update_todos":   (_tool_update_todos,   "Track multi-step task progress as a live checklist"),
    "search_code":    (_tool_search_code,    "Search for patterns in code (grep)"),
    "search":         (_tool_search_code,    "Search for patterns in code (alias for search_code)"),
    "run_command":    (_tool_run_command,    "Execute a shell command"),
    "process":        (_tool_process,        "Read, write to or stop a background command"),
    # These four had schemas in local_tool_schemas.py and no handler here,
    # which is the mirror of the earlier defect: a schema with nothing
    # behind it offers the model a tool that cannot run.
    "ask_user":       (_src_ask_user,        "Ask the user for clarification"),
    "lsp_hover":      (_tool_lsp_hover,      "Get hover documentation/type signature (LSP)"),
    "lsp_definition": (_tool_lsp_definition, "Find definition (LSP)"),
    "lsp_references": (_tool_lsp_references, "Find references (LSP)"),
    # ── Extended tools (Claude Code parity) ─────────────────────────────────
    "web_fetch":      (_tool_web_fetch,      "Fetch a URL and return page text"),
    "github":         (_tool_github,         "GitHub API/CLI: PRs, issues, diffs, search, git_status, commit_and_push (commits as Aria bot)"),
    "glob":           (_tool_glob,           "Fast glob file-pattern search"),
    "notebook_read":  (_tool_notebook_read,  "Read a Jupyter notebook (.ipynb)"),
    "notebook_edit":  (_tool_notebook_edit,  "Edit a cell in a Jupyter notebook"),
    # ── Market data ─────────────────────────────────────────────────────────
    "get_market_data": (_tool_get_market_data, "Fetch real-time quote + technical indicators for any stock/ETF/crypto"),
    "get_market_history": (_tool_get_market_history, "Fetch OHLC price history (compact summary + recent candles) for any stock/ETF/index/crypto"),
    # ── Broker account data ──────────────────────────────────────────────────
    "broker_query": (_tool_broker_query, "Query connected broker: account balance, positions, or orders"),
    "broker_order": (_tool_broker_order, "Propose a trade order — requires explicit user confirmation before execution"),
})

# ── Register subagent tools ──────────────────────────────────────────────────
try:
    from runtime.subagent import SUBAGENT_TOOLS, SUBAGENT_SCHEMAS
    LOCAL_TOOLS.update(SUBAGENT_TOOLS)
    logger.info("Registered %d subagent tools", len(SUBAGENT_TOOLS))
except Exception as _exc:
    logger.debug("Subagent tools init error: %s", _exc)
    SUBAGENT_SCHEMAS = []

# ── Register LSP diagnostics tool ─────────────────────────────────────────────
try:
    from runtime.lsp import LSP_TOOLS, LSP_SCHEMAS
    LOCAL_TOOLS.update(LSP_TOOLS)
    logger.info("Registered %d LSP tools", len(LSP_TOOLS))
except Exception as _exc:
    logger.debug("LSP tools init error: %s", _exc)
    LSP_SCHEMAS = []

# ── Register repo-map tools (symbol index + symbol lookup) ────────────────────
try:
    from aria_code.runtime.repo_map import REPO_MAP_TOOLS, REPO_MAP_SCHEMAS
    LOCAL_TOOLS.update(REPO_MAP_TOOLS)
    logger.info("Registered %d repo-map tools", len(REPO_MAP_TOOLS))
except Exception as _exc:
    logger.debug("Repo-map tools init error: %s", _exc)
    REPO_MAP_SCHEMAS: list = []  # type: ignore[no-redef]

# ── Register the project graph (impact analysis before editing) ──────────────
try:
    from aria_code.runtime.project_graph import PROJECT_GRAPH_TOOLS, PROJECT_GRAPH_SCHEMAS
    LOCAL_TOOLS.update(PROJECT_GRAPH_TOOLS)
except Exception as _exc:
    logger.debug("Project graph tools init error: %s", _exc)
    PROJECT_GRAPH_SCHEMAS: list = []  # type: ignore[no-redef]

# ── Register artifact publishing (model-facing canvas tool) ───────────────────
try:
    from aria_code.tools.artifact_tools import ARTIFACT_TOOLS, ARTIFACT_TOOL_SCHEMAS
    LOCAL_TOOLS.update(ARTIFACT_TOOLS)
    logger.info("Registered %d artifact tools", len(ARTIFACT_TOOLS))
except Exception as _exc:
    logger.debug("Artifact tools init error: %s", _exc)
    ARTIFACT_TOOL_SCHEMAS: list = []  # type: ignore[no-redef]

# ── Register computer-use tools (browser automation + desktop control) ──────
_HAS_COMPUTER_USE = False
try:
    from computer_use_tools import COMPUTER_USE_TOOLS, COMPUTER_USE_SCHEMAS as _CU_SCHEMAS
    LOCAL_TOOLS.update(COMPUTER_USE_TOOLS)
    _HAS_COMPUTER_USE = True
    logger.info("Registered %d computer-use tools", len(COMPUTER_USE_TOOLS))
except ImportError:
    _CU_SCHEMAS = []

# Pre-initialize so finance/plugin registrations can append schemas to it.
# The bulk static schemas are extended below; this empty list must exist first.
# Owned by apps/cli/tool_registry.py; filled in place below.

# ── Register local finance fallback tools (yfinance / akshare / ccxt) ──────
# These fill in for remote Aria tools when local_mode=True or backend offline.
if _HAS_LOCAL_FINANCE:
    try:
        _n_finance = register_local_finance_tools(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
        if _n_finance:
            logger.info("Registered %d local finance tools", _n_finance)
    except Exception as _exc:
        logger.debug("Local finance tools init error: %s", _exc)

# ── Register project plugin tools (aria_tools.py auto-discovery) ─────────
if _HAS_PLUGIN:
    try:
        _n_plugin, _plugin_path = register_plugin_tools(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
        if _n_plugin and _plugin_path:
            logger.info("Registered %d plugin tools from %s", _n_plugin, _plugin_path.name)
    except Exception as _exc:
        logger.debug("Plugin tool registration error: %s", _exc)

# ── Register spreadsheet deliverable tools (structured xlsx writer) ────────
try:
    from spreadsheet_tools import register_spreadsheet_tools as _reg_xlsx
    _n_xlsx = _reg_xlsx(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_xlsx:
        logger.info("Registered %d spreadsheet tools", _n_xlsx)
except Exception as _exc:
    logger.debug("Spreadsheet tools init error: %s", _exc)

# ── Register markdown→PDF deliverable tools (styled zh/en report render) ───
try:
    from markdown_pdf import register_markdown_pdf_tools as _reg_mdpdf
    _n_mdpdf = _reg_mdpdf(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_mdpdf:
        logger.info("Registered %d markdown-pdf tools", _n_mdpdf)
except Exception as _exc:
    logger.debug("Markdown-pdf tools init error: %s", _exc)

# ── Register real (non-financial) image generation tools ───────────────────
# Without this the chat loop can generate stock charts but has no tool for
# "generate me a real photo" — see image_gen_tools.py's module docstring.
try:
    from image_gen_tools import register_image_tools as _reg_image
    _n_image = _reg_image(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_image:
        logger.info("Registered %d image generation tools", _n_image)
except Exception as _exc:
    logger.debug("Image tool registration error: %s", _exc)
    
# ── Register enterprise logistics & corporate finance analysis tools ────────
try:
    from tools.logistics_tools import register_logistics_tools as _reg_logistics
    _n_logistics = _reg_logistics(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_logistics:
        logger.info("Registered %d logistics tools", _n_logistics)
except Exception as _exc:
    logger.debug("Logistics tools init error: %s", _exc)

try:
    from tools.enterprise_finance_tools import register_enterprise_finance_tools as _reg_corp_fin
    _n_corp_fin = _reg_corp_fin(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_corp_fin:
        logger.info("Registered %d enterprise finance tools", _n_corp_fin)
except Exception as _exc:
    logger.debug("Enterprise finance tools init error: %s", _exc)

try:
    from tools.stripe_tools import register_stripe_tools as _reg_stripe
    _n_stripe = _reg_stripe(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_stripe:
        logger.info("Registered %d stripe tools", _n_stripe)
except Exception as _exc:
    logger.debug("Stripe tools init error: %s", _exc)

try:
    from tools.broker_tools import register_broker_tools as _reg_broker
    _n_broker = _reg_broker(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    if _n_broker:
        logger.info("Registered %d broker tools", _n_broker)
except Exception as _exc:
    logger.debug("Broker tools init error: %s", _exc)

try:
    try:
        from aria_code.tools.extended_tools import register_extended_tools as _reg_ext
    except ImportError:
        from tools.extended_tools import register_extended_tools as _reg_ext  # type: ignore[no-redef]
    _reg_ext(LOCAL_TOOLS)
    logger.info("Registered extended enterprise tools (Slack, Feishu, TradingView, QuickBooks, Shopify, Snowflake)")
except Exception as _exc:
    logger.debug("Extended tools init error: %s", _exc)

try:
    try:
        from aria_code.tools.code_audit_tools import register_code_audit_tools as _reg_audit
    except ImportError:
        from tools.code_audit_tools import register_code_audit_tools as _reg_audit  # type: ignore[no-redef]
    # The schema list matters: these are real implementations that were
    # registered and callable but had no schema, so the model was never
    # offered them. Registering the handler alone hides a tool rather than
    # gating it.
    _reg_audit(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
    logger.info("Registered code audit & diff tools")
except Exception as _exc:
    logger.debug("Code audit tools init error: %s", _exc)

# Ollama tool schemas (for function calling) — extend so finance schemas added above are kept


# 20 个本地工具的 JSON Schema（437 行纯字面量）已移到
# apps/cli/local_tool_schemas.py。它们跟周围那些 `try: import / LOCAL_TOOLS.update`
# 的注册副作用不同——schema 只是数据，没有执行顺序依赖，混在一起容易让人
# 误以为它们同样敏感。
from aria_code.apps.cli.local_tool_schemas import build_local_tool_schemas
LOCAL_TOOL_SCHEMAS.extend(build_local_tool_schemas(
    todo_schema=_todo_schema(),
    config_dir=CONFIG_DIR,
))

# Append computer-use schemas if the module loaded successfully
if _HAS_COMPUTER_USE:
    LOCAL_TOOL_SCHEMAS.extend(_CU_SCHEMAS)




# Make spawn_task / task_* and lsp_diagnostics visible to the model.
LOCAL_TOOL_SCHEMAS.extend(_wrap_bare_schemas(SUBAGENT_SCHEMAS))
LOCAL_TOOL_SCHEMAS.extend(_wrap_bare_schemas(LSP_SCHEMAS))
# Without these two the tools are registered but have no schema, which
# hides them from the model instead of gating them — the same defect
# tests/test_tool_scope.py caught for the code-audit tools.
LOCAL_TOOL_SCHEMAS.extend(_wrap_bare_schemas(REPO_MAP_SCHEMAS))
LOCAL_TOOL_SCHEMAS.extend(_wrap_bare_schemas(PROJECT_GRAPH_SCHEMAS))
LOCAL_TOOL_SCHEMAS.extend(_wrap_bare_schemas(ARTIFACT_TOOL_SCHEMAS))




_dedup_tool_schemas()


# Tools that require user confirmation before execution
_CONFIRM_TOOLS = {"write_file", "edit_file", "multi_edit", "run_command"}
# Bot mode (ARIA_BOT_MODE=1) is about output only: plain text for a chat card.
# It used to start the session with every tool approved, and the Feishu and
# Telegram bots set it — so whoever could message a bot could run shell
# commands and write files on its host. Tools that need confirmation are now
# refused in bot runs (nobody in a chat can answer the prompt); a bot that
# needs one passes --allow-tools for exactly that tool.
_ARIA_BOT_MODE: bool = bool(os.environ.get("ARIA_BOT_MODE"))
_auto_approve_session: bool = False  # Set True when user chooses "Yes, allow all"

# Per-tool session allow list — populated by "Always allow [tool] this session" choice.
# More granular than _auto_approve_session: allows write_file without approving run_command.
_session_always_allow: set = set()

# Prefix-scoped run-command approvals, matching Codex's session rules.  A rule
# such as ("python3", "/path/report.py") is safer than approving every shell
# command while still preventing repeated prompts for the same workflow.
_session_command_prefixes: set[tuple[str, ...]] = set()

# Plan mode singleton — intercepts ALL tool calls for step-by-step approval
_PLAN_MODE = PlanModeState()

# Load JSON hooks once at startup; reloaded on demand via /hooks reload
try:
    from apps.cli.hooks import load_hooks as _load_hooks, fire as _fire_json_hook
    _JSON_HOOKS: dict = _load_hooks()
    _HAS_JSON_HOOKS = True
except Exception:
    _JSON_HOOKS = {}
    _HAS_JSON_HOOKS = False









def _command_approval_prefix(command: str) -> tuple[str, ...]:
    """Return a useful, bounded session prefix for one shell command."""
    import shlex as _approval_shlex

    try:
        parts = tuple(_approval_shlex.split(str(command or "").strip()))
    except ValueError:
        return ()
    if not parts:
        return ()
    executable = pathlib.Path(parts[0]).name.lower()
    if executable.startswith("python"):
        if len(parts) >= 3 and parts[1] == "-m":
            return parts[:3]
        if len(parts) >= 2 and parts[1].endswith(".py"):
            return parts[:2]
    if executable in {"pip", "pip3"} and len(parts) >= 2 and parts[1] == "install":
        return parts[:2]
    if executable in {"npm", "pnpm", "yarn"} and len(parts) >= 3 and parts[1] == "run":
        return parts[:3]
    return parts


def _command_matches_session_prefix(command: str) -> bool:
    import shlex as _approval_shlex

    try:
        parts = tuple(_approval_shlex.split(str(command or "").strip()))
    except ValueError:
        return False
    return any(parts[:len(prefix)] == prefix for prefix in _session_command_prefixes if prefix)






def execute_local_tool(tool_name: str, params: dict) -> dict:
    """Execute a local tool by name."""
    executor = ToolExecutor(
        LOCAL_TOOLS,
        hook=_run_hook,
        config={
            "command_policy": _ACTIVE_COMMAND_POLICY[0],
            "permission_mode": _ACTIVE_PERMISSION_MODE[0],
            "network_enabled": _ACTIVE_NETWORK_ENABLED[0],
        },
    )
    return executor.execute_local(tool_name, params)


def _run_hook(hook_type: str, tool_name: str, params: dict, result: dict = None) -> None:
    """Fire-and-forget hook execution from .ariarc hooks config.

    hook_type: "pre_tool" | "post_tool" | "on_error"
    Hooks are shell commands with {key} template substitution from params/result.

    Example .ariarc:
      "hooks": {
        "pre_tool":  {"write_file": "echo 'Writing: {path}'"},
        "post_tool": {"run_command": "notify-send 'Done'"},
        "on_error":  "echo 'Error: {error}'"
      }
    """
    if not _HAS_ARIARC:
        return
    try:
        _arc = get_ariarc()
        hooks = _arc.data.get("hooks", {}) if hasattr(_arc, "data") else {}
        if not hooks:
            return
        hook_spec = hooks.get(hook_type, {})
        # hook_spec can be: dict keyed by tool_name, or bare string for all tools
        if isinstance(hook_spec, dict):
            cmd = hook_spec.get(tool_name) or hook_spec.get("*")
        else:
            cmd = hook_spec  # bare string applies to all tools
        if not cmd:
            return
        # Template substitution: {path}, {command}, {error}, etc.
        fmt_ctx: Dict[str, str] = {k: str(v) for k, v in (params or {}).items()}
        if result:
            fmt_ctx["error"] = str(result.get("error", ""))
            fmt_ctx["success"] = str(result.get("success", ""))
        try:
            cmd = cmd.format_map(fmt_ctx)
        except (KeyError, ValueError):
            pass  # Ignore missing keys in template
        import subprocess as _sp
        _sp.run(cmd, shell=True, timeout=5, capture_output=True)
    except Exception:
        pass  # Hooks must never crash the main flow

    # Also fire JSON hooks (PostToolUse / PreToolUse)
    if _HAS_JSON_HOOKS:
        try:
            _event = "PostToolUse" if hook_type == "post_tool" else (
                "PreToolUse" if hook_type == "pre_tool" else None
            )
            if _event:
                _fire_json_hook(
                    _event, tool=tool_name, params=params, result=result,
                    hooks=_JSON_HOOKS,
                )
        except Exception:
            pass


# TTL cache for read-only tool responses
# Owned by apps/cli/tool_executor.py, which is where execute_aria_tool lives.
# Defining a second pair here meant the rebound copy cached into this module's
# dict while the original cached into that one — two caches, and whichever
# module a caller reached decided which it got.
from aria_code.apps.cli.tool_executor import (  # noqa: F401
    _CACHE_TTL,
    _TOOL_CACHE,
)




# ============================================================================
# Ollama Local Client (fallback when AWS unavailable)
# ============================================================================

from aria_code.apps.cli.prompts.coding import CODING_SYSTEM_PROMPT  # noqa: F401 — extracted


def _detect_lang(text: str) -> str:
    """Thin shim — implementation in apps/cli/prompts/system_prompts.py."""
    from apps.cli.prompts.system_prompts import detect_lang as _f
    return _f(text)


from aria_code.apps.cli.prompts.system_prompts import LANG_RULE as _LANG_RULE


# Aliased, not wrapped: a wrapper is a different function object from what it
# calls, so stream_ollama importing the real builders while this module kept
# wrappers would run two different objects on the CLI and fallback paths.
from aria_code.apps.cli.prompts.system_prompts import (  # noqa: E402
    build_analysis_prompt_lite as _build_analysis_prompt_lite,
    build_analysis_system_prompt as _build_analysis_system_prompt,
    build_coding_prompt_lite as _build_coding_prompt_lite,
    build_finance_prompt as _build_finance_prompt,
    build_prefetched_analysis_prompt as _build_prefetched_analysis_prompt,
)


# NOTE: FINANCE_CHAT_PROMPT is a function now — it injects the current date dynamically.


FINANCE_CHAT_PROMPT = _build_finance_prompt()  # evaluated once at import; rebuilt per stream call

# ============================================================================
# ANALYSIS_SYSTEM_PROMPT: for stock/crypto/macro analysis queries that need
# real data via tool calls but don't require writing Python scripts
# ============================================================================



ANALYSIS_SYSTEM_PROMPT = _build_analysis_system_prompt()


# ── LaTeX → plain-text converter ────────────────────────────────────────────
import re as _re_latex

# Delegate to the canonical formula renderer when available
try:
    from finance_formulas import (
        FORMULA_PROMPT_BLOCK_CORE as _FORMULA_PROMPT_BLOCK_CORE,
        strip_latex_for_cli as _strip_latex_impl,
    )
    _HAS_FORMULA_LIB = True
except ImportError:
    _HAS_FORMULA_LIB = False
    _FORMULA_PROMPT_BLOCK_CORE = ""
    _strip_latex_impl = None  # type: ignore


def _strip_latex(text: str) -> str:
    """Convert LaTeX math notation to readable plain-text for terminal display.

    Delegates to finance_formulas.strip_latex_for_cli when available (preferred).
    Falls back to the legacy inline implementation otherwise.
    """
    if "\\" not in text and "$" not in text:
        return text

    if _HAS_FORMULA_LIB and _strip_latex_impl is not None:
        return _strip_latex_impl(text)

    # ── Legacy fallback (finance_formulas not importable) ───────────────────

    # Display-math blocks: \[ ... \] → ▶ prefix
    text = _re_latex.sub(r'\\\[\s*',   '\n  ▶ ', text)
    text = _re_latex.sub(r'\s*\\\]',   '\n',      text)
    text = _re_latex.sub(
        r'\$\$(.+?)\$\$',
        lambda m: '\n  ▶ ' + m.group(1).strip() + '\n',
        text, flags=_re_latex.DOTALL,
    )

    # Common math symbols — simple string replace (no regex needed)
    # Key: actual backslash + command name (Python string '\\sum' = \sum)
    _SYM = {
        '\\sum':'Σ', '\\prod':'Π', '\\int':'∫', '\\infty':'∞',
        '\\alpha':'α', '\\beta':'β', '\\gamma':'γ', '\\delta':'δ',
        '\\theta':'θ', '\\lambda':'λ', '\\mu':'μ', '\\sigma':'σ',
        '\\tau':'τ', '\\phi':'φ', '\\psi':'ψ', '\\omega':'ω',
        '\\pi':'π', '\\rho':'ρ', '\\epsilon':'ε',
        '\\times':'×', '\\cdot':'·', '\\pm':'±',
        '\\leq':'≤', '\\geq':'≥', '\\neq':'≠', '\\approx':'≈',
        '\\to':'→', '\\Rightarrow':'⇒', '\\partial':'∂',
        '\\forall':'∀', '\\exists':'∃', '\\in':'∈', '\\notin':'∉',
        '\\cup':'∪', '\\cap':'∩', '\\subset':'⊂',
        '\\ldots':'…', '\\cdots':'…', '\\left':'', '\\right':'',
        # LaTeX spacing commands — ';,:,!' are NOT caught by \\[A-Za-z]+ regex
        '\\;':' ', '\\,':'', '\\:':' ', '\\!':'',
        '\\quad':'  ', '\\qquad':'   ',
    }
    for cmd, sym in _SYM.items():
        text = text.replace(cmd, sym)

    # \text{X} \mathbf{X} \mathrm{X} \hat{X} etc → X
    # Use a single pattern that matches any \word{...}
    text = _re_latex.sub(
        r'\\(?:text|mathbf|mathrm|mathit|mathcal|boldsymbol|hat|bar|tilde|vec|overline|underline)\{([^{}]*)\}',
        r'\1', text,
    )

    # \frac{a}{b} → (a)/(b)
    for _ in range(3):   # handle nested fracs up to 3 deep
        text = _re_latex.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'(\1)/(\2)', text)
    # \sqrt{x} → √(x)
    text = _re_latex.sub(r'\\sqrt\{([^{}]*)\}', r'√(\1)', text)

    # ^{exp} → ^exp,   _{sub} → _sub
    text = _re_latex.sub(r'\^\{([^{}]{1,10})\}', r'^\1', text)
    text = _re_latex.sub(r'_\{([^{}]{1,10})\}',  r'_\1', text)

    # Non-alpha spacing commands: \; \, \: \! — not caught by \\[A-Za-z]+
    text = _re_latex.sub(r'\\[;,!:]', ' ', text)

    # Strip any remaining \command  (bare backslash commands with no braces)
    text = _re_latex.sub(r'\\([A-Za-z]+)', r'\1', text)

    # Inline math: $...$  → content only
    text = _re_latex.sub(r'\$([^$\n]{1,80})\$', r'\1', text)

    # Remove lone parens around single chars created by frac simplification
    text = _re_latex.sub(r'\(([A-Za-z0-9_^])\)/\(([A-Za-z0-9_^])\)', r'\1/\2', text)

    return text


# Intent classification — thin shims over apps/cli/intent.py
from aria_code.apps.cli.intent import (
    CODING_KEYWORDS as _CODING_KEYWORDS,
    ANALYSIS_KEYWORDS as _ANALYSIS_KEYWORDS,
    ANALYSIS_NON_STOCK_TOPICS as _ANALYSIS_NON_STOCK_TOPICS,
    GENERAL_KNOWLEDGE_KEYWORDS as _GENERAL_KNOWLEDGE_KEYWORDS,
    FINANCE_CONCEPT_TERMS as _FINANCE_CONCEPT_TERMS,
    SPORTS_KEYWORDS as _SPORTS_KEYWORDS,
    is_coding_request as _is_coding_request,
    is_analysis_request as _is_analysis_request,
    is_general_knowledge as _is_general_knowledge,
    is_sports_query as _is_sports_query,
)


from aria_code.apps.cli.handlers.sports_handlers import (  # noqa: E402
    try_prefetch_sports_data as _try_prefetch_sports_data,
)




def _refresh_project_context() -> str:
    """Re-scan for ARIA.md (call at session start or /reload)."""
    global _PROJECT_CONTEXT
    _PROJECT_CONTEXT = _load_project_context()
    return _PROJECT_CONTEXT


# Cache project context at module level (refreshed per session)
_PROJECT_CONTEXT = _load_project_context()


def _fix_json_string(raw: str) -> str:
    """Fix common JSON issues from LLM output (triple quotes, unescaped newlines)."""
    # Fix Python triple-quoted strings: """...""" → proper JSON string
    triple_pattern = re_module.compile(r'"""\s*\n([\s\S]*?)"""')
    def _replace_triple(m):
        content = m.group(1)
        # Escape for JSON: backslashes, quotes, newlines
        content = content.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return '"' + content + '"'
    fixed = triple_pattern.sub(_replace_triple, raw)
    return fixed


# Aliased rather than wrapped. These were three one-line shims delegating to
# apps/cli/message_processing.py; a shim is a *different function object* from
# the thing it calls, so stream_ollama importing the real functions while this
# module held wrappers made the CLI and fallback paths run different objects —
# caught by tests/test_ollama_stream_imports.py. Signatures are identical, so
# the alias is an exact replacement.
from aria_code.apps.cli.message_processing import (  # noqa: E402
    compact_messages as _compact_messages,
    parse_text_tool_calls as _parse_text_tool_calls,
    strip_tool_call_tags as _strip_tool_call_tags,
)




from aria_code.apps.cli.message_processing import (  # noqa: E402
    build_broker_context_block as _build_broker_context_block,
)






_try_prefetch_market_data = _src_prefetch_market_data  # alias, not a wrapper


import re as _re_fi

# Matches absolute/relative paths and bare filenames with known extensions.
# Single capturing group so findall always returns the full matched path string.
_FILE_PATH_RE = _re_fi.compile(
    r'('
    r'(?:~/|\.{1,2}/|/(?:Users|home|workspace|tmp|private/tmp|var|private/var)/)\S+'  # abs/rel paths
    r'|'
    r'(?<!\w)[\w./-]{3,}\.(?:py|js|ts|json|yaml|yml|md|txt|csv|toml|sh|cfg|ini|env|log'
    r'|docx|xlsx|pptx|pdf)(?!\w)'  # bare filenames (incl. Office docs)
    r')'
)
def _build_file_tool_hint(message: str) -> str:
    """Build resource pointers for file tools without reading file contents."""
    raw_matches = _FILE_PATH_RE.findall(message)
    candidates = [m for m in raw_matches if m]
    if not candidates:
        return ""
    references: list[str] = []
    seen: set = set()
    for raw in candidates[:6]:
        raw = raw.strip().rstrip("，,。.）)")
        # Unescape shell-escaped parentheses: \( → (  \) → )
        raw = raw.replace("\\(", "(").replace("\\)", ")")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            p = pathlib.Path(raw).expanduser().resolve()
        except Exception:
            continue
        if not p.exists():
            continue
        try:
            if not _is_safe_path(p):
                continue
        except Exception:
            continue
        if p.is_dir():
            references.append(f"- folder: {p} (use list_files or search_code)")
        elif p.suffix.lower() in {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".mov", ".mp4"}:
            references.append(f"- file: {p} (use analyze_file)")
        else:
            references.append(f"- file: {p} (use read_file)")
    if not references:
        return ""
    return (
        "[Referenced local resources - pointers only]\n"
        "No file content is preloaded. Call the indicated tools before answering.\n"
        + "\n".join(references)
        + "\n[End referenced local resources]\n\n"
    )


_STOCK_ANALYSIS_INTENT_KW = frozenset({
    "怎么样", "分析", "预测", "看多", "看空", "建议", "机会", "涨", "跌",
    "值得买", "行情", "走势", "趋势", "布局", "操作", "仓位", "支撑", "压力",
    "analyze", "predict", "outlook", "opinion", "bullish", "bearish",
    "target", "buy", "sell", "hold", "forecast",
})

def _is_stock_analysis_intent(message: str) -> bool:
    """Return True if the message is asking for stock/market opinion or analysis."""
    low = message.lower()
    return any(kw in low for kw in _STOCK_ANALYSIS_INTENT_KW)


def _fetch_quick_ml_signal(symbols: list, timeout: float = 3.0) -> str:
    """Fetch ML 5-day predictions for up to 3 symbols; return a compact signal string or ''."""
    import concurrent.futures as _fut
    try:
        from local_finance_tools import _get_predictions
        with _fut.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_get_predictions, {"symbols": symbols[:3], "prediction_days": 5})
            r = fut.result(timeout=timeout)
        if not r.get("success"):
            return ""
        lines = []
        for p in r.get("predictions", []):
            sym   = p.get("symbol", "")
            ret   = float(p.get("predicted_return") or 0)
            conf  = float(p.get("confidence") or 0.5)
            arrow = "↑" if ret > 0 else "↓"
            lines.append(
                f"{sym}: 预计5日{arrow}{abs(ret)*100:.1f}%  置信度{conf:.0%}  "
                f"({'云端LightGBM' if r.get('provider')=='aliyun_cloud' else '动量因子'})"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def _check_memory_trigger(text: str) -> Optional[str]:
    """Detect memory-saving trigger phrases in the first 60 chars of the message.

    Returns the fact string to save, or None if no trigger is found.
    This powers the auto-memory feature: "记住：QQQ是我们的核心仓位" → appended to ARIA.md.
    """
    _TRIGGERS = (
        "记住：", "记住:", "记住 ",
        "remember that ", "note that ", "please note ", "don't forget ",
        "请记住：", "请记住:", "请记住 ",
    )
    low = text.lower()
    prefix = low[:60]
    for t in _TRIGGERS:
        if t in prefix:
            idx = text.lower().index(t) + len(t)
            fact = text[idx:].strip().lstrip(":： ").strip()
            return fact if fact else None
    return None


# Financial/analytical terms that look like tickers but are NOT stock symbols.
# Prevents the regex from matching "DCF", "EPS", "RSI", etc. as ticker codes.
























# _fetch_snapshot_row_for_symbol is now in apps.cli.handlers.market_handlers
# (kept as local alias for any direct callers in this file)
from aria_code.apps.cli.handlers.market_handlers import _fetch_snapshot_row_for_symbol  # noqa


def _try_handle_multi_market_snapshot(message: str, symbols: list) -> dict:
    """Thin wrapper — real implementation in apps.cli.handlers.market_handlers."""
    return _src_multi_snapshot(message, symbols)


def _try_handle_realty_query(message: str) -> dict:
    return _src_handle_realty_query(
        message,
        is_realty_query=_is_realty_query,
        cn_cities=_CN_CITIES,
        intl_cities=_INTL_CITIES,
    )


def _try_handle_market_snapshot_analysis(message: str, history: list = None, *,
                                         evidence_required: bool = False) -> dict:
    """Thin wrapper — real implementation in apps.cli.handlers.market_handlers."""
    return _src_market_snapshot_analysis(message, history, evidence_required=evidence_required)


def _try_handle_market_overview(message: str) -> dict:
    """Thin wrapper — real implementation in apps.cli.handlers.market_handlers."""
    return _src_market_overview(message)


def _try_handle_strategy_advice(message: str) -> dict:
    return _src_strategy_advice(message)


def _run_deterministic_chain(message: str, *, model_has_tools: bool,
                             history: list = None) -> dict:
    """Thin wrapper around the SDK-safe deterministic router."""
    from apps.cli.deterministic import run_deterministic_chain

    return run_deterministic_chain(
        message,
        model_has_tools=model_has_tools,
        history=history,
        has_brokers=_HAS_BROKERS,
        get_broker_registry=_get_broker_registry,
    )


def _fmt_int(value) -> str:
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return "N/A"




def _recover_repetition_stopped_text(text: str) -> str:
    marker = "*[model stopped — repetition detected]*"
    if marker not in (text or ""):
        return text
    clean = text.split(marker, 1)[0].rstrip()
    # A model often starts a Markdown table immediately before entering a
    # repetition loop.  A header plus separator without any data row is not a
    # useful partial result and renders as raw pipes in narrow terminals.
    lines = clean.splitlines()
    if len(lines) >= 2:
        separator = lines[-1].strip().strip("|").split("|")
        if (
            lines[-2].strip().startswith("|")
            and separator
            and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell or "") for cell in separator)
        ):
            lines = lines[:-2]
            clean = "\n".join(lines).rstrip()
    if clean.count("```") % 2 == 1:
        clean += "\n```"
    note = (
        "\n\n> 已检测到模型开始重复输出，已自动停止展开。"
        "未完成的尾部已隐藏；成功的工具结果和已生成文件仍然有效。"
    )
    return (clean + note).strip()


def _is_market_snapshot_refresh_request(message: str) -> bool:
    low = (message or "").lower()
    return any(k in low for k in ("刷新", "重试", "重新", "更新", "强制", "refresh", "retry", "reload", "force"))


def _market_snapshot_cache_entry(result: dict, now: float | None = None) -> dict | None:
    symbol = str(result.get("symbol") or "").strip().upper()
    price = result.get("price")
    if not symbol or price in (None, "", "N/A", "—"):
        return None
    try:
        price_key = round(float(price), 4)
    except Exception:
        return None
    change = result.get("change_pct")
    try:
        change_key = round(float(change), 4) if change is not None else None
    except Exception:
        change_key = None
    signature = (
        symbol,
        price_key,
        change_key,
        str(result.get("signal") or ""),
        str(result.get("support") or ""),
        str(result.get("resistance") or ""),
    )
    return {
        "ts": time.time() if now is None else now,
        "signature": signature,
        "symbol": symbol,
        "name": result.get("name") or symbol,
        "currency": result.get("currency") or "USD",
        "price": price_key,
        "change_pct": change_key,
        "signal": result.get("signal") or "—",
        "support": result.get("support") or "—",
        "resistance": result.get("resistance") or "—",
        "as_of": result.get("as_of") or datetime.now().strftime("%Y-%m-%d"),
    }


def _build_market_snapshot_repeat_notice(
    result: dict,
    previous: dict | None,
    *,
    now: float | None = None,
    ttl_seconds: int = 60,
    lang: str = "zh",
) -> str:
    current = _market_snapshot_cache_entry(result, now=now)
    if not current or not previous:
        return ""
    now_ts = time.time() if now is None else now
    if now_ts - float(previous.get("ts") or 0) > ttl_seconds:
        return ""
    if current.get("signature") != previous.get("signature"):
        return ""

    symbol = current["symbol"]
    name = current.get("name") or symbol
    currency = current.get("currency") or "USD"
    change = current.get("change_pct")
    change_text = "—" if change is None else f"{float(change):+.2f}%"
    if lang == "en":
        return "\n".join([
            f"## {name}  `{symbol}`",
            "",
            f"**Unchanged**: you asked within the last {ttl_seconds} seconds and the price, signal "
            "and key levels match the previous answer, so the full tables are left out.",
            "",
            f"- Last price: **{currency} {float(current['price']):,.2f}**  `{change_text}`",
            f"- Signal: `{current.get('signal') or '—'}`",
            f"- Support: {current.get('support') or '—'}",
            f"- Resistance: {current.get('resistance') or '—'}",
            f"- As of: {current.get('as_of') or '—'}",
            "",
            "**Next steps**",
            f"- Full snapshot: `/quote {symbol}`",
            f"- Deep analysis: `/team {symbol}`",
            f"- Chart: `/ta {symbol}`",
        ])
    return "\n".join([
        f"## {name}  `{symbol}`",
        "",
        f"**行情未变化**：过去 {ttl_seconds} 秒内已查询过，价格、信号和关键位与上一条一致，已省略完整表格。",
        "",
        f"- 最新价：**{currency} {float(current['price']):,.2f}**  `{change_text}`",
        f"- 信号：`{current.get('signal') or '—'}`",
        f"- 支撑：{current.get('support') or '—'}",
        f"- 阻力：{current.get('resistance') or '—'}",
        f"- 数据日期：{current.get('as_of') or '—'}",
        "",
        "**下一步**",
        f"- 查看完整快照：`/quote {symbol}`",
        f"- 深度分析：`/team {symbol}`",
        f"- 打开图表：`/ta {symbol}`",
    ])


def _fmt_tv_num(value: Any, digits: int = 2) -> str:
    try:
        if value in (None, "", "N/A", "—"):
            return "—"
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "—"


def _fmt_tv_money(currency: str, value: Any) -> str:
    num = _fmt_tv_num(value, 2)
    return "—" if num == "—" else f"{currency} {num}"


def _build_tradingview_indicator_readout(snapshot: dict, *, tv_symbol: str = "", mode: str = "analyze") -> str:
    """Build a compact analysis block after opening TradingView.

    TradingView is only used as the chart surface here.  The indicator values
    come from Aria's market snapshot pipeline so the user does not get the
    false impression that we scraped the TradingView chart.
    """
    symbol = str(snapshot.get("symbol") or "").strip().upper() or "AAPL"
    name = snapshot.get("name") or symbol
    currency = snapshot.get("currency") or "USD"
    price = snapshot.get("price")
    change = snapshot.get("change_pct")
    rsi = snapshot.get("rsi")
    macd_hist = snapshot.get("macd_hist")
    ma20 = snapshot.get("ma20")
    ma60 = snapshot.get("ma60")
    signal = snapshot.get("signal") or "—"
    confidence = snapshot.get("signal_confidence")
    supports = snapshot.get("supports") or []
    resistances = snapshot.get("resistances") or []
    support = supports[0] if supports else None
    resistance = resistances[0] if resistances else None

    bullish: list[str] = []
    bearish: list[str] = []
    confirm: list[str] = []

    try:
        price_f = float(price)
    except Exception:
        price_f = None
    try:
        chg_f = float(change)
    except Exception:
        chg_f = None
    try:
        rsi_f = float(rsi)
    except Exception:
        rsi_f = None
    try:
        macd_f = float(macd_hist)
    except Exception:
        macd_f = None
    try:
        ma20_f = float(ma20)
    except Exception:
        ma20_f = None
    try:
        ma60_f = float(ma60)
    except Exception:
        ma60_f = None

    if chg_f is not None:
        if chg_f > 0:
            bullish.append(f"当日涨幅 `{chg_f:+.2f}%`，短线有回升迹象。")
        elif chg_f < 0:
            bearish.append(f"当日跌幅 `{chg_f:+.2f}%`，短线承压。")
    if price_f is not None and ma20_f is not None:
        if price_f > ma20_f:
            bullish.append(f"价格高于 MA20（{_fmt_tv_money(currency, ma20_f)}），短线趋势偏强。")
        else:
            bearish.append(f"价格低于 MA20（{_fmt_tv_money(currency, ma20_f)}），短线仍受压。")
    if price_f is not None and ma60_f is not None:
        if price_f > ma60_f:
            bullish.append(f"价格高于 MA60（{_fmt_tv_money(currency, ma60_f)}），中期支撑仍在。")
        else:
            bearish.append(f"价格低于 MA60（{_fmt_tv_money(currency, ma60_f)}），中期趋势偏弱。")
    if macd_f is not None:
        if macd_f > 0:
            bullish.append(f"MACD hist `{macd_f:.4f}` 为正，多头动能占优。")
        elif macd_f < 0:
            bearish.append(f"MACD hist `{macd_f:.4f}` 为负，动能仍偏空。")
    if rsi_f is not None:
        if 30 < rsi_f < 70:
            bullish.append(f"RSI `{rsi_f:.1f}` 未超买，若价格企稳仍有反弹空间。")
        elif rsi_f <= 30:
            bullish.append(f"RSI `{rsi_f:.1f}` 接近/进入超卖，可能出现技术反弹。")
        elif rsi_f >= 70:
            bearish.append(f"RSI `{rsi_f:.1f}` 接近/进入超买，追高风险上升。")

    if resistance is not None:
        confirm.append(f"上破 `{_fmt_tv_money(currency, resistance)}`，看涨信号才更可靠。")
    if support is not None:
        confirm.append(f"跌破 `{_fmt_tv_money(currency, support)}`，下行风险会放大。")
    if not confirm:
        confirm.append("当前数据不足以给出明确确认位，建议先看完整技术图表。")

    positive_signals = {"STRONG_BUY", "BUY", "HOLD+"}
    negative_signals = {"STRONG_SELL", "SELL", "HOLD−", "HOLD-"}
    if signal in positive_signals:
        verdict = "偏多，但仍需确认突破是否有效。"
    elif signal in negative_signals:
        verdict = "偏弱，暂不适合直接按看涨处理。"
    else:
        verdict = "不是明确看涨，更接近震荡观察；需要价格重新站上关键阻力后再确认。"

    if mode == "bullish":
        lead_title = "看涨数据"
        lead_items = bullish or ["当前快照没有明显看涨证据。"]
        secondary_title = "看跌/需要确认"
        secondary_items = bearish or ["暂无明显看跌信号，但仍需等待突破确认。"]
    elif mode == "bearish":
        lead_title = "看跌数据"
        lead_items = bearish or ["当前快照没有明显看跌证据。"]
        secondary_title = "看涨/支撑因素"
        secondary_items = bullish or ["暂无明显看涨支撑。"]
    else:
        lead_title = "看涨数据"
        lead_items = bullish or ["当前快照没有明显看涨证据。"]
        secondary_title = "看跌/需要确认"
        secondary_items = bearish or ["暂无明显看跌信号，但仍需等待突破确认。"]

    confidence_text = ""
    try:
        confidence_text = f" · 置信度 {float(confidence):.0%}"
    except Exception:
        pass

    lines = [
        "",
        f"### TradingView 旁路分析  `{symbol}`",
        f"*TradingView 已作为图表界面打开；以下分析基于 Aria 当前行情与技术指标，不直接抓取 TradingView 页面数据。*",
        "",
        f"**结论**：{verdict} 当前信号 `{signal}`{confidence_text}。",
        f"**现价**：{_fmt_tv_money(currency, price)}  `{float(chg_f):+.2f}%`" if chg_f is not None else f"**现价**：{_fmt_tv_money(currency, price)}",
        "",
        f"**{lead_title}**",
    ]
    lines.extend(f"- {item}" for item in lead_items[:4])
    lines += ["", f"**{secondary_title}**"]
    lines.extend(f"- {item}" for item in secondary_items[:4])
    lines += ["", "**确认条件**"]
    lines.extend(f"- {item}" for item in confirm[:3])
    lines += [
        "",
        "**下一步**",
        f"- 图表细看：`/ta {symbol}`",
        f"- 深度分析：`/team {symbol}`",
    ]
    if tv_symbol:
        lines.append(f"- TradingView 标的：`{tv_symbol}`")
    return "\n".join(lines)




def _generate_chart_sync(symbol: str, period: str = "1y") -> dict:
    """
    `/chart` 命令的同步入口：为指定 symbol 生成 HTML 分析图表。
    A股代码先尝试 tushare/akshare 获取数据，美股走 yfinance。
    """
    # 对 A股代码做格式转换（600519 → 600519.SS / 000001 → 000001.SZ）
    sym_yf = symbol
    if re.match(r"^\d{6}$", symbol):
        if symbol.startswith(("6", "9")):
            sym_yf = symbol + ".SS"
        else:
            sym_yf = symbol + ".SZ"

    return _try_handle_stock_chart_analysis_direct(sym_yf, period=period)


def _resolve_market_arg_symbol(raw: str) -> str:
    """Resolve a command argument to a chart/quote-friendly market symbol."""
    token = str(raw or "").strip().strip(",，")
    upper = token.upper()
    if not upper:
        return ""
    if _is_blocked_market_symbol_candidate(upper):
        return ""
    if re.match(r"^\^[A-Z0-9]{2,12}$", upper):
        return upper
    if re.match(r"^[A-Z0-9]{1,12}=F$", upper) or re.match(r"^[A-Z]{6}=X$", upper):
        return upper
    if re.match(r"^[A-Z0-9]{1,12}\.[A-Z]{1,4}$", upper):
        return upper
    return _extract_market_symbol(token) or _extract_market_symbol(upper) or upper






def _is_market_artifact_followup(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text or text.startswith("/"):
        return False
    return any(k in text for k in (
        "继续以上", "继续上面", "继续这个", "继续任务", "继续",
        "直接运行", "那你直接运行", "开始运行", "帮我运行",
        "执行", "生成吧", "开始生成", "直接生成", "跑一下",
        "continue", "run it", "execute", "go ahead",
    ))


def _is_artifact_location_followup(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text or text.startswith("/"):
        return False
    return any(k in text for k in (
        "文件在哪", "文件在哪里", "保存在哪", "保存到哪", "保存到哪里",
        "路径", "生成的文件", "刚才的文件",
        "那文件在哪", "图在哪", "图表在哪",
        "where is the file", "where did you save", "saved file", "file path",
    ))


def _is_artifact_action_followup(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text or text.startswith("/"):
        return False
    return any(k in text for k in (
        "打不开", "打开不了", "无法打开", "怎么打开", "打开这文件", "打开这个文件",
        "打开文件", "打开它", "打开图表", "在访达", "所在目录", "复制", "复制内容",
        "粘贴", "剪贴板",
        "can't open", "cannot open", "open it", "open file", "reveal",
        "show in finder", "copy", "clipboard",
    ))


def _artifact_primary_path(pending_artifact: dict) -> str:
    for key in ("pine_path", "path", "html_path", "png_path", "chart_path", "raw_path"):
        value = str(pending_artifact.get(key) or "").strip()
        if value:
            return value
    return ""








def _write_text_companion(path: str) -> str:
    try:
        p = pathlib.Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return ""
        companion = p.with_suffix(".txt")
        companion.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        return str(companion)
    except Exception:
        return ""


def _handle_pending_artifact_action(pending_artifact: dict, message: str) -> str:
    kind = str(pending_artifact.get("kind") or "artifact")
    path = _artifact_primary_path(pending_artifact)
    url = str(pending_artifact.get("url") or "").strip()
    text = (message or "").lower()
    is_pine = kind == "tradingview_pine_strategy" or path.endswith(".pine")
    lines: list[str] = []

    if is_pine:
        lines.append("TradingView Pine Script 是源码文件，TradingView 网页不能直接打开本地 `.pine` 文件。")
        if path:
            lines.append(f"文件路径: `{path}`")
            companion = ""
            if any(k in text for k in ("打不开", "打开不了", "无法打开", "can't open", "cannot open")):
                companion = _write_text_companion(path)
                if companion:
                    lines.append(f"已生成可用文本副本: `{companion}`")
            try:
                pine_text = pathlib.Path(path).expanduser().read_text(encoding="utf-8")
                copied, copy_err = _copy_text_to_clipboard(pine_text)
                if copied:
                    lines.append("已把 Pine Script 内容复制到剪贴板。")
                else:
                    lines.append(f"剪贴板不可用: {copy_err}")
            except Exception as exc:
                lines.append(f"读取 Pine 文件失败: {exc}")
            revealed, reveal_err = _reveal_path_in_finder(path)
            if revealed:
                lines.append("已在 Finder 中定位该文件。")
            else:
                lines.append(f"Finder 定位失败: {reveal_err}")
        if url:
            opened, open_err = _open_path_or_url(url)
            if opened:
                lines.append("已打开 TradingView 图表页面。")
            else:
                lines.append(f"TradingView 打开失败: {open_err}")
        lines.append("使用方式: 打开 TradingView 图表 -> 底部 Pine Editor -> 粘贴 -> Save -> Add to chart。")
    elif path:
        opened, open_err = _open_path_or_url(path)
        if opened:
            lines.append(f"已尝试打开: `{path}`")
        else:
            lines.append(f"打开失败: {open_err}")
            revealed, reveal_err = _reveal_path_in_finder(path)
            if revealed:
                lines.append("已改为在 Finder 中定位该文件。")
            else:
                lines.append(f"Finder 定位也失败: {reveal_err}")
    elif url:
        opened, open_err = _open_path_or_url(url)
        lines.append("已打开 TradingView 图表页面。" if opened else f"TradingView 打开失败: {open_err}")
    else:
        lines.append("最近任务没有记录可打开的文件或 URL。")
    return "\n".join(lines)


def _print_pending_artifact_location(pending_artifact: dict) -> None:
    kind = str(pending_artifact.get("kind") or "artifact")
    symbol = str(pending_artifact.get("display") or pending_artifact.get("symbol") or "").strip()
    period = str(pending_artifact.get("period") or "").strip()
    command = str(pending_artifact.get("command") or "").strip()
    paths: list[tuple[str, str]] = []

    for label, key in (
        ("Pine", "pine_path"),
        ("HTML", "html_path"),
        ("PNG", "png_path"),
        ("图表", "chart_path"),
        ("文件", "path"),
        ("原始数据", "raw_path"),
        ("URL", "url"),
    ):
        value = str(pending_artifact.get(key) or "").strip()
        if value and value not in {p for _, p in paths}:
            paths.append((label, value))

    children = pending_artifact.get("children") or []
    child_paths: list[tuple[str, str, str]] = []
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            child_label = str(child.get("display") or child.get("symbol") or "子图表").strip()
            child_path = str(child.get("html_path") or child.get("chart_path") or child.get("path") or "").strip()
            if child_path:
                child_paths.append((child_label, "HTML", child_path))

    if HAS_RICH:
        console.print("\n[bold]Aria[/bold]\n")
        heading = "最近生成的文件"
        if symbol:
            heading += f"：{symbol}"
        if period:
            heading += f" · {period}"
        console.print(f"  [bold]{heading}[/bold]")
        console.print(f"  [dim]类型: {kind}[/dim]")
        if paths:
            for label, path in paths:
                console.print(f"  [dim]{label}:[/dim] [link={_file_uri(path)}]{_display_path(path, fallback=label)}[/link]")
        elif command:
            console.print("  [yellow]上一项任务只记录了命令，尚未记录具体文件路径。[/yellow]")
        if child_paths:
            console.print("  [dim]包含的单标的图表:[/dim]")
            for child_label, label, path in child_paths:
                console.print(f"    [dim]{child_label} {label}:[/dim] [link={_file_uri(path)}]{_display_path(path, fallback=child_label)}[/link]")
        if command:
            console.print(f"  [dim]复现命令: {command}[/dim]")
        console.print()
    else:
        title = "最近生成的文件"
        if symbol:
            title += f": {symbol}"
        if period:
            title += f" · {period}"
        print(f"\nAria\n\n  {title}")
        print(f"  类型: {kind}")
        if paths:
            for label, path in paths:
                print(f"  {label}: {_display_path(path, fallback=label)}")
        elif command:
            print("  上一项任务只记录了命令，尚未记录具体文件路径。")
        for child_label, label, path in child_paths:
            print(f"  {child_label} {label}: {_display_path(path, fallback=child_label)}")
        if command:
            print(f"  复现命令: {command}")
        print()


def _natural_language_visual_artifact_route(message: str, available_commands: set[str]):
    """Return a deterministic visual-artifact route for natural language input."""
    text = (message or "").strip()
    if not text or text.startswith("/"):
        return None
    try:
        from apps.cli.intent_router import build_intent_route

        route = build_intent_route(text)
        if not route.visual_artifact:
            return None
        if route.primary not in {"chart", "dashboard", "report", "ui_artifact"}:
            return None
    except Exception:
        if not any(k in text.lower() for k in (
            "图表", "走势图", "k线图", "k线", "chart", "dashboard", "看板", "报告", "report",
        )):
            return None
    return route_top_level_text(text, available_commands)


def _fetch_macro_data(indicator: str, country: str = "WLD", days: int = 365):
    """Fetch macro data from FRED or World Bank, return list of (date, value) tuples."""
    try:
        from datasources.sources.fred_source import FREDSource, MACRO_ALIASES
        if indicator.upper() in MACRO_ALIASES or indicator.upper() in MACRO_ALIASES.values():
            src = FREDSource()
            h = src.history(indicator, days=days)
            if h and h.data is not None and not h.data.empty:
                return [(str(idx.date()), float(row["close"])) for idx, row in h.data.iterrows()]
    except Exception as _e:
        pass
    try:
        from datasources.sources.world_bank_source import WorldBankSource
        src = WorldBankSource()
        h = src.history(f"{country}:{indicator}", days=days)
        if h and h.data is not None and not h.data.empty:
            return [(str(idx.date()), float(row["close"])) for idx, row in h.data.iterrows()]
    except Exception:
        pass
    return None


def _fetch_edgar_data(symbol: str, sub: str = "filings"):
    """Fetch SEC EDGAR data for a US stock."""
    try:
        from datasources.sources.edgar_source import EDGARSource
        src = EDGARSource()
        if sub == "filings":
            return src.get_recent_filings(symbol)
        elif sub == "facts":
            return src.get_company_facts(symbol)
        elif sub == "insider":
            return src.get_insider_trades(symbol)
    except Exception as _e:
        pass
    return None


def _test_datasource(name: str) -> None:
    """Test connectivity of a named data source."""
    try:
        from datasources.router import _SOURCE_REGISTRY
        cls = _SOURCE_REGISTRY.get(name.lower())
        if not cls:
            if HAS_RICH:
                console.print(f"  [red]未知数据源: {name}[/red]")
            return
        src = cls()
        if not src.is_configured():
            if HAS_RICH:
                console.print(f"  [yellow]⚠ {name} 未配置（缺少 API key）[/yellow]")
            return
        # Try a simple query
        test_symbol = "AAPL" if "us" in getattr(cls, "markets", []) else "600519"
        q = src.quote(test_symbol)
        if HAS_RICH:
            if q:
                console.print(f"  [green]✓ {name} 正常 — {test_symbol} = {q.price:.2f}[/green]")
            else:
                console.print(f"  [yellow]⚠ {name} 返回空数据[/yellow]")
    except Exception as e:
        if HAS_RICH:
            console.print(f"  [red]✗ {name} 失败: {e}[/red]")


def _generate_stat_arb_chart(sym_a: str, sym_b: str, period: str = "2y") -> None:
    """Generate interactive z-score history chart for stat-arb pair."""
    try:
        import pandas as _pd
        import yfinance as _yf
        import numpy as _np
        import pathlib as _pl
        import re as _re
        import json as _json

        raw = _yf.download([sym_a, sym_b], period=period, progress=False, auto_adjust=True)
        if raw.empty:
            return
        # Support both multi-level and flat column formats
        if isinstance(raw.columns, _pd.MultiIndex):
            prices = raw["Close"][[sym_a, sym_b]].dropna()
        else:
            prices = raw[["Close"]].rename(columns={"Close": sym_a}).dropna()
            return  # need both

        spread = prices[sym_a] - prices[sym_b]
        roll   = spread.rolling(60)
        z      = ((spread - roll.mean()) / roll.std()).dropna()

        x     = [d.strftime("%Y-%m-%d") for d in z.index]
        z_val = [round(float(v), 3) for v in z.values]
        z_rows = [{"date": d, "z": v} for d, v in zip(x, z_val)]

        entry_lo, entry_hi = -2.0, 2.0
        stop_lo, stop_hi   = -3.5, 3.5
        last_z_value = z_val[-1] if z_val else None

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{sym_a}/{sym_b} Z-Score</title>
{plotly_script_tag()}
</head><body style="background:#0d1117;color:#e6edf3;margin:0;padding:16px;font-family:monospace">
<h2 style="color:#58a6ff">{sym_a} / {sym_b} — 配对价差 Z-Score ({period})</h2>
<div id="chart" style="width:100%;height:500px"></div>
<script>
const x = {_json.dumps(x)};
const z = {_json.dumps(z_val)};
const last_z = z[z.length-1];
const colors = z.map(v => v > {entry_hi} || v < {entry_lo} ? (Math.abs(v) > {stop_hi} ? '#f85149' : '#f0883e') : '#58a6ff');
Plotly.newPlot('chart', [
  {{x, y: z, type:'scatter', mode:'lines', name:'Z-Score',
    line:{{color:'#58a6ff', width:1.5}}}},
  {{x:[x[0],x[x.length-1]], y:[{entry_hi},{entry_hi}], type:'scatter', mode:'lines',
    name:'做空阈值 (+{entry_hi})', line:{{color:'#f0883e', width:1, dash:'dot'}}}},
  {{x:[x[0],x[x.length-1]], y:[{entry_lo},{entry_lo}], type:'scatter', mode:'lines',
    name:'做多阈值 ({entry_lo})', line:{{color:'#3fb950', width:1, dash:'dot'}}}},
  {{x:[x[0],x[x.length-1]], y:[{stop_hi},{stop_hi}], type:'scatter', mode:'lines',
    name:'止损上轨 (+{stop_hi})', line:{{color:'#f85149', width:1, dash:'dash'}}}},
  {{x:[x[0],x[x.length-1]], y:[{stop_lo},{stop_lo}], type:'scatter', mode:'lines',
    name:'止损下轨 ({stop_lo})', line:{{color:'#f85149', width:1, dash:'dash'}}}},
  {{x:[x[0],x[x.length-1]], y:[0,0], type:'scatter', mode:'lines',
    name:'均值归零', line:{{color:'#8b949e', width:1}}}}
], {{
  paper_bgcolor:'#0d1117', plot_bgcolor:'#161b22',
  font:{{color:'#e6edf3', family:'monospace'}},
  xaxis:{{gridcolor:'#21262d', tickfont:{{size:10}}}},
  yaxis:{{gridcolor:'#21262d', title:'Z-Score'}},
  legend:{{bgcolor:'#161b22', bordercolor:'#30363d'}},
  annotations:[{{
    x:x[x.length-1], y:last_z, text:`当前 Z=${{last_z.toFixed(2)}}`,
    showarrow:true, arrowcolor:'#e6edf3',
    font:{{color:'#e6edf3', size:12}}, bgcolor:'#30363d'
  }}]
}}, {{responsive:true}});
</script></body></html>"""

        safe = _re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{sym_a}_{sym_b}")
        from artifacts import create_user_artifact, write_artifact_metadata, write_artifact_raw_data
        art = create_user_artifact("reports/stat-arb", f"{sym_a}_{sym_b}", f"{safe}_zscore", ".html")
        art.path.write_text(html, encoding="utf-8")
        write_artifact_metadata(art, {
            "kind": "stat_arb_chart",
            "status": "complete",
            "symbols": [sym_a, sym_b],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "data": {
                "points": len(z_rows),
                "entry_threshold": abs(entry_hi),
                "stop_threshold": abs(stop_hi),
                "last_z": last_z_value,
            },
        })
        write_artifact_raw_data(art, {
            "symbols": [sym_a, sym_b],
            "rows": z_rows,
        })
        if HAS_RICH:
            console.print(f"  [dim]Z-Score 图表: [link={_file_uri(art.path)}]{_display_path(art.path)}[/link][/dim]")
            import subprocess
            try:
                subprocess.Popen(["open", str(art.path)])
            except Exception:
                pass
    except Exception as _e:
        if HAS_RICH:
            console.print(f"  [dim]Z-Score 图表生成跳过: {_e}[/dim]")


def _try_handle_broker_query(message: str) -> dict:
    return _src_handle_broker_query(
        message,
        has_brokers=_HAS_BROKERS,
        is_broker_intent=_is_broker_intent,
        get_broker_registry=_get_broker_registry,
    )


def _try_handle_stock_chart_analysis_direct(symbol: str, period: str = "1y") -> dict:
    return _src_chart_analysis_direct(symbol, period=period)


def _try_handle_stock_chart_analysis(message: str) -> dict:
    return _src_chart_analysis(
        message,
        is_chart_request=_is_stock_chart_analysis_request,
        extract_symbol=_extract_market_symbol,
    )


# stream_ollama is rebound at the end of this module — see _rebind_stream_ollama().

# ============================================================================
# Aria SSE Stream Client — cancel + auth + user context
# ============================================================================

async def stream_chat(base_url: str, message: str, history: list,
                      model: str = "qwen2.5:7b", thinking_mode: str = "auto",
                      user_context: dict = None, auth_token: str = None,
                      on_token=None, on_thinking=None, on_tool_call=None,
                      on_tool_result=None, on_status=None,
                      cancel_event: asyncio.Event = None) -> dict:
    """Thin shim — implementation lives in apps/cli/providers/llm/sse_stream.py."""
    from apps.cli.providers.llm.sse_stream import stream_chat as _stream_chat
    return await _stream_chat(
        base_url, message, history,
        model=model, thinking_mode=thinking_mode,
        user_context=user_context, auth_token=auth_token,
        on_token=on_token, on_thinking=on_thinking,
        on_tool_call=on_tool_call, on_tool_result=on_tool_result,
        on_status=on_status, cancel_event=cancel_event,
        project_context=_PROJECT_CONTEXT,
    )





def _build_user_context(config: dict) -> Optional[dict]:
    """Build user context from config for personalized AI responses."""
    ctx = {}
    watchlist = config.get("watchlist", [])
    if watchlist:
        ctx["watchlist"] = watchlist
    user_id = config.get("user_id")
    if user_id:
        ctx["user_id"] = user_id
    # Inject current datetime and session info
    now = datetime.now()
    ctx["current_datetime"] = now.strftime("%Y-%m-%d %H:%M")
    ctx["day_of_week"] = now.strftime("%A")
    # The US session on New York's clock — this compared local hours with
    # 09:30-16:00, so a user in Beijing had the market "open" overnight.
    _us_session = market_session("SPY")
    if _us_session:
        ctx["market_status"] = {"pre": "pre-market", "post": "after-hours"}.get(_us_session, _us_session)
    # Active model name
    model_id = config.get("model", "qwen2.5:7b")
    mkey = resolve_model_key(model_id)
    minfo = MODELS.get(mkey, {})
    ctx["ai_model"] = minfo.get("name", model_id)
    if config.get("local_mode"):
        ctx["inference_mode"] = "local"
    return ctx if ctx else None


# ============================================================================
# Tool Output Formatters
# ============================================================================





def _format_tool_params(tool_name: str, params: dict) -> str:
    """Format tool params into a short, target-safe UI hint."""
    if not params:
        return ""
    def _short(value: object, limit: int = 60) -> str:
        text = str(value or "").strip().replace("\n", " ")
        return text[: limit - 1] + "…" if len(text) > limit else text

    if tool_name in ("read_file", "write_file", "edit_file"):
        # Basename only — informative without leaking full workspace paths.
        return _short(pathlib.Path(str(params.get("path", ""))).name or "file tool", 40)
    if tool_name == "run_command":
        # The command itself, secrets masked: "shell tool" said nothing, while
        # the approval menu under it named the command anyway.
        from apps.cli.runtime_consumer import _redact_activity_text
        return _short(_redact_activity_text(params.get("command", ""), limit=200) or "shell", 72)
    if tool_name == "list_files":
        return _short(params.get("pattern") or params.get("path") or "file tool", 40)
    if tool_name == "search_code":
        return _short(params.get("pattern", "") or "file tool", 40)
    if tool_name in ("get_market_data", "get_market_history", "get_crypto_data", "get_forex_data",
                      "get_commodities_data", "get_futures_data", "get_bonds_data"):
        return params.get("symbol", params.get("symbols", ""))
    if tool_name == "backtest_strategy":
        return f"{params.get('strategy', '')} {params.get('symbol', '')}"
    if tool_name in ("web_search", "search_web"):
        return _short(params.get("query", "") or "web search")
    if tool_name == "web_fetch":
        _u = str(params.get("url", ""))
        _u = _u.split("://", 1)[-1]  # drop scheme; host+path is the signal
        return _short(_u or "web fetch")
    if tool_name == "analyze_news":
        return params.get("symbol", params.get("query", ""))
    if tool_name.startswith("mcp__"):
        return "MCP"
    if tool_name.startswith("skill") or tool_name in {"TaskCreate", "TaskUpdate"}:
        return "skill"
    # Fallback: first short scalar param is far more useful than "tool"
    for _v in params.values():
        if isinstance(_v, (str, int, float)) and str(_v).strip():
            return _short(_v, 40)
    return "tool"


_TOOL_ACTION_LABELS: dict = {
    # Market data
    "get_market_data":           "loading market data",
    "get_market_history":        "loading price history",
    "get_quote":                 "fetching quote",
    "get_ohlcv":                 "loading price history",
    "get_fundamental_data":      "loading fundamentals",
    "get_news":                  "fetching news",
    "get_earnings":              "loading earnings data",
    "get_crypto_data":           "loading crypto data",
    "get_forex_data":            "loading forex rates",
    "get_commodity_data":        "loading commodity data",
    # Technical / quant
    "get_technical_indicators":  "computing technical indicators",
    "calculate_factors":         "running factor model",
    "calculate_risk_metrics":    "calculating risk metrics",
    "get_options_chain":         "loading options chain",
    "get_peer_comparison":       "running peer comparison",
    "calculate_correlation":     "computing correlation matrix",
    # Backtest / strategy
    "run_backtest":              "running backtest simulation",
    "run_walk_forward":          "running walk-forward analysis",
    "portfolio_backtest":        "running portfolio simulation",
    "optimize_portfolio":        "optimizing portfolio weights",
    # Research / reports
    "generate_report":           "generating research report",
    "get_market_snapshot":       "scanning market",
    "get_sector_flow":           "loading sector flow data",
    "get_limit_up_pool":         "scanning limit-up pool",
    "get_north_bound_flow":      "loading north-bound capital flow",
    # File / code
    "read_file":                 "reading file",
    "analyze_file":              "analyzing file",
    "write_file":                "writing file",
    "edit_file":                 "editing file",
    "list_files":                "listing files",
    "search_code":               "searching codebase",
    "run_command":               "executing command",
    "process":                   "checking background process",
    # Macro / realty
    "get_macro_data":            "loading macro indicators",
    "get_house_price_index":     "loading house price data",
    "get_reits_data":            "loading REITs data",
    # Broker
    "get_account_info":          "fetching account info",
    "get_positions":             "loading positions",
    "get_orders":                "loading orders",
    "place_order":               "preparing order",
    # SQL / data
    "sql_query":                 "running SQL query",
    "export_to_excel":           "exporting to Excel",
}


_STUB_PLACEHOLDER_MARKERS = (
    "欢迎使用 Aria AI 金融助手",
    "这是一个需要详细解释的概念。请稍后重试",
    "Welcome to Aria",
    "请提供更具体的问题",
    "I'm here to help with financial",
    "股票数据查询",
    "请提供您想查询的股票",
    "请输入您的具体问题",
    "支持的查询方式",
)


def _response_is_stub_placeholder(resp: str) -> bool:
    """True if resp is the api_url stub backend's canned help/welcome text.

    Shared by the REPL chat loop and run_prompt so both fall back to Ollama
    instead of showing boilerplate. (Empty/too-short is handled by callers.)
    """
    if not resp:
        return False
    if any(p in resp for p in _STUB_PLACEHOLDER_MARKERS):
        return True
    _markers = ("请提供", "请输入", "示例问题", "支持的查询", "股票代码：")
    return sum(1 for m in _markers if m in resp) >= 2


def _render_answer_block(text: str) -> None:
    """Render the AI's final answer with a ⏺ bullet + hanging indent.

    Mirrors the tool-call rhythm (⏺ for every turn segment) so the answer
    aligns visually with the tool tree. Bullet sits at the margin, the
    Markdown body is indented past it — Claude Code's hanging-indent look.
    """
    if _ARIA_BOT_MODE:
        console.print(make_markdown(_strip_latex(text)))
        return
    if not HAS_RICH:
        print(f"\n  ⏺  {text}")
        return
    from rich.padding import Padding
    console.print(Padding(make_markdown(_strip_latex(text)), (0, 0, 0, 4)))


_MODEL_UNAVAILABLE_MARKERS = (
    "vertex_needs_project", "vertex_not_logged_in", "vertex_gcloud_failed", "missing_api_key:",
    "no_cloud_provider", "no_provider", "all_providers_failed", "http 401", "http 403", "http 402",
    "insufficient balance", "http 429", "rate limit",
)


def _model_unavailable(error: object) -> bool:
    """The turn failed because no model could be reached — not because of what was asked."""
    text = str(error or "").lower()
    return any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS)


def _data_without_model(message: str, history: list) -> dict:
    """What Aria can answer from data alone, for a turn whose model is unreachable.

    With no Google project set, "AAPL price" printed setup guidance and
    nothing else, though the quote needs no model: the same pre-fetch that
    serves tool-less routes answers it.
    """
    try:
        found = _run_deterministic_chain(message, model_has_tools=False, history=history)
    except Exception as exc:
        logger.debug("data fallback failed: %s", exc)
        return {}
    return found if found.get("success") and (found.get("response") or "").strip() else {}


def _announce_route(command_text: str) -> None:
    """Say which command a plain-language message ran: "→ /news AAPL".

    Routed messages used to run their command silently, so a misroute looked
    like Aria misunderstanding, and nobody learned the command for next time.
    """
    if _ARIA_BOT_MODE:
        return
    if HAS_RICH:
        from rich.text import Text

        console.print(Text(f"  → {command_text}", style="dim"))
    else:
        print(f"  → {command_text}")


def _print_tool_call(tool_name: str, params: dict):
    """Print tool call header — Claude Code-style ⏺ bullet tree."""
    if _ARIA_BOT_MODE:
        return
    hint = _format_tool_params(tool_name, params)
    # MCP tools are named mcp__<server>__<tool> — render as "server · tool"
    # with a dim MCP tag so the user knows it came from an external server.
    _mcp_tag = ""
    if tool_name.startswith("mcp__"):
        _parts = tool_name.split("__")
        if len(_parts) >= 3:
            action = f"{_parts[1]} · {_parts[2].replace('_', ' ')}"
            _mcp_tag = "  [dim]MCP[/dim]"
        else:
            action = tool_name.replace("_", " ")
    else:
        action = _TOOL_ACTION_LABELS.get(tool_name, tool_name.replace("_", " "))
    if HAS_RICH:
        if hint:
            console.print(f"\n  [#C08050]⏺[/#C08050]  [bold]{action}[/bold]{_mcp_tag}  [dim]{hint}[/dim]")
        else:
            console.print(f"\n  [#C08050]⏺[/#C08050]  [bold]{action}[/bold]{_mcp_tag}")
    else:
        label = f"{action}  {hint}" if hint else action
        print(f"\n  ⏺ {label}", end="", flush=True)


def _print_tool_done(tool_name: str, elapsed_ms: int, success: bool = True, summary: str = ""):
    """Print a compact ✓/✗ result line under the ⏺ tool-call header."""
    if _ARIA_BOT_MODE or not HAS_RICH:
        return
    action = _TOOL_ACTION_LABELS.get(tool_name, tool_name.replace("_", " "))
    icon   = "[green]✓[/green]" if success else "[red]✗[/red]"
    t_txt  = ("" if elapsed_ms <= 0 else f"{elapsed_ms}ms" if elapsed_ms < 1000 else f"{elapsed_ms / 1000:.1f}s")
    if summary:
        # 单行预算:summary 按显示宽度(CJK 记 2 格)截断,保证图标+动作+摘要+时长
        # 排在同一行——否则 Rich 整行回卷,时长 chip 顶格孤立在下一行,树形缩进被破坏。
        # 错误文本可能含 [] 等 Rich 标记字符,一并转义防串样式。
        import shutil as _sh
        from rich.cells import cell_len as _cl
        from rich.markup import escape as _esc
        cols   = _sh.get_terminal_size((100, 24)).columns
        fixed  = 5 + _cl(action) + (2 + len(t_txt) if t_txt else 0) + 2
        budget = max(12, cols - fixed)
        if _cl(summary) > budget:
            out, w = [], 0
            for ch in summary:
                cw = _cl(ch)
                if w + cw > budget - 1:
                    break
                out.append(ch)
                w += cw
            summary = "".join(out) + "…"
        summary = _esc(summary)
    s_str = f"  [dim]{summary}[/dim]" if summary else ""
    t_str = f"  [dim]{t_txt}[/dim]"   if t_txt   else ""
    console.print(f"  {icon}  [dim]{action}[/dim]{s_str}{t_str}")


def _print_phase(label: str):
    """Bloomberg-style phase divider for multi-step operations."""
    if _ARIA_BOT_MODE or not HAS_RICH:
        return
    import shutil as _sh
    w       = _sh.get_terminal_size((80, 24)).columns
    bar_len = max(0, w - len(label) - 8)
    console.print(f"\n  [dim]── {label} {'─' * bar_len}[/dim]")




def _error_hint(error: str, context: str = "") -> str:
    from ui.render.output import error_hint as _eh
    return _eh(error, context)


def _print_error(msg: str, context: str = ""):
    from ui.render.output import print_error as _pe
    _pe(msg, context, console=console, has_rich=HAS_RICH, rich_box=rich_box)


def _print_preflight_notice(message: str, *, quiet: bool = False) -> bool:
    """Show missing dependency/tool guidance for the likely user intent."""
    if quiet:
        return False
    try:
        report = build_intent_preflight(message)
    except Exception as exc:
        logger.debug("intent preflight failed: %s", exc)
        return False
    if not report.has_findings:
        return False
    # Optional accelerators and fallbacks should not interrupt a normal task.
    # They remain visible through /preflight and /install when explicitly asked.
    if not report.has_required_findings:
        return False

    text = format_preflight_plain(report)
    if not text:
        return False
    if HAS_RICH:
        style = "yellow" if report.has_required_findings else "dim"
        title = "[yellow]依赖预检[/yellow]" if report.has_required_findings else "[dim]依赖预检[/dim]"
        console.print(Panel(
            text,
            title=title,
            border_style=style,
            box=rich_box.ROUNDED,
            padding=(0, 1),
        ))
    else:
        print(text)
    return True


from contextlib import contextmanager as _contextmanager

@_contextmanager
def _null_ctx():
    """No-op context manager for conditional `with` blocks."""
    yield


# ── Verdict banner ─────────────────────────────────────────────────────────────

# Alias kept for any internal references that pre-date the move to team_render.
_VERDICT_STYLE: dict = VERDICT_STYLE


def _print_verdict_banner(verdict: str, subtitle: str = "", confidence: float = None,
                          lang: str = "zh") -> None:
    """Thin wrapper — rendering logic lives in team_render.render_verdict_banner."""
    render_verdict_banner(verdict, subtitle, confidence,
                          console=console, has_rich=HAS_RICH, lang=lang)


def _print_agent_table(sym: str, results: list, use_full: bool = False) -> None:
    """Thin wrapper — rendering logic lives in team_render.render_team_table."""
    import shutil as _shutil
    rows = build_team_table_rows(results)
    tw   = getattr(console, "width", None) or _shutil.get_terminal_size().columns
    render_team_table(sym, rows, use_full,
                      console=console, terminal_width=tw, has_rich=HAS_RICH)


def _team_live_price(data_bundle) -> Optional[float]:
    """Extract a usable live/reference price from a DataBundle-like object."""
    try:
        quote = getattr(data_bundle, "quote", {}) or {}
        value = quote.get("price") or quote.get("current_price") or quote.get("regular_market_price")
        if value is None:
            return None
        value = float(value)
        return value if value > 0 else None
    except Exception:
        return None




_TEAM_DOLLAR_RE = re.compile(r"(?<![A-Za-z0-9])\$\s*([0-9][0-9,]*(?:\.\d+)?)")


def _team_conflicting_prices(text: str, live_price: Optional[float]) -> list[float]:
    """Find dollar prices that are clearly incompatible with current quote.

    This is intentionally conservative: it only inspects explicit "$123" style
    figures and only flags values far outside the live-price range. The goal is
    to catch split-adjusted/stale LLM output such as NVDA $945 when live price is
    around $205, without rejecting normal support/target ranges nearby.
    """
    if not text or not live_price or live_price <= 0:
        return []
    conflicts: list[float] = []
    for raw in _TEAM_DOLLAR_RE.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if value <= 0:
            continue
        ratio = value / live_price
        if ratio >= 1.8 or ratio <= 0.35:
            conflicts.append(value)
    return conflicts[:8]


def _sanitize_team_result_with_market_data(team_result, data_bundle) -> list[str]:
    """Validate /team output against live data and mark stale/hallucinated parts."""
    notes: list[str] = []
    live_price = _team_live_price(data_bundle)
    if not team_result or not live_price:
        return notes

    for result in getattr(team_result, "results", []) or []:
        text = "\n".join([
            str(getattr(result, "analysis", "") or ""),
            "\n".join(str(p) for p in (getattr(result, "key_points", []) or [])),
        ])
        conflicts = _team_conflicting_prices(text, live_price)
        if not conflicts:
            continue
        result.analysis = (
            "该 Agent 输出包含与当前行情明显冲突的价格，已从报告正文中移除。\n\n"
            f"- 当前参考价: {live_price:.2f}\n"
            f"- 冲突价格: {', '.join(f'${v:g}' for v in conflicts)}\n"
            "- 请重新运行 /team，或先运行 /doctor 检查数据源与模型上下文。"
        )
        result.key_points = [f"数据冲突: 输出价格与当前参考价 {live_price:.2f} 不一致"]
        result.signal = "HOLD"
        result.confidence = min(float(getattr(result, "confidence", 0.0) or 0.0), 0.2)
        result.error = "stale_or_conflicting_price"
        notes.append(
            f"{getattr(result, 'agent', 'agent')}: removed stale/conflicting prices "
            f"({', '.join(f'${v:g}' for v in conflicts)})"
        )

    conflicts = _team_conflicting_prices(getattr(team_result, "synthesis", "") or "", live_price)
    if conflicts:
        team_result.synthesis = (
            "综合结论已降级：原始综合结论包含与当前行情明显冲突的价格，"
            "因此不应作为投资依据。\n\n"
            f"- 当前参考价: {live_price:.2f}\n"
            f"- 冲突价格: {', '.join(f'${v:g}' for v in conflicts)}\n"
            "- 建议先确认数据源健康，再重新运行 /team 或 /ta。"
        )
        team_result.final_signal = "HOLD"
        team_result.confidence = min(float(getattr(team_result, "confidence", 0.0) or 0.0), 0.2)
        notes.append(
            "synthesis: replaced stale/conflicting conclusion "
            f"({', '.join(f'${v:g}' for v in conflicts)})"
        )
    return notes
    console.print()




# A-share code → Chinese name lookup with on-disk JSON cache (7-day TTL)
_ASHARE_NAMES_CACHE: dict = {}
_ASHARE_NAMES_LOADED: bool = False
_ASHARE_NAMES_FAIL_TS: float = 0.0  # timestamp of last fetch failure; retry after 5 min
_ASHARE_NAMES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research", ".cache", "ashare_names.json")


def _ensure_ashare_names_loaded() -> dict:
    """Load (and auto-refresh) the A-share code→Chinese name mapping."""
    global _ASHARE_NAMES_CACHE, _ASHARE_NAMES_LOADED, _ASHARE_NAMES_FAIL_TS
    if _ASHARE_NAMES_LOADED:
        return _ASHARE_NAMES_CACHE

    import json as _json
    import time as _time

    # Back off for 5 minutes after a network failure to avoid hammering AKShare
    if _ASHARE_NAMES_FAIL_TS and _time.time() - _ASHARE_NAMES_FAIL_TS < 300:
        return _ASHARE_NAMES_CACHE

    cache_path = _ASHARE_NAMES_PATH
    cache_dir  = os.path.dirname(cache_path)

    # Try reading existing cache
    if os.path.exists(cache_path):
        try:
            mtime = os.path.getmtime(cache_path)
            if _time.time() - mtime < 7 * 86400:  # 7-day TTL
                with open(cache_path, encoding="utf-8") as _f:
                    _ASHARE_NAMES_CACHE = _json.load(_f)
                _ASHARE_NAMES_LOADED = True
                return _ASHARE_NAMES_CACHE
        except Exception:
            pass

    # Cache missing or stale — rebuild from akshare
    try:
        import akshare as _ak  # type: ignore
        df = _ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            mapping: dict = {}
            for _, row in df.iterrows():
                code = str(row.get("code", row.iloc[0])).zfill(6)
                name = str(row.get("name", row.iloc[1]))
                mapping[code] = name
            _ASHARE_NAMES_CACHE = mapping
            # Persist to disk
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as _f:
                    _json.dump(mapping, _f, ensure_ascii=False)
            except Exception:
                pass
            _ASHARE_NAMES_LOADED = True
    except Exception:
        _ASHARE_NAMES_FAIL_TS = _time.time()  # retry after 5-min backoff, not permanently locked

    return _ASHARE_NAMES_CACHE


def _ashare_code_to_name(symbol: str) -> str:
    """Return the Chinese company name for a 6-digit A-share code, or empty string."""
    # Normalise to bare 6-digit code
    code = symbol.upper().strip()
    code = code.replace(".SS", "").replace(".SZ", "")
    code = code.lstrip("SH").lstrip("SZ") if not code[:2].isdigit() else code
    code = code.zfill(6) if code.isdigit() else code

    names = _ensure_ashare_names_loaded()
    return names.get(code, "")


from aria_code.ui.render.output import FINANCE_TOOL_NAMES as _FINANCE_TOOL_NAMES
from aria_code.ui.render.output import display_path as _display_path



def _print_tool_result(tool_name: str, result: dict, elapsed: float = 0, params: dict = None):
    from ui.render.output import print_tool_result as _ptr
    _ptr(
        tool_name, result, elapsed, params,
        console=console, has_rich=HAS_RICH, rich_box=rich_box,
        print_finance_fn=_print_finance_result,
        bot_mode=_ARIA_BOT_MODE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Finance rendering — implementation lives in ui/render/finance.py
# These thin wrappers supply the module-level console / HAS_RICH / _ARIA_BOT_MODE.
# ─────────────────────────────────────────────────────────────────────────────

def _print_finance_result(tool_name: str, result: dict) -> None:
    render_finance_result(tool_name, result,
                          console=console, has_rich=HAS_RICH,
                          bot_mode=_ARIA_BOT_MODE)


def _render_macro_result(r: dict, title: str, lang: str = "zh") -> None:
    render_macro_result(r, title, console=console, has_rich=HAS_RICH, lang=lang)


def _render_cb_rates(r: dict, lang: str = "zh") -> None:
    render_cb_rates(r, console=console, has_rich=HAS_RICH, lang=lang)


def _render_econ_calendar(r: dict) -> None:
    render_econ_calendar(r, console=console, has_rich=HAS_RICH)


def _render_options_chain(r: dict) -> None:
    render_options_chain(r, console=console, has_rich=HAS_RICH)


def _render_quality_scores(symbol: str, f_r: dict, z_r: dict) -> None:
    render_quality_scores(symbol, f_r, z_r, console=console, has_rich=HAS_RICH)


def _render_ichimoku(r: dict) -> None:
    render_ichimoku(r, console=console, has_rich=HAS_RICH)


def _render_fear_greed(r: dict) -> None:
    render_fear_greed(r, console=console, has_rich=HAS_RICH)


def _render_funding_rates(r: dict) -> None:
    render_funding_rates(r, console=console, has_rich=HAS_RICH)


def _render_funding_compare(r: dict) -> None:
    if not r.get("success"):
        if HAS_RICH:
            console.print(f"[red]{r.get('error', '获取失败')}[/red]")
        else:
            print(r.get("error", "获取失败"))
        return

    comparison = r.get("comparison", [])
    exchanges  = r.get("exchanges", ["binance", "okx", "bybit"])
    arb_note   = r.get("arb_note", "")
    max_spread = r.get("max_spread", 0.0)

    if HAS_RICH:
        from rich.table import Table
        tbl = Table(title="资金费率三所对比", box=rich_box.SIMPLE_HEAVY, show_lines=True)
        tbl.add_column("标的", style="bold")
        for ex in exchanges:
            tbl.add_column(ex.upper(), justify="right")
        tbl.add_column("价差", justify="right")
        tbl.add_column("套利信号", justify="center")

        for row in comparison:
            sym = row["symbol"]
            cells = []
            rates = []
            for ex in exchanges:
                d = row.get(ex)
                if d:
                    rate = d["rate"]
                    rates.append(rate)
                    color = "red" if rate > 0.05 else "green" if rate < -0.01 else "white"
                    cells.append(f"[{color}]{d['rate_pct']}[/{color}]")
                else:
                    cells.append("[dim]N/A[/dim]")
            spread = round(max(rates) - min(rates), 4) if len(rates) >= 2 else 0.0
            spread_str = f"[yellow]{spread:.4f}%[/yellow]" if spread > 0.02 else f"{spread:.4f}%"
            signal = "⚡ 套利" if spread > 0.02 else "—"
            tbl.add_row(sym, *cells, spread_str, signal)

        console.print()
        console.print(tbl)
        color = "yellow" if max_spread > 0.02 else "dim"
        console.print(f"\n[{color}]{arb_note}  最大价差 {max_spread:.4f}%[/{color}]")
        console.print(f"[dim]资金费率对比 · 本内容不构成投资建议[/dim]")
        console.print(Rule(style="dim"))
    else:
        print("\n资金费率三所对比")
        for row in comparison:
            sym = row["symbol"]
            parts = []
            for ex in exchanges:
                d = row.get(ex)
                parts.append(f"{ex}:{d['rate_pct'] if d else 'N/A'}")
            print(f"  {sym}  " + "  ".join(parts))
        print(f"\n{arb_note}")


def _render_peer_comparison(r: dict) -> None:
    render_peer_comparison(r, console=console, has_rich=HAS_RICH)


def _render_house_price(r: dict) -> None:
    render_house_price(r, console=console, has_rich=HAS_RICH)


def _render_reits_list(r: dict) -> None:
    render_reits_list(r, console=console, has_rich=HAS_RICH)


def _render_rental_yield(r: dict) -> None:
    render_rental_yield(r, console=console, has_rich=HAS_RICH)


def _render_property_val(r: dict) -> None:
    render_property_val(r, console=console, has_rich=HAS_RICH)


def _render_multi_city(r: dict) -> None:
    render_multi_city(r, console=console, has_rich=HAS_RICH)


def _render_asset_score(r: dict) -> None:
    render_asset_score(r, console=console, has_rich=HAS_RICH)


def _render_corr_matrix(r: dict) -> None:
    render_corr_matrix(r, console=console, has_rich=HAS_RICH)


def _render_portfolio_bt(r: dict) -> None:
    render_portfolio_bt(r, console=console, has_rich=HAS_RICH)


def _render_sql_result(r: dict) -> None:
    render_sql_result(r, console=console, has_rich=HAS_RICH)


def _render_alerts(r: dict) -> None:
    render_alerts(r, console=console, has_rich=HAS_RICH)


def _prompt_float(label: str, default: float) -> float:
    """交互式数字输入，失败时返回 default。"""
    try:
        if HAS_RICH:
            from rich.prompt import Prompt
            raw = Prompt.ask(f"  {label}", default=str(default))
        else:
            raw = input(f"  {label}") or str(default)
        return float(raw)
    except ValueError:
        if HAS_RICH:
            console.print(f"  [yellow]请输入有效数字，已使用默认值 {default}[/yellow]")
        else:
            print(f"  请输入有效数字，已使用默认值 {default}")
        return default
    except KeyboardInterrupt:
        return default


def _prompt_str(label: str, default: str) -> str:
    """交互式字符串输入，失败时返回 default。"""
    try:
        if HAS_RICH:
            from rich.prompt import Prompt
            return Prompt.ask(f"  {label}", default=default)
        else:
            return input(f"  {label}") or default
    except (ValueError, KeyboardInterrupt):
        return default


def format_quote_output(data: dict):
    """Format market data as clean two-column rows."""
    if not HAS_RICH:
        return json.dumps(data, indent=2, ensure_ascii=False)

    d = data.get("data", data)
    symbol = d.get("symbol", "???")
    price = d.get("current_price", d.get("price", 0))
    change = d.get("change_percent", d.get("changePercent", 0))
    high52 = d.get("high_52w", d.get("yearHigh", "-"))
    low52 = d.get("low_52w", d.get("yearLow", "-"))
    volume = d.get("volume", "-")
    market_cap = d.get("market_cap", d.get("marketCap", "-"))

    color = "green" if change >= 0 else "red"
    arrow = "+" if change >= 0 else ""

    out = Text()
    out.append(f"  {symbol}\n", style="bold")
    price_str = f"${price:,.2f}" if isinstance(price, (int, float)) else str(price)
    out.append(f"  {'Price':<16s}", style="dim")
    out.append(f"{price_str}\n")
    out.append(f"  {'Change':<16s}", style="dim")
    out.append(f"{arrow}{change:.2f}%\n", style=color)
    if isinstance(high52, (int, float)):
        out.append(f"  {'52W High':<16s}", style="dim")
        out.append(f"${high52:,.2f}\n")
    if isinstance(low52, (int, float)):
        out.append(f"  {'52W Low':<16s}", style="dim")
        out.append(f"${low52:,.2f}\n")
    if volume != "-":
        vol_str = f"{volume:,}" if isinstance(volume, (int, float)) else str(volume)
        out.append(f"  {'Volume':<16s}", style="dim")
        out.append(f"{vol_str}\n")
    if market_cap and market_cap != "-":
        mc = market_cap
        if isinstance(mc, (int, float)):
            mc_str = f"${mc/1e12:.2f}T" if mc >= 1e12 else f"${mc/1e9:.2f}B" if mc >= 1e9 else f"${mc/1e6:.0f}M"
        else:
            mc_str = str(mc)
        out.append(f"  {'Market Cap':<16s}", style="dim")
        out.append(f"{mc_str}\n")
    # Sparkline from chart_prices
    chart_prices = d.get("chart_prices", [])
    if chart_prices and len(chart_prices) >= 2:
        prices = [p.get("close", p.get("price", 0)) if isinstance(p, dict) else p
                  for p in chart_prices]
        prices = [p for p in prices if isinstance(p, (int, float)) and p > 0]
        if len(prices) >= 2:
            spark = format_sparkline(prices, width=24)
            out.append(f"  {'1M':<16s}", style="dim")
            out.append(f"{spark}\n", style=color)
    return out






# ============================================================================
# Tab Completer — commands, skills, stock symbols
# ============================================================================

class ArtheraCompleter:
    """Tab completion for basic readline fallback."""

    def __init__(self, commands: list, skills: list, watchlist: list):
        self.tokens = list(commands) + [s["command"] for s in skills]
        self.tokens.extend([
            "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX",
            "AMD", "INTC", "SPY", "QQQ", "DIA", "IWM", "BTC", "ETH", "SOL",
            "JPM", "BAC", "GS", "V", "MA", "UNH", "JNJ", "PFE", "XOM", "CVX",
        ])
        self.tokens.extend(watchlist)
        self.matches = []

    def complete(self, text: str, state: int):
        if state == 0:
            self.matches = [t for t in self.tokens
                            if t.lower().startswith(text.lower())] if text else []
        try:
            return self.matches[state]
        except IndexError:
            return None


from aria_code.ui.completer import AriaPTCompleter, ARIA_PT_STYLE, build_aria_pt_style
from aria_code.apps.cli.commands.market_cmds import _parse_nl_team_pair




# ============================================================================
# Slash Commands
# ============================================================================

import types as _types

def _rebind_mixin_globals(mixin_cls):
    """Point mixin methods' __globals__ to this module's namespace so bare names resolve."""
    for _attr_name, _attr in list(vars(mixin_cls).items()):
        if isinstance(_attr, _types.FunctionType):
            _new_fn = _types.FunctionType(
                _attr.__code__, globals(), _attr.__name__,
                _attr.__defaults__, _attr.__closure__
            )
            setattr(mixin_cls, _attr_name, _new_fn)


def _rebind_module_function_globals(module, names):
    """Rebind extracted helper functions to this legacy CLI module's globals.

    The CLI is being split into focused modules.  A number of helpers still
    deliberately read session state (approval policy, console, feature flags)
    from ``aria_cli``.  Importing them with ``from … import *`` without this
    bridge leaves those functions pointing at the small extracted module,
    where that state does not exist.  Keep the compatibility boundary here
    until the state is injected explicitly at every call site.
    """
    for _name in names:
        _attr = getattr(module, _name, None)
        if isinstance(_attr, _types.FunctionType):
            _rebound = _types.FunctionType(
                _attr.__code__, globals(), _attr.__name__,
                _attr.__defaults__, _attr.__closure__,
            )
            # FunctionType takes positional defaults but not keyword-only ones,
            # so a rebound function silently loses them and every keyword-only
            # parameter becomes required. _format_tool_summary's char_limit hit
            # exactly this: the extracted module declares a default, the rebind
            # dropped it, and callers that had always omitted it started raising
            # TypeError. __doc__ goes across for the same reason — the rebind
            # should be invisible.
            _rebound.__kwdefaults__ = _attr.__kwdefaults__
            _rebound.__doc__ = _attr.__doc__
            globals()[_name] = _rebound


import apps.cli.tool_executor as _tool_executor_module
_rebind_module_function_globals(_tool_executor_module, _tool_executor_module.__all__)

# _test_api_key 同理：model_cmds.py 的 mixin 用裸名调用它。
import apps.cli.provider_endpoints as _provider_endpoints_module
_rebind_module_function_globals(_provider_endpoints_module, ["_test_api_key"])

# ── Broker rendering ──────────────────────────────────────────────────────────
# 实现已移到 apps/cli/broker_render.py。同 football_reports：这些函数依赖本模块的
# console / HAS_RICH / Panel / rich_box，必须重绑到本模块 globals，普通 import 会
# 在运行期 NameError。broker_cmds.py 与测试都通过 aria_cli 命名空间取用。
import apps.cli.broker_render as _broker_render_module
_rebind_module_function_globals(_broker_render_module, _broker_render_module.__all__)

class SlashCommands(
    CoreCommandsMixin,BrokerCommandsMixin, CanvasCommandsMixin, BacktestCommandsMixin, AnalysisCommandsMixin, ASharePredictionCommandsMixin, DataCommandsMixin, OpsCommandsMixin, DiagnosticCommandsMixin, DiagnosticOpsCommandsMixin, UiCommandsMixin, SessionUxCommandsMixin, AuthCommandsMixin, FileCommandsMixin, FxCommodityCommandsMixin, FinanceServiceCommandsMixin, OrchestratorCommandsMixin, WorkflowCommandsMixin, BusinessWorkflowCommandsMixin, WarehouseCommandsMixin, LogisticsCommandsMixin, SessionCommandsMixin, WorkspaceCommandsMixin, ModelCommandsMixin, MarketCommandsMixin, PortfolioCommandsMixin, PdfExportCommandsMixin):
    """Claude Code-style slash command system."""


    def __init__(self, terminal: 'ArtheraTerminal'):
        self.terminal = terminal
        self.commands = {
            # ── Session ───────────────────────────────────────────────────────
            "/help":      (self.cmd_help,     "Show commands and examples"),
            "/clear":     (self.cmd_clear,    "Clear conversation"),
            "/compact":   (self.cmd_compact,  "Compress context: /compact [--hard]"),
            "/history":   (self.cmd_history,  "Show conversation history"),
            "/cost":      (self.cmd_cost,     "Token usage and estimated cost"),
            "/status":    (self.cmd_status,   "Runtime: engine · model · tools · context"),
            "/health":    (self.cmd_health,   "Check connection; --model tests text, --tools tests function calling"),
            "/trace":     (self.cmd_trace,    "Show tool call trace"),
            "/context":   (self.cmd_context,  "Show AI context and session info"),
            "/regen":     (self.cmd_regen,    "Regenerate last response"),
            "/undo":      (self.cmd_undo,     "Undo last message pair"),
            "/rewind":    (getattr(self, "cmd_rewind", self._cmd_rewind_unavailable),
                           "Restore code/chat: /rewind code|conversation|both|list, or a whole turn: /rewind turn [N]|turns|green"),
            "/fork":      (self.cmd_fork,     "Fork conversation: /fork [name]"),
            "/load-fork": (self.cmd_load_fork,"Restore forked conversation: /load-fork <id>"),
            "/copy":      (self.cmd_copy,     "Copy last response to clipboard"),
            "/recap":     (self.cmd_recap,    "Summarise this session"),
            "/btw":       (self.cmd_btw,      "Side question without adding to history"),
            # ── Sessions ─────────────────────────────────────────────────────
            "/save":      (self.cmd_save,     'Save session: /save ["name"]'),
            "/load":      (self.cmd_load,     "Load session: /load <id>"),
            "/rename":    (self.cmd_rename,   'Rename session: /rename "title"'),
            "/sessions":  (self.cmd_sessions, "List/search sessions: /sessions [keyword]"),
            "/recall":    (self.cmd_recall,   "Full-text session search: /recall <query>"),
            "/export":    (self.cmd_export,   "Export: /export json|csv|md|sft|bundle [file]"),
            "/export-pdf": (self.cmd_export_pdf, "Export a structured report to a designed PDF: /export-pdf <report.md> [--theme=institutional|bloomberg] [--sections=a,b] [--exclude=x,y]"),
            # ── Config / mode ─────────────────────────────────────────────────
            "/model":     (self.cmd_model,    "Switch AI model (interactive picker)"),
            "/thinking":  (self.cmd_thinking, "Toggle extended thinking: /thinking on|off"),
            "/config":    (self.cmd_config,   "Show/set config: /config set key=value"),
            "/permissions":(self.cmd_permissions, "Tool permissions: /permissions [allow|deny|ask|reset]"),
            "/input":     (self.cmd_input,    "UI theme: /input theme auto|dark|light"),
            "/privacy":   (self.cmd_privacy,  "Privacy: /privacy status|opt-in|opt-out"),
            "/local":     (self.cmd_local,    "Toggle local-only mode: /local [on|off]"),
            # ── Setup / discovery ────────────────────────────────────────────
            "/setup":     (self.cmd_setup,    "First-run wizard: /setup [mcp|feishu|telegram]"),
            "/apikey":    (self.cmd_apikey,   "API key wizard: /apikey [list|test|remove]"),
            "/doctor":    (self.cmd_doctor,   "Diagnose install, models, API keys"),
            "/license":   (self.cmd_license,  "Show feature license/entitlement status"),
            "/architecture": (self.cmd_architecture, "Show layered architecture contract: /architecture [--gaps]"),
            "/install":   (self.cmd_install,  "Detect & install missing deps: /install [pkg|--auto|--required]"),
            "/mcp":       (self.cmd_mcp,      "MCP servers: /mcp status|tools|reload [server]"),
            "/providers": (self.cmd_providers,"List local LLM backends and status"),
            "/collab":   (self.cmd_collab,   "Multi-model API collaboration: /collab status|use|ask"),
            "/ashare":   (self.cmd_ashare,   "A-share prediction engine: /ashare status|latest|predict|evaluate"),
            "/markets":  (self.cmd_markets,  "Financial market services: /markets [A股|港股|美股|crypto|forex|commodity]"),
            "/orchestrate": (self.cmd_orchestrate, "Task graph preview: /orchestrate <request>"),
            "/ariarc":    (self.cmd_ariarc,   "Show .ariarc project config: /ariarc [reload]"),
            "/skills":    (self.cmd_skills,   "List all available skills"),
            "/services":  (self.cmd_services, "Show service tiers and workflows"),
            "/tools":     (self.cmd_tools,    "List all Aria tools"),
            "/packages":  (self.cmd_packages, "Packages: /packages [connect arthera]"),
            "/datasource":(self.cmd_datasource,"Data source config: /datasource [test FRED]"),
            "/hooks":     (self.cmd_hooks,    "Event hooks: /hooks list|edit|run"),
            # ── Auth ─────────────────────────────────────────────────────────
            "/login":     (self.cmd_login,    "Login: /login <email>"),
            "/logout":    (self.cmd_logout,   "Logout current user"),
            "/whoami":    (self.cmd_whoami,   "Show current user and token status"),
            # ── Persistent data (direct DB writes — no LLM) ──────────────────
            "/alert":     (self.cmd_alert,    "Price alerts: /alert add AAPL gt 200 | list | delete"),
            "/journal":   (self.cmd_journal,  "Trade ledger: /journal add buy AAPL 100 150.0 | pnl"),
            "/watch":     (self.cmd_watch,    "Watchlist: /watch add AAPL | list | remove AAPL"),
            "/note":      (self.cmd_note,     "Append note to ARIA.md: /note <text>"),
            "/memory":    (self.cmd_memory,   "Memory: /memory show|add <text>|clear|search|profile"),
            "/todo":      (self.cmd_todo,     "Tasks: /todo add|done|list|clear"),
            # ── Broker / account (direct reads/connects) ─────────────────────
            "/broker":    (self.cmd_broker,   "Broker: /broker guide|doctor|services|list|connect|add"),
            "/account":   (self.cmd_account,  "Account funds: /account [broker_id]"),
            "/positions": (self.cmd_positions,"Current positions: /positions [broker_id]"),
            "/orders":    (self.cmd_orders,   "Order history: /orders [open|filled|all]"),
            "/paper":     (self.cmd_paper,    "Paper trading: /paper start|account|positions|orders|reset"),
            "/trade":     (self.cmd_trade,    "Trade preview/confirm: /trade preview ... | confirm <id>"),
            "/strategy":  (self.cmd_strategy, "Strategy vault: /strategy show [name] (overview/workspace)|save|list|diff|load|review"),
            "/deploy":    (self.cmd_deploy,  "Deploy strategy to live ledger: /deploy <name> AAPL:10 | $100000 AAPL:30% | rebalance AAPL:30% | close"),
            "/accuracy":  (self.cmd_accuracy, "Prediction track record vs live prices"),
            "/artifacts": (self.cmd_artifacts,"Manage generated files: /artifacts [limit|open|reveal|path|copy-path|stats|prune]"),
            "/canvas":    (self.cmd_canvas,   "Live preview server: /canvas [stop] — reports/charts update in a browser tab in real time"),
            "/update":    (self.cmd_update, "Check or install Aria Code: /update [--check | --to VERSION | --rollback]"),
            # ── Code & project ────────────────────────────────────────────────
            "/project":   (self.cmd_project,  "Project: /project load|tree|grep|ask|task|status"),
            "/init":      (self.cmd_init,     "Generate ARIA.md for current project: /init [--force]"),
            "/review":    (self.cmd_review,   "AI code review: /review [file] | /review --staged"),
            "/code":      (self.cmd_code,     "Generate & run code: /code <description> [--save f.py]"),
            "/scaffold":  (self.cmd_scaffold, "Scaffold: /scaffold <name> [--template strategy]"),
            "/plan":      (self.cmd_plan,     "Draft plan: /plan step1 ; step2"),
            "/run":       (self.cmd_run,      "Run shell command: /run <command>"),
            "/completions":(self.cmd_completions, "Shell completions: /completions [bash|zsh|install]"),
            "/read":      (self.cmd_read,     "Read file: /read <path> [offset] [limit]"),
            "/write":     (self.cmd_write,    "Write file: /write [--stage] <path>"),
            "/edit":      (self.cmd_edit,     "Edit file: /edit <path>"),
            "/ls":        (self.cmd_ls,       "List files: /ls [path] [pattern]"),
            "/search":    (self.cmd_search,   "Search code: /search <pattern> [path] [glob]"),
            "/verify":    (self.cmd_verify,   "Run checks: /verify [--dry-run] [paths...]"),
            "/lsp":       (self.cmd_lsp,      "Language-server diagnostics: /lsp [file|status]"),
            "/changes":   (self.cmd_changes,  "List staged file changes"),
            "/apply-change": (self.cmd_apply_change,  "Apply staged change: /apply-change <id>"),
            "/reject-change":(self.cmd_reject_change, "Reject staged change: /reject-change <id>"),
            "/apply":     (self.cmd_apply,    "Extract & save code from last response"),
            # ── Quantitative (multi-step, structured output) ──────────────────
            "/backtest":  (self.cmd_backtest, "Backtest + HTML report: /backtest momentum SPY --period 1y"),
            "/wf":        (self.cmd_walk_forward, "Walk-forward test: /wf SPY [momentum] [rolling]"),
            "/compare":   (self.cmd_compare,  "Compare strategies: /compare SPY [start] [end]"),
            "/research":  (self.cmd_research, "Market research workflow: /research AAPL"),
            "/earnings":  (self.cmd_earnings_workflow, "Earnings review workflow: /earnings AAPL"),
            "/asset-diag": (self.cmd_asset_diag, "Asset diagnosis workflow: /asset-diag asset_000001"),
            "/contract-draft": (self.cmd_contract_draft, "Contract draft workflow: /contract-draft proj_001"),
            "/revenue-calc": (self.cmd_revenue_calc, "Revenue split workflow: /revenue-calc proj_001 200000"),
            "/risk-scan": (self.cmd_realty_risk_scan, "Realty risk scan workflow: /risk-scan proj_001"),
            "/ops-report": (self.cmd_ops_report, "Ops report workflow: /ops-report proj_001"),
            "/exit-calc": (self.cmd_exit_calc, "Exit settlement workflow: /exit-calc proj_001"),
            "/auto-strategy":(self.cmd_auto_strategy,"Auto-optimize strategy: /auto-strategy momentum SPY"),
            "/execution": (self.cmd_execution,"Algo execution compare: /execution AAPL buy 100000"),
            "/chart":     (self.cmd_chart,    "Chart artifact: /chart AAPL [period]"),
            "/tv":        (self.cmd_tv,       "TradingView chart/Pine: /tv AAPL [--open] [--analyze|--bullish|--bearish] [--pine]"),
            "/dashboard": (self.cmd_dashboard,"Dashboard artifact: /dashboard [brief|market|portfolio|full]"),
            "/report":    (self.cmd_report,   "Research report artifact: /report AAPL [--format html|md]"),
            # ── Market data / analysis (direct, no LLM loop) ────────────────
            "/quote":     (self.cmd_quote,    "Quote: /quote AAPL [MSFT...]"),
            "/analyze":   (self.cmd_analyze,  "Deep market analysis: /analyze AAPL"),
            "/team":      (self.cmd_team,     "Multi-agent research team: /team AAPL [--full]"),
            "/warehouse": (self.cmd_warehouse, "Read-only warehouse ERP analysis: /warehouse WH-CN-01 [--json]"),
            "/inventory": (self.cmd_inventory, "3PL inventory policy: reorder points, safety stock, ABC/XYZ, dead stock: /inventory skus.csv --owner ACME"),
            "/carriers": (self.cmd_carriers, "3PL carrier scorecard by lane, cost anomalies and savings: /carriers waybills.csv --owner ACME"),
            "/deep":      (self.cmd_deep,     "Deep layered research (P0–P3): /deep AAPL [--deep|--brief]"),
            "/ta":        (self.cmd_ta,       "Technical indicators: /ta AAPL [days=120]"),
            "/market":    (self.cmd_market,   "Market overview: /market [indices|sectors]"),
            "/macro":     (self.cmd_macro,    "Macro data: /macro [us|cn|rates|calendar]"),
            "/options":   (self.cmd_options,  "Options chain: /options AAPL [calls|puts]"),
            "/quality":   (self.cmd_quality,  "Quality scores: /quality AAPL"),
            "/ichimoku":  (self.cmd_ichimoku, "Ichimoku analysis: /ichimoku AAPL"),
            "/feargreed": (self.cmd_fear_greed, "Crypto fear & greed index"),
            "/football":  (self.cmd_football, "Football prediction: /football home vs away"),
            "/screen":    (self.cmd_screen,   "Stock screener: /screen ..."),
            "/screen-cn": (self.cmd_screen_cn, "A-share screener: /screen-cn ..."),
            "/limitup":   (self.cmd_limitup,  "A-share limit-up pool: /limitup [date]"),
            "/north":     (self.cmd_north,    "Northbound flow: /north"),
            "/news":      (self.cmd_news,     "Market news: /news AAPL"),
            # ── Strategy / plan workflows (declared in usage hints, now wired) ──
            "/apply-plan":   (self.cmd_apply_plan,   "Execute a saved plan: /apply-plan [--resume] [--from N]"),
            "/plan-report":  (self.cmd_plan_report,  "Plan run report: /plan-report [md|json] [file] [--open]"),
            "/tasks":        (self.cmd_tasks,         "Background tasks: /tasks [list|cancel <id>]"),
            "/task":         (self.cmd_task,          "Current coding task's worktree: /task [diff|apply|discard]"),
            "/impact":       (self.cmd_impact,        "What a change reaches: /impact <path|symbol> [...]"),
            "/workflow":     (self.cmd_workflow,      "Project workflows in .aria/workflows: /workflow list|show|run|new <name>"),
            "/delegate":     (self.cmd_delegate,      'Delegate to another agent CLI: /delegate claude|codex "<prompt>"'),
            "/canva":        (self.cmd_canva,         "Canva Connect: /canva connect <client_id> <client_secret> | status"),
            "/optimize-port":(self.cmd_optimize_port,"Portfolio optimization: /optimize-port [SYMBOL...]"),
            "/factor-lab":   (self.cmd_factor_lab,   "Factor lab: /factor-lab [SYMBOL]"),
            "/stat-arb":     (self.cmd_stat_arb,     "Statistical arbitrage: /stat-arb SYMBOL_A SYMBOL_B [period=2y]"),
            "/screenshot":   (self.cmd_screenshot,   "Desktop screenshot: /screenshot [monitor]"),
            # ── UI generation (sets Bloomberg design context) ─────────────────
            "/ui":        (self.cmd_ui,       "Generate Bloomberg-style HTML: /ui <description>"),
            "/cloud":     (self.cmd_cloud,    "Aliyun config: /cloud status|set|data|token|health"),
            "/vision":    (self.cmd_vision,   "Load image for visual analysis: /vision <path|url|clipboard>"),
            "/upload-image": (self.cmd_vision, "Upload image for visual analysis: /upload-image <path|url|clipboard>"),
            "/file":      (self.cmd_file,     "File analysis: /file load|analyze|ask|list|clear"),
            # ── Feedback ─────────────────────────────────────────────────────
            "/bug":       (self.cmd_bug,      "Report issue locally: /bug <description>"),
            "/feedback":  (self.cmd_feedback, "Rate response: /feedback good|bad|note <text>"),
            # ── Analysis / data commands with dedicated deterministic handlers ──
            # These have real cmd_* handlers; without registration they fell
            # through to the LLM (which hallucinated, e.g. /team → fake roster).
            "/portfolio": (self.cmd_portfolio,"Portfolio: /portfolio [analyze|rebalance] [SYMBOL...] | holdings (按策略分组持仓看板)"),
            "/realty":    (self.cmd_realty,   "Real-estate data: /realty market|reit|valuation|compare"),
            "/crypto":    (self.cmd_crypto,   "Crypto data: /crypto BTC ETH | /crypto account"),
            "/risk":      (self.cmd_risk,     "Risk metrics: /risk AAPL | /risk portfolio"),
            "/corr":      (self.cmd_corr,     "Correlation matrix: /corr AAPL MSFT TSLA [1y]"),
            "/factors":   (self.cmd_factors,  "Factor analysis: /factors AAPL"),
            "/peer":      (self.cmd_peer,     "Peer valuation: /peer <symbol> [peers...]"),
            "/optimize":  (self.cmd_optimize, "Portfolio optimization: /optimize [symbols...]"),
            "/signal":    (self.cmd_signal,   "ML signal: /signal <symbol>"),
            "/predict":   (self.cmd_predict,  "ML return predictions: /predict <symbols...>"),
            "/forex":     (self.cmd_forex,    "Forex rates: /forex EUR/USD USD/CNY"),
            "/commodity": (self.cmd_commodity,"Commodities: /commodity gold oil silver"),
            "/funding":   (self.cmd_funding,  "Perp funding rates: /funding [compare] [BTC ETH]"),
            "/indices":   (self.cmd_indices,  "Global indices real-time: /indices"),
            "/hot":       (self.cmd_hot,      "Hot/active stocks: /hot [cn|us] [top=20]"),
            "/stress":    (self.cmd_stress,   "Stress test: /stress <strategy> [symbol]"),
            "/edgar":     (self.cmd_edgar,    "SEC EDGAR filings: /edgar <ticker|query>"),
            "/compliance":(self.cmd_compliance,"Compliance check: /compliance <strategy>"),
            "/data":      (self.cmd_data,     "DuckDB SQL/Excel: /data sql \"...\" | export | load"),
            "/longterm":  (self.cmd_longterm, "A股长线分析（月线，3-18个月）: /longterm <代码>"),
            "/shortterm": (self.cmd_shortterm,"A股短线分析（日线，3-15日）: /shortterm <代码>"),
            "/git":       (self.cmd_git,      "Git helper: /git <status|log|diff|...>"),
            "/gh":        (self.cmd_gh,       "GitHub CLI: /gh prs|issues|pr N|issue N|search"),
            "/browser":   (self.cmd_browser,  "Open URL in headless browser: /browser <url>"),
        }
        # ── Visible commands: shown in /help (session/config/state management only)
        # All other commands still work when typed — just not cluttering /help.
        # Analysis, data, and market queries are handled by the LLM via tool calling.
        self._visible_cmds = set(VISIBLE_SLASH_COMMANDS)

        # Register skills as slash commands
        self.skill_map = {}
        for skill in SKILLS:
            self.skill_map[skill["command"]] = skill



    # Per-command detailed help: (usage, examples)
    _COMMAND_HELP = {
        "/quote":     ("Usage: /quote [SYMBOL...]", ["/quote AAPL", "/quote AAPL MSFT GOOGL", "/quote  (uses watchlist)"]),
        "/analyze":   ("Usage: /analyze [SYMBOL]", ["/analyze AAPL", "/analyze TSLA"]),
        "/backtest":  ("Usage: /backtest [strategy] [symbol] [start] [end] [--period 1y] [--fast 20 --slow 60] [--output ./aria-output]", ["/backtest momentum SPY --period 1y", "/backtest sma_cross AAPL --fast 20 --slow 60 --output ./reports"]),
        "/wf":        ("Usage: /wf [symbol] [strategy] [method]", ["/wf SPY momentum rolling", "/wf QQQ breakout anchored"]),
        "/compare":   ("Usage: /compare [symbol] [start] [end]", ["/compare SPY", "/compare AAPL 2022-01-01 2025-01-01"]),
        "/tv":        ("Usage: /tv SYMBOL [--open] [--interval 60] [--analyze|--bullish|--bearish] [--pine] [--copy] [--reveal] [--txt]", ["/tv NVDA", "/tv NVDA --open --bullish", "/tv NVDA --pine --copy --reveal"]),
        "/watch":     ("Usage: /watch [add|remove|list] [SYMBOL]", ["/watch add AAPL", "/watch remove TSLA", "/watch list"]),
        "/crypto":    ("Usage: /crypto [SYMBOL...]", ["/crypto BTC", "/crypto ETH SOL"]),
        "/forex":     ("Usage: /forex [PAIR...]", ["/forex EUR/USD", "/forex GBP/USD JPY/USD"]),
        "/commodity": ("Usage: /commodity [NAME...]", ["/commodity gold", "/commodity oil silver"]),
        "/risk":      ("Usage: /risk [SYMBOL|portfolio]", ["/risk AAPL", "/risk portfolio"]),
        "/market":    ("Usage: /market [indices|sectors]", ["/market", "/market sectors"]),
        "/optimize":  ("Usage: /optimize [SYMBOL...]", ["/optimize AAPL MSFT GOOGL", "/optimize  (uses watchlist)"]),
        "/stress":    ("Usage: /stress [strategy] [symbol]", ["/stress momentum SPY"]),
        "/factors":   ("Usage: /factors [SYMBOL]", ["/factors AAPL"]),
        "/compliance":("Usage: /compliance [strategy]", ["/compliance momentum"]),
        "/web":       ("Usage: /web <query>", ["/web AAPL earnings Q4 2025", "/web Fed rate decision"]),
        "/services":  ("Usage: /services", ["/services"]),
        "/plan":      ("Usage: /plan <step1 ; step2 ; step3>", ["/plan git status ; rg TODO src ; pytest -q"]),
        "/apply-plan":("Usage: /apply-plan [--resume] [--from N]", ["/apply-plan", "/apply-plan --resume", "/apply-plan --from 2"]),
        "/plan-report":("Usage: /plan-report [md|json] [file] [--open]", ["/plan-report", "/plan-report md plan_report.md --open", "/plan-report json plan_report.json"]),
        "/git":       ("Usage: /git [status|diff|summary|patch|commit <msg>]", ["/git status", "/git patch apps/cli/aria_cli.py", '/git commit "feat: improve planner"']),
        "/gh":        ("Usage: /gh [prs|issues|pr N|issue N|search <q>|create-pr]", ["/gh prs", "/gh issues", "/gh pr 42", "/gh search 'async def'", "/gh create-pr"]),
        "/verify":    ("Usage: /verify [--dry-run] [path...]", ["/verify --dry-run", "/verify aria_cli.py", "/verify src/App.tsx"]),
        "/changes":   ("Usage: /changes [--all]", ["/changes", "/changes --all"]),
        "/apply-change": ("Usage: /apply-change <change_id>", ["/apply-change abc123"]),
        "/reject-change": ("Usage: /reject-change <change_id>", ["/reject-change abc123"]),
        "/news":      ("Usage: /news [topic|symbol]", ["/news", "/news AAPL", "/news technology"]),
        "/config":    ("Usage: /config [show] | /config set key=value", ["/config", "/config set model=aria-sonata:4.5", "/config set notify_webhook=https://...", "/config set brave_key=BSAAxxx"]),
        "/input":     ("Usage: /input [panel|box|plain|reset] | /input theme auto|dark|light", ["/input", "/input panel", "/input theme auto"]),
        "/privacy":   ("Usage: /privacy [status|opt-in|opt-out|export [path]|delete]", ["/privacy", "/privacy opt-in", "/privacy export"]),
        "/context":   ("Usage: /context", ["/context"]),
        "/trace":     ("Usage: /trace [--json]", ["/trace", "/trace --json"]),
        "/model":     ("Usage: /model [name|number|id]", ["/model", "/model qwen7b", "/model 2", "/model qwen2.5:7b"]),
        "/thinking":  ("Usage: /thinking [on|off|auto]", ["/thinking on", "/thinking off"]),
        "/login":     ("Usage: /login <email>  (password prompted securely)", ["/login user@example.com"]),
        "/whoami":    ("Usage: /whoami", ["/whoami"]),
        "/export":    ("Usage: /export [json|csv|md|sft|bundle] [file]", ["/export bundle", "/export md report.md"]),
        "/export-pdf": ("Usage: /export-pdf <report.md> [--theme=institutional|bloomberg] [--sections=a,b] [--exclude=x,y]",
                        ["/export-pdf research/shortterm/reports/shortterm_2026-07-17.md",
                         "/export-pdf report.md --theme=bloomberg --exclude=新闻面"]),
        "/save":      ("Usage: /save [name]", ["/save", '/save "AAPL Strategy Research"']),
        "/load":      ("Usage: /load <session_id>", ["/load abc123"]),
        "/sessions":  ("Usage: /sessions", ["/sessions"]),
        "/clear":     ("Usage: /clear", ["/clear"]),
        "/btw":       ("Usage: /btw <question>  (ephemeral — not added to history)", ["/btw what was the variable name?", "/btw which file has the config?"]),
        "/recap":     ("Usage: /recap  (session summary)", ["/recap"]),
        "/code":      ("Usage: /code <description> [--save file.py]", ["/code AAPL momentum backtest --save bt.py"]),
        "/write":     ("Usage: /write [--stage] <file_path>", ["/write report.py", "/write --stage strategy.py"]),
        # ── Financial analysis ──────────────────────────────────────────────
        "/team":      ("Usage: /team [SYMBOL] [--agents a,b] [--full]", ["/team NVDA", "/team AAPL --agents technical,risk", "/team watchlist", "/team SPY --full"]),
        "/warehouse": ("Usage: /warehouse <warehouse_id> [--json]", ["/warehouse WH-CN-01", "/warehouse WH-CN-01 --json"]),
        "/inventory": ("Usage: /inventory <skus.csv|json> [--owner ID | --all-owners] [--service-level 0.95] [--review-days 7] [--json]",
                       ["/inventory skus.csv --owner ACME", "/inventory skus.json --owner ACME --service-level 0.98"]),
        "/carriers": ("Usage: /carriers <waybills.csv|json> [--owner ID | --all-owners] [--min-shipments 20] [--json]",
                      ["/carriers waybills.csv --owner ACME", "/carriers waybills.csv --all-owners"]),
        "/deep":      ("Usage: /deep [SYMBOL] [--brief|--deep] [--agents a,b]", ["/deep NVDA", "/deep AAPL --deep", "/deep 000333 --brief", "/deep TSLA --agents technical,risk,macro", "/deep calibrate"]),
        "/architecture": ("Usage: /architecture [--gaps]", ["/architecture", "/architecture --gaps"]),
        "/ta":        ("Usage: /ta [SYMBOL] [days=N]", ["/ta AAPL", "/ta NVDA days=60"]),
        "/signal":    ("Usage: /signal [SYMBOL] [market]", ["/signal AAPL", "/signal sh600519 CN"]),
            "/predict":   ("Usage: /predict [SYMBOL...]", ["/predict sh600519 sh601318"]),
            "/research":  ("Usage: /research [topic or symbol]", ["/research NVDA AI chips", "/research 600519"]),
            "/earnings":  ("Usage: /earnings [SYMBOL]", ["/earnings AAPL", "/earnings TSLA"]),
            "/asset-diag": ("Usage: /asset-diag <asset_id>", ["/asset-diag asset_000001"]),
            "/contract-draft": ("Usage: /contract-draft <project_id>", ["/contract-draft proj_001"]),
            "/revenue-calc": ("Usage: /revenue-calc <project_id> <gross> [refunds]", ["/revenue-calc proj_001 200000"]),
            "/risk-scan": ("Usage: /risk-scan [project_id]", ["/risk-scan proj_001"]),
            "/ops-report": ("Usage: /ops-report [project_id]", ["/ops-report proj_001"]),
            "/exit-calc": ("Usage: /exit-calc <project_id> [--reason <reason>]", ["/exit-calc proj_001"]),
            "/chart":     ("Usage: /chart [SYMBOL] [period]", ["/chart AAPL", "/chart NVDA 6mo"]),
        "/options":   ("Usage: /options [SYMBOL]", ["/options AAPL", "/options SPY"]),
        "/macro":     ("Usage: /macro [topic]", ["/macro", "/macro fed rates"]),
        "/peer":      ("Usage: /peer [SYMBOL]", ["/peer AAPL", "/peer TSLA"]),
        "/corr":      ("Usage: /corr [SYMBOL...]", ["/corr AAPL MSFT NVDA", "/corr  (uses watchlist)"]),
        "/report":    ("Usage: /report [SYMBOL] [--format html|md] [--output ./aria-output]", ["/report AAPL", "/report SPY --format md --output ./reports"]),
        "/dashboard": ("Usage: /dashboard  — 生成含持仓/行情/预警的个人化 HTML Dashboard，自动在浏览器打开", ["/dashboard"]),
        "/ui":        ("Usage: /ui <描述>  — 生成 Bloomberg Terminal 风格 HTML (自动暗色/亮色模式)", ["/ui 今日A股热力图", "/ui 持仓组合报告", "/ui 市场晨报看板"]),
        "/artifacts": ("Usage: /artifacts [limit|open|reveal|path|copy-path|stats|prune]", ["/artifacts", "/artifacts open latest", "/artifacts reveal 2", "/artifacts copy-path 1", "/artifacts stats", "/artifacts prune 20"]),
        "/shortterm": ("Usage: /shortterm [SYMBOL]", ["/shortterm AAPL", "/shortterm sh600519"]),
        "/longterm":  ("Usage: /longterm [SYMBOL]", ["/longterm AAPL", "/longterm sh600519"]),
        # ── China market ────────────────────────────────────────────────────
        "/screen-cn": ("Usage: /screen-cn [criteria]", ["/screen-cn momentum", "/screen-cn value"]),
        "/limitup":   ("Usage: /limitup", ["/limitup"]),
        "/north":     ("Usage: /north", ["/north"]),
        "/hot":       ("Usage: /hot [sector]", ["/hot", "/hot tech"]),
        "/indices":   ("Usage: /indices", ["/indices"]),
        # ── Portfolio & journal ─────────────────────────────────────────────
        "/portfolio": ("Usage: /portfolio [analyze|rebalance] [SYMBOL...]", ["/portfolio", "/portfolio analyze AAPL MSFT TSLA", "/portfolio rebalance"]),
        "/journal":   ("Usage: /journal [add|trades|pnl|realized|export|delete]", ["/journal", "/journal add buy AAPL 100 185.50", "/journal pnl", "/journal realized", "/journal export"]),
        "/optimize-port": ("Usage: /optimize-port [SYMBOL...]", ["/optimize-port AAPL MSFT NVDA"]),
        # ── Alerts & screening ──────────────────────────────────────────────
        "/alert":     ("Usage: /alert [add|list|delete|check] [SYMBOL] [gt|lt] [price]", ["/alert add AAPL gt 200", "/alert list", "/alert check", "/alert delete 1"]),
        "/screen":    ("Usage: /screen [criteria]", ["/screen tech growth", "/screen value dividend"]),
        "/watchlist-scan": ("Usage: /watchlist-scan", ["/watchlist-scan"]),
        # ── Real estate ─────────────────────────────────────────────────────
        "/realty":    ("Usage: /realty [market CITY] [calc buy|rent|roi] [compare] [trend CITY]", ["/realty market 北京", "/realty calc buy", "/realty compare", "/realty trend 上海"]),
        # ── Brokers ─────────────────────────────────────────────────────────
        "/broker":    ("Usage: /broker [guide|doctor|services|list|connect NAME|disconnect|status]", ["/broker guide", "/broker doctor", "/broker connect alpaca_paper"]),
        "/paper":     ("Usage: /paper [start CASH CURRENCY|account|positions|orders|reset]", ["/paper start 100000 USD", "/paper account", "/paper orders"]),
        "/trade":     ("Usage: /trade [mode|preview SYMBOL buy|sell QTY PRICE|confirm PREVIEW_ID|previews]", ["/trade mode", "/trade preview AAPL buy 10 190", "/trade confirm tp_xxx"]),
        "/account":   ("Usage: /account", ["/account"]),
        "/positions": ("Usage: /positions", ["/positions"]),
        "/orders":    ("Usage: /orders [pending|all]", ["/orders", "/orders pending"]),
        # ── Utilities ───────────────────────────────────────────────────────
        "/vision":      ("Usage: /vision <image_path|image_url|clipboard>", ["/vision ~/Pictures/chart.png", "/vision https://example.com/chart.png", "/vision clipboard"]),
        "/upload-image": ("Usage: /upload-image <image_path|image_url|clipboard>", ["/upload-image ~/Pictures/chart.png", "/upload-image clipboard"]),
        "/browser":     ("Usage: /browser <url>  or  /browser screenshot <url>", ["/browser https://example.com", "/browser screenshot https://github.com"]),
        "/screenshot":  ("Usage: /screenshot [monitor]", ["/screenshot", "/screenshot 1"]),
        "/memory":    ("Usage: /memory [show|add|clear|search]", ["/memory show", "/memory add 我偏好技术分析", "/memory search 风险偏好"]),
        "/project":   ("Usage: /project [load|analyze|files|symbols|tasks|status|close]", ["/project load .", "/project analyze", "/project files", "/project tasks"]),
        "/mcp":       ("Usage: /mcp [list|connect|disconnect|tools]", ["/mcp list", "/mcp tools"]),
        "/skills":    ("Usage: /skills", ["/skills"]),
        "/tools":     ("Usage: /tools [list|call TOOL_NAME]", ["/tools", "/tools list"]),
        "/data":      ("Usage: /data [SYMBOL] [field]", ["/data AAPL", "/data sh600519 history"]),
        "/apikey":    ("Usage: /apikey → 向导  /apikey list → 查看  /apikey test <p> → 测试  /apikey set <p> <k>", ["/apikey", "/apikey list", "/apikey test deepseek", "/apikey set openai sk-..."]),
        "/ariarc":    ("Usage: /ariarc [show|init|set key=val]", ["/ariarc show", "/ariarc init", "/ariarc set default_symbols=AAPL,MSFT"]),
        "/setup":     ("Usage: /setup [mcp|broker|keys|feishu|telegram|all]", ["/setup", "/setup mcp", "/setup feishu", "/setup telegram"]),
        "/doctor":    ("Usage: /doctor", ["/doctor"]),
        "/history":   ("Usage: /history [N]", ["/history", "/history 20"]),
        "/compact":   ("Usage: /compact", ["/compact"]),
        "/note":      ("Usage: /note [list|add|delete N]", ["/note add 重要观察点", "/note list", "/note delete 1"]),
        "/todo":      ("Usage: /todo [add|done|list|clear] [text]", ["/todo add 分析NVDA", "/todo list", "/todo done 1"]),
        "/copy":      ("Usage: /copy [N]", ["/copy", "/copy 3"]),
        "/bug":       ("Usage: /bug <description>", ["/bug 行情数据没刷新", "/bug 期权定价报错"]),
        "/read":      ("Usage: /read <file_path>", ["/read strategy.py", "/read data/prices.csv"]),
        "/edit":      ("Usage: /edit <file_path>", ["/edit strategy.py"]),
        "/run":       ("Usage: /run <command>", ["/run python strategy.py", "/run pytest -q"]),
        "/ls":        ("Usage: /ls [path]", ["/ls", "/ls src/"]),
        "/search":    ("Usage: /search <query>", ["/search AAPL earnings", "/search momentum strategy"]),
        "/local":     ("Usage: /local [on|off|status]", ["/local", "/local on", "/local off"]),
        "/providers": ("Usage: /providers", ["/providers"]),
        "/feargreed": ("Usage: /feargreed", ["/feargreed"]),
        "/funding":   ("Usage: /funding [compare] [SYMBOL] [exchange]", ["/funding BTC", "/funding compare BTC ETH SOL", "/funding ETH bybit"]),
        "/quality":   ("Usage: /quality [SYMBOL]", ["/quality AAPL", "/quality 600519"]),
        "/ichimoku":  ("Usage: /ichimoku [SYMBOL]", ["/ichimoku AAPL", "/ichimoku USDJPY"]),
        "/factor-lab":("Usage: /factor-lab [SYMBOL]", ["/factor-lab AAPL", "/factor-lab sh600519"]),
        "/execution": ("Usage: /execution SYMBOL buy|sell QTY [algo=compare] [price=N]", ["/execution AAPL buy 100000", "/execution SPY sell 50000 algo=is"]),
        "/stat-arb":  ("Usage: /stat-arb SYMBOL_A SYMBOL_B [period=2y]", ["/stat-arb GLD SLV", "/stat-arb SPY QQQ period=1y"]),
        "/edgar":     ("Usage: /edgar SYMBOL [filings|facts|insider]", ["/edgar AAPL", "/edgar MSFT facts", "/edgar TSLA insider"]),
        "/datasource":("Usage: /datasource | /datasource test SOURCE | /datasource config", ["/datasource", "/datasource test fred", "/datasource config"]),
        "/sector-rotation": ("Usage: /sector-rotation", ["/sector-rotation"]),
        "/auto-strategy":   ("Usage: /auto-strategy [objective] [SYMBOL...]", ["/auto-strategy momentum AAPL", "/auto-strategy mean_reversion SPY"]),
        "/morning-brief":   ("Usage: /morning-brief", ["/morning-brief"]),
        "/deep-analysis":   ("Usage: /deep-analysis [SYMBOL]", ["/deep-analysis NVDA"]),
        "/trade-idea":      ("Usage: /trade-idea [SYMBOL]", ["/trade-idea AAPL"]),
        "/review":          ("Usage: /review [file_or_code]", ["/review strategy.py", "/review"]),
        "/init":            ("Usage: /init [template]", ["/init", "/init quant"]),
        "/scaffold":        ("Usage: /scaffold [type] [name]", ["/scaffold strategy momentum", "/scaffold agent news"]),
        "/cost":            ("Usage: /cost [session|total|reset]", ["/cost", "/cost session", "/cost reset"]),
        "/rename":          ("Usage: /rename <new_name>", ['/rename "NVDA Research Session"']),
        "/feedback":        ("Usage: /feedback <message>", ["/feedback 分析结果不够准确"]),
        "/hooks":           ("Usage: /hooks [list|enable|disable]", ["/hooks list", "/hooks enable pre_trade"]),
        "/logout":          ("Usage: /logout", ["/logout"]),
        "/status":          ("Usage: /status", ["/status"]),
        "/health":          ("Usage: /health [--model] [--tools] [--json]", ["/health", "/health --model", "/health --model --tools --json"]),
    }



    # ────────────────────────────────────────────────────────────────────────
    # New Industry Commands
    # ────────────────────────────────────────────────────────────────────────






    # ── /realty 不动产命令 ─────────────────────────────────────────────────────
    # ── /football 足球分析命令 ────────────────────────────────────────────────

    # ── /data 数据分析命令 ─────────────────────────────────────────────────────
























    # ── Project scaffold templates ────────────────────────────────────────────

    # Scaffold templates moved to apps.cli.commands.scaffold_templates
    from apps.cli.commands.scaffold_templates import SCAFFOLD_TEMPLATES as _SCAFFOLD_TEMPLATES  # noqa



    # ---- Provider / API Key management (Open Interpreter style) ----



    # ---- Code generation command ----


    # ---- Scaffold command ----
    # ---- Feedback command ----



    # ---- Market data commands (expose unused Aria tools) ----














    # ---- Local mode toggle ----


    # ---- Models list ----

    # ---- MCP server management ----


    # ---- .ariarc project config ----



    # ---- Local LLM provider status ----
    # ---- Alibaba Cloud data service config ----
    # ---- AI Signal from cloud ----


    # ---- ML Predictions from cloud ----


    # ---- Cloud backtest ----


    # ---- Market insights ----


    # ---- Recommend local models ----


    # ---- Finance local tool shortcuts ----





    # ════════════════════════════════════════════════════════════════════════
    # 金融 Agent 团队命令
    # ════════════════════════════════════════════════════════════════════════







    # ════════════════════════════════════════════════════════════════════════
    # 策略金库命令
    # ════════════════════════════════════════════════════════════════════════

    # ---- ORCL analysis ----





    # ---- News command ----
    # ── /file 多格式文件分析命令 ──────────────────────────────────────────────
    # ── /project — Claude Code style project folder analysis ─────────────────


















    @property
    def context(self):
        """Delegate to the terminal's AriaContext (mixins call self.context)."""
        return self.terminal.context














# ── 经营权共创平台：Agent 输出辅助函数（模块级，SlashCommands 内外均可用）────────────








# ============================================================================
# Main Terminal — Claude Code-like REPL
# ============================================================================

from aria_code.apps.cli.context import AriaContext
from aria_code.apps.cli.chat_turn import ChatTurnMixin as _ChatTurnMixin
from aria_code.apps.cli.terminal_chrome import ChromeMixin as _ChromeMixinSource
from aria_code.apps.cli.headless import HeadlessMixin as _HeadlessMixinSource
from aria_code.apps.cli.mixin_binding import bind_mixin as _bind_mixin

# Blocks of ArtheraTerminal kept in their own modules, bound to this module's
# names: a chat turn (apps/cli/chat_turn.py), the banner, status line,
# toolbar and keys (apps/cli/terminal_chrome.py), and -p and --watch
# (apps/cli/headless.py).
_ChatTurn = _bind_mixin(_ChatTurnMixin, globals())
_Chrome = _bind_mixin(_ChromeMixinSource, globals())
_HeadlessMixin = _bind_mixin(_HeadlessMixinSource, globals())


class ArtheraTerminal(_ChatTurn, _Chrome, _HeadlessMixin):
    """Interactive REPL inspired by Claude Code CLI."""

    def __init__(self, config: dict):
        self.config = config
        self._session_banner_mode: Optional[str] = None
        self.context = AriaContext(
            console=globals().get('console'),
            config=config,
            workspace=globals().get('workspace_ctx'),
            save_config_cb=save_config,
            load_config_cb=load_config
        )
        _sync_write_policy(config)  # ensure module-level policy matches loaded config
        self.api_url = config.get("api_url", DEFAULT_CONFIG["api_url"])
        self.conversation: List[dict] = []
        self.running = True
        self.session_id = config.get("last_session_id") or str(uuid.uuid4())[:8]
        self.session_mgr = SessionManager(SESSIONS_DIR)
        self._run_store: Optional[RunStore] = None
        self._active_run_id: Optional[str] = None
        try:
            self._run_store = RunStore()
            self._run_store.recover_orphaned_runs()
        except Exception as exc:
            logger.debug("Durable run store unavailable: %s", exc)
        # JSONL session store: crash-safe, append-per-turn
        try:
            from apps.cli.session_jsonl import JsonlSessionStore
            self._jsonl_store: Optional[Any] = JsonlSessionStore()
            self._jsonl_store.init_session(self.session_id)
        except Exception:
            self._jsonl_store = None
        # Fire SessionStart hook
        if _HAS_JSON_HOOKS:
            try:
                _fire_json_hook("SessionStart", session_id=self.session_id, hooks=_JSON_HOOKS)
            except Exception:
                pass
        _run_event_hook("session_start", {"ARIA_SESSION": self.session_id})
        # Refresh project context at session start (pick up ARIA.md changes)
        _refresh_project_context()
        self.pending_plan: List[str] = []
        self.last_plan_results: List[dict] = []
        try:
            # event_sink lands with the durable run-state store
            # (runtime/events.py); older RuntimeTrace() takes no arguments.
            self.runtime_trace = RuntimeTrace(event_sink=self._persist_runtime_event)
        except TypeError:
            self.runtime_trace = RuntimeTrace()
        try:
            self.tool_executor = ToolExecutor(
                LOCAL_TOOLS,
                hook=_run_hook,
                trace=self.runtime_trace,
                config=self.config,
                execution_context=lambda: {
                    "_run_id": self._active_run_id,
                    "_session_id": self.session_id,
                },
            )
        except TypeError:
            # execution_context lands with the durable run-state store too.
            self.tool_executor = ToolExecutor(
                LOCAL_TOOLS,
                hook=_run_hook,
                trace=self.runtime_trace,
                config=self.config,
            )
        self.cancel_event: Optional[asyncio.Event] = None
        self._streaming = False
        self._last_provider = ""   # last successful provider ("" = no message sent yet)

        # ── Wire subagent runner so spawn_task can use the same LLM ─────────
        try:
            from runtime.subagent import (
                register_runner as _register_subagent_runner,
                restore_tasks as _restore_subagent_tasks,
            )
            _terminal_ref = self

            async def _subagent_runner(prompt: str, task=None) -> str:
                """Run a task through the chat runtime, in the task's own workspace."""
                from apps.cli.providers.runtime_bridge import run_subagent_turn

                return await run_subagent_turn(
                    prompt, task, local_tools=LOCAL_TOOLS, tool_schemas=LOCAL_TOOL_SCHEMAS,
                    config=_terminal_ref.config, api_url=_terminal_ref.api_url,
                )

            _register_subagent_runner(_subagent_runner)
            _restore_subagent_tasks()
        except Exception:
            pass
        self._actual_model: Optional[str] = None  # actual Ollama model in use (may differ from config)
        self._ollama_alive = False                # set by print_header / health check
        self._installed_models: set = set()       # installed Ollama models (from header detection)
        self._auto_healed_from: Optional[str] = None  # original model if auto-paired at startup

        # ── Session-level telemetry (like Claude Code's /cost) ──────────
        import time as _time_mod
        self._session_start: float = _time_mod.time()
        self._session_input_tokens: int = 0   # prompt tokens this session
        self._session_output_tokens: int = 0  # completion tokens this session
        self._session_thinking_tokens: int = 0
        self._session_turns: int = 0           # number of exchange pairs
        self._last_response: str = ""          # last assistant message text (for /copy)
        self._last_market_snapshot_cache: Optional[dict] = None
        self._last_turn_envelope: Optional[AgentTurnEnvelope] = None
        self._forks: List[dict] = []           # forked conversation snapshots
        self._pending_image: Optional[dict] = None  # pending vision content block
        self._pending_market_artifact: Optional[dict] = None
        self._pending_market_resolution: Optional[dict] = None
        self._last_preflight_key: str = ""
        self._auto_compact_count: int = 0
        # /file initializes its session when requested. Importing FileSession
        # here also loads PDF, Excel and pandas parsers before the first prompt.
        self._file_session: Optional[Any] = None

        # ── Project folder analysis session (Claude Code style) ──────────────
        self._project_session: Optional[Any] = None  # set by /project load
        self._project_ctx_injected: bool = False

        # ── ariarc: project-level context injection ──────────────────────
        self.ariarc: Optional[Any] = None
        if _HAS_ARIARC:
            try:
                self.ariarc = get_ariarc()
                if self.ariarc.found:
                    logger.info("ariarc loaded from %s", self.ariarc.source_path)
            except Exception as _exc:
                logger.debug("ariarc load error: %s", _exc)

        # ── MCP registry placeholder (started async in run_interactive) ──
        self._mcp_registry: Optional[Any] = None
        self._mcp_started = False
        self._mcp_connection_notice_shown = False
        self._pending_notifications: list = []  # printed before next input cycle to avoid corrupting pt UI

        # ── Global user memory ────────────────────────────────────────────
        try:
            from memory_manager import MemoryManager
            self.memory_mgr: Optional[Any] = MemoryManager()
        except Exception:
            self.memory_mgr = None

        # ``@`` is a read-only context plane. Resolution is shared with the
        # completer and slash-command executor so displayed behavior matches
        # submitted behavior.
        try:
            from apps.cli.config_paths import resolve_user_output_root
            from packages.aria_services.references import build_reference_service
            self._reference_service = build_reference_service(
                workspace=pathlib.Path.cwd(),
                output_root=resolve_user_output_root(),
            )
        except Exception:
            self._reference_service = None

        self.commands = SlashCommands(self)

        # Setup input — prefer prompt_toolkit, fallback to readline.
        # Skip interactive input setup entirely in non-interactive mode (-p flag)
        # to avoid prompt_toolkit emitting "Warning: Input is not a terminal".
        self._pt_session = None
        self._pt_completer = None
        self._pt_history = None
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

        _interactive = sys.stdin.isatty()
        # Task list state — toggled by Ctrl+T
        self._task_list_visible = False
        self._task_list: list[dict] = []
        # Transcript / tool-call log — toggled by Ctrl+O
        self._transcript_log: list[str] = []
        self._transcript_visible = False
        self._last_thinking: str = ""   # full thinking text of last turn (Ctrl+O)
        self._action_details: list = []  # every action of the last turn, in full (Ctrl+O)
        # Session recap: timestamp of last completed AI turn
        self._last_turn_ts: float = 0.0

        if HAS_PT and _interactive:
            try:
                from apps.cli.config_paths import resolve_user_output_root as _reference_output_root
                _completion_output_root = _reference_output_root()
            except Exception:
                _completion_output_root = None
            self._pt_completer = AriaPTCompleter(
                self.commands.commands, SKILLS, config.get("watchlist", []),
                workspace=pathlib.Path.cwd(),
                output_root=_completion_output_root,
                lang=config.get("ui_lang", "en"),
            )
            self._pt_history = FileHistory(str(HISTORY_FILE))
            _placeholder = (
                [("class:placeholder", "Ask Aria, edit files, run commands, or /help")]
                if config.get("input_style", "panel") == "box"
                else HTML('<style fg="#888888">Ask Aria · @ context  !cmd  /help</style>')
            )
            _kb = self._build_keybindings()
            self._pt_session = PromptSession(
                history=self._pt_history,
                completer=self._pt_completer,
                complete_while_typing=True,
                style=build_aria_pt_style(config.get("input_theme", "auto")),
                placeholder=_placeholder,
                key_bindings=_kb,
            )
        elif _interactive:
            try:
                if HISTORY_FILE.exists():
                    readline.read_history_file(str(HISTORY_FILE))
                readline.set_history_length(1000)
                completer = ArtheraCompleter(
                    list(self.commands.commands.keys()),
                    SKILLS,
                    config.get("watchlist", []),
                )
                readline.set_completer(completer.complete)
                readline.parse_and_bind("tab: complete")
                readline.set_completer_delims(" ")
            except Exception:
                pass

    def _privacy_status_label(self, rich: bool = False) -> str:
        from ui.banner import privacy_status_label as _psl
        _lang = self.config.get("ui_lang", "en") or "en"
        return _psl(self.config, rich=rich, lang=_lang)

    def _control_status_label(self, rich: bool = False) -> str:
        from ui.banner import control_status_label as _csl
        _lang = self.config.get("ui_lang", "en") or "en"
        return _csl(self.config, rich=rich, lang=_lang)

    def _ollama_status_label(self, rich: bool = False) -> str:
        from ui.banner import ollama_status_label as _osl
        _lang = self.config.get("ui_lang", "en") or "en"
        return _osl(
            getattr(self, "_ollama_alive", False),
            getattr(self, "_installed_models", set()) or set(),
            self.config,
            rich=rich,
            lang=_lang,
        )

    def _maybe_show_intent_preflight(self, message: str, *, quiet: bool = False) -> bool:
        key = message.strip()
        if not key or key == self._last_preflight_key:
            return False
        try:
            if _try_handle_strategy_advice(key).get("success"):
                return False
        except Exception:
            pass
        shown = _print_preflight_notice(key, quiet=quiet)
        if shown:
            self._last_preflight_key = key
        return shown

    def _print_reference_errors(self, prepared) -> None:
        is_zh = str(self.config.get("ui_lang", "en")).lower().startswith("zh")
        title = "上下文引用无法解析" if is_zh else "Context reference could not be resolved"
        hint = (
            "使用 @file:路径、@folder:路径、@asset:代码，输入 @ 可查看全部类型。"
            if is_zh else
            "Use @file:path, @folder:path, or @asset:symbol. Type @ to see all types."
        )
        lines = [f"{ref.raw}: {ref.error}" for ref in prepared.errors]
        if HAS_RICH:
            from rich.panel import Panel as _ReferencePanel
            from rich.markup import escape as _escape_reference_markup
            console.print(_ReferencePanel(
                "\n".join(f"[red]{_escape_reference_markup(line)}[/red]" for line in lines)
                + f"\n[dim]{_escape_reference_markup(hint)}[/dim]",
                title=title,
                border_style="red",
            ))
        else:
            print(f"{title}: " + "; ".join(lines))
            print(hint)

    def _print_reference_summary(self, prepared) -> None:
        refs = [ref for ref in prepared.references if ref.ok]
        if not refs:
            return
        labels = []
        for ref in refs[:4]:
            label = ref.path.name if ref.path is not None else ref.resolved_value
            labels.append(f"{ref.kind}:{label}")
        suffix = f" +{len(refs) - 4}" if len(refs) > 4 else ""
        lead = "已引用资源" if str(self.config.get("ui_lang", "en")).lower().startswith("zh") else "Resource referenced"
        message = f"  {lead}  ·  {' · '.join(labels)}{suffix}"
        if HAS_RICH:
            from rich.markup import escape as _escape_reference_markup
            console.print(f"[dim]{_escape_reference_markup(message)}[/dim]")
        else:
            print(message)

    async def _try_football_nl_intercept(self, message: str) -> bool:
        """Route an NL football query to the Poisson engine. Returns True if handled.

        Shared by send_message (REPL) and run_prompt (-p) so the two paths can't
        drift — this intercept was previously missing from -p mode, letting the
        LLM hallucinate match data.
        """
        if message.startswith("/"):
            return False
        try:
            from apps.cli.commands.market_cmds import (
                _is_probable_football_query as _pfq,
                _parse_nl_team_pair as _pfnl,
            )
            pair = _pfnl(message)
            if pair and _pfq(message, pair):
                if HAS_RICH:
                    console.print("\n[dim]⚽ 识别足球对阵，调用 Poisson 引擎…[/dim]")
                await self.commands.cmd_football(f"{pair[0]} vs {pair[1]} wc")
                return True
        except Exception:
            pass
        return False

    def _persist_runtime_event(self, event) -> None:
        """Append an in-memory runtime event to the active durable run."""
        if self._run_store is None or not self._active_run_id:
            return
        self._run_store.append_event(
            self._active_run_id,
            event.type,
            event.data,
            event_id=event.event_id,
            timestamp=event.timestamp,
        )

    def _begin_runtime_run(self, prompt: str) -> Optional[str]:
        """Create a queued run before planning or model execution starts."""
        if self._run_store is None:
            return None
        try:
            if self._active_run_id:
                previous = self._run_store.get_run(self._active_run_id)
                if previous and previous.status not in {
                    RunStatus.SUCCEEDED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }:
                    self._run_store.transition(
                        previous.run_id,
                        RunStatus.INTERRUPTED,
                        reason="superseded_by_new_turn",
                    )
            record = self._run_store.create_run(
                session_id=self.session_id,
                prompt=prompt,
                workspace=str(pathlib.Path.cwd()),
                provider=self._last_provider,
                metadata={
                    "model": self.config.get("model", ""),
                    "permission_mode": self.config.get("permission_mode", "workspace-write"),
                    "local_mode": bool(self.config.get("local_mode", False)),
                },
            )
            self._active_run_id = record.run_id
            return record.run_id
        except Exception as exc:
            logger.debug("Unable to create durable run: %s", exc)
            self._active_run_id = None
            return None

    def _transition_runtime_run(
        self,
        status: RunStatus,
        *,
        reason: str = "",
        error: Optional[str] = None,
        provider: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> None:
        """Move the active run through the runtime-owned state machine."""
        if self._run_store is None or not self._active_run_id:
            return
        try:
            self._run_store.transition(
                self._active_run_id,
                status,
                reason=reason,
                error=error,
                provider=provider,
                data=data,
            )
            if status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
                self._active_run_id = None
        except Exception as exc:
            logger.debug("Unable to transition durable run to %s: %s", status.value, exc)

    def _record_feedback(self, rating: str, message: str, comment: str = "") -> None:
        """Record an implicit/explicit feedback signal — Claude Code-style.

        Captures the signals the user already produces by acting (tool
        accept/reject, /copy) plus /bug reports. Local-first: written to
        ~/.arthera/feedback/feedback.jsonl and NEVER uploaded unless the user
        opted in via /privacy. Honors the ARIA_NO_TELEMETRY kill switch and
        never raises into the main flow.
        """
        import os as _os
        if _os.environ.get("ARIA_NO_TELEMETRY"):
            return
        try:
            from aria_code.safety import SafetyService
            _settings = SafetyService(self.config).privacy()
            _rec = FeedbackRecord.create(
                rating=rating,
                message=(message or "")[:500],
                comment=(comment or "")[:1000],
                model=self.config.get("model", ""),
                session_id=getattr(self, "session_id", ""),
                shared=_settings.data_sharing and _settings.feedback_upload,
            )
            FeedbackStore(CONFIG_DIR).append(_rec)
        except Exception:
            pass  # feedback must never break the chat flow

    def _record_prediction(self, symbol: str, response_text: str,
                           entry_price: float = 0.0) -> None:
        """Record an LLM market call for later outcome verification.

        The market is the objective judge — a correct/wrong call becomes a
        chosen/rejected DPO training signal once verified. Fetches the entry
        price itself if not given, so call sites stay one-liners. Local-first,
        honors ARIA_NO_TELEMETRY, never raises.
        """
        try:
            from apps.cli.prediction_feedback import PredictionTracker
            if not entry_price or entry_price <= 0:
                try:
                    import market_data_client as _mdc
                    q = _mdc.MarketDataClient().quote(symbol)
                    entry_price = q.get("price") if q.get("success") else 0.0
                except Exception:
                    entry_price = 0.0
            if not entry_price:
                return
            PredictionTracker(CONFIG_DIR).record(
                symbol=symbol, response_text=response_text, entry_price=entry_price,
                session_id=getattr(self, "session_id", ""),
                model=self.config.get("model", ""),
            )
        except Exception:
            pass

    def _verify_predictions(self, min_age_hours: float = 24.0) -> dict:
        """Settle pending predictions against live prices; emit DPO feedback."""
        try:
            from apps.cli.prediction_feedback import PredictionTracker

            def _quote(sym: str):
                try:
                    import market_data_client as _mdc
                    q = _mdc.MarketDataClient().quote(sym)
                    return q.get("price") if q.get("success") else None
                except Exception:
                    return None

            return PredictionTracker(CONFIG_DIR).verify_pending(
                _quote, min_age_hours=min_age_hours,
                emit_feedback=self._record_feedback,
            )
        except Exception:
            return {"settled": 0, "correct": 0, "wrong": 0}

    def _bottom_toolbar_plain(self) -> str:
        model_label, cwd, privacy, est_tokens, max_ctx = self._bottom_toolbar_parts()
        return f"{model_label} · {cwd} · {privacy} · /help · esc · {est_tokens:,}/{max_ctx:,}"

    def _bottom_toolbar_parts(self):
        from ui.banner import bottom_toolbar_parts as _btp
        # /clear、/compact 等把对话截短后,上一轮的真实 token 计数即失效
        # (自愈式判定,免去在每个重置点手工清零)。
        known = getattr(self, "_last_prompt_tokens", 0)
        if len(self.conversation) < getattr(self, "_last_prompt_tokens_msgs", 0):
            known = 0
            self._last_prompt_tokens = 0
            self._last_prompt_tokens_msgs = 0
        return _btp(
            self.conversation, self.config, self._actual_model, get_model_cfg,
            known_context_tokens=known,
        )

    async def _maybe_auto_compact_before_turn(self, incoming_content: str = "") -> bool:
        """Compact history before a request enters the model when context is hot."""
        if not bool(self.config.get("auto_compact_context", True)):
            return False
        try:
            threshold = float(self.config.get("auto_compact_threshold", 0.78))
        except Exception:
            threshold = 0.78
        try:
            from apps.cli.message_processing import context_compaction_decision
            decision = context_compaction_decision(
                self.conversation,
                model_key=self.config.get("model", "qwen2.5:7b"),
                extra_content=incoming_content,
                threshold=threshold,
            )
        except Exception:
            return False
        if not decision.get("should_compact"):
            return False

        old_pct = int(decision.get("fill_pct") or 0)
        old_count = len(self.conversation)
        try:
            await self.commands._smart_compact_async(silent=True)
        except Exception:
            try:
                max_chars = int((decision.get("max_tokens") or 16384) * 3 * 0.55)
                self.conversation = _compact_messages(
                    self.conversation,
                    max_chars=max_chars,
                    model_key=self.config.get("model", "qwen2.5:7b"),
                )
            except Exception:
                if len(self.conversation) > 10:
                    self.conversation = self.conversation[-10:]

        try:
            new_decision = context_compaction_decision(
                self.conversation,
                model_key=self.config.get("model", "qwen2.5:7b"),
                extra_content=incoming_content,
                threshold=threshold,
            )
            new_pct = int(new_decision.get("fill_pct") or 0)
        except Exception:
            new_pct = 0
        self._auto_compact_count += 1
        _run_event_hook("compact", {
            "ARIA_SESSION": self.session_id,
            "ARIA_COMPACT_MODE": "auto-preflight",
            "ARIA_CONTEXT_BEFORE": str(old_pct),
            "ARIA_CONTEXT_AFTER": str(new_pct),
            "ARIA_MESSAGES_BEFORE": str(old_count),
            "ARIA_MESSAGES_AFTER": str(len(self.conversation)),
        })
        if HAS_RICH:
            console.print(
                f"  [dim]↩ Auto-compacted context before request "
                f"({old_pct}% → {new_pct}%)[/dim]"
            )
        return True

    async def _startup_health_check(self):
        """Probe only the selected local runtime; cloud startup stays offline."""
        if not HAS_RICH:
            return
        from apps.cli.providers.chat_routing import first_round_route
        active_route = first_round_route(
            str(self.config.get("model") or ""), self.config, self.api_url,
        )
        try:
            parts = []
            if active_route == "ollama":
                import aiohttp as _aio
                ollama_url = self.config.get("ollama_url", "http://localhost:11434")
                try:
                    async with _aio.ClientSession() as s:
                        async with s.get(
                            f"{ollama_url}/api/tags",
                            timeout=_aio.ClientTimeout(total=2),
                        ) as r:
                            if r.status == 200:
                                _tags = await r.json()
                                _n = len(_tags.get("models", []))
                                self._ollama_alive = True
                                parts.append(
                                    f"[dim]Ollama · {_n} models[/dim]"
                                    if _n else "[dim]Ollama[/dim]"
                                )
                            else:
                                parts.append("[dim]Ollama offline[/dim]")
                except Exception:
                    parts.append("[dim]Ollama offline[/dim]")

            # Cloud provider check (only if API key is set)
            if self.config.get("auth_token") or os.getenv("ANTHROPIC_API_KEY"):
                parts.append("[dim]Cloud[/dim]")

            # Auto-connect default broker from ~/.arthera/brokers.json
            if _HAS_BROKERS:
                try:
                    _reg = _get_broker_registry()
                    _broker = _reg.connect_default()
                    if _broker:
                        parts.append(f"[dim]{_broker.label} · account connected[/dim]")
                except Exception as _be:
                    logger.debug("Auto-connect broker failed: %s", _be)

            # Global memory fact count
            if getattr(self, "memory_mgr", None):
                try:
                    _mcount = self.memory_mgr.fact_count()
                    if _mcount:
                        parts.append(f"[dim]memory {_mcount} facts[/dim]")
                except Exception:
                    pass

            # Broker connection shown in banner status; log remainder for debug
            if parts:
                logger.debug("startup health: %s", "  ".join(parts))
            # Broker connection shown separately (not in 5-row banner to keep it compact)
            for p in parts:
                if "account connected" in p and HAS_RICH:
                    console.print(f"  {p}")
        except ImportError:
            pass

    async def _alert_watchdog(self):
        """Background task: check price alerts every 30s and fire notifications."""
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        while self.running:
            await _asyncio.sleep(30)
            if not self.running:
                break
            try:
                from data_analysis_tools import check_alerts
                result = await loop.run_in_executor(None, check_alerts)
                triggered = result.get("triggered", [])
                if triggered:
                    for alrt in triggered:
                        sym = alrt.get("symbol", "")
                        cur = alrt.get("triggered_price", "")
                        self._pending_notifications.append(
                            f"\n[bold yellow]⚡ 预警触发[/bold yellow] [cyan]{sym}[/cyan] → {cur}"
                        )
                        try:
                            from notification_tools import send_alert_notification
                            await loop.run_in_executor(None, send_alert_notification, alrt)
                        except Exception as _ne:
                            logger.debug("Alert notification failed: %s", _ne)
            except Exception as _we:
                logger.debug("Alert watchdog error: %s", _we)

    async def run_interactive(self):
        """Run the interactive REPL loop."""
        self.print_header()
        await self._startup_health_check()

        # Background: settle pending market calls (>24h) vs live prices so the
        # prediction track record + DPO signals accrue with zero user effort.
        try:
            import threading as _th
            _th.Thread(target=self._verify_predictions,
                       kwargs={"min_age_hours": 24.0}, daemon=True).start()
        except Exception:
            pass

        # ── Start MCP servers (non-blocking background task) ─────────────
        if _HAS_MCP and not self._mcp_started:
            self._mcp_started = True
            async def _start_mcp():
                global _mcp_registry
                try:
                    from mcp_client import MCPToolRegistry
                    self._mcp_registry = MCPToolRegistry()
                    results = await self._mcp_registry.start_all()
                    if results:
                        n = self._mcp_registry.register_into(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
                        _mcp_registry = self._mcp_registry
                        if n and not self._mcp_connection_notice_shown:
                            self._mcp_connection_notice_shown = True
                            # The status bar is the single source of truth for
                            # MCP state. A standalone async success message used
                            # to interrupt the next input frame and corrupt IME
                            # composition on terminals without reliable CPR.
                            logger.info(
                                "MCP connected: %d tools, %d servers",
                                n,
                                len(results),
                            )
                except Exception as _exc:
                    logger.debug("MCP startup error: %s", _exc)
            asyncio.create_task(_start_mcp())

        # ── Start plugin hot-reload watcher ───────────────────────────────
        if _HAS_PLUGIN:
            global _plugin_watcher
            if _plugin_watcher is None:
                try:
                    _plugin_watcher = PluginWatcher(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS)
                    asyncio.create_task(_plugin_watcher.start())
                except Exception:
                    pass

        # ── Start alert watchdog (30s background price-alert checker) ─────
        asyncio.create_task(self._alert_watchdog())

        while self.running:
            try:
                if self._pt_session:
                    if self.config.get("input_style", "panel") == "panel":
                        from ui import PanelInputConfig, run_panel_input_async
                        # Drain notifications queued while pt was active (avoids stdout corruption)
                        while self._pending_notifications:
                            _note = self._pending_notifications.pop(0)
                            console.print(_note) if HAS_RICH else print(_note)
                        set_robot_state(RobotState.IDLE)
                        _ml, _cwd, _priv, _etok, _mctx = self._bottom_toolbar_parts()
                        _skills_n = len(SKILLS)
                        _tools_n = len(LOCAL_TOOLS)
                        _git_branch, _git_dirty = self._workspace_git_state()
                        _mcp_running = _mcp_total = _mcp_tools = 0
                        if self._mcp_registry:
                            try:
                                _mcp_states = self._mcp_registry.status()
                                _mcp_total = len(_mcp_states)
                                _mcp_running = sum(1 for state in _mcp_states if state.get("running"))
                                _mcp_tools = sum(
                                    int(state.get("tool_count", 0))
                                    for state in _mcp_states if state.get("running")
                                )
                            except Exception:
                                pass
                        _ollama_st = ""
                        if getattr(self, "_ollama_alive", False):
                            _om = len(getattr(self, "_installed_models", set()) or [])
                            _ollama_st = f"ollama {_om}m" if _om else "ollama ●"
                        user_input = await run_panel_input_async(
                            completer=self._pt_completer,
                            history=self._pt_history,
                            config=PanelInputConfig(
                                theme=self.config.get("input_theme", "auto"),
                                lang=self.config.get("ui_lang", "en") or "en",
                                model_label=_ml,
                                cwd=_cwd,
                                privacy=_priv,
                                est_tokens=_etok,
                                max_tokens=_mctx,
                                tools_count=_tools_n,
                                skills_count=_skills_n,
                                ollama_status=_ollama_st,
                                permission_mode=self.config.get("permission_mode", "workspace-write"),
                                git_branch=_git_branch,
                                git_dirty=_git_dirty,
                                mcp_running=_mcp_running,
                                mcp_total=_mcp_total,
                                mcp_tool_count=_mcp_tools,
                            ),
                        )
                    else:
                        user_input = await self._pt_session.prompt_async(
                            [("class:prompt", "> ")],
                            bottom_toolbar=self._bottom_toolbar,
                        )
                    user_input = user_input.strip()
                elif HAS_RICH:
                    user_input = console.input("[dim]>[/dim] ").strip()
                else:
                    user_input = input("> ").strip()

                if not user_input:
                    continue

                # ── Session recap: show summary if away for 3+ minutes ─────────
                import time as _time
                _now = _time.time()
                if self._last_turn_ts and (_now - self._last_turn_ts) > 180 and self.conversation:
                    _recap_turns = len(self.conversation)
                    if _recap_turns >= 6:
                        _last_ai = next(
                            (m["content"][:120] for m in reversed(self.conversation)
                             if m.get("role") == "assistant" and m.get("content")), None
                        )
                        if _last_ai:
                            _gap = int((_now - self._last_turn_ts) / 60)
                            if HAS_RICH:
                                console.print(
                                    f"  [dim]↩ 回到会话（{_gap}分钟前）— "
                                    f"{_last_ai[:80]}…[/dim]"
                                )
                self._last_turn_ts = _now

                # ── ! prefix: Shell mode ─────────────────────────────────────
                # Run shell command directly, add output to conversation context
                if user_input.startswith("!"):
                    shell_cmd = user_input[1:].strip()
                    if shell_cmd:
                        import subprocess as _subp
                        if HAS_RICH:
                            console.print(f"  [dim]$ {shell_cmd}[/dim]")
                        try:
                            _result = _subp.run(
                                shell_cmd, shell=True, capture_output=True,
                                text=True, timeout=30,
                            )
                            _out = (_result.stdout + _result.stderr).strip()
                            if _out:
                                if HAS_RICH:
                                    console.print(f"[dim]{_out}[/dim]")
                                else:
                                    print(_out)
                                # Inject into conversation context as user observation
                                self.conversation.append({
                                    "role": "user",
                                    "content": f"[shell $ {shell_cmd}]\n{_out}",
                                })
                            # Update shell autocomplete history
                            if self._pt_completer and hasattr(self._pt_completer, "add_shell_history"):
                                self._pt_completer.add_shell_history(shell_cmd)
                        except _subp.TimeoutExpired:
                            if HAS_RICH:
                                console.print("[yellow]  Command timed out (30s)[/yellow]")
                        except Exception as _se:
                            if HAS_RICH:
                                console.print(f"[red]  Error: {_se}[/red]")
                    continue

                # Multi-line input: start with """ to enter multi-line mode
                if user_input == '"""' or user_input.startswith('"""'):
                    lines = []
                    if user_input != '"""':
                        lines.append(user_input[3:])  # Text after opening """
                    if HAS_RICH:
                        console.print("[dim]  Multi-line mode (type \"\"\" to end)[/dim]")
                    else:
                        print('  Multi-line mode (type """ to end)')
                    while True:
                        try:
                            line = input("  ... ")
                            if line.strip() == '"""':
                                break
                            lines.append(line)
                        except (EOFError, KeyboardInterrupt):
                            break
                    user_input = "\n".join(lines).strip()
                    if not user_input:
                        continue

                if user_input.lower() in ("exit", "quit", "q"):
                    if self.conversation and self.config.get("auto_save_sessions"):
                        self.session_mgr.save_session(self.session_id, self.conversation)
                        self.config["last_session_id"] = self.session_id
                        if self._jsonl_store is not None:
                            try:
                                self._jsonl_store.flush_meta(self.session_id)
                            except Exception:
                                pass
                    # Fire SessionEnd hooks
                    if _HAS_JSON_HOOKS:
                        try:
                            _fire_json_hook("SessionEnd", session_id=self.session_id, hooks=_JSON_HOOKS)
                        except Exception:
                            pass
                    _run_event_hook("session_end", {"ARIA_SESSION": self.session_id})
                    if HAS_RICH:
                        console.print("[dim]Goodbye[/dim]")
                    else:
                        print("Goodbye")
                    break

                if self.commands.is_command(user_input):
                    self._maybe_show_intent_preflight(user_input)
                    await self.commands.execute(user_input)
                    continue

                # ── Top-level command router (quant CLI style) ─────────────────
                # Intercepts bare keywords like "analyze AAPL" → /analyze AAPL
                # so users don't need to type the slash for common quant workflows.
                self._maybe_show_intent_preflight(user_input)
                _routed = await try_top_level_route(user_input, self.commands, announce=_announce_route)
                if _routed:
                    continue

                # Auto memory trigger: "记住 X" / "remember that X" → silent /note
                _mem_fact = _check_memory_trigger(user_input)
                if _mem_fact:
                    self.commands.cmd_note(_mem_fact)

                await self.send_message(user_input, route_text=True)

            except KeyboardInterrupt:
                _esc_watcher.stop()
                if self._streaming and self.cancel_event:
                    self.cancel_event.set()
                    self._streaming = False
                    if HAS_RICH:
                        console.print("\n[dim]Cancelled[/dim]")
                    else:
                        print("\n  (cancelled)")
                else:
                    if HAS_RICH:
                        console.print("\n[dim]Press ESC to cancel · Ctrl+D to exit[/dim]")
                    else:
                        print("\nESC to cancel · Ctrl+D to exit")
            except EOFError:
                break

        save_config(self.config)



# ============================================================================
# CLI Entry Point
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(
        prog="aria-code",
        # Same sentence as pyproject's description, so `--help`, PyPI and npm
        # describe one product. It used to say "Quantitative Investment
        # Terminal" here while the package metadata had been repositioned;
        # tests/test_first_run_works.py keeps the two in step.
        description="Aria Code — Open-source AI agent for coding, financial analysis and logistics operations: numbers you can check, client data kept apart, actions a person approves",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  # Interactive REPL
  %(prog)s --resume                         # Resume last session
  %(prog)s -p "Analyze AAPL technicals"     # Single query
  %(prog)s quote AAPL MSFT                  # Quick quote
  %(prog)s backtest momentum SPY            # Strategy backtest
  %(prog)s --thinking                       # Enable thinking mode
  %(prog)s -p "AAPL PE ratio" --json        # JSON output
  %(prog)s -p "分析AAPL" --output report.md  # Save to file
  %(prog)s -p "报价" --format csv --quiet    # CSV, data only
  %(prog)s quote AAPL --watch 30             # Refresh every 30s
  echo "AAPL MSFT" | %(prog)s -p "比较"      # Unix pipe
        """
    )

    parser.add_argument("--version", "-V", action="version", version=f"aria-code {__version__}")
    parser.add_argument("-p", "--prompt", help="Single prompt (non-interactive)")
    parser.add_argument("--model", help="AI model: sonata|prelude|sonata-thinking|prelude-thinking or full Ollama ID")
    parser.add_argument("--thinking", action="store_true", help="Enable thinking mode")
    parser.add_argument("--json", action="store_true", help="JSON output (with -p)")
    parser.add_argument("--format", choices=["table", "json", "jsonl", "csv", "md"], default="table",
                        help="Output format (default: table). With -p, jsonl streams one JSON event "
                             "per line (turn.started, tool.started, tool.completed, turn.completed)")
    parser.add_argument("--output", "-o", help="Save output to file")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode (data only, no UI)")
    parser.add_argument("--watch", "-w", type=int, metavar="SECS", help="Refresh interval in seconds")
    parser.add_argument("--url", help="Backend API URL")
    parser.add_argument("--local", action="store_true", help="Local-only mode: skip AWS, use Ollama directly")
    parser.add_argument("--no-banner", action="store_true", help="Skip startup banner (same as --banner off)")
    parser.add_argument("--banner", choices=["full", "compact", "off"], help="Banner mode: full|compact|off")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--session", help="Load specific session ID")
    parser.add_argument("-C", "--cd", metavar="DIR", help="Open a local project directory")
    parser.add_argument("--add-dir", action="append", default=[], metavar="DIR",
                        help="Allow reads and writes in an additional directory (repeatable)")
    parser.add_argument("--read-dir", action="append", default=[], metavar="DIR",
                        help="Allow reads in an additional directory (repeatable)")
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="Skip ALL tool confirmation prompts this session (use in trusted scripts)",
    )
    parser.add_argument(
        "--allow-tools",
        metavar="TOOLS",
        help="Comma-separated tools to auto-allow this session, e.g. write_file,edit_file",
    )
    parser.add_argument("command", nargs="?", help="Direct command (quote, backtest, etc.)")
    parser.add_argument("args", nargs="*", help="Command arguments")

    args = parser.parse_args()
    # Resolve every path relative to the invocation directory before changing it.
    _write_roots = [str(pathlib.Path(p).expanduser().resolve()) for p in args.add_dir]
    _read_roots = [str(pathlib.Path(p).expanduser().resolve()) for p in args.read_dir]
    if args.cd:
        _project_dir = pathlib.Path(args.cd).expanduser().resolve()
        if not _project_dir.is_dir():
            parser.error(f"Project directory does not exist: {_project_dir}")
        os.chdir(_project_dir)

    # Machine-readable -p output: stdout carries only JSON. Everything else —
    # banners, tool steps, warnings, anything a tool prints — goes to stderr.
    # It all went to stdout, ahead of the JSON, so `aria-code -p … --json | jq`
    # could not parse what it was given.
    _machine_out = None
    if args.prompt and (args.json or args.format in ("json", "jsonl")):
        _machine_out = sys.stdout
        sys.stdout = sys.stderr

    config = load_config()
    config["_session_workspace_root"] = str(pathlib.Path.cwd().resolve())
    config["_session_write_roots"] = _write_roots
    config["_session_read_roots"] = _read_roots

    # ── Start background update check (non-blocking, daemon thread) ──────────
    try:
        from apps.cli.update_check import start_update_check
        _ui_lang_early = config.get("ui_lang", "en") or "en"
        start_update_check(__version__, lang=_ui_lang_early, enabled=bool(config.get("check_for_update_on_startup", True)))
    except Exception:
        pass

    # Apply syntax theme from config (P3)
    global _SYNTAX_THEME
    _SYNTAX_THEME = config.get("syntax_theme", "monokai")

    # Apply CLI overrides
    if args.model:
        raw_model = str(args.model).strip()
        if "/" in raw_model and not raw_model.startswith("http"):
            provider_name, selected_model = raw_model.split("/", 1)
            from apps.cli.providers.chat_routing import normalize_provider_name, KNOWN_MODEL_PROVIDERS

            provider_name = normalize_provider_name(provider_name)
            if provider_name in KNOWN_MODEL_PROVIDERS:
                # --model names the inference provider explicitly, just as /model
                # does. A stale backend_chat flag must not override that choice.
                config["backend_chat"] = False
            config["local_provider"] = provider_name
            config["model"] = selected_model
            config["local_mode"] = provider_name in {
                "ollama", "lmstudio", "vllm", "llamacpp", "jan", "custom",
            }
        else:
            mkey = resolve_model_key(raw_model)
            config["model"] = MODELS[mkey]["id"] if mkey in MODELS else raw_model
            config["local_provider"] = "ollama"
    if getattr(args, "local", False):
        config["local_mode"] = True
    # Banner flags apply only to this invocation; the REPL saves config on exit.
    session_banner_mode = (
        "off" if getattr(args, "no_banner", False) else getattr(args, "banner", None)
    )
    if args.thinking:
        config["thinking_mode"] = "thinking"
    if args.url:
        config["api_url"] = args.url

    # --dangerously-skip-permissions: bypass all confirmation prompts this session
    if getattr(args, "dangerously_skip_permissions", False):
        global _auto_approve_session
        _auto_approve_session = True
        if HAS_RICH:
            console.print("[yellow dim]⚠ 所有工具确认已跳过 (--dangerously-skip-permissions)[/yellow dim]")
        else:
            print("⚠ All tool confirmations skipped")

    # --allow-tools: pre-populate per-tool session allow list
    if getattr(args, "allow_tools", None):
        for _t in args.allow_tools.split(","):
            _t = _t.strip()
            if _t:
                _session_always_allow.add(_t)
        if HAS_RICH:
            console.print(f"[dim]Auto-allowed tools: {', '.join(sorted(_session_always_allow))}[/dim]")

    terminal = ArtheraTerminal(config)
    terminal._session_banner_mode = session_banner_mode

    # Resume session
    if args.resume or args.session:
        sid = args.session or config.get("last_session_id")
        if sid:
            data = terminal.session_mgr.load_session(sid)
            if data:
                terminal.conversation = data.get("messages", [])
                terminal.session_id = data["id"]
                title = data.get("metadata", {}).get("title", "Untitled")
                n = len(terminal.conversation)
                if HAS_RICH:
                    console.print(f"[green]Resumed: {title} ({n} messages)[/green]")
                else:
                    print(f"Resumed: {title} ({n} msgs)")

    # Shared output flags
    fmt = args.format if hasattr(args, 'format') else "table"
    output_file = args.output if hasattr(args, 'output') else None
    quiet = args.quiet if hasattr(args, 'quiet') else False
    watch_interval = args.watch if hasattr(args, 'watch') else None

    # Store quiet flag for watch mode
    terminal.config["_quiet"] = quiet

    # Unix pipe: read stdin if not a TTY and prepend to prompt
    piped_input = ""
    if not sys.stdin.isatty():
        piped_input = sys.stdin.read().strip()
        if piped_input and args.prompt:
            args.prompt = f"Context data:\n{piped_input}\n\nUser request: {args.prompt}"
        elif piped_input and not args.prompt:
            args.prompt = piped_input

    # Mode 1: Single prompt
    if args.prompt:
        if watch_interval:
            await terminal.run_watch(
                lambda _: terminal.run_prompt(args.prompt, json_output=args.json, fmt=fmt, output_file=output_file, quiet=quiet, machine_out=_machine_out),
                watch_interval, ""
            )
        else:
            await terminal.run_prompt(args.prompt, json_output=args.json, fmt=fmt, output_file=output_file, quiet=quiet, machine_out=_machine_out)
        return

    # Mode 2: Direct command
    if args.command and not (args.command.lower() == "code" and not args.args):
        cmd = args.command.lower()
        cmd_args = " ".join(args.args)

        # Build the command function for potential watch wrapping
        async def run_direct_cmd(_):
            await dispatch_direct_command(
                terminal,
                cmd,
                cmd_args,
                json_output=args.json,
                fmt=fmt,
                output_file=output_file,
                quiet=quiet,
            )

        if watch_interval and is_watchable_direct_command(cmd):
            await terminal.run_watch(run_direct_cmd, watch_interval, cmd_args)
        else:
            await run_direct_cmd(None)
        return

    # Mode 3: Interactive REPL (default)
    await terminal.run_interactive()


# ── Football helper functions ─────────────────────────────────────────────────
# 实现已移到 apps/cli/football_reports.py。这里必须用 _rebind_module_function_globals
# 而不是普通 import：market_cmds.py 那个 mixin 用裸名字调 _football_standings(...)，
# 靠的是 _rebind_mixin_globals 把它的 __globals__ 指向本模块；同时这些函数自身也
# 依赖本模块的 console。重新绑定后两边才都能解析。与 tool_executor 同一套机制。
import apps.cli.football_reports as _football_reports_module
_rebind_module_function_globals(_football_reports_module, _football_reports_module.__all__)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)


# ── stream_ollama rebind ──────────────────────────────────────────────────────
# Deliberately last. Unlike _rebind_module_function_globals, which hands the
# function this module's *live* globals dict, stream_ollama needs a merge of two
# namespaces — its own module's and this one's — so it gets a new dict, and a new
# dict is a snapshot.
#
# That snapshot used to be taken around line 2532, which left out the 79 names
# defined below it. One of them, _print_tool_result, is called unconditionally
# after every local tool execution in the streaming loop, so that line raised
# NameError with nothing to warn you at import. Taking the snapshot here, after
# every definition, is what makes the merge safe.
#
# tests/test_rebind_contract.py asserts the result: every global name
# stream_ollama reads has to exist in the namespace it was rebound into.
def _rebind_stream_ollama():
    import types

    from aria_code.apps.cli.providers.llm.ollama_stream import (
        stream_ollama as _source,
    )

    merged = dict(_source.__globals__)
    merged.update(globals())
    rebound = types.FunctionType(
        _source.__code__, merged, "stream_ollama",
        _source.__defaults__, _source.__closure__,
    )
    # Same omission as _rebind_module_function_globals: stream_ollama has no
    # keyword-only parameters today, so this is insurance rather than a fix —
    # but the sibling rebind proved what happens when one is added.
    rebound.__kwdefaults__ = _source.__kwdefaults__
    rebound.__doc__ = _source.__doc__
    return rebound


stream_ollama = _rebind_stream_ollama()
