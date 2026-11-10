# Python to native: next migration boundaries

Source audit: 2026-10-29, based on the v0.164.0-derived working tree. This is a
caller/contract audit, not a CPU profile or evidence that Python itself is slow.
Historical measurements remain in [the earlier audit](python-migration-audit.md).

The measured Python `src/aria_code` tree has **559 modules / 154,267 raw lines**.
`runtime/` is 11,133 lines, CLI commands 23,097, provider layers 7,174, MCP 2,311.
Go has 64 non-test source files / 16,579 lines (116 files / 22,804 lines including
separate tests). Rust contains inline tests, so a percentage comparison across
languages would not be a meaningful migration-completion metric.

## Implemented in this increment

The new observation/action state machine, deterministic navigation, host-stored
plans, lease/revision checks, STOP latch, bounded audit trace, JSONL service and
local HTTP UI run in Rust (`rust/physical`). `aria-native physical` calls the same
library directly. No Python core or cloud service is needed for this path.
Python supplies an independent executable-test oracle, not its production planner.
This avoids adding another Python implementation that must later be ported.

This is a new native subsystem, **not removal of an existing Python subsystem**.
The installed application's Python worker and its compatibility routes remain.

## Prioritized existing Python slices

| Priority / slice | Target | Evidence and replacement gate |
| --- | --- | --- |
| P0: relay and background lifecycle | Go | Relay done (2026-11-01): `Dockerfile.relay`, the root image's Cloud Run entrypoint and CI run `go/relay`; `aria_relay_server.py` and `relay_store.py` are removed after the shared contract pinned their behaviour (Python and Go both passed it, including a database carried over in both directions). `aria_daemon.py` (1,436), `aria_feishu_bot.py` and the relay client stay Python: the bot calls the Python market, finance and sports code directly. |
| P0: MCP protocol server | Go | `packages/aria_mcp/server.py` is 1,687 lines; the package is 2,311. `pyproject.toml` still exposes the Python `aria-code-mcp` entrypoint. Preserve tool names, schemas, approval/auth, cancellation and external Python consumers through a compatibility shim, then switch packaging and test actual clients. |
| P1: semantic diff / change impact | Rust / Go | `semantic_diff.reverse_apply` and `symbol_changes` ask `aria-native index changes` first. The delivery report's impact line now reads the same on-disk graph cache in Go and does not scan the tree. Python remains the fallback and the graph builder. Do not delete it. |
| P1: update installation | Rust | `apps/cli/updater.py` is 316 lines. `app_server.py` still supplies `updater.install_native` to auto-update; Rust update support alone does not replace installation. Preserve version/channel checks, verification, atomic replacement, rollback, platform permissions and frozen-layout behavior before switching. |
| P1: remaining command/session/config routes | Go | CLI command handlers total 23,097 lines. Native `commands.go` already covers a subset; migrate one public command family at a time, including history/state and permission behavior. Keep provider/domain-specific Python routes until adapters and actual release entrypoints are verified. |
| P2: remaining execution/acceptance/state adapters | Go | Go already has `agent.go`, `acceptance.go`, `risk.go`, `contract.go`, checkpoint and session storage. A local model that writes tool calls or continues the prompt as text is sent back, then the turn stops as `text_tool_calls` or `prompt_echo` instead of completed. Before a model turn, history past `auto_compact_threshold` (default 78% of `context_window` / `num_ctx`) is summarised locally, without another model call. `/compact` still asks the model. A check whose tool is missing is `not_run`, not a failure, and the delivery report says to run it yourself. The report names a definition from the current file and this turn's diff, and names tests from the on-disk graph cache. The gate fingerprints those paths and the host's `acceptance_revision_inputs` before and after the commands; a later edit or a fingerprint it cannot read leaves `verified` unset. Still Python: whole-turn restore, and the syntax-tree reading of a file the regex symbols do not cover. Check SQLite compatibility, cancellation, OS containment and public callers before replacing those wrappers. A matching filename is insufficient. |

## Keep Python where it still provides value

- Numeric/data services using pandas/numpy/scipy and vendor-specific market SDKs;
  preserve those implementations as independent oracles while comparing native cores.
- Model training/evaluation and future `rclpy`/robot-learning SDK integration where
  the upstream ecosystem is Python. Such SDK integration is not implemented here.
- Thin compatibility adapters required by external consumers until their imports
  and deployment pins have actually moved.

The old Python terminal rendering should eventually be retired, not translated
line for line, after the Rust frontend and native command inventory replace all
required behavior. The installed worker still needs Python today.

**Decision:** the end state is the same as Codex. The core (Rust TUI and Go runtime)
runs without Python. Finance, markets and data stay in Python as the separate
`financial-service`, reached over MCP/HTTP. There will be no pure-Python install
to maintain alongside the native one. [The command migration matrix](command-migration-matrix.md)
tracks where each of the 182 Python commands goes and which batches are done.

## Physical service architecture

Rust owns navigation/state validation in this increment. A future Go gateway is a
reasonable home for authenticated multi-tenant APIs, device connection lifecycle,
job queues, retries and durable event storage. It must not retry physical commands
blindly: device acknowledgements and idempotency must be explicit. A real-time
controller and independent hardware stop remain outside cloud model inference.

The Rust localhost HTTP adapter is a single-user development endpoint, not that
production gateway. The logical grid is not physics validation or sim-to-real proof.
See [native navigation](physical-navigation.md) for boundaries and runnable checks.

## Exit criteria for each migration

1. Identify every current public caller, transport and persistence contract.
2. Implement the native route and retain a deliberate compatibility route.
3. Compare observable effects, refusals, cancellation and saved-state fixtures.
4. Change actual packaging/deployment callers and validate supported platforms.
5. Only then remove the Python implementation. Report measured latency/memory only
   after controlled same-task measurements; language counts are not performance.
