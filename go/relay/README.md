# aria-relay (Go)

The Go implementation of Aria's Feishu relay, `src/aria_code/aria_relay_server.py`.
It is the first Go service in the migration (`docs/rust-cli-migration.md`: Rust
for the CLI and local work, Go for network services). **Production still deploys
the Python relay** (`Dockerfile.relay`); switching is a separate, deliberate step.

## Same contract

`tests/test_relay_contract.py` starts a relay process, talks to it only over HTTP
and WebSocket, and stands in for Feishu with a local fake. Both implementations
must pass it, and a database written by either must work in the other:

```sh
cd go/relay && go build -o aria-relay . && cd ../..
pytest tests/test_relay_contract.py                                   # Python relay
ARIA_RELAY_COMMAND=$PWD/go/relay/aria-relay pytest tests/test_relay_contract.py   # Go relay, plus switching
```

The contract covers `/status`, event verification (token and signature), the
URL challenge, registration (trust on first use, `RELAY_SECRET`), binding,
forwarding with an immediate answer to Feishu and de-duplicated retries, the
offline and unbound notices, sending on a client's behalf and what is refused,
card presses, and reconnects. CI (`.github/workflows/go-relay.yml`) also runs
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

## Switching production

1. Deploy `go/relay/Dockerfile` as a separate Cloud Run revision or service with
   the same environment (`RELAY_STORE=firestore`) and no traffic.
2. Check `/status` and send a few messages through it from a test Feishu app.
3. Move traffic; keep the Python revision for rollback (same data, both ways).
4. Update `cloudbuild.yaml` to build `go/relay/Dockerfile`. The compose
   healthcheck uses `curl`, which the distroless image does not have.
