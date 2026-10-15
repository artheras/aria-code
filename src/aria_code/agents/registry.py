"""
agents/registry.py — Agent 注册与自动发现
==========================================
1. 内置 agent 预注册（technical / fundamental / macro / risk）
2. 扫描 ./aria_agents/ → 自动注册用户自定义 agent
3. 提供 get() / list() / register() 接口

自定义 Agent 放置规则:
    项目根/aria_agents/my_agent.py
    类需继承 BaseAgent，且声明 name 属性
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import BaseAgent
from .financial.technical import TechnicalAgent

logger = logging.getLogger(__name__)

# ── 内置 agent 预注册 ─────────────────────────────────────────────────────────
_BUILTIN: Dict[str, Type[BaseAgent]] = {
    "technical": TechnicalAgent,
}

# 尝试加载其他内置 agent（文件存在则注册）
def _try_import_builtin(module_path: str, name: str) -> None:
    try:
        parts = module_path.split(".")
        mod = importlib.import_module(module_path)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, BaseAgent) and cls is not BaseAgent:
                _BUILTIN[cls.name] = cls
    except ImportError:
        pass


_try_import_builtin("aria_code.agents.financial.macro",       "macro")
_try_import_builtin("aria_code.agents.financial.fundamental",  "fundamental")
_try_import_builtin("aria_code.agents.financial.risk",         "risk")
_try_import_builtin("aria_code.agents.financial.synthesis",    "synthesis")
_try_import_builtin("aria_code.agents.financial.news",         "news")
_try_import_builtin("aria_code.agents.financial.catalyst",     "catalyst")
_try_import_builtin("aria_code.agents.financial.debate",       "debate")
_try_import_builtin("aria_code.agents.financial.earnings",     "earnings")
_try_import_builtin("aria_code.agents.financial.sector",       "sector")
_try_import_builtin("aria_code.agents.financial.strategist",   "strategist")
_try_import_builtin("aria_code.agents.engineering.coder",      "coder")
_try_import_builtin("aria_code.agents.engineering.tester",     "tester")
_try_import_builtin("aria_code.agents.financial.corporate_finance", "corporate_finance")
_try_import_builtin("aria_code.agents.financial.cashflow_burn",     "cashflow_burn")
_try_import_builtin("aria_code.agents.financial.stripe_revenue",    "stripe_revenue")
_try_import_builtin("aria_code.agents.portfolio_agent",        "portfolio")
_try_import_builtin("aria_code.agents.supervisor",             "supervisor")

# ── 经营权共创平台 Agent 包 ────────────────────────────────────────────────────
_try_import_builtin("aria_code.agents.realty.asset_diagnosis",  "asset_diagnosis")
_try_import_builtin("aria_code.agents.realty.business_match",   "business_match")
_try_import_builtin("aria_code.agents.realty.contract_rules",   "contract_rules")
_try_import_builtin("aria_code.agents.realty.revenue_share",    "revenue_share")
_try_import_builtin("aria_code.agents.realty.cashflow_verify",  "cashflow_verify")
_try_import_builtin("aria_code.agents.realty.energy_anomaly",   "energy_anomaly")
_try_import_builtin("aria_code.agents.realty.fulfillment_risk", "fulfillment_risk")
_try_import_builtin("aria_code.agents.realty.ops_optimize",     "ops_optimize")
_try_import_builtin("aria_code.agents.realty.exit_settlement",  "exit_settlement")

# ── 海外仓与物流供应链 Agent 包 ──────────────────────────────────────────────────
_try_import_builtin("aria_code.agents.warehouse.logistics_sync", "warehouse_logistics_sync")
_try_import_builtin("aria_code.agents.warehouse.inbound_exceptions", "warehouse_inbound_exceptions")
_try_import_builtin("aria_code.agents.warehouse.inventory_health", "warehouse_inventory_health")
_try_import_builtin("aria_code.agents.warehouse.logistics_cost", "warehouse_logistics_cost")
_try_import_builtin("aria_code.agents.warehouse.fulfillment_leadtime", "warehouse_fulfillment_leadtime")

# ── 视频制作与多媒体 Agent 包 ──────────────────────────────────────────────────
_try_import_builtin("aria_code.agents.video.director", "video_director")



class AgentRegistry:
    """
    Agent 注册中心（单例）。

    用法:
        registry = get_registry()
        registry.register(MyAgent)
        agent_cls = registry.get("my_agent")
    """

    def __init__(self):
        self._agents: Dict[str, Type[BaseAgent]] = dict(_BUILTIN)
        self._scanned = False

    def register(self, cls: Type[BaseAgent]) -> None:
        """手动注册一个 Agent 类"""
        if not (isinstance(cls, type) and issubclass(cls, BaseAgent)):
            raise TypeError(f"{cls} 不是 BaseAgent 子类")
        self._agents[cls.name] = cls
        logger.info(f"✓ 注册 Agent: {cls.name}")

    def get(self, name: str) -> Optional[Type[BaseAgent]]:
        """按名称获取 Agent 类"""
        self._ensure_scanned()
        return self._agents.get(name.lower())

    def list(self) -> List[Dict]:
        """列出所有已注册 Agent"""
        self._ensure_scanned()
        return [
            {"name": name, "description": cls.description, "builtin": name in _BUILTIN}
            for name, cls in self._agents.items()
        ]

    def _ensure_scanned(self) -> None:
        if not self._scanned:
            self._scan_project()
            self._scanned = True

    def _scan_project(self) -> None:
        """扫描当前工作目录及父级的 aria_agents/ 目录"""
        search_paths = [Path.cwd(), Path.cwd().parent]
        for base in search_paths:
            agents_dir = base / "aria_agents"
            if not agents_dir.is_dir():
                continue
            for py_file in sorted(agents_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._load_file(py_file)
            break   # 找到第一个 aria_agents/ 即停止向上搜索

    def _load_file(self, path: Path) -> None:
        """加载一个 .py 文件，自动注册其中的 BaseAgent 子类"""
        try:
            spec   = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (cls.__module__ == module.__name__
                        and issubclass(cls, BaseAgent)
                        and cls is not BaseAgent
                        and hasattr(cls, "name")
                        and cls.name != "base"):
                    self._agents[cls.name] = cls
                    logger.info(f"✓ 自动发现 Agent: {cls.name} ({path.name})")
        except Exception as e:
            logger.warning(f"加载 agent 文件 {path} 失败: {e}")


# ── 单例 ──────────────────────────────────────────────────────────────────────
_registry: Optional[AgentRegistry] = None

def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
