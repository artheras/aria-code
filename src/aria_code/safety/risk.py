"""Risk assessment for agent actions: what an action can touch, and how badly.

``classify_command_risk`` answers one question — low, medium or high — and the
policy layer decides from that whether a command may run. That is enough to
gate, not enough to explain. "Allow `npm install jose`? [y/N]" asks a person to
judge a command; what they actually need to judge is its consequences: it
reaches the npm registry, it rewrites package.json and the lockfile, and it can
be undone. This module turns an action into that description.

An assessment carries:

- a **level**, L0–L4, the unit approvals are decided in:

  ====  ===============================  ===============================
  L0    read / inspect / verify          ls, cat, rg, git diff, pytest
  L1    change inside the workspace      edit a source file, git add
  L2    change the environment           npm install, pip install, curl
  L3    act outside the machine's repo   git commit/push, gh pr create
  L4    destructive or production        rm, force push, migrate, deploy,
                                         secrets, sudo
  ====  ===============================  ===============================

- a **score** (0–100) for ordering and display within a level;
- the **capabilities** used (``network.read``, ``package.install``,
  ``git.push`` …) — finer than a permission mode, and what a Change Contract
  names when it forbids something;
- whether it is **reversible**, the **reasons**, and its **scope** (hosts,
  files, systems) — the blast radius.

It never lowers what the existing policy says: a command
``classify_command_risk`` calls high is at least L3 here. Assessment is
description; whether to run stays with ``evaluate_command_policy``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from .permissions import (
    classify_command_risk,
    has_shell_control,
    is_verification_command,
    normalize_command,
    writes_a_device,
)

LEVEL_NAMES = {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}
_BASE_SCORE = {0: 5, 1: 20, 2: 40, 3: 65, 4: 85}

# Every capability an assessment can name. A contract that forbids one not in
# this set is a typo, and is reported as one.
CAPABILITIES = frozenset({
    "filesystem.read", "filesystem.write", "filesystem.delete",
    "process.execute",
    "network.read", "network.write",
    "package.install",
    "git.stage", "git.commit", "git.push", "git.history",
    "github.pr",
    "database.read", "database.write",
    "secret.read",
    "cloud.deploy",
    "system.admin",
})


@dataclass(frozen=True)
class RiskAssessment:
    level: int
    score: int
    capabilities: frozenset = frozenset()
    reversible: bool = True
    reasons: tuple = ()
    # Blast radius: what the action reaches, by kind.
    hosts: tuple = ()
    files: tuple = ()
    systems: tuple = ()
    summary: str = ""

    @property
    def name(self) -> str:
        return LEVEL_NAMES[self.level]

    @property
    def legacy_risk(self) -> str:
        """The low/medium/high the older policy layer speaks."""
        return "low" if self.level == 0 else "high" if self.level >= 3 else "medium"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "name": self.name,
            "score": self.score,
            "capabilities": sorted(self.capabilities),
            "reversible": self.reversible,
            "reasons": list(self.reasons),
            "hosts": list(self.hosts),
            "files": list(self.files),
            "systems": list(self.systems),
            "summary": self.summary,
        }


@dataclass
class _Draft:
    level: int = 0
    capabilities: set = field(default_factory=set)
    reversible: bool = True
    reasons: list = field(default_factory=list)
    hosts: list = field(default_factory=list)
    files: list = field(default_factory=list)
    systems: list = field(default_factory=list)
    summary: str = ""

    def raise_to(self, level: int, reason: str = "", *, reversible: Optional[bool] = None) -> None:
        self.level = max(self.level, level)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if reversible is False:
            self.reversible = False

    def merge(self, other: "_Draft") -> None:
        self.level = max(self.level, other.level)
        self.capabilities |= other.capabilities
        self.reversible = self.reversible and other.reversible
        for mine, theirs in ((self.reasons, other.reasons), (self.hosts, other.hosts),
                             (self.files, other.files), (self.systems, other.systems)):
            mine.extend(item for item in theirs if item not in mine)
        self.summary = self.summary or other.summary

    def freeze(self, *, extra: int = 0) -> RiskAssessment:
        score = _BASE_SCORE[self.level] + extra
        if not self.reversible:
            score += 8
        if "network.write" in self.capabilities or "network.read" in self.capabilities:
            score += 3
        return RiskAssessment(
            level=self.level,
            score=max(0, min(100, score)),
            capabilities=frozenset(self.capabilities),
            reversible=self.reversible,
            reasons=tuple(self.reasons),
            hosts=tuple(self.hosts),
            files=tuple(self.files),
            systems=tuple(self.systems),
            summary=self.summary,
        )


# ── commands ──────────────────────────────────────────────────────────────────

_SEGMENTS = re.compile(r"\|\||&&|[|;&\n]")
_READ_COMMANDS = {
    "ls", "pwd", "echo", "cat", "head", "tail", "less", "more", "wc", "rg", "grep", "find",
    "fd", "tree", "which", "whoami", "date", "uname", "file", "stat", "du", "df", "diff",
    "sort", "uniq", "cut", "awk", "sed", "jq", "true", "false", "test", "basename", "dirname",
}
_GIT_READ = {"status", "diff", "log", "show", "blame", "branch", "rev-parse", "ls-files",
             "remote", "describe", "shortlog", "grep", "config"}
_INSTALLERS = {
    # tool -> (subcommands that install, files rewritten, registry host)
    "npm": ({"install", "i", "add", "ci", "update", "uninstall", "remove"},
            ("package.json", "package-lock.json"), "registry.npmjs.org"),
    "pnpm": ({"install", "i", "add", "update", "remove"}, ("package.json", "pnpm-lock.yaml"),
             "registry.npmjs.org"),
    "yarn": ({"install", "add", "upgrade", "remove", ""}, ("package.json", "yarn.lock"),
             "registry.yarnpkg.com"),
    "bun": ({"install", "add", "i", "remove"}, ("package.json", "bun.lockb"), "registry.npmjs.org"),
    "pip": ({"install", "uninstall"}, (), "pypi.org"),
    "pip3": ({"install", "uninstall"}, (), "pypi.org"),
    "uv": ({"add", "pip", "sync", "remove"}, ("pyproject.toml", "uv.lock"), "pypi.org"),
    "poetry": ({"add", "install", "update", "remove"}, ("pyproject.toml", "poetry.lock"), "pypi.org"),
    "cargo": ({"add", "install", "update", "remove"}, ("Cargo.toml", "Cargo.lock"), "crates.io"),
    "go": ({"get", "install"}, ("go.mod", "go.sum"), "proxy.golang.org"),
    "gem": ({"install", "uninstall"}, (), "rubygems.org"),
    "bundle": ({"install", "add", "update"}, ("Gemfile", "Gemfile.lock"), "rubygems.org"),
    "brew": ({"install", "uninstall", "upgrade"}, (), "formulae.brew.sh"),
    "apt": ({"install", "remove"}, (), "deb.debian.org"),
    "apt-get": ({"install", "remove"}, (), "deb.debian.org"),
}
_DEPLOY = (
    re.compile(r"^vercel\b.*--prod"), re.compile(r"^(fly|flyctl) deploy\b"),
    re.compile(r"^netlify deploy\b.*--prod"), re.compile(r"^firebase deploy\b"),
    re.compile(r"^gcloud (app|run|functions) deploy\b"), re.compile(r"^terraform (apply|destroy)\b"),
    re.compile(r"^pulumi (up|destroy)\b"), re.compile(r"^kubectl (apply|delete|rollout|scale)\b"),
    re.compile(r"^helm (install|upgrade|uninstall)\b"), re.compile(r"^docker push\b"),
    re.compile(r"^(npm|pnpm|yarn) publish\b"), re.compile(r"^twine upload\b"),
    re.compile(r"^cargo publish\b"), re.compile(r"^aws .*\b(deploy|update-function-code|s3 (rm|sync|cp))\b"),
    re.compile(r"^serverless deploy\b"), re.compile(r"^sls deploy\b"),
)
_MIGRATIONS = (
    re.compile(r"\bmigrate\b"), re.compile(r"\balembic (upgrade|downgrade)\b"),
    re.compile(r"\bprisma (migrate|db push)\b"), re.compile(r"\bdb:(migrate|rollback|drop|reset)\b"),
    re.compile(r"\bknex migrate\b"), re.compile(r"\bflyway migrate\b"),
)
_SQL_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create)\b", re.IGNORECASE)
_DB_CLIENTS = {"psql", "mysql", "sqlite3", "mongosh", "mongo", "redis-cli", "cqlsh"}
_SECRET_PATH = re.compile(
    r"(^|[\s/'\"])(\.env(\.[\w-]+)?|\.npmrc|\.pypirc|\.netrc|id_rsa|id_ed25519|"
    r"[\w.-]+\.pem|[\w.-]+\.key|credentials(\.json)?|\.aws/credentials|\.ssh/)(\s|$|['\"])"
)
_URL = re.compile(r"https?://[^\s'\"]+")
_PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")
_HTTP_WRITE = re.compile(r"(\s-X\s*(POST|PUT|PATCH|DELETE)\b|\s--data\b|\s-d\s|\s--data-\w+|\s-F\s|\s--form\b)",
                         re.IGNORECASE)


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _hosts(segment: str) -> list[str]:
    hosts = []
    for url in _URL.findall(segment):
        host = urlparse(url).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _targets(words: list[str]) -> list[str]:
    return [w for w in words[1:] if not w.startswith("-")]


def _assess_segment(segment: str) -> _Draft:
    draft = _Draft()
    words = _words(segment)
    if not words:
        return draft
    # `VAR=1 cmd`, `sudo cmd`, `time cmd`: judge the command, note the wrapper.
    while words and re.match(r"^\w+=", words[0]):
        words = words[1:]
    if words and words[0] == "sudo":
        draft.capabilities.add("system.admin")
        draft.raise_to(4, "Runs with administrator privileges", reversible=False)
        draft.systems.append("Operating system")
        words = words[1:]
    while words and words[0] in {"time", "nice", "nohup", "env"} and len(words) > 1:
        words = words[1:]
    if not words:
        return draft
    head = Path(words[0]).name
    sub = words[1] if len(words) > 1 else ""
    lowered = segment.lower().strip()
    draft.capabilities.add("process.execute")

    if _SECRET_PATH.search(" " + segment + " ") and head in _READ_COMMANDS | {"source", "."}:
        draft.capabilities.add("secret.read")
        draft.raise_to(4, "Reads a credentials or secrets file", reversible=False)
        draft.summary = "Read secrets"
        return draft
    if head in {"env", "printenv"}:
        draft.capabilities.add("secret.read")
        draft.raise_to(3, "Prints environment variables, which may hold secrets")
        draft.summary = "Print environment"
        return draft

    if head == "git":
        return _assess_git(words, draft)
    if head == "gh":
        return _assess_gh(words, draft)
    if head in _INSTALLERS:
        installs, files, host = _INSTALLERS[head]
        if sub in installs:
            draft.capabilities |= {"package.install", "network.read"}
            draft.raise_to(2, f"Changes installed dependencies ({head} {sub})".strip())
            draft.hosts.append(host)
            draft.files.extend(files)
            if not files:
                draft.systems.append("Python environment" if head.startswith("pip") else "System packages"
                                     if head in {"brew", "apt", "apt-get"} else f"{head} environment")
            draft.summary = "Install dependency"
            return draft
    for pattern in _DEPLOY:
        if pattern.search(lowered):
            draft.capabilities |= {"cloud.deploy", "network.write"}
            draft.raise_to(4, "Deploys or publishes outside this machine", reversible=False)
            draft.systems.append("Production / remote environment")
            draft.summary = "Deploy or publish"
            return draft
    if any(pattern.search(lowered) for pattern in _MIGRATIONS):
        draft.capabilities.add("database.write")
        draft.raise_to(4, "Runs a database migration", reversible=False)
        draft.systems.append("Database schema")
        draft.summary = "Database migration"
        return draft
    if head in _DB_CLIENTS:
        if _SQL_WRITE.search(segment):
            draft.capabilities.add("database.write")
            draft.raise_to(4, "Writes to a database", reversible=False)
            draft.systems.append("Database")
            draft.summary = "Database write"
        else:
            draft.capabilities.add("database.read")
            draft.raise_to(2, "Connects to a database")
            draft.systems.append("Database")
            draft.summary = "Database query"
        return draft
    if head in {"curl", "wget", "http", "https", "httpie", "xh"}:
        hosts = _hosts(segment)
        draft.hosts.extend(hosts)
        if head in {"curl", "http", "https", "httpie", "xh"} and _HTTP_WRITE.search(" " + segment + " "):
            draft.capabilities.add("network.write")
            draft.raise_to(3, "Sends data to a remote server", reversible=False)
            draft.summary = "Network write"
        else:
            draft.capabilities.add("network.read")
            draft.raise_to(2, "Fetches from the network")
            draft.summary = "Network request"
        return draft
    if head == "rm" or head == "rmdir" or head == "shred" or head == "unlink":
        draft.capabilities.add("filesystem.delete")
        targets = _targets(words)
        draft.files.extend(targets)
        draft.raise_to(4, "Deletes files without a checkpoint", reversible=False)
        if any(t in {"/", "~", "*", ".", ".."} or t.startswith(("/", "~")) for t in targets):
            draft.raise_to(4, "Deletes outside the workspace or a whole tree")
        draft.summary = "Delete files"
        return draft
    if head in {"mv", "cp", "touch", "mkdir", "ln", "tee"}:
        draft.capabilities.add("filesystem.write")
        draft.files.extend(_targets(words)[-1:])
        draft.raise_to(1, "Changes files in the workspace")
        draft.summary = "Change files"
        return draft
    if head in {"chmod", "chown", "mkfs", "dd", "shutdown", "reboot", "systemctl", "launchctl", "passwd", "kill",
                "killall", "pkill", "docker", "kubectl"}:
        draft.capabilities.add("system.admin")
        draft.raise_to(3 if head in {"kill", "pkill", "killall", "docker"} else 4,
                       "Changes system state outside the workspace", reversible=False)
        draft.systems.append("Operating system")
        draft.summary = "System change"
        return draft
    if is_verification_command(segment):
        draft.capabilities.add("filesystem.read")
        draft.summary = "Verify"
        return draft
    if head in _READ_COMMANDS:
        if head == "sed" and re.search(r"\s-i\b", segment):
            draft.capabilities.add("filesystem.write")
            draft.raise_to(1, "Edits files in place")
            draft.files.extend(_targets(words)[-1:])
            draft.summary = "Change files"
        else:
            draft.capabilities.add("filesystem.read")
            draft.summary = "Inspect"
        return draft
    # A program we cannot see into: it runs code and may write.
    draft.capabilities |= {"filesystem.write"}
    draft.raise_to(1, f"Runs {head}, which may change files in the workspace")
    draft.summary = f"Run {head}"
    return draft


def _assess_git(words: list[str], draft: _Draft) -> _Draft:
    args = [w for w in words[1:] if not w.startswith("-C")]
    sub = next((w for w in args if not w.startswith("-")), "")
    flags = set(words)
    if sub in _GIT_READ:
        draft.capabilities.add("filesystem.read")
        draft.summary = "Inspect repository"
        return draft
    if sub in {"fetch", "pull", "clone"}:
        draft.capabilities |= {"network.read", "git.history"} if sub != "clone" else {"network.read",
                                                                                         "filesystem.write"}
        draft.raise_to(2, f"git {sub} reaches a remote")
        draft.summary = f"git {sub}"
        return draft
    if sub in {"add", "rm", "mv", "restore", "stash", "switch", "checkout", "worktree"}:
        draft.capabilities.add("git.stage")
        draft.raise_to(1, f"git {sub} changes the working tree or index")
        discards = (sub == "checkout" and "--" in words) or (sub == "restore" and "--staged" not in flags)
        if discards:
            draft.raise_to(4, "Discards uncommitted changes", reversible=False)
        draft.summary = f"git {sub}"
        return draft
    if sub in {"commit", "merge", "rebase", "cherry-pick", "revert", "tag", "am"}:
        draft.capabilities.add("git.commit")
        draft.raise_to(3, f"git {sub} writes repository history")
        draft.summary = f"git {sub}"
        return draft
    if sub == "push":
        draft.capabilities |= {"git.push", "network.write"}
        draft.hosts.append("git remote")
        if flags & {"--force", "-f", "--force-with-lease", "--mirror", "--delete", "-d"} or any(
                w.startswith("+") for w in args[1:]):
            draft.raise_to(4, "Rewrites or deletes history on the remote", reversible=False)
            draft.summary = "Force push"
        else:
            draft.raise_to(3, "Publishes commits to the remote", reversible=False)
            draft.summary = "git push"
        return draft
    if sub in {"reset", "clean"}:
        draft.capabilities.add("git.history")
        if "--hard" in flags or sub == "clean":
            draft.raise_to(4, "Discards uncommitted work", reversible=False)
        else:
            draft.raise_to(1, "Moves the index")
        draft.summary = f"git {sub}"
        return draft
    draft.capabilities.add("git.history")
    draft.raise_to(1, f"git {sub or 'command'}")
    draft.summary = f"git {sub}".strip()
    return draft


def _assess_gh(words: list[str], draft: _Draft) -> _Draft:
    sub = words[1] if len(words) > 1 else ""
    action = words[2] if len(words) > 2 else ""
    draft.hosts.append("github.com")
    if sub == "pr" and action in {"create", "merge", "close", "edit", "comment", "review", "ready"}:
        draft.capabilities |= {"github.pr", "network.write"}
        draft.raise_to(4 if action == "merge" else 3, f"gh pr {action} acts on GitHub", reversible=action != "merge")
        draft.summary = f"gh pr {action}"
        return draft
    if sub in {"release", "repo", "secret", "workflow", "api"} and action not in {"view", "list", "status", ""}:
        draft.capabilities.add("network.write")
        draft.raise_to(4 if sub in {"secret", "release", "repo"} else 3, f"gh {sub} {action} changes GitHub",
                       reversible=False)
        draft.summary = f"gh {sub} {action}"
        return draft
    draft.capabilities.add("network.read")
    draft.raise_to(2, "Reads from GitHub")
    draft.summary = f"gh {sub}".strip()
    return draft


def assess_command(command: Any) -> RiskAssessment:
    """What running *command* touches, and how badly. Never raises."""
    normalized = normalize_command(command)
    if not normalized:
        return _Draft().freeze()
    draft = _Draft()
    segments = [s.strip() for s in _SEGMENTS.split(normalized) if s.strip()] or [normalized]
    for segment in segments:
        draft.merge(_assess_segment(segment))
    if len(segments) > 1:
        draft.summary = draft.summary or "Compound command"
    # Segments are judged one by one, so this has to look across the pipe.
    if _PIPE_TO_SHELL.search(normalized):
        draft.capabilities.add("network.read")
        draft.raise_to(4, "Runs a downloaded script without reading it", reversible=False)
        draft.summary = "Download and run"
    if writes_a_device(normalized):
        draft.capabilities.add("system.admin")
        draft.raise_to(4, "Writes to a device", reversible=False)
    if re.search(r">>?\s*[^&\s]", normalized) and "filesystem.write" not in draft.capabilities:
        draft.capabilities.add("filesystem.write")
        draft.raise_to(1, "Redirects output into a file")
    # Never describe as safer than the gate treats it.
    legacy = classify_command_risk(normalized)
    if legacy == "high" and draft.level < 3:
        draft.raise_to(3, "Classified high-risk by the command policy")
    if legacy == "medium" and draft.level == 0 and not is_verification_command(normalized):
        draft.raise_to(1)
    extra = 4 if has_shell_control(normalized) and len(segments) > 1 else 0
    return draft.freeze(extra=extra)


# ── tools ─────────────────────────────────────────────────────────────────────

READ_TOOLS = frozenset({
    "read_file", "list_files", "search_code", "glob", "project_context", "git_status", "git_diff",
    "repo_map", "find_symbol", "analyze_file", "notebook_read", "update_todos",
})
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "multi_edit", "apply_change", "apply_patch", "notebook_edit",
})
NETWORK_READ_TOOLS = frozenset({"web_fetch", "web_search", "get_market_data", "get_market_history"})
_PATH_KEYS = ("path", "file_path", "filename", "target", "notebook_path")


def tool_paths(params: Mapping[str, Any] | None) -> list[str]:
    """The file paths a tool call names."""
    paths = []
    for key in _PATH_KEYS:
        value = (params or {}).get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    patch = (params or {}).get("patch")
    if isinstance(patch, str):
        for match in re.finditer(r"^\+\+\+ (?:b/)?(\S+)", patch, re.MULTILINE):
            if match.group(1) != "/dev/null":
                paths.append(match.group(1))
    return paths


def _inside(path: str, root: Optional[Path]) -> bool:
    if root is None:
        return True
    try:
        candidate = Path(path).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def assess_tool(tool: str, params: Mapping[str, Any] | None = None, *,
                root: Optional[Path | str] = None) -> RiskAssessment:
    """What one tool call touches, and how badly. Never raises."""
    name = (tool or "").strip()
    params = params or {}
    root_path = Path(root).expanduser() if root else None
    if name == "run_command":
        return assess_command(params.get("command", ""))
    draft = _Draft()
    if name in READ_TOOLS:
        draft.capabilities.add("filesystem.read")
        draft.summary = "Inspect"
        return draft.freeze()
    if name in WRITE_TOOLS:
        paths = tool_paths(params)
        draft.capabilities.add("filesystem.write")
        draft.files.extend(paths)
        draft.raise_to(1, "Edits files in the workspace (checkpointed)")
        outside = [p for p in paths if not _inside(p, root_path)]
        if outside:
            draft.raise_to(3, "Writes outside the workspace: " + ", ".join(outside))
        secrets = [p for p in paths if _SECRET_PATH.search(" " + p + " ")]
        if secrets:
            draft.capabilities.add("secret.read")
            draft.raise_to(4, "Writes a credentials or secrets file")
        draft.summary = "Edit files"
        return draft.freeze()
    if name in NETWORK_READ_TOOLS:
        draft.capabilities.add("network.read")
        url = params.get("url")
        if isinstance(url, str):
            draft.hosts.extend(_hosts(url))
        draft.raise_to(2, "Fetches from the network")
        draft.summary = "Network request"
        return draft.freeze()
    if name == "github":
        action = str(params.get("action") or params.get("command") or "")
        return assess_command(f"gh {action}") if action else _gh_read()
    if name in {"broker_order"}:
        draft.capabilities.add("network.write")
        draft.raise_to(4, "Places an order with a broker", reversible=False)
        draft.systems.append("Brokerage account")
        draft.summary = "Broker order"
        return draft.freeze()
    draft.capabilities.add("process.execute")
    draft.raise_to(1, f"Runs the {name or 'unknown'} tool")
    draft.summary = f"Run {name}" if name else "Unknown tool"
    return draft.freeze()


def _gh_read() -> RiskAssessment:
    draft = _Draft(capabilities={"network.read"}, hosts=["github.com"], summary="Read GitHub")
    draft.raise_to(2, "Reads from GitHub")
    return draft.freeze()


APPROVAL_MODES = ("manual", "risk")
# The highest level approval_mode=risk may run without asking. L3 (acting
# outside the repo) and L4 (destructive) are never automatic.
MAX_AUTO_LEVEL = 2


def approval_requirement(assessment: RiskAssessment, *, mode: str = "manual", auto_level: int = 1) -> str:
    """How an action that would otherwise need approval should get it.

    ``"always"`` — L4: ask, even when this session already allowed the tool or
    the command prefix. A standing "yes" was given for something milder.
    ``"auto"``   — ``mode="risk"`` and the action is at or below
    ``auto_level`` (capped at :data:`MAX_AUTO_LEVEL`): run without asking.
    ``"ask"``    — everything else: the usual prompt.
    """
    if assessment.level >= 4:
        return "always"
    try:
        ceiling = max(0, min(MAX_AUTO_LEVEL, int(auto_level)))
    except (TypeError, ValueError):
        ceiling = 1
    if mode == "risk" and assessment.level <= ceiling:
        return "auto"
    return "ask"


def unknown_capabilities(names: Iterable[str]) -> list[str]:
    """Names that are not capabilities (a contract typo), in input order."""
    return [name for name in names if name not in CAPABILITIES]


__all__ = [
    "APPROVAL_MODES",
    "CAPABILITIES",
    "MAX_AUTO_LEVEL",
    "approval_requirement",
    "LEVEL_NAMES",
    "RiskAssessment",
    "assess_command",
    "assess_tool",
    "tool_paths",
    "unknown_capabilities",
]
