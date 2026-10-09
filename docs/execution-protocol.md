# Aria Code Execution Protocol

Aria Code competes on the control layer of software engineering, not on
writing code faster: every code change should be **bounded, verified,
explained, approved by risk and reversible**.

```text
Understand before editing.   Plan before executing.
Approve by risk, not by command.   Change atomically.
Verify every change.   Review independently.   Make everything reversible.
```

The runtime state machine this aims for:

```text
INSPECT → PLAN → (approval if needed) → EXECUTE → VERIFY → REVIEW → DELIVER
                                           ↑                    │
                                           └──── REPAIR ◄───────┘
```

## What exists today

| Piece | Where | Status |
|---|---|---|
| Durable runs and event log | `runtime/run_state.py`, `runtime/run_store.py` | shipped |
| Acceptance gate — "done" requires green checks | `runtime/acceptance.py` | shipped |
| File checkpoints, `/rewind code <run>` | `runtime/checkpoints.py` | shipped |
| Worktree isolation for background agents | `runtime/worktrees.py` | shipped |
| Symbol-ranked repo map | `runtime/repo_map.py` | shipped |
| **Risk assessment** (L0–L4, capabilities, blast radius) | `safety/risk.py` | **phase 1** |
| **Change contract**, enforced per tool call | `runtime/contract.py` | **phase 1** |
| **Delivery report** from evidence | `runtime/delivery.py` | **phase 1** |
| **Risk-aware approval** (card, `approval_mode: risk`, L4 always asks) | `apps/cli/tool_executor.py` | **phase 2** |
| **Independent review gate** | `runtime/review.py` | **phase 2** |
| **Actions transcript** (Explored / Ran / Edit) | `ui/render/actions.py` | **phase 3** |
| **Approval shortcuts, deny with feedback** | `ui/picker.py`, `runtime/approval.py` | **phase 3** |
| **Semantic diff** (definitions touched, behaviour, tested by) | `runtime/semantic_diff.py` | **phase 3** |
| **Detail on demand** (output tail, ctrl+o) | `ui/render/actions.py` | **phase 3** |
| **Edit by symbol** (`edit_file` with `symbol`) | `runtime/symbol_edit.py` | **phase 3** |

## Risk levels

`assess_command()` / `assess_tool()` describe what an action touches.

| Level | Meaning | Examples |
|---|---|---|
| L0 | read, inspect, verify | `ls`, `rg`, `git diff`, `pytest`, `tsc --noEmit` |
| L1 | change inside the workspace | edit a file, `git add`, `sed -i` |
| L2 | change the environment | `npm install`, `pip install`, `curl` |
| L3 | act outside the local repo | `git commit`, `git push`, `gh pr create`, HTTP POST |
| L4 | destructive or production | `rm`, force push, `reset --hard`, migrations, deploy, secrets, `sudo` |

Each assessment also names its **capabilities** — `filesystem.write`,
`network.read`, `package.install`, `git.push`, `github.pr`, `database.write`,
`secret.read`, `cloud.deploy`, `system.admin`, … — whether it is
**reversible**, and its **blast radius**: hosts (`registry.npmjs.org`), files
(`package.json`, `package-lock.json`) and systems (`Database schema`).

It is descriptive: the existing command policy still decides what runs, and an
assessment never reports a command milder than that policy does.

## Change contracts

A project bounds what a coding task may change in `.aria/policy.yaml`:

```yaml
contract:
  allow: [src/auth/**, tests/**]      # paths the task may change (default: whole workspace)
  restrict: [src/auth/secrets.py]     # never, even inside allow
  forbid: [git.push, cloud.deploy, database.write]   # capabilities it may not use
  max_level: 3                        # highest risk level an action may have
  success:
    - existing tests pass
    - expired sessions refresh
```

Each request becomes the contract's goal. The model sees the contract before
the first round; the runtime checks every tool call against it before anything
runs and refuses the ones that break it — the call does not execute, and the
model receives the reason as the tool's result. Reading is never restricted.

A `policy.yaml` that cannot be read (bad YAML, an unknown capability, a
`max_level` outside 0–4) gives a **fail-closed** contract: reads and checks
only, with the error shown to the model so it can report it. It never silently
becomes "no contract".

## Delivery report

When a turn changed files, ran checks or had calls refused, the runtime ends
it with a report built from what happened, not from the model's summary:

```text
DONE

Changed
  M src/session.py  +2 -1
  A src/refresh.py  +3
  2 files · +5 / -1

Verified
  ✓ pytest -q

Review
  Not reviewed

Risk
  L2 medium · Install dependency

Checkpoint
  2 checkpoints · /rewind code a1b2c3d4

Next
  Ready to commit
```

`INCOMPLETE` whenever the turn stopped early or a check is red, however the
model's last message reads. `delivery_report: false` in the config hides it in
the REPL; headless (`-p`) results always carry it, and the contract, as data.

## Approval by risk

Every approval prompt opens with the assessment:

```text
  Risk     L2 medium · 43/100 · reversible
  Network  registry.npmjs.org
  Files    package.json, package-lock.json
  Why      Changes installed dependencies (npm install)
```

| `approval_mode` | Behaviour |
|---|---|
| `manual` (default) | asks as before; the card explains what is being approved |
| `risk` | runs actions at or below `auto_approve_level` (0–2, default 1) without a prompt, with a dim `✓ auto-approved · L1 low · …` line |

L3 is never automatic. **L4 always asks** — even after "always allow" for the
tool or the command prefix, which was given for something milder. Deny lists,
plan mode and PreToolUse hooks apply first.

```text
/config set approval_mode=risk
/config set auto_approve_level=2
```

## Independent review

`/config set review_gate=true` adds a reviewer to coding turns. Once a turn
that changed files has green checks (or none could be inferred), the same model
is called again with a **fresh context and no tools**. It sees the goal, the
contract, the check results and the diff — not the builder's transcript — and
answers in JSON: findings marked `blocking` or `suggestion`. Style is never
blocking.

- Blocking findings go back to the builder once (`review_max_attempts`,
  default 1); the checks run again after it edits, then the new diff is reviewed.
- Findings that survive the repair leave the delivery report `INCOMPLETE`
  with *Address the review's blocking findings*.
- A reviewer that does not answer in JSON never blocks; the report says the
  review did not complete.

It costs one model call per reviewed change, so it is off by default.

## The transcript

Tool calls are shown as actions:

```text
⏺  Explored
   └ Read session.py, refresh.py
     Search "refresh_token"
⏺  Edit src/auth/session.py
   └ ✓ +12 -3 · 14ms
⏺  Ran python3 -m pytest -q
   └ ✓ 2.1s · 14 lines
     … +12 lines (ctrl+o)
     ........
     5 passed in 0.31s
```

Three levels of detail: the cell; the tail of a command's output inside it
(two lines when it passes, four in red when it fails); and **ctrl+o**, which
prints every action of the last turn in full — commands with their whole
output, edits with their whole diff, errors.

Reads, searches, listings, `git status`/`diff` and read-only commands coalesce
into one *Explored* cell, printed when the next kind of action starts or the
answer begins. `tool_display: classic` restores one line per call.

Approval prompts are numbered with one-key answers — `y` yes, `a` always for
this scope, `n` no. *No, tell Aria what to do instead* asks for a sentence: it
goes to the model as the declined call's result and the turn continues. Enter
with nothing typed, or Esc, stops the turn.

## Semantic diff

The delivery report names the definitions each file's change touched,
computed by undoing the turn's diffs on the current file and comparing the
definitions in both versions (any language `repo_map` parses):

```text
Changed
  M src/auth/session.py  +12 -3
      SessionManager added · Session.refresh() modified · legacy_refresh() removed
      tested by tests/test_session.py
```

*Tested by* lists test files that reference an added or modified definition
(a method only where its class is referenced too).

## Edit by symbol

`edit_file` takes `symbol` — a name or `Class.method` — in place of
`old_string`; `new_string` is the whole new definition, or with
`position: "after"` code to insert after it. The definition's full text is
found and becomes `old_string`, so it is the same tool with the same
approval, preview, checkpoint, contract and checks. Code written
flush-left is indented to the definition's level.

With the review gate on, the reviewer also describes the change as behaviour —
*Before / After / Why / Impact* — shown above its findings.

## How Codex and Claude Code present the same things

From the Codex TUI source (`codex-rs/tui`, its render snapshots) and the
Claude Code changelog, October 2026:

- **Approvals** (both): a bold question, an italic *Reason*, the permission
  rule or command, then numbered choices with one-key shortcuts — *yes once*,
  *yes for this session / this host / don't ask again for this prefix*, *no,
  and tell the agent what to do differently* (esc). Aria's card adds what
  neither shows: level, score, reversibility and blast radius.
- **Transcript** (Codex): actions, not tool calls — consecutive reads and
  searches coalesce into one `• Explored └ Read a.rs, b.rs` cell; commands
  are `• Ran <cmd>` with head/tail output and `… +N lines`; edits are
  `• Edited 2 files (+2 -1)` with per-file counts; plans are
  `• Updated Plan · 1/4 complete` with ✔ / □.
- **Detail on demand** (both): the full transcript behind one key
  (`⌃T` / `ctrl+o`); the default view stays one line per action.
- **Rewind** (Claude Code): `Esc Esc` / `/rewind` restores code, conversation
  or both.

## Roadmap

**Phase 4 — transactions and project knowledge**

- Worktree per task by default, merged on approval.
- Transaction checkpoints that also restore task graph, approvals, test
  baseline and conversation, not only files.
- Persistent project graph (files, symbols, tests, services and their edges)
  for impact analysis before editing.
- `.aria/workflows/*.yaml` for user-defined pipelines (`/release` → test →
  build → security review → changelog → PR).
