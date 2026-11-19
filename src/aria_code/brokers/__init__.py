"""
brokers/ — Aria Code 券商接入层
================================
统一接口连接中国和国际主流券商，支持账户查询、持仓管理和下单。

快速使用::

    from brokers.registry import get_registry

    reg    = get_registry()
    broker = reg.connect("xt_main")          # 从 ~/.arthera/brokers.json 读取
    acct   = broker.account_info()
    pos    = broker.positions()

支持券商
--------
中国：
  xtquant     迅投 XTQuant（中信/华鑫/浙商等QMT平台）
  easytrader  EasyTrader（同花顺/通达信/华泰/国君 等客户端）
  futu        富途牛牛 OpenAPI（港/美/A股）
  tiger       老虎证券 OpenAPI（美/港/A股）
  longbridge  长桥证券 OpenAPI（港/美/A股）

国际：
  ibkr        Interactive Brokers TWS/Gateway
  alpaca      Alpaca Markets（美股 + 模拟盘）
  webull      Webull（美股，只读模式）

配置文件：~/.arthera/brokers.json
"""

from .base     import BrokerBase, AccountInfo, Position, Order, OrderResult, PortfolioSummary
from .config   import (
    load_config, list_broker_configs, get_broker_config,
    add_broker_config, remove_broker_config, set_default_broker,
    validate_broker_config, supported_broker_types, get_config_template,
    BROKERS_CONFIG_PATH,
)
from .registry import BrokerRegistry, get_registry
from .planning import (
    PortfolioSnapshot, StrategyIntent, RiskRuleSet, PlannedOrder, OrderPlan,
    snapshot_from_broker, infer_intent_from_backtest, plan_order,
    evaluate_risk, plans_from_strategy_results,
)
from .capabilities import (
    BrokerCapability, broker_connection_plan, broker_dependency_state,
    broker_service_playbook, filter_capabilities, get_broker_capability,
    list_broker_capabilities,
)
from .paper_broker import PaperBroker, reset_paper_account
from .trading import (
    OrderIntent, TradingPolicy, build_order_preview, execute_order_preview,
    list_order_previews, load_order_preview, policy_from_config, resolve_trading_mode,
    global_dry_run,
)

__all__ = [
    "BrokerBase", "AccountInfo", "Position", "Order", "OrderResult", "PortfolioSummary",
    "load_config", "list_broker_configs", "get_broker_config",
    "add_broker_config", "remove_broker_config", "set_default_broker",
    "validate_broker_config", "supported_broker_types", "get_config_template",
    "BROKERS_CONFIG_PATH",
    "BrokerRegistry", "get_registry",
    "PortfolioSnapshot", "StrategyIntent", "RiskRuleSet", "PlannedOrder", "OrderPlan",
    "snapshot_from_broker", "infer_intent_from_backtest", "plan_order",
    "evaluate_risk", "plans_from_strategy_results",
    "BrokerCapability", "broker_connection_plan", "broker_dependency_state",
    "broker_service_playbook", "filter_capabilities", "get_broker_capability",
    "list_broker_capabilities",
    "PaperBroker", "reset_paper_account",
    "OrderIntent", "TradingPolicy", "build_order_preview", "execute_order_preview",
    "list_order_previews", "load_order_preview", "policy_from_config", "resolve_trading_mode",
    "global_dry_run",
]
