# Go project-review migration candidate

This service is the Go implementation of the hosted v1 project-review and
remote-workspace HTTP surface. It uses only the Go standard library and is built
with the repository-pinned Go 1.27.3 toolchain.

The active production image remains `Dockerfile.review` (Python/FastAPI).
`Dockerfile.review-go` builds this candidate. It is intentionally not wired to
the live deployment until report fidelity and all consumer contracts have
been compared.

## Run

```sh
go run .
```

Set `ARIA_CODE_REVIEW_TOKEN` before using any `/v1/*` endpoint. The health
endpoint is public. `PORT` defaults to `8787`,
`ARIA_CODE_WORKSPACE_ROOT` defaults to a temporary directory, and
`ARIA_CODE_WORKSPACE_TTL_HOURS` defaults to 24 hours and is clamped to seven
days. Uploaded project archives are limited to 25 MiB; archive contents are
never executed, and sensitive files, links, generated folders, and oversized
members are excluded.

## HTTP surface

- `GET /health`
- `POST /v1/project-reviews`
- `POST /v1/workspaces`
- `GET|DELETE /v1/workspaces/{workspaceID}`
- `POST /v1/workspaces/{workspaceID}/files/read`
- `POST /v1/workspaces/{workspaceID}/files/list`
- `POST /v1/workspaces/{workspaceID}/search`
- `POST /v1/workspaces/{workspaceID}/changes`
- `GET /v1/workspaces/{workspaceID}/changes/{changeID}`
- `POST /v1/workspaces/{workspaceID}/changes/{changeID}/apply`
- `POST /v1/workspaces/{workspaceID}/changes/{changeID}/rollback`

Every `/v1/*` request uses the existing bearer-token contract. Workspace
ownership is represented by a SHA-256 digest; raw owner and run identifiers are
not returned. Change application uses base and proposed content hashes to
reject stale approvals, and rollback refuses to overwrite subsequent edits.

## Cutover gates

The API shape is ported, but this candidate has not yet passed route-level
parity checks against the Python service. In particular, static-review findings
and edge-case error responses need comparison with the active implementation
before changing the deployment image. Keep the Python service available until
the consuming Arthera backend has exercised the complete workspace lifecycle
against this service.
