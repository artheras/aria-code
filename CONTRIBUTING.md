# Contributing to Aria Code

We welcome contributions of all kinds — bug fixes, new features, documentation improvements, and new financial tools.

---

## Quick Start

```bash
git clone https://github.com/artheras/aria-code.git
cd aria-code
git switch -c feature/your-change origin/main
```

For the full multi-language development environment (Rust, Go, Python `.venv`, Node.js, and git pre-commit hook), run:

```bash
./scripts/dev_setup.sh
./scripts/dev_check.sh
```

To build and run the native Rust CLI (`aria-native`) directly:

```bash
bash scripts/run_native_dev.sh --help
```

For Python tests, evals, or Python services (`services/aria-bots`), use Python 3.10–3.13 inside `.venv`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate

# Editable install so `aria-code` and `aria_code` resolve cleanly.
pip install -e ".[dev]"

# Run the unified multi-language verification gate.
./scripts/dev_check.sh
```

Do not reuse another checkout's virtual environment or copy its ignored local
configuration. The normal application does not require a sibling private
Arthera checkout. See [GitHub-first development](docs/operations/github-first-development.md)
for repository ownership, service setup and the legacy opt-in configuration.

Ollama is optional. Install it only when working on local inference; choose a
model supported by your machine. Provider tests use stubs rather than a paid
model request:

```bash
ollama pull qwen2.5-coder:7b
aria-code --model qwen2.5-coder:7b -p "hello"
```

---

## What to Contribute

### Great first issues
- Add a new `/slash` command in `rust/cli/src/chat/`
- Add a quantitative algorithm or test in `rust/quant/src/`
- Improve an enterprise agent pack template in `rust/cli/src/engine/pack.rs`
- Add a browser/MCP tool or E2E test in `services/aria-computer-use/`
- Write a new Rust, Go, or Python unit test

### Bigger contributions
- Native engine tools, MCP, or permission rules in `rust/cli/src/engine/`
- Go runtime / relay / projectreview improvements in `go/`
- Logistics & bot channel integrations in `services/aria-bots/`
- Evaluation & training pipeline improvements in `src/aria_code/evals/` & `src/aria_code/training/`

---

## Code Style

```bash
# Format (Black)
pip install black
black .

# Lint (Ruff)
pip install ruff
ruff check .

# Type check (mypy)
pip install mypy
mypy src/aria_code --ignore-missing-imports
```

**Rules:**
- Max line length: 100
- Docstrings on all public functions
- Type annotations on new code
- No `print()` for logging — use `console.print()` (Rich) in CLI context

---

## Running Tests

```bash
./scripts/dev_check.sh                   # Full Rust + Go + Python + TS verification gate
pytest tests/ -q                         # Python test suite
pytest tests/ -k "not slow_packaging"    # Skip wheel build packaging tests
```

---

## Commit Convention

```
feat(cli): add /whale slash command for dark pool detection
fix(formula): handle unclosed LaTeX buffer at stream end
docs(readme): add Tushare setup instructions
test(provider): add Ollama connection timeout test
```

Types: `feat` · `fix` · `docs` · `style` · `refactor` · `test` · `chore`

---

## Branch Convention

Long-lived branches:

- `main` for protected releases.
- `develop` for integration, if the team keeps a staging branch.

Short-lived branches:

- `feature/<topic>`
- `fix/<topic>`
- `refactor/<topic>`
- `chore/<topic>`
- `docs/<topic>`
- `release/vX.Y`
- `codex/<topic>` for temporary agent-authored work only.

Do not merge directly into `main`. Open a pull request and wait for CI.

For organization migration and repository ownership, see
[`docs/operations/github-first-development.md`](docs/operations/github-first-development.md).
The earlier [organization migration runbook](docs/operations/github_enterprise_migration.md)
is historical. Run the preflight when changing remotes:

```bash
python3 scripts/github_migration_preflight.py
```

---

## PR Checklist

- [ ] Tests pass: `pytest tests/ -v`
- [ ] Code formatted: `black . --check`
- [ ] No hardcoded API keys, IPs, or credentials
- [ ] New features have at least one test
- [ ] `requirements.txt` updated if new dependency added
- [ ] Graceful fallback if optional dependency is missing

---

## Adding a Financial Tool

The finance service owns shared financial calculations. Prefer adding a new
service capability in `artheras/financial-service` and consuming it through
`/finance setup`. The three managed compatibility modules listed in
`finance-core.lock.json` must be changed upstream and imported with
`scripts/sync_finance_core.py --update FULL_COMMIT_SHA`; do not edit both copies.

For an existing local-only Python compatibility tool, the registry pattern is:

```python
def calculate_your_metric(param1: float, param2: float) -> dict:
    """
    Brief description. Formula: result = param1 / param2
    
    Args:
        param1: Description of param1
        param2: Description of param2
    
    Returns:
        dict with keys: result, formula, interpretation
    """
    result = param1 / param2
    return {
        "result": result,
        "formula": "result = param1 / param2",
        "interpretation": f"Value is {result:.2f}"
    }
```

Then register it in `LOCAL_FINANCE_TOOL_REGISTRY` in
`src/aria_code/tools/local_finance_tools.py` so the model can call it. The
finance pack in `src/aria_code/packs/finance/` reads that registry to advertise
the tool's name.

---

## Security

**Never commit:**
- API keys or tokens
- `.env` files (only `.env.example`)
- Private keys (`.pem`, `.key`)
- Hardcoded server IPs

Report security vulnerabilities privately: use **Security → Report a vulnerability** on GitHub, or email `security@arthera.xyz`. Please do not open a public issue.

---

## License

By contributing, you agree your contributions will be licensed under the
[Apache License, Version 2.0](LICENSE) — the same license the project itself
uses. This line used to say MIT, which had not been the project's license since
4.2.0; a contributor reading it was being told the wrong terms.

Note that Apache 2.0 includes an explicit patent grant (section 3) and requires
that modified files carry a notice of change (section 4b). If you are
contributing on behalf of an employer, check that you are authorized to grant
those terms.
