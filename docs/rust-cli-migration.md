# Incremental Rust CLI migration

The current stage makes Rust the default interactive frontend in native/npm
packages; `aria`, `aria code`, and `aria-code` keep their existing names and
robot design. See **Stage 5** below for installation and fallback behavior.
Python remains the model/tool application service. The Go relay has a separate
Cloud Run candidate deployment; production still deploys the Python relay.

Stages 1–4 below describe the earlier opt-in migration. The Rust crate has its
own version (`0.5.0` currently); `aria-native run -- --version` reports the Aria
product version.

## Build and try it

Install Rust using [the official installer](https://rust-lang.org/tools/install/).
Install Aria Code into a Python environment with the normal project dependencies.
The source build pins its Rust toolchain and commits `Cargo.lock`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cd rust
cargo build --release --locked
cd ..
export ARIA_PYTHON="$PWD/.venv/bin/python"
export PYTHONPATH="$PWD/src"
rust/target/release/aria-native --help
rust/target/release/aria-native run -- --version
rust/target/release/aria-native run
rust/target/release/aria-native -C ./my-project tool read_file '{"path":"README.md"}'
rust/target/release/aria-native -C ./my-project tool --approve-write edit_file \
  '{"path":"app.py","old_string":"old","new_string":"new"}'
```

On Windows use `.venv\Scripts\python.exe`, set `ARIA_PYTHON` and `PYTHONPATH`
in PowerShell, and run `rust\target\release\aria-native.exe`. JSON quoting follows
the shell's rules. The prototype does not require Rust on a machine running a
prebuilt executable, but its chat and tool modes still require Python Aria.
Downloadable prototype builds and measurements are CI artifacts of the
**Rust CLI prototype** workflow; they are not stable npm/native release assets.

## What Rust owns in this stage

- `--help` and `--version` execute without importing or finding Python.
- `run` forwards argument vectors, the working directory and terminal streams
  to the existing console entry point. On Unix it replaces itself with Python,
  preserving signal and exit status behavior. Existing model/cloud settings,
  approval UI, TUI and update flow continue to belong to Python.
- `tool` starts a Python worker, sends one typed JSON-RPC request, validates the
  matching response, and returns exit `0` for success, `1` for a tool denial or
  failure, `2` for invocation/protocol/worker errors, and `130` for cancellation.
  Requests and responses are capped at 1 MiB. `--timeout-ms` defaults to 30 seconds
  and is capped at 300 seconds. Timeout/cancellation cleans up the process group
  on Unix and the assigned Job Object on Windows, including child processes.

## File access and approval

The initial bridge exposes `read_file`, `list_files`, `search_code`, `write_file`
and `edit_file`. It reuses `ToolExecutor`, `WorkspaceSecurity`, persistent tool
policy, and the existing CLI handlers. Approved writes retain the current change
store and verification behavior; there is no second write implementation.

The host selects one workspace with `-C`. Reads and writes are restricted to that
directory, including resolved symlink targets. Requests cannot set internal
workspace/approval flags. Writes require `--approve-write` for that invocation;
the flag is host authorization, so an agent must not invent it for itself.
Persistent denial still wins. Ask-always reads require the interactive Python CLI.
There are no arbitrary commands, extra roots, full-access mode, or cloud execution
methods in this initial bridge. `run` retains the full stable CLI's own policy.

These are application-level file checks and process cleanup, **not an OS sandbox**
for arbitrary Python code. The host must trust the selected interpreter and
installed Python package. A timeout may interrupt a write after it has applied;
it is not a transactional rollback guarantee.

## Wire contract

Internal transport is one UTF-8 JSON object followed by a newline, one request
per worker process. The method is `tools.call`, with `params.name` and
`params.arguments`. A response has the same string/integer/null ID, `jsonrpc:2.0`,
and exactly one of `result` or `error`. Tool results retain the existing
`success/data/error` structure. Diagnostics go to stderr. Batch requests are
unsupported; notifications have no response and perform no file operations.
Standard JSON-RPC errors are used for parse, request, method and parameter errors.

```json
{"jsonrpc":"2.0","id":1,"method":"tools.call","params":{"name":"read_file","arguments":{"path":"README.md"}}}
```

## Native project-graph indexing

Building the project graph (`runtime/project_graph.py`) parsed every Python file
twice with `ast`: once in `repo_map` for its definitions, once for its imports.
The `aria-graph` crate does both in parallel with the same rules, behind two
commands that take one JSON request on stdin and write one response on stdout,
reading files themselves without Python:

- `aria-native index imports`: `root`, every `[path, language]`, the paths to
  `parse` → `imports` by path (module index, nearest-candidate pick, relative
  levels, imports at any depth, JS/TS relative specifiers).
- `aria-native index symbols`: `root`, the Python paths to `parse` → `symbols`
  by path as `[name, kind, line, parent]` (top-level functions and classes,
  functions directly in a class body, upper-case module constants; the line
  of `def`/`class`, not of a decorator).

Both answer `fallback` for files they leave to Python: a file the Rust parser
rejects, or (for symbols) one it cannot read. `runtime/native_index.py` uses
them when a binary is found (`ARIA_NATIVE_BINARY`; in a native build, the copy
bundled with it; else `aria-native` on `PATH`), at least 32 files need parsing, and `ARIA_NATIVE_GRAPH` is not `off`. Anything
else (no binary, a non-zero exit, a timeout, a malformed or incomplete answer)
falls back to the Python implementation for the whole batch. The two must
agree: `tests/test_native_index.py` compares them on this repository and on
edge cases (relative levels, imports nested in functions/`try`/`match`,
ambiguous module names, decorators, CRLF, byte-order marks, syntax errors, JS
`index` files), and `repo_map` keeps its walk order.

On this repository (979 files, 1973 import edges, no fallbacks) the graph built
either way is identical; `repo_map` went from 1.6 s to 0.54 s and a full graph
build from 3.7 s to 0.83 s. What remains is the file walk, regex symbols for
other languages and the identifier scan for references, all in Python. The
parser's grammar and Unicode tables grow the binary from 0.6 MB to 5.5 MB and
`--version` by about 0.3 ms.

Native releases ship it: `scripts/bundle_native_indexer.py` builds
`aria-native` on each release runner and copies it into the PyInstaller build's
contents folder (`_internal/`, `sys._MEIPASS` at run time), runs it, and fails
the build if it does not answer. On macOS it is signed and notarized with the
other Mach-O files there. The npm platform packages are made from the same
archives, so they carry it too; pip installs do not, and keep the Python
implementation unless `aria-native` is on `PATH`. The Rust CI runs the same
script on Linux, macOS and Windows for every pull request. If the download
size matters, the indexer can move to its own executable without changing the
wire contract.

## Rust and Go

Rust owns the CLI and local, per-repository work: entry points, tool bridging,
indexing, stored-state commands and the opt-in TUI. The opt-in Go relay under
`go/relay` implements the existing HTTP/WebSocket and storage contract, with
race tests and shared Python/Go contract tests in its own CI workflow. Production
still deploys the Python relay on Google Cloud. See
[the Go relay guide](../go/relay/README.md) for staging checks before a traffic switch.

The combined Rust entry point includes both project-graph indexing and the
native state/update commands below. CI exercises both in one executable.

## Verification and next stages

`cargo test`, Clippy and real executable tests run on Linux, macOS and Windows.
They exercise forwarding, UTF-8, approved writes, persistent denials, forged
permissions, path traversal, symlinks, malformed responses, size limits, timeout
and process cleanup. The benchmark records binary size and first/warm entry-point
times against direct and delegated Python entry points under the same environment:

```sh
python scripts/benchmark_rust_cli.py --binary rust/target/release/aria-native \
  --output rust/target/benchmark.json
```

This measures entry-point overhead only. Delegated chat still pays Python startup;
it does not establish that the full TUI or a cloud model responds faster.

The second stage adds native state inspection and update metadata checks (below).
The third stage adds single-turn terminal rendering and event streaming (below).
The fourth stage adds a persistent interactive TUI and approval prompts (below).
Keep each stage opt-in until provider calls, coding acceptance,
file changes, approval prompts, cancellation, resume and all supported installers
pass parity checks. Keep financial/data/document tools in Python and keep the
Google Cloud backend unless measurements justify a separate backend change.

## Stage 2: native configuration, sessions and update checks

These commands execute without locating Python or loading a provider:

```sh
aria-native config paths
aria-native config show
aria-native sessions list --limit 20
aria-native sessions search '中文项目' --limit 10
aria-native sessions show abc123
aria-native update check --current 0.126.0 --channel native
aria-native update check --current 0.126.0 --channel npm --offline
```

Use **your installed `aria-code --version`** for `--current`, rather than the
experimental Rust crate's version. The checker reports updates to the stable
Python product; there is no Rust stable release channel yet. `--channel` defaults
to `ARIA_CODE_INSTALL_CHANNEL` or `native`; it cannot infer the delegated Python
environment's installer. All native inspection results are one JSON object on
stdout. Errors go to stderr; argument/state errors exit `2`.

Configuration paths follow Python's existing `ARIA_HOME > ~/.arthera if present
> ~/.aria-code` precedence and support `~`/`~/...`. A named-user `~someone` override
must be written as an absolute path. `ARIA_USER_OUTPUT_ROOT` remains supported.
Inspection creates no directories and does not move data. `config show` exposes
only a positive list of non-secret stored preferences, never credentials or
provider/hook objects. It explicitly reports `effective:false`: Python still owns
defaults, retired-model migration, project `.ariarc` overlays, policy and writes.

Session commands read the existing JSON snapshots and JSONL append logs without
rewriting either. A saved `.json` snapshot wins when both formats have the same ID,
matching the stable Python resume path. JSONL-only sessions are viewable and
searchable; a partial JSONL line is skipped while complete records survive.
Listings skip malformed/oversized records and report their count. Session IDs are
1–128 ASCII letters/digits/underscores/hyphens; symlinked history files are refused.
Files are bounded at 8 MiB, directory scans at 10,000 entries and result limits at
1,000. `truncated:true` indicates the directory scan cap. Search ranks by matching
message blocks, then recency, with a UTF-8 preview around the first match.

```sh
aria-native --python /path/to/python -C ./project resume abc123 -- --model google/gemini-3.5-flash
```

Resume validates the saved JSON snapshot and then delegates to the existing Python
`--session` entry point, preserving terminal streams, arguments and exit status.
Delegated stdin/stdout uses UTF-8 on every platform, including Windows pipes.
A relative `ARIA_HOME` is made absolute before changing the workspace. JSONL-only
history is not silently converted or substituted for a saved snapshot. Forwarded
arguments cannot override the chosen session with another `--session`/`--resume`.
Native state readers do not provide an OS sandbox and must read trusted user state.

Update checks use HTTPS with the operating system's certificate verifier and only
the official scoped npm metadata or `artheras/aria-code` GitHub release endpoint.
Redirects, drafts, prereleases, foreign package metadata and non-stable versions
are refused. Pip checks also verify that the exact release exists on PyPI. The
historical 4.x releases are compared with the same migration rules as Python, so
0.x users are not advised to install old 4.4.2 code.

There is a four-second total network budget, no retries and a 2 MiB metadata limit.
Validated results are cached per installation channel for 24 hours in
`ARIA_HOME/native_update_check-CHANNEL.json`, using a private temporary file and
atomic replacement. Python's update cache, configuration and installed commands
are untouched. `--refresh` forces a check; `--offline` reads only the cache. Unknown
and stale results are identified explicitly. Failed online checks exit `1` and may
report a stale cached result; offline inspection exits `0`. A cache write failure
is reported without discarding valid fetched metadata.

This checker never downloads an artifact, executes an installer or replaces the
running binary. Python's existing verified updater and rollback remain the
installation owner. Help, version, state inspection and ordinary `run` startup
never make a network request. The next TUI stage can schedule explicit background
metadata checks without adding network waits to startup.

CI tests compare native path/snapshot results with the actual Python stores,
exercise a real executable with a missing Python interpreter, and cover Chinese
content, text blocks, corrupt/partial records, traversal, symlinks, cache TTL and
channel separation, provider credential omission and resume forwarding. Rust
tests also exercise an actual HTTP reader's body limit and timeout. The benchmark
now compares native/Python listing against 100 stored sessions (20 returned).

## Stage 3: streamed single-turn terminal output

The experimental Rust entry point now renders a single execution turn:

```sh
# Uses your existing Python environment and model/cloud configuration.
aria-native --python /path/to/python -C ./project exec 'Explain this project'
aria-native --python /path/to/python -C ./project exec 'Fix the failing test' \
  -- --allow-tools read_file,edit_file,run_command
aria-native --python /path/to/python -C ./project exec --jsonl 'Explain this project' \
  > turn.jsonl
# Replay a recorded turn without importing Python, calling a model or running tools.
aria-native render < turn.jsonl
aria-native render --jsonl < turn.jsonl
```

Only the operator may supply `--allow-tools` or `--dangerously-skip-permissions`.
Rust never adds either. Python's existing headless approval callback denies
ungranted operations; persistent policy, allowed roots, tool execution and the
acceptance gate still belong to Python. `-C` selects the workspace; relative
`ARIA_HOME` is resolved against the caller before changing directory. Options
forwarded after the prompt are limited to `--model`, `--url`, `--thinking`,
`--local`, `--allow-tools`, `--dangerously-skip-permissions`, `--add-dir` and
`--read-dir`. Values are passed as literal argument vectors, never through a
shell. Use `run` for other Python CLI options, session resume and the full REPL.

Answer tokens are flushed to stdout as they arrive. Activity, status and failures
go to stderr. Code fences, indentation, line breaks and Unicode remain plain
text; this is not yet a full-screen Markdown/TUI renderer. Terminal controls
(including ESC, carriage return, C1 and bidi controls) are filtered from human
output. Machine JSONL retains the original payload and extra fields. Runtime
stderr remains the existing Python diagnostic stream. The final response is
reconciled against all streamed tokens to avoid printing it twice; when a repair
replaces earlier prose, the authoritative response gets a `final response` label.

The execution stream reuses `aria-code -p --format jsonl` with two additive event
types. `ARIA_EVENTS_STREAM=1` enables them; legacy JSONL consumers get their old
sequence by default. Visible provider text produces `answer.delta`; runtime
progress produces `turn.status`. Provider reasoning is not forwarded. The native
host enables streaming and sets `ARIA_EVENTS_FULL=0` to retain the existing
redacted activity view.

```jsonl
{"type":"turn.started","prompt":"Explain this project","model":"google/gemini-3.5-flash"}
{"type":"answer.delta","text":"I will inspect the project.\n"}
{"type":"tool.started","tool":"read_file","params":{"path":"README.md"}}
{"type":"tool.completed","tool":"read_file","success":true}
{"type":"turn.status","state":"acceptance_passed","message":"Checks passed"}
{"type":"answer.delta","text":"Here is the result.\n"}
{"type":"turn.completed","success":true,"response":"I will inspect the project.\nHere is the result.\n"}
```

The consumer requires one `turn.started`, followed by known events and exactly
one `turn.completed`. Invalid UTF-8/JSON, stdout pollution, unknown types,
inconsistent success/failed acceptance, records after completion and missing
completion fail explicitly. Each record and accumulated answer are capped at
1 MiB; a stream is capped at 16 MiB or 50,000 events. Reads and the queued event
buffer are bounded. Output is flushed per event, without buffering the whole
turn until the child exits.

Successful completion exits `0`; a failed turn exits `1`; invocation, protocol
and timeout errors exit `2`; Ctrl+C in live execution exits `130`. A nonzero
Python exit after a valid completion is preserved. `--timeout-ms` defaults to
300 seconds for `exec` (30 seconds for `tool`) and accepts 1–300,000 ms. The
supervisor remains responsive even if stdout is a blocked pipe. Timeout,
cancellation, malformed events and broken output terminate the Python process
group on Unix or Job Object on Windows. An acknowledgement before runtime
import prevents execution before Windows Job assignment. A cancelled write is
not rolled back automatically.

Tests exercise the compiled binary, real Python CLI and real agent loop/file
tool (only inference is stubbed), granting and denying an actual local write,
streaming before completion, Chinese Windows pipes, literal arguments,
truncated/corrupt streams, nonzero child exits, Ctrl+C, blocked output and child
cleanup. The existing native bridge, state/index parity and headless approval
regressions run alongside them on Linux, macOS and Windows; Windows CI skips
signal injection because no interactive console is available.

This stage does not replace `aria`, `aria code` or `aria-code`, remove Python
startup, accelerate cloud inference, or switch Google Cloud production to Go.
Stage 4 adds the persistent interactive frontend below. A default UI switch
remains a separate installer/provider parity milestone.


## Stage 4: persistent Rust interactive frontend

The full-screen frontend uses Ratatui/Crossterm and one persistent Python
application worker. Rust owns keyboard/paste input, Unicode editing, scrolling,
transcript rendering, status and approval/input dialogs. The existing
`ArtheraTerminal.send_message` owns inference, conversation context, project/file
references, command dispatch, tool policy, task isolation, transactions, checks
and delivery. It is the full interactive execution path, not a second agent loop.

```sh
aria-native --python /path/to/python -C ./project chat
aria-native --python /path/to/python -C ./project chat --resume abc123
aria-native --python /path/to/python -C ./project chat -- --model google/gemini-3.5-flash
```

Model/backend settings come from the existing user/project configuration; opening
the frontend does not switch providers, probe every service or check for updates.
The header shows both the installed Python product version and the separate Rust
UI prototype version. Configured MCP connections start in the background.
Sessions save through the existing local stores; JSON snapshots use atomic
replacement so interrupted writes retain the previous complete history.
Starting or restoring another session resets temporary tool grants, plans,
loaded-file/project state and turn telemetry. Persistent policy remains intact.

| Input | Action |
| --- | --- |
| Enter | Submit a prompt or dialog response |
| Alt+Enter / Shift+Enter / Ctrl+J | Insert a newline (terminal-dependent) |
| Arrow keys | Edit text; Up/Down browse input history at its boundaries |
| Tab | Complete/list slash commands |
| Ctrl+U / Ctrl+K / Ctrl+W | Delete to line start/end or previous word |
| PgUp / PgDn / mouse wheel | Scroll transcript |
| Ctrl+O | Show/hide tool parameters |
| F1 | Show keyboard help |
| Esc / Ctrl+C | Cancel the active task; decline a selection dialog |
| Ctrl+D with empty input | Save and exit |
| `/new` / `/resume ID` | Switch to a new/saved session |

Existing slash commands (including `/model`, `/health`, `/sessions`, `/file`,
`/project` and review/delivery commands), `@file:path` and `!command` use the
Python runtime. Menus supplied by that runtime become explicit frontend choices;
Rust answers with the request ID and turn ID. The original once/session/deny
semantics and risk policy run in Python. Stale responses never grant access.
Tool approvals keep the redacted file/command/directory context visible while
scrolling the choices. Long workspace paths retain the project name without
hiding the permission mode or network status in the header.
Secret text is masked and excluded from input history and the native transcript;
model reasoning callbacks are not emitted as visible answers.

Terminal input is capped at 64 KiB UTF-8, each protocol line at 1 MiB, queues at
32 records, a displayed entry at 256 KiB and the transcript at 4 MiB/2,000 entries.
Large display entries are truncated without truncating the saved conversation.
Editing/deletion use Unicode grapheme boundaries and layout uses display columns.
Streaming answers are reconciled with the authoritative final response to avoid
showing duplicated text after a tool round trip. Code fences/headings have basic
styling; this is not a full browser Markdown renderer.

`--timeout-ms` defaults to 300 seconds during startup or a running turn, and pauses
while idle or awaiting your response. Cancel/shutdown has a two-second grace
period. A normal model cancellation retains the session and accepts another turn.
A stuck tool/worker causes the frontend to exit with an error and stop its worker;
it does not pretend that a cancelled thread has finished. Terminal raw mode,
paste/mouse capture and the alternate screen are restored on error and exit.
Windows uses the assigned Job Object; Unix kills the worker group and attempts
to stop separately grouped descendants while the worker is still alive. Normal
shutdown stops Aria's tracked background processes and MCP connections. These
process cleanup measures are not an OS sandbox or a guarantee against deliberately
detaching/reparenting an untrusted program. An interrupted write is not rolled back.

### Bidirectional application protocol

For embedding or protocol diagnostics, use `aria-native chat --jsonl` with pipes,
or `python -u -m aria_code.apps.cli.app_server`. Every UTF-8 JSONL object carries
`protocol:1`. `chat` without `--jsonl` requires a terminal.

Frontend requests:

```json
{"protocol":1,"type":"turn.submit","turn_id":"turn-1","text":"Inspect this project"}
{"protocol":1,"type":"dialog.respond","turn_id":"turn-1","request_id":"REQUEST_ID","choice":0}
{"protocol":1,"type":"dialog.respond","turn_id":"turn-1","request_id":"REQUEST_ID","text":"My feedback"}
{"protocol":1,"type":"turn.cancel","turn_id":"turn-1"}
{"protocol":1,"type":"session.resume","session_id":"abc123"}
{"protocol":1,"type":"session.new"}
{"protocol":1,"type":"shutdown"}
```

Worker events: `session.ready/state`, `turn.started/completed`, `answer.delta/replace`,
`tool.started/completed`, `turn.status`, `output.delta`, `approval.requested`,
`input.requested`, `dialog.closed`, `protocol.error` and `session.failed/closed`.
Turn/dialog events identify their current turn; a dialog additionally identifies
its request. A ready snapshot contains the newest 40 messages for display while
the worker retains the full model context. Legacy command output is captured into
output events, keeping stdout valid JSONL. In machine mode a nonzero worker exit
is preserved, and malformed/blocked output fails explicitly with process cleanup.

CI tests the actual compiled executable with the real Python agent loop, replacing
only inference and blocking external model calls. Coverage includes multi-turn
history, restart/resume, approved/denied local writes, refusal feedback, stale
responses, cancellation followed by another turn, atomic-save failure, malformed
workers, nonzero exits and separately grouped child cleanup. Rust reducer/editor
and TestBackend tests cover Unicode, secrets and resize; Unix PTY tests exercise
paste, approvals, cancellation, resize and terminal-mode restoration. Windows
runs the protocol and TestBackend tests; CI has no interactive Windows console.

The stable commands and release installers still open the existing Python UI.
Try this interface explicitly with `aria-native chat`; publishing this stage does
not silently switch existing users to the preview. Provider response quality,
UI startup performance and installer cutover require their own measurements.


## Stage 5: default native frontend (Rust UI 0.5)

Native archives and npm platform packages now put the Rust executable at
`aria-code-bin` and the bundled Python application at `aria-code-worker` beside
`_internal/`. The installer/command names and archive layout stay the same.
`aria`, `aria-code`, and `aria code` open the Rust interface in a terminal.
The product version is compiled from `_version.py`, independently of the Rust
crate version. The worker is discovered relative to the executable, so these
packages do not need a system Python installation.

The default route supports existing interactive model, thinking, permission,
workspace, banner and resume flags. `-p`, pipe input, direct data/review commands,
`--help`, `health`, and `update` retain the Python command/output contract.
`ARIA_FRONTEND=python aria` selects the original interface for rollback.
Pure Python wheels use the Rust frontend when an entry-protocol-1 `aria-native`
is available via `ARIA_NATIVE_BINARY`/PATH; otherwise they retain the Python UI.
The separate `aria-native` command still exposes index/stream/inspection tools.

The mascot design is unchanged: snapshots send the exact spans returned by
`ui.robot.get_robot_row`, and Ratatui applies their foreground and background
colours without replacing glyphs or drawing another mascot. Robot-off and
banner-off preferences still hide it. Tests lock the original PNG checksum,
check both palettes and verify the rendered eyes in a real PTY.

Each native release runs `scripts/verify_native_frontend.py` against the frozen
application before archiving. It checks product-version parity, the application
handshake, canonical robot, real local file reads and shutdown. macOS signs the
Python worker as well as the frontend and its libraries. Source tests also run
paste, approvals, denial follow-up, cancellation, resize and terminal restoration
through the prototype, formal default route and installed-console bootstrap.

Go relay rollout now has `cloudbuild.go.yaml`: it builds `go/relay`, deploys a
`go-candidate` revision with `--no-traffic`, preserves the existing service's
environment/identity and probes `/status` on its production port 8080. Production
traffic remains unchanged until the candidate's Firestore and client contracts
are verified. For local containers use the compose override documented in the
Go relay README; its built-in health probe works without curl.
