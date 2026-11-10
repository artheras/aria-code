# Project skills and memory for delegated tasks

Delegates can now receive selected project-local workflow guidance and memory.
The parent discovers metadata, chooses IDs for the task, and the host reads a
bounded snapshot with source provenance. This does not scan a user's memory index,
global skill catalogs, unrelated projects or arbitrary filesystem paths.

The existing portable skill loader and MemoryManager keep their main-conversation
interfaces. This task-specific path deliberately does not invoke global discovery
or load every indexed memory. It reads only project sources declared below.

## Configure a project

Create `.aria/task-context.json` in the workspace that owns the task:

```json
{
  "version": 1,
  "entries": [
    {
      "id": "product_brief",
      "kind": "skill",
      "path": ".aria/skills/product-brief/SKILL.md",
      "description": "Draft factual customer copy from a product brief.",
      "roles": ["writer", "researcher"],
      "auto": false
    },
    {
      "id": "voice",
      "kind": "memory",
      "path": ".aria/memory/voice.md",
      "description": "Approved plain-English project voice.",
      "roles": ["writer"],
      "auto": true
    }
  ]
}
```

The manifest references existing UTF-8 Markdown or text files inside this workspace.
Skills may be existing `SKILL.md` workflow files; their full bounded text is supplied
as guidance. This path does not run their scripts, import policies from sibling
files, load supporting assets automatically or install plugins. A skill's text
cannot grant tool or filesystem access. Memories are fallible source context,
not verification of current facts.

`roles` defaults to all known task roles. `auto` defaults to false. Set auto only
for project preferences that should accompany every eligible task. An optional
`sessions` array restricts automatic selection and metadata visibility to those
exact host-provided session IDs. Without a matching trusted session ID, that
entry cannot be selected. Empty sessions means no additional session filter.

Session IDs are selection filters, not credentials. The host must already own an
authorized workspace; this manifest is not a multi-tenant access-control system.
Read tools may independently access files under their existing permissions.

## Delegate with context

In the source preview, start Aria in [the runnable example](../examples/task-context).
Keep the same provider/model configuration:

```sh
cd /absolute/path/to/aria-code
python -m pip install -e .
python -m aria_code.aria_cli -C "$PWD/examples/task-context"
```

Example request:

> Start an independent writer task to draft English customer copy from brief.md.
> Discover the writer's available project context and select the product-brief
> workflow. Apply the saved project writing preferences, wait for the task, and
> return its draft and source provenance. Do not edit, send or publish anything.

The parent calls `list_task_context` with `agent=writer`. Discovery returns eligible
IDs, descriptions, kinds and automatic-selection metadata, without reading source
bodies. Then it passes selected IDs, not paths, in `spawn_task.context_refs`:

```json
{
  "agent": "writer",
  "prompt": "Read brief.md and draft factual English customer copy.",
  "context_refs": ["product_brief"],
  "mode": "read-only"
}
```

The host supplies the selected workflow and the eligible auto voice memory. An
entry is loaded once even if it is both automatic and explicitly selected. Without
a manifest, an empty context_refs list preserves existing behavior. Explicitly
requested missing/ineligible IDs fail before a task starts; unavailable auto sources
also fail rather than silently losing the requested project guidance.

The host can set `task_context_enabled=false` to disable both discovery and loading.
Model-provided underscore arguments cannot override this flag, the workspace or
the actual host session ID.

## Provenance, snapshots and storage

Task status/results expose `context_sources`: source ID, kind, workspace-relative
path, project fingerprint, SHA-256 of the exact loaded bytes, byte count and how
the source was selected. The fingerprint derives from the canonical workspace
path; it is not an authentication identity or a stable cross-machine project ID.

Sources are frozen at delegation. A writing worktree receives the parent project's
snapshot, while its file tools still act in its isolated worktree. Editing a source
after delegation does not silently change a running task's context. A later task
loads a new snapshot. Provenance establishes what was supplied, not whether a model
followed it correctly or whether its factual claims are current.

The task ledger records references and provenance, without an additional copy of
the raw source snapshot. Restored records retain that metadata but do not reload
source bodies or resume interrupted tasks. Delegates' outputs may quote their
sources, and existing provider/trace/session logging can retain prompts or outputs.
This is not encrypted storage or a guarantee that supplied text is never persisted.
Supplied context goes to the task's configured model provider just like its prompt.

## Local provider boundary

For delegated Ollama turns, the adapter disables ambient context: global memory,
global skill discovery, cached prose, parent project summaries, ariarc/broker
context, implicit market/sports prefetch and automatic file-path pre-reading.
The new selected project block is supplied in the task prompt. Normal main-chat
Ollama calls retain their prior defaults. Tests exercise the actual request builder
at the HTTP boundary and check that ambient sentinels are absent.

Remote provider services can have their own server-side policies and logging;
this host-side feature does not audit or change those services. Scoped context
currently requires the Aria backend. Legacy external Claude/Codex general adapters
do not automatically receive these sources. Standalone Go, desktop UI and MCP
scheduler integration remain separate work.

## Bounds and verification

Limits are a 32 KiB manifest, 64 entries, eight selected/automatic sources, 8 KiB per
source and 16 KiB total. Budgets count UTF-8 bytes, not characters. Invalid or empty
sources are rejected rather than silently truncated. Unknown fields, executable
configuration, absolute/parent/drive paths and symlink components are rejected.
The bounded workspace reader is shared with task artifact verification.

Context does not relax the role's allowed tools, permission mode, approvals or
declared acceptance predicates. `context_sources` is not a `verified=true` claim;
use [task verification](task-verification.md) for machine-checkable outcomes and
review semantic correctness separately.

```sh
PYTHONPATH=src pytest tests/test_task_context.py tests/test_general_agent_profiles.py \
  tests/test_subagent.py tests/test_subagent_workspace.py tests/test_task_verification.py \
  tests/test_ollama_stream_tools.py tests/test_ollama_stream_imports.py
```

Tests cover lazy metadata discovery, role/session/project selection, trusted host
context, disabled mode, budgets, unsafe paths, immutable snapshots, ledger storage,
worktree provenance and actual Ollama request assembly. A live provider acceptance
should separately demonstrate real selection and delegation, not a parent model
that skips the task and writes a draft itself.
