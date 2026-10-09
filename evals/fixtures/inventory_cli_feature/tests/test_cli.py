import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InventoryCLI(unittest.TestCase):
    def invoke(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "report.json"
            source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            done = subprocess.run([sys.executable, "-m", "inventory_report", str(source), "--output", str(output)],
                                  capture_output=True, text=True, timeout=10)
            data = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
            return done, data

    def test_aggregate_and_sort(self):
        done, data = self.invoke([{"sku": "B", "on_hand": 9, "reserved": 2},
                                  {"sku": "A", "on_hand": 5, "reserved": 1},
                                  {"sku": "B", "on_hand": 3, "reserved": 4}])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(data, [{"sku": "A", "available": 4}, {"sku": "B", "available": 6}])

    def test_empty(self):
        done, data = self.invoke([])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(data, [])

    def test_unicode(self):
        done, data = self.invoke([{"sku": "冷链商品", "on_hand": 8, "reserved": 3}])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(data, [{"sku": "冷链商品", "available": 5}])

    def test_invalid_input_does_not_create_report(self):
        invalid = [None, {}, [{"sku": "A"}], [{"sku": "", "on_hand": 1, "reserved": 0}],
                   [{"sku": 5, "on_hand": 1, "reserved": 0}],
                   [{"sku": "A", "on_hand": -1, "reserved": 0}],
                   [{"sku": "A", "on_hand": 1.5, "reserved": 0}],
                   [{"sku": "A", "on_hand": True, "reserved": 0}],
                   [{"sku": "A", "on_hand": 1, "reserved": "0"}]]
        for rows in invalid:
            with self.subTest(rows=rows):
                done, data = self.invoke(rows)
                self.assertNotEqual(done.returncode, 0)
                self.assertIsNone(data)

    def test_readme_documents_cli(self):
        self.assertIn("python -m inventory_report", Path("README.md").read_text(encoding="utf-8"))
        self.assertIn("--output", Path("README.md").read_text(encoding="utf-8"))
        self.assertTrue(Path("inventory_report/report.py").exists())

    def test_no_third_party_test_runner_or_vendored_packages(self):
        # The project explicitly uses only the standard library. A new pytest
        # shim or vendored dependency is not a valid way to satisfy acceptance.
        self.assertFalse(Path("pytest.py").exists())
        self.assertFalse(Path("local_packages").exists())
        import ast
        for source in Path("inventory_report").glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = ([a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import)
                           else [str(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else [])
                for name in modules:
                    self.assertIn(name, sys.stdlib_module_names | {"inventory_report", "", "report"})


if __name__ == "__main__":
    unittest.main()
