# Incremental Rust CLI migration

The first stage adds an **opt-in `aria-native` prototype**, not a replacement
for `aria`, `aria code`, or `aria-code`. The Rust crate has its own experimental
version `0.2.0`; `aria-native run -- --version` reports the Python product version.
The Go relay prototype is opt-in; production still deploys the Python relay.

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
them when a binary is found (`ARIA_NATIVE_BINARY`, or `aria-native` on `PATH`),
at least 32 files need parsing, and `ARIA_NATIVE_GRAPH` is not `off`. Anything
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
`--version` by about 0.3 ms. If the download size matters when native builds
ship, the indexer can move to its own executable without changing the wire
contract.

## Rust and Go

Rust owns the CLI and local, per-repository work: entry points, tool bridging,
indexing, stored-state commands and later the TUI. The opt-in Go relay under
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
Next, migrate TUI rendering and event streaming. Make each stage opt-in until provider calls, coding acceptance,
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
