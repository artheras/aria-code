#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${ARIA_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

echo "==> [1/4] Rust fmt + clippy"
(cd "$ROOT/rust" && cargo fmt --check && cargo clippy --workspace --all-targets -- -D warnings)

echo "==> [2/4] Rust unit tests (aria-native)"
(cd "$ROOT/rust" && cargo test --bin aria-native)

echo "==> [3/4] Ruff + Python architecture/entrypoint tests"
"$PY" -m ruff check . --select E9,F821,F823 --statistics
"$PY" -m pytest -q \
  tests/test_container_entrypoints.py \
  tests/test_mcp_entry_handover.py \
  tests/test_rust_cli.py

echo "==> [4/4] Computer Use TypeScript tests"
(cd "$ROOT/services/aria-computer-use" && npm test)

echo "All dev-check gates passed."
