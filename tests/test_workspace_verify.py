import json
import tempfile
import unittest
from pathlib import Path

from aria_code.workspace import VerificationPlanner


class VerificationPlannerTests(unittest.TestCase):
    def test_python_file_gets_compile_and_pytest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            plan = VerificationPlanner(root).infer(["app.py"])
            self.assertIn("python3 -m py_compile app.py", plan.commands)
            self.assertIn("python3 -m pytest -q", plan.commands)

    def test_node_file_uses_package_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}),
                encoding="utf-8",
            )
            plan = VerificationPlanner(root).infer(["src/App.tsx"])
            self.assertIn("npm test", plan.commands)
            self.assertIn("npm run build", plan.commands)

    def test_no_paths_falls_back_to_project_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            plan = VerificationPlanner(root).infer([])
            self.assertEqual(plan.commands, ["python3 -m pytest -q"])


if __name__ == "__main__":
    unittest.main()


class FlatLayoutTests(unittest.TestCase):
    """Tests beside the code, with no config and no tests/ directory."""

    def plan(self, files, changed=("orders.py",)):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return VerificationPlanner(root).infer(list(changed)).commands

    def test_a_test_file_next_to_the_code_is_run_not_just_compiled(self):
        commands = self.plan({"orders.py": "x = 1\n", "test_orders.py": "def test_x():\n    assert True\n"})
        self.assertEqual(commands, ["python3 -m py_compile orders.py", "python3 -m pytest -q"])

    def test_the_suffix_form_and_conftest_count_too(self):
        self.assertIn("python3 -m pytest -q", self.plan({"orders.py": "", "orders_test.py": "def test_x():\n    pass\n"}))
        self.assertIn("python3 -m pytest -q", self.plan({"orders.py": "", "conftest.py": ""}))

    def test_a_flat_unittest_project_runs_unittest_not_pytest(self):
        commands = self.plan({"orders.py": "", "test_orders.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"})
        self.assertEqual(commands[-1], "python3 -m unittest discover -s . -v")

    def test_a_one_level_subdirectory_counts_but_a_virtualenv_does_not(self):
        self.assertIn("python3 -m pytest -q", self.plan({"orders.py": "", "pkg/test_orders.py": "def test_x():\n    pass\n"}))
        commands = self.plan({"orders.py": "", ".venv/lib/test_vendor.py": "", "node_modules/x/test_y.py": ""})
        self.assertEqual(commands, ["python3 -m py_compile orders.py"])

    def test_no_tests_anywhere_still_means_only_a_syntax_check(self):
        self.assertEqual(self.plan({"orders.py": "x = 1\n"}), ["python3 -m py_compile orders.py"])

    def test_every_flat_eval_fixture_gets_its_tests_run(self):
        fixtures = Path(__file__).resolve().parents[1] / "evals" / "fixtures"
        flat = [f for f in sorted(fixtures.iterdir())
                if f.is_dir() and any(f.glob("test*.py")) and not any((f / n).exists() for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "tests"))]
        self.assertGreater(len(flat), 40)
        for fixture in flat:
            source = next((p.name for p in fixture.glob("*.py") if not p.name.startswith("test")), None)
            if source is None:
                continue
            commands = VerificationPlanner(fixture).infer([source]).commands
            self.assertTrue(any("pytest" in c or "unittest" in c for c in commands), f"{fixture.name}: {commands}")
