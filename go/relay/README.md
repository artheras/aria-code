# aria-relay (Go)

Aria's Feishu relay. It replaced the Python relay (`aria_relay_server.py`,
removed in November 2026) and is the first Go service in the migration
(`docs/rust-cli-migration.md`: Rust for the CLI and local work, Go for network
services). `Dockerfile.relay` builds it for production (Cloud Run via
`cloudbuild.yaml`, or `docker-compose.prod.yml`), and the root `Dockerfile`
runs it when Cloud Run sets `PORT`.

## Same contract

`tests/test_relay_contract.py` starts a relay process, talks to it only over HTTP
and WebSocket, and stands in for Feishu with a local fake. Both implementations
passed it, and a database written by either worked in the other, before the
Python relay was removed. It now holds the Go relay to that behaviour:

```sh
(cd go/relay && go build -o aria-relay .) && pytest tests/test_relay_contract.py
```

The contract covers `/status`, event verification (token and signature), the
URL challenge, registration (trust on first use, `RELAY_SECRET`), binding,
forwarding with an immediate answer to Feishu and de-duplicated retries, the
offline and unbound notices, sending on a client's behalf and what is refused,
card presses, and reconnects. It also covers what used to be checked only by
the Python relay's own unit tests: a refused registration does not evict the
connected machine, a rotated bind code replaces the old one and needs the
machine's token, events without an id are not dropped, a failed tenant-token
fetch is not cached, a send Feishu cannot take is answered rather than dropped,
the Cloud Run SQLite warning, and an unknown `RELAY_STORE` failing at start. CI (`.github/workflows/go-relay.yml`) also runs
the Go unit tests and the contract with the race detector, and builds the image.

The environment is the Python relay's: `PORT`, `DB_PATH`, `RELAY_STORE`
(`sqlite` or `firestore`, with `RELAY_FIRESTORE_PROJECT` /
`RELAY_FIRESTORE_DATABASE`), `FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
`FEISHU_VERIFICATION_TOKEN`, `FEISHU_ENCRYPT_KEY`, `RELAY_SECRET`,
`RELAY_ALLOW_UNVERIFIED_EVENTS`, `MESSAGE_TIMEOUT`, and `FEISHU_API_BASE` (for
tests). The SQLite schema and the Firestore layout are the same, so either
implementation opens the other's data.

## Measured here

| | Python | Go |
|---|---|---|
| Ready to answer `/status` | 0.41 s | 0.02 s |
| Idle memory (RSS) | 51 MB | 19 MB |
| Contract run (25 tests) | 38 s | 19 s |

The static binary is about 23 MB (most of it the Firestore client).

## Known differences

- A frame that is not a JSON object is skipped; the Python relay closes the
  socket on it.
- Bind codes are normalised with Go's Unicode upper-casing and letter/number
  classes; Python's differ for a few characters (`ß` → `SS`). Generated codes
  are ASCII, so real codes normalise the same way.
- The Firestore store is tested against an in-memory fake of Firestore's
  document semantics (`store_test.go`), not a live database or the emulator.
  Verify it against a staging Firestore before switching production.

## Deploying and rolling back

The SQLite schema and the Firestore layout are the Python relay's, so a deploy
keeps every binding and machine credential, and Cloud Run can still roll back
to the last Python revision (same data) if needed:
`gcloud run services update-traffic <service> --to-revisions=<python-revision>=100`.

`cloudbuild.go.yaml` deploys a build as a tagged revision with no traffic
(`go-candidate`), for checking `/status` and a test Feishu app before moving
traffic.

The static executable supports `--healthcheck` (three-second `/status` deadline,
JSON/storage validation and a nonzero exit on failure); the image and compose use
it, so distroless needs no curl or shell. The relay runs as UID 65532: a new
SQLite volume is created owned by it, and `docker-compose.prod.yml` hands an
existing volume (written by the Python relay as root) over once before start.
Neither implementation changes the database schema.
