"""Stateless file / code search tools — thin wrappers around WorkspaceFiles.

These functions are pure: they take a params dict, call _get_workspace_files(), and
return a result dict. No console/HAS_RICH/write-policy state involved.

They are registered in aria_cli.py's execute_aria_tool dispatch table via:

    from aria_code.apps.cli.tools.file_tools import tool_read_file, tool_list_files, ...

    "read_file":   tool_read_file,
    "list_files":  tool_list_files,
    "search_code": tool_search_code,
    "glob":        tool_glob,
"""

from __future__ import annotations

import pathlib
import sys
import os

# WorkspaceFiles lives at the aria-code root; insert it when running tests
_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # aria-code/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aria_code.workspace import WorkspaceFiles  # noqa: E402
from aria_code.workspace.files import WorkspaceSecurity
import os

def _get_workspace_files(params=None):
    """Reads for the local CLI: cwd, home, and the temp dirs.

    ``allow_home=False`` used to be hardcoded here to "force restrict to the
    current directory". It does not do that. WorkspaceSecurity only appends the
    temp roots on the allow_home branch, so the flag additionally removed /tmp
    and /var/folders — and since Python's tempfile lives under /var/folders on
    macOS, the CLI could write a file into its own scratch directory and then
    be denied reading it back.

    Confinement for remote workers is the same documented mechanism used
    everywhere else: ARIA_RUNTIME_SCOPE=remote, which Dockerfile.review sets.
    Leaving the flag unset here honours it without disabling the local paths.
    """
    security = WorkspaceSecurity.from_tool_params(params or {})
    return WorkspaceFiles(security=security)



def tool_read_file(params: dict) -> dict:
    """Read file contents with optional line range."""
    path = params.get("path", "")
    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    try:
        offset = int(params.get("offset", 0) or 0)
        limit  = int(params.get("limit",  0) or 0)
        if not offset and not limit:
            limit = 160
        result = _get_workspace_files(params).read_file(path, offset=offset, limit=limit)
        content = result.content
        if limit and result.lines >= limit and "use offset/limit to read more" not in content:
            content += "\n... [default read limit applied — use offset/limit to read more]"
        return {"success": True, "data": {
            "path":    result.path,
            "lines":   result.lines,
            "content": content,
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_list_files(params: dict) -> dict:
    """List files in a directory, optionally matching a glob pattern."""
    path    = params.get("path", ".")
    pattern = params.get("pattern", "*")
    try:
        data = _get_workspace_files(params).list_files(path, pattern)
        return {"success": True, "data": {
            "path":    data["path"],
            "pattern": data["pattern"],
            "count":   data["count"],
            "items":   data["items"],
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_search_code(params: dict) -> dict:
    """Search for a pattern in files (like grep)."""
    pattern   = params.get("pattern", "")
    path      = params.get("path", ".")
    file_glob = params.get("glob", "**/*.py")
    if not pattern:
        return {"success": False, "error": "Missing 'pattern' parameter"}
    try:
        data = _get_workspace_files(params).search_code(pattern, path, file_glob)
        return {"success": True, "data": {
            "pattern": data["pattern"],
            "path":    data["path"],
            "count":   data["count"],
            "matches": data["matches"],
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_glob(params: dict) -> dict:
    """Fast file-pattern search (supports ** recursive globs).

    Returns a flat sorted list of matching file paths up to *limit* entries.
    """
    pattern = params.get("pattern", "**/*")
    root    = (params.get("path", ".") or ".").strip()
    limit   = min(int(params.get("limit", 200)), 1000)
    try:
        security = WorkspaceSecurity.from_tool_params(params)
        p = security.require_safe(root)
        if not p.is_dir():
            return {"success": False, "error": f"Directory not found: {p}"}
        results = sorted(
            str(fp.relative_to(p) if fp.is_relative_to(p) else fp)
            for fp in p.glob(pattern)
            if fp.is_file() and security.is_safe_path(fp)
        )[:limit]
        return {"success": True, "data": {
            "pattern": pattern,
            "root":    str(p),
            "count":   len(results),
            "files":   results,
        }}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_lsp_hover(params: dict) -> dict:
    """Get hover documentation (LSP) for a symbol at a specific line and column."""
    path = params.get("path", "")
    line = params.get("line", 1)
    col = params.get("col", 1)
    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    try:
        from aria_code.runtime.lsp import get_hover
        res = get_hover(path, int(line), int(col))
        if not res:
            return {"success": False, "error": f"No hover info available at {path}:{line}:{col} (Is the language server installed?)"}
        return {"success": True, "data": {"hover": res}}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_lsp_definition(params: dict) -> dict:
    """Get definition locations (LSP) for a symbol at a specific line and column."""
    path = params.get("path", "")
    line = params.get("line", 1)
    col = params.get("col", 1)
    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    try:
        from aria_code.runtime.lsp import get_definition
        res = get_definition(path, int(line), int(col))
        if not res:
            return {"success": False, "error": f"No definitions found at {path}:{line}:{col} (Is the language server installed?)"}
        return {"success": True, "data": {"definitions": res}}
    except Exception as e:
        return {"success": False, "error": str(e)}

def tool_lsp_references(params: dict) -> dict:
    """Get all references (LSP) for a symbol at a specific line and column."""
    path = params.get("path", "")
    line = params.get("line", 1)
    col = params.get("col", 1)
    if not path:
        return {"success": False, "error": "Missing 'path' parameter"}
    try:
        from aria_code.runtime.lsp import get_references
        res = get_references(path, int(line), int(col))
        if not res:
            return {"success": False, "error": f"No references found at {path}:{line}:{col} (Is the language server installed?)"}
        return {"success": True, "data": {"references": res}}
    except Exception as e:
        return {"success": False, "error": str(e)}
