"""Every module the program loads by name at runtime must be in the frozen binary.

PyInstaller follows import statements. A module loaded by name —

    sys.modules[__name__] = import_module("clients.market_data_client")
    importlib.import_module(module_path)        # from a registry table

— is invisible to it. Inspected, the v0.56.0 macOS binary contained:

    compatibility shim targets    0 of 26
    broker adapters               1 of 10   (paper trading only)
    agents loaded by name         8 of 31   (no warehouse, realty, coder, supervisor)

Every one of those loaders catches ImportError and logs at debug level, so
nothing failed. `quote` fell back to a backend at localhost:8000; the broker
list was shorter; whole agent teams were absent.

scripts/pyinstaller_collect_args.py writes a PyInstaller hook listing every
first-party module under both import roots. These tests keep it that way
without building anything: each runtime target must be listed under the exact
name it is imported by (the bare and packaged roots are different modules —
listing only one is how --collect-submodules failed), and every PyInstaller
invocation must use the collector.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aria_code"
WORKFLOW = ROOT / ".github" / "workflows" / "build-native-binaries.yml"
MAC_SCRIPT = ROOT / "scripts" / "build_native_binary.sh"


def _collector():
    spec = importlib.util.spec_from_file_location(
        "pyinstaller_collect_args", ROOT / "scripts" / "pyinstaller_collect_args.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_targets() -> dict[str, list[str]]:
    shims = [m.group(1) for f in sorted(SRC.glob("*.py"))
             if (m := re.search(r'_import_module\("([\w.]+)"\)', f.read_text(encoding="utf-8")))]
    brokers = re.findall(r'\("(brokers\.[\w.]+)",', (SRC / "brokers" / "registry.py").read_text(encoding="utf-8"))
    agents = re.findall(r'_try_import_builtin\(\s*"([\w.]+)"', (SRC / "agents" / "registry.py").read_text(encoding="utf-8"))
    return {"shim": shims, "broker": brokers, "agent": agents}


class EveryRuntimeImportIsListed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hidden = set(_collector().hidden_imports())

    def test_the_tables_were_found(self) -> None:
        # Guards against the parsing below silently matching nothing, which
        # would make the next test pass vacuously.
        targets = _runtime_targets()
        self.assertGreaterEqual(len(targets["shim"]), 20)
        self.assertGreaterEqual(len(targets["broker"]), 5)
        self.assertGreaterEqual(len(targets["agent"]), 20)

    def test_each_target_is_listed_under_its_exact_name(self) -> None:
        for kind, modules in _runtime_targets().items():
            for module in modules:
                with self.subTest(kind=kind, module=module):
                    self.assertIn(
                        module, self.hidden,
                        f"{module} is loaded by name at runtime but is not in the hook, so "
                        f"the binary will not contain it — and the loader will swallow "
                        f"the ImportError",
                    )

    def test_both_roots_are_listed(self) -> None:
        self.assertIn("clients.market_data_client", self.hidden)
        self.assertIn("aria_code.clients.market_data_client", self.hidden)
        self.assertIn("aria_code", self.hidden)

    def test_each_target_exists_on_disk(self) -> None:
        # Listing a module still cannot supply one that is not there.
        for kind, modules in _runtime_targets().items():
            for module in modules:
                parts = module.split(".")
                if parts[0] == "aria_code":
                    parts = parts[1:]
                base = SRC.joinpath(*parts)
                with self.subTest(kind=kind, module=module):
                    self.assertTrue(
                        base.with_suffix(".py").is_file() or (base / "__init__.py").is_file(),
                        f"{module} does not exist",
                    )


class TheHookLoads(unittest.TestCase):
    def test_written_hook_is_valid_python_with_every_name(self) -> None:
        collector = _collector()
        with tempfile.TemporaryDirectory() as tmp:
            hook = collector.write_hook(pathlib.Path(tmp)) / "hook-aria_code.py"
            tree = ast.parse(hook.read_text(encoding="utf-8"))
        (assign,) = [n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "hiddenimports" for t in n.targets)]
        self.assertEqual(assign.targets[0].id, "hiddenimports")
        self.assertEqual(ast.literal_eval(assign.value), collector.hidden_imports())

    def test_hook_fires_even_if_the_entry_never_imports_aria_code(self) -> None:
        # A hook runs only when its module is imported; without this a build
        # whose entry skipped aria_code silently got none of the list.
        args = _collector().collect_args()
        self.assertIn("--additional-hooks-dir", args)
        i = args.index("--hidden-import")
        self.assertEqual(args[i + 1], "aria_code")

    def test_printed_args_have_no_spaces(self) -> None:
        # Spliced in by an unquoted $(...): a space would split an argument.
        for arg in _collector().collect_args():
            self.assertNotIn(" ", arg)


class TheNumbaStackStaysOut(unittest.TestCase):
    """numba/llvmlite made the Linux npm package too large to publish."""

    def test_the_collector_excludes_it(self) -> None:
        args = _collector().collect_args()
        excluded = {args[i + 1] for i, a in enumerate(args) if a == "--exclude-module"}
        self.assertEqual(excluded, {"pandas_ta", "numba", "llvmlite"})

    def test_no_first_party_module_needs_it_to_import(self) -> None:
        # Excluding is only safe while every import of these is lazy or
        # guarded; a bare module-level import would stop the binary starting.
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    with self.subTest(file=str(path.relative_to(SRC)), module=name):
                        self.assertNotIn(name.split(".")[0], {"pandas_ta", "numba", "llvmlite"})


class EveryBuildUsesTheCollector(unittest.TestCase):
    def test_workflow_builds(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        builds = text.count("--distpath dist-native/dist")
        self.assertGreater(builds, 0)
        self.assertEqual(text.count("pyinstaller_collect_args.py"), builds,
                         "a PyInstaller build in the workflow does not use the collector")

    def test_macos_script_builds(self) -> None:
        text = MAC_SCRIPT.read_text(encoding="utf-8")
        builds = text.count("--onedir --name")
        self.assertGreater(builds, 0)
        self.assertEqual(text.count("pyinstaller_collect_args.py"), builds,
                         "a PyInstaller build in the macOS script does not use the collector")


if __name__ == "__main__":
    unittest.main()
