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

## Go: to do, in batches

| Batch | Commands |
|---|---|
| 2b: session | /cost (needs token usage from the providers) /copy (clipboard belongs to the Rust TUI) /load-fork /thinking /ui /input |
| 4: git and review | /git /commit /pr /gh /review /verify /test /fix /bug /trace /quality |
| 5: code tools | /search /read /write /edit /file /ls /run /build /graph /impact /architecture /lsp /code |
| 6: agents | /task /tasks /worktree /workflow /wf /delegate /orchestrate /agents /team /collab /consult /deep |
| 7: extensions | /mcp /hooks /skills /packages /install /services /browser /screenshot /vision /upload-image /canvas /artifacts |
| 8: account and maintenance | /login /logout /whoami /account /apikey /providers /local /cloud /update /doctor /feedback /privacy /license /setup /completions /deploy |

## Finance service

/accuracy /alert /analyze /ashare /asset-diag /auto-strategy /backtest /broker /carriers /chart /commodity /compare /compliance /contract-draft /corr /crypto /dashboard /data /datasource /earnings /edgar /execution /exit-calc /factor-lab /factors /feargreed /football /forex /funding /hot /ichimoku /indices /inventory /journal /limitup /longterm /macro /market /markets /news /north /ops-report /optimize /optimize-port /options /orders /paper /peer /plan-report /portfolio /positions /predict /quote /realty /report /research /revenue-calc /risk /risk-scan /screen /screen-cn /shortterm /signal /sim /stat-arb /strategy /stress /ta /trade /tv /warehouse /watch

The Go runtime already has a native backtest and risk core (`backtest.go`, `risk.go`, `quant/`). /backtest and /risk keep that local path. The service adds the market data behind it.

## Retire

| Command | Replaced by |
|---|---|
| /apply | /apply-change |
| /export-pdf | /export |
| /canva | /canvas |
| /scaffold | /init |
| /ariarc | ARIA.md (/memory, /init) and /config |
