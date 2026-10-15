#!/bin/sh
# Install aria-code from its GitHub Releases.
#
# Both Claude Code and Codex lead with a one-line installer, and both fall
# back to GitHub Releases; aria-code publishes there already, so that is this
# script's only source — no CDN to go stale, and nothing to configure.
#
# What it will not do: pick an archive by guessing. If the platform is not one
# we publish, it says which ones exist rather than installing something that
# cannot run.
set -eu

REPO="artheras/aria-code"
INSTALL_DIR="${ARIA_INSTALL_DIR:-$HOME/.local/bin}"
# The bundle is ~100 MB and must stay together; only the launcher goes on PATH.
LIB_DIR="${ARIA_LIB_DIR:-$HOME/.local/share/aria-code}"
VERSION="${ARIA_VERSION:-latest}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required but not installed."; }
need curl
need tar

case "$(uname -s)" in
  Darwin) os=macos ;;
  Linux)  os=linux ;;
  *) fail "Unsupported system: $(uname -s). The releases page has macOS, Linux and Windows builds: https://github.com/$REPO/releases" ;;
esac

case "$(uname -m)" in
  arm64|aarch64) arch=arm64 ;;
  x86_64|amd64)  arch=x64 ;;
  *) fail "Unsupported architecture: $(uname -m). Published builds are arm64 and x64." ;;
esac

asset="aria-code-${os}-${arch}.tar.gz"

if [ "$VERSION" = latest ]; then
  url="https://github.com/$REPO/releases/latest/download/$asset"
else
  url="https://github.com/$REPO/releases/download/${VERSION}/$asset"
fi

tmp="$(mktemp -d)"
# Keep a failed install from leaving a part-written binary behind.
trap 'rm -rf "$tmp"' EXIT INT TERM

say "Downloading $asset ($VERSION)…"
curl -fsSL --proto '=https' --tlsv1.2 "$url" -o "$tmp/$asset" \
  || fail "Could not download $url
Check the version exists: https://github.com/$REPO/releases"

say "Extracting…"
tar -xzf "$tmp/$asset" -C "$tmp" || fail "The archive could not be extracted; the download may be incomplete."

# The release is a PyInstaller onedir bundle: an executable beside an
# _internal/ tree it loads at runtime. Copying the executable out on its own
# produces something that cannot start, so the whole directory is installed
# and a small launcher on PATH execs into it.
bundle="$tmp/aria-code-bin"
[ -x "$bundle/aria-code-bin" ] || fail "$asset does not contain the expected aria-code-bin bundle."

mkdir -p "$LIB_DIR"
# Replace the previous install as one step: a half-copied bundle on disk is
# an aria-code that starts and then fails to find its own files.
rm -rf "$LIB_DIR/aria-code-bin.incoming"
cp -R "$bundle" "$LIB_DIR/aria-code-bin.incoming"
rm -rf "$LIB_DIR/aria-code-bin"
mv "$LIB_DIR/aria-code-bin.incoming" "$LIB_DIR/aria-code-bin"

mkdir -p "$INSTALL_DIR"
cat > "$INSTALL_DIR/aria-code.incoming" <<LAUNCHER
#!/bin/sh
exec "$LIB_DIR/aria-code-bin/aria-code-bin" "\$@"
LAUNCHER
chmod +x "$INSTALL_DIR/aria-code.incoming"
mv "$INSTALL_DIR/aria-code.incoming" "$INSTALL_DIR/aria-code"

say "Installed to $INSTALL_DIR/aria-code"
say "Runtime in $LIB_DIR/aria-code-bin"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) say "Run: aria-code" ;;
  *)
    say ""
    say "$INSTALL_DIR is not on your PATH. Add it:"
    say "  echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.profile && . ~/.profile"
    say "Or run it directly: $INSTALL_DIR/aria-code"
    ;;
esac
