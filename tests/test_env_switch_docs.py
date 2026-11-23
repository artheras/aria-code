"""A documented environment switch must be a switch that exists.

SafetyService.trading_dry_run() documented the trading kill-switch as
ARIA_TRADING_DRY_RUN while the implementation read ARIA_DRY_RUN. Setting the
documented variable froze nothing and reported no error, on a facade whose
whole purpose is to be the authoritative entry point — and since no .md file
mentions the switch at all, that docstring was its only documentation.

Naming an env var in prose costs nothing and is never type-checked, so this
guard is static: every ARIA_* name that appears in a docstring must actually
be read somewhere in the tree. The read is looked for tree-wide rather than
in the same package on purpose — a facade documenting a switch whose
os.getenv lives in the package it delegates to is correct, and that is exactly
the shape of this one.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "aria_code"
SERVICES = REPO_ROOT / "services"
# Tree-wide. Scoping this to a few packages would have missed aria_daemon.py's
# module docstring, which advertised an ARIA_API_BASE that appears exactly once
# in the repository — in that line. The backend URL is the `api_url` config
# key; the env var never existed.
SCOPE = SRC
ENV_NAME = re.compile(r"\bARIA_[A-Z0-9_]+\b")


def _docstring_env_names(root: pathlib.Path) -> dict:
    """{env name: [where it was documented]} across a package's docstrings."""
    found: dict = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - would fail the lint gate first
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node) or ""
            for name in ENV_NAME.findall(doc):
                where = f"{path.name}:{getattr(node, 'name', '<module>')}"
                found.setdefault(name, []).append(where)
    return found


def _env_names_read(root: pathlib.Path) -> set:
    """Every ARIA_* string literal appearing outside a docstring."""
    read: set = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                read.update(ENV_NAME.findall(node.value))
    if SERVICES.is_dir():
        for service_src in sorted(SERVICES.glob("*/src")):
            for path in sorted(service_src.rglob("*.ts")):
                read.update(ENV_NAME.findall(path.read_text(encoding="utf-8")))
    return read


class DocumentedEnvSwitchesExist(unittest.TestCase):
    def test_every_documented_aria_var_is_actually_read(self):
        documented = _docstring_env_names(SCOPE)
        read = _env_names_read(SCOPE)
        self.assertGreater(len(documented), 20,
                           "scan found almost nothing — the walk is broken")
        for name, places in sorted(documented.items()):
            with self.subTest(var=name):
                self.assertIn(
                    name, read,
                    f"{name} is documented in {places} but no code under "
                    f"src/aria_code/ ever reads it",
                )


class TradingKillSwitchWorks(unittest.TestCase):
    """The behavioural half: the switch the docstring names must freeze trading."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("ARIA_DRY_RUN", "ARIA_TRADING_DRY_RUN")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _documented_var(self) -> str:
        from aria_code.safety import SafetyService
        names = ENV_NAME.findall(SafetyService.trading_dry_run.__doc__ or "")
        self.assertTrue(names, "trading_dry_run() documents no env var")
        # Exactly one: the docstring deliberately describes the old dead name
        # rather than spelling it, so this guard cannot be satisfied by prose.
        self.assertEqual(len(set(names)), 1, f"ambiguous switch name: {names}")
        return names[0]

    def test_documented_switch_freezes_a_live_account(self):
        from aria_code.safety import SafetyService
        svc = SafetyService({"mode": "live"})
        os.environ.pop("ARIA_DRY_RUN", None)
        os.environ.pop("ARIA_TRADING_DRY_RUN", None)
        self.assertEqual(svc.trading_mode(), "live")
        self.assertFalse(svc.trading_dry_run())

        os.environ[self._documented_var()] = "1"
        self.assertTrue(svc.trading_dry_run(),
                        "the documented kill-switch did not engage")
        self.assertEqual(svc.trading_mode(), "read_only",
                         "kill-switch engaged but a live account stayed live")


if __name__ == "__main__":
    unittest.main()
