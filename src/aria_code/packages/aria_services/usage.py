"""User-facing service usage catalog for Aria Code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceUsageSpec:
    name: str
    purpose: str
    cli_entrypoints: tuple[str, ...]
    package_sources: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    next_step: str


def list_service_usage_specs() -> tuple[ServiceUsageSpec, ...]:
    """Return how each project service is meant to be used from Aria."""

    return (
        ServiceUsageSpec(
            name="agent_runtime",
            purpose="模型回合、工具调用、并行/串行执行、审批、取消、LoopGuard 和 trace",
            cli_entrypoints=("/chat", "-p", "aria"),
            package_sources=("runtime", "packages/aria_sdk", "apps/cli/providers/runtime_bridge.py"),
            mcp_tools=("aria.agent.run", "aria.tools.execute"),
            next_step="/architecture --gaps 查看 runtime cutover 状态",
        ),
        ServiceUsageSpec(
            name="settings_config",
            purpose="模型配置、provider key、权限模式、网络开关、语言和 UI 偏好",
            cli_entrypoints=("/config", "/model", "/apikey", "/setup"),
            package_sources=("apps/cli/config_store.py", "apps/cli/config_paths.py", "config"),
            mcp_tools=(),
            next_step="/config list 或 /setup",
        ),
        ServiceUsageSpec(
            name="context_memory",
            purpose="上下文压力检测、自动压缩、会话恢复和用户偏好记忆",
            cli_entrypoints=("/compact", "/memory", "/session"),
            package_sources=("packages/aria_services/context.py", "apps/cli/message_processing.py"),
            mcp_tools=("aria.context.compact",),
            next_step="/status 查看 auto compact 状态",
        ),
        ServiceUsageSpec(
            name="context_references",
            purpose="使用 @ 将文件、目录、市场资产和研究产物作为只读上下文附加到请求",
            cli_entrypoints=("@file:", "@folder:", "@asset:", "@portfolio:", "@strategy:", "@dataset:", "@run:", "@report:"),
            package_sources=("packages/aria_services/references.py", "ui/completer.py"),
            mcp_tools=(),
            next_step="输入 @ 查看类型，或使用 /risk @portfolio:core",
        ),
        ServiceUsageSpec(
            name="tool_registry",
            purpose="本地工具、MCP 工具、schema、权限和 deterministic 输出封装",
            cli_entrypoints=("/tools", "/mcp", "!shell"),
            package_sources=("packages/aria_tools", "packages/aria_mcp", "runtime/tool_executor.py"),
            mcp_tools=("read_file", "run_command", "broker_order"),
            next_step="/tools 或 /packages tools arthera",
        ),
        ServiceUsageSpec(
            name="market_data",
            purpose="行情、历史K线、技术指标、数据质量标记",
            cli_entrypoints=("/quote", "/market", "/ta", "/chart"),
            package_sources=("packages/data", "packages/quant_engine"),
            mcp_tools=("calculate_factors", "northbound_flow"),
            next_step="/quote AAPL 或 /chart MC.PA 1y",
        ),
        ServiceUsageSpec(
            name="signals_prediction",
            purpose="量化信号、预测收益、市场状态和仓位建议",
            cli_entrypoints=("/signal", "/predict", "/team"),
            package_sources=("packages/quant_engine", "packages/ml"),
            mcp_tools=("generate_signal", "get_ai_signal", "get_predictions", "kelly_position"),
            next_step="/signal NVDA 或 /packages tools arthera",
        ),
        ServiceUsageSpec(
            name="backtest",
            purpose="策略回测、交易成本、胜率、夏普和最大回撤",
            cli_entrypoints=("/backtest", "/strategy"),
            package_sources=("packages/quant_engine/backtest",),
            mcp_tools=("run_backtest",),
            next_step="/backtest momentum NVDA --period 1y",
        ),
        ServiceUsageSpec(
            name="risk_portfolio",
            purpose="持仓风险、组合风险、压力测试和资金管理",
            cli_entrypoints=("/risk", "/positions", "/account"),
            package_sources=("brokers", "packages/quant_engine/risk", "packages/contracts"),
            mcp_tools=("detect_regime", "kelly_position"),
            next_step="/broker guide 然后 /positions",
        ),
        ServiceUsageSpec(
            name="reports_artifacts",
            purpose="研报、图表、看板、Pine 文件、完成度门禁和本地 artifact 管理",
            cli_entrypoints=("/report", "/dashboard", "/chart", "/tv", "/artifacts"),
            package_sources=("packages/reporting", "artifacts"),
            mcp_tools=(
                "aria.report.generate",
                "aria.artifacts.list",
                "research_report_assess",
                "research_run_record_quality",
            ),
            next_step="/report MSFT --format html",
        ),
        ServiceUsageSpec(
            name="broker_execution",
            purpose="券商连接、账户只读查询、仿盘账户、订单预览、确认后执行和审计",
            cli_entrypoints=("/broker", "/paper", "/trade", "/account", "/positions", "/orders"),
            package_sources=("brokers", "brokers/trading.py", "brokers/paper_broker.py"),
            mcp_tools=("broker_query", "broker_order"),
            next_step="/paper start 100000 USD 或 /broker doctor",
        ),
        ServiceUsageSpec(
            name="tradingview_webhook",
            purpose="TradingView 图表打开、Pine 策略导出、Alert Webhook 接入",
            cli_entrypoints=("/tv", "aria_daemon.py"),
            package_sources=("apps/cli/tradingview_bridge.py", "aria_daemon.py"),
            mcp_tools=(),
            next_step="/tv NVDA --pine --txt",
        ),
        ServiceUsageSpec(
            name="mcp_bridge",
            purpose="把 Arthera QuantEngine 作为 MCP 工具暴露给本地客户端",
            cli_entrypoints=("/packages connect arthera", "/mcp reload", "/packages tools arthera"),
            package_sources=("packages/quant_engine/mcp_server.py",),
            mcp_tools=("calculate_factors", "run_backtest", "price_option", "execution_schedule"),
            next_step="/packages connect arthera --reload",
        ),
        ServiceUsageSpec(
            name="safety_policy",
            purpose="文件、shell、网络、券商交易和隐私反馈的统一权限/审计边界",
            cli_entrypoints=("/permissions", "/trade preview", "/feedback"),
            package_sources=("safety", "runtime/approval.py", "command_safety.py", "privacy"),
            mcp_tools=("run_command", "broker_order"),
            next_step="/permissions 或 /trade mode",
        ),
        ServiceUsageSpec(
            name="observability",
            purpose="doctor、架构覆盖、数据源健康、trace、manifest 和支持包",
            cli_entrypoints=("/doctor", "/architecture", "/packages doctor"),
            package_sources=("packages/aria_infra", "packages/aria_services/provider_health.py"),
            mcp_tools=("aria.health", "aria.manifest.export"),
            next_step="/doctor 或 /architecture --gaps",
        ),
    )


def service_usage_map() -> dict[str, ServiceUsageSpec]:
    return {spec.name: spec for spec in list_service_usage_specs()}
