#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Checking required toolchains"
for cmd in cargo go node npm python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "  ✓ $cmd ($(command -v "$cmd"))"
  else
    echo "  ✗ missing $cmd" >&2
    exit 1
  fi
done

echo "==> Setting up Python .venv"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q -e ".[dev]" ruff

echo "==> Installing Node dependencies"
if [ ! -d "$ROOT/services/aria-computer-use/node_modules" ]; then
  (cd "$ROOT/services/aria-computer-use" && npm ci --no-audit --no-fund)
fi
if [ ! -d "$ROOT/npm/node_modules" ] && [ -f "$ROOT/npm/package-lock.json" ]; then
  (cd "$ROOT/npm" && npm ci --no-audit --no-fund)
fi

echo "==> Installing git pre-commit hook"
if [ -d "$ROOT/.git/hooks" ]; then
  ln -sf ../../scripts/pre-commit "$ROOT/.git/hooks/pre-commit"
  chmod +x "$ROOT/scripts/pre-commit" "$ROOT/scripts/dev_check.sh"
fi

echo "Setup complete. Run ./scripts/dev_check.sh to verify all gates."
