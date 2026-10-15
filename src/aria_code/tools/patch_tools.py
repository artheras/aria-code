"""
Surgical Code Editing & Intelligence Tools for Aria Code Agent.

Provides precision editing capabilities (unified diff patch application,
line-range reading, AST symbol extraction, and ripgrep reference finding)
to prevent full-file rewrite corruptions by LLMs.
"""

from __future__ import annotations

import ast
import difflib
import logging
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", ".idea",
    ".vscode", ".next", ".nuxt", ".output", "coverage", ".tox", "Pods",
})


def _parse_hunks(patch_text: str) -> List[Dict[str, Any]]:
    """Parse a unified diff text into individual hunks."""
    lines = patch_text.splitlines()
    hunks: List[Dict[str, Any]] = []
    current_hunk: Optional[Dict[str, Any]] = None

    hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    for line in lines:
        match = hunk_header_re.match(line)
        if match:
            if current_hunk:
                hunks.append(current_hunk)
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current_hunk = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "lines": [],
            }
        elif current_hunk is not None:
            if line.startswith(("+", "-", " ")) or line == "":
                current_hunk["lines"].append(line)

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


def tool_apply_patch(params: dict) -> dict:
    """
    Apply a unified diff patch to a target file with fuzzy offset matching.
    """
    path_str = params.get("path") or params.get("file_path")
    patch_text = params.get("patch") or params.get("diff")

    if not path_str or not patch_text:
        return {"success": False, "error": "Both 'path' and 'patch' parameters are required."}

    file_path = pathlib.Path(path_str).resolve()
    if not file_path.is_file():
        return {"success": False, "error": f"Target file does not exist: {file_path}"}

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"success": False, "error": f"Failed to read file {file_path}: {exc}"}

    lines = content.splitlines(keepends=True)
    hunks = _parse_hunks(patch_text)

    if not hunks:
        return {"success": False, "error": "No valid diff hunks (@@ ... @@) found in patch."}

    # Backup before applying
    backup_path = file_path.with_suffix(file_path.suffix + ".patch.bak")
    try:
        shutil.copy2(file_path, backup_path)
    except Exception as exc:
        logger.warning("Failed to create backup at %s: %s", backup_path, exc)

    applied_lines = list(lines)
    lines_added = 0
    lines_removed = 0
    hunks_applied = 0

    # Apply each hunk with fuzzy start offset tolerance (±3 lines)
    for hunk in hunks:
        old_start = max(1, hunk["old_start"]) - 1  # 0-indexed
        hunk_lines = hunk["lines"]

        # Extract expected old lines from hunk
        expected_old = [
            l[1:] for l in hunk_lines if l.startswith((" ", "-"))
        ]

        # Try exact index first, then fuzzy search ±3 lines
        match_idx = None
        for offset in (0, 1, -1, 2, -2, 3, -3):
            cand_idx = old_start + offset
            if 0 <= cand_idx <= len(applied_lines):
                # Compare slice
                slice_len = len(expected_old)
                cand_slice = [
                    l.rstrip("\r\n") for l in applied_lines[cand_idx : cand_idx + slice_len]
                ]
                norm_expected = [l.rstrip("\r\n") for l in expected_old]
                if cand_slice == norm_expected:
                    match_idx = cand_idx
                    break

        if match_idx is None:
            # Revert from backup
            if backup_path.exists():
                shutil.copy2(backup_path, file_path)
            return {
                "success": False,
                "error": f"Failed to locate hunk target at line {hunk['old_start']} (fuzzy match failed).",
                "hunk": hunk,
            }

        # Build replacement lines for this hunk
        new_hunk_lines = []
        for hl in hunk_lines:
            if hl.startswith("+"):
                lines_added += 1
                new_hunk_lines.append(hl[1:] + ("\n" if not hl[1:].endswith("\n") else ""))
            elif hl.startswith("-"):
                lines_removed += 1
            elif hl.startswith(" ") or hl == "":
                raw = hl[1:] if hl.startswith(" ") else hl
                new_hunk_lines.append(raw + ("\n" if not raw.endswith("\n") else ""))

        # Replace slice in applied_lines
        slice_len = len(expected_old)
        applied_lines[match_idx : match_idx + slice_len] = new_hunk_lines
        hunks_applied += 1

    # Write patched file
    try:
        file_path.write_text("".join(applied_lines), encoding="utf-8")
    except Exception as exc:
        if backup_path.exists():
            shutil.copy2(backup_path, file_path)
        return {"success": False, "error": f"Failed to write patched file: {exc}"}

    return {
        "success": True,
        "path": str(file_path),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "hunks_applied": hunks_applied,
        "backup": str(backup_path),
    }


def tool_read_range(params: dict) -> dict:
    """
    Read a specific line range from a file with line numbers prefixed.
    """
    path_str = params.get("path") or params.get("file_path")
    start_line = params.get("start_line", 1)
    end_line = params.get("end_line")
    context_lines = params.get("context_lines", 0)

    if not path_str:
        return {"success": False, "error": "Parameter 'path' is required."}

    file_path = pathlib.Path(path_str).resolve()
    if not file_path.is_file():
        return {"success": False, "error": f"File not found: {file_path}"}

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"success": False, "error": f"Cannot read file: {exc}"}

    all_lines = content.splitlines()
    total_lines = len(all_lines)

    try:
        start_line = int(start_line)
        end_line = int(end_line) if end_line is not None else total_lines
        context_lines = int(context_lines)
    except ValueError:
        return {"success": False, "error": "Line numbers must be integers."}

    actual_start = max(1, start_line - context_lines)
    actual_end = min(total_lines, end_line + context_lines)

    selected = []
    for idx in range(actual_start, actual_end + 1):
        line_content = all_lines[idx - 1] if idx - 1 < total_lines else ""
        selected.append(f"{idx:5d}: {line_content}")

    return {
        "success": True,
        "path": str(file_path),
        "start_line": actual_start,
        "end_line": actual_end,
        "total_lines": total_lines,
        "content": "\n".join(selected),
    }


def _extract_py_symbols(file_path: pathlib.Path, name_filter: Optional[str] = None, kind_filter: Optional[str] = None) -> List[dict]:
    """Parse Python AST to extract symbols."""
    symbols = []
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"), filename=str(file_path))
    except Exception:
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if (not name_filter or node.name == name_filter) and (not kind_filter or kind_filter == "class"):
                symbols.append({
                    "file": str(file_path),
                    "line": node.lineno,
                    "kind": "class",
                    "name": node.name,
                    "signature": f"class {node.name}",
                })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            if (not name_filter or node.name == name_filter) and (not kind_filter or kind_filter in (kind, "function", "method")):
                args = [a.arg for a in node.args.args]
                sig = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({', '.join(args)})"
                symbols.append({
                    "file": str(file_path),
                    "line": node.lineno,
                    "kind": kind,
                    "name": node.name,
                    "signature": sig,
                })

    return symbols


def tool_find_symbol(params: dict) -> dict:
    """
    Find symbol definitions (classes, functions, methods) across codebase.
    """
    name = params.get("name")
    search_root = pathlib.Path(params.get("path", ".")).resolve()
    kind = params.get("kind")

    if not name:
        return {"success": False, "error": "Parameter 'name' is required."}

    if not search_root.exists():
        return {"success": False, "error": f"Search path does not exist: {search_root}"}

    matches: List[dict] = []

    # Non-python regexes
    regex_pattern = re.compile(
        rf"(?:function|class|def|const|let|var|type|interface|struct|fn|func)\s+({re.escape(name)})\b"
    )

    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            fp = pathlib.Path(root) / f
            if fp.suffix == ".py":
                syms = _extract_py_symbols(fp, name_filter=name, kind_filter=kind)
                matches.extend(syms)
            elif fp.suffix in (".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".swift", ".java", ".cpp", ".c", ".h"):
                try:
                    for line_no, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if regex_pattern.search(line):
                            matches.append({
                                "file": str(fp),
                                "line": line_no,
                                "kind": "symbol",
                                "name": name,
                                "signature": line.strip()[:100],
                            })
                except Exception:
                    continue

    return {
        "success": True,
        "symbol": name,
        "total_matches": len(matches),
        "matches": matches[:100],
    }


def tool_find_references(params: dict) -> dict:
    """
    Find all references / usages of a symbol across the codebase using ripgrep with fallback.
    """
    name = params.get("name")
    search_root = pathlib.Path(params.get("path", ".")).resolve()
    max_results = int(params.get("max_results", 50))

    if not name:
        return {"success": False, "error": "Parameter 'name' is required."}

    references: List[dict] = []

    # 1. Try ripgrep first
    rg_path = shutil.which("rg")
    if rg_path:
        try:
            cmd = [
                rg_path,
                "--line-number",
                "--no-heading",
                "--color=never",
                "--max-count", str(max_results),
                "-w", name,
                str(search_root),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            for line in res.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    references.append({
                        "file": parts[0],
                        "line": int(parts[1]),
                        "content": parts[2].strip()[:150],
                    })
                    if len(references) >= max_results:
                        break
            if references:
                return {
                    "success": True,
                    "name": name,
                    "total": len(references),
                    "references": references,
                    "engine": "ripgrep",
                }
        except Exception as exc:
            logger.debug("Ripgrep invocation fallback: %s", exc)

    # 2. Python fallback grep
    word_pattern = re.compile(rf"\b{re.escape(name)}\b")
    for root, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            fp = pathlib.Path(root) / f
            try:
                for line_no, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if word_pattern.search(line):
                        references.append({
                            "file": str(fp),
                            "line": line_no,
                            "content": line.strip()[:150],
                        })
                        if len(references) >= max_results:
                            break
            except Exception:
                continue
            if len(references) >= max_results:
                break
        if len(references) >= max_results:
            break

    return {
        "success": True,
        "name": name,
        "total": len(references),
        "references": references,
        "engine": "python_regex",
    }


PATCH_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff patch to a file with fuzzy offset matching. Preferred for surgical code edits instead of rewriting whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path to patch"},
                    "patch": {"type": "string", "description": "Unified diff patch text starting with @@ hunks"},
                },
                "required": ["path", "patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_range",
            "description": "Read specific line range from a file with line numbers prefixed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "start_line": {"type": "integer", "description": "1-indexed start line"},
                    "end_line": {"type": "integer", "description": "1-indexed end line (inclusive)"},
                    "context_lines": {"type": "integer", "description": "Extra context lines above and below", "default": 0},
                },
                "required": ["path", "start_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "Find symbol definitions (classes, functions, interfaces) across codebase using AST and pattern matching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to look for"},
                    "path": {"type": "string", "description": "Search directory path", "default": "."},
                    "kind": {"type": "string", "description": "Optional filter: 'class', 'function', 'method'"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find all usages/references of a symbol across the codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Identifier name to search"},
                    "path": {"type": "string", "description": "Search directory path", "default": "."},
                    "max_results": {"type": "integer", "description": "Maximum number of occurrences to return", "default": 50},
                },
                "required": ["name"],
            },
        },
    },
]


def register_patch_tools(registry_or_dict: Any, schema_registry: Optional[list] = None) -> int:
    """
    Register patch tools into CLI LOCAL_TOOLS dictionary or ToolRegistry.
    """
    tools = {
        "apply_patch": (tool_apply_patch, "Apply unified diff patch to a file"),
        "read_range": (tool_read_range, "Read a specific line range from a file"),
        "find_symbol": (tool_find_symbol, "Find symbol definitions in codebase"),
        "find_references": (tool_find_references, "Find usages of a symbol across codebase"),
    }

    count = 0
    if isinstance(registry_or_dict, dict):
        for name, entry in tools.items():
            registry_or_dict[name] = entry
            count += 1
    elif hasattr(registry_or_dict, "register"):
        for name, (func, doc) in tools.items():
            registry_or_dict.register(name, func, doc)
            count += 1

    if schema_registry is not None and isinstance(schema_registry, list):
        for schema in PATCH_TOOL_SCHEMAS:
            if schema not in schema_registry:
                schema_registry.append(schema)

    return count
