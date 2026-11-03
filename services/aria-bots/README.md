# aria-bots

Aria's chat bots, split out of the Python core:

| Module | What it is |
| --- | --- |
| `aria_bots.feishu_bot` | Feishu (Lark) bot: commands, chat, approval cards, attachments |
| `aria_bots.telegram_bot` | Telegram long-polling client |
| `aria_bots.relay_client` | Connects to the Go relay and hands Feishu events to the bot |
| `aria_bots.channels` | Conversation store (chat ↔ shipper binding), approvals, shipper digests |
| `aria_bots.logistics` | Freight analysis, carrier scoring, inventory policy; every tool is scoped to the shipper a chat is bound to (`logistics.tenancy`) |
| `aria_bots.logistics.mcp_server` | The same tools over MCP (`aria-logistics-mcp`) for the Go engine |
| `aria_bots.engine` | Runs a chat turn on `aria-runtime stream` |
| `aria_bots.market` | `/price`, `/brief`, `/alert`, `/alerts`, `/status` through `aria-runtime daemon` |

The package does not import `aria_code`. Until the Python core is removed,
`aria_code` keeps shims at the old paths (`aria_feishu_bot`,
`aria_code.apps.channels.conversation`, `aria_code.tools.logistics_*`, …) and
its wheel ships `aria_bots` too, so existing deployments keep working.

## How a chat turn runs

The bot runs `aria-runtime stream` (the Go engine) in `~/.aria/bots/workspace`
and reads its JSONL until `turn.completed`:

- **Engine:** `ARIA_RUNTIME`, else `aria-runtime` on `PATH`. The image carries
  one. Without an engine the bot falls back to `aria_cli.py -p` while aria-code
  still ships it.
- **Approvals:** the turn is headless, so a tool that needs approval is
  refused. `ARIA_BOT_ALLOW_TOOLS=web_fetch,...` grants named tools.
- **Logistics tools:** the bot registers `aria-logistics-mcp` through
  `ARIA_MCP_EXTRA_CONFIG` (`~/.aria/bots/mcp_servers.json`, rewritten each
  turn). The user's own `mcp_servers.json` is untouched, and an entry of the
  same name there wins.
- **Shipper scope:** a chat bound to a shipper (`/owner ACME`) runs its turn
  with `ARIA_OWNER_SCOPE=ACME`. The engine passes its environment to the MCP
  server, and `logistics.tenancy` refuses anything else. The model cannot lift
  it.
- **Files:** the MCP server reads `file_path` only inside
  `ARIA_LOGISTICS_DATA_DIR`. If that isn't set, it takes inline records only.

The model and provider are whatever the engine is configured with
(`~/.aria/config.json`, or `ARIA_HOME`).

## Run

```bash
pip install -e services/aria-bots
aria-relay-client                     # or: python -m aria_bots.relay_client
python -m aria_bots.feishu_bot 8888   # standalone webhook server
```

or, from the repository root:

```bash
docker build -f services/aria-bots/Dockerfile -t aria-bots .
```

Credentials come from the environment (or `~/.aria/.env`): `ARIA_RELAY_URL`,
`ARIA_RELAY_CLIENT_ID`/`ARIA_RELAY_CLIENT_TOKEN` (or a one-time
`ARIA_RELAY_BIND_CODE`), `FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
`FEISHU_VERIFICATION_TOKEN`/`FEISHU_ENCRYPT_KEY`, `FEISHU_ALLOWED_USER_IDS`.

## Market commands

`/price`, `/brief`, `/alert add`, `/alerts` and `/status` run `aria-runtime
daemon quote|once|alert|status`. Quotes come from financial-service. Alerts go
into the same `daemon.db` (`ARIA_DAEMON_DIR`, else `~/.aria`) that a running
`aria-runtime daemon` watches and notifies from. `/report SYMBOL` is an engine
turn that uses the financial-service tools. `/screen` is gone: it was the
Python daemon's A-share screener, which has no replacement.

## Still being moved

- The Telegram bot's command handler is still in the Python daemon.
- `/football` and `/swarm` go away.
