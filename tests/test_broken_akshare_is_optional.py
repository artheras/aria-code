"""A broken akshare must disable China data, not crash the CLI.

akshare reads bundled data files on import (futures/cons.py loads a calendar
JSON). Once the native binary started bundling every first-party module,
tools/local_finance_tools.py was in it, and with akshare present but its data
missing every command — `quote AAPL` included — died with FileNotFoundError
before doing anything. The guards caught only ImportError.
"""

from __future__ import annotations

import importlib
import importlib.abc
import sys
import unittest

MODULES = (
    "aria_code.tools.local_finance_tools",
)


class _BrokenAkshare(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "akshare" or name.startswith("akshare."):
            raise FileNotFoundError("akshare/file_fold/calendar.json")
        return None


class BrokenAkshareIsOptional(unittest.TestCase):
    def test_modules_import_with_china_data_disabled(self) -> None:
        saved = {k: v for k, v in sys.modules.items()
                 if k == "akshare" or k.startswith("akshare.") or k in MODULES}
        for name in saved:
            del sys.modules[name]
        finder = _BrokenAkshare()
        sys.meta_path.insert(0, finder)
        try:
            for name in MODULES:
                with self.subTest(module=name):
                    module = importlib.import_module(name)
                    self.assertFalse(module._HAS_AK)
        finally:
            sys.meta_path.remove(finder)
            for name in MODULES:
                sys.modules.pop(name, None)
            sys.modules.update(saved)


if __name__ == "__main__":
    unittest.main()
