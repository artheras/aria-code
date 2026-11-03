"""Channel registry — the declared inventory of external entrypoints.

First slice of the channels layer (contract: "Channels submit structured
tasks to gateway/runtime and never call CLI internals directly"). The
registry is pure data + config resolution: it names each channel, its
direction, the config/env that enables it, and which capability it maps to,
so the daemon and doctor can reason about channels uniformly instead of each
integration hard-coding its own discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

CHANNEL_TASK_SCHEMA = "aria.channel_task.v1"


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    direction: str              # "inbound" | "outbound" | "both"
    description: str
    capabilities: Tuple[str, ...] = ()
    # Any of these config keys (or env vars) being truthy enables the channel.
    config_keys: Tuple[str, ...] = ()
    env_keys: Tuple[str, ...] = ()

    def enabled(self, config: Mapping[str, Any], env: Mapping[str, str]) -> bool:
        for key in self.config_keys:
            if config.get(key):
                return True
        for key in self.env_keys:
            if str(env.get(key, "") or "").strip():
                return True
        return False


def default_channels() -> List[ChannelSpec]:
    return [
        ChannelSpec(
            name="tradingview",
            direction="inbound",
            description="TradingView alert webhooks → structured alert tasks",
            capabilities=("alerts.ingest", "orders.preview"),
            config_keys=("tradingview_webhook",),
            env_keys=("ARIA_WEBHOOK_SECRET",),
        ),
        ChannelSpec(
            name="webhook_jobs",
            direction="inbound",
            description="Generic daemon webhook job queue (/webhook/trigger)",
            capabilities=("tasks.enqueue",),
            env_keys=("WEBHOOK_TOKEN",),
        ),
        ChannelSpec(
            name="telegram",
            direction="both",
            description="Telegram bot push + command intake via the daemon",
            capabilities=("notify.push", "chat.intake"),
            config_keys=("telegram_bot_token",),
            env_keys=("TELEGRAM_BOT_TOKEN",),
        ),
        ChannelSpec(
            name="feishu",
            direction="outbound",
            description="Feishu webhook card push",
            capabilities=("notify.push",),
            config_keys=("feishu_webhook",),
            env_keys=("FEISHU_WEBHOOK_URL",),
        ),
        ChannelSpec(
            name="macos_notify",
            direction="outbound",
            description="Local macOS notification center",
            capabilities=("notify.push",),
            config_keys=("macos_notifications",),
        ),
        ChannelSpec(
            name="email",
            direction="outbound",
            description="SMTP email push",
            capabilities=("notify.push",),
            config_keys=("email_smtp_host",),
            env_keys=("ARIA_SMTP_HOST",),
        ),
    ]


def channel_map() -> Dict[str, ChannelSpec]:
    return {spec.name: spec for spec in default_channels()}


def enabled_channels(config: Mapping[str, Any], env: Mapping[str, str]) -> List[ChannelSpec]:
    return [spec for spec in default_channels() if spec.enabled(config, env)]
