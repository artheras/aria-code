# aria-bots

Aria's chat bots, split out of the Python core:

| Module | What it is |
| --- | --- |
| `aria_bots.feishu_bot` | Feishu (Lark) bot: commands, chat, approval cards, attachments |
| `aria_bots.telegram_bot` | Telegram long-polling client |
| `aria_bots.relay_client` | Connects to the Go relay and hands Feishu events to the bot |
| `aria_bots.channels` | Conversation store (chat ↔ shipper binding), approvals, shipper digests |
| `aria_bots.logistics` | Freight analysis, carrier scoring, inventory policy; every tool is scoped to the shipper a chat is bound to (`logistics.tenancy`) |

The package does not import `aria_code`. Until the Python core is removed,
`aria_code` keeps shims at the old paths (`aria_feishu_bot`,
`aria_code.apps.channels.conversation`, `aria_code.tools.logistics_*`, …) and
its wheel ships `aria_bots` too, so existing deployments keep working.

## Run

```bash
pip install -e services/aria-bots
aria-relay-client                 # or: python -m aria_bots.relay_client
python -m aria_bots.feishu_bot 8888   # standalone webhook server
```

or `docker build -t aria-bots services/aria-bots`.

Credentials come from the environment (or `~/.aria/.env`): `ARIA_RELAY_URL`,
`ARIA_RELAY_CLIENT_ID`/`ARIA_RELAY_CLIENT_TOKEN` (or a one-time
`ARIA_RELAY_BIND_CODE`), `FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
`FEISHU_VERIFICATION_TOKEN`/`FEISHU_ENCRYPT_KEY`, `FEISHU_ALLOWED_USER_IDS`.

## Still being moved

- Chat answers still run `aria_cli.py -p` when aria-code is installed beside
  it; they move to the Go engine (`aria-runtime`).
- Market commands (`price`, `report`, `brief`, `screen`, `alert`) still call
  the Python daemon; they move to financial-service and `aria-runtime daemon`.
- The logistics tools will also be served over MCP, so the Go engine can use
  them with the same per-shipper scope.
