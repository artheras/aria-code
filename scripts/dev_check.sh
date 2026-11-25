#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${ARIA_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

echo "==> [1/6] Rust fmt + clippy"
(cd "$ROOT/rust" && cargo fmt --check && cargo clippy --workspace --all-targets -- -D warnings)

echo "==> [2/6] Rust workspace tests (cli, graph, physical, quant)"
(cd "$ROOT/rust" && cargo test --workspace)

echo "==> [3/6] Go vet & unit tests"
for mod in buildtools projectreview relay runtime; do
  if [ -d "$ROOT/go/$mod" ]; then
    (cd "$ROOT/go/$mod" && go vet ./... && go test ./...)
  fi
done

echo "==> [4/6] Ruff + Python architecture/contract tests"
"$PY" -m ruff check . --select E9,F821,F823 --statistics
if [ -z "${ARIA_NATIVE_BINARY:-}" ] && [ -x "$ROOT/rust/target/debug/aria-native" ]; then
  export ARIA_NATIVE_BINARY="$ROOT/rust/target/debug/aria-native"
fi
"$PY" -m pytest -q \
  tests/test_container_entrypoints.py \
  tests/test_mcp_entry_handover.py \
  tests/test_erp_logistics_host.py \
  tests/test_native_mcp_contract.py \
  tests/test_rust_cli.py

echo "==> [5/6] Computer Use TypeScript tests"
(cd "$ROOT/services/aria-computer-use" && npm test)

if [ -d "$ROOT/services/arthera-for-confluence/backend/tests" ]; then
  echo "==> [6/6] Confluence Gateway backend tests"
  "$PY" -m pytest -q services/arthera-for-confluence/backend/tests
fi

echo "All dev-check gates passed."
