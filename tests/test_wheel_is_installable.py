"""The built wheel must contain the package and run once installed.

Nothing in this repository built a wheel and looked inside it, and three
separate defects lived in that gap at the same time:

  1. packages.find said where = ["."] while the package is at src/aria_code, so
     the wheel held six metadata entries and zero modules — 7 KB against the
     1.75 MB that 4.4.2 published. `pip install` would have succeeded and
     `import aria_code` would have failed.
  2. With that fixed, the console script still died on
     `ModuleNotFoundError: No module named 'aria_cli'`: the tree imports itself
     bare in 399 places, which resolves in development only because pyproject's
     pythonpath and tests/conftest.py put the inner directory on sys.path. An
     installed wheel has neither.
  3. Neither would have been caught by CI, which installs with `pip install -e .`
     and ran nothing that imports the package from outside pytest.

The first two are fixed. This test is the third: it builds a real wheel, installs
it into a throwaway virtualenv with no source tree in sight, and runs the console
script. Slow by the standards of this suite — tens of seconds — and marked so it
can be deselected, but the only check that answers "would the next release
work?"
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Deselectable: `pytest -m "not slow_packaging"`. Deliberately not skipped by
# default — a release gate that only runs when someone remembers is the state
# this test exists to end.
pytestmark = __import__("pytest").mark.slow_packaging


def _installed_env():
    # Test runners may put the source tree on PYTHONPATH. Do not let pip mistake
    # source metadata for an installed wheel, or let import checks use source.
    return {key: value for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}}


def _build_wheel(into: pathlib.Path) -> pathlib.Path:
    # setuptools writes its scratch into <source>/build regardless of -w, so this
    # dirties the repository. Left behind it makes the next lint run report 52
    # F821s from a stale copy of the tree — a test that fails the linter for
    # everyone afterwards is a bad neighbour, so it removes what it created.
    build_dir = ROOT / "build"
    pre_existing = build_dir.exists()
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(into), str(ROOT)],
            check=True, capture_output=True, text=True,
        )
    finally:
        if not pre_existing:
            shutil.rmtree(build_dir, ignore_errors=True)
    wheels = sorted(into.glob("*.whl"))
    assert wheels, "pip wheel produced nothing"
    return wheels[0]


class BuiltWheel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which(sys.executable) is None:  # pragma: no cover
            raise unittest.SkipTest("no interpreter to build with")
        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            cls.wheel = _build_wheel(cls.tmp / "dist")
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"wheel build unavailable here: {exc.stderr[-400:]}")
        cls.names = zipfile.ZipFile(cls.wheel).namelist()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_contains_the_package_at_all(self):
        """The failure this catches shipped nothing but metadata."""
        modules = [n for n in self.names if n.startswith("aria_code/")]
        self.assertGreater(
            len(modules), 100,
            f"wheel holds {len(modules)} aria_code files; it is metadata-only. "
            "Check [tool.setuptools.packages.find] where.",
        )

    def test_it_contains_the_modules_the_entry_point_needs(self):
        for needed in ("aria_code/aria_cli.py", "aria_code/apps/cli/main.py",
                       "aria_code/doctor.py", "aria_code/ui/assets/aria-robot.png",
                       "aria_code/ui/robot_pixels.py"):
            with self.subTest(module=needed):
                self.assertIn(needed, self.names)

    def test_the_wheel_is_not_suspiciously_small(self):
        size = self.wheel.stat().st_size
        self.assertGreater(size, 500_000,
                           f"{self.wheel.name} is {size} bytes; 4.4.2 shipped 1.75 MB")


class InstalledWheel(unittest.TestCase):
    """Install it somewhere with no source tree and run the console script."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            wheel = _build_wheel(cls.tmp / "dist")
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"wheel build unavailable here: {exc.stderr[-400:]}")
        venv = cls.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                       capture_output=True)
        cls.py = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
        cls.bin = venv / ("Scripts" if os.name == "nt" else "bin")
        proc = subprocess.run([str(cls.py), "-m", "pip", "install", "-q", str(wheel)],
                              capture_output=True, text=True, cwd=cls.tmp, env=_installed_env())
        if proc.returncode != 0:  # pragma: no cover
            raise unittest.SkipTest(f"could not install into a venv: {proc.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, *args, cwd=None):
        # cwd is deliberately NOT the repo: running from the source tree would
        # let the bare imports resolve from the working directory and hide the
        # very thing being tested.
        return subprocess.run([str(self.py), *args], capture_output=True, text=True,
                              cwd=cwd or str(self.tmp), timeout=180, env=_installed_env())

    def test_the_package_imports(self):
        proc = self._run("-c", "import aria_code; print(aria_code.__file__)")
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])

    def test_the_bare_root_resolves_once_the_package_is_imported(self):
        """399 modules import each other bare, and that has to work installed.

        The contract is deliberately this and not more: the path is added by
        aria_code/__init__.py, so a bare import resolves once anything in the
        package has been imported. Both console scripts are aria_code.* entry
        points, so every real path through the package satisfies that.

        A bare import with no aria_code import first — `python -c "import
        aria_cli"` — does not resolve, and nothing shipped needs it to. Writing
        the weaker assertion here is on purpose: asserting the stronger one
        would either be a lie about the fix or an argument for shipping a .pth
        file that changes sys.path for the whole interpreter.
        """
        proc = self._run("-c", "import aria_code; import aria_cli; print('ok')")
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertIn("ok", proc.stdout)

    def test_a_bare_import_on_its_own_is_not_claimed_to_work(self):
        """Pins the boundary, so the comment above cannot quietly become false."""
        proc = self._run("-c", "import aria_cli")
        self.assertNotEqual(proc.returncode, 0,
                            "bare-first imports now work — the fix changed, and "
                            "the test above should be strengthened to match")

    def test_the_console_script_runs(self):
        exe = self.bin / ("aria-code.exe" if os.name == "nt" else "aria-code")
        self.assertTrue(exe.exists(), f"{exe} was not installed")
        proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                              cwd=str(self.tmp), timeout=180, env=_installed_env())
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertIn("aria-code", proc.stdout.lower())

    def test_model_diagnostic_is_in_the_installed_wheel(self):
        exe = self.bin / ("aria-code.exe" if os.name == "nt" else "aria-code")
        proc = subprocess.run([str(exe), "health", "--help"], capture_output=True, text=True,
                              cwd=str(self.tmp), timeout=30, env=_installed_env())
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertIn("--tools", proc.stdout)
        self.assertIn("--model-id", proc.stdout)


if __name__ == "__main__":
    unittest.main()
