# Terminal conversation and operation design

Aria uses a Rust terminal frontend and a persistent Python model/tool service.
The frontend owns display and editing; the service owns approvals, workspace
permissions, model routing, tool execution and session persistence. The installed
path retains that worker. The opt-in Go replacement and revised native cream
robot are described in [the migration inventory](native-runtime-migration.md).

## Public references reviewed on 2026-10-11

| Reference | Verifiable design | Aria implementation |
| --- | --- | --- |
| [Codex](https://github.com/openai/codex), [Markdown renderer](https://github.com/openai/codex/blob/main/codex-rs/tui/src/markdown_render.rs) | CommonMark events become styled terminal lines; tables and wrapping belong to display code | Separate `chat/markdown.rs` renderer, keeping original answer text in application state |
| [Codex history cells](https://github.com/openai/codex/blob/main/codex-rs/tui/src/history_cell/mod.rs) | Different history entries have different presentation responsibilities | Explicit operation state, one compact row per operation, in the event stream's order |
| [Claude Code interactive mode](https://code.claude.com/docs/en/interactive-mode) | Keyboard editing, multiline input, transcript details and compact operation presentation | Existing editing and approval flow retained; Ctrl+O expands operation parameters and timings |
| [Claude Code repository](https://github.com/anthropics/claude-code) | Public documentation, plugins, examples and change history | Useful interface references; this repository does not expose the full application UI implementation |

These are observations from public sources, not claims about either product's
private implementation. Aria's changes are independently implemented.

## Behaviour contracts

- A standalone greeting, thanks or goodbye gets no tool schemas or project
  context. The executor also rejects unsolicited tool calls during that turn.
  Such turns skip task worktree preparation and code acceptance/review gates.
- Matching uses the whole message. `你好，读取 README.md`, `yes`, `continue`
  and messages with an explicit system override retain normal task execution.
  A following file task restores tools and still requires normal write approval.
- Identical local reads in the same parallel batch share one execution, with
  independent result objects. There is no cache across rounds; later file edits
  must remain visible. Different targets and mutating tools are not coalesced.
- Answers and operations appear in chronological event order. An operation's
  row updates from pending to completed, failed or cancelled. There are no
  separate empty "Action" headings or duplicate start/completion rows.
- Operation summaries include a target and supported result metadata.
  Workspace files use relative paths; outside paths remain explicit. Counts
  derive from actual returned items, not prose. Ctrl+O shows redacted parameters.
- Approval dialogs carry the context of the specific operation being approved,
  rather than the last tool announced in a parallel batch. Existing approval
  options, deny policy and permission scope continue to control execution.
- Headings, lists, inline code, links, fenced code and tables render as Markdown.
  Code whitespace is preserved; narrow tables become labelled rows. Ordinary
  list items are not styled as deleted code.
- UI language follows `ui_lang`. Light and dark text palettes have automated
  4.5:1 contrast checks against supported white and near-black backgrounds.
  The robot uses its own palette independently of text contrast.
- The input has a rounded boundary, a prompt gutter and an empty-state hint.
  It grows from one to six visible content rows; long input scrolls within the
  boundary. Wrapping uses exactly the available text width after borders,
  padding and the prompt, including Chinese characters and emoji. Send/newline
  hints shorten on narrow terminals. Busy input is a draft that the user
  sends after the current task; it is not automatically queued or submitted.
  The footer shows status and elapsed time once. Manual history scrolling keeps
  the viewport stable as new output arrives.

## Compact mascot and composer, 2026-10-12

The [official Claude Code 2.1.27 distribution](https://www.npmjs.com/package/@anthropic-ai/claude-code/v/2.1.27)
contains a compact mascot made from three text rows, at most nine columns
including its spacing. This is a measurement of that published version, not a
claim about all versions or access to its private source repository. It informed
the visual budget; no Claude mascot artwork is included in Aria.

Aria's compact mascot uses 14 columns by six rows, including a half-row of
transparent space above the stepped cap. This replaces the 12-by-five prototype:
its single-column feet looked too thin and its eyes lost definition. The two
ivory eyes are identical, each two columns by one row, mirrored about the screen. Each of the four attached feet is
1.5 columns wide by one row high. Mirrored half-column edges keep the feet
symmetrical; their gaps retain the terminal's own background.

The cream shell, inset black screen, orange base line and ear details follow
`docs/assets/aria-robot.png`. The original asset is unchanged. The compact
terminal rendition is pixel-fitted separately, not pixel-identical image
scaling. Standard half/quarter/eighth block glyphs avoid terminal image
protocols. The compact size stays fixed when the window grows; pixel proportions
also depend on the terminal's font and line height.

The Python worker sends an optional `robot_design` alongside its existing span
packet. New Rust frontends draw the named mascot, matching the Go worker;
older frontends still receive their supported spans. `--no-banner`,
`--banner off` and `ARIA_ROBOT_RENDER=off` keep the drawing hidden.

The rounded composer keeps its two-column prompt gutter separate from editable
text. Chinese multiline paste, long input scrolling, shrinking after deletion,
and resizing from 100 to 60 columns were checked through the actual source-built
Rust/Go processes in a pseudo-terminal. Source-build previews are reconstructed
from captured terminal cells, not screenshots of an installed release.

## Validation

Regression coverage includes a provider attempting an unsolicited greeting
tool call, greeting followed by an approved file write, repeated and fresh reads,
multi-round answer ordering, distinct approval targets, cancellation, session
restoration, narrow/wide Markdown tables and viewport anchoring. Native PTY
tests exercise paste, resize, approval denial, cancellation, process cleanup and
terminal restoration. These tests run in the Rust bridge CI matrix.

A local verification on 2026-10-11 used the configured Google model, not an
inference fixture. Greeting took 5.93 seconds and made zero tool calls. Reading
a temporary project file returned an unpredictable marker from its contents.
An approved edit changed that actual file and was read back. A separate Chinese
PTY run verified operation-before-answer ordering, unchanged robot colours and
terminal restoration. Two source frontend samples reached ready state in
0.491 and 0.918 seconds.
These are single local samples, not cold-start or cross-product benchmarks.

## What would justify a claim of better task execution

UI polish alone does not establish better coding ability than Codex or Claude
Code. Evaluate all candidates on the same repositories, task specifications,
available model, context budget, permissions, machine and network conditions.
Record task acceptance-test pass rate, unnecessary file reads, tool failures,
recovery success, time to first visible feedback, total completion time and
cost. Include Chinese/English prompts, multi-file edits, build/test repair,
approval rejection and cancelled-task recovery. Report failures and sample
sizes alongside results. Aria's greeting scope and repeated-read rules are
specific improvements that can already be verified without that comparison.

Remaining work includes richer verified-change summaries, side-by-side edit
previews, large-session incremental Markdown rendering and a reproducible
coding-task benchmark. The model/tool runtime remains Python; these interface
changes do not claim a completed Rust/Go rewrite of the entire product.
