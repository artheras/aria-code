#!/usr/bin/env python3
"""Bump the aria-code version in every place that hard-codes it, atomically.

The version lives in three files that must stay in lock-step, because each is a
separate publish source:

  pyproject.toml   version = "X"       -> PyPI  (uv publish reads this)
  npm/package.json "version": "X"      -> npm   (npm publish reads this)
  aria_code/_version.py  __version__ = "X"  -> `aria-code --version`

`publish.yml` triggers on a `vX.Y.Z` tag and publishes whatever these files say,
so a mismatch ships a wrong/duplicate version or makes --version lie. This script
is the single entry point: it sets all three plus the README PyPI badge and link,
verifies they agree, and prints the tag command. The companion check (`--check`)
is what CI runs to refuse a release
whose files disagree with the tag.

Usage:
  python scripts/bump_version.py 4.1.5     # set all three to 4.1.5
  python scripts/bump_version.py --check            # assert all three already agree
  python scripts/bump_version.py --check 4.1.5      # assert all three == 4.1.5 (CI: pass the tag)
  python scripts/bump_version.py --next-release     # print the next release version
  python scripts/bump_version.py --next-hotfix      # print the next hotfix version

Codex's scheme, measured from its 196 published stable versions rather than
assumed: major is always 0, minor carries the release count, and patch is only
used for a fix on top of a release that already shipped.

  0.157.0 -> 0.158.0 -> 0.159.0        one release each (--next-release)
  0.159.0 -> 0.159.1 -> 0.159.2        hotfixes on 0.159 (--next-hotfix)

A release is one merge, so minor is a counter, not a summary. Reaching 0.121.0
takes 121 releases; that is how Codex reached 0.159, and it is the point.

Major stays 0 deliberately. It is the signal Codex uses and it is honest: the
CLI surface still moves.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "npm" / "package.json"
# The single hand-written copy of the version. This path has been wrong twice.
# It pointed at the repo root after the src/ restructure, so `--check` died with
# FileNotFoundError — the first job publish.yml runs on a tag, so releases could
# not start at all and the error named a missing file rather than a stale path.
# Then the version moved out of aria_cli.py into aria_code/_version.py (so it
# can be read without importing the CLI) and the check reported "<missing>"
# instead. tests/test_version_consistency.py follows the same file.
VERSION_FILE = ROOT / "src" / "aria_code" / "_version.py"

# PyPI's project JSON still chooses legacy 4.4.2 over newer 0.x releases.
# Read the exact release JSON with a short-cache dynamic badge (the standard
# PyPI badge caches even a missing release for 12 hours). Keep its click target
# on that release too, so an unpublished version is shown as unavailable.
README_FILES = (ROOT / "README.md", ROOT / "README_CN.md")
README_VERSION_PATTERNS = {
    "PyPI badge": re.compile(r'(https%3A%2F%2Fpypi\.org%2Fpypi%2Faria-code%2F)([^%&"\s]+)'),
    "PyPI link": re.compile(r'(https://pypi\.org/project/aria-code/)([^/"\s]+)'),
}

SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$")


def read_versions() -> dict[str, str]:
    """Current version as recorded in each source. Anchored patterns so a
    dependency pin like `foo>=4.4.0` is never mistaken for the project version."""
    out: dict[str, str] = {}

    m = re.search(r'(?m)^version = "([^"]+)"', PYPROJECT.read_text())
    out["pyproject.toml"] = m.group(1) if m else "<missing>"

    m = re.search(r'(?m)^__version__ = "([^"]+)"', VERSION_FILE.read_text())
    out["aria_code/_version.py"] = m.group(1) if m else "<missing>"

    out["npm/package.json"] = json.loads(PACKAGE_JSON.read_text()).get("version", "<missing>")
    for readme in README_FILES:
        text = readme.read_text(encoding="utf-8")
        for label, pattern in README_VERSION_PATTERNS.items():
            match = pattern.search(text)
            out[f"{readme.name} {label}"] = match.group(2) if match else "<missing>"
    return out


def write_version(new: str) -> None:
    PYPROJECT.write_text(
        re.sub(r'(?m)^version = "[^"]+"', f'version = "{new}"', PYPROJECT.read_text(), count=1)
    )
    VERSION_FILE.write_text(
        re.sub(r'(?m)^__version__ = "[^"]+"', f'__version__ = "{new}"', VERSION_FILE.read_text(), count=1)
    )
    # Swap only the top-level "version" value in package.json — a regex line edit,
    # not a JSON round-trip, so the file's hand-formatting (compact arrays, inline
    # objects) is preserved. count=1 hits the first (top-level) version key.
    text = re.sub(r'(?m)^(\s*"version":\s*")[^"]+(")', rf"\g<1>{new}\g<2>",
                  PACKAGE_JSON.read_text(), count=1)
    # The dispatcher pins each per-platform binary package, and those are built
    # and published from this same release, so the pins have to move with it.
    # They did not: at 0.47.0 the file still pinned 4.4.1 — versions from the
    # 4.x line that this release does not produce. npm would then either fetch
    # a 4.4.1 binary under a 0.47.0 launcher, or, if the pinned version is
    # absent, skip the optionalDependency *silently* and leave the user a
    # launcher with no binary. That silent skip is the exact failure
    # npm/lib/platform.js exists to report.
    text = re.sub(r'("@artheras/aria-code-[a-z0-9-]+":\s*")[^"]+(")',
                  rf"\g<1>{new}\g<2>", text)
    PACKAGE_JSON.write_text(text)
    for readme in README_FILES:
        text = readme.read_text(encoding="utf-8")
        for pattern in README_VERSION_PATTERNS.values():
            text = pattern.sub(lambda match: match.group(1) + new, text, count=1)
        readme.write_text(text, encoding="utf-8")


def cmd_check(expected: str | None) -> int:
    versions = read_versions()
    distinct = set(versions.values())
    ok = len(distinct) == 1 and "<missing>" not in distinct
    if expected is not None:
        ok = ok and distinct == {expected.lstrip("v")}

    for f, v in versions.items():
        print(f"  {v:<12} {f}")
    if expected is not None:
        print(f"  expected: {expected.lstrip('v')}")

    if ok:
        print("✓ versions aligned")
        return 0
    print("✗ version mismatch — run: python scripts/bump_version.py <version>", file=sys.stderr)
    return 1


def cmd_bump(new: str) -> int:
    new = new.lstrip("v")
    if not SEMVER.match(new):
        print(f"✗ not a valid version: {new!r} (expected X.Y.Z)", file=sys.stderr)
        return 2
    before = read_versions()
    write_version(new)
    after = read_versions()
    for f in after:
        print(f"  {before[f]:>10} → {after[f]:<10} {f}")
    print(f"\n✓ bumped to {new}. Next:")
    print(f"    git commit -am 'release: v{new}'")
    print(f"    git tag v{new} && git push --tags    # triggers publish.yml")
    return 0


def _triple(version: str):
    """(major, minor, patch) as ints, or None if it is not a plain X.Y.Z."""
    parts = version.split(".")
    if len(parts) < 3:
        return None
    head = parts[2].split("-")[0].split("+")[0]
    if not (parts[0].isdigit() and parts[1].isdigit() and head.isdigit()):
        return None
    return int(parts[0]), int(parts[1]), int(head)


def _highest_tag_triple():
    """The highest vX.Y.Z tag in this checkout, or None."""
    import subprocess

    proc = subprocess.run(["git", "tag", "--list", "v*"],
                          capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        return None
    triples = [t for t in (_triple(name[1:]) for name in proc.stdout.split()) if t]
    return max(triples) if triples else None


# Where the 0.x counter starts, for the one case where "next" cannot mean "one
# more than what exists": crossing out of the old 4.x line, which is numerically
# higher than everything that follows it.
#
# 45 rather than 1 because minor is a release counter and this project has
# already shipped: 44 pull requests were merged before the "one merge, one
# release" rule existed, so 44 is the number of releases it would have had under
# it. Codex reached 0.159 by shipping 196 times — the number means "how many",
# nothing else, which is exactly why starting it at an invented figure would say
# something untrue.
#
# Only ever raise this, and only with a basis. The counter must stay above every
# tag that exists.
FIRST_ZEROX_MINOR = 45


def _zerox(triple) -> bool:
    return triple is not None and triple[0] == 0


def _current() -> tuple:
    current = read_versions()["pyproject.toml"]
    triple = _triple(current)
    if triple is None:
        raise SystemExit(f"cannot parse the current version {current!r}")
    return triple


def _base_for_next() -> tuple:
    """The version to count from: the files, or the highest tag if it is ahead.

    Only ever compares within the same major line. Once the files say 0.x, a
    4.4.x tag is history and not a number to count from — otherwise the first
    0.x release would try to be 4.4.6 again.
    """
    from_file = _current()
    from_tag = _highest_tag_triple()
    if from_tag and from_tag[0] == from_file[0] and from_tag > from_file:
        print(f"note: highest tag v{'.'.join(map(str, from_tag))} is ahead of "
              f"pyproject {'.'.join(map(str, from_file))}; counting from the tag",
              file=sys.stderr)
        return from_tag
    return from_file


def cmd_next_release() -> int:
    """Print the next release version: minor + 1, patch back to 0.

    Codex's shape, and the reason patch resets: patch means "a fix on top of a
    release that already shipped", so carrying it forward would say something
    untrue about the new release.
    """
    base = _base_for_next()
    if not _zerox(base):
        # Crossing from 4.x into the 0.x line. There is nothing to increment —
        # 0.x is lower than every 4.x, so the first one is stated, not derived.
        print(f"note: leaving the {base[0]}.x line; starting 0.x at "
              f"0.{FIRST_ZEROX_MINOR}.0", file=sys.stderr)
        print(f"0.{FIRST_ZEROX_MINOR}.0")
        return 0
    print(f"0.{base[1] + 1}.0")
    return 0


def cmd_next_hotfix() -> int:
    """Print the next hotfix version: patch + 1 on the current minor.

    For a fix that has to go out on top of the release that just shipped, which
    is what Codex's 0.159.1 and 0.159.2 are. Deliberately separate from
    --next-release so the automation cannot produce one by accident.
    """
    base = _base_for_next()
    if not _zerox(base):
        print(f"refusing to hotfix the {base[0]}.x line; cut a 0.x release first",
              file=sys.stderr)
        return 1
    print(f"0.{base[1]}.{base[2] + 1}")
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--next-release":
        return cmd_next_release()
    if args and args[0] == "--next-hotfix":
        return cmd_next_hotfix()
    if args and args[0] == "--check":
        return cmd_check(args[1] if len(args) > 1 else None)
    if len(args) == 1:
        return cmd_bump(args[0])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
