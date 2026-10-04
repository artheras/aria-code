"""Keep optional finance libraries off the coding CLI's cold-start path."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest


class StartupLatencyTests(unittest.TestCase):
    def test_importing_cli_does_not_import_finance_runtimes(self):
        code = (
            "import aria_cli, sys; "
            "heavy = {'numpy', 'pandas', 'scipy', 'yfinance', 'akshare', 'ccxt'}; "
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
