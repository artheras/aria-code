"""Declarative agent architecture contract for Aria Code.

The contract keeps Codex/Claude Code style boundaries explicit while the legacy
CLI is extracted into smaller packages. It is intentionally pure data so tests,
doctor checks, docs, and future UI views can share one source of truth.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple


class LayerStatus(str, Enum):
    DONE = "done"
    PARTIAL = "partial"
    PLANNED = "planned"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ArchitectureLayer:
    name: str
    responsibility: str
    target_state: str
    current_state: str
    status: LayerStatus
    source_paths: Tuple[str, ...] = field(default_factory=tuple)
    depends_on: Tuple[str, ...] = field(default_factory=tuple)
    next_steps: Tuple[str, ...] = field(default_factory=tuple)
    blockers: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        return self.status == LayerStatus.DONE

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


ARCHITECTURE_SCHEMA_VERSION = "aria.agent-architecture.v1"


_ARCHITECTURE_LAYERS: Tuple[ArchitectureLayer, ...] = (
    ArchitectureLayer(
        name="launcher",
        responsibility="Stable executable entrypoint, runtime selection, and dependency bootstrap.",
        target_state="Shell entrypoints resolve the repo, use a controlled virtualenv, and never depend on the caller's random Python.",
        current_state="aria and aria-code bootstrap local dependencies. /doctor runs a python_venv drift check (pyvenv.cfg recorded version vs the running interpreter, and whether the venv's base interpreter still exists — the Homebrew-upgrade breakage); install.sh --rebuild and npm run repair both perform the drift-aware venv rebuild it suggests (repair auto-detects drift via npm/lib/venv.js before reusing an existing venv).",
        status=LayerStatus.PARTIAL,
        source_paths=("aria-code", "install.sh", "doctor.py"),
        next_steps=("Exercise the drift rebuild end-to-end on a real system-Python upgrade, then consider promoting launcher to ok.",),
    ),
    ArchitectureLayer(
        name="settings",
        responsibility="Configuration, secrets, model profiles, and permission policy resolution.",
        target_state="A single settings service resolves env, config files, CLI flags, and secrets without leaking credentials.",
        current_state=(
            "packages/aria_services/settings.py exists and apps/cli/config_store.py builds it, but "
            "aria_cli.py and aria_daemon.py are its callers. Two of the three sites this entry "
            "used to name were miscategorised: brokers/config.py owns a different file "
            "(brokers.json, broker credentials) and already writes it owner-only via "
            "packages/aria_core/secure_file.py, and packages/aria_mcp/server.py never reads the "
            "main config at all — it delegates to brokers.config.is_chat_confirm_enabled. The one "
            "real bypass was aria_daemon.py, which built the runtime gateway's config as a dict "
            "literal pinned to Ollama and so never saw the user's configuration: a Vertex/Gemini "
            "user had every channel alert analysed by a local qwen2.5:7b, and a user without "
            "Ollama had the gateway raise on every alert and degrade to the legacy summary at log "
            "level info. It now loads through SettingsService per call, keeping "
            "ARIA_DAEMON_MODEL/OLLAMA_URL as operator overrides; "
            "tests/test_daemon_gateway_config.py pins it and fails on a hardcoded model name."
        ),
        status=LayerStatus.PARTIAL,
        source_paths=(
            "packages/aria_services/settings.py",
            "apps/cli/config_store.py",
            "apps/cli/config_paths.py",
            "packages/aria_core/secure_file.py",
        ),
        next_steps=(
            "Consider a settings-service entry point for brokers.json so brokers/config.py "
            "stops owning its own load/save pair — it is already secure, so this is "
            "consolidation rather than a fix.",
        ),
    ),
    ArchitectureLayer(
        name="ui",
        responsibility="Terminal rendering, input UX, progress display, and artifact links.",
        target_state="A thin UI adapter renders compact, resumable, non-repetitive output with graceful plain-terminal fallback.",
        current_state="Terminal streaming and approval prompts now have a CLI runtime event consumer; Rich/prompt_toolkit remain optional and generated-file UX still needs hardening.",
        status=LayerStatus.PARTIAL,
        source_paths=("ui/", "apps/cli/commands/", "apps/cli/runtime_consumer.py"),
        next_steps=("Move generated-file open actions and remaining terminal panels behind a UI service.",),
    ),
    ArchitectureLayer(
        name="context",
        responsibility="Conversation memory, context compaction, task continuity, and artifact-backed summaries.",
        target_state="Context is automatically compacted before overflow, with recoverable task state and traceable artifacts.",
        current_state="ContextService owns pressure checks, local compaction, summary prompts, and resume envelopes; /doctor runs a context check and /export bundle carries context_health. Every compaction path (smart, hard, both fallbacks) first persists an aria.context_checkpoint.v1 full-snapshot record (2MB cap, keep-5 per session, 30-day expiry; config keys context_checkpoint_keep / context_checkpoint_max_age_days) via packages.aria_services.context_checkpoints under ~/.arthera/runtime/context_checkpoints/. Of the three places send_message decides that history is too hot, the post-turn char-estimate path honoured neither auto_compact_context nor auto_compact_threshold and hardcoded 90%/70%, so turning auto-compaction off did not turn it off and a 0.95 threshold was overridden by the hidden 0.90; the decision now lives in apps/cli/turn_planning.post_turn_context_decision alongside the other pure per-turn decisions, warning still fires when compaction is disabled, and tests/test_post_turn_context_decision.py covers it.",
        status=LayerStatus.PARTIAL,
        source_paths=("packages/aria_services/context.py", "packages/aria_services/context_checkpoints.py", "apps/cli/message_processing.py", "apps/cli/commands/session_ux_cmds.py"),
        next_steps=("Wire checkpoint restore into the conversation-rewind flow once cmd_rewind lands, so a compaction can be undone from the CLI.",),
    ),
    ArchitectureLayer(
        name="runtime",
        responsibility="Agent turn loop, planning, tool execution, retries, streaming, and interruption handling.",
        target_state="Runtime is separate from UI and business services, with typed tool calls, retries, cancellation, and traces.",
        current_state="A public packages.aria_sdk facade owns SDK-style query/result events, provider selection, streaming normalization, and the reusable runtime tool-turn loop; CLI rendering/approval is consumed through apps.cli.runtime_consumer. Every CLI chat turn runs through run_agent via apps.cli.providers.runtime_bridge and runtime.gateway (the inline send_message loop was retired 2026-07 after real-turn validation), with route-aware provider/fallback decisions in apps.cli.providers.chat_routing, per-tool approval and execution context threaded into run_agent's tool loop, and a single direct cloud rescue on runtime failure.",
        status=LayerStatus.PARTIAL,
        source_paths=("aria_cli.py", "runtime/", "packages/aria_sdk/", "apps/cli/runtime_consumer.py", "apps/cli/deterministic.py", "apps/cli/providers/"),
        depends_on=("settings", "tools", "safety", "context"),
        next_steps=(
            "Fold send_message's remaining pre-turn (routing/decomposition/context injection) and post-turn (rendering/history/metrics) sections into testable modules, mirroring turn_planning/prompt_assembly.",
            "Untangle stream_ollama's 12 aria_cli module-global borrowings (45 → 30 → 20 → 12; 47 before "
            "the response cache moved to apps/cli/providers/llm/response_cache.py). Counted with "
            "stdlib symtable rather than a hand-rolled AST walk. The 33 reclaimed are module-level "
            "imports in ollama_stream.py: the rebind merges rather than replaces "
            "(dict(source.__globals__) then .update(globals())), so a name defined there is the base "
            "of the merge, aria_cli still wins on conflict, and the CLI path is untouched while the "
            "fallback path (SDK, daemon) stops dying on it. Each one is checked to be the same "
            "object aria_cli holds. That rule keeps paying: _CONFIRM_TOOLS is excluded because "
            "apps.cli.tool_executor's copy is an empty set() placeholder against aria_cli's real "
            "confirmation set, and it caught aria_cli holding one-line *wrapper* shims over "
            "apps/cli/message_processing.py — a wrapper is a different function object from what it "
            "calls, so the two paths ran different objects until the shims became aliases. "
            "aria_cli also shed code rather than just re-pointing imports: the three offline/greeting "
            "responders moved to apps/cli/providers/llm/offline_responses.py and the sports "
            "prefetch to apps/cli/handlers/sports_handlers.py, each next to the thing whose "
            "failure or domain it belongs to, and eleven wrapper shims became aliases. "
            "ANALYSIS_SYSTEM_PROMPT is the one documented exception to the identity rule: "
            "build_analysis_system_prompt() stamps the date at call time, so the two paths hold "
            "equal strings that are never the same object, asserted by value instead. "
            "Still live for the remaining 12: _ACTIVE_COMMAND_POLICY, _ARIA_BOT_MODE, "
            "_CONFIRM_TOOLS, _HAS_ARIARC, _PROJECT_CONTEXT, execute_local_tool, get_ariarc, "
            "build_tool_system_prompt, _print_tool_result and the three approval/summary "
            "functions — all of them either CLI session state or objects whose aria_cli copy "
            "differs from the extracted module's. "
            "tests/test_ollama_stream_imports.py pins the identity rule and the budget.",
        ),
    ),
    ArchitectureLayer(
        name="tools",
        responsibility="Tool registry, schemas, permissions, local commands, and MCP adapters.",
        target_state="All tools are manifests with input schema, permission level, owner service, and deterministic output shape.",
        current_state="Legacy tools can be converted to manifests; some direct command paths still bypass the registry.",
        status=LayerStatus.PARTIAL,
        source_paths=("packages/aria_tools/", "tools/"),
        depends_on=("safety",),
        next_steps=("Route new slash commands through tool/service manifests instead of direct CLI functions.",),
    ),
    ArchitectureLayer(
        name="services",
        responsibility="Product service boundaries for data, reports, brokers, skills, channels, and gateway.",
        target_state="Business logic lives behind services; CLI, daemon, MCP, and webhooks are adapters.",
        current_state="Required service specs now cover gateway, runtime, settings, context, tools, data, reports, brokers, skills, MCP, safety, and observability; several implementations still sit in legacy modules.",
        status=LayerStatus.PARTIAL,
        source_paths=(
            "packages/aria_services/",
            "docs/architecture/service_boundaries.md",
        ),
        depends_on=("settings", "runtime", "tools"),
        next_steps=("Extract concrete SettingsService, SafetyService, ReportService, GatewayService, and ObservabilityService implementations behind the registered service manifests.",),
    ),
    ArchitectureLayer(
        name="mcp",
        responsibility="External package/tool integration through MCP and explicit adapters.",
        target_state="MCP servers expose typed tools, health, permissions, and connection status independent of CLI state.",
        current_state="Arthera Quant Engine bridge has manifests and doctor checks; lifecycle management still needs tightening.",
        status=LayerStatus.PARTIAL,
        source_paths=("packages/aria_mcp/",),
        depends_on=("tools", "services"),
        next_steps=("Add MCP reload/reconnect policy, tool provenance, and per-server failure isolation.",),
    ),
    ArchitectureLayer(
        name="safety",
        responsibility="Filesystem, shell, network, broker, and privacy guardrails.",
        target_state="Every risky action is classified, previewed when needed, audited, and blocked by default for live trading.",
        current_state=(
            "safety/service.py SafetyService unifies evaluate_tool, evaluate_command, "
            "classify_risk, privacy and trading policy, and is now constructed at its "
            "config-holding call sites: /run and /apply-plan for command policy, /feedback "
            "and /privacy for privacy settings. tests/test_safety_service_adoption.py keeps "
            "apps/cli/commands/ from reaching past it. Two defects were closed on the way: "
            "evaluate_command_policy resolved an unrecognised mode to workspace-write rather "
            "than read-only (so apps/cli/tools/system_tools.py, which defaulted the argument "
            "to the *policy* name \"safe\", silently ran at workspace-write), and the service "
            "returned a PrivacySettings from the bare `privacy` root that was a different "
            "class object from the aria_code.privacy one its callers use. "
            "Still direct, on purpose: apps/cli/tools/ takes explicit params and holds no "
            "config, and apps/cli/tool_executor.py calls the stateless classify_command_risk. "
            "The broker paths are now routed too: /broker status and /trade mode go through "
            "trading_dry_run/trading_policy. brokers/ itself stays direct on purpose — it IS "
            "the trading domain and SafetyService is the facade over it, so a call the other "
            "way would invert the layering. A third defect surfaced here: trading_dry_run "
            "documented the kill switch under a variable name nothing reads, and that "
            "docstring was its only documentation, so following it froze nothing and raised "
            "nothing. tests/test_env_switch_docs.py now checks all 27 ARIA_* names appearing "
            "in docstrings against what the tree actually reads (it also caught aria_daemon.py "
            "advertising an ARIA_API_BASE that never existed; the backend URL is the api_url "
            "config key). Broker preview/confirm and the two-gate chat-confirm gate "
            "are real and in use; credential files are written owner-only via "
            "packages/aria_core/secure_file.py."
        ),
        status=LayerStatus.PARTIAL,
        source_paths=("safety/", "brokers/", "apps/cli/commands/broker_cmds.py"),
        depends_on=("settings", "tools"),
        next_steps=(
            "Give SafetyService a privacy setter so /privacy on|off stops constructing "
            "PrivacySettings itself; until it has one, the adoption guard can only forbid "
            "deriving settings from config, not constructing them.",
        ),
    ),
    ArchitectureLayer(
        name="channels",
        responsibility="Daemon, webhook, TradingView alerts, Feishu, Telegram, and future external entrypoints.",
        target_state="Channels submit structured tasks to gateway/runtime and never call CLI internals directly.",
        current_state="apps/channels now has the channel registry (six channels with direction/capabilities/enable resolution from config+env) and the TradingView adapter: alert payloads become aria.channel_task.v1 structured tasks (passphrase/HMAC verification reused from tradingview_bridge, fail-closed on mismatch, explicit verified/open_mode flags, stable dedup keys, gateway-facing prompt that forbids order placement). The daemon webhook endpoint now refuses open-mode intake from non-loopback clients and runs alert analysis through runtime.gateway.run_turn (tool-less turn; legacy quick summary kept as fallback).",
        status=LayerStatus.PARTIAL,
        source_paths=("apps/channels/", "apps/cli/tradingview_bridge.py"),
        depends_on=("settings", "runtime", "services", "safety"),
        next_steps=("Consider context-aware disambiguation for MA (Mastercard vs moving-average) in alert prompts; otherwise the channel flow is exercised end-to-end.",),
    ),
    ArchitectureLayer(
        name="observability",
        responsibility="Doctor checks, traces, provider health, audit logs, and user-visible diagnostics.",
        target_state="Health checks explain missing services, degraded providers, unsafe configs, and incomplete architecture layers.",
        current_state="Provider and package doctor checks exist; this contract represents architecture coverage. The /architecture command renders it (layers, status, gaps, per-layer next steps; --gaps for outstanding work) and /doctor prints a one-line coverage summary. /export bundle emits the support bundle: redacted config, runtime trace, provider health (+summary), artifact summary, the architecture contract, doctor checks, and MCP server/circuit status.",
        status=LayerStatus.PARTIAL,
        source_paths=("packages/aria_infra/doctor.py", "packages/aria_services/provider_health.py", "packages/aria_core/export.py", "apps/cli/commands/diagnostic_ops_cmds.py", "aria_cli.py"),
        depends_on=("services", "mcp", "safety"),
        next_steps=("Persist bundle/doctor history for trend diagnosis, and fold durable run summaries in once the run store lands on main.",),
    ),
)


def list_architecture_layers() -> List[ArchitectureLayer]:
    """Return the product architecture layers in dependency order."""

    return list(_ARCHITECTURE_LAYERS)


def architecture_layer_map() -> Dict[str, ArchitectureLayer]:
    return {layer.name: layer for layer in _ARCHITECTURE_LAYERS}


def required_architecture_layer_names() -> List[str]:
    return [layer.name for layer in _ARCHITECTURE_LAYERS]


def architecture_gaps() -> List[ArchitectureLayer]:
    return [layer for layer in _ARCHITECTURE_LAYERS if not layer.is_complete]


def architecture_status_counts() -> Dict[str, int]:
    counts = {status.value: 0 for status in LayerStatus}
    for layer in _ARCHITECTURE_LAYERS:
        counts[layer.status.value] += 1
    return counts


def architecture_contract() -> Dict[str, Any]:
    return {
        "schema_version": ARCHITECTURE_SCHEMA_VERSION,
        "layers": [layer.to_dict() for layer in _ARCHITECTURE_LAYERS],
        "status_counts": architecture_status_counts(),
    }
