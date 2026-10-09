"""Confine run_command to what the permission mode says, at the OS level.

"workspace-write" and "network off" were checks on the command text: a
pattern list caught `curl` and `rm -rf /`, but a script could still write
anywhere in the home directory and open any connection. On macOS a command
now runs under sandbox-exec (Seatbelt), as Codex does:

- workspace-write: writes only to the command's directory (and the git
  repository it is in), Aria's output folders, temporary directories and /dev.
- read-only: writes only to temporary directories and /dev.
- network off: no outbound IP connections at all, localhost included, since a
  local proxy would otherwise carry traffic out.
- full-access, or ``os_sandbox: off`` in the config: no sandbox.

On Linux, bubblewrap (when installed) provides the equivalent read-only root
and writable-root mounts plus an isolated network namespace. If it is absent,
or on Windows, command policy still applies; capability() reports the limit.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
CONFINED_MODES = ("read-only", "workspace-write")


def available() -> bool:
    return (sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)) or (
        sys.platform.startswith("linux") and bool(shutil.which("bwrap"))
    )


def capability(setting: object = None) -> str:
    if not enabled(setting):
        return "disabled"
    if not available():
        return "policy-only"
    return "bubblewrap" if sys.platform.startswith("linux") else "seatbelt"


def enabled(setting: object = None) -> bool:
    """False when the user turned it off (config os_sandbox, or ARIA_OS_SANDBOX)."""
    value = setting if setting not in (None, "") else os.environ.get("ARIA_OS_SANDBOX", "auto")
    return str(value).strip().lower() not in ("off", "false", "0", "no", "disabled")


def _real(path: Path | str) -> str | None:
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return None


def _git_root(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def writable_roots(mode: str, cwd: Path | str | None, extra: Iterable[Path | str] = ()) -> list[str]:
    """Directories a command may write to under ``mode``, as real paths."""
    roots: list[Path | str] = ["/dev", "/tmp", "/private/var/folders", tempfile.gettempdir()]
    if os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    if mode == "workspace-write":
        here = Path(cwd or os.getcwd()).expanduser()
        roots.append(here)
        repo = _git_root(here.resolve())
        if repo:
            roots.append(repo)
        try:
            from aria_code.artifacts import artifact_root, user_output_root
            roots += [user_output_root(), artifact_root()]
        except Exception:
            pass
        roots += list(extra)
    seen: dict[str, None] = {}
    for root in roots:
        real = _real(root)
        if real and real != "/":
            seen[real] = None
    return list(seen)


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(roots: Sequence[str], network: bool) -> str:
    """A Seatbelt profile: everything allowed except writes outside ``roots`` (and the network if off)."""
    keep = " ".join(f"(require-not (subpath {_quote(r)}))" for r in roots)
    lines = ["(version 1)", "(allow default)", f"(deny file-write* (require-all {keep}))"]
    if not network:
        lines.append('(deny network-outbound (remote ip "*:*"))')
    return "\n".join(lines)


def wrap(command: str | Sequence[str], *, use_shell: bool, mode: str, network: bool,
         cwd: Path | str | None = None, setting: object = None,
         workspace: Path | str | None = None, extra: Iterable[Path | str] = ()) -> list[str] | None:
    """The argv that runs ``command`` in the sandbox, or None to run it as is."""
    if mode not in CONFINED_MODES or not enabled(setting) or not available():
        return None
    roots = writable_roots(mode, workspace or cwd, extra)
    if use_shell or isinstance(command, str):
        shell = shutil.which("sh") or "/bin/sh"
        body = command if isinstance(command, str) else " ".join(command)
        argv = [shell, "-c", body]
    else:
        argv = list(command)
    if sys.platform.startswith("linux"):
        # No fallback after launch failure: a kernel that forbids namespaces
        # must return the bwrap error, never execute the command unconfined.
        wrapped = [shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session",
                   "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--ro-bind", "/", "/",
                   "--proc", "/proc", "--dev", "/dev"]
        if not network:
            wrapped.append("--unshare-net")
        for root in roots:
            if root not in ("/dev", "/proc", "/") and os.path.isdir(root):
                wrapped += ["--bind", root, root]
        return [*wrapped, "--chdir", _real(cwd or os.getcwd()) or "/", "--", *argv]
    return [SANDBOX_EXEC, "-p", profile(roots, network), *argv]


def denial_hint(text: str, *, mode: str, network: bool) -> str:
    """What to tell the model when the sandbox stopped a command."""
    if not any(error in text for error in ("Operation not permitted", "Read-only file system",
                                           "Network is unreachable", "bwrap:")):
        return ""
    limits = ["writes outside the project, Aria's output folder and temporary directories"
              if mode == "workspace-write" else "all writes outside temporary directories"]
    if not network:
        limits.append("network access")
    return (f"The command ran in the {mode} sandbox, which blocks {' and '.join(limits)}. "
            "Do not retry it as is. If it needs more, tell the user what and why; they can switch "
            "the permission mode (Shift+Tab, or /config set permission_mode=full-access) or turn "
            "the network on (/config set network_enabled=true).")


__all__ = ["available", "enabled", "capability", "writable_roots", "profile", "wrap", "denial_hint"]
