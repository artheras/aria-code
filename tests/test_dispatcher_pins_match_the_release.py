"""The dispatcher must pin the platform packages at its own version.

npm/package.json lists each per-platform binary package as an
optionalDependency. Those packages are built and published from the same
release, at the same version, so the pins have to move with it.

They did not. bump_version.py rewrote only the top-level "version" key, so at
0.47.0 the file still pinned 4.4.1 — versions from the 4.x line that the 0.x
releases do not produce. Either outcome is bad and neither is loud:

  - 4.4.1 still exists on npm, so a user gets a 4.4.1 binary under a 0.47.0
    launcher; or
  - the pinned version is absent, and npm skips an unresolvable
    optionalDependency *silently*, leaving a launcher with no binary.

That silent skip is the precise failure npm/lib/platform.js was written to
report, so it would have been reported — as "no binary for your platform" on a
release that built binaries for every platform.

Checked against the file rather than against a literal version, so this stays
true for every release.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "npm" / "package.json"
BUMP = ROOT / "scripts" / "bump_version.py"

# Kept in step with scripts/make_platform_packages.py PLATFORM_KEYS.
PLATFORM_KEYS = ("darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64", "win32-x64")


def _manifest() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


class ThePinsMatchTheDispatchersOwnVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _manifest()

    def test_every_platform_is_listed(self) -> None:
        listed = set(self.manifest.get("optionalDependencies") or {})
        expected = {f"@artheras/aria-code-{k}" for k in PLATFORM_KEYS}
        expected |= {f"@artheras/aria-code-mcp-{k}" for k in PLATFORM_KEYS}
        self.assertEqual(
            listed, expected,
            "the dispatcher must list exactly the platform packages the release "
            "builds; an unlisted one is never installed, and a listed one that "
            "is never built is skipped silently",
        )

    def test_every_pin_equals_the_dispatchers_version(self) -> None:
        version = self.manifest["version"]
        for name, pin in (self.manifest.get("optionalDependencies") or {}).items():
            with self.subTest(package=name):
                self.assertEqual(
                    pin, version,
                    f"{name} is pinned at {pin} while the dispatcher is {version}; "
                    f"they are published together and must match",
                )


class BumpingTheVersionMovesThePins(unittest.TestCase):
    """The guard above is only durable if the bump keeps it true by itself."""

    def test_bump_version_rewrites_the_pins_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            # A minimal tree with the version sources and release badges.
            (work / "npm").mkdir()
            (work / "src" / "aria_code").mkdir(parents=True)
            (work / "scripts").mkdir()
            (work / "pyproject.toml").write_text('[project]\nversion = "0.47.0"\n')
            (work / "src" / "aria_code" / "_version.py").write_text(
                '__version__ = "0.47.0"\n')
            (work / "npm" / "package.json").write_text(json.dumps({
                "name": "@artheras/aria-code",
                "version": "0.47.0",
                "optionalDependencies": {
                    **{f"@artheras/aria-code-{k}": "0.47.0" for k in PLATFORM_KEYS},
                    **{f"@artheras/aria-code-mcp-{k}": "0.47.0" for k in PLATFORM_KEYS},
                },
            }, indent=2) + "\n")
            (work / "scripts" / "bump_version.py").write_text(
                BUMP.read_text(encoding="utf-8"), encoding="utf-8")
            for name in ("README.md", "README_CN.md"):
                (work / name).write_text(
                    (ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(work / "scripts" / "bump_version.py"), "0.48.0"],
                capture_output=True, text=True, cwd=work, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])

            bumped = json.loads((work / "npm" / "package.json").read_text())
            self.assertEqual(bumped["version"], "0.48.0")
            for name, pin in bumped["optionalDependencies"].items():
                with self.subTest(package=name):
                    self.assertEqual(
                        pin, "0.48.0",
                        f"{name} was left at {pin} after bumping to 0.48.0",
                    )

    def test_the_rewrite_does_not_touch_unrelated_dependencies(self) -> None:
        # The substitution is scoped to @artheras/aria-code-* so that a real
        # third-party dependency is not rewritten to the project's version.
        pattern = re.compile(r'("@artheras/aria-code-[a-z0-9-]+":\s*")[^"]+(")')
        sample = '{"deps": {"chalk": "^5.0.0", "@artheras/aria-code-linux-x64": "1.0.0"}}'
        out = pattern.sub(r"\g<1>9.9.9\g<2>", sample)
        self.assertIn('"chalk": "^5.0.0"', out)
        self.assertIn('"@artheras/aria-code-linux-x64": "9.9.9"', out)


if __name__ == "__main__":
    unittest.main()
