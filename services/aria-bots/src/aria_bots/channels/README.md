# Channel Apps

Target role: adapters for non-terminal entrypoints.

Examples:

- relay server/client;
- Feishu bot;
- Telegram bot;
- webhooks;
- future desktop or browser UI.

Each channel should translate inbound messages into gateway requests and render
gateway responses back to the channel. It should not bypass safety, runtime, or
artifact policies.

## The conversation layer (`conversation.py`)

Everything a chat bot decides that does not depend on the platform lives in
one module, so every channel — Feishu today, Arthera's own bot later — gets
the same rules from the same code:

| Rule | Where |
|------|-------|
| In a group, answer only when addressed; in a direct chat, always | `should_respond` |
| One group = one shipper (货主), bound by an admin with `/owner <id>` | `handle_owner_command`, `ConversationStore` |
| Admins are per channel: `ARIA_CHANNEL_ADMINS=feishu:ou_x,telegram:42` | `is_admin` |
| Context is the conversation's own recent messages, never another's; capped at 20 messages and 7 days | `ConversationStore.history` |
| A bound conversation runs every turn with `ARIA_OWNER_SCOPE`, enforced by the logistics tools themselves | `ChannelTurn.env`, `aria_bots/logistics/tenancy.py` |

A new channel is an adapter: turn the platform's event into an
`InboundMessage`, then

```python
if not should_respond(msg):
    return
reply = handle_owner_command(msg, store)          # /owner commands
if reply is None:
    turn = prepare_turn(msg, store)               # context + shipper scope
    reply = await run_model(turn.prompt, history=turn.history, env=turn.env)
    store.record(msg.key, "assistant", reply)
send(reply)
```

`aria_feishu_bot.feishu_inbound` is the Feishu version of that first step.
Conversation keys are `<channel>:<conversation id>`, so the same chat id on
two platforms is two conversations.

## The shipper digest (`digest.py`)

The first thing the conversation layer enables that a reply-only bot cannot:
each group bound to a shipper is sent, unprompted, what needs attention —
reorders, SKUs with too little history to plan, dead stock, waybills to query,
and safe carrier switches. `run_digests(store, senders)` walks the bindings and
hands each digest to that channel's sender, so a new channel registers a
`(conversation_id, title, body) -> bool` function and gets the digest too.

Data comes from the operator's `ARIA_SHIPPER_FEEDS` file, never from chat, and
is read through the logistics tools under `ARIA_OWNER_SCOPE`, so a shared
multi-shipper export yields only that shipper's lines. Configuration problems
go to the log, not the client's group; a day with nothing to report sends
nothing.

## Approvals (`approvals.py`)

Bot runs cannot use confirmation-required tools, so a bot that may only
suggest still leaves someone retyping its suggestion. Approvals are the
sanctioned way through: the bot stores exactly what it proposes, an
authorised person presses approve or reject in the chat, and only then does
the action run — once, under the shipper's scope, with who and when recorded.

| Rule | Why |
|------|-----|
| Approvers per channel (`ARIA_CHANNEL_APPROVERS`, plus admins); none by default | fail closed |
| Only in the conversation that raised it | a forwarded card cannot be approved elsewhere |
| A single conditional UPDATE claims the request | two presses cannot both execute |
| Expires after 24 h | stale numbers should be recomputed, not approved |
| The conversation must still be bound to the same shipper | a rebound group cannot run the old client's action |
| The payload is frozen at request time | what was approved is what runs |
| Failures are recorded, not shown in the chat | exceptions can name host paths |

A channel renders the request with two buttons and calls
`ApprovalStore.decide`; actions are registered in `EXECUTORS`. The first,
`purchase_order_draft`, writes a CSV to `ARIA_APPROVAL_OUTBOX/<shipper>/` for
the 3PL's purchasing system: Aria has no integration that places orders, and
does not pretend to.

## Relay mode

In relay mode the user's machine has no Feishu app credentials — the app
secret stays on the relay — so `aria_feishu_bot._send_message` hands every
message back over the relay WebSocket and the relay sends it. The relay
accepts a send only where the client has standing: a reply to a message it
forwarded to that client (within an hour), or a post to a chat that client's
bound user has spoken to the bot in; text or cards, rate-limited. A press on a
card is routed to the client that posted the card, which holds the approval.
Attachments still need the app's credentials and are declined with a message.
