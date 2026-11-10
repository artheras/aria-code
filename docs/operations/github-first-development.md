# GitHub-first development

The canonical core repository is <https://github.com/artheras/aria-code>.
`main` is the integration source; release tags identify published versions.
A developer checkout, an installed CLI and a running service can legitimately
have different versions. Never use the newest local folder as an implicit
release source.

## Starting or continuing work

```bash
git clone https://github.com/artheras/aria-code.git
cd aria-code
git switch -c feature/your-change origin/main
```

Follow [CONTRIBUTING](../../CONTRIBUTING.md) for native and Python setup. Use a
separate environment per checkout. Pull requests contain code, tests and
dependency pins; local sessions, credentials, caches, generated installers and
virtual environments do not belong in the repository. Store release credentials
in the existing CI secret store, never in a source migration.

Before updating an existing branch:

```bash
git status --short
git fetch origin
git log --left-right --oneline HEAD...origin/main
```

Commit or preserve local changes before rebasing or merging. Inspect unpushed
branches, ignored configuration and `git worktree list` before retiring an old
checkout. A worktree's Git metadata can live inside that old folder. A Git
bundle preserves committed refs, not uncommitted edits or ignored files; keep
those separately until reviewed. Never replace a whole repository with a copy
from one developer's machine.

Push the feature branch and open a pull request into `main`. Other contributors
can propose fork-based PRs; direct branch pushes require organization/repository
write access. This procedure does not change repository visibility, team access
or branch protection. Repository administrators should use existing teams and
require review and passing checks for merges.

## Implementation owners

| Capability | Canonical owner | Consumer boundary |
| --- | --- | --- |
| Core CLI, Rust UI and Go runtime | `artheras/aria-code` | Versioned core releases |
| Shared finance calculations and read-only market tools | `artheras/financial-service` | MCP via `/finance setup`; managed compatibility copies below |
| Feishu/Telegram integration and logistics adapters | `services/aria-bots` in aria-code | Separate package/image using the runtime protocol |
| Hosted project-review HTTP API | `project_review_api.py` / `Dockerfile.review` in aria-code | Active Python deployment; Go replacement candidate is `go/projectreview` / `Dockerfile.review-go` |
| Private application/backend services | Their organization repositories | Published API/package contracts; explicit legacy source opt-in only |

No default core import should discover a private backend in `~/Desktop` or by
walking sibling checkouts. Normal contributors do not need the private backend
to work on core. `ARTHERA_ROOT` explicitly enables the legacy package bridge;
`ARTHERA_PATH` is an explicit legacy integration option.
Bundled modules keep precedence. `/packages connect arthera` requires a real
configured MCP entrypoint and will not write a guessed developer path.

### Managed finance compatibility sources

`finance-core.lock.json` lists three managed Python modules. Their
implementation lives in financial-service; the checked-in copies keep existing
Python imports and Python 3.10 consumers working without installing the
service's Python 3.11+ package. This is a transitional boundary, not two owners.

The pure backtest engine and statistics live in `domain/_finance_core/` as
unchanged upstream files. `domain/backtest_engine.py` keeps only public
compatibility exports, data loading and order-intent conversion. Broker access
and host data providers are not dependencies of the pure kernel.
`domain/performance_stats.py` aliases the managed module so old imports and
monkeypatches refer to the same statistics object.

1. Change and review the implementation in financial-service.
2. Record the full reviewed commit SHA in a core PR:

   ```bash
   python scripts/sync_finance_core.py --update FULL_COMMIT_SHA
   python scripts/sync_finance_core.py --check
   python -m pytest tests/test_performance_stats.py tests/test_backtest_engine.py tests/test_sec_statement_fallback.py tests/test_finance_core_boundaries.py -q
   ```

The update downloads only the three known files from that immutable GitHub commit
and parses, but does not execute, their source. `--source /path/to/source-clone`
can read the same committed tree locally; unstaged edits are never imported.
Review both the code and lock diff. Updates refuse to overwrite a locally
modified managed copy. Normal checks need no network, and CI detects edits that
do not match the pin. LF attributes keep byte hashes consistent on Windows.

## Python removal gates

Delete by proven ownership and runtime reachability, not filename or language.
Import searches alone miss CLI registries, dynamic tools, `python -m`, Docker
entrypoints and external consumers.

| Code | Current decision | Gate before physical deletion |
| --- | --- | --- |
| Implicit private source discovery | Removed from core namespace/SDK discovery and the legacy MCP connection default | Explicit integration continues to be tested |
| Finance statistics, SEC parser and pure backtest engine | One upstream owner, immutable pin and offline check; host adapters separated | Replace remaining Python callers with a compatible shared package or service contract before deleting the managed sources |
| Four unregistered financial skill/demo wrappers | Removed after confirming no product callers; calls used demo mode or incompatible arguments | Reintroduce only with real input, provenance and result contracts |
| Python CLI/worker/UI and Go ports | Keep compatibility paths | Retire pure-wheel fallback, CLI `run`/help paths and explicit Python engine support, or provide equivalent replacements |
| Bot legacy imports | Keep thin compatibility shims; implementation is in aria-bots | Migrate external callers and version the import change |
| Hosted project-review API | Go replacement candidate added; current deployment stays on Python | Compare report fidelity, route/auth/error and workspace lifecycle contracts before changing the production image; keep both implementations during transition |
| Unique trading/analyst/finance tools | Keep until ported or deliberately retired | Match data, preview/confirmation and output contracts; verify callers use the replacement |
| Retired cloud/APNs integrations | APNs push removed from `aria_daemon.py` and `.env.daemon.template`; audit remaining cloud callers separately | Move to an explicit service API or remove the feature and its callers together; never return fake success |
| Evals/training | Keep Python intentionally | No deletion required for native core migration |

Four old local financial-tool wrappers were removed after the source audit
found no callers outside their own package and two agent adapters. They invoked
skill scripts in demonstration mode; the risk path could therefore show sample
portfolio results for an arbitrary ticker. Both financial agents now use only
the market/fundamental fields already fetched for the requested symbol, and
state when data or a real stress/factor report is missing. A future factor or
portfolio-risk service must accept real, timestamped input and return provenance.

Use [the detailed audit](../python-removal-audit.md) and the
[command migration matrix](../command-migration-matrix.md) for the remaining
service work. A clean GitHub clone must pass the relevant tests without any old
developer checkout present. Deployment and removal are separate changes when
there are live consumers.
