# Prelude routing experiment (off by default)

Aria's router decides, for each message, whether it is `coding`, `analysis`, `realtime`,
`general` or `finance`. A fine-tuned 1.5B Prelude can make that call locally. This is an
experiment, not a default: nothing changes unless you turn it on.

## Run it

Serve the model and the adapter with MLX (Apple Silicon, `pip install mlx-lm`):

```bash
python -m mlx_lm.server --model <Qwen2.5-1.5B-Instruct> \
  --adapter-path ~/.aria/prelude/adapters/intent-v0 --host 127.0.0.1 --port 8099
```

Then choose a mode before starting Aria (`ARIA_PRELUDE_MLX`):

| Mode | What happens |
|---|---|
| `off` (default) | the existing router; the model is never asked |
| `shadow` | both are asked, **the existing answer is used**, and one line records whether they agreed |
| `on` | the model's label is used; no answer, a timeout or an error falls back to the existing router |

`ARIA_PRELUDE_MLX_URL` points at a different server (default `http://127.0.0.1:8099`). The
model is given two seconds. A message that is a visual market artifact request (charts,
reports) is never sent to the model; the existing rule owns it.

## What shadow mode keeps

`~/.aria/prelude/shadow.jsonl`, owner-only, outside every repository: the two labels,
whether they agreed, the message length and an eight-character hash. **Never the message.**

```bash
python -m aria_code.apps.cli.intent_shadow
```

prints how many messages were compared, how often the model was silent, the agreement rate
and the most common disagreements (for example `general -> finance`).

## How to read it

Agreement is not accuracy. The existing router is wrong in known ways (it sends many
personal-finance questions to `general`), so a disagreement can be the model being right.
To know which, label a sample of real messages yourself with
`python -m aria_code.training.prelude_real`, which keeps them on this computer and reports
counts only.

The labels the model was trained on put personal-finance decisions, finance concepts and
macro commentary under `finance`, judgements on a named security or sector under `analysis`,
programming concepts under `coding` and writing under `general`. The router's existing
post-rules (for example "macro topics are never `analysis`") apply only to the existing
path, not to the model's label in `on` mode.

## Cost and limits

About 100 ms per message on an M1 Pro once the server is warm; `shadow` adds that to every
turn because it asks both. On the 251 hand-written messages the trained model scored 93% on
the held-out part (untrained base 65%), but those were written by one author; no independent
accuracy figure exists yet. The adapter is MLX only, so it cannot be loaded by Ollama.
