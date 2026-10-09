"""Aria Code.

This package is importable under two roots by design, and has been since the
src/ restructure: ``aria_code.X`` and a bare ``X``. The tree uses both — 399
modules import each other bare (``from market_data_client import get_mdc``)
while others use the package path, and the tests patch whichever root the code
under test actually uses. tests/test_single_import_root.py exists to keep a
single *file* from mixing the two.

In development both roots resolve because pyproject's
``pythonpath = [".", "src", "src/aria_code"]`` and tests/conftest.py put them
there. An installed wheel has neither, so only ``aria_code`` resolved and the
console script died immediately:

    $ aria-code --version
    ModuleNotFoundError: No module named 'aria_cli'

4.4.1 did not hit this because its wheel predates the restructure — it shipped
``aria_cli.py`` and 56 other modules at top level, so the bare root *was* the
package. After the move, nothing put the inner directory on the path.

Adding it here rather than shipping a .pth file keeps it inside the package, so
it applies to `pip install`, `pip install -e .`, a zipapp and a PyInstaller
bundle alike, and it is visible to anyone reading the package rather than hidden
in site-packages.

Both roots now give the same module object (``_OneModulePerFile`` below), so
a bare import is an alias rather than a second copy.

This is a bridge, not the destination. One root is the goal — v4.4.2 converted
the tree to ``aria_code.*`` throughout — but converting it means moving every
test's patch target with it, and doing that silently under a release is how the
bare-root bugs in this history were introduced.
"""

import importlib as _importlib
import importlib.abc as _importlib_abc
import importlib.machinery as _machinery
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    # Appended, not inserted: a module that legitimately shadows one of these
    # names in the user's own project must keep winning.
    _sys.path.append(_HERE)


class _OneModulePerFile(_importlib_abc.MetaPathFinder, _importlib_abc.Loader):
    """A bare import of a module in this package returns the ``aria_code.*`` object.

    Two roots used to mean two module objects per file, each with its own
    globals, and nothing ever errored: a patch, a registry, a cache or an
    exception class applied to one copy simply missed code using the other.
    In one week that was a task list cleared in the copy nobody wrote to, a
    rewind whose conflict error was a different class from the one caught,
    and a test fixture isolating a ledger nobody used while the real one
    filled with pending tasks. Here ``import runtime.checkpoints`` and
    ``import aria_code.runtime.checkpoints`` give the same object.

    Only when the bare name would load a file from this package: a user's
    own ``tools`` or ``utils`` earlier on ``sys.path`` is found first and
    left alone, as before.
    """

    def __init__(self, root: str) -> None:
        self._own: dict = {}
        self.root = _os.path.normcase(root) + _os.sep
        self.names = {
            entry[:-3] if entry.endswith(".py") else entry
            for entry in _os.listdir(root)
            if entry != "__init__.py" and not entry.startswith(("_", "."))
            and (entry.endswith(".py") or _os.path.isfile(_os.path.join(root, entry, "__init__.py")))
        }

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] not in self.names:
            return None
        spec = _machinery.PathFinder.find_spec(fullname, path)
        origin = getattr(spec, "origin", None) or ""
        locations = list(getattr(spec, "submodule_search_locations", None) or ())
        mine = [loc for loc in [origin, *locations] if loc and _os.path.normcase(loc).startswith(self.root)]
        if spec is None or not mine:
            return None
        return _machinery.ModuleSpec(fullname, self, is_package=bool(locations))

    def create_module(self, spec):
        module = _importlib.import_module("aria_code." + spec.name)
        # Keyed by name: importing the real module can alias others first.
        self._own[spec.name] = (module.__spec__, getattr(module, "__loader__", None))
        return module

    def exec_module(self, module):
        # Already executed under its aria_code.* name. The import system has
        # just stamped this alias's spec on it; put the real one back, or
        # importlib.reload() would re-run this no-op instead of the module.
        module.__spec__, module.__loader__ = self._own.pop(module.__spec__.name)


if not any(isinstance(finder, _OneModulePerFile) for finder in _sys.meta_path):
    _sys.meta_path.insert(0, _OneModulePerFile(_HERE))

del _HERE, _importlib_abc  # the finder still uses _os, _importlib and _machinery
