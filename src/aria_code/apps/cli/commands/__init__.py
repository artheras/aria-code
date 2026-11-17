"""CLI command adapters, imported only when used.

Importing a lightweight command such as /architecture must not initialize the
finance stack (pandas/numpy) through this package's __init__.
"""

from importlib import import_module

__all__ = [
    "BrokerCommandsMixin",
    "WorkspaceCommandsMixin",
    "ModelCommandsMixin",
    "MarketCommandsMixin",
    "PortfolioCommandsMixin",
]

_MIXIN_MODULES = {
    "BrokerCommandsMixin": "broker_cmds",
    "WorkspaceCommandsMixin": "workspace_cmds",
    "ModelCommandsMixin": "model_cmds",
    "MarketCommandsMixin": "market_cmds",
    "PortfolioCommandsMixin": "portfolio_cmds",
}


def __getattr__(name: str):
    module_name = _MIXIN_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value
