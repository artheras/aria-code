"""EngineeringCommandsMixin — the everyday commands of a coding session.

/diff /commit /pr /checkpoint /worktree /graph /agents /build /test /fix

Most are a short name for something Aria already does: /diff is `/git` with a
patch, /worktree is `/task`, /checkpoint is `/rewind list`, /graph is `/impact`.
They exist because these are the words people reach for in Codex and Claude
Code, and because "/" should list them. The new work is small and lives in
:func:`detect_commands`, which finds a project's test, build and lint commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Dict, Optional


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _make_targets(root: Path) -> set:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = root / name
        if path.is_file():
            try:
                return set(re.findall(r"^([A-Za-z0-9_.-]+):", path.read_text(errors="ignore"), re.M))
            except OSError:
                return set()
    return set()


_DECLARED_KINDS = ("test", "build", "lint", "sim")


def _is_ros2_workspace(root: Path) -> bool:
    """A colcon workspace (src/<pkg>/package.xml) or a single ROS 2 package."""
    if (root / "package.xml").is_file():
        return True
    src = root / "src"
    if not src.is_dir():
        return False
    try:
        return any((child / "package.xml").is_file() for child in src.iterdir() if child.is_dir()) or any(
            True for _ in src.glob("*/*/package.xml"))
    except OSError:
        return False


def declared_commands(root) -> Dict[str, str]:
    """Commands the project states for itself in `.aria/project.yaml`:

        commands:
          test: ./scripts/test.sh
          sim: ros2 launch my_robot sim_scenarios.launch.py

    These win over anything detected. `sim` has no detection: running a robot
    simulation is never guessed.
    """
    path = Path(root) / ".aria" / "project.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    block = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {}
    return {k: v.strip() for k, v in block.items() if k in _DECLARED_KINDS and isinstance(v, str) and v.strip()}


def detect_commands(root) -> Dict[str, str]:
    """The project's own test, build and lint commands: {"test": ..., "build": ..., "lint": ...}.

    What `.aria/project.yaml` declares wins; otherwise read from what the
    project has (ROS 2 packages, package.json scripts, Cargo.toml, go.mod,
    pyproject.toml, Makefile targets), first match per kind. Nothing is
    guessed for a kind the project does not declare. A colcon test run also
    yields `test_report`, the command that prints its results.
    """
    root = Path(root)
    found: Dict[str, str] = {}

    def set_(kind: str, command: str) -> None:
        found.setdefault(kind, command)

    if _is_ros2_workspace(root):
        # colcon test only runs; the verdicts are in `colcon test-result`.
        set_("build", "colcon build")
        set_("test", "colcon test")
        found["test_report"] = "colcon test-result --verbose"

    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        pm = _package_manager(root)
        if "test" in scripts:
            set_("test", f"{pm} test")
        if "build" in scripts:
            set_("build", f"{pm} run build")
        if "lint" in scripts:
            set_("lint", f"{pm} run lint")
    if (root / "Cargo.toml").is_file():
        set_("test", "cargo test")
        set_("build", "cargo build")
        set_("lint", "cargo clippy --all-targets")
    if (root / "go.mod").is_file():
        set_("test", "go test ./...")
        set_("build", "go build ./...")
        set_("lint", "go vet ./...")
    pyproject = root / "pyproject.toml"
    py_text = ""
    if pyproject.is_file():
        try:
            py_text = pyproject.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            py_text = ""
    if py_text or (root / "setup.py").is_file() or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        if "pytest" in py_text or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
            set_("test", "python3 -m pytest -q")
        if "[build-system]" in py_text:
            set_("build", "python3 -m build")
        if "ruff" in py_text or (root / "ruff.toml").is_file():
            set_("lint", "ruff check .")
    targets = _make_targets(root)
    for kind in ("test", "build", "lint"):
        if kind in targets:
            set_(kind, f"make {kind}")
    declared = declared_commands(root)
    found.update(declared)
    if "test" in declared:
        found.pop("test_report", None)  # the project's own test command reports for itself
    return found


def fix_prompt(description: str, commands: Dict[str, str]) -> str:
    """What /fix asks the model to do: find the cause, change as little as possible, check, repair."""
    checks = ", ".join(f"`{c}`" for c in (commands.get("test"), commands.get("lint")) if c)
    run = f" Run {checks} when you are done." if checks else " Run the project's checks when you are done."
    return (
        f"Fix this: {description.strip()}\n\n"
        "Find the cause first: read the code involved and reproduce the problem if you can. "
        "Make the smallest change that fixes it; do not refactor unrelated code."
        f"{run} If your change broke something, repair it."
    )


def _tail(text: str, lines: int = 30) -> str:
    rows = (text or "").rstrip().splitlines()
    cut = len(rows) - lines
    return ("… +%d lines\n" % cut if cut > 0 else "") + "\n".join(rows[-lines:])


class EngineeringCommandsMixin:
    # ── output ──────────────────────────────────────────────────────────────
    def _eng_say(self, text: str, style: str = "") -> None:
        console = self.context.console
        if self.context.has_rich:
            from rich.markup import escape

            body = escape(text)
            console.print(f"[{style}]{body}[/{style}]" if style else body, highlight=False)
        else:
            print(text)

    def _eng_note_shadow(self, name: str) -> None:
        """A project workflow with this command's name is not shadowed in silence.

        Built-in commands win over `.aria/workflows/<name>.yaml` (a repository
        file must not replace a command the user knows), but `/test`, `/build`
        and `/fix` are names projects plausibly gave their own workflows, so
        say how to reach it.
        """
        try:
            shadowed = self._workflow_named("/" + name)
        except Exception:
            shadowed = False
        if shadowed:
            self._eng_say(f"Note: this project also has a workflow named '{name}'; run it with /workflow run {name}.", "dim")

    def _eng_root(self) -> str:
        return self.terminal.config.get("_session_workspace_root") or os.getcwd()

    async def _eng_call(self, handler, *args):
        """Run another command, which may be sync or async."""
        result = handler(*args)
        if asyncio.iscoroutine(result):
            await result

    def _eng_run(self, command: str, *, timeout: int = 600) -> dict:
        # The wrapper lives in tool_executor; importing it from aria_cli would add a
        # reverse dependency (tests/test_import_graph_budget.py forbids growing them).
        from aria_code.apps.cli.tool_executor import _tool_run_command

        return _tool_run_command({
            "command": command,
            "policy": "balanced",
            "permission_mode": self.terminal.config.get("permission_mode", "workspace-write"),
            "network_enabled": bool(self.terminal.config.get("network_enabled", True)),
            "user_approved": True,  # typed by the user, so it is theirs to run
            "timeout": timeout,
        })

    def _eng_show(self, result: dict, label: str, started: float) -> bool:
        data = result.get("data") or {}
        out = (data.get("stdout") or "") + (("\n" + data["stderr"]) if data.get("stderr") else "")
        if out.strip():
            self._eng_say(_tail(out))
        took = f"{time.monotonic() - started:.1f}s"
        if result.get("success"):
            self._eng_say(f"✓ {label} passed · {took}", "green")
            return True
        self._eng_say(f"✗ {label} failed · {took} · {result.get('error', '')}".rstrip(" ·"), "red")
        return False

    # ── checks ──────────────────────────────────────────────────────────────
    async def _eng_check(self, kind: str, extra: str) -> None:
        commands = detect_commands(self._eng_root())
        command = commands.get(kind)
        if not command:
            if kind == "sim":
                self._eng_say(
                    "No simulation command declared. Add it to .aria/project.yaml:\n"
                    "  commands:\n    sim: ros2 launch my_robot sim_scenarios.launch.py", "dim")
            else:
                self._eng_say(
                    f"No {kind} command found. Aria looked for .aria/project.yaml, ROS 2 packages, package.json "
                    "scripts, Cargo.toml, go.mod, pyproject.toml and Makefile targets.", "dim")
            return
        if extra.strip():
            command += " " + " ".join(shlex.quote(p) for p in shlex.split(extra))
        self._eng_say(f"$ {command}", "dim")
        started = time.monotonic()
        result = await asyncio.to_thread(self._eng_run, command)
        self._eng_show(result, kind, started)
        report = commands.get("test_report") if kind == "test" else None
        if report:
            # The run's own exit code says little (colcon test exits 0 with failures);
            # the report is the verdict.
            self._eng_say(f"$ {report}", "dim")
            started = time.monotonic()
            result = await asyncio.to_thread(self._eng_run, report)
            self._eng_show(result, "test results", started)

    async def cmd_test(self, args: str):
        """/test [paths] — run the project's tests."""
        self._eng_note_shadow("test")
        await self._eng_check("test", args)

    async def cmd_sim(self, args: str):
        """/sim [scenario] — run the simulation the project declares (never guessed)."""
        self._eng_note_shadow("sim")
        await self._eng_check("sim", args)

    async def cmd_build(self, args: str):
        """/build — run the project's build."""
        self._eng_note_shadow("build")
        await self._eng_check("build", "")

    # ── git ─────────────────────────────────────────────────────────────────
    async def cmd_diff(self, args: str):
        """/diff [path ...] | --patch — what changed: a summary, or the patch."""
        self._eng_note_shadow("diff")
        text = args.strip()
        if not text:
            # Two plain read-only commands, not /git summary's `a && b`: the default
            # command policy blocks compound commands.
            await self._eng_call(self.cmd_git, "status")
            await self._eng_call(self.cmd_git, "diff")
        elif text in ("--patch", "-p"):
            await self._eng_call(self.cmd_git, "patch")
        else:
            await self._eng_call(self.cmd_git, "patch " + text)

    async def cmd_commit(self, args: str):
        """/commit [message] — commit everything that changed."""
        self._eng_note_shadow("commit")
        await self._eng_call(self.cmd_git, ("commit " + args.strip()).strip())

    async def cmd_pr(self, args: str):
        """/pr [status|view|create [title]] — the branch's pull request."""
        self._eng_note_shadow("pr")
        words = shlex.split(args) if args.strip() else []
        sub = words[0].lower() if words else "status"
        if shutil.which("gh") is None:
            self._eng_say("The GitHub CLI (gh) is not installed. See https://cli.github.com", "yellow")
            return
        if sub == "status":
            command = "gh pr status"
        elif sub == "view":
            command = "gh pr view"
        elif sub == "create":
            title = " ".join(words[1:]).strip()
            command = "gh pr create --fill" + (f" --title {shlex.quote(title)}" if title else "")
        else:
            self._eng_say("Usage: /pr [status|view|create [title]]", "dim")
            return
        started = time.monotonic()
        result = await asyncio.to_thread(self._eng_run, command, timeout=120)
        self._eng_show(result, command, started)

    # ── session and project ─────────────────────────────────────────────────
    async def cmd_checkpoint(self, args: str):
        """/checkpoint [list | restore [N|id]] — file checkpoints and turn points."""
        self._eng_note_shadow("checkpoint")
        words = args.split()
        sub = words[0].lower() if words else "list"
        if sub == "list":
            await self._eng_call(self.cmd_rewind, "list")
            await self._eng_call(self.cmd_rewind, "turns")
        elif sub == "restore":
            await self._eng_call(self.cmd_rewind, ("turn " + " ".join(words[1:])).strip())
        else:
            self._eng_say("Usage: /checkpoint [list | restore [N|id]]", "dim")

    async def cmd_worktree(self, args: str):
        """/worktree [diff|apply|discard] — the coding task's worktree (same as /task)."""
        self._eng_note_shadow("worktree")
        await self._eng_call(self.cmd_task, args)

    async def cmd_graph(self, args: str):
        """/graph [path|symbol ...] — the project graph; with targets, what a change reaches."""
        self._eng_note_shadow("graph")
        if args.strip():
            await self._eng_call(self.cmd_impact, args)
            return
        from aria_code.runtime.project_graph import load_project_graph

        summary = await asyncio.to_thread(lambda: load_project_graph(self._eng_root()).summary())
        services = summary.get("services") or {}
        lines = [
            f"Project graph: {summary['files']} files · {summary['symbols']} symbols · "
            f"{summary['import_edges']} imports · {summary['tests']} test files",
        ]
        if services:
            lines.append("Services: " + ", ".join(sorted(services)[:12]))
        lines.append("/graph <path|symbol> shows what a change reaches.")
        self._eng_say("\n".join(lines))

    async def cmd_agents(self, args: str):
        """/agents — project agents in .aria/agents and the background tasks running."""
        self._eng_note_shadow("agents")
        folder = Path(self._eng_root()) / ".aria" / "agents"
        names = sorted(p.stem for p in folder.glob("*.md")) if folder.is_dir() else []
        self._eng_say("Project agents: " + (", ".join(names) if names else "none (add .aria/agents/<name>.md)"))
        await self._eng_call(self.cmd_tasks, "")

    async def cmd_fix(self, args: str):
        """/fix <what is wrong> — find the cause, make the smallest change, check, repair."""
        self._eng_note_shadow("fix")
        if not args.strip():
            self._eng_say("Usage: /fix <what is wrong>   e.g. /fix expired sessions are not refreshed", "dim")
            return
        await self.terminal.send_message(fix_prompt(args, detect_commands(self._eng_root())))
