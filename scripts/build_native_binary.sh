#!/usr/bin/env bash
# scripts/build_native_binary.sh — build, sign, and (optionally) notarize the
# standalone aria-code CLI and MCP server via PyInstaller --onedir, then archive
# each as dist-native/release/<name>.tar.gz for the release.
#
# --onedir, not --onefile: --onefile unpacked ~400 native libraries to a new
# temp directory on every launch and macOS re-scanned each one, so every
# command took ~90 s. See scripts/package_onedir.py.
#
# Why this exists: npm install / pip install both fetch or build a separate
# Python runtime on the user's machine, which is what produced the whole
# class of Windows install bugs fixed in 4.1.7 (readline, os.uname(),
# console encoding — all only reachable because the runtime is assembled
# live on the user's box). A self-contained signed+notarized binary sidesteps
# that entire failure class, the same way Claude Code's install.sh does.
#
# Usage:
#   bash scripts/build_native_binary.sh              # build + sign only
#   bash scripts/build_native_binary.sh --notarize    # + submit for notarization
#
# Notarization credentials (only needed with --notarize) — set ONE of:
#   App Store Connect API key (recommended, no password ever typed):
#     APPLE_API_KEY=/absolute/path/AuthKey_XXXXXXXXXX.p8
#     APPLE_API_KEY_ID=XXXXXXXXXX
#     APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   or Apple ID + app-specific password, stored once via a keychain profile
#   so the password is never on the command line or in shell history:
#     xcrun notarytool store-credentials "aria-notary" \
#       --apple-id you@example.com --team-id 2HJXDCWWKX --password xxxx-xxxx-xxxx-xxxx
#     export NOTARY_KEYCHAIN_PROFILE=aria-notary

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script builds the macOS binary. Windows/Linux need their own build job." >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Xindi Wang (2HJXDCWWKX)}"
BUILD_DIR="${BUILD_DIR:-$PROJECT_ROOT/dist-native}"
VENV_DIR="$BUILD_DIR/.build-venv"
BIN_NAME="aria-code-bin"
# Each build is a directory holding the executable of the same name.
BIN_DIR="$BUILD_DIR/dist/$BIN_NAME"
BIN_PATH="$BIN_DIR/$BIN_NAME"
MCP_BIN_NAME="aria-code-mcp-bin"
MCP_BIN_DIR="$BUILD_DIR/dist/$MCP_BIN_NAME"
MCP_BIN_PATH="$MCP_BIN_DIR/$MCP_BIN_NAME"
RELEASE_DIR="$BUILD_DIR/release"
ENTITLEMENTS="$BUILD_DIR/entitlements.plist"

echo "── Finding a Python within pyproject.toml's requires-python bound ──"
# Bare `python3` isn't safe to assume: it resolves to whatever is first on
# PATH, and pyproject.toml caps at <3.14 (numba, a pandas_ta dependency,
# hard-refuses to build on 3.14 — see the requires-python comment). Search
# newest-first so the freeze picks up current interpreter features.
BUILD_PYTHON=""
for cand in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    BUILD_PYTHON="$cand"
    break
  fi
done
if [[ -z "$BUILD_PYTHON" ]]; then
  echo "No Python 3.10-3.13 found on PATH (pyproject.toml requires-python is >=3.10,<3.14)." >&2
  echo "Install one, e.g.: brew install python@3.13" >&2
  exit 1
fi
echo "  using $BUILD_PYTHON ($("$BUILD_PYTHON" --version))"

echo "── Building isolated venv for the freeze (keeps project .venv untouched) ──"
rm -rf "$VENV_DIR"
"$BUILD_PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet -e "$PROJECT_ROOT" pyinstaller

echo "── Running PyInstaller (--onedir) ──"
"$VENV_DIR/bin/pyinstaller" --noconfirm --onedir --name "$BIN_NAME" \
  --distpath "$BUILD_DIR/dist" \
  --workpath "$BUILD_DIR/build" \
  --specpath "$BUILD_DIR" \
  --paths "$PROJECT_ROOT/src/aria_code" \
  --paths "$PROJECT_ROOT/src" \
  $("$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/pyinstaller_collect_args.py") \
  --collect-all rich \
  --collect-all prompt_toolkit \
  "$PROJECT_ROOT/src/aria_code/aria_cli.py"

# The Rust project-graph indexer goes inside the CLI build's _internal/, so
# the signing loop below signs it like every other Mach-O there.
# SKIP_NATIVE_INDEXER=1 builds without it on a machine with no Rust toolchain.
if [[ "${SKIP_NATIVE_INDEXER:-}" != "1" ]]; then
  echo "── Bundling the native indexer (aria-native) ──"
  (cd "$PROJECT_ROOT/rust" && rustup show >/dev/null)   # the toolchain rust-toolchain.toml pins
  "$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/bundle_native_indexer.py" "$BIN_DIR"
fi

echo "── Running PyInstaller for the MCP server binary (--onedir) ──"
# Separate entry point, separate binary: the MCP server (packages/aria_mcp/
# server.py) has to be launchable on its own (`claude mcp add aria-code --
# /path/to/aria-code-mcp-bin`) — a Claude Code/Codex/Cursor user shouldn't
# need a full Python environment just to register aria-code as an MCP
# server, the same rationale that motivated bundling aria_cli.py at all.
# --copy-metadata aria-code: confirmed empirically — without this,
# `aria-code-mcp-bin --version` (server.py's smoke-test flag) prints
# "unknown" instead of the real version, because importlib.metadata.version()
# can't find the package's dist-info inside a frozen PyInstaller app unless
# it's explicitly copied in.
"$VENV_DIR/bin/pyinstaller" --noconfirm --onedir --name "$MCP_BIN_NAME" \
  --distpath "$BUILD_DIR/dist" \
  --workpath "$BUILD_DIR/build" \
  --specpath "$BUILD_DIR" \
  --paths "$PROJECT_ROOT/src/aria_code" \
  --paths "$PROJECT_ROOT/src" \
  $("$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/pyinstaller_collect_args.py") \
  --copy-metadata aria-code \
  "$PROJECT_ROOT/src/aria_code/aria_mcp_server.py"

# Both binaries (CLI + MCP server) go through the same sign/notarize/verify
# pipeline below — iterate rather than duplicate the whole block per binary.
BIN_NAMES=("$BIN_NAME" "$MCP_BIN_NAME")
BIN_PATHS=("$BIN_PATH" "$MCP_BIN_PATH")
BIN_DIRS=("$BIN_DIR" "$MCP_BIN_DIR")

# The release ships each directory as one archive: upload-artifact would drop
# the build's symlinks and execute bits. Called on every exit path below, so an
# unsigned CI build is archived exactly like a signed one.
archive_builds() {
  rm -rf "$RELEASE_DIR"
  for d in "${BIN_DIRS[@]}"; do
    "$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/package_onedir.py" pack "$d" "$RELEASE_DIR"
  done
}

echo "── Checking for signing identity in keychain ──"
# CI runners (and any machine without the real Developer ID cert imported)
# don't have $SIGN_IDENTITY available — that's expected there, not an error.
# Skip signing/notarization gracefully (produce unsigned binaries + a clear
# note) instead of hard-failing, mirroring how publish.yml skips PyPI
# publish when PYPI_API_TOKEN isn't set rather than failing the whole run.
if ! security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_IDENTITY"; then
  echo "── Smoke test: the unsigned binaries must actually run ──"
  for p in "${BIN_PATHS[@]}"; do "$p" --version; done
  archive_builds
  echo ""
  echo "Signing identity '$SIGN_IDENTITY' not found in keychain — built unsigned: ${BIN_PATHS[*]}"
  echo "This machine can't sign/notarize (no cert imported). Gatekeeper will reject"
  echo "these binaries on end-user machines — only distribute a signed+notarized build"
  echo "(run this script on a machine with the real Developer ID cert imported)."
  exit 0
fi

# disable-library-validation: hardened runtime refuses to load a library
# whose Team ID differs from the executable's. Every Mach-O in _internal/ is
# re-signed with our identity below, but Python extension modules can still
# dlopen() third-party libraries at runtime, and the --onefile build failed to
# launch at all without this ("different Team IDs"). Kept rather than re-proven.
cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
</dict>
</plist>
PLIST

for i in "${!BIN_PATHS[@]}"; do
  p="${BIN_PATHS[$i]}"
  d="${BIN_DIRS[$i]}"
  # Inside out: every Mach-O under _internal/ (notarization rejects an
  # unsigned or ad-hoc-signed nested binary), then the executable. Symlinks are
  # skipped — signing one signs its target, which find visits on its own.
  echo "── Signing the libraries in $d/_internal ──"
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q 'Mach-O'; then
      codesign --force --sign "$SIGN_IDENTITY" --options runtime --timestamp "$f"
    fi
  done < <(find "$d/_internal" -type f -print0)

  echo "── Signing $p with $SIGN_IDENTITY ──"
  codesign --force --sign "$SIGN_IDENTITY" --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" "$p"

  echo "── Verifying signature ──"
  codesign --verify --strict --verbose=2 "$p"

  echo "── Smoke test: the signed binary must actually run ──"
  "$p" --version
done

if [[ "${1:-}" != "--notarize" ]]; then
  archive_builds
  echo ""
  echo "Signed (not notarized): ${BIN_PATHS[*]}"
  echo "Gatekeeper will currently reject them (spctl -a -vvv -t exec <path>)."
  echo "Re-run with --notarize once Apple credentials are set (see header of this script)."
  exit 0
fi

has_api_creds() {
  [[ -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER:-}" ]]
}
has_keychain_profile() {
  [[ -n "${NOTARY_KEYCHAIN_PROFILE:-}" ]]
}

if ! has_api_creds && ! has_keychain_profile; then
  cat >&2 <<'EOF'

No notarization credentials found. Set one of:

  App Store Connect API key:
    APPLE_API_KEY=/absolute/path/AuthKey_XXXXXXXXXX.p8
    APPLE_API_KEY_ID=XXXXXXXXXX
    APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

  Apple ID app-specific password, stored once (password never touches the
  command line or shell history again after this one-time setup):
    xcrun notarytool store-credentials "aria-notary" \
      --apple-id you@example.com --team-id 2HJXDCWWKX --password xxxx-xxxx-xxxx-xxxx
    export NOTARY_KEYCHAIN_PROFILE=aria-notary
EOF
  exit 1
fi

for i in "${!BIN_PATHS[@]}"; do
  p="${BIN_PATHS[$i]}"
  n="${BIN_NAMES[$i]}"
  d="${BIN_DIRS[$i]}"

  echo "── Zipping the $n directory for submission ──"
  NOTARY_ZIP="$BUILD_DIR/${n}-notarize.zip"
  ditto -c -k --keepParent "$d" "$NOTARY_ZIP"

  echo "── Submitting $n to Apple notarization (this polls and can take a few minutes) ──"
  if has_api_creds; then
    xcrun notarytool submit "$NOTARY_ZIP" \
      --key "$APPLE_API_KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER" --wait
  else
    xcrun notarytool submit "$NOTARY_ZIP" --keychain-profile "$NOTARY_KEYCHAIN_PROFILE" --wait
  fi

  # Stapling only works on .app/.pkg/.dmg containers — there is no resource
  # fork slot on a bare Mach-O executable to attach a ticket to. A notarized
  # bare binary relies on Gatekeeper's online check instead, which happens at
  # actual process-launch time via syspolicyd — NOT via `spctl -a -t exec`.
  # Confirmed empirically: `spctl -a -t exec` on a bare (non-.app) binary
  # reliably answers "rejected (the code is valid but does not seem to be an
  # app)" or "Unnotarized Developer ID" regardless of notarization status —
  # it's simply the wrong assessment type for a raw Mach-O CLI tool, so it is
  # NOT used here. The real test is what actually happens to a user's
  # downloaded copy: quarantine it (simulating a browser/curl download) and
  # execute it directly. A rejected binary throws an unrecoverable
  # "cannot be opened because Apple cannot verify..." error at exec time;
  # an accepted one just runs.
  echo ""
  echo "── Verifying Gatekeeper acceptance for $n (real test: execute a quarantined copy, not spctl -t exec) ──"
  GATEKEEPER_TEST_COPY="$BUILD_DIR/gatekeeper-test-copy-$n"
  rm -rf "$GATEKEEPER_TEST_COPY"
  cp -R "$d" "$GATEKEEPER_TEST_COPY"
  # Every file, as a browser download extracted by Archive Utility would be.
  find "$GATEKEEPER_TEST_COPY" -type f -exec \
    xattr -w com.apple.quarantine "0181;$(printf '%x' "$(date +%s)");Safari;" {} +
  if ! "$GATEKEEPER_TEST_COPY/$n" --version >/dev/null 2>&1; then
    echo "Gatekeeper rejected the quarantined $n binary at launch — notarization did not take effect." >&2
    rm -rf "$GATEKEEPER_TEST_COPY"
    exit 1
  fi
  rm -rf "$GATEKEEPER_TEST_COPY"
  echo "  quarantined copy launched cleanly — Gatekeeper accepts $n."
done

archive_builds
echo ""
echo "Signed + notarized: ${BIN_PATHS[*]}"
