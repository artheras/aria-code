"""Where the open side ends and the private quant engine begins.

CLOSING_SOURCE.md describes moving packages/quant_engine to a private repository
as future work ("阶段 2 — 拆库"). Reading the tree, most of it has already
happened. Six of the engine's subpackages are not in this repository at all:

    services/        corporate finance, Stripe analytics, A-share prediction
    agent_runtime/   the quant strategist agent
    risk/            the risk-compliance agent
    analysis/        signal pipeline
    strategies/      strategy bases
    mcp_server.py    the engine's own MCP server

The open side still imports them, guarded, and degrades with a message. What
remains in the tree is stochastic/, sports/, backtest/ and portfolio/.

That split is load-bearing and invisible: an import of an already-private module
looks exactly like an import of a present one, and the difference only shows up
at runtime in a public checkout. So this test pins it.

The property that actually matters is the third test below. An *unguarded*
import of an already-private module is not a degradation -- it is a crash in
every public checkout, on a code path that works perfectly on a developer
machine that has the private repository checked out next door.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aria_code"
ENGINE = SRC / "packages" / "quant_engine"

# Subpackages that live in the private repository and are deliberately absent
# here. Adding a name to this list is a statement that the open side must not
# depend on it unguarded; removing one means it was brought back into the tree.
PRIVATE_SUBPACKAGES = frozenset({
    "services", "agent_runtime", "risk", "analysis", "strategies", "mcp_server", "sports",
})

# Subpackages that are in this tree and published under its license.
PUBLIC_SUBPACKAGES = frozenset({"stochastic", "backtest", "portfolio"})


def _engine_imports() -> list[tuple[str, int, str, bool]]:
    """(relative path, line, first submodule name, guarded) for each open-side import."""
    found: list[tuple[str, int, str, bool]] = []

    for path in sorted(SRC.rglob("*.py")):
        if ENGINE in path.parents or path.name == "build_quant_engine.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = next((a.name for a in node.names if "quant_engine." in a.name), "")
            if "quant_engine." not in module:
                continue

            sub = module.split("quant_engine.", 1)[1].split(".")[0]

            # Guarded == inside the body of a try whose handlers catch ImportError.
            cur, guarded = node, False
            while cur in parents:
                parent = parents[cur]
                if isinstance(parent, ast.Try) and cur in parent.body:
                    for handler in parent.handlers:
                        names: list[str] = []
                        if handler.type is None:
                            guarded = True
                        elif isinstance(handler.type, ast.Name):
                            names = [handler.type.id]
                        elif isinstance(handler.type, ast.Tuple):
                            names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
                        elif isinstance(handler.type, ast.Attribute):
                            names = [handler.type.attr]
                        if {"ImportError", "ModuleNotFoundError",
                            "Exception", "BaseException"} & set(names):
                            guarded = True
                    if guarded:
                        break
                cur = parent

            found.append((str(path.relative_to(SRC)), node.lineno, sub, guarded))
    return found


class TheDeclaredBoundaryMatchesTheTree(unittest.TestCase):
    def test_public_subpackages_are_present(self) -> None:
        for name in sorted(PUBLIC_SUBPACKAGES):
            with self.subTest(subpackage=name):
                self.assertTrue(
                    (ENGINE / name).is_dir(),
                    f"{name}/ is declared public but is not in the tree -- if it was "
                    f"extracted, move it to PRIVATE_SUBPACKAGES",
                )

    def test_private_subpackages_are_absent(self) -> None:
        for name in sorted(PRIVATE_SUBPACKAGES):
            with self.subTest(subpackage=name):
                present = (ENGINE / name).is_dir() or (ENGINE / f"{name}.py").exists()
                self.assertFalse(
                    present,
                    f"{name} is declared private but IS in this tree, which means this "
                    f"repository's license now covers it",
                )

    def test_the_availability_stub_survives_extraction(self) -> None:
        # is_available() must live outside every extractable subpackage, or it
        # leaves with them and the guards lose the thing they ask.
        init = ENGINE / "__init__.py"
        self.assertTrue(init.exists(), "the engine's __init__ is the public stub")
        self.assertIn("def is_available", init.read_text(encoding="utf-8"))


class EveryEngineImportTargetsAKnownSubpackage(unittest.TestCase):
    def test_no_import_names_an_unclassified_subpackage(self) -> None:
        known = PRIVATE_SUBPACKAGES | PUBLIC_SUBPACKAGES
        for rel, line, sub, _ in _engine_imports():
            with self.subTest(site=f"{rel}:{line}"):
                self.assertIn(
                    sub, known,
                    f"{rel}:{line} imports quant_engine.{sub}, which is in neither "
                    f"PRIVATE_SUBPACKAGES nor PUBLIC_SUBPACKAGES -- classify it, so the "
                    f"license boundary stays explicit",
                )


class ImportsOfPrivateCodeAreGuarded(unittest.TestCase):
    """The one invariant that a public checkout depends on.

    A guarded import of an absent module degrades. An unguarded one raises
    wherever it sits -- and it raises only in a public checkout, never on a
    machine that has the private repository alongside, which is exactly the
    configuration this is developed on.
    """

    # Known exception, kept explicit rather than silently tolerated:
    # build_prediction_service() imports services/ unguarded. It survives because
    # all three of its callers wrap the call in `except Exception`; see
    # tests/test_degrades_without_quant_engine.py, which asserts that.
    KNOWN_UNGUARDED = {("apps/cli/commands/ashare_prediction_cmds.py", "services")}

    def test_private_imports_do_not_escape_their_guard(self) -> None:
        offenders = [
            (rel, line, sub)
            for rel, line, sub, guarded in _engine_imports()
            if sub in PRIVATE_SUBPACKAGES and not guarded
            and (rel, sub) not in self.KNOWN_UNGUARDED
        ]
        self.assertEqual(
            offenders, [],
            "these import already-private engine code without catching ImportError, "
            "so they raise in every public checkout:\n"
            + "\n".join(f"  {r}:{ln} -> quant_engine.{s}" for r, ln, s in offenders),
        )

    def test_the_known_exception_is_still_the_only_one_and_still_real(self) -> None:
        # If this fails because the site became guarded, delete the entry --
        # a stale allowance is how a guard rots into a rubber stamp.
        unguarded_private = {
            (rel, sub)
            for rel, _, sub, guarded in _engine_imports()
            if sub in PRIVATE_SUBPACKAGES and not guarded
        }
        self.assertEqual(
            unguarded_private, self.KNOWN_UNGUARDED,
            "KNOWN_UNGUARDED no longer describes the tree",
        )


if __name__ == "__main__":
    unittest.main()
