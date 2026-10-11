"""Every file the binary build hands PyInstaller must exist.

The src/ restructure moved aria_cli.py and aria_mcp_server.py to
src/aria_code/. The release path kept passing the old root-relative names:

    pyinstaller --onedir --name aria-code-bin ... aria_cli.py

Six places in total -- four in build-native-binaries.yml (Windows and Linux,
CLI and MCP server) and two in scripts/build_native_binary.sh (macOS). All five
platform builds failed on the first release that actually reached them, and
because the binaries job gates the publish job, the release tagged v0.45.0 and
published nothing.

Nothing noticed for over a month. The binaries workflow last succeeded in
August, and between then and now it was only ever *triggered* by a tag push
that a GITHUB_TOKEN could not trigger -- so the stale paths sat in a job that
never ran. The same stale root path was in CONTRIBUTING.md too, where it only
misled a reader.

This test is deliberately about file existence rather than about the specific
path, so moving an entry point again fails here instead of in a release.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-native-binaries.yml"
MAC_SCRIPT = ROOT / "scripts" / "build_native_binary.sh"

# The entry points the freeze is built from. Named here so that a build which
# silently stops producing one of them is also a failure.
EXPECTED_ENTRYPOINTS = {"aria_cli.py", "aria_mcp_server.py"}

# A bare continuation argument, not a YAML path-filter list item or a helper
# command such as `python scripts/verify_native_frontend.py`.
_PY_ARG = re.compile(r"[\w./${}]+\.py")


def _entrypoint_args(text: str) -> set[str]:
    """Every *.py token that appears as a bare argument on its own line.

    Both the workflow (`run: >` folded scalars) and the shell script put the
    entry point on its own continuation line, so this finds them without having
    to parse either language.
    """
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip().rstrip("\\").strip()
        if stripped.startswith("#"):
            continue
        # Quotes come off before the suffix check: the shell script writes
        # "$PROJECT_ROOT/src/aria_code/aria_cli.py", which ends in a quote.
        token = stripped.strip('"').strip("'")
        if not _PY_ARG.fullmatch(token):
            continue
        token = token.replace("$PROJECT_ROOT/", "").replace("${PROJECT_ROOT}/", "")
        found.add(token)
    return found


class EntryPointExtraction(unittest.TestCase):
    def test_path_filters_and_helper_commands_are_not_freeze_arguments(self):
        text = '''
      - 'src/aria_code/apps/cli/app_server.py'
      - 'scripts/verify_native_frontend.py'
        python scripts/verify_native_frontend.py
        pyinstaller --onedir --name aria-code-bin
          src/aria_code/aria_cli.py
          "$PROJECT_ROOT/src/aria_code/aria_mcp_server.py" \\
          src/aria_code/missing_entrypoint.py
        '''
        self.assertEqual(_entrypoint_args(text), {
            'src/aria_code/aria_cli.py', 'src/aria_code/aria_mcp_server.py',
            'src/aria_code/missing_entrypoint.py',
        })


class TheBuildPointsAtFilesThatExist(unittest.TestCase):
    def test_workflow_entrypoints_exist(self) -> None:
        self.assertTrue(WORKFLOW.exists(), "build-native-binaries.yml is missing")
        args = _entrypoint_args(WORKFLOW.read_text(encoding="utf-8"))
        self.assertTrue(args, "found no PyInstaller entry points in the workflow")
        for arg in sorted(args):
            with self.subTest(entrypoint=arg):
                self.assertTrue(
                    (ROOT / arg).is_file(),
                    f"build-native-binaries.yml builds {arg}, which does not exist. "
                    f"PyInstaller fails the job, the publish job is skipped, and the "
                    f"release ships a tag with no artifacts.",
                )

    def test_macos_script_entrypoints_exist(self) -> None:
        self.assertTrue(MAC_SCRIPT.exists(), "build_native_binary.sh is missing")
        args = _entrypoint_args(MAC_SCRIPT.read_text(encoding="utf-8"))
        self.assertTrue(args, "found no PyInstaller entry points in the mac script")
        for arg in sorted(args):
            with self.subTest(entrypoint=arg):
                self.assertTrue(
                    (ROOT / arg).is_file(),
                    f"scripts/build_native_binary.sh builds {arg}, which does not exist",
                )


class BothBinariesAreStillBuilt(unittest.TestCase):
    """Guards against the existence check passing because a build went away."""

    def test_every_platform_builds_the_cli_and_the_mcp_server(self) -> None:
        for path in (WORKFLOW, MAC_SCRIPT):
            with self.subTest(file=path.name):
                names = {pathlib.PurePath(a).name
                         for a in _entrypoint_args(path.read_text(encoding="utf-8"))}
                self.assertEqual(
                    names, EXPECTED_ENTRYPOINTS,
                    f"{path.name} builds {sorted(names)}; expected "
                    f"{sorted(EXPECTED_ENTRYPOINTS)}",
                )


class TheFreezeCanResolveTheBareImportRoot(unittest.TestCase):
    """--paths is load-bearing, and its absence fails late and expensively.

    The tree is importable under two roots: `aria_code.X` via src/, and a bare
    `X` via src/aria_code/ — 399 modules import each other bare
    (`from apps.cli.broker_render import ...`). PyInstaller resolves imports at
    analysis time against its own search path, which does not include either.

    Without --paths the build *succeeds* and produces an 84 MB binary that dies
    on startup:

        ModuleNotFoundError: No module named 'apps.cli.broker_render'

    Measured, not inferred: that is what the first local rebuild did once the
    entry-point paths were corrected. Each platform's smoke test does catch it
    — correctly — but only after a full freeze on five runners. This fails in
    milliseconds instead.
    """

    REQUIRED_ROOTS = ("src/aria_code", "src")

    def test_the_workflow_passes_both_roots_to_every_build(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        builds = text.count("--distpath dist-native/dist")
        self.assertEqual(builds, 4, "expected four PyInstaller invocations")
        # Counted as whole arguments: "--paths src" is a prefix of
        # "--paths src/aria_code", so a substring count double-counts.
        args = re.findall(r"--paths\s+(\S+)", text)
        for root in self.REQUIRED_ROOTS:
            with self.subTest(root=root):
                self.assertEqual(
                    args.count(root), builds,
                    f"not every build passes --paths {root}; the ones that do not "
                    f"produce a binary that builds and then fails to start",
                )

    def test_the_macos_script_passes_both_roots_to_every_build(self) -> None:
        text = MAC_SCRIPT.read_text(encoding="utf-8")
        builds = text.count("--onedir --name")
        self.assertEqual(builds, 2, "expected two PyInstaller invocations")
        args = [a.strip('"') for a in re.findall(r"--paths\s+(\S+)", text)]
        for root in self.REQUIRED_ROOTS:
            with self.subTest(root=root):
                self.assertEqual(
                    args.count(f"$PROJECT_ROOT/{root}"), builds,
                    f"not every build passes --paths for {root}",
                )

    def test_every_frozen_binary_is_smoke_tested(self) -> None:
        # The assertion above checks the flag; this checks that something still
        # runs the result. Between them, a binary that cannot start cannot ship.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            workflow.count("name: Smoke test"), 4,
            "every frozen binary must be executed before it is uploaded",
        )
        self.assertIn('--version', MAC_SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
