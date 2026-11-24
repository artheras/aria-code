#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="${ARTHERA_DESKTOP_DIR:-$(cd "$ROOT/.." && pwd)/Arthera/apps/desktop}"

if [ ! -d "$DESKTOP_DIR" ]; then
  echo "Desktop directory not found: $DESKTOP_DIR" >&2
  echo "Set ARTHERA_DESKTOP_DIR=/path/to/Arthera/apps/desktop and retry." >&2
  exit 1
fi

echo "==> Building aria-native..."
(cd "$ROOT/rust" && cargo build --bin aria-native)

echo "==> Building aria-computer-use..."
(cd "$ROOT/services/aria-computer-use" && npm run build)

export ARIA_NATIVE_BINARY="$ROOT/rust/target/debug/aria-native"
export ARIA_COMPUTER_USE_CLI="$ROOT/services/aria-computer-use/dist/cli.js"

echo "==> Launching ARIA Desktop with:"
echo "    ARIA_NATIVE_BINARY=$ARIA_NATIVE_BINARY"
echo "    ARIA_COMPUTER_USE_CLI=$ARIA_COMPUTER_USE_CLI"
cd "$DESKTOP_DIR"
exec npm run dev
