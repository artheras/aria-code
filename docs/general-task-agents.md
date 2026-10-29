# General task agents

Aria's default Python chat runtime now supports domain-neutral task roles. A
parent model interprets the goal, chooses a role, provides context and acceptance
criteria, and checks the returned evidence. Financial analysis and warehouse
simulation remain optional specialisms rather than the contract for every task.

This builds on Aria's existing model loop, tools, task ledger and isolated Git
worktrees. It does not add another agent framework or require another model key.
These are model-backed task roles, not subclasses of the legacy financial
`BaseAgent.analyze(symbol, data)` interface.

| Role | Work | Tools and mode |
| --- | --- | --- |
| `general` | Tasks without a narrower role | Parent's local tools, minus delegation; read-only by default |
| `explorer` | Source discovery and execution-path explanations | File inspection, no shell or edits |
| `planner` | Goals, dependencies and acceptance plans | Inspection and available search/fetch tools, no edits |
| `researcher` | Sourced product, technical and business research | Inspection and available search/fetch tools, no edits |
| `implementer` | Bounded software implementation and checks | File, notebook and command tools under host policy |
| `reviewer` | Independent correctness and security review | Static inspection, no shell or edits |
| `data_analyst` | Data quality and reproducible calculations | File, notebook and command tools under host policy |
| `writer` | Specifications, documentation and content drafts | File, notebook and command tools under host policy |

All roles default to read-only. Only general, implementer, data_analyst and writer
can request workspace-write, which requires a Git worktree. The tool list is an
intersection with tools actually registered by the host: a research role does not
establish a browser connection, and a writer does not install a document renderer.
Read-only commands remain subject to Aria's existing command policy; a role cannot
approve its own commands. Reviewers report static inspection rather than claiming
to have executed tests. Publication and messaging are not part of these roles.

## Usage

After installing this source revision, start Aria Code with the same model/provider
configuration as usual. Describe the objective in normal language, for example:

> Ask a research assistant to read brief.md for this education product. Identify
> the audience, three requirements and their acceptance criteria. Wait for the
> task, inspect its source evidence, then summarize in Chinese. Do not edit files.

> Have an explorer trace the login flow. Then ask a reviewer to inspect the
> relevant sources for correctness issues and cite file locations.

> Inspect this survey dataset for missing values and duplicate respondents. Use
> reproducible calculations and report limitations before drafting the findings.

The model discovers roles through `list_agents` and delegates with `spawn_task`.
Simple or dependent work can remain in the main conversation; seven roles are not
seven mandatory pipeline stages. The model selects roles semantically rather
than a keyword classifier deciding which industry owns the request.

Tool contract example:

```json
{
  "agent": "researcher",
  "prompt": "Read brief.md and extract the intended audience and requirements.",
  "success_criteria": "Cite brief.md, distinguish facts from assumptions, and list unresolved questions.",
  "mode": "read-only",
  "max_rounds": 12,
  "timeout_seconds": 120
}
```

`task_status`, `task_result` and `task_cancel` retain the existing interface.
Task records now retain the selected role, acceptance criteria and budgets across
restart. Restarted running tasks become interrupted, not silently resumed.
`done` means the runner returned without a runtime error. Declare machine predicates
in `spawn_task.checks`, then inspect `task_result.verified`: true means these predicates
passed, false means failed checks, and null means none were requested. The parent
must still review semantic correctness and completeness. A handoff's VERIFICATION
field is the delegate's report, not a signed test result. See
[declared task verification](task-verification.md) for file, tool and command checks.

## Enforced execution boundaries

- The child uses an empty conversation history and the existing parent model.
- The role narrows both offered schemas and the executor's allowed tools;
  unsolicited local and remote tool calls outside that set fail.
- Nested task delegation is excluded from child tools.
- A child cannot upgrade a read-only parent. Underscore-prefixed model arguments
  cannot override the trusted host permission or workspace context.
- Write tasks still use existing isolated worktrees and explicit apply behavior.
- Roles cap model rounds (20 or 30) and timeout (300 seconds). Callers may lower
  these limits. Eight pending/running tasks may exist per scheduler process.
- Timeouts cancel the async task and mark it failed. Already-running synchronous
  tools have their own cancellation/timeout behavior; the task timeout is not a
  hard process-kill guarantee or an OS sandbox.
- A runtime error with partial text is a failed task, not a successful result.
- `ARIA_TASK_LEDGER_PATH` remains the explicit ledger override; otherwise
  `ARIA_CONFIG_DIR`, then `ARIA_HOME`, then the existing `~/.aria` fallback is used.
  Existing ledger files are not automatically moved or merged.

Specialized roles currently require the Aria backend. Legacy `backend=claude` or
`backend=codex` general tasks remain separate external CLI adapters; their CLI
availability, permission handling and account access are not established by this
change. The Go chat worker, hosted multi-tenant sessions, desktop UI and MCP
server do not automatically gain this Python role scheduler through these files.

## Comparison and remaining work

The public Codex implementation applies bounded role overrides to a parent-derived
configuration rather than replacing its authority:
[role.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/agent/role.rs).
Codex documents general/worker/explorer roles and project-scoped custom definitions:
[Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
Claude Code documents separate task contexts, role descriptions, scoped tools and
custom definitions:
[Claude Code subagents](https://code.claude.com/docs/en/sub-agents).
Reusable workflow knowledge belongs in skills:
[OpenAI plugin skills](https://developers.openai.com/plugins/concepts/skills).
This comparison uses public sources; it does not establish proprietary internals.

The repository already has a runtime loop, approval handling, skill loading,
memory components, task ledger, external CLI adapters and domain packs. The useful
next increments are integration and evidence rather than more industry labels:

| Priority | Gap | Next concrete increment |
| --- | --- | --- |
| Delivered | General roles and working delegation | Shared catalog, role tool restrictions, async scheduling fix, budgets and persisted contracts |
| Delivered | Declared machine predicates | Bounded artifact read-back and host-observed tool/command receipts; see task-verification.md |
| Next | Broader acceptance | Current-revision command binding, semantic review and document rendering checks |
| Next | Skills and scoped memory in delegates | Load relevant authorized workflow knowledge with provenance and tenant/project boundaries |
| Next | Custom task-role definitions | Validated declarative project roles; reject permission upgrades and untrusted executable configuration |
| Next | Documents and datasets | Connect actual renderers/computation tools and verify exported files with representative tasks |
| Later | Browser and business operations | Authorized connectors, account selection, preview/approval and read-back verification |
| Later | Scheduling and cross-platform hosts | Durable idempotent jobs, review inbox and shared Chat/Work/Code/MCP adapters with tenant isolation |
| Later | Physical execution | Simulator/sensor transport, calibrated safety constraints and measured outcomes before hardware |

## Verification

```sh
PYTHONPATH=src pytest tests/test_general_agent_profiles.py tests/test_subagent.py \
  tests/test_subagent_workspace.py tests/test_runtime_tool_executor.py \
  tests/test_runtime_bridge.py tests/test_runtime_gateway.py \
  tests/test_output_discipline_prompt.py tests/test_tool_scope.py \
  tests/test_headless_command_approval.py
```

The live acceptance fixture should exercise a non-financial task through the real
CLI, inspect the selected role and tool evidence, retrieve its final result and
check the source/artifact state. Model runs are separate from deterministic tests
and incur the configured provider's normal inference costs.
