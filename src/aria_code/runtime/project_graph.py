"""Project graph — what a change reaches before it is made.

``repo_map`` answers "where is X defined and who mentions it" for one name at
a time. Deciding whether an edit is safe needs the other direction, walked:
change ``session.py`` and which files import it, which import those, which
tests exercise any of them, which deployable service ships them. That is what
this builds, from four kinds of node and the edges between them:

* files, and the symbols they define (``repo_map``'s scan);
* ``imports`` between files — Python resolved with ``ast``, JS/TS relative
  specifiers resolved against the tree; other languages fall back to
  references;
* ``references`` — a file naming a symbol another file defines, using the
  same filters that keep ``repo_map``'s ranking honest (no names under four
  characters, none defined in more than a handful of files);
* tests (files under ``tests/``, ``test_*``, ``*.test.ts`` …), which cover
  what they import or reference;

Impact follows imports up to three hops but not through a package
``__init__`` or a file importing more than twenty others: past those,
everything reaches everything.
* services — a ``docker-compose`` service's build context, or a directory
  with its own ``pyproject.toml`` / ``package.json`` / ``go.mod`` /
  ``Cargo.toml`` — which contain the files below them.

The graph is saved per repository under ``<aria home>/graphs``. A session
that finds every file at the size and mtime it was saved with reuses it after
a stat per file; otherwise it is rebuilt, re-parsing only changed files'
imports.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .repo_map import RepoMap

GRAPH_VERSION = 1

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
_JS_IMPORT = re.compile(
    r"""(?:\bimport\s[^'"]*?\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bexport\s[^'"]*?\bfrom\s*|\bimport\s+)"""
    r"""['"](\.{1,2}/[^'"]+)['"]"""
)
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
_PACKAGE_MARKERS = ("pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml")
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Same thresholds as repo_map's ranking: a short or widely defined name says
# nothing about which file a mention depends on.
_MIN_NAME = 4
_MAX_DEFINING_FILES = 3
_TRANSITIVE_DEPTH = 3
# A file importing this many others is an aggregator (a CLI entry point, an
# app factory). It is reached, but the walk does not continue through it, nor
# through a package ``__init__``: past either, everything imports everything
# and "reached through imports" would list the whole repository.
_HUB_FAN_OUT = 20


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


@dataclass
class Impact:
    targets: list[str]
    unresolved: list[str] = field(default_factory=list)
    direct: list[str] = field(default_factory=list)       # import or reference a target
    indirect: list[str] = field(default_factory=list)     # import those, up to three hops
    tests: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)

    @property
    def breadth(self) -> str:
        reach = len(self.direct) + len(self.indirect)
        if reach == 0:
            return "isolated"
        if reach <= 5:
            return "narrow"
        if reach <= 25:
            return "moderate"
        return "wide"

    def as_dict(self, *, limit: int = 40) -> dict:
        return {
            "targets": self.targets,
            "unresolved": self.unresolved,
            "breadth": self.breadth,
            "direct_dependents": self.direct[:limit],
            "indirect_dependents": self.indirect[:limit],
            "tests": self.tests[:limit],
            "services": self.services,
            "counts": {"direct": len(self.direct), "indirect": len(self.indirect),
                       "tests": len(self.tests)},
        }

    def render(self, *, limit: int = 12) -> str:
        def block(title: str, items: Sequence[str]) -> list[str]:
            if not items:
                return []
            shown = [f"    {item}" for item in items[:limit]]
            if len(items) > limit:
                shown.append(f"    … and {len(items) - limit} more")
            return [f"  {title} ({len(items)})", *shown]

        lines = [f"Impact of {', '.join(self.targets) or '(nothing)'}: {self.breadth}"]
        if self.unresolved:
            lines.append(f"  not found: {', '.join(self.unresolved)}")
        lines += block("Used directly by", self.direct)
        lines += block("Reached through imports", self.indirect)
        lines += block("Tests", self.tests)
        if self.services:
            lines.append(f"  Services  {', '.join(self.services)}")
        if not (self.direct or self.indirect or self.tests):
            lines.append("  Nothing else in the repository depends on it.")
        return "\n".join(lines)


class ProjectGraph:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.files: dict[str, dict] = {}          # path -> {mtime, size, language, imports, symbols}
        self.services: dict[str, str] = {}        # name -> directory ("" for the root)
        self.refs: dict[str, list[str]] = {}      # symbol name -> files mentioning it
        self.defs: dict[str, list[str]] = {}      # symbol name -> files defining it
        self.built_at = 0.0

    # ── building ───────────────────────────────────────────────────────────

    def build(self, previous: Optional["ProjectGraph"] = None) -> "ProjectGraph":
        repo = RepoMap(self.root).build()
        prior = previous.files if previous is not None else {}
        modules = _module_index(repo.files)
        files: dict[str, dict] = {}
        for path, entry in repo.files.items():
            cached = prior.get(path)
            if cached and cached.get("mtime") == entry.mtime and cached.get("size") == entry.size:
                imports = cached.get("imports") or []
            else:
                imports = _imports(self.root, path, entry.language, modules, repo.files)
            files[path] = {
                "mtime": entry.mtime, "size": entry.size, "language": entry.language,
                "imports": sorted(set(imports) - {path}),
                "symbols": [[s.name, s.kind, s.line, s.parent] for s in entry.symbols],
            }
        self.files = files
        self.defs = {name: sorted({path for path, _symbol in places}) for name, places in repo.defs.items()}
        self.refs = {name: sorted(paths) for name, paths in repo.refs.items()
                     if len(name) >= _MIN_NAME and len(self.defs.get(name, ())) <= _MAX_DEFINING_FILES}
        self.services = _services(self.root, files)
        self.built_at = time.time()
        return self

    def is_current(self) -> bool:
        """Every saved file still at its size and mtime, and no new ones."""
        repo = RepoMap(self.root)
        seen = 0
        for path in repo._walk():
            try:
                rel = str(path.relative_to(self.root))
                stat = path.stat()
            except (OSError, ValueError):
                return False
            if stat.st_size > repo.max_file_bytes:
                continue
            saved = self.files.get(rel)
            if saved is None or saved.get("mtime") != stat.st_mtime or saved.get("size") != stat.st_size:
                return False
            seen += 1
        return seen == len(self.files)

    # ── persistence ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"version": GRAPH_VERSION, "root": str(self.root), "built_at": self.built_at,
                "files": self.files, "services": self.services, "refs": self.refs, "defs": self.defs}

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectGraph":
        graph = cls(data["root"])
        graph.files = dict(data.get("files") or {})
        graph.services = dict(data.get("services") or {})
        graph.refs = dict(data.get("refs") or {})
        graph.defs = dict(data.get("defs") or {})
        graph.built_at = float(data.get("built_at") or 0.0)
        return graph

    # ── edges ──────────────────────────────────────────────────────────────

    def importers(self) -> dict[str, set[str]]:
        reverse: dict[str, set[str]] = {}
        for path, info in self.files.items():
            for target in info.get("imports") or ():
                reverse.setdefault(target, set()).add(path)
        return reverse

    def service_of(self, path: str) -> Optional[str]:
        best, best_len = None, -1
        for name, directory in self.services.items():
            if directory == "" or path == directory or path.startswith(directory.rstrip("/") + "/"):
                if len(directory) > best_len:
                    best, best_len = name, len(directory)
        return best

    def _is_hub(self, path: str) -> bool:
        return (Path(path).name == "__init__.py"
                or len(self.files.get(path, {}).get("imports") or ()) > _HUB_FAN_OUT)

    def _symbol_users(self, dotted: str) -> tuple[set[str], set[str]]:
        """(defining files, mentioning files) for ``name`` or ``Class.method``."""
        parts = [part for part in dotted.split(".") if part]
        if not parts:
            return set(), set()
        defining = set(self.defs.get(parts[-1], ()))
        users = set(self.refs.get(parts[-1], ()))
        for outer in parts[:-1]:
            users &= set(self.refs.get(outer, ()))
            defining &= set(self.defs.get(outer, ())) or defining
        return defining, users

    def _file_users(self, path: str) -> set[str]:
        users = set(self.importers().get(path, ()))
        for name, _kind, _line, parent in self.files.get(path, {}).get("symbols") or ():
            if parent:
                continue  # a method is reached through its class
            if path in self.defs.get(name, ()) and name in self.refs:
                users |= set(self.refs[name])
        users.discard(path)
        return users

    def _resolve(self, target: str) -> Optional[str]:
        raw = target.strip()
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            try:
                raw = str(candidate.resolve().relative_to(self.root))
            except ValueError:
                return None
        raw = raw.removeprefix("./")
        if raw in self.files:
            return raw
        matches = [path for path in self.files if path.endswith("/" + raw)]
        return matches[0] if len(matches) == 1 else None

    # ── impact ─────────────────────────────────────────────────────────────

    def impact(self, targets: Iterable[str]) -> Impact:
        wanted = [str(t).strip() for t in targets if str(t).strip()]
        result = Impact(targets=wanted)
        origin: set[str] = set()
        changed_files: set[str] = set()   # walked from; a symbol's file is not
        direct: set[str] = set()
        for target in wanted:
            path = self._resolve(target)
            if path is not None:
                origin.add(path)
                changed_files.add(path)
                direct |= self._file_users(path)
                continue
            # Importing the file that defines a symbol is not using it: only
            # files that name it are affected, and the walk starts from them.
            defining, users = self._symbol_users(target)
            if defining or users:
                origin |= defining
                direct |= users
            else:
                result.unresolved.append(target)
        direct -= origin

        importers = self.importers()
        indirect: set[str] = set()
        frontier = deque((path, 1) for path in direct | changed_files)
        visited = set(direct | origin)
        while frontier:
            path, depth = frontier.popleft()
            if depth > _TRANSITIVE_DEPTH or (path not in changed_files and self._is_hub(path)):
                continue
            for parent in importers.get(path, ()):
                if parent not in visited:
                    visited.add(parent)
                    indirect.add(parent)
                    frontier.append((parent, depth + 1))

        reached = origin | direct | indirect
        result.direct = sorted(direct)
        result.indirect = sorted(indirect - direct)
        result.tests = sorted(path for path in (direct | indirect) if is_test_path(path))
        result.services = sorted({s for s in (self.service_of(p) for p in reached) if s})
        return result

    def summary(self) -> dict:
        edges = sum(len(info.get("imports") or ()) for info in self.files.values())
        return {"files": len(self.files), "tests": sum(1 for p in self.files if is_test_path(p)),
                "import_edges": edges, "symbols": len(self.defs), "services": dict(self.services),
                "built_at": self.built_at}


# ── imports ────────────────────────────────────────────────────────────────

def _module_index(files) -> dict[str, list[str]]:
    """Dotted module names, every suffix of the path, mapped to Python files.

    ``src/aria_code/runtime/agent.py`` is ``agent``, ``runtime.agent``,
    ``aria_code.runtime.agent`` and ``src.aria_code.runtime.agent``: which of
    those an import uses depends on what the project puts on its path.
    """
    index: dict[str, list[str]] = {}
    for path, entry in files.items():
        if entry.language != "python":
            continue
        parts = list(Path(path).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        for start in range(len(parts)):
            index.setdefault(".".join(parts[start:]), []).append(path)
    return index


def _pick(candidates: Sequence[str], importer: str) -> Optional[str]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Several files answer to a short name: prefer the one sharing most of the
    # importer's directory.
    importer_parts = Path(importer).parts
    def shared(path: str) -> int:
        count = 0
        for a, b in zip(Path(path).parts, importer_parts):
            if a != b:
                break
            count += 1
        return count
    ranked = sorted(candidates, key=shared, reverse=True)
    return ranked[0] if shared(ranked[0]) > shared(ranked[1]) else None


def _python_imports(root: Path, path: str, modules: dict) -> list[str]:
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError, RecursionError):
        return []
    package = list(Path(path).parent.parts)
    found: list[str] = []

    def resolve(name: str) -> Optional[str]:
        return _pick(modules.get(name, ()), path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = resolve(alias.name)
                if hit:
                    found.append(hit)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            for alias in node.names:
                hit = resolve(f"{base}.{alias.name}" if base else alias.name) or (resolve(base) if base else None)
                if hit:
                    found.append(hit)
    return found


def _js_imports(root: Path, path: str, files) -> list[str]:
    try:
        source = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    folder = Path(path).parent
    for spec in _JS_IMPORT.findall(source):
        base = os.path.normpath(str(folder / spec))
        candidates = [base, *(base + ext for ext in _JS_EXTENSIONS),
                      *(f"{base}/index{ext}" for ext in _JS_EXTENSIONS)]
        hit = next((c for c in candidates if c in files), None)
        if hit:
            found.append(hit)
    return found


def _imports(root: Path, path: str, language: str, modules: dict, files) -> list[str]:
    if language == "python":
        return _python_imports(root, path, modules)
    if path.endswith(_JS_EXTENSIONS):
        return _js_imports(root, path, files)
    return []


# ── services ───────────────────────────────────────────────────────────────

def _services(root: Path, files: dict) -> dict[str, str]:
    services: dict[str, str] = {}
    for name in _COMPOSE_FILES:
        compose = root / name
        if not compose.is_file():
            continue
        try:
            import yaml

            data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for service, spec in (data.get("services") or {}).items():
            build = (spec or {}).get("build") if isinstance(spec, dict) else None
            context = build.get("context") if isinstance(build, dict) else build
            if isinstance(context, str):
                directory = os.path.normpath(context)
                services[str(service)] = "" if directory in (".", "") else directory
    directories = {str(parent) for p in files for parent in Path(p).parents}
    for directory in sorted(directories):
        if directory in (".", "") or directory in services.values():
            continue
        if any((root / directory / marker).is_file() for marker in _PACKAGE_MARKERS):
            services.setdefault(Path(directory).name, directory)
    return services


# ── loading ────────────────────────────────────────────────────────────────

def _graph_file(root: Path) -> Path:
    from ..packages.aria_core.paths import aria_home

    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return aria_home() / "graphs" / f"{root.name}-{digest}.json"


_CACHE: dict[str, ProjectGraph] = {}


def load_project_graph(root: Path | str = ".", *, refresh: bool = True) -> ProjectGraph:
    """The graph for ``root``: from memory, then disk, rebuilt when stale."""
    resolved = Path(root).expanduser().resolve()
    key = str(resolved)
    graph = _CACHE.get(key)
    path = _graph_file(resolved)
    if graph is None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == GRAPH_VERSION and data.get("root") == key:
                graph = ProjectGraph.from_dict(data)
        except (OSError, ValueError, KeyError, TypeError):
            graph = None
    if graph is not None and (not refresh or graph.is_current()):
        _CACHE[key] = graph
        return graph
    fresh = ProjectGraph(resolved).build(previous=graph)
    _CACHE[key] = fresh
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(fresh.to_dict()), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass
    return fresh


def clear_cache() -> None:
    _CACHE.clear()


def cached_graph(root: Path | str) -> Optional[ProjectGraph]:
    """The graph already in memory or on disk; never builds one.

    For approval cards and reports, which must not stall on a first scan.
    It may be a little stale, which is fine for a hint.
    """
    resolved = Path(root).expanduser().resolve()
    graph = _CACHE.get(str(resolved))
    if graph is not None:
        return graph
    try:
        data = json.loads(_graph_file(resolved).read_text(encoding="utf-8"))
        if data.get("version") == GRAPH_VERSION and data.get("root") == str(resolved):
            graph = ProjectGraph.from_dict(data)
            _CACHE[str(resolved)] = graph
            return graph
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _repository_of(path: Path) -> Optional[tuple[Path, str]]:
    """``(repository root, path inside it)`` — a task worktree maps to its repository."""
    import subprocess

    folder = path if path.is_dir() else path.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=folder,
                             capture_output=True, text=True, timeout=5, check=True).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        out = []
    if len(out) < 2:
        # Not a git repository: the nearest folder that has a graph.
        for parent in [folder, *folder.parents]:
            if str(parent.resolve()) in _CACHE or _graph_file(parent.resolve()).is_file():
                try:
                    return parent.resolve(), str(path.resolve().relative_to(parent.resolve()))
                except ValueError:
                    return None
        return None
    top = Path(out[0]).resolve()
    common = Path(out[1])
    common = (folder / common).resolve() if not common.is_absolute() else common.resolve()
    origin = common.parent if common.name == ".git" else top
    try:
        return origin, str(path.resolve().relative_to(top))
    except ValueError:
        return None


def impact_of_paths(paths: Sequence[str]) -> Optional[Impact]:
    """Impact of changing these absolute paths, from a cached graph only."""
    targets: list[str] = []
    graph: Optional[ProjectGraph] = None
    for raw in paths:
        located = _repository_of(Path(raw))
        if located is None:
            continue
        root, relative = located
        candidate = cached_graph(root)
        if candidate is None or (graph is not None and candidate.root != graph.root):
            continue
        graph = candidate
        targets.append(relative)
    if graph is None or not targets:
        return None
    return graph.impact(targets)


def impact_line(impact: Optional[Impact]) -> str:
    """One line for a card or report: ``9 files · 7 tests · api``."""
    if impact is None or not impact.targets or impact.unresolved == impact.targets:
        return ""
    reach = len(impact.direct) + len(impact.indirect)
    parts = [f"{impact.breadth}", f"{reach} file{'s' if reach != 1 else ''} depend on it",
             f"{len(impact.tests)} test{'s' if len(impact.tests) != 1 else ''}"]
    if impact.services:
        parts.append(", ".join(impact.services))
    return " · ".join(parts)


# ── model-facing tool ──────────────────────────────────────────────────────

def tool_impact_analysis(params: dict) -> dict:
    """What a change to these files or symbols reaches, before making it."""
    try:
        targets = params.get("targets") or params.get("target") or []
        if isinstance(targets, str):
            targets = [targets]
        targets = [str(t) for t in targets if str(t).strip()]
        if not targets:
            return {"success": False, "error": "Missing 'targets': file paths or symbol names"}
        graph = load_project_graph(params.get("path") or ".")
        impact = graph.impact(targets)
        return {"success": True, "data": {**impact.as_dict(), "report": impact.render()}}
    except Exception as exc:
        return {"success": False, "error": f"impact_analysis failed: {exc}"}


PROJECT_GRAPH_TOOLS = {
    "impact_analysis": (tool_impact_analysis,
                        "What depends on a file or symbol: callers, importers, tests, services"),
}

PROJECT_GRAPH_SCHEMAS = [
    {
        "name": "impact_analysis",
        "description": (
            "Before editing, find what a change would reach. Give file paths and/or symbol "
            "names (`Class.method` for a method). Returns the files that use them directly, "
            "the files reached through imports (up to three hops), the tests that cover any "
            "of those, the services (compose services, packages) that ship them, and a "
            "breadth rating. Unknown targets are listed as not found rather than guessed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repository-relative paths or symbol names, e.g. src/auth/session.py, Session.refresh",
                },
                "path": {"type": "string", "description": "Repository root (default: current directory)"},
            },
            "required": ["targets"],
        },
    },
]
