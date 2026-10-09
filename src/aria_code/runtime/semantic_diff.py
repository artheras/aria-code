"""Semantic diff — which definitions a change touched, not which lines.

"+43 -18 in session.py" says how much changed. What a reviewer reads first is
*what*: ``refresh_session()`` was modified, ``SessionManager`` added,
``legacy_refresh()`` removed. This module works that out per file, in any
language ``repo_map`` can parse (Python by AST, the rest by pattern):

1. Read the file as it is now.
2. Undo this turn's diffs, newest first, to get it as it was. A diff that does
   not apply — the file was changed some other way in between — means no
   answer for that file rather than a wrong one.
3. Extract definitions from both versions and compare each one's text.

A definition counts as modified when its own text changed; a class whose only
changes are inside its methods reports the methods, not itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class SymbolChange:
    kind: str           # "added" | "modified" | "removed"
    name: str           # qualified: "SessionManager.refresh"
    symbol_kind: str    # "def", "class", "function", …

    def label(self) -> str:
        callable_kinds = {"def", "async def", "function", "method", "func", "fn"}
        suffix = "()" if self.symbol_kind in callable_kinds else ""
        return f"{self.name}{suffix} {self.kind}"


def reverse_apply(after: str, diff: str) -> Optional[str]:
    """The text *diff* was made from, given the text it produced. None if it does not fit."""
    lines = after.splitlines(keepends=True)
    hunks = []
    current = None
    for raw in diff.splitlines(keepends=True):
        match = _HUNK.match(raw)
        if match:
            current = {"new_start": int(match.group(3)), "old": [], "new": []}
            hunks.append(current)
            continue
        if current is None or raw.startswith(("---", "+++", "\\")):
            continue
        tag, body = raw[:1], raw[1:]
        if tag == " ":
            current["old"].append(body)
            current["new"].append(body)
        elif tag == "-":
            current["old"].append(body)
        elif tag == "+":
            current["new"].append(body)
        elif raw.strip() == "":   # a context line whose leading space was trimmed
            current["old"].append(raw)
            current["new"].append(raw)
    if not hunks:
        return None
    for hunk in reversed(hunks):
        start = max(0, hunk["new_start"] - 1) if hunk["new"] else hunk["new_start"]
        end = start + len(hunk["new"])
        if [l.rstrip("\r\n") for l in lines[start:end]] != [l.rstrip("\r\n") for l in hunk["new"]]:
            return None
        lines[start:end] = hunk["old"]
    return "".join(lines)


def _bodies(source: str, language: str) -> dict:
    """qualified name → (kind, text) for each definition."""
    from .repo_map import extract_symbols

    symbols = sorted(extract_symbols(source, language), key=lambda s: s.line)
    lines = source.splitlines()
    out = {}
    for index, symbol in enumerate(symbols):
        # A definition runs to the next one at the same or an outer level.
        end = len(lines)
        for later in symbols[index + 1:]:
            if later.parent != symbol.qualified and not later.parent.startswith(symbol.qualified + "."):
                end = later.line - 1
                break
        children = {s.line for s in symbols[index + 1:] if s.parent == symbol.qualified}
        own = [line for number, line in enumerate(lines[symbol.line - 1:end], start=symbol.line)
               if not _inside_child(number, children, symbols)]
        # Blank and comment-only lines at the end belong to whatever follows;
        # counting them made a comment above the next definition "modify"
        # the one before it.
        while own and (not own[-1].strip() or own[-1].lstrip().startswith(("#", "//"))):
            own.pop()
        out[symbol.qualified] = (symbol.kind, "\n".join(own))
    return out


def _inside_child(number: int, child_lines: set, symbols: Sequence) -> bool:
    if not child_lines:
        return False
    starts = sorted(child_lines)
    for start in starts:
        nxt = next((s.line for s in symbols if s.line > start), None)
        if start <= number < (nxt or 10 ** 9):
            return True
    return False


def _parent(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else ""


def symbol_changes(before: str, after: str, language: str) -> list[SymbolChange]:
    old, new = _bodies(before, language), _bodies(after, language)
    changes = []
    for name, (kind, text) in new.items():
        if name not in old:
            parent = _parent(name)
            if parent and parent in new and parent not in old:
                continue  # inside an added class: the class says it
            changes.append(SymbolChange("added", name, kind))
        elif old[name][1] != text:
            changes.append(SymbolChange("modified", name, kind))
    for name, (kind, _) in old.items():
        if name not in new:
            parent = _parent(name)
            if parent and parent in old and parent not in new:
                continue  # inside a removed class
            changes.append(SymbolChange("removed", name, kind))
    order = {"added": 0, "modified": 1, "removed": 2}
    return sorted(changes, key=lambda c: (order[c.kind], c.name))


def file_symbol_changes(path: str | Path, diffs: Iterable[str]) -> Optional[list[SymbolChange]]:
    """What this turn's *diffs* (oldest first) did to the definitions in *path*. None if unknown."""
    from .repo_map import LANGUAGES

    path = Path(path)
    language = LANGUAGES.get(path.suffix.lower(), "")
    if not language:
        return None
    try:
        after = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    before = after
    for diff in reversed(list(diffs)):
        if "--- /dev/null" in diff:
            before = ""
            break
        previous = reverse_apply(before, diff)
        if previous is None:
            return None
        before = previous
    return symbol_changes(before, after, language)


__all__ = ["SymbolChange", "file_symbol_changes", "reverse_apply", "symbol_changes"]
