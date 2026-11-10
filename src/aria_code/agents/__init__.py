"""
agents/ — Aria Code 可组合多智能体系统
=======================================
用户可在项目根放置 aria_agents/ 目录，其中的 Agent 类自动被发现。

内置 Agent:
  macro       → 宏观环境、利率、行业周期
  fundamental → 财务指标、估值、竞争壁垒
  technical   → 图形形态、动量、关键价位
  risk        → 风险评分、仓位建议
  synthesis   → 汇总以上，输出可操作建议

自定义 Agent 示例 (aria_agents/northbound_agent.py):
    from agents.base import BaseAgent, AgentResult

    class NorthboundAgent(BaseAgent):
        name        = "northbound"
        description = "北向资金分析专家"

        async def analyze(self, symbol: str, data: dict) -> AgentResult:
            ...
"""

# Keep the public analysis-agent API, without eager registration of every domain.
# Task-agent capabilities must not initialize finance/vendor SDKs on import.
from importlib import import_module

_EXPORTS = {
    "BaseAgent": (".base", "BaseAgent"), "AgentResult": (".base", "AgentResult"),
    "AgentRegistry": (".registry", "AgentRegistry"), "get_registry": (".registry", "get_registry"),
    "AgentTeam": (".team", "AgentTeam"), "run_team": (".team", "run_team"),
    "SignalScheme": (".signal_scheme", "SignalScheme"),
    "FINANCIAL_SCHEME": (".signal_scheme", "FINANCIAL_SCHEME"),
    "REALTY_SCHEME": (".signal_scheme", "REALTY_SCHEME"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
