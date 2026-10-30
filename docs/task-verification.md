# Declared task verification

Background tasks now distinguish execution from acceptance. `status=done` means
the runner returned; `verified=true` means the declared machine predicates passed.
The delegate's VERIFICATION heading is a report, never an acceptance receipt.

The parent model declares `checks` in `spawn_task`. Writing conditions only in
`prompt`, `context` or `success_criteria` does not register machine checks.
Existing callers without checks continue to work with `verified=null` and
`verification.status=not_requested`. The parent must explicitly report that no
machine acceptance was performed in that case.

## File-based research

```json
{
  "agent": "researcher",
  "prompt": "Read brief.md and summarize the audience and requirements.",
  "mode": "read-only",
  "checks": [
    {"kind": "tool", "tool": "read_file", "path": "brief.md"},
    {"kind": "artifact", "path": "brief.md", "format": "text", "min_bytes": 1}
  ]
}
```

This proves a successful host-observed read call and a currently readable nonempty
UTF-8 source file. It does not prove that the summary interpreted the source
correctly or that the read tool returned every byte. Those require separate review.

## Documents and datasets

```json
[
  {"kind":"artifact", "path":"requirements.json", "format":"json",
   "required_keys":["audience","requirements"], "changed":true},
  {"kind":"artifact", "path":"survey.csv", "format":"csv",
   "required_keys":["respondent","score"], "min_rows":2},
  {"kind":"artifact", "path":"summary.md", "format":"text",
   "contains":"Limitations", "min_bytes":40}
]
```

Artifact formats are binary, UTF-8 text, JSON and UTF-8 CSV (including a BOM).
JSON rejects duplicate keys and non-finite numbers; required keys refer to the
top-level object. CSV checks unique headers, consistent row widths, required
columns and minimum data-row counts. Text conditions are literal, not executable
expressions or regular expressions. An optional `sha256` requires exact bytes.

`changed=true` compares the post-task digest with a bounded pre-execution baseline
captured in the task's actual workspace, after worktree creation. A pre-existing
unchanged file cannot satisfy that predicate. A newly created file can. This does
not grant write permissions to a read-only task.

These checks validate structure and declared conditions, not arithmetic accuracy,
document rendering, completeness or editorial quality. A structurally valid JSON
report can still contain wrong values. No PDF/Office rendering adapter is added.

## Commands and tools

```json
[
  {"kind":"command", "command":"python -m unittest -v", "cwd":"."},
  {"kind":"tool", "tool":"read_file", "path":"requirements.json"}
]
```

The bridge captures actual `ToolExecutor` trace records, including prepared paths
and command parameters. The verifier does not execute commands, fetch URLs or
approve tools. The task must execute its check through its normal inherited tool
policy. A denied command has no passing execution receipt. A command with
`success=true` but exit code 1 still fails acceptance; an absent exit code fails too.
The last matching invocation wins, allowing an actual failing check followed by a
successful repair while rejecting a stale pass followed by a failure.

Command matching uses the exact prepared command string's SHA-256 and the
workspace-relative working directory. It does not equate aliases, rewrite
quoting, infer which command was intended or test semantic equivalence.
An observed command is **historical execution evidence**: it does not establish
that later source changes were tested. A current-revision test binding is future
work. Tool checks similarly prove only an observed successful invocation.

Receipts retain tool names, relative paths, success, command digests, exit codes
and timestamps. They do not copy file contents, stdout/stderr or raw commands.
The declared contract itself is persisted and can contain paths, literal conditions
or command text; it should not include credentials. Local ledger records are trusted
local state, not cryptographic attestations or a remote tenant security boundary.

## Result and lifecycle

```json
{
  "status":"done",
  "success":true,
  "verified":true,
  "verification":{
    "status":"verified",
    "coverage":"declared_predicates_only",
    "checked_at":1793280000,
    "checks":[{"index":0,"kind":"artifact","passed":true,"path":"summary.md"}]
  }
}
```

If a predicate fails, execution can remain `done` while `task_result.success=false`
and `verified=false`; the result includes the delegate's output and per-check
diagnostics for inspection. No new task, weakened criterion or wider permission is
automatically created. Failed/cancelled execution cannot count as verified.

Artifact checks run after completion and again on `task_result`, so deletion,
invalid bytes or a changed condition cannot inherit an earlier green result.
Each artifact report contains its current digest and size. A changed file that
still satisfies every declared predicate can pass with its new digest; use an
exact digest when byte identity is required. Command receipts remain historical.

Checks, bounded receipts, baselines and reports persist in the existing task
ledger. Restored reports are marked stale until retrieval rechecks predicates.
A removed worktree cannot supply current artifact evidence; retrieving that result
will fail the file predicate rather than verify a missing output.

Limits: 16 checks, 1 MiB per artifact, bounded paths and conditions, and 256
receipts per task. Truncated receipt history cannot pass execution predicates.
Paths must be relative to the task workspace; parent traversal, absolute paths,
Windows drive paths and symlink components are rejected. POSIX uses descriptor-
relative no-follow opens; other platforms reject observed symlinks and compare
file identity. This reader is not a replacement for the host's OS sandbox.

Run `pytest tests/test_task_verification.py` plus the existing role/runtime tests.
Coverage includes fabricated prose, actual gateway receipts, approved real
commands with exit codes 0/1, malformed artifacts, stale files, restart recovery,
symlink escapes, missing checks, and unchanged-output baselines.
