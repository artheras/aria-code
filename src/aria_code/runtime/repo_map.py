"""Repo map — what is defined where, ranked by what the rest of the repo needs.

Why this exists
---------------
Before this module, Aria's only way to find anything in an unfamiliar repository
was to guess a string and grep for it.  That works in a 20-file project and
falls apart in an enterprise one: the model burns four or five rounds probing
names that do not exist, and a small local model — the configuration this
project is actually built around — never recovers, because it has no prior about
what this codebase calls things.

A file tree does not fix that; it says where files are, not what is in them.
What a model needs on the first round is a *symbol* map: the classes and
functions this repository defines, which file each lives in, and — critically —
which of them the rest of the repository actually depends on.

Ranking is the whole difference
-------------------------------
Every real repo has far more symbols than fit in a context window, so an
unranked dump is just a differently-shaped grep.  This module ranks a file by
how much the *rest of the codebase* reaches into it: for each symbol a file
defines, how many other files mention that symbol.  A widely-referenced module
scores high; a leaf script nothing imports scores near zero.  That ordering is
what makes a budgeted map useful — the first 8 KB describe the parts of the
system a change is most likely to touch.

Three corrections keep that number honest, each for an observed way the naive
count lies:

  - Names under four characters are ignored.  ``id``, ``get``, ``run``, ``fn``
    collide with ordinary vocabulary and would rank whichever file happened to
    define them above everything else.
  - A name defined in more than a handful of files contributes nothing.  A
    ``main`` or ``setup`` defined in thirty places tells you nothing about which
    of the thirty matters.
  - One symbol's contribution is capped, so a single popular helper cannot
    outvote a file that is broadly depended on.

No new dependencies
-------------------
Python is parsed with the stdlib ``ast`` — exact, including nesting.  Every
other language is matched by declaration regex.  This is deliberately less
precise than tree-sitter, and the trade is worth it: an approximate map that
installs everywhere beats an exact one gated behind a native build, because the
map's job is to point the model at the right file, and ``read_file`` is what
establishes the truth once it gets there.
"""

from __future__ import annotations

import ast
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import native_index

__all__ = [
    "REPO_MAP_SCHEMAS",
    "REPO_MAP_TOOLS",
    "RepoMap",
    "Symbol",
    "tool_find_symbol",
    "tool_repo_map",
]


# Directories that are never the user's own code.  Skipped by name at any
# depth: a single unpruned ``.venv`` or ``node_modules`` outweighs the entire
# repository and would dominate both the scan time and the ranking.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".env", "site-packages", "dist", "build", "target", ".next",
    ".nuxt", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".gradle", ".idea", ".vscode", "vendor", "coverage",
    "htmlcov", ".terraform", "Pods", "DerivedData", ".dart_tool",
    "bower_components", "jspm_packages", ".serverless", "out",
})

# Extension → language label.  Membership here is also what decides whether a
# file is scanned at all.
LANGUAGES: Dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".scala": "scala",
    ".sh": "shell", ".bash": "shell",
    ".sql": "sql",
    ".lua": "lua",
    ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir",
}

# Declaration patterns per language.  Each yields (kind, name).  These match
# declarations, not uses — anchored at line start (modulo indentation and
# modifiers) so a call like ``foo(function() {})`` is not read as a definition.
_DECL_PATTERNS: Dict[str, Sequence[Tuple[str, re.Pattern]]] = {
    "javascript": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
    ),
    "go": (
        ("func", re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)")),
        ("type", re.compile(r"^type\s+([A-Za-z_][\w]*)")),
    ),
    "rust": (
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][\w]*)")),
        ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][\w]*)")),
        ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][\w]*)")),
        ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([A-Za-z_][\w]*)")),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:public|private|protected|abstract|final|static|\s)*class\s+([A-Za-z_][\w]*)")),
        ("interface", re.compile(r"^\s*(?:public|private|protected|abstract|\s)*interface\s+([A-Za-z_][\w]*)")),
        ("enum", re.compile(r"^\s*(?:public|private|protected|\s)*enum\s+([A-Za-z_][\w]*)")),
    ),
    "ruby": (
        ("class", re.compile(r"^\s*class\s+([A-Za-z_][\w]*)")),
        ("module", re.compile(r"^\s*module\s+([A-Za-z_][\w]*)")),
        ("def", re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_][\w]*[?!=]?)")),
    ),
    "php": (
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_][\w]*)")),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_][\w]*)")),
    ),
    "csharp": (
        ("class", re.compile(r"^\s*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*class\s+([A-Za-z_][\w]*)")),
        ("interface", re.compile(r"^\s*(?:public|private|protected|internal|\s)*interface\s+([A-Za-z_][\w]*)")),
        ("record", re.compile(r"^\s*(?:public|private|protected|internal|\s)*record\s+([A-Za-z_][\w]*)")),
    ),
    "swift": (
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*class\s+([A-Za-z_][\w]*)")),
        ("struct", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+)?struct\s+([A-Za-z_][\w]*)")),
        ("protocol", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+)?protocol\s+([A-Za-z_][\w]*)")),
        ("func", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|static\s+|override\s+|@\w+\s+)*func\s+([A-Za-z_][\w]*)")),
    ),
    "kotlin": (
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|data\s+|sealed\s+|abstract\s+)*class\s+([A-Za-z_][\w]*)")),
        ("object", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+)?object\s+([A-Za-z_][\w]*)")),
        ("fun", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|suspend\s+|override\s+)*fun\s+(?:<[^>]*>\s*)?([A-Za-z_][\w]*)")),
    ),
    "c": (
        ("struct", re.compile(r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_][\w]*)")),
        ("function", re.compile(r"^[A-Za-z_][\w \*]*\s+\*?([A-Za-z_][\w]*)\s*\([^;]*\)\s*\{")),
    ),
    "scala": (
        ("class", re.compile(r"^\s*(?:case\s+)?class\s+([A-Za-z_][\w]*)")),
        ("object", re.compile(r"^\s*(?:case\s+)?object\s+([A-Za-z_][\w]*)")),
        ("trait", re.compile(r"^\s*trait\s+([A-Za-z_][\w]*)")),
        ("def", re.compile(r"^\s*def\s+([A-Za-z_][\w]*)")),
    ),
    "shell": (
        ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{")),
    ),
    "sql": (
        ("table", re.compile(r"(?i)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([A-Za-z_][\w]*)")),
        ("view", re.compile(r"(?i)^\s*create\s+(?:or\s+replace\s+)?view\s+[`\"\[]?([A-Za-z_][\w]*)")),
        ("function", re.compile(r"(?i)^\s*create\s+(?:or\s+replace\s+)?function\s+[`\"\[]?([A-Za-z_][\w]*)")),
    ),
    "lua": (
        ("function", re.compile(r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)")),
    ),
    "dart": (
        ("class", re.compile(r"^\s*(?:abstract\s+)?class\s+([A-Za-z_][\w]*)")),
    ),
    "elixir": (
        ("defmodule", re.compile(r"^\s*defmodule\s+([A-Za-z_][\w.]*)")),
        ("def", re.compile(r"^\s*defp?\s+([a-z_][\w]*[?!]?)")),
    ),
}
_DECL_PATTERNS["typescript"] = _DECL_PATTERNS["javascript"] + (
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
)
_DECL_PATTERNS["cpp"] = _DECL_PATTERNS["c"] + (
    ("class", re.compile(r"^\s*class\s+([A-Za-z_][\w]*)")),
    ("namespace", re.compile(r"^\s*namespace\s+([A-Za-z_][\w]*)")),
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Ranking guards — see the module docstring for what each one corrects.
_MIN_RANKED_NAME = 4
_MAX_DEFINING_FILES = 5
_MAX_SYMBOL_CONTRIBUTION = 20

# Rendering budget: how many files a map should try to touch, and roughly what
# one symbol line costs. Together these turn a char budget into a per-file
# symbol cap, so breadth survives a small budget.
_TARGET_FILES = 28
_AVG_SYMBOL_LINE = 46


@dataclass(frozen=True)
class Symbol:
    """One definition found in one file."""

    name: str
    kind: str
    line: int
    parent: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass
class FileEntry:
    path: str
    language: str
    symbols: List[Symbol] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0
    score: float = 0.0


def _python_symbols(source: str) -> List[Symbol]:
    """Exact symbols for Python, including one level of class nesting.

    Methods are kept with their class as ``parent`` rather than flattened: a
    map that lists ``run`` without saying it is ``AcceptanceGate.run`` sends
    the model to the wrong file about as often as it helps.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []

    out: List[Symbol] = []

    def _kind(node: ast.AST) -> str:
        if isinstance(node, ast.AsyncFunctionDef):
            return "async def"
        if isinstance(node, ast.FunctionDef):
            return "def"
        return "class"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(Symbol(name=node.name, kind=_kind(node), line=node.lineno))
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append(Symbol(
                            name=child.name, kind=_kind(child),
                            line=child.lineno, parent=node.name,
                        ))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Module-level constants are part of a module's surface: registries
            # like LOCAL_TOOLS and SKIP_DIRS are exactly what a caller looks up.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) > 2:
                    out.append(Symbol(name=target.id, kind="const", line=node.lineno))
    return out


def _regex_symbols(source: str, language: str) -> List[Symbol]:
    patterns = _DECL_PATTERNS.get(language)
    if not patterns:
        return []
    out: List[Symbol] = []
    seen: Set[Tuple[str, int]] = set()
    for lineno, line in enumerate(source.splitlines(), 1):
        if len(line) > 400:  # minified or generated; never a readable declaration
            continue
        for kind, pattern in patterns:
            match = pattern.match(line)
            if match:
                name = match.group(1)
                key = (name, lineno)
                if key not in seen:
                    seen.add(key)
                    out.append(Symbol(name=name, kind=kind, line=lineno))
                break
    return out


def extract_symbols(source: str, language: str) -> List[Symbol]:
    """Definitions in *source*.  Never raises — an unparseable file has none."""
    try:
        if language == "python":
            return _python_symbols(source)
        return _regex_symbols(source, language)
    except Exception:
        return []


class RepoMap:
    """A scanned, ranked symbol index for one repository root."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        max_files: int = 4000,
        max_file_bytes: int = 400_000,
        extra_skip_dirs: Iterable[str] = (),
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_files = max(1, int(max_files))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.skip_dirs = SKIP_DIRS | {str(d) for d in extra_skip_dirs}
        self.files: Dict[str, FileEntry] = {}
        self.defs: Dict[str, List[Tuple[str, Symbol]]] = {}
        self.refs: Dict[str, Set[str]] = {}
        self.built_at: float = 0.0
        self.truncated: bool = False
        self.scan_seconds: float = 0.0

    # ── scanning ──────────────────────────────────────────────────────────

    def _walk(self) -> List[Path]:
        found: List[Path] = []
        stack = [self.root]
        while stack and len(found) < self.max_files:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                name = entry.name
                if name.startswith(".") and name not in (".github",):
                    if entry.is_dir():
                        continue
                try:
                    if entry.is_dir():
                        if name not in self.skip_dirs:
                            stack.append(entry)
                        continue
                    if entry.suffix.lower() in LANGUAGES:
                        found.append(entry)
                        if len(found) >= self.max_files:
                            self.truncated = True
                            break
                except OSError:
                    continue
        return found

    def build(self, *, force: bool = False) -> "RepoMap":
        """Scan the tree.  Unchanged files keep their cached symbols.

        Incremental by mtime and size so a rebuild between turns costs a stat
        per file rather than a reparse — the map is only useful if refreshing
        it is cheaper than the grep round it replaces.
        """
        started = time.time()
        paths = self._walk()
        previous = self.files if not force else {}
        files: Dict[str, FileEntry] = {}
        order: List[str] = []   # walk order, which ranking ties fall back on
        stale: List[Tuple[str, Path, os.stat_result, str]] = []

        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > self.max_file_bytes:
                continue
            try:
                rel = str(path.relative_to(self.root))
            except ValueError:
                rel = str(path)

            order.append(rel)
            cached = previous.get(rel)
            if cached is not None and cached.mtime == stat.st_mtime and cached.size == stat.st_size:
                files[rel] = cached
                continue
            stale.append((rel, path, stat, LANGUAGES.get(path.suffix.lower(), "")))

        # Python definitions come from the native indexer when it can answer
        # (one parallel pass); everything it does not is extracted here.
        native = native_index.python_symbols(
            self.root, [rel for rel, _path, _stat, language in stale if language == "python"])
        parsed = native[0] if native is not None else {}

        for rel, path, stat, language in stale:
            if rel in parsed:
                symbols = [Symbol(name=n, kind=k, line=line, parent=p) for n, k, line, p in parsed[rel]]
            else:
                try:
                    source = path.read_text(encoding="utf-8-sig", errors="replace")
                except (OSError, UnicodeError):
                    continue
                symbols = extract_symbols(source, language)
            files[rel] = FileEntry(path=rel, language=language, symbols=symbols,
                                   mtime=stat.st_mtime, size=stat.st_size)

        self.files = {rel: files[rel] for rel in order if rel in files}
        self._index()
        self.built_at = time.time()
        self.scan_seconds = self.built_at - started
        return self

    def _index(self) -> None:
        """Build the definition table, the reference table, and the ranking."""
        defs: Dict[str, List[Tuple[str, Symbol]]] = {}
        for entry in self.files.values():
            for symbol in entry.symbols:
                defs.setdefault(symbol.name, []).append((entry.path, symbol))
        self.defs = defs

        # References: identifiers a file mentions that some *other* file
        # defines.  Re-reading each file here is what a second pass costs; it
        # is bounded by max_file_bytes and only runs on a real rebuild.
        known = set(defs)
        refs: Dict[str, Set[str]] = {}
        for entry in self.files.values():
            path = self.root / entry.path
            try:
                source = path.read_text(encoding="utf-8-sig", errors="replace")
            except (OSError, UnicodeError):
                continue
            own = {symbol.name for symbol in entry.symbols}
            for token in set(_IDENTIFIER.findall(source)):
                if token in known and token not in own:
                    refs.setdefault(token, set()).add(entry.path)
        self.refs = refs

        for entry in self.files.values():
            entry.score = self._score(entry)

    def _effective_refs(self, name: str) -> int:
        """Reference count, with the two names that always lie zeroed out.

        A name under four characters (``get``, ``run``, ``id``) collides with
        ordinary vocabulary, and a name defined in more than a handful of files
        (``main``, ``setup``, ``status``) cannot tell you which of them is the
        one that matters. Both would otherwise dominate every ranking.
        """
        if len(name) < _MIN_RANKED_NAME:
            return 0
        if len(self.defs.get(name, ())) > _MAX_DEFINING_FILES:
            return 0
        return len(self.refs.get(name, ()))

    def _score(self, entry: FileEntry) -> float:
        total = 0.0
        counted: Set[str] = set()
        for symbol in entry.symbols:
            if symbol.name in counted:
                continue
            counted.add(symbol.name)
            total += min(self._effective_refs(symbol.name), _MAX_SYMBOL_CONTRIBUTION)
        return total

    # ── querying ──────────────────────────────────────────────────────────

    def ranked(self, *, focus: Sequence[str] = ()) -> List[FileEntry]:
        """Files, most depended-upon first.

        ``focus`` promotes paths the caller already knows are relevant (the
        files in the current diff, say) above the global ranking, because what
        the rest of the repo needs and what *this task* needs are different
        questions and the caller often knows the second one.
        """
        wanted = tuple(f for f in focus if f)

        def _is_focused(entry: FileEntry) -> bool:
            return any(token in entry.path for token in wanted)

        return sorted(
            (e for e in self.files.values() if e.symbols),
            key=lambda e: (0 if _is_focused(e) else 1, -e.score, e.path),
        )

    def notable(self, entry: FileEntry, limit: int) -> List[Symbol]:
        """The *limit* symbols in this file worth naming, in source order.

        Two rules, in order. Top-level definitions come before methods: a class
        name is the handle a caller navigates by, and three of its methods say
        less about the file than the class itself does. Within a level, the
        most-referenced name wins.

        Listing a file's first N definitions instead — the obvious
        implementation — is what makes a map read like noise: the top of a
        large module is usually private constants and local helpers, so the
        budget gets spent on the exact symbols no outside caller will ever
        ask about.
        """
        if len(entry.symbols) <= limit:
            return list(entry.symbols)
        ranked = sorted(
            entry.symbols,
            key=lambda s: (
                bool(s.parent),
                s.name.startswith("_"),
                -self._effective_refs(s.name),
                s.line,
            ),
        )[:limit]
        return sorted(ranked, key=lambda s: s.line)

    def render(self, *, budget_chars: int = 8000, focus: Sequence[str] = ()) -> str:
        """The map, as compact text, cut to fit *budget_chars*."""
        if not self.files:
            return "(repo map empty — no source files found)"

        header = (
            f"# Repo map — {self.root.name}\n"
            f"# {len(self.files)} files indexed, "
            f"{sum(len(e.symbols) for e in self.files.values())} symbols, "
            f"ranked by how much the rest of the repo references them.\n"
            f"# Use read_file for the actual code; this map only says where to look.\n"
        )
        if self.truncated:
            header += f"# (truncated at {self.max_files} files)\n"

        blocks: List[str] = []
        used = len(header)
        shown = 0
        entries = self.ranked(focus=focus)

        # Spread the budget across files instead of letting the first one eat
        # it. A map that covers thirty files shallowly is navigable; one that
        # covers a single file exhaustively is a worse `read_file`.
        per_file = max(3, min(12, budget_chars // (_TARGET_FILES * _AVG_SYMBOL_LINE)))

        for entry in entries:
            lines = [f"\n{entry.path}"]
            listed = self.notable(entry, per_file)
            for symbol in listed:
                lines.append(f"  {symbol.line:>5}  {symbol.kind} {symbol.qualified}")
            if len(entry.symbols) > len(listed):
                lines.append(f"         … +{len(entry.symbols) - len(listed)} more")
            block = "\n".join(lines)
            if used + len(block) > budget_chars:
                break
            blocks.append(block)
            used += len(block)
            shown += 1

        footer = ""
        if shown < len(entries):
            footer = (
                f"\n\n… {len(entries) - shown} lower-ranked files omitted. "
                "Use find_symbol(name) to locate anything not listed here."
            )
        return header + "".join(blocks) + footer

    def find_symbol(self, name: str, *, limit: int = 20) -> dict:
        """Where *name* is defined and which files mention it.

        This is the call that replaces a speculative grep: it answers "does
        this repo have such a thing, and where" in one round instead of three.
        """
        query = (name or "").strip()
        if not query:
            return {"symbol": "", "definitions": [], "referenced_by": [], "error": "empty name"}

        definitions = [
            {"file": path, "line": symbol.line, "kind": symbol.kind, "name": symbol.qualified}
            for path, symbol in self.defs.get(query, ())
        ]

        if not definitions:
            # An exact miss is the common case for a half-remembered name, so
            # answer with the near misses rather than an empty result.
            lowered = query.lower()
            near = sorted(
                (n for n in self.defs if lowered in n.lower()),
                key=lambda n: (len(n), n),
            )[:limit]
            return {
                "symbol": query,
                "definitions": [],
                "referenced_by": [],
                "did_you_mean": near,
            }

        return {
            "symbol": query,
            "definitions": definitions[:limit],
            "referenced_by": sorted(self.refs.get(query, ()))[:limit],
            "reference_count": len(self.refs.get(query, ())),
        }

    def summary(self) -> dict:
        return {
            "root": str(self.root),
            "files": len(self.files),
            "symbols": sum(len(e.symbols) for e in self.files.values()),
            "truncated": self.truncated,
            "scan_seconds": round(self.scan_seconds, 3),
        }


# ── tool surface ──────────────────────────────────────────────────────────
# One cached map per root: rebuilding is incremental, so a repeated call in the
# same session costs a stat per file rather than a full reparse.

_CACHE: Dict[str, RepoMap] = {}


def get_repo_map(root: str | Path = ".", *, refresh: bool = True) -> RepoMap:
    key = str(Path(root).expanduser().resolve())
    existing = _CACHE.get(key)
    if existing is None:
        existing = RepoMap(key)
        _CACHE[key] = existing
        return existing.build()
    if refresh:
        existing.build()
    return existing


def clear_cache() -> None:
    _CACHE.clear()


def tool_repo_map(params: dict) -> dict:
    """Model-facing: an overview of what this repository defines."""
    try:
        root = params.get("path") or "."
        budget = int(params.get("budget_chars") or 8000)
        focus = params.get("focus") or []
        if isinstance(focus, str):
            focus = [focus]
        repo = get_repo_map(root)
        return {
            "success": True,
            "data": {
                "map": repo.render(budget_chars=max(500, min(budget, 40000)), focus=focus),
                **repo.summary(),
            },
        }
    except Exception as exc:
        return {"success": False, "error": f"repo_map failed: {exc}"}


def tool_find_symbol(params: dict) -> dict:
    """Model-facing: where one named thing is defined, and who uses it."""
    try:
        name = str(params.get("name") or params.get("symbol") or "").strip()
        if not name:
            return {"success": False, "error": "Missing 'name' parameter"}
        repo = get_repo_map(params.get("path") or ".")
        return {"success": True, "data": repo.find_symbol(name)}
    except Exception as exc:
        return {"success": False, "error": f"find_symbol failed: {exc}"}


REPO_MAP_TOOLS = {
    "repo_map": (tool_repo_map,
                 "Ranked map of what this repository defines and where"),
    "find_symbol": (tool_find_symbol,
                    "Locate a class/function by name and see which files reference it"),
}

REPO_MAP_SCHEMAS = [
    {
        "name": "repo_map",
        "description": (
            "Get a ranked map of the classes, functions and constants this repository "
            "defines, with file paths and line numbers. Files are ordered by how much "
            "the rest of the codebase references them, so the top of the map is where "
            "a change most likely belongs. CALL THIS FIRST when working in an "
            "unfamiliar repository — it replaces several rounds of guessing filenames "
            "and grepping. Then use read_file on the files it points to."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository root (default: current directory)"},
                "budget_chars": {"type": "integer", "description": "Max size of the returned map (default 8000)"},
                "focus": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Path fragments to list first, e.g. files you are already editing",
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_symbol",
        "description": (
            "Find where a class, function or constant is defined and which files "
            "reference it. Use this instead of guessing a path or grepping for a name: "
            "it returns exact file:line definitions, and near-miss suggestions when the "
            "name does not exist, so a wrong guess costs one round instead of four."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to look up"},
                "path": {"type": "string", "description": "Repository root (default: current directory)"},
            },
            "required": ["name"],
        },
    },
]
