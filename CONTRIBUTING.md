# Contributing to Aria Code

We welcome contributions of all kinds — bug fixes, new features, documentation improvements, and new financial tools.

---

## Quick Start

```bash
git clone https://github.com/artheras/aria-code.git
cd aria-code
python3 -m venv .venv && source .venv/bin/activate

# Editable install. This is what puts `aria-code` on your PATH and what makes
# the package importable both ways the tree needs; `pip install -r
# requirements.txt` alone does neither.
pip install -e ".[dev]"

# Verify everything works
aria-code --version
aria-code -p "hello"
```

Ollama is optional. The default model is a cloud one (`google/gemini-2.5-pro`),
and the test suite does not need a model at all — it stubs providers. Install
Ollama only if you are working on the local-inference path:

```bash
ollama pull qwen2.5-coder:7b
aria-code --model qwen2.5-coder:7b -p "hello"
```

---

## What to Contribute

### Great first issues
- Add a new `/slash` command
- Add a new financial formula to `local_finance_tools.py`
- Improve a built-in prompt template
- Add a new data source adapter in `datasources/sources/`
- Fix a formatting issue in `finance_formulas.py`
- Write a new test

### Bigger contributions
- New LLM provider in `providers/llm/`
- New specialist agent in `agents/financial/`
- New quant strategy in `strategy_vault.py`
- MCP tool improvements

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
pytest tests/ -v
pytest tests/test_aria_cli_core.py -v    # Core tests only
pytest tests/ -k "not e2e"               # Skip end-to-end (requires running LLM)
```

`tests/test_terminal_snapshots.py` compares what the terminal shows for key commands (`/help`, `/team`, `/portfolio`, `/compare`, `/peer`, a market snapshot) with the text in `tests/snapshots/`. If you change one of those outputs on purpose, regenerate the snapshots and review the diff like any other change:

```bash
ARIA_UPDATE_SNAPSHOTS=1 pytest tests/test_terminal_snapshots.py
git diff tests/snapshots/
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
[`docs/operations/github_enterprise_migration.md`](docs/operations/github_enterprise_migration.md).
Run the migration preflight before pushing to a new Arthera remote:

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

All local financial calculations live in `local_finance_tools.py`. To add one:

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
