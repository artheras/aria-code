"""Keep optional finance libraries off the coding CLI's cold-start path."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path


class StartupLatencyTests(unittest.TestCase):
    def test_terminal_startup_defers_file_parsers_until_file_command(self):
        code = '''
import asyncio, sys
import aria_code.aria_cli as cli
from pathlib import Path
terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
assert terminal._file_session is None
assert not any(name.endswith("file_analysis_tools") for name in sys.modules)
heavy = {"pandas", "pdfplumber", "pypdf", "docx", "openpyxl"}
assert not heavy.intersection(sys.modules), heavy.intersection(sys.modules)
# Registration must not load Excel; its first real workbook still works.
from aria_code.tools.spreadsheet_tools import HAS_OPENPYXL, write_workbook
if HAS_OPENPYXL:
    result = write_workbook({"sheets": [{"name": "Sheet", "headers": ["Value"], "rows": [[3]]}]},
                            out_path=Path("first-workbook.xlsx"))
    assert result["success"] and Path("first-workbook.xlsx").is_file()
    assert "openpyxl" in sys.modules
assert terminal._file_session is None
path = Path("notes.txt")
path.write_text("Hello Aria / 你好", encoding="utf-8")
async def use_files():
    await terminal.commands.cmd_file("load " + str(path))
    session = terminal._file_session
    assert session is not None
    assert "Hello Aria / 你好" in session.get_active().content
    await terminal.commands.cmd_file("list")
    assert terminal._file_session is session
asyncio.run(use_files())
'''
        with tempfile.TemporaryDirectory(prefix="aria-lazy-files-") as directory:
            env = dict(os.environ, HOME=directory,
                       ARIA_HOME=str(Path(directory) / "state"),
                       ARIA_USER_OUTPUT_ROOT=str(Path(directory) / "output"))
            result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                                    env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_model_probe_help_does_not_load_agent_or_finance(self):
        code = (
            "import runpy, sys; sys.argv = ['aria-code', 'health', '--help']; "
            "\ntry: runpy.run_module('aria_code.aria_cli', run_name='__main__')"
            "\nexcept SystemExit as exc: assert exc.code == 0"
            "\nassert not {'pandas', 'scipy', 'yfinance', 'aria_code.runtime.agent_loop'}.intersection(sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                env=os.environ.copy(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--tools", result.stdout)

    def test_importing_cli_does_not_import_finance_runtimes(self):
        code = (
            "import aria_code.aria_cli, sys; "
            "heavy = {'pandas', 'scipy', 'yfinance', 'akshare', 'ccxt'}; "
            "loaded = heavy.intersection(sys.modules); "
            "assert not loaded, f'eager finance imports: {sorted(loaded)}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=os.environ.copy(), capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_exits_before_importing_finance_runtimes(self):
        code = (
            "import runpy, sys; "
            "sys.argv = ['aria_cli.py', '--version']; "
            "runpy.run_module('aria_code.aria_cli', run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=os.environ.copy(), capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^aria-code \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
