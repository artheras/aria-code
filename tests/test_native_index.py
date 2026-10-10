"""The native import resolver must agree with the Python one, edge for edge."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aria_code.runtime import native_index
from aria_code.runtime import project_graph as pg

REPO = Path(__file__).resolve().parents[1]
needs_binary = pytest.mark.skipif(native_index.native_binary() is None,
                                  reason="aria-native not built (set ARIA_NATIVE_BINARY)")


def _build(root: Path, monkeypatch, native: bool) -> dict[str, list[str]]:
    monkeypatch.setenv("ARIA_NATIVE_GRAPH", "on" if native else "off")
    monkeypatch.setattr(native_index, "_MIN_FILES", 0)
    if native:
        calls = []
        real = native_index.resolve_imports

        def spy(*args, **kwargs):
            result = real(*args, **kwargs)
            calls.append(result)
            return result

        monkeypatch.setattr(native_index, "resolve_imports", spy)
    graph = pg.ProjectGraph(root).build()
    if native:
        assert calls and calls[0] is not None, "the native resolver was not used"
    return {path: info["imports"] for path, info in graph.files.items()}


def _write(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


EDGE_CASES = {
    "app/__init__.py": "",
    "app/core.py": """
        from . import models
        from .models import User
        from .. import toplevel
        from ...beyond import nothing
        import app.helpers as h

        def lazy():
            from app.services import billing
            return billing

        class Loader:
            try:
                import yaml
                from app import util
            except ImportError:
                from .compat import util
            finally:
                pass

        match 1:
            case 1:
                import app.cases
    """,
    "app/models.py": "class User: ...\n",
    "app/helpers.py": "",
    "app/compat.py": "",
    "app/cases.py": "",
    "app/services/__init__.py": "",
    "app/services/billing.py": "from ..models import User\nfrom . import *\n",
    "toplevel.py": "",
    # Two files answer to `util`: the importer's own directory wins, a tie resolves to nothing.
    "app/util.py": "",
    "lib/util.py": "",
    "lib/user.py": "import util\n",
    "other/user.py": "import util\n",
    "bom.py": b"\xef\xbb\xbfimport app.models\n",
    "broken.py": "def oops(:\n    import app.models\n",
    "web/index.ts": "import { a } from './a'\nexport * from './lib'\nconst c = require('../shared/c')\nimport('./lazy')\n",
    "web/a.tsx": "",
    "web/lib/index.js": "",
    "web/lazy.mjs": "",
    "shared/c.js": "import x from 'react'\n",
}


@needs_binary
def test_edge_cases_resolve_the_same_way(tmp_path, monkeypatch):
    root = _write(tmp_path, EDGE_CASES)
    python = _build(root, monkeypatch, native=False)
    native = _build(root, monkeypatch, native=True)
    assert native == python
    # And the answers are the ones the Python implementation is known to give.
    assert python["app/core.py"] == sorted({
        "app/models.py", "toplevel.py", "app/helpers.py",
        "app/services/billing.py", "app/util.py", "app/compat.py", "app/cases.py"})
    assert python["lib/user.py"] == ["lib/util.py"]
    assert python["other/user.py"] == []
    assert python["bom.py"] == [] and python["broken.py"] == []
    assert python["web/index.ts"] == ["shared/c.js", "web/a.tsx", "web/lazy.mjs", "web/lib/index.js"]


@needs_binary
@pytest.mark.timeout(300)  # two full builds of this repository; slow on Windows runners
def test_this_repository_resolves_the_same_way(monkeypatch):
    assert _build(REPO, monkeypatch, native=True) == _build(REPO, monkeypatch, native=False)


def test_python_is_used_when_native_is_off_or_missing(tmp_path, monkeypatch):
    files = [("a.py", "python")]
    monkeypatch.setenv("ARIA_NATIVE_GRAPH", "off")
    assert native_index.resolve_imports(tmp_path, files, ["a.py"], minimum=0) is None
    monkeypatch.setenv("ARIA_NATIVE_GRAPH", "on")
    monkeypatch.setenv("ARIA_NATIVE_BINARY", str(tmp_path / "missing"))
    assert native_index.resolve_imports(tmp_path, files, ["a.py"], minimum=0) is None


def test_small_batches_stay_in_python(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_NATIVE_BINARY", str(tmp_path / "never-called"))
    (tmp_path / "never-called").write_text("")
    assert native_index.resolve_imports(tmp_path, [("a.py", "python")], ["a.py"]) is None


@needs_binary
def test_a_rejected_file_is_handed_back_to_python(tmp_path, monkeypatch):
    root = _write(tmp_path, {"a.py": "def oops(:\n", "b.py": "import a\n"})
    monkeypatch.setenv("ARIA_NATIVE_GRAPH", "on")
    imports, fallback = native_index.resolve_imports(
        root, [("a.py", "python"), ("b.py", "python")], ["a.py", "b.py"], minimum=0)
    assert fallback == ["a.py"] and imports == {"b.py": ["a.py"]}
