"""doctor must describe the installer that ships, not the one that was deleted.

The npm package was rewritten from a postinstall that built a venv into a
zero-dependency launcher that execs a prebuilt binary, delivered as one
optionalDependency per platform. `npm/scripts/postinstall.js`, `npm/lib/venv.js`
and `npm/lib/paths.js` were deleted; the only script left in package.json is
`test`.

doctor's repair suggestions did not follow:

    "Repair with: node $(npm root -g)/aria-code/scripts/postinstall.js"
    "Run npm repair/update-engine or reinstall dependencies."
    "... bash install.sh --rebuild (repo checkout) or npm run repair (npm install)"

All three name things that no longer exist. A user who hit any of them was
sent to a dead end by the tool whose whole job is telling them what to do.

The new check is the one that matters for the current installer: npm skips an
optionalDependency it cannot resolve *silently* — that is what "optional"
means — so a launcher with no binary behind it reports nothing until the user
runs something. Every npm install to date is in that state, because the five
platform packages have never been published.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "src" / "aria_code"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aria_code import doctor  # noqa: E402

DOCTOR_SRC = (ROOT / "src" / "aria_code" / "doctor.py").read_text(encoding="utf-8")
NPM_PKG = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))


def _suggestions() -> list[str]:
    """Every repair string doctor can print, as literals in its source.

    Read from the source rather than by running the checks, because most
    suggestions only appear on a machine in the broken state they describe.
    """
    # Comment lines are stripped first: the comments that record *why* each
    # dead command was removed quote the command, and matching those would make
    # this test fail on its own explanation.
    code = "\n".join(line for line in DOCTOR_SRC.splitlines()
                     if not line.lstrip().startswith("#"))
    return re.findall(r'"([^"]{12,})"', code)


class NoSuggestionNamesSomethingThatWasDeleted(unittest.TestCase):
    # Each of these existed when the suggestion was written.
    GONE = ("postinstall.js", "npm repair", "update-engine", "npm run repair")

    def test_the_dead_commands_are_not_suggested(self) -> None:
        for dead in self.GONE:
            with self.subTest(command=dead):
                live = [s for s in _suggestions() if dead in s]
                self.assertEqual(
                    live, [],
                    f"doctor still tells users to run {dead!r}, which no longer "
                    f"exists: {live}",
                )

    def test_the_npm_package_really_has_no_repair_script(self) -> None:
        # The assertion above is only meaningful while this holds; if a repair
        # script comes back, the suggestions may legitimately return.
        scripts = set(NPM_PKG.get("scripts") or {})
        self.assertNotIn("repair", scripts)
        self.assertNotIn("update-engine", scripts)

    def test_the_deleted_launcher_files_are_still_deleted(self) -> None:
        for rel in ("npm/scripts/postinstall.js", "npm/lib/venv.js", "npm/lib/paths.js"):
            with self.subTest(path=rel):
                self.assertFalse((ROOT / rel).exists())


class ThePlatformKeyMatchesTheOnesWePublish(unittest.TestCase):
    """doctor's keys must agree with the packages the release actually builds."""

    CASES = {
        ("Darwin", "arm64"): "darwin-arm64",
        ("Darwin", "x86_64"): "darwin-x64",
        ("Linux", "x86_64"): "linux-x64",
        ("Linux", "aarch64"): "linux-arm64",
        ("Windows", "AMD64"): "win32-x64",
        # No binary is built for these; "" is the correct answer, and it routes
        # the user to pip rather than to a package that will never exist.
        ("Windows", "ARM64"): "",
        ("FreeBSD", "x86_64"): "",
        ("Linux", "riscv64"): "",
    }

    def test_each_platform_maps_as_published(self) -> None:
        for (system, machine), expected in self.CASES.items():
            with self.subTest(system=system, machine=machine):
                self.assertEqual(doctor.npm_platform_key(system, machine), expected)

    def test_the_keys_match_the_package_generator(self) -> None:
        gen = (ROOT / "go" / "buildtools" / "packages.go").read_text(encoding="utf-8")
        match = re.search(r"platformKeys = \[\]string\{([^}]*)\}", gen)
        self.assertIsNotNone(match, "packages.go has no platformKeys")
        published = set(re.findall(r'"([a-z0-9-]+)"', match.group(1)))
        produced = {v for v in self.CASES.values() if v}
        self.assertEqual(
            produced, published,
            "doctor reports on platform packages the release does not build, or "
            "misses ones it does",
        )

    def test_the_dispatcher_lists_exactly_those(self) -> None:
        names = set(NPM_PKG.get("optionalDependencies") or {})
        expected = {v for v in self.CASES.values() if v}
        cli = {f"@artheras/aria-code-{key}" for key in expected}
        mcp = {f"@artheras/aria-code-mcp-{key}" for key in expected}
        self.assertEqual(names, cli | mcp)


class TheBinaryCheckReportsASilentlyMissingPackage(unittest.TestCase):
    def test_an_unsupported_platform_skips_and_points_at_pip(self) -> None:
        original = doctor.npm_platform_key
        doctor.npm_platform_key = lambda *a, **k: ""  # type: ignore[assignment]
        try:
            check = doctor._platform_package_check("/usr/bin/npm")
        finally:
            doctor.npm_platform_key = original  # type: ignore[assignment]
        self.assertEqual(check.status, "skip")
        self.assertIn("pip", check.suggestion)

    def test_a_missing_package_is_an_error_not_a_warning(self) -> None:
        # npm's silence is the whole problem; reporting it quietly would
        # reproduce it.
        original = doctor._capture_cmd
        doctor._capture_cmd = lambda *a, **k: (0, "/nonexistent/node_modules")  # type: ignore[assignment]
        try:
            check = doctor._platform_package_check("/usr/bin/npm")
        finally:
            doctor._capture_cmd = original  # type: ignore[assignment]
        self.assertEqual(check.status, "err")
        self.assertIn("no binary", check.detail)
        self.assertIn("npm install -g", check.suggestion)


if __name__ == "__main__":
    unittest.main()
