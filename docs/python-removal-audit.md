# Python removal audit

Before anything is deleted: is each part of `src/aria_code` (about 165,000 lines) really duplicated somewhere else, and is it still on the path an installed `aria` takes?

## What an install runs today

| Install | Frontend | Engine |
|---|---|---|
| `pip install aria-code` | Python (`aria = aria_code.apps.cli.main`); the Rust TUI only if a native binary is on the machine | **Python** worker (`app_server`) |
| Native release | Rust `aria-native` | **Python** worker, frozen with PyInstaller (`aria-code-worker`) |
| `scripts/run_native_dev.sh`, CI, evals | Rust | Go `aria-runtime` (only when `ARIA_RUNTIME` points to it) |

`rust/cli/src/runtime.rs` looks for `ARIA_RUNTIME`, then `ARIA_PYTHON`, then a bundled `aria-code-worker`, then `python3`. No release ships `aria-runtime`. **Every user still runs the Python engine**, so deleting the worker now would break every install, even though the Go runtime has every core command.

## Part by part

| Part | Lines | Duplicated by | Can it go? |
|---|---|---|---|
| Worker and commands: `apps/cli` (`app_server`, `aria_cli.py`, `commands/`) | ~61,000 | Go runtime (all core commands, see the [matrix](command-migration-matrix.md)) | After the Go runtime is the shipped engine |
| Terminal UI: `ui/`, Python REPL rendering | ~6,400 | Rust TUI | After pip installs get the native binary, or pure-pip is dropped |
| Agent loop, review, acceptance, checkpoints, risk: `runtime/`, `safety/` | ~13,500 | Go ports; `tests/test_native_go_*.py` compare Go with this Python | Same time as the worker; the Python then stops being the comparison oracle |
| Providers: `providers/llm` | ~3,000 | Partly. Go has Google, Ollama and declared OpenAI-compatible endpoints | **Gap.** Go has no native Anthropic API, no Arthera gateway provider and no automatic setup from `OPENAI_API_KEY`-style variables |
| `performance_stats.py`, `sec_statements.py` | 318 | **Identical** copies in financial-service | Yes, once aria-code's finance code goes to the service |
| `domain/backtest_engine.py` | 354 | financial-service (33 lines differ) and Go `backtest.go` | Same; there are three backtest engines today |
| Brokers, read side: `brokers/cn`, `brokers/intl` | part of ~4,000 | financial-service has the same 10 adapters, read-only | Same |
| Brokers, trading side: `brokers/trading.py`, `automation.py`, `planning.py` | part of ~4,000 | **Nothing.** financial-service cannot place orders | **No.** Unique; deleting it removes trading |
| Other finance: `tools/` (finance, macro, realty, logistics), `datasources/`, `agents/financial`, `packs/`, `report_generator.py`, `dashboard_generator.py`, `data_service.py` | ~25,000 | **Nothing.** financial-service has 10 tools | **No.** Move to financial-service first, or the finance commands lose them |
| Feishu bot and relay client: `aria_feishu_bot.py`, `clients/aria_relay_client.py` | ~2,500 | **Nothing** (only the relay server is Go) | **No.** Running integration |
| Daemon: `aria_daemon.py` | 1,461 | **Nothing** | **No** |
| MCP server: `aria-code-mcp` (`packages/aria_mcp`) | ~2,300 | **Nothing** (Go has an MCP client, not a server) | **No.** Published entry point |
| Review service: `review_service.py`, `Dockerfile.review` | ~500 | **Nothing** | **No.** Deployed service |
| Evals, training, video workflow | ~6,000 | **Nothing** | Keep (Python ecosystem by design) |

## Order that loses nothing

1. ~~**Close the Go provider gaps.**~~ Done (`provider_presets.go`): OpenAI, Anthropic, DeepSeek, SiliconFlow, DashScope, Moonshot, Zhipu, Groq, Together and LM Studio are built in. Each reads the usual API-key variable. The Arthera gateway uses the `/login` session. Anthropic goes through its OpenAI-compatible endpoint, not the native Messages API.
2. **Ship the Go runtime and make it the engine.**
   - ~~Native releases~~ done: `go/buildtools` builds `aria-runtime`, stamped with the product version, next to the frozen worker.
     - `runtime.rs` runs it for chat, `exec` and tool calls.
     - `--help`, `--version` and `run` stay on the Python worker.
     - `ARIA_ENGINE=python` brings the Python engine back.
     - The release smoke test fails if a session does not come from the Go engine.
   - Still to do: give pip and npm installs the native binaries. Until then, they run the Python engine.
3. **Move aria-code's unique finance code into financial-service**, and delete the copies already duplicated there. Trading needs its own decision: it either becomes a service capability with the same preview and confirmation gates, or it stays out.
4. **Decide the standalone services.** For the Feishu bot, the daemon, the MCP server and the review service: keep each as its own Python service, or port it.
5. **Then delete** the worker, the Python UI and the command modules, with the parity tests that compare against them.

Steps 1 to 3 are each a PR. Deleting anything before step 2 ships breaks installs. Deleting finance code before step 3 removes features.
