# Code review

`/review` in the CLI and `aria-code review` on the command line run the same
review service (`src/aria_code/review_service.py`). Its design follows the
review flow of OpenAI's Codex CLI (Apache-2.0); the rubric and code are
Aria Code's own.

## What gets reviewed

| Command | Target |
| --- | --- |
| `/review` · `aria-code review` | Uncommitted changes: staged, unstaged **and untracked** files |
| `--staged` | Staged changes only |
| `--base main` | Everything since the merge base with `main` (its upstream if it has one) — what a pull request would merge |
| `--commit <sha>` | One commit, with its title |
| `/review path/to/file.py` · `--file` | One whole file |

A change too large for one review is cut at a file boundary, and the
reviewer is told which files it did not see — it is never handed half a diff
in silence.

## The reviewer

The model gets three things and nothing else: the rubric, the project's own
guidance (`AGENTS.override.md`, `AGENTS.md` or `ARIA.md`, the first that
exists), and the change. No conversation history and no tools, so a review
cannot run commands or edit files. Aria's deterministic pattern checks
(`agents/code_review.py`) are passed along as leads for the reviewer to
confirm or discard, not as findings. The model is `review_model` if set,
otherwise `model`.

The rubric asks only for problems the author would fix before merging:
introduced by this change, specific, shown rather than guessed. Each finding
has a priority — `[P0]` blocks a release, `[P1]` urgent, `[P2]` normal,
`[P3]` minor — a confidence, and the narrowest file and line range. The
review ends with a verdict: *patch is correct* or *patch is incorrect*.

If the reply is not the JSON asked for, the parser tries the first `{…}` in
the text, then keeps the text as the explanation. Findings on lines the
change did not touch are listed separately and never fail a gate.

In the REPL the review is added to the conversation, so a follow-up such as
"fix finding 1" knows what finding 1 is.

## In CI

```
aria-code review --base main --fail-on P1 --json > review.json
```

Exit codes: `0` reviewed, nothing at or above the threshold; `1` a finding at
or above it; `2` the review could not run, or the reply was not a structured
review while `--fail-on` was set — a gate does not pass on a review that did
not happen.

A GitHub Actions step, with Gemini on Vertex AI through Workload Identity
Federation (no key in the repository):

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0          # --base needs the history to find the merge base
- uses: google-github-actions/auth@v2
  with:
    workload_identity_provider: ${{ vars.WIF_PROVIDER }}
    service_account: ${{ vars.REVIEW_SA }}
- run: python3 -m pip install "aria-code[google]<4"
- run: aria-code review --base origin/main --fail-on P1 --model google/gemini-3.8-flash
  env:
    GOOGLE_CLOUD_PROJECT: ${{ vars.GCP_PROJECT }}
```

The auth step provides Application Default Credentials, which Aria reaches
through the `google` extra. On a workstation a `gcloud auth login` is enough.
