# Warehouse task agent

The user describes an objective, a map/destination and constraints. Aria's existing
model understands that request and delegates a bounded task to `warehouse_task`.
The user does not need to assemble an observe/plan/step API sequence.

The agent has an explicit goal contract, per-mission working memory, capabilities,
feedback-driven route decisions, resource limits and completion verification. Its
Rust controller observes current state, selects or repairs a route, executes a
validated movement and checks the outcome. An unreachable goal becomes
`needs_input`; budgets become `failed`; a start acknowledgement never means success.
The host model explains results and decides how to handle missing input, rather
than silently changing the user's objective or increasing limits.

## Scope

This is a **logical simulation task agent**. It has no physical hardware driver,
physics simulator, real sensor feed or learned navigation policy. BFS is its local
route capability; natural-language interpretation stays in the calling model.
No second LLM or separate agent framework is introduced. The task controller makes
no inference calls. The existing host model's normal inference costs still apply.

This capability does not inherit the legacy read-only financial `BaseAgent.analyze`
contract: analysis results and stateful task execution have different lifecycles.
It is exposed as one coherent task-agent capability through Aria and MCP.

## Aria Code source preview

```sh
cd rust
cargo build --locked -p aria-physical -p aria-native
cd ..
python -m pip install -e .
export ARIA_PHYSICAL_BINARY="$PWD/rust/target/debug/aria-physical"
python -m aria_code.aria_cli
```

Example user request:

> 在二维示例仓库里，把机器人从 [0,0] 送到 [4,0]。地图 5×4，没有初始障碍；最多 12 步、10 秒。遇到障碍就调整路线，无法到达时告诉我原因，完成后给我实际位置和验证结果。

The model gets workflow knowledge and the `warehouse_task` schema for relevant
navigation requests. Tool results contain the task ID, status, actual position,
constraints, decision records and verified outcome. `wait` is bounded to one second
per call; the controller continues between calls. STOP interrupts a running task
without requiring another model planning loop. `trace` reads the complete bounded
engine snapshot for that mission.

The default Python chat worker registers this capability. Python is only a thin
process adapter; it does not reimplement planning or movement. The standalone Go
chat worker has not gained an in-process delegate in this increment; use the native
MCP capability or direct native entrypoint for that integration. Installed stable
releases remain unchanged until normal packaging/release validation.

## Task contract and memory

Actions: `health`, `create`, `start`, `status`, `wait`, `stop`, `obstacle`, `trace`.
Creation requires a goal, scenario, and unique request ID. Start and obstacle
updates also use stable request IDs. Repeating the same mutation returns current
state without performing it again; reusing an ID with different content fails.

Each mission owns its own simulator. Each trusted Aria conversation owns a separate
native process; model-supplied scope fields cannot select another conversation.
The MCP stdio process is one client trust boundary. Task IDs are not authentication
credentials and this implementation is not a multi-tenant cloud service.

Defaults: 128 steps, 30 seconds; hard limits: 512 steps, 120 seconds, 32 missions
per native process and 16 host conversation processes. Memories are **process-local**.
EOF/disconnection stops unfinished tasks. There is no automatic resume after a
process restart, no durable fleet state, and no transparent retry after an uncertain
process failure. Save a trace through the host's authorized file tools if needed.

## MCP

The existing `aria-code-mcp` server exposes `aria.warehouse.task`. There is also a
standalone native MCP server with no Python dependency:

```sh
./rust/target/debug/aria-physical mcp
# Equivalent: ./rust/target/debug/aria-native physical mcp
```

It supports MCP stdio initialize, tool discovery, tool calls, ping and notifications.
Only requests with IDs execute tools. The raw `agent-jsonl` protocol is for trusted
host adapters and is not MCP. The original localhost `/api` grid UI remains a
separate single-user development fixture; this increment does not expose an
unauthenticated remote MCP endpoint or a production cloud gateway.

Example local client launch commands (use absolute executable paths):

```sh
codex mcp add aria-warehouse -- /absolute/path/aria-physical mcp
claude mcp add --transport stdio aria-warehouse -- /absolute/path/aria-physical mcp
```

## Verification

```sh
cd rust
cargo test --workspace --locked
cd ..
PYTHONPATH=src ARIA_PHYSICAL_BINARY="$PWD/rust/target/debug/aria-physical" \
python tests/test_warehouse_task_agent.py
```

Tests cover adaptive replanning, unreachable goals, budgets, STOP, idempotency,
conversation isolation, trusted read-only policy, actual Aria registration and
MCP stdio discovery/calls. A real-model acceptance should separately demonstrate
that the model translates a user objective into a valid task, waits for terminal
state, and reports the tool-verified outcome. Service tests alone are not that
model acceptance evidence.
