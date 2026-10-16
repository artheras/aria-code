#!/usr/bin/env bash
# Run this checkout's Rust/Go implementation without replacing installed commands.
set -euo pipefail
native_dev_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
native_dev_output="$native_dev_root/build/native-dev"
mkdir -p "$native_dev_output"
(
  cd "$native_dev_root/go/runtime"
  go build -trimpath -o "$native_dev_output/aria-runtime" .
)
cargo build --locked --manifest-path "$native_dev_root/rust/Cargo.toml" \
  --target-dir "$native_dev_root/rust/target" -p aria-native
if [[ $# -eq 0 ]]; then
  set -- chat
fi
export ARIA_RUNTIME="$native_dev_output/aria-runtime"
if [[ -d "$native_dev_output/tessdata" && -z "${ARIA_OCR_DATA:-}" ]]; then
  export ARIA_OCR_DATA="$native_dev_output/tessdata"
fi
exec "$native_dev_root/rust/target/debug/aria-native" "$@"
