#!/bin/sh
# Install the standalone Aria Code CLI without Python, Node.js, or npm.
set -eu

fail() { printf 'Aria Code install: %s\n' "$*" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || fail 'curl is required'

case "$(uname -s)" in
  Darwin) platform=macos ;;
  Linux) platform=linux ;;
  *) fail 'this installer supports macOS and Linux; use install.ps1 on Windows' ;;
esac
case "$(uname -m)" in
  arm64|aarch64) arch=arm64 ;;
  x86_64|amd64) arch=x64 ;;
  *) fail "unsupported CPU architecture: $(uname -m)" ;;
esac

version=${ARIA_CODE_VERSION:-latest}
if [ "$version" = latest ]; then
  release_url=https://github.com/artheras/aria-code/releases/latest/download
else
  printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail 'ARIA_CODE_VERSION must look like v0.55.0'
  release_url="https://github.com/artheras/aria-code/releases/download/$version"
fi

asset="aria-code-$platform-$arch"
install_dir=${ARIA_CODE_INSTALL_DIR:-"$HOME/.local/bin"}
# Releases ship a PyInstaller --onedir build: the executable plus the
# libraries beside it. Install immutable directories and switch one symlink;
# a failed replacement must leave the previous version runnable.
# (--onefile re-unpacked ~400 libraries on every launch, which macOS re-scanned
# each time: ~90 s per command. The first launch after install is still slow
# while macOS scans them once.)
app_root=${ARIA_CODE_HOME:-"$HOME/.local/share/aria-code"}
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/aria-code-install.XXXXXX") || fail 'could not create a temporary directory'
trap 'rm -rf "$tmp_dir"; if [ -n "${staged:-}" ]; then rm -rf "$staged"; fi' EXIT HUP INT TERM

curl -fLsS --retry 3 --connect-timeout 10 -o "$tmp_dir/SHA256SUMS" "$release_url/SHA256SUMS" || fail 'could not download release checksums'

# Releases before the --onedir switch have a single-file binary instead.
if awk -v name="$asset.tar.gz" '$2 == name { found = 1 } END { exit !found }' "$tmp_dir/SHA256SUMS"; then
  file="$asset.tar.gz"
elif awk -v name="$asset" '$2 == name { found = 1 } END { exit !found }' "$tmp_dir/SHA256SUMS"; then
  file="$asset"
else
  fail "this release has no build for $platform-$arch"
fi

printf 'Downloading %s...\n' "$file"
curl -fLsS --retry 3 --connect-timeout 10 -o "$tmp_dir/$file" "$release_url/$file" || fail "could not download $file"

expected=$(awk -v name="$file" '$2 == name { print $1 }' "$tmp_dir/SHA256SUMS")
[ -n "$expected" ] || fail "checksum for $file is missing from this release"
case "$expected" in *[!0123456789abcdefABCDEF]*|'') fail 'invalid SHA-256 checksum' ;; esac
[ "${#expected}" -eq 64 ] || fail 'invalid SHA-256 checksum length'
if command -v shasum >/dev/null 2>&1; then
  actual=$(shasum -a 256 "$tmp_dir/$file" | awk '{ print $1 }')
elif command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$tmp_dir/$file" | awk '{ print $1 }')
else
  fail 'a SHA-256 checker (shasum or sha256sum) is required'
fi
[ "$actual" = "$expected" ] || fail "checksum mismatch for $file"

mkdir -p "$install_dir"
if [ "$file" = "$asset" ]; then
  chmod 755 "$tmp_dir/$asset"
  "$tmp_dir/$asset" --version >/dev/null || fail 'downloaded binary failed its version check'
  cp "$tmp_dir/$asset" "$install_dir/.aria-code-new-$$"
  chmod 755 "$install_dir/.aria-code-new-$$"
  mv -f "$install_dir/.aria-code-new-$$" "$install_dir/aria-code"
else
  command -v tar >/dev/null 2>&1 || fail 'tar is required'
  mkdir -p "$tmp_dir/unpacked"
  tar -xzf "$tmp_dir/$file" -C "$tmp_dir/unpacked" || fail "could not unpack $file"
  [ -x "$tmp_dir/unpacked/aria-code-bin/aria-code-bin" ] || fail "$file does not contain aria-code-bin/aria-code-bin"
  printf 'Checking the build (the first launch is slow while macOS scans it)...\n'
  "$tmp_dir/unpacked/aria-code-bin/aria-code-bin" --version >/dev/null || fail 'downloaded binary failed its version check'

  mkdir -p "$app_root/releases"
  app_root=$(cd "$app_root" && pwd -P)
  staged=$(mktemp -d "$app_root/releases/.staging.XXXXXX") || fail "could not stage in $app_root"
  cp -R "$tmp_dir/unpacked/aria-code-bin" "$staged/aria-code-bin" \
    || fail "could not write to $app_root"
  [ -x "$staged/aria-code-bin/aria-code-bin" ] || fail "staged build is incomplete"
  # The staged directory is on the same filesystem as the release directory.
  # Keep the old directory so a running process and a rollback both work.
  installed="$app_root/releases/$expected-$$"
  mv "$staged" "$installed" || fail "could not activate the staged build"

  # Replaces an old single-file install or an older onedir symlink atomically.
  ln -s "$installed/aria-code-bin/aria-code-bin" "$install_dir/.aria-code-new-$$"
  mv -f "$install_dir/.aria-code-new-$$" "$install_dir/aria-code"
fi

cat > "$tmp_dir/aria" <<'EOF'
#!/bin/sh
if [ "${1-}" = code ]; then shift; fi
exec "$(dirname "$0")/aria-code" "$@"
EOF
chmod 755 "$tmp_dir/aria"
cp "$tmp_dir/aria" "$install_dir/.aria-new-$$"
mv -f "$install_dir/.aria-new-$$" "$install_dir/aria"

if [ -z "${ARIA_CODE_INSTALL_DIR:-}" ]; then
  case "${SHELL:-}" in
    */zsh) profile="$HOME/.zprofile" ;;
    */bash) profile="$HOME/.bash_profile" ;;
    *) profile="$HOME/.profile" ;;
  esac
  path_line='export PATH="$HOME/.local/bin:$PATH"'
  if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
    printf '\n%s\n' "$path_line" >> "$profile"
    printf 'Added ~/.local/bin to %s for future shells.\n' "$profile"
  fi
fi

printf 'Installed %s\n' "$install_dir/aria-code"
printf 'Run it now: %s or %s code\n' "$install_dir/aria-code" "$install_dir/aria"
