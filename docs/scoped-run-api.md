# Host-scoped run API foundation

`runtime.scoped_runs.ScopedRuns` provides create/get/list/events on the existing SQLite RunStore. It does not start a second scheduler or expose an HTTP listener. Creating a run records a queued intent; it does not dispatch work or replay a previous tool call.

```python
from aria_code.runtime.run_store import RunStore
from aria_code.runtime.scoped_runs import RunGrant, ScopedRuns

# The backend must first authenticate the caller and authorize the project,
# session and canonical workspace. Obtain all of these values from that host
# context, never from model arguments or client-supplied scope fields.
grant = RunGrant(organization_id, project_id, user_id, session_id, approved_workspace)
tasks = ScopedRuns(RunStore(database_path), grant)
run = tasks.create('Prepare a draft for review')
summary = tasks.get(run['run_id'])
events = tasks.events(run['run_id'], after=0, limit=50)
```

Bindings are persisted in a separate table, independent of model metadata. Reads require the same organization, project, subject, session and canonical workspace. This first increment intentionally allows only the session owner, not all members of a project. Missing and out-of-scope run IDs produce the same Task not found error. Scope filtering happens in SQL before list limits. Events use a strictly increasing sequence cursor and bounded pages.

Summary and event metadata exclude prompt, filesystem paths, error details and event payloads. This is a minimal task-card interface; tools, outputs and artifacts require separately authorized endpoints before they can be displayed. A succeeded run remains unverified here because RunStore lifecycle alone does not establish declared acceptance.

The underlying RunStore is still a trusted-host API. Any service which also exposes its unscoped methods defeats this adapter's boundary. RunGrant is a host capability, not a token validator; never deserialize it directly from a request. This change supplies no login, JWT validation, organization directory, membership check, network rate limit, cross-session sharing or production tenancy certification.

Run creation and binding use separate existing-store transactions. A crash between them can leave an unbound run. Such records are denied; no ownership migration or inference from legacy metadata is attempted. A future dispatcher must consume only explicitly bound/authorized records and implement its own crash-safe claim and idempotency contract. Do not feed every legacy queued record into execution.

Before network adoption: add a real authentication adapter, authorize both project and session membership, map permission errors without leaking existence, apply request/rate limits, and test every endpoint with another organization's credentials. Desktop/Go/MCP adapters, approval actions, result evidence refresh and durable resume remain separate work.
