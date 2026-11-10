# Durable task briefs

Python RunStore.create_run now records initial task intent in the same SQLite transaction as the run and its creation event. Existing Python CLI callers use this entry point, so ordinary new turns retain their original goal without a separate scheduler or model call. A task brief contains a goal, boundaries, a concise decision rationale, and textual acceptance criteria. It is immutable through this API. A changed agreement requires a new run, optionally linked with parent_run_id; the old agreement remains available.

```python
from aria_code.runtime.run_store import RunStore
from aria_code.runtime.task_brief import TaskBrief

store = RunStore('/tmp/aria-demo/runs.sqlite3')
run = store.create_run(
    session_id='local-research',
    prompt='Build a reproducible offline navigation experiment',
    workspace='/tmp/aria-demo',
    task_brief=TaskBrief(
        goal='Build a reproducible offline navigation experiment',
        boundaries=('Simulation only', 'Do not actuate hardware or publish'),
        rationale='Use deterministic inputs so another researcher can replay failures',
        acceptance=('Replay matches the saved trajectory', 'Blocked goal requests input'),
    ),
)
# A second process can retrieve the same record after restart.
view = RunStore(store.database_path).get_task_brief(run.run_id)
assert view['acceptance_defined']
assert view['verification_state'] == 'unverified'
```

For existing callers, the default brief contains only the prompt as its goal: empty criteria explicitly mean acceptance_defined=false. Empty or over-64-KiB legacy prompts retain the existing run behavior and have no automatically generated brief. Old database rows are not backfilled from mutable metadata. Missing briefs return None; missing runs raise RunNotFoundError; invalid stored schema or a digest mismatch raises ValueError. Hosts must present missing/invalid intent truthfully.

Bounds: goal 64 KiB; rationale 16 KiB; up to 32 boundaries and 32 criteria, each at most 4 KiB of UTF-8. Known credential patterns are redacted before storage and hashing, using the existing RunStore redactor. This cannot identify every sensitive value, and is not a privacy guarantee. Creation events contain only the brief digest, not a second copy of the full brief.

The digest detects accidental content corruption; anyone with database write access can replace both content and digest. This is neither a signature nor user approval. Criteria are inert text: creating or reading a brief never executes commands, publishes content, resumes a run, grants capabilities, or establishes verification. Lifecycle success does not change its unverified label. Existing ChangeContract, command policy, approvals, and actual acceptance evidence still apply independently.

RunStore is a trusted local-host persistence API, not an authenticated HTTP endpoint. Callers must enforce existing session/workspace/tenant ownership before reading a brief. Do not expose get_task_brief directly by accepting arbitrary remote run IDs. The Go runtime, desktop chat display, and connector-facing task tools do not yet consume these briefs; this change does not claim a complete cross-platform workflow UI or automatic validation of every criterion.
