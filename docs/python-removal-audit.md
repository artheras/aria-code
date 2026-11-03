# Python removal audit

Before anything is deleted: is each part of `src/aria_code` (about 165,000 lines) really duplicated somewhere else, and is it still on the path an installed `aria` takes?

## What an install runs today

| Install | Frontend | Engine |
|---|---|---|
| `pip install aria-code` on Linux (glibc 2.17+, x86_64/arm64), macOS 12+ (arm64/x64) or Windows x64 | Rust `aria-native` from the platform wheel | **Go** `aria-runtime` from the same wheel; the Python worker for `--help`/`run` and `ARIA_ENGINE=python` |
| `pip install aria-code` elsewhere (pure wheel) | Python (`aria = aria_code.apps.cli.main`); the Rust TUI only if a native binary is on the machine | **Python** worker (`app_server`) |
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
| Other finance: `tools/` (finance, macro, realty), `datasources/`, `agents/financial`, `packs/`, `report_generator.py`, `dashboard_generator.py`, `data_service.py` | ~25,000 | **Nothing.** financial-service has 10 tools | **No.** Move to financial-service first, or the finance commands lose them |
| Feishu/Telegram bots, relay client, channels, logistics tools | ~4,400 | Moved to `services/aria-bots` (own package and image; old paths are shims) | Moved. Chat runs on `aria-runtime stream`, with the logistics tools as an MCP server scoped per shipper. Market commands still call the Python daemon; they move to financial-service and `aria-runtime daemon` next |
| Daemon: `aria_daemon.py` | 1,461 | Alerts and schedules: `aria-runtime daemon` | Its bot and webhook parts go with the bots |
| MCP server: `aria-code-mcp` (`packages/aria_mcp`) | ~2,300 | `aria-runtime mcp` (core, media, artifacts, transcription), financial-service, `arthera-trading` | Yes, with the worker: `aria-code-mcp` already hands over to `aria-runtime mcp` (`ARIA_MCP_ENGINE=python` keeps it until then) |
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
   - npm needs nothing more: its platform packages are built from the same native archives, so they ship `aria-runtime` too.
   - pip: done. `scripts/platform_wheels.py` turns the pure wheel and the five native archives into platform wheels carrying `aria-native` and `aria-runtime` in `aria_code/_bin/`; the Rust frontend runs the Go engine beside it. Each tag is read from the binaries (Linux: the newest glibc symbol; macOS: the declared minimum). The Linux frontend is built against glibc 2.17 with cargo-zigbuild (`ARIA_RUST_TARGET`) and the Go engine is static, so the Linux wheels are manylinux_2_17 and cover practically every distribution in use. Other platforms get the pure wheel and the Python engine.
3. **Move aria-code's unique finance code into financial-service**, and delete the copies already duplicated there. Trading needs its own decision: it either becomes a service capability with the same preview and confirmation gates, or it stays out.
   - In progress. Moved so far (financial-service #5 to #12): multi-source quotes and fundamentals, macro series and snapshots, central bank rates, technical factors, portfolio weights, Piotroski and Altman scores, options chains, funding rates, Fear & Greed, sector and index overviews, northbound flow, limit-up pool, A-share screener, A-share and crypto history, news.
   - Since then (#13 to #15): futures and treasury yields, Ichimoku and walk-forward backtests, peer comparison, and `arthera-trading` (paper). The equity report moved to Go (`aria.report.generate`).
   - Left: the analyst agents in `agents/financial/` (better as skills); realty, logistics and data-analysis tools; the dashboard generator. The Alibaba Cloud signal, prediction and cloud backtest tools depend on a retired backend and are not planned.
4. **The standalone services**, decided:
   - **Review:** the `/review` service is ported to Go (`code_review.go`, `aria-runtime code-review`); `review_service.py` goes with the Python CLI. The project-review HTTP API (`project_review_api.py`, `Dockerfile.review`) is a separate deployment that Arthera calls; it stays a Python service for now.
   - **MCP server** (`aria-code-mcp`): the core is ported as `aria-runtime mcp` (`go/runtime/mcp_server.go`): `aria.risk.assess`, `aria.contract.check`, `aria.project.impact`, `aria.project.commands`, `aria.skill.list` and `aria.skill.get`, all read-only. The Python server's other 39 tools go elsewhere:
     - finance (`aria.market.quote`, `aria.agent.team`, `aria.backtest.run`): financial-service, itself an MCP server;
     - broker (`aria.broker.*`): `arthera-trading`;
     - reports, images, video, Figma and Canva: decided, port to Go in four slices. Video is done (`go/runtime/media_video.go`: probe, scene detection, trim, concat, caption, audio, convert, speed); local Whisper transcription is not ported. Documents are done (`go/runtime/media_documents.go`: Markdown to HTML, PDF, Word, PowerPoint). Charts and the equity report are done (`go/runtime/media_charts.go`: `aria.report.chart`, `indicator_chart`, `comparison_chart`, `allocation_chart` as SVG, and `aria.report.generate` as HTML or PDF), under the Python names; price rows come from the caller or the registered financial-service, and the report no longer runs the analyst agents itself: the caller passes its analysis as Markdown. The image, Figma and video-generation clients are done (`go/runtime/media_clients.go`: OpenAI gpt-image-1 generate/edit with EXIF rotation, local generation through an AUTOMATIC1111-compatible server at `ARIA_LOCAL_IMAGE_URL` instead of in-process diffusers, Figma file tree and comments, Kling and Runway submit/status with download); keys come from the environment, and the paid tools need `confirmed=true` and are marked destructive so Aria asks a person each time. Canva is done (`go/runtime/canva.go`: `aria-runtime canva connect|status|disconnect` signs in with OAuth and PKCE, the app id and secret come from the environment and the session tokens stay in a user-only file; `aria.report.canva_upload_asset` and `aria.report.canva_design` upload and autofill), so no media tool needs Python any more;
     - `aria.artifacts.list` and `aria.video.transcribe` are ported (`go/runtime/mcp_artifacts.go`; `media_transcribe.go` runs a local whisper.cpp instead of faster-whisper); `aria.warehouse.task`, a simulation, is not carried over.
   - `aria-code-mcp` now starts `aria-runtime mcp` wherever it is installed: a platform wheel, next to the frozen native MCP build (`go/buildtools runtime`), or on `PATH`. `ARIA_MCP_ENGINE=python`, or an install without the Go engine, keeps the Python server.
   - **Trading:** a separate `arthera-trading` MCP server next to the read-only financial-service, off by default, paper trading first, every order previewed and confirmed by a person, with per-order and daily limits and a kill switch. A webhook only creates a preview; the daemon's unattended auto-execution is not carried over.
   - **Daemon:** price alerts and schedules are in the Go runtime (`aria-runtime daemon`, `go/runtime/daemon.go`): the same `daemon.db`, quotes and history from financial-service, Telegram, Feishu and desktop notifications, the same cron schedules and four built-in briefs, `install` as a macOS LaunchAgent or Linux systemd user unit. While it runs, `aria_daemon.py` leaves alerts and schedules to it and keeps its Telegram command bot, TradingView webhook queue and portfolio watchdog; those go with the bots. APNs push depended on the Arthera backend and is not carried over. Anything that places orders goes through `arthera-trading`.
   - **Feishu and Telegram bots:** stay in Python, moved out of the core into their own service that talks to the runtime over its protocol.
5. **Then delete** the worker, the Python UI and the command modules, with the parity tests that compare against them.

Steps 1 to 3 are each a PR. Deleting anything before step 2 ships breaks installs. Deleting finance code before step 3 removes features.
