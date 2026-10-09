"""Behaviour checks: how the agent went about a task, from its tool calls.

A task's ``verify`` says whether the work is right. Some protocol features
are about how it gets there — look up what depends on a function before
changing it, edit a definition by name instead of rewriting the file — and a
correct result says nothing about them. A task may list ``behavior`` checks;
they are judged on the run's JSONL events (``aria-code -p … --format jsonl``,
``ARIA_EVENTS_FULL=1``) and reported next to the outcome, never changing it::

    behavior:
      - called_before: {tool: impact_analysis, before: [edit_file, write_file], path: pricing.py}
      - param_used: {tool: edit_file, param: symbol}
      - not_called: write_file

``called`` / ``not_called``: the tool (or any of a list) ran at least once /
never. ``called_before``: the first call to ``tool`` came before the first
call to any ``before`` tool (default: the edit tools) whose path, command or
targets mention ``path``. ``param_used``: some call to ``tool`` set ``param``.
A label may be given with ``label:``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

CHECKS = ("called", "not_called", "called_before", "param_used")
EDIT_TOOLS = ("edit_file", "write_file", "multi_edit", "apply_patch", "notebook_edit")


@dataclass(frozen=True)
class Call:
    index: int
    tool: str
    params: dict

    def mentions(self, fragment: str) -> bool:
        if not fragment:
            return True
        text = " ".join(str(self.params.get(k) or "") for k in ("path", "file", "command", "targets"))
        return fragment in text


def calls(events_text: str) -> list[Call]:
    """The tool calls in a run's JSONL output, in order; other lines are skipped."""
    found: list[Call] = []
    for line in str(events_text or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "tool.started":
            name = str(event.get("tool") or "").rsplit("__", 1)[-1]
            found.append(Call(len(found), name, dict(event.get("params") or {})))
    return found


def _names(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, (list, tuple)) else (str(value),)


def validate(raw: Any) -> dict:
    """One check from a suite file, or ValueError naming what is wrong with it."""
    if not isinstance(raw, dict):
        raise ValueError("a behavior check is a mapping")
    kinds = [key for key in raw if key in CHECKS]
    unknown = set(raw) - set(CHECKS) - {"label"}
    if unknown or len(kinds) != 1:
        raise ValueError(f"a behavior check needs exactly one of {', '.join(CHECKS)}; got {sorted(raw)}")
    kind = kinds[0]
    spec = raw[kind]
    if kind in ("called_before", "param_used") and not (isinstance(spec, dict) and spec.get("tool")):
        raise ValueError(f"{kind} needs a tool")
    if kind == "param_used" and not spec.get("param"):
        raise ValueError("param_used needs a param")
    return {"type": kind, "spec": spec, "label": str(raw.get("label") or "")}


def describe(check: dict) -> str:
    return check.get("label") or f"{check['type']}: {check['spec']}"


def judge(check: dict, run: list[Call]) -> tuple[bool, str]:
    kind, spec = check["type"], check["spec"]
    if kind in ("called", "not_called"):
        tools = _names(spec.get("tool") if isinstance(spec, dict) else spec)
        hits = [c for c in run if c.tool in tools]
        ok = bool(hits) if kind == "called" else not hits
        return ok, f"{len(hits)} call(s) to {'/'.join(tools)}"
    if kind == "called_before":
        tools, later = _names(spec["tool"]), _names(spec.get("before", EDIT_TOOLS))
        path = str(spec.get("path") or "")
        look = next((c.index for c in run if c.tool in tools), None)
        edit = next((c.index for c in run if c.tool in later and c.mentions(path)), None)
        if edit is None:
            return False, f"no {'/'.join(later)} call on {path or 'any file'}"
        if look is None:
            return False, f"{'/'.join(tools)} never called"
        return look < edit, f"{run[look].tool} at call {look}, first edit at call {edit}"
    tools, param = _names(spec["tool"]), str(spec["param"])
    hits = [c for c in run if c.tool in tools]
    used = [c for c in hits if c.params.get(param) not in (None, "")]
    return bool(used), f"{len(used)}/{len(hits)} {'/'.join(tools)} call(s) set {param}"


def evaluate(checks: Iterable[dict], events_text: str) -> tuple[dict, ...]:
    run = calls(events_text)
    results = []
    for check in checks:
        passed, detail = judge(check, run)
        results.append({"check": describe(check), "passed": passed, "detail": detail})
    return tuple(results)
