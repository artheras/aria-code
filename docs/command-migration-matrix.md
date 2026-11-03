# Command migration matrix

**Goal:** like Codex, the core runs without Python.

- **Core in Rust and Go:** the terminal UI, the runtime, tools, providers and commands. The Rust binary is `aria`; the Go runtime is `aria-native`.
- **Finance as a separate Python service:** `financial-service`, reached over MCP/HTTP, holds the finance, markets and data work, where Python's libraries are the reason to keep it.
- **No pure-Python install:** this replaces the earlier plan to keep one. The pip package becomes a client of the native core and of `financial-service`. It stops being a second implementation.

The Python worker (`src/aria_code/aria_cli.py`, `self.commands`) has 182 slash commands. Each one lands in one of three places:

| Target | Meaning |
|---|---|
| **Go** | Ported to `go/runtime` (`nativeCommands`, with an English and a Chinese `commandInfo` entry and Go tests) |
| **Finance service** | Served by `financial-service` as an MCP tool. The native core gets one generic route to it, not a port per command |
| **Retire** | Covered by another command, or for a feature that is gone. Deleted with the Python worker |

A Python command is deleted only after its replacement ships, and only together with the Python worker.

## Go: done

| Command | Since |
|---|---|
| /help /model /models /config /health /sessions /export /import /new /resume /permissions /undo /rewind /changes /apply-change /reject-change | earlier |
| /clear /status /context /tools /diff | batch 1 |
| /compact /btw /recap /regen /history /rename /fork /save /load | batch 2 |
| /init /memory /note /recall /todo /project /plan /apply-plan /checkpoint | batch 3 |
| /git /commit /pr /gh /review /test /verify /fix /bug /trace /quality | batch 4 |
| /search /read /ls /write /edit /code /run /build | batch 5 |
| /task /worktree /tasks /delegate /agents /workflow /wf /consult | batch 6 |
| /mcp /skills /vision /upload-image, plus MCP tools and $skill activation in turns | batch 7 |
| /providers /apikey /whoami /logout /doctor /update /feedback /privacy | batch 8 |
| /cost /thinking, with token usage from both providers | batch 2b |
| /graph /impact /lsp, with a native project graph | batch 5b |
| /hooks /browser /screenshot, with Pre/PostToolUse, ResponseDone and SessionStart hooks in turns | batch 7b |
| /login (browser sign-in with PKCE and a loopback hand-back) | batch 8b |
| /copy (OSC 52, in the Rust TUI) and `aria-native completions bash\|zsh\|fish` | Rust TUI |

## Still to do

Every command now has a home outside the Python worker. What remains:

1. ~~**Finance routing.**~~ Done: the commands below reach `financial-service` through the native MCP client (see below).
2. **Delete the Python implementation**: the Python worker, its terminal UI and the command modules, with the tests that exercise them. The pip package then only connects to the native core and the finance service.

## Finance service

/accuracy /alert /analyze /ashare /asset-diag /auto-strategy /backtest /broker /carriers /chart /commodity /compare /compliance /contract-draft /corr /crypto /dashboard /data /datasource /earnings /edgar /execution /exit-calc /factor-lab /factors /feargreed /football /forex /funding /hot /ichimoku /indices /inventory /journal /limitup /longterm /macro /market /markets /news /north /ops-report /optimize /optimize-port /options /orders /paper /peer /plan-report /portfolio /positions /predict /quote /realty /report /research /revenue-calc /risk /risk-scan /screen /screen-cn /shortterm /signal /sim /stat-arb /strategy /stress /ta /team /deep /ui /canvas /artifacts /account /cloud /deploy /trade /tv /warehouse /watch

These are routed in `go/runtime/finance.go`:

- `/finance` shows whether the service is connected; `/finance setup <absolute path>` registers its console script (or a venv Python, run with `-I -m arthera_finance.server`) in `mcp_servers.json` as `arthera-finance`.
- `/broker [connections | account|positions|orders <connection>]`, and `/account`, `/positions`, `/orders`, call the service's read-only broker tools directly.
- Every other command in the list becomes a read-only turn: the model answers it with the service's tools (all `readOnlyHint`), cites their sources and dates, keeps missing values unknown, and says so when the service cannot answer (live quotes, news, order placement and A-share screens are not among its tools). Nothing is written and no command runs during such a turn.
- Without the service, these commands say how to connect it instead of reaching the model.

The service's tools are statements, SEC facts, risk metrics, backtests, market history and read-only broker data. Commands that need more (news, live quotes, A-share data, trading) get an honest "not available" until the service grows the tool.

## Retire

| Command | Replaced by |
|---|---|
| /apply | /apply-change |
| /export-pdf | /export |
| /canva | /canvas |
| /scaffold | /init |
| /ariarc | ARIA.md (/memory, /init) and /config |
| /file | @file references in any prompt |
| /architecture | Internal layer contract of the Python package; not a user feature |
| /collab | /consult <provider>/<model> |
| /orchestrate | /plan, then /apply-plan or /task |
| /packages /services | Python package internals; not a user feature |
| /install | pip dependencies of the Python worker; the native install has none |
| /local | /model ollama <model> |
| /setup | /model, /config and /init |
| /license | The Python licensing module; the native runtime has no entitlements |
| /load-fork | /resume (a fork is a saved session) |
| /input | The Rust TUI's composer; /config set ui_theme=… for the theme |
