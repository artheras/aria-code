# Remaining Python migration work

## Audit of 2026-10-19 (main `dd4af04`+)

Measured, not estimated. Tracked code: Python `src` 549 modules / 151k lines
(tests 53k), Go 23k, Rust 8k. About 87% of runtime code is still Python, but
numeric libraries (numpy, pandas, scipy, sklearn, torch) appear in only 13
files; the rest is command handling, orchestration and HTTP.

**Reachability caveats.** A static import walk from the shipped entry points
finds 85 modules (18.7k lines) unreachable, but almost all are loaded another
way: 54 compatibility shims at the `aria_code/` root (`import
local_finance_tools` resolves to `tools.local_finance_tools`), command names,
`python -m` entries and Dockerfiles. Unreachable is a lead, not a verdict;
check `git grep -w NAME` (and the Dockerfiles) before deleting.

**External consumers.** The Arthera platform installs aria-code at a pinned
commit and imports `runtime`, `task_ledger`, `verify_loop`, `self_healing`,
`packages.aria_mcp.server` and `agents.video.director`. Those are public API
for now: changing them needs a pin bump on the Arthera side.

**Removed in this pass** (zero references): `demo_player.py`, `demo.tape` and
the 7.6 MB `demo-2026-06-22.gif`, `scripts/do_move.sh`, `top_level_modules.txt`,
`PROJECT_CONTEXT.md`.

**Unwired but tested; decide wire-or-delete** (no production caller, dedicated
tests): `cloud_memory.py`, `workspace_context.py`, `tools/video_tools.py`,
`runtime/crash_report.py`, `video_workflow/music/foley_service.py`,
`utils/vscode_uri.py`.

### Where each area should end up

| Area (lines) | Target | Why / state |
| --- | --- | --- |
| Feishu relay, daemon (`aria_relay_server`, `aria_feishu_bot`, `aria_daemon`, ~3.9k) | Go (`go/relay`, 1.8k exists) | Server side, no terminal coupling; best first migration |
| `packages/aria_mcp` (server 1.5k + installer) | Go | Protocol server; Arthera imports it, so keep a Python shim during cutover |
| `runtime/` (11k: agent loop, transactions, sandbox) | Go (`agent.go` exists) | Blockers: whole-turn restore, process sandbox, change impact |
| `providers/` + `apps/cli/providers` (7k, Ollama stream 1.6k) | Go | Google, OpenAI-compatible and tuned providers already native |
| File/code/notebook/web tools (~3k) | Go | Behind the same read/write policy as Go file tools |
| Slash commands (`apps/cli/commands`, 22.7k) | Go for session/config/model/workspace; finance stays Python until its data layer moves | Largest single block |
| Self-update install (`updater.py`) | Rust | `updates.rs` only checks; install needs Python and does not support Windows |
| `semantic_diff`, `repo_map`, `project_graph` | Rust (`rust/graph`) | Already partly there |
| Legacy REPL: `aria_cli.py` (5.2k), `ui/` (6.4k), `picker.py`, `terminal_chrome.py` | Retire, do not port | The native TUI replaces them once the command inventory is complete |
| `packages/quant_engine`, market/broker data, `data_cleaner`, training, evals, ADK apps | Stay Python | pandas/numpy, vendor SDKs (akshare, tushare), Python-only SDKs; keep as the numeric oracle for Go backtests |

### Service gaps found

* Go `/model` menu has no search, grouping or cloud model lists (Python's does).
* Self-update installs only native macOS/Linux builds.
* Go runtime: Linux/Windows sandboxes, embedded Office charts, whole-turn restore.
* CI: Windows Go `bridge` subprocess has a 5 s timeout that flakes under load;
  `test_doctor` depends on running inside a virtualenv; the `timeout` pytest
  mark is unregistered.

---

# Earlier baseline (2026-10-14)

Audit baseline: main `541ab4f` (v0.152.0), 2026-10-14. This checkout contains
520 Python files under `src/aria_code` and, before this increment, 12 Python
scripts. A matching Go file is not sufficient evidence that a Python module
is unused: the shipped worker and compatibility entrypoints still call it.

## Completed in this increment

The release frontend smoke test moves from `scripts/verify_native_frontend.py`
to `go/buildtools/verify.go`. macOS, Windows and Linux release entrypoints now
call the Go command. The Python script and obsolete workflow path filters are
removed. Verification retains the version, symlink, application startup,
robot, read-tool and shutdown contracts; it additionally checks the returned
file content and removes inherited runtime overrides so the test cannot
accidentally validate an unrelated worker. No production service is switched.

Three unreferenced one-time src-layout migration scripts are also removed:
`fix_imports.py`, `rewrite_imports.py`, and `migrate_to_src_layout.py`. The source
layout is already migrated; these scripts have no repository callers and the
first two rewrite source/tests as an import-time side effect. They remain
recoverable from Git history.

Archive packing/extraction and npm platform assembly now also run in
`go/buildtools` (`pack`, `unpack`, `packages`). Their two Python implementations
are removed, native workflows use the replacements, and the npm assembly job
no longer installs Python. Independent Python archive fixtures validate the
Go output, including a real execution of the workflow's assembly shell script.
All inputs are validated before existing platform packages are promoted;
malformed archives preserve old output. Six Python scripts remain.

Native session export is now exposed through `/export` in the Go worker:
JSON, CSV, Markdown, SFT and notebooks. Actual executable output is compared
with the retained Python implementation for plain conversations. JSON also
preserves attachment blocks and unknown message fields; readable formats show
attachment placeholders, and text-only SFT excludes attachment-dependent pairs.
Exports respect write roots, persistent denials, project contracts and approval,
use checkpointed writes, and leave saved conversation history unchanged.
`apps/cli/session_export.py` is retained because the installed Python worker
still calls it. Native diagnostic summaries now have a distinct schema; legacy
provider/MCP health probes and raw trace parity have not been ported. Bounded,
privacy-preserving lifecycle traces are now persisted and exported natively.

The native interaction increment adds `/model` provider/model menus, `/models`
discovery, atomic provider/model selection, Google Cloud project/region settings,
and an Ollama OpenAI-compatible shortcut. Explicit `@file:`/`@path` references
now read bounded UTF-8 files through the host's read policy and preserve snapshots
in provider history. Rust requests scoped file completion from Go rather than
reading host directories itself. This replaces part of the Python command and
reference flow. The next increment adds DOCX main-body/table text and PPTX
slide text (relationship order, bounded ZIP/XML parsing), `/export session`
including provider snapshots, and `/import` for independent validated sessions.
PDF text-layer extraction now uses optional Poppler through read and execution
approval, with bounded input/output/time and explicit blank-page markers.
Tesseract OCR handles empty-text PDF pages with English/simplified/traditional
Chinese data. macOS Seatbelt contains conversion, including restricted modes;
Linux/Windows restricted adapters remain. Opt-in PNG/JPEG/PDF page images reach
vision models and persist in history; external chart-answer accuracy and embedded
Office charts remain unverified/unsupported. `/export bundle` includes bounded
safe lifecycle logs, not the full legacy bundle. See native-document-validation.md.

## Next migrations, in dependency order

| Area | Remaining Python callers / contract | Required replacement before deletion |
| --- | --- | --- |
| Release archives and npm platform assembly | Completed in `go/buildtools/archive.go` and `packages.go`; old scripts removed. | Cross-platform release CI must still exercise real frozen artifacts. Local tests cover TAR/ZIP, links/modes, malicious archives, platform metadata and the actual assembly shell step. |
| Frozen Python module discovery | `scripts/pyinstaller_collect_args.py`, native build scripts and CLI/MCP PyInstaller entrypoints. | Keep until both the CLI worker and MCP server have complete native release replacements. Removing this first produces incomplete frozen applications. |
| Application and CLI routing | `apps/cli/app_server.py`, `main.py`, `native_entry.py`, `aria_cli.py`, command adapters. Rust release packages still launch the Python application worker. | Complete native command inventory, persistence and attachment handling, then validate the installer and worker selection across all supported platforms. Preserve a deliberate compatibility route. |
| Files and task restoration | Python file tools, `runtime/transactions.py`, workspace/task state and native parity tests. Go covers individual checkpoints and staged changes. | Whole-turn restoration, semantic patches, diagnostics and task-worktree semantics must be ported. The old SQLite/state readers must stay compatible. |
| Session attachments | Go supports text/Office/PDF references, multilingual PDF OCR, macOS sandboxed conversion, opt-in visual references, exports/import and safe lifecycle diagnostics. Python callers remain active in the installed worker. | Linux/Windows restricted adapters, embedded Office charts, other formats and legacy diagnostic health/raw-trace parity. Preserve replay and fail-before-save contracts. |
| Process execution | Python sandbox and background-process adapters; Go rejects unsupported constrained modes. | Native OS sandbox and background-service lifecycle, cancellation and restart behavior. Do not replace a denied constrained operation with an unrestricted shell. |
| Change impact | `runtime/semantic_diff.py`, `repo_map.py`, `project_graph.py` and delivery consumers. | Complete native impact analysis and wire it into delivery; Rust indexing currently replaces only part of graph construction. |
| Financial/data and training services | Data feeds, broker adapters, optimizer/report routes, Python eval/training callers. | Connect native backtest/training cores to public routes and data adapters; keep numerical parity and fixture coverage before removing oracle implementations. |
| MCP, integrations and daemon/API | `packages/aria_mcp`, channel/daemon adapters, browser/notebook/document tools and deployed Python services. | Native protocol, authentication, cancellation, persistence and deployment entrypoints for each family. Go relay candidate readiness does not establish production cutover. |

## Deletion evidence

For each removal, record the old callers, new public entrypoint, persisted-data
compatibility and observable test result. Port references and release checks in
the same change. Keep independent oracle fixtures when removing implementation
code; do not weaken parity tests merely to reduce the Python count.

The legacy local branch `feat/verified-agent-loop` also has video/platform and
workspace-export work outside this baseline. It remains preserved in the
original checkout and requires a separate feature comparison before any branch
replacement. This worktree does not install or replace the user's current CLI.

## Validation of this increment

Go build-tool tests, race checks and `go vet` pass on macOS. Windows amd64 and
Linux amd64 build-tool binaries cross-compile successfully; that is not a
runtime test on those platforms. Packaging regressions pass (51 tests plus
80 parameter subtests), along with shell syntax and whitespace checks.
The verifier is exercised with real subprocess fixtures locally. A complete
frozen release was not rebuilt during this increment; each platform's release
workflow now invokes the Go verifier on the actual frozen distribution.

The session-export increment passes the complete Go runtime suite and `go vet`,
targeted export tests under the race detector, and the actual Go executable's
five-format comparison with the Python oracle on macOS. The export test is
included in the Linux/macOS/Windows native-runtime CI matrix; those remote
jobs have not been run by this local change.

The command/reference/model-selection increment passes the full Go race suite
and vet, 70 Rust workspace tests and clippy, and 12 Python-driven actual-binary
tests covering model menus, Ollama-compatible requests, reference content,
exports, provider configuration and Rust-to-Go execution. Model endpoints are
local fixtures: no paid cloud generation, model download or production cutover
is included in this evidence. `scripts/run_native_dev.sh` builds and starts the
new implementation without replacing installed `aria`/`aria-code` commands.

The import/Office-text increment passes the full Go race suite and vet. Nine
actual-binary integration tests pass, including DOCX text reaching the local
model fixture and full-session export/import/resume. Dedicated Go tests cover
PPTX relationship order, Unicode/table text, missing/broken/oversized XML,
cancellation, preservation of file snapshots and failure without creating a
session. No Python business module is deleted in this increment because the
installed worker still uses those implementations.
