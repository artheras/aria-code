"""Edit a definition by name — ``edit_file`` with ``symbol`` instead of ``old_string``.

``edit_file`` replaces an exact string, so changing a function meant reading
the file and copying the old function back character for character. A
missed space, a stale read, and the edit fails or — worse — lands on the
wrong of two similar blocks. Naming the definition is shorter and exact:

    edit_file(path="src/auth/session.py", symbol="Session.refresh",
              new_string="def refresh(self):\\n    ...")

    edit_file(path="src/auth/session.py", symbol="Session.refresh",
              position="after", new_string="def close(self):\\n    ...")

The definition's whole text — signature, body, nested definitions, up to the
next definition at its level, minus trailing blank and comment lines — is
found with repo_map's extractor and becomes ``old_string``. Everything else
is ordinary ``edit_file``: the same approval, preview, checkpoint, diff,
contract check and acceptance gate, because it is the same tool. Decorators
above a ``def`` are not part of the definition and stay as they are.

New code is re-indented to the definition's level when it comes in less
indented, so a method can be written flush-left.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

POSITIONS = ("replace", "after")


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(code: str, indent: str) -> str:
    lines = code.strip("\n").splitlines()
    present = [_indent(l) for l in lines if l.strip()]
    if not present or not indent:
        return "\n".join(lines)
    current = min(present, key=len)
    if len(current) >= len(indent):
        return "\n".join(lines)
    extra = indent[len(current):]
    return "\n".join(extra + l if l.strip() else l for l in lines)


def definition_text(source: str, language: str, symbol: str) -> tuple[Optional[str], str]:
    """(the definition's text, "") or (None, why not)."""
    from .repo_map import extract_symbols

    symbols = sorted(extract_symbols(source, language), key=lambda s: s.line)
    matches = [s for s in symbols if s.qualified == symbol] or [s for s in symbols if s.name == symbol]
    if not matches:
        known = ", ".join(s.qualified for s in symbols[:20]) or "none found"
        return None, f"no definition named '{symbol}' (this file defines: {known})"
    if len(matches) > 1:
        names = ", ".join(f"{s.qualified} (line {s.line})" for s in matches)
        return None, f"'{symbol}' names {len(matches)} definitions: {names}; use the qualified name"
    target = matches[0]
    lines = source.splitlines()
    end = len(lines)
    for later in symbols:
        if later.line <= target.line:
            continue
        inside = later.parent == target.qualified or later.parent.startswith(target.qualified + ".")
        if not inside:
            end = later.line - 1
            break
    body = lines[target.line - 1:end]
    while body and (not body[-1].strip() or body[-1].lstrip().startswith(("#", "//"))):
        body.pop()
    return "\n".join(body), ""


def resolve_symbol_edit(params: dict) -> Optional[str]:
    """Turn ``symbol``/``position`` into ``old_string``/``new_string`` in place.

    Returns an error message, or None when the params are ready (or were not a
    symbol edit at all). Safe to call twice: once resolved, it does nothing.
    """
    symbol = str(params.get("symbol") or "").strip()
    if not symbol or params.get("old_string") or params.get("old_str"):
        return None
    position = str(params.get("position") or "replace").strip().lower()
    if position not in POSITIONS:
        return f"position must be one of: {', '.join(POSITIONS)}"
    new_code = params.get("new_string", params.get("new_str"))
    if not isinstance(new_code, str) or not new_code.strip():
        return "new_string is required with symbol: the new definition, or the code to insert"
    from .repo_map import LANGUAGES

    path = Path(str(params.get("path") or "")).expanduser()
    language = LANGUAGES.get(path.suffix.lower(), "")
    if not language:
        return f"symbol edits need a source file repo_map can parse; {path.name} is not one"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot read {path}: {exc}"
    text, why = definition_text(source, language, symbol)
    if text is None:
        return why
    indent = _indent(text.splitlines()[0])
    code = _reindent(new_code, indent)
    if position == "replace":
        params["new_string"] = code
    else:
        gap = "\n\n\n" if not indent and language == "python" else "\n\n"
        params["new_string"] = text + gap + code
    params["old_string"] = text
    params.pop("new_str", None)
    return None


__all__ = ["POSITIONS", "definition_text", "resolve_symbol_edit"]
