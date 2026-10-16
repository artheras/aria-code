# Native application runtime

This Go executable runs the persistent JSONL application service consumed by
the Rust terminal. It is an opt-in migration implementation. The installed
launcher still uses its existing worker until the whole feature inventory has
passed validation and the packaging switch is complete.

## Build and exercise

To build and run this checkout without changing installed command symlinks:

```sh
bash scripts/run_native_dev.sh -C /path/to/project chat
```

Run that command from the repository root. It uses the usual user state;
set `ARIA_HOME` to a separate directory for an isolated evaluation.

From this directory:

```sh
go test -race ./...
go vet ./...
go build -trimpath -o /tmp/aria-runtime .
```

From the repository root, with the Rust frontend already built:

```sh
ARIA_RUNTIME=/tmp/aria-runtime aria-native -C /path/to/project chat
ARIA_RUNTIME=/tmp/aria-runtime aria-native -C /path/to/project exec 'Read README.md'
ARIA_RUNTIME=/tmp/aria-runtime aria-native -C /path/to/project tool read_file '{"path":"README.md"}'
```

On Windows, build `aria-runtime.exe` and set `ARIA_RUNTIME` to its absolute
path. The opt-in frontend modes are `chat`/`app`, `exec`/`stream` and
`tool`/`bridge`. The full legacy `run` command catalog remains to port, so the
override should not be saved globally or enabled by the release launcher yet.

The worker waits for the host's barrier newline before reading or writing
user state. Requests and events use application protocol 1 with a 1 MiB frame
limit. The Rust host owns terminal state and process containment. File tools
use host-selected roots and `os.Root`; process containment alone does not
provide filesystem or network sandboxing.

## Implemented routes

- `/` and `/help` show command usage; Tab completes command prefixes.
  `/model` opens the interactive provider/model picker; `/models [gcp|google|ollama]`
  lists available models and `/model <provider> <model>` switches explicitly.
  Provider and model are persisted together, with no generation during selection.
  Ollama uses its OpenAI-compatible API, defaulting to `127.0.0.1:11434`;
  `ollama_base_url`, `OLLAMA_HOST` and user-declared providers support other hosts.
  Google Cloud keeps the existing authentication, project and region settings.
- `@file:path`, `@path` and `@"path with spaces"` load actual UTF-8 files through
  read permissions before generation. File snapshots remain in provider history
  across resume; subsequent turns do not silently re-read changed files. Tool
  events report reads and failures. Limits are 16 unique references, 256 KiB per
  file and 512 KiB per turn. DOCX main-body/table text and PPTX slide text are
  supported, in presentation relationship order rather than filename order.
  Office input is capped at 32 MiB, XML at 8 MiB per component / 16 MiB total,
  and extracted text at 256 KiB. PDF text layers use optional host `pdftotext`
  (Poppler), with 32 MiB input, 256 KiB text output and 2000 pages. Empty text-layer
  pages use Tesseract OCR (up to 20 pages, 2200px rendering, 20 seconds per
  subprocess / 90 seconds per PDF). OCR is labelled as fallible; completely
  unreadable pages fail explicitly. `document_ocr=false` disables OCR.
  Default `ocr_language=eng+chi_sim+chi_tra` requires all three traineddata files;
  missing data fails rather than silently dropping Chinese. `ARIA_OCR_DATA`
  selects a host-provided data directory. No runtime downloads are performed.
  Conversion additionally requires `run_command` approval/project permission.
  macOS conversion uses Seatbelt to deny network, private/project reads and
  writes outside temporary scratch. Restricted modes work through that adapter;
  other platforms still refuse restricted conversion. Converters receive no
  inherited credentials or project cwd. There is no hard OS memory quota.
  `document_vision=true` explicitly opts into sending PNG/JPEG references and
  PDF page images to the selected model. It must support vision. At most six
  images/pages and 6 MiB of encoded image data are accepted per turn; split
  larger PDFs. Images persist in provider history. Chart values are estimates
  unless backed by exact data; this does not extract embedded Office charts.
  Tab at the end of an `@` draft requests workspace-scoped directory completion;
  older workers without this capability retain their existing behavior.

- Google Gemini SDK and the existing Vertex Chat Completions route, including
  streamed function arguments, opaque tool signatures and cancelled batches.
- Both model routes retry busy/empty responses within a bounded budget, stop
  backoff on cancellation, and never replay partial output or malformed calls.
  Truncated/blocked SDK finishes cannot execute a proposed tool. Unsigned hidden
  prose is omitted; signed payloads remain intact in private provider history
  and are not rendered as user-visible answers.
- Existing Google Cloud CLI login, resolved for the active account through
  read-only SQLite. OAuth refresh is native Go and does not start `gcloud` or
  a Python process. ADC and API-key configurations remain separate routes.
- macOS manual HTTP/HTTPS proxy settings, their exceptions, and explicit
  environment proxy settings. PAC and automatic proxy discovery are not yet
  implemented.
- Existing JSON session/config formats, preserved unknown message fields,
  bounded display snapshots, resume-last and supported image attachment blocks.
  Unsupported attachment types fail explicitly without replacing saved data.
- `/export json|csv|md|sft|ipynb [path]` exports the saved conversation without
  calling a model or adding the command to history. Paths use the session's
  configured write roots, project rules and approval policy; writes create a
  durable checkpoint and new files use mode 0600. The default is JSON in the
  workspace. JSON retains attachment blocks and unknown fields; readable
  formats use attachment placeholders and text-only SFT excludes media pairs.
  The native file-tool limit is 4 MiB. The Python export module stays available
  for the existing worker.
- `/export bundle [path]` exports `aria.native_diagnostic_bundle.v1`, including
  OS/architecture/Go version, conversation counts and document-support status.
  It works for empty conversations and uses the same write approval/checkpoints.
  It never includes conversation values, config, credentials, environment or
  file paths, and does not probe the network. A rolling 512-event lifecycle trace
  records timestamps, known tool names, outcomes and turn status; it persists
  privately under the native state directory and survives restarts. Export reports
  truncation in the current process and persistence failures. Raw model reasoning,
  tool output and provider/MCP health probes are not collected; this is not full
  parity with the legacy Python diagnostic bundle.
- `/export session [path]` exports the complete native session, including
  provider history and file snapshots. `/import <path>` accepts it or a JSON
  message array, validates both histories and creates an independent session.
  Use the returned `/resume ID` to open it. Imports cannot overwrite the source
  or current session, and imported metadata cannot grant checkpoint authority.
  Import is bounded to 16 MiB and 10,000 messages; export retains the existing
  4 MiB write-tool limit. Malformed/unsupported history fails before saving.
- Read, list, recursive glob, RE2 code search, write, exact edit and atomic
  ordered multi-edit. CRLF and file modes are retained. A change to a file
  while approval is pending prevents the write.
- Local shell commands and `!command`, with actual exit codes, bounded output,
  explicit approval and cancellation. A session command grant matches the
  exact command/parameters, rather than authorizing every future shell command.
  Native remote/read-only/network-disabled shell sandbox adapters are pending;
  those modes reject shell execution rather than running unconstrained.
- Standalone social messages receive no tools. They cannot inspect files even
  if a provider attempts a call. Read results are reused only within a batch
  and invalidated by mutations/commands.
- Headless execution retains the existing single-turn JSONL event contract and
  saves its session. It never invents a confirmation or waits for terminal
  input. Only explicit grants can satisfy a default write prompt; persistent
  denials and always-ask policies retain precedence.
- The one-request JSON-RPC file bridge is limited to the selected workspace.
  Writes require the host's per-invocation named grant; notifications never
  execute. File read/list/search results are compared with the old handlers.
  Approved file writes prepare a durable checkpoint before changing disk.
  `list_checkpoints` and host-approved `restore_checkpoint` work across worker
  processes and read the existing Python SQLite checkpoint tables. Native
  records preserve physical preimages; legacy records preserve their logical
  text contract. Group restores preflight every file, reject changed files and
  paths redirected to other targets, and retain the recorded file mode. Groups
  exceeding the 64 MiB image-read budget stop before modifying files and can be
  restored by individual checkpoint ID.
- `stage_only: true` on file mutations prepares a persistent change without
  writing the file. `/changes`, `/apply-change` and `/reject-change` review it;
  the one-request bridge also exposes `list_changes`, `apply_change` and
  `reject_change`. Applying a change requires a new explicit write grant and
  checks the current preimage and original tool policy. Staging cannot silently
  become an immediate write; malformed staging flags are rejected.
- `/undo` removes the latest user turn from both display and provider history.
  The frontend refreshes its visible history while retaining the draft; earlier
  signed tool history remains intact. `/rewind list|code|both|conversation`
  restores files, conversation, or both through the current application policy;
  noninteractive restores require an explicit grant or `--yes`, which does not
  override a persistent deny or always-ask policy.

Native staging uses an additional SQLite table, leaving legacy tables available
to the old worker. The old in-memory staged changes are not imported. SQLite
and project files are not one crash transaction: a pending checkpoint stores
recovery images before mutation, while an interrupted staged apply remains
marked `applying` for manual inspection. Shell commands, task-worktree state,
approval grants and background tasks are not restored by these file commands;
the broader transaction/turn rewind is a separate migration item.

The native robot is rendered by Rust from the `cream-block-v2` design name,
with full/compact grids and four separated feet. No Python drawing module is
used by this route.

## Native domain commands

```sh
aria-runtime backtest --input bars.json
aria-runtime training compare --baseline baseline.json --candidate candidate.json
aria-runtime training dataset trajectories/ --suites evals/suites --out sft/
```

`backtest` accepts the existing bar field names in a `data` object keyed by
symbol. Its object order is retained for allocation/fill ordering. Configuration
fields include `starting_cash`, `commission`, `slippage`, `periods_per_year`,
`strategy`, `fast`, `slow` and `weight`. The engine supports custom native
strategies and preserves the next-open fill, cost-basis and benchmark rules.
The command operates on supplied bars; market-data loading and native broker
adapters are separate migration work.

Training comparison keeps unscored errors separate, paired task rates,
deterministic bootstrap draws and the exact sign test. Its numeric oracle is
the current Python 3.13 implementation, including compensated float summation.
Dataset preparation retains holdout exclusion, failed acceptance exclusion,
grader protection, per-task hash splits, deduplication, Unicode result trimming
and input provenance. Outputs are prepared in temporary files, so parsing
failures preserve previous files. Final promotion consists of individual
renames with the manifest last; it is not a multi-file crash transaction.
These commands prepare/evaluate training data and do not launch a paid tuning
job or upload user data.

## Validation and deletion gate

The Go tests use actual temporary files/processes and local HTTP services.
The native-runtime CI builds on Linux, macOS and Windows and compares the
executable against the retained Python business implementations. Python here
is a test oracle, not a dependency of the Go worker.

Local macOS evidence also exercises the actual Rust-to-Go `tool` and `exec`
process boundaries. Linux/Windows cross-compilation is not a substitute for
the matrix's runtime tests; those run when the branch is pushed.

For explicit local OAuth refresh validation:

```sh
ARIA_VERIFY_GOOGLE_LOGIN=1 go test -run '^TestGoogleLoginRefreshIntegration$' -count=1
```

This test prints no credentials and checks that the credential database is
unchanged. Ordinary CI does not run it or call paid models.

See [the product migration inventory](../../docs/native-runtime-migration.md).
No Python source is removed solely because a corresponding Go file exists.
Its callers, public command routes, output contracts and deployment entrypoints
must first use and validate the replacement.
