"""Safe workspace file operations for Aria Code."""

from __future__ import annotations

import pathlib
import re
import tempfile
import os
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class ReadResult:
    path: str
    lines: int
    content: str


class WorkspaceSecurity:
    """Path security policy shared by CLI tools and future runtimes."""

    BLOCKED_ROOTS = (
        "/etc",
        "/sys",
        "/proc",
        "/dev",
        "/boot",
        "/root",
    )

    def __init__(
        self,
        cwd: str | pathlib.Path | None = None,
        *,
        allowed_roots: List[str | pathlib.Path] | None = None,
        write_roots: List[str | pathlib.Path] | None = None,
        allow_home: bool | None = None,
    ) -> None:
        self.cwd = pathlib.Path(cwd or pathlib.Path.cwd()).expanduser().resolve()
        self._configured_roots = tuple(
            pathlib.Path(root).expanduser().resolve() for root in (allowed_roots or ())
        )
        self._write_roots = tuple(
            pathlib.Path(root).expanduser().resolve() for root in (write_roots or ())
        )
        if allow_home is None:
            # Local CLI keeps its historical convenience. Remote/API workers must
            # set ARIA_RUNTIME_SCOPE=remote (or pass allow_home=False) so one user
            # can never inspect the worker account's home directory.
            allow_home = os.getenv("ARIA_RUNTIME_SCOPE", "local").strip().lower() != "remote"
        self.allow_home = bool(allow_home)

    def allowed_roots(self) -> List[pathlib.Path]:
        roots = [self.cwd, *self._configured_roots, *self._write_roots]
        if self.allow_home:
            roots.append(pathlib.Path.home().resolve())
        # Temporary roots are required by the local CLI, but a remote worker is
        # confined to its explicitly assigned workspace root.
        if not self.allow_home:
            return list(dict.fromkeys(roots))
        roots.append(pathlib.Path("/tmp").resolve())
        for tmp_candidate in (
            "/var/folders",
            "/private/tmp",
            "/private/var/folders",
            "/var/tmp",
            "/private/var/tmp",
            tempfile.gettempdir(),
        ):
            try:
                root = pathlib.Path(tmp_candidate).resolve()
                if root not in roots:
                    roots.append(root)
            except Exception:
                pass
        return roots

    @classmethod
    def from_tool_params(cls, params: dict):
        unrestricted = params.get("_permission_mode") == "full-access" and not params.get("_workspace_restricted")
        return cls(
            cwd=params.get("_workspace") or os.getcwd(),
            allowed_roots=["/"] if unrestricted else params.get("_allowed_read_roots", ()),
            write_roots=["/"] if unrestricted else params.get("_allowed_write_roots", ()),
            allow_home=False if params.get("_workspace_restricted") else None,
        )

    def writable_roots(self) -> List[pathlib.Path]:
        roots = [self.cwd, *self._write_roots]
        if self.allow_home:
            roots += [pathlib.Path("/tmp").resolve(), pathlib.Path(tempfile.gettempdir()).resolve()]
            try:
                from aria_code.artifacts import artifact_root, user_output_root
                roots += [pathlib.Path(artifact_root()).resolve(), pathlib.Path(user_output_root()).resolve()]
            except ImportError:
                pass
        return list(dict.fromkeys(roots))

    def resolve(self, path: str | pathlib.Path) -> pathlib.Path:
        target = pathlib.Path(path).expanduser()
        return (target if target.is_absolute() else self.cwd / target).resolve()

    def is_safe_path(self, path: str | pathlib.Path, *, write: bool = False) -> bool:
        resolved = self.resolve(path)
        for blocked in self.BLOCKED_ROOTS:
            try:
                resolved.relative_to(pathlib.Path(blocked).resolve())
                return False
            except ValueError:
                pass
        for root in self.writable_roots() if write else self.allowed_roots():
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                pass
        return False

    def require_safe(self, path: str | pathlib.Path, *, write: bool = False) -> pathlib.Path:
        resolved = self.resolve(path)
        if not self.is_safe_path(resolved, write=write):
            raise PermissionError(f"Access denied: path '{resolved}' is outside allowed directories")
        return resolved


class WorkspaceFiles:
    """Read, list, and search files under Aria Code's safety policy."""

    MAX_READ_BYTES = 2_000_000
    LARGE_FILE_BYTES = 500_000
    LARGE_FILE_DEFAULT_LINES = 500
    MAX_SEARCH_BYTES = 5_000_000

    def __init__(self, security: WorkspaceSecurity | None = None) -> None:
        self.security = security or WorkspaceSecurity()

    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> ReadResult:
        target = self.security.require_safe(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        if not target.is_file():
            raise ValueError(f"Not a file: {target}")
        size = target.stat().st_size
        if size > self.MAX_READ_BYTES:
            raise ValueError(
                f"File too large: {size:,} bytes (max 2 MB). "
                "Use offset/limit parameters to read sections."
            )
        text = target.read_text(errors="replace")
        lines = text.splitlines()
        total_lines = len(lines)
        if not offset and not limit and size > self.LARGE_FILE_BYTES:
            limit = self.LARGE_FILE_DEFAULT_LINES
        if offset or limit:
            end = offset + limit if limit else total_lines
            shown = lines[offset:end]
            content = "\n".join(f"{i + offset + 1:4d}│ {line}" for i, line in enumerate(shown))
            if limit and end < total_lines:
                content += f"\n... [{len(shown)} of {total_lines} lines shown — use offset/limit to read more]"
            return ReadResult(str(target), len(shown), content[:30000])
        content = "\n".join(f"{i + 1:4d}│ {line}" for i, line in enumerate(lines))
        return ReadResult(str(target), len(lines), content[:30000])

    def list_files(self, path: str = ".", pattern: str = "*") -> Dict[str, Any]:
        target = self.security.require_safe(path)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {target}")
        if target.is_file():
            read = self.read_file(str(target))
            return {
                "path": read.path,
                "pattern": pattern,
                "count": 1,
                "items": [{"name": target.name, "type": "file", "size": target.stat().st_size}],
                "content": read.content,
            }
        matches = sorted(target.glob(pattern))[:100]
        items = []
        for match in matches:
            try:
                self.security.require_safe(match)
            except PermissionError:
                continue
            rel = match.relative_to(target) if match.is_relative_to(target) else match
            kind = "dir" if match.is_dir() else "file"
            size = match.stat().st_size if match.is_file() else 0
            items.append({"name": str(rel), "type": kind, "size": size})
        return {"path": str(target), "pattern": pattern, "count": len(items), "items": items}

    def search_code(self, pattern: str, path: str = ".", file_glob: str = "**/*.py") -> Dict[str, Any]:
        if not pattern:
            raise ValueError("Missing 'pattern' parameter")
        target = self.security.require_safe(path)
        regex = re.compile(pattern, re.IGNORECASE)
        matches = []
        for file_path in sorted(target.glob(file_glob))[:200]:
            try:
                safe_file = self.security.require_safe(file_path)
            except PermissionError:
                continue
            if not safe_file.is_file() or safe_file.stat().st_size > self.MAX_SEARCH_BYTES:
                continue
            try:
                lines = safe_file.read_text(errors="replace").splitlines()
            except Exception:
                continue
            for line_number, line in enumerate(lines, 1):
                if regex.search(line):
                    matches.append({
                        "file": str(safe_file.relative_to(target) if safe_file.is_relative_to(target) else safe_file),
                        "line": line_number,
                        "content": line.strip()[:200],
                    })
                    if len(matches) >= 50:
                        break
            if len(matches) >= 50:
                break
        return {"pattern": pattern, "path": str(target), "count": len(matches), "matches": matches}
