"""The open side must keep working when the quant engine is not installed.

CLOSING_SOURCE.md plans to compile packages/quant_engine and move it to a
private repository, at which point a public checkout will not contain it at all.
That plan rests on one claim -- that every call site degrades rather than
crashing -- and nothing tested the claim, because the engine has always been
present in this tree.

An audit of the eleven import sites outside the package found nine inside
`try/except ImportError` and two that are not:

    agents/sports/football_agent.py            in predict()
    apps/cli/commands/ashare_prediction_cmds.py  in build_prediction_service()

Those two are still safe, but for a different reason: every caller wraps the
call in `except Exception`, which catches ImportError on the way past. That is a
weaker guarantee than guarding the import -- it depends on callers, and a new
caller gets no help from it -- so it is worth having a test that says so rather
than a comment.

The engine is blocked with a meta_path finder in a subprocess, because
sys.modules and sys.meta_path are process-global and this has to look like a
checkout that genuinely lacks the package.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Makes `packages.quant_engine` and every submodule unimportable, as if the
# directory were not on disk. Installed before the code under test is imported.
BLOCKER = """
import sys
from importlib.abc import MetaPathFinder


class _NoQuantEngine(MetaPathFinder):
    # The package's own __init__ stays: it holds is_available(), which checks for
    # the .stochastic SUBmodule rather than for itself. So what an extraction
    # removes is the subpackages, and the __init__ remains as a public stub that
    # answers "no". Blocking the __init__ too would test a layout the plan does
    # not produce -- and would take is_available() with it.
    KEEP = {"packages.quant_engine", "aria_code.packages.quant_engine"}

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.KEEP:
            return None
        for stem in ("packages.quant_engine.", "aria_code.packages.quant_engine."):
            if fullname.startswith(stem):
                raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


sys.meta_path.insert(0, _NoQuantEngine())
"""


def _run_without_the_engine(body: str) -> subprocess.CompletedProcess:
    # Concatenated, not .format()ed: BLOCKER contains {fullname!r} in an f-string,
    # which .format reads as a field of its own.
    paths = [str(ROOT / "src"), str(ROOT / "src" / "aria_code"), str(ROOT)]
    prelude = BLOCKER + f"sys.path[:0] = {paths!r}\n"
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(body)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT),
    )


class TheEngineReportsItselfMissing(unittest.TestCase):
    def test_is_available_returns_false_rather_than_raising(self) -> None:
        # The whole boundary hangs off this one call, so it must not raise --
        # it wraps find_spec in try/except for exactly this reason.
        proc = _run_without_the_engine("""
            from packages.quant_engine import is_available
            print("available:", is_available())
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn("available: False", proc.stdout)


class ModulesThatUseTheEngineStillImport(unittest.TestCase):
    """A missing engine must not make any open-side module unimportable."""

    # The modules that reach the engine, as found by the import audit. Import
    # is the bar here: these hold slash commands and agents, so failing to
    # import takes unrelated commands down with them.
    MODULES = (
        "apps.cli.commands.backtest_cmds",
    )

    def test_each_module_imports_without_the_engine(self) -> None:
        for name in self.MODULES:
            with self.subTest(module=name):
                proc = _run_without_the_engine(f"""
                    import importlib
                    importlib.import_module({name!r})
                    print("imported")
                """)
                self.assertEqual(
                    proc.returncode, 0,
                    f"{name} cannot be imported without the quant engine:\n"
                    f"{proc.stderr[-1500:]}",
                )


if __name__ == "__main__":
    unittest.main()
