# Rust/Go runtime and business migration

The requirement is to retain every existing business capability, including
finance, backtesting and model training, and remove old Python implementations
only after functional replacement is proven. The entire product migration is
not complete. The current installed release remains on its existing worker;
the new Go service is explicitly selectable with `ARIA_RUNTIME` for native
`chat`, `exec` and `tool` verification. Production services and release packages are not switched
by this branch.

## Current implementation

| Area | Native replacement | Evidence and remaining boundary |
| --- | --- | --- |
| Terminal | `rust/cli/src/chat/` | Persistent editing, Markdown, chronological operations, Chinese/English display, reviews and cancelled-turn state. A rounded multiline composer and a hand-tuned compact cream robot with four feet, aligned eyes and a thin orange stripe. |
| Application protocol | `go/runtime/server.go` | Actual executable barrier, approvals, multi-turn persistence, resume, idle cancellation and broken-output shutdown. Commands/events beyond the implemented catalog remain to port. |
| Headless and file bridge | `go/runtime/stream.go`, `bridge.go` | Existing single-turn events, bounded JSON-RPC, literal arguments, per-call write grants, refusal, persistent staged changes and checkpoint restore routes. Full legacy command catalog remains. |
| Model providers | `go/runtime/provider_*.go`, `google_auth.go` | Google is built in: SDK and Vertex-compatible streams; Aria's own tuned Gemini endpoints (`tuned_models` short names or `projects/…/endpoints/…`, always on the Vertex SDK route in the endpoint's own region); signed calls; existing login; independent native OAuth refresh; macOS manual proxy support. Every other model is a user-declared OpenAI-compatible endpoint (`provider_declared.go`, `model_providers` as in `providers/llm/custom.py`), read from user-level files only, with credentials from an environment variable or an auth command; other provider families are deliberately not built in (see `docs/model-providers.md`). Automatic proxy discovery remains. |
| Files | `go/runtime/files.go`, `search.go`, `staged_changes.go`, `checkpoints.go` | Actual edits/read-back, ordered atomic multi-edit, CRLF/mode preservation, root isolation, persistent staged review/apply/reject, shared SQLite checkpoints, grouped conflict checks and file restoration. Python-specific regex extensions, semantic patches, whole-turn transactions and diagnostics remain. |
| Shell | `go/runtime/command_*.go` | Actual command output/exit, refusal, exact session grants, bounded capture and cancellation. Native OS sandbox/background-service parity remains; unsupported modes reject execution. |
| Sessions | `go/runtime/history.go`, `state.go` | Old JSON formats, unknown fields, supported images and tool history. Additional attachment formats and public import/export routes remain. |
| Portfolio backtest core | `go/runtime/quant/` | 48 complete oracle results plus actual executable comparisons, including gaps, ordering, fees, realized PnL and benchmarks. Market feeds, live broker risk/execution, optimizer/report routes remain. |
| Training evaluation | `go/runtime/training/compare.go` | Paired reports, fixed bootstrap sampling, confidence intervals, costs and error exclusion compared with Python. |
| SFT dataset preparation | `go/runtime/training/dataset.go` | Holdout isolation, task splits, eligibility, deduplication, provenance and Unicode trimming compared with Python; malformed inputs preserve existing files. Eval trajectories come from the native runtime (`evals.runner --runtime native`, full events under `ARIA_EVENTS_FULL`), checked end to end into SFT records. |
| Vertex tuning | `go/runtime/tuning.go` | `training tune submit/status/wait`: content-addressed dataset upload to Cloud Storage, supervised tuning job creation, a record tracing the job to the dataset's hashes, and registration of the tuned endpoint in `tuned_models`. Tested against Cloud Storage and Vertex AI stand-ins; not yet run against a real project. |
| Risk assessment and change contracts | `go/runtime/risk.go`, `contract.go` | L0–L4 levels, capabilities, blast radius and score for commands and tools; `.aria/policy.yaml` contracts with allow/restrict/forbid/max_level, fail-closed loading, and a check on every native tool call before approval. Compared field by field with `safety/risk.py` and `runtime/contract.py` through `aria-runtime risk` and `aria-runtime contract`. Go's ASCII `\b` can only rate a keyword flush against a non-ASCII letter higher, never lower. Approval by risk: the risk card in every approval, `approval_mode: risk` with `auto_approve_level` (shown on the action as auto-approved), and L4 asking even after a standing grant or `--dangerously-skip-permissions`. `/config set` accepts `approval_mode`, `auto_approve_level`, `review_gate` and `delivery_report` with the Python values. |
| Acceptance gate | `go/runtime/acceptance.go` | A turn that wrote files runs its checks when the model says it is done — `acceptance_commands`, else `.ariarc` `acceptance.default`, else the checks inferred from the changed files and project (py_compile, pytest or unittest, npm test/build, tsc) — through the `run_command` policy without a prompt. A red check goes back to the model with the trimmed failing output and `file:line` anchors, up to `acceptance_max_attempts` (default 2). Planner, result reading, report text, trimming, anchors and `.ariarc` are compared with Python through `aria-runtime acceptance`. Python reads test files' syntax trees to choose pytest or unittest; Go reads their imports and top-level test functions line by line. A red check is rerun once on the code before the turn (`go/runtime/baseline.go`): in git, a snapshot of the working tree through a scratch index checked out as a temporary worktree with the turn's edits undone; outside git, a bounded copy. Failures that were already there — by pytest test id, otherwise by exit status — are not sent back to be repaired; the comparison and report text are compared with `runtime/baseline.py`. Domain-pack commands and task-worktree base commits remain. |
| Independent review | `go/runtime/review.go` | With `review_gate` on, a coding turn the model calls done is read by the same model with a fresh context and no tools: goal, contract, checks and diff. Blocking findings go back to the builder up to `review_max_attempts` times and the review lands in the delivery report; a failed reviewer never blocks. Answer parsing, the prompt and the report text are compared with `runtime/review.py` through `aria-runtime review`. |
| Delivery report | `go/runtime/delivery.go` | Every coding turn that changed files, ran checks or had calls refused ends with the report on `turn.completed` (shown by the Rust chat, always present headless): changed files with line counts, checks, risk, contract refusals, status and next step from evidence, compared with `runtime/delivery.py` through `aria-runtime delivery`. The checks come from the acceptance gate; with `acceptance_gate: false` they are the verification commands the model ran after its last change. Definitions touched, tests that reference them and project-graph impact need `semantic_diff`, `repo_map` and `project_graph`, which remain. |
| Relay | `go/relay/` | Existing separately validated Go candidate and storage contracts. Production switch/old-entrypoint deletion are still separate deployment work. |
| Release assembly | `go/buildtools/` | Standard-library Go replacement builds and probes Rust, retains the worker, and installs the formal frontend in both PyInstaller contents layouts. All release entrypoints now use it. The old `scripts/bundle_native_indexer.py` is removed; application-worker and business modules remain. |

All source families are in scope. In particular, agents/packs, financial data
and broker adapters, document/notebook/browser tools, API/daemon services,
MCP/LSP/hooks, client isolation, task worktrees/acceptance checks and packaging
are not covered merely by the rows above. Their Python implementations remain
available and must not be removed to make a language count appear complete.

## Local evidence

The 2026-10-14 integration is based on main v0.151.0, including the Go
approval, delivery, acceptance, declared-provider, trajectory and tuning-job
increments. The integrated runtime race suite, 68 Rust workspace tests, and
230 native/packaging tests (3,144 parameter subtests) passed locally. Staged
proposals are excluded from applied-file delivery evidence. Windows CI exposed
verbatim path spelling differences between Rust's canonical workspace and
Go/legacy checkpoints; the scope now normalizes equivalent absolute drive/UNC
spellings while retaining symlink and root checks.

Gradual Python removal starts with release assembly. The Go assembler verifies
copied executables before replacing the launcher, refuses an existing worker,
and restores the original launcher if its final install fails. It is tested
with subprocess fixtures and the actual Rust release executable; CI repeats
those tests on Windows, Linux and macOS. Native packages still contain the
application worker; this build-time replacement does not remove business
features or require users to install Go.

The 2026-10-13 checkpoint/staging increment passed 148 Go cases and subcases
under the race detector (one opt-in live-login test skipped), 66 Rust workspace
tests, and 88 actual-executable integration/domain comparisons on macOS. The
Python oracle interpreter for this run was 3.12.14; CI continues to use 3.13.
The terminal test verifies that `/undo` removes the rewound content immediately,
retains an already typed draft, never calls the model, and restores terminal
settings on exit. The JSON session and native provider history are checked too.
Rust formatting/clippy, Go formatting/vet, Ruff correctness checks and
`git diff --check` passed. Final Linux amd64 and Windows amd64 binaries were
cross-compiled without CGO; runtime checks on those systems remain the CI
matrix's responsibility.

The retained Python checkpoint and staged-change implementations serve as
independent oracles in `tests/test_native_checkpoints.py`. Native writes prepare
durable preimages before the disk operation; restoration accepts legacy SQLite
records, rejects changed or redirected files, preserves native physical bytes,
and records legacy run-group restoration events. Listing and selecting the
latest checkpoint avoid loading old file bodies. Group restores have a 64 MiB
image-read budget and stop before changing files when that budget is exceeded;
individual checkpoints remain selectable.

This increment does not migrate the complete `/rewind turn` transaction,
shell side effects or task worktrees. Legacy in-memory stages and the earlier
native prototype's incomplete `changes/*.json` backups are not automatically
imported. The latter lack postimages needed to verify a safe restore. Existing
Python modules are retained for their remaining callers and as test oracles.

On 2026-10-12 the Rust/Go path completed a real configured Google greeting
without file tools, read an unpredictable marker from a temporary project,
then approved, edited and read back its file. The latest single samples took
6.10, 6.05 and 10.79 seconds respectively. A separate native OAuth refresh bypassed the CLI
access-token cache and passed without modifying its credentials database.
These observations validate those flows, not every provider/model or an
overall performance comparison with other coding agents.

The later headless/file-bridge increment passed 126 Go test cases (including
subtests; one opt-in live auth test skipped), 63 Rust tests and 111 targeted
Python-driven native/domain/legacy-stream tests on macOS. Linux amd64 and
Windows amd64 Go binaries cross-compiled without CGO. Their runtime behavior
still requires the configured CI matrix. The native model fixture verifies
SDK requests, actual disk effects and the Rust process boundary without live
cloud credentials.

A real pseudo-terminal check rendered the compact cream robot, exercised
Chinese multiline paste, long-input scrolling, resize and clearing, and restored
terminal settings after exit. Earlier source-build startup samples reached the
ready screen in 0.047 and 0.129 seconds; model latency is separate from this
first-screen measurement.

The retained Python domain code serves as the independent oracle in
`tests/test_native_runtime_domain.py`, `test_native_training_compare.py` and
`test_native_training_dataset.py`, `test_native_go_risk.py` and
`test_native_go_contract.py`. The new `.github/workflows/go-runtime.yml`
builds the executable before running those comparisons. Live model/auth
verification is opt-in and is not part of normal CI.

## Required before deleting a module

1. Its behavior and callers have an identified native replacement, including
   public CLI/API paths and saved-state compatibility.
2. Functional tests cover its real effects and outputs, refusal/cancellation
   and relevant edge cases. Imports or placeholder adapters are insufficient.
3. Packaging, installers, service entrypoints and release checks use that
   replacement and preserve the existing deployment contract.
4. Source references and obsolete tests/metadata are updated together, with
   recoverable Git history. The integrated product checks must pass before
   pushing, merging or publishing the full migration.

The robot/input/conversation changes were separately merged in UI PR #184.
Native runtime/domain increments have since merged through PR #201. This
checkpoint/staging increment is on `feat/native-checkpoints`; it does not
constitute a completed product migration. The existing Python worker
remains the installed default until the remaining feature and packaging checks
are satisfied.
