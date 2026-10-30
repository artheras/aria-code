# Shared task outcome view (version 1)

The Python SDK adds `data.task_outcome` to `task_status` and `task_result` tool events. Existing `data.result` is unchanged. Other tool events and normal chat results do not acquire task verification labels.

Execution and acceptance are independent: `execution_state` retains the host task lifecycle; `verification_state` is `verified`, `failed` or `unverified`. A completed task without declared checks remains unverified. Execution failure or cancellation must never be rendered as successful work; SDK cancellation now returns `success=false` while preserving partial output.

Only a fresh `task_result`, whose existing scheduler rereads artifact evidence, may supply a verified or failed acceptance label. `task_status` is a snapshot and always requires refresh. Stale, contradictory, malformed and missing reports remain unverified. Verified means the declared predicates passed, not that the whole business goal is semantically correct.

The compact projection includes contract version, execution state, acceptance state, nullable verified value, check count, refresh requirement and acceptance scope. It does not copy source text, commands, artifact contents, context provenance or raw evidence. The original tool event still has its existing result payload and access requirements; this is not a redaction boundary or a public API.

## Adoption order

1. Consume this additive SDK view in a task card that distinguishes execution from acceptance. Use task_result for refreshed evidence; never infer verification from model prose or done status.
2. Define authenticated project/session ownership before exposing scheduler endpoints to another process. Do not expose the local ledger as a shared multi-user store.
3. Add ordered run events, artifact identifiers and an approval inbox, with client contract fixtures. Keep conversational Chat separate; Work and Code can share task execution.
4. Add durable scheduling only with persisted inputs, idempotency, explicit interrupted-state recovery and tests for uncertain side effects. Do not blindly replay commands or publications.

Desktop, Go worker and MCP task scheduling are not implemented by this projection. No remote endpoint, durable retry, automatic resume, context graph or enterprise authorization is claimed. Navigation remains a logical simulation, not hardware control.

## Measurement definitions

Record both the cohort and denominator. Verified completion rate is verified terminal tasks divided by all terminal tasks in the cohort, including cancelled/interrupted tasks. Separately report tasks with no declared checks. Recovery rate needs linked attempts; intervention rate needs explicit human-action records. Cost per verified task and time to verified result require actual metering and timestamps. Missing measurements must remain unavailable, not zero. This change does not invent or emit those metrics.
