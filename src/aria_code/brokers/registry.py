"""
brokers/registry.py — 券商注册表 & 连接管理器
================================================
统一管理所有已连接的券商实例。
支持多账户并发持有（如同时连接 IBKR + 富途）。

用法::

    from brokers.registry import BrokerRegistry

    reg = BrokerRegistry()
    broker = reg.connect("xt_main")         # 从 brokers.json 读取并连接
    acct   = broker.account_info()
    pos    = broker.positions()

    # 切换默认账户
    reg.set_active("ibkr_us")
    broker = reg.active()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

from .base   import BrokerBase
from .config import (
    get_broker_config, get_default_broker_config,
    list_broker_configs, set_default_broker,
)

logger = logging.getLogger(__name__)

# ── 适配器注册表 ───────────────────────────────────────────────────────────────

_BROKER_CLASSES: Dict[str, Type[BrokerBase]] = {}


def _register_all() -> None:
    """延迟注册内置模拟盘适配器（实盘券商适配器已迁移至 artheras/financial-service）。"""
    # No `global` needed: _BROKER_CLASSES is only read and mutated in place.
    if _BROKER_CLASSES:
        return
    _map: Dict[str, tuple] = {
        "paper": ("brokers.paper_broker", "PaperBroker"),
    }
    for btype, (module_path, class_name) in _map.items():
        try:
            import importlib
            mod = importlib.import_module(module_path)
            _BROKER_CLASSES[btype] = getattr(mod, class_name)
        except Exception as e:
            logger.debug("Broker class load failed for %s: %s", btype, e)


def get_broker_class(broker_type: str) -> Optional[Type[BrokerBase]]:
    _register_all()
    return _BROKER_CLASSES.get(broker_type)


# ── 连接管理器 ────────────────────────────────────────────────────────────────

class BrokerRegistry:
    """全局券商连接池，单例使用。"""

    def __init__(self):
        self._instances: Dict[str, BrokerBase] = {}  # broker_id → instance
        self._active_id: Optional[str] = None

    # ── 连接 ──────────────────────────────────────────────────────────────────

    def connect(self, broker_id: str) -> BrokerBase:
        """连接指定 id 的券商（如已连接则直接返回）。"""
        if broker_id in self._instances and self._instances[broker_id].is_connected:
            return self._instances[broker_id]

        cfg = get_broker_config(broker_id)
        if not cfg:
            raise ValueError(f"未找到券商配置: {broker_id!r}  (请在 ~/.arthera/brokers.json 添加)")

        broker_type = cfg.get("type", "")
        cls = get_broker_class(broker_type)
        if not cls:
            raise ValueError(
                f"不支持的券商类型: {broker_type!r}\n"
                f"本地仅保留模拟盘类型: {', '.join(_BROKER_CLASSES)}\n"
                "实盘券商适配器已迁移至 artheras/financial-service，请连接 arthera-trading MCP 服务。"
            )

        instance = cls(broker_id=broker_id, config=cfg)
        instance.connect()
        self._instances[broker_id] = instance

        if self._active_id is None:
            self._active_id = broker_id

        logger.info("✓ 已连接券商: %s (%s)", instance.label, broker_type)
        return instance

    def connect_default(self) -> Optional[BrokerBase]:
        """连接 brokers.json 中标记为 default 的券商。"""
        cfg = get_default_broker_config()
        if not cfg:
            return None
        return self.connect(cfg["id"])

    def connect_all(self) -> List[BrokerBase]:
        """尝试连接所有已配置的券商，跳过连接失败的。"""
        connected = []
        for cfg in list_broker_configs():
            try:
                b = self.connect(cfg["id"])
                connected.append(b)
            except Exception as e:
                logger.warning("连接券商 %s 失败: %s", cfg.get("id"), e)
        return connected

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def active(self) -> Optional[BrokerBase]:
        """返回当前活跃的券商实例。"""
        if not self._active_id:
            return None
        return self._instances.get(self._active_id)

    def get(self, broker_id: str) -> Optional[BrokerBase]:
        """按 id 获取已连接的实例。"""
        return self._instances.get(broker_id)

    def ensure(self, broker_id: str) -> BrokerBase:
        """返回可用实例，链路掉线则透明重连。长驻调用方应优先用此方法而非 get()/connect()。"""
        inst = self._instances.get(broker_id)
        if inst is not None:
            try:
                if inst.ensure_connected():
                    return inst
            except Exception:
                pass
            # 实例级重连失败 → 丢弃后整体重建
            self._instances.pop(broker_id, None)
        return self.connect(broker_id)

    def health(self) -> List[Dict[str, Any]]:
        """连接健康快照（供 /broker 状态展示）。"""
        out: List[Dict[str, Any]] = []
        for bid, inst in self._instances.items():
            try:
                healthy = bool(inst.is_connected and inst.ping())
            except Exception:
                healthy = False
            out.append({
                "broker_id": bid,
                "label": getattr(inst, "label", bid),
                "type": getattr(inst, "broker_type", ""),
                "connected": bool(inst.is_connected),
                "healthy": healthy,
                "active": bid == self._active_id,
            })
        return out

    def list_connected(self) -> List[BrokerBase]:
        """返回所有已连接的券商。"""
        return [b for b in self._instances.values() if b.is_connected]

    def set_active(self, broker_id: str) -> bool:
        """设置当前活跃账户。"""
        if broker_id not in self._instances:
            return False
        self._active_id = broker_id
        set_default_broker(broker_id)
        return True

    # ── 断开 ──────────────────────────────────────────────────────────────────

    def disconnect(self, broker_id: str) -> None:
        b = self._instances.pop(broker_id, None)
        if b:
            b.disconnect()
        if self._active_id == broker_id:
            remaining = list(self._instances)
            self._active_id = remaining[0] if remaining else None

    def disconnect_all(self) -> None:
        for b in list(self._instances.values()):
            try:
                b.disconnect()
            except Exception:
                pass
        self._instances.clear()
        self._active_id = None

    def __repr__(self) -> str:
        ids = list(self._instances)
        return f"<BrokerRegistry active={self._active_id!r} connected={ids}>"


# 全局单例（在 aria_cli.py 中 import 后使用）
_global_registry: Optional[BrokerRegistry] = None


def get_registry() -> BrokerRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = BrokerRegistry()
    return _global_registry
