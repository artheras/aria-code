# Aria Code — Privacy Policy

_Last updated: 2026-06-23 · Draft — review with legal counsel before relying on it._

Aria Code is a **local-first** financial terminal. By default it runs entirely
on your machine and **sends no usage data, prompts, or financial information to
Arthera or any third party**. This document explains exactly what data exists,
where it lives, and what changes only if you explicitly opt in.

---

## 1. What stays on your machine (always)

The following never leave your device unless *you* transmit them (e.g. by
configuring a cloud LLM provider or broker that you choose):

- **Credentials & secrets** — LLM API keys, broker API keys/tokens. Stored
  locally (`~/.arthera/`, `~/.aria/`). Aria never transmits these to Arthera.
- **Financial data** — your positions, orders, account balances, watchlists,
  trade journal, and any portfolio data. These are read from the broker/data
  source you configured and are processed locally.
- **Conversations & sessions** — your prompts, model responses, and saved
  sessions (`~/.arthera/sessions/`).
- **Notes, alerts, memory** — anything you write via `/note`, `/alert`,
  `/journal`, `/memory`.

When you point Aria at a **cloud LLM** (e.g. OpenAI, DeepSeek) or a remote data
service, your prompt/query goes to **that** provider under **their** privacy
terms — not to Arthera. Use a local model (Ollama) to keep everything offline.

## 2. What is collected — only if you opt in

Aria has an **opt-in, off-by-default** feedback/telemetry mechanism. Nothing in
this section happens unless you explicitly enable it.

- **Default state:** `data_sharing = false`, `feedback_upload = false`.
  No telemetry, no uploads.
- **How to opt in:** `/privacy opt-in` (enables both flags).
- **How to opt out / erase:** `/privacy opt-out`, and `/privacy delete` to wipe
  all local feedback records.

If — and only if — you opt in, the following may be shared with Arthera to
improve the product and its models:

| Data | Purpose | Notes |
|------|---------|-------|
| Feedback records (`/feedback`) | Improve model quality | Your rating, the related model message, optional comment, model id, a local session id, timestamp |
| Anonymized usage signals | Improve product & features | Which commands run, error types — designed to be aggregated, not tied to your identity |

We do **not** ask for, and you should **not** submit, broker credentials,
account numbers, or personally identifying financial details through feedback.

## 3. Where local data is stored

- `~/.arthera/` — config, sessions, feedback (`~/.arthera/feedback/feedback.jsonl`), tool policy
- `~/.aria/` — environment/secrets (`.env`), data-source config

You own these files. You can inspect, export (`/privacy export`), or delete them
at any time.

## 4. Retention & your rights

- **Local data** persists until you delete it (`/privacy delete`, or remove the
  files above).
- **Opt-in shared data**, if any, is retained only as long as needed for the
  stated purposes; you may request export or deletion at privacy@arthera.xyz.
- You can withdraw consent at any time with `/privacy opt-out`.

## 5. Future changes

As Aria Code evolves toward commercial offerings, any expansion of data
collection will:

1. remain **opt-in and off by default**,
2. be disclosed in an updated version of this policy **before** collection
   begins, and
3. never retroactively apply to data gathered under a prior policy.

## 6. Contact

Questions or data requests: **privacy@arthera.xyz**

---

_This is a good-faith engineering draft, not legal advice. If you collect or
process financial data or personal information — especially across China (PIPL),
the EU (GDPR), or other jurisdictions — have it reviewed by qualified counsel._
