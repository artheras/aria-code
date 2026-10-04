#!/usr/bin/env bash
# Make a staged release visible only after installers and registries are ready.
set -euo pipefail

tag="${1:?usage: finalize_release.sh vX.Y.Z}"
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid release tag: $tag" >&2; exit 1; }
version="${tag#v}"
repo="${GITHUB_REPOSITORY:-artheras/aria-code}"

for command in gh npm node curl; do
  command -v "$command" >/dev/null 2>&1 || { echo "$command is required" >&2; exit 1; }
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
gh release download "$tag" --repo "$repo" --pattern SHA256SUMS --dir "$tmp_dir"
manifest="$tmp_dir/SHA256SUMS"

assets=(
  aria-code-macos-arm64.tar.gz aria-code-macos-x64.tar.gz
  aria-code-linux-arm64.tar.gz aria-code-linux-x64.tar.gz
  aria-code-windows-x64.zip
  aria-code-mcp-macos-arm64.tar.gz aria-code-mcp-macos-x64.tar.gz
  aria-code-mcp-linux-arm64.tar.gz aria-code-mcp-linux-x64.tar.gz
  aria-code-mcp-windows-x64.zip
)
release_assets="$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[].name')"
for asset in "${assets[@]}"; do
  digest="$(awk -v name="$asset" '$2 == name { print $1 }' "$manifest")"
  [[ "$digest" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "Release $tag has no valid SHA-256 entry for $asset" >&2
    exit 1
  }
  if ! grep -Fxq "$asset" <<< "$release_assets"; then
    echo "Release $tag is missing $asset" >&2
    exit 1
  fi
done

packages="$(node -e '
const manifest = require("./npm/package.json");
const entries = Object.entries(manifest.optionalDependencies || {});
if (manifest.version !== process.argv[1] || entries.length !== 10 ||
    entries.some(([name, version]) =>
      !name.startsWith("@artheras/aria-code-") || version !== manifest.version)) {
  console.error("Dispatcher version or platform pins do not match the tag");
  process.exit(1);
}
for (const [name, version] of entries) console.log(`${name}@${version}`);
' "$version")"
while IFS= read -r package; do
  npm view "$package" version --registry=https://registry.npmjs.org/ >/dev/null || {
    echo "npm has not published $package" >&2
    exit 1
  }
done <<< "$packages"
npm view "@artheras/aria-code@$version" version --registry=https://registry.npmjs.org/ >/dev/null || {
  echo "npm has not published the dispatcher at $version" >&2
  exit 1
}
curl -fsS --connect-timeout 10 --max-time 30 \
  "https://pypi.org/pypi/aria-code/$version/json" -o /dev/null || {
  echo "PyPI has not published aria-code $version" >&2
  exit 1
}

if [[ "$(gh release view "$tag" --repo "$repo" --json isDraft --jq '.isDraft')" == true ]]; then
  gh release edit "$tag" --repo "$repo" --draft=false
  echo "Published GitHub Release $tag after verifying 10 archives, checksums, npm and PyPI."
else
  echo "GitHub Release $tag is already public; all release artifacts verified."
fi
