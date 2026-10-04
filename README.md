<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/aria-code-icon.png">
    <img src="docs/assets/aria-code-icon-light.png" alt="Aria Code icon" width="88">
  </picture>
</p>

<h1 align="center">Aria Code</h1>

<p align="center">An open-source AI agent for logistics and finance work.<br>
Numbers you can check, client data kept apart, actions a person approves.</p>

<p align="center"><a href="README_CN.md">简体中文</a> · English</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@artheras/aria-code"><img src="https://img.shields.io/npm/v/@artheras/aria-code?style=flat-square&logo=npm&label=npm" alt="npm version"></a>
  <a href="https://pypi.org/project/aria-code/"><img src="https://img.shields.io/pypi/v/aria-code?style=flat-square&logo=pypi&label=PyPI" alt="PyPI version"></a>
  <a href="https://github.com/artheras/aria-code/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/artheras/aria-code/ci.yml?branch=main&style=flat-square&logo=githubactions&label=CI" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-64748b?style=flat-square" alt="Apache License 2.0"></a>
</p>

<p align="center"><img src="docs/assets/demo-logistics.gif" alt="Real Aria Code terminal recording: reorder points for one shipper, a refusal to mix two shippers' data, and a carrier scorecard with a saving and two anomalies" width="860"></p>
<p align="center"><sub>Recorded from a real Aria Code session on the sample 3PL data in <code>evals/fixtures</code>. No model is called, so every number on screen comes from the tools; pauses are shortened. <a href="docs/assets/demo-logistics.png">Still screenshot</a> · <a href="scripts/record_logistics_demo.py">Recorder</a> · <a href="scripts/render_logistics_demo.py">Renderer</a>.</sub></p>

## What Aria Code does

A general-purpose assistant aims for an answer that reads well. In logistics and finance the number gets acted on — one purchase order left out of a reorder quantity and the stock is bought twice. Aria Code is built around three things that follow from that.

**Numbers you can check.** Reorder points, safety stock and carrier rankings come from stated formulas, with the formula and every assumption in the result — as does stablecoin settlement reconciliation, from the [Artheras skills](https://github.com/artheras/skills) Aria Code can install. When the data cannot support an answer — under 14 days of demand history, rows that name no shipper — it says so instead of estimating. The [`operations` eval suite](docs/verifiable-evals.md) checks exactly these traps.

**Client data kept apart.** A third-party logistics provider holds many clients' stock side by side. A chat group bound to one shipper confines every analysis to that shipper, and the logistics tools themselves refuse another shipper's data or an unattributable file — the rule holds below the prompt, whatever the model asks for.

**Actions a person approves.** In Feishu, each shipper's group gets a daily digest of reorders, dead stock and freight anomalies. `/补货` turns the reorder list into a purchase-order draft card; nothing happens until an authorised approver presses approve, and what runs is exactly what was shown. The draft is a file for your own purchasing system — Aria Code places no orders.

Underneath is a terminal coding agent: it inspects project files, applies edits, runs commands and tests, and explains the changes ([recording](docs/assets/demo-coding-workflow.gif)), with a local Ollama model or a supported cloud provider. A fresh install has no live business data; the logistics tools read your exports (CSV or JSON), and the chat features need a Feishu app or the Aria relay.

## Quick start

Install the standalone CLI on macOS or Linux without Python, Node.js, or npm:

```bash
curl -fsSL https://raw.githubusercontent.com/artheras/aria-code/main/scripts/install.sh | sh
~/.local/bin/aria
```

On Windows x64, run this in PowerShell:

```powershell
irm https://raw.githubusercontent.com/artheras/aria-code/main/scripts/install.ps1 | iex
aria
```

The installer downloads the binary for your OS from the latest [GitHub release](https://github.com/artheras/aria-code/releases/latest), checks its SHA-256 digest, and installs it in your user account. Open a new terminal to start the interactive CLI with `aria`, `aria code`, or `aria-code`. Set `ARIA_CODE_VERSION=v0.55.0` to pin a release.

Alternative package-manager installs: `npm install -g @artheras/aria-code` (requires npm) or `python3 -m pip install --upgrade "aria-code<4"` (requires Python 3.10+; the `<4` skips an older 4.x numbering that PyPI still lists). For development from source, see [CONTRIBUTING.md](CONTRIBUTING.md).

### Updates

Aria checks for a newer stable version in the background at startup, at most once per day. Cached notices appear immediately; network checks never delay the prompt. It checks GitHub Releases, or scoped npm for npm installs. Pip updates pin the GitHub version and verify that version is available on PyPI, avoiding the older 4.x numbering still listed as PyPI's latest. Source checkouts receive Git update instructions.

```bash
aria update --check  # check without installing
aria update          # install the newest stable version for this install channel
```

In an interactive session, use `/update --check` or `/update`, then restart Aria. Native updates verify SHA-256 and the downloaded binary's version before switching the command; a failed download or verification leaves the existing command in place. Startup checks only notify. To disable them, set `check_for_update_on_startup` to `false` in `~/.aria-code/config.json`.

For GitHub notifications, open [this repository](https://github.com/artheras/aria-code), select **Watch → Custom → Releases**, and save. Notifications follow your GitHub notification settings. Published stable releases appear on the [release page](https://github.com/artheras/aria-code/releases/latest) once the platform assets and package publishing have completed.

To use a local model, install [Ollama](https://ollama.com/download), pull a coding model, and start Aria in local-only mode:

```bash
ollama pull qwen2.5-coder:7b
aria-code --local
```

Or run a single task in an existing project:

```bash
aria-code -p "Inspect this project, fix the failing tests, run them, and summarize the diff."
```

Aria may request approval before editing files or running commands, according to the configured permission mode. Review changes and test results before committing them.

## Models and accounts

| Route | What it needs |
| --- | --- |
| Local Ollama | An installed and running Ollama model; no cloud account for local inference. |
| Direct cloud provider | That provider's API credentials and network access. |
| Direct Google Cloud Vertex AI | `aria-code[google]`, Google Cloud Application Default Credentials, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION`. |
| Arthera hosted service | Install `aria-code[google]` and sign in through `/login` in the CLI. Arthera account sign-in does not grant Vertex AI credentials to your own machine. |

Install extras only for features you use; see [pyproject.toml](pyproject.toml) for the current package options. Local inference can work offline, but live market data, remote integrations, sign-in, and cloud models cannot.

## Explore the project

| Topic | Start here |
| --- | --- |
| Architecture and runtime | [Architecture](docs/architecture.md) |
| Tool permissions and data boundaries | [Safety notes](docs/aria-code-safety.md) |
| Warehouse agents and their read-only data contract | [Warehouse ERP agents](docs/warehouse-erp-agents.md) |
| Strategy and backtesting workflows | [Strategy workspace](docs/strategy_workspace.md) |
| More examples | [Examples](examples/README.md) |
| Releases and changes | [Changelog](CHANGELOG.md) |

For the current command-line flags, run `aria-code --help`. For help inside the interactive CLI, run `/help`. These are preferable to a copied command inventory that can drift from the product.

## Contributing and license

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Aria Code is open source under the [Apache License, Version 2.0](LICENSE).

Releases 4.2.0 through 4.4.5 were published under the Business Source License 1.1 and remain available under those terms; releases up to and including 4.1.2 were MIT. A license grant cannot be withdrawn from someone who already has it, so each version keeps the license it shipped with — see [NOTICE](NOTICE).
