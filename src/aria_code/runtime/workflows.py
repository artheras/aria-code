"""User-defined workflows: ``.aria/workflows/<name>.yaml``, run as ``/<name>``.

A release is the same five steps every time — test, build, review, write
the changelog, open the PR — and typing them is where one gets skipped. A
workflow file names them once::

    # .aria/workflows/release.yaml
    description: Test, build, review, changelog, PR
    steps:
      - name: Tests
        run: python -m pytest -q
      - name: Build
        run: python -m build
        timeout: 600
      - name: Security review
        command: /review --base main
      - name: Changelog
        prompt: Add a CHANGELOG.md entry for the changes since the last tag. {{args}}
      - name: Open the PR
        run: gh pr create --fill
        confirm: true

A step has exactly one of ``run`` (a shell command, through the same
``run_command`` tool, policy and sandbox as the model's commands),
``prompt`` (a full Aria turn with tools — its checks decide whether it
passed) or ``command`` (any slash command). ``confirm`` asks before the step,
``continue_on_error`` keeps going past a failure, ``timeout`` bounds a
``run``. ``{{args}}`` is what followed the command, ``{{date}}`` today.

The file comes with the repository, so whoever wrote it chose those
commands. The first run of a workflow — and the first after its file
changes — shows every step and asks; the answer is remembered by content
hash in ``<aria home>/trusted_workflows.json``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STEP_KINDS = ("run", "prompt", "command")
_STEP_KEYS = {"name", "run", "prompt", "command", "confirm", "continue_on_error", "timeout"}
_TOP_KEYS = {"name", "description", "steps"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class WorkflowError(ValueError):
    """A workflow file that cannot be run, with the reason."""


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    kind: str          # run | prompt | command
    body: str
    confirm: bool = False
    continue_on_error: bool = False
    timeout: int = 300

    def render(self, variables: dict) -> str:
        return _PLACEHOLDER.sub(lambda m: str(variables.get(m.group(1), m.group(0))), self.body).strip()


@dataclass(frozen=True)
class Workflow:
    name: str
    path: str
    description: str
    steps: tuple[WorkflowStep, ...]
    digest: str = ""

    def outline(self) -> list[str]:
        lines = [f"/{self.name} — {self.description}" if self.description else f"/{self.name}",
                 f"  from {self.path}"]
        for index, step in enumerate(self.steps, 1):
            flags = [flag for flag, on in (("asks first", step.confirm),
                                           ("continues on error", step.continue_on_error)) if on]
            suffix = f"  ({', '.join(flags)})" if flags else ""
            body = " ".join(step.body.split())
            lines.append(f"  {index}. {step.name} — {step.kind}: {body[:90]}{suffix}")
        return lines


def workflow_dirs(workspace: Path | str) -> list[Path]:
    """``.aria/workflows`` in the workspace, then in its git root if different."""
    start = Path(workspace).expanduser().resolve()
    dirs = [start / ".aria" / "workflows"]
    for parent in start.parents:
        if (parent / ".git").exists():
            if parent / ".aria" / "workflows" not in dirs:
                dirs.append(parent / ".aria" / "workflows")
            break
    return [d for d in dirs if d.is_dir()]


def _files(workspace: Path | str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in workflow_dirs(workspace):
        for path in sorted(directory.glob("*.y*ml")):
            if path.suffix in (".yaml", ".yml"):
                found.setdefault(path.stem.lower(), path)
    return found


def names(workspace: Path | str) -> list[str]:
    return sorted(_files(workspace))


def _parse_step(index: int, raw: object, path: Path) -> WorkflowStep:
    where = f"{path.name}, step {index}"
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where}: a step is a mapping with run, prompt or command")
    unknown = set(raw) - _STEP_KEYS
    if unknown:
        raise WorkflowError(f"{where}: unknown key {', '.join(sorted(unknown))}")
    kinds = [kind for kind in STEP_KINDS if raw.get(kind) not in (None, "")]
    if len(kinds) != 1:
        raise WorkflowError(f"{where}: needs exactly one of run, prompt, command")
    kind = kinds[0]
    body = str(raw[kind])
    if kind == "command" and not body.lstrip().startswith("/"):
        raise WorkflowError(f"{where}: command must be a slash command, e.g. /review")
    try:
        timeout = int(raw.get("timeout", 300))
    except (TypeError, ValueError):
        raise WorkflowError(f"{where}: timeout must be a number of seconds") from None
    default_name = " ".join(body.split())[:40]
    return WorkflowStep(name=str(raw.get("name") or default_name), kind=kind, body=body,
                        confirm=bool(raw.get("confirm", False)),
                        continue_on_error=bool(raw.get("continue_on_error", False)),
                        timeout=max(1, timeout))


def parse(path: Path | str) -> Workflow:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"{path}: cannot read: {exc}") from exc
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception as exc:
        raise WorkflowError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path.name}: expected a mapping with steps")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise WorkflowError(f"{path.name}: unknown key {', '.join(sorted(unknown))}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError(f"{path.name}: steps must be a non-empty list")
    name = str(data.get("name") or path.stem).lower()
    if not _NAME.match(name):
        raise WorkflowError(f"{path.name}: name '{name}' must be lowercase letters, digits, - or _")
    return Workflow(
        name=name, path=str(path), description=str(data.get("description") or ""),
        steps=tuple(_parse_step(i, raw, path) for i, raw in enumerate(steps, 1)),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def find(workspace: Path | str, name: str) -> Optional[Workflow]:
    """The workflow called ``name`` (``/name`` works too), or None. Invalid files raise."""
    path = _files(workspace).get(name.lstrip("/").lower())
    return parse(path) if path is not None else None


def variables(workflow: Workflow, args: str) -> dict:
    return {"args": args.strip(), "date": _dt.date.today().isoformat(), "workflow": workflow.name}


# ── trust ──────────────────────────────────────────────────────────────────

@dataclass
class TrustStore:
    path: Path
    entries: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "TrustStore":
        if path is None:
            from ..packages.aria_core.paths import aria_home

            path = aria_home() / "trusted_workflows.json"
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = {}
        return cls(Path(path), entries if isinstance(entries, dict) else {})

    def trusted(self, workflow: Workflow) -> bool:
        return self.entries.get(str(Path(workflow.path).resolve())) == workflow.digest

    def trust(self, workflow: Workflow) -> None:
        self.entries[str(Path(workflow.path).resolve())] = workflow.digest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.entries, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)


TEMPLATE = """\
# /{name} — run with /{name} [args]; {{{{args}}}} is what you type after it.
description: Test, review and summarise the current change
steps:
  - name: Tests
    run: python -m pytest -q
  - name: Review
    command: /review
  - name: Summary
    prompt: Summarise the current change for a pull request description. {{{{args}}}}
    continue_on_error: true
"""
