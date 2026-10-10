"""The delivery report — how a coding turn ends, written from evidence.

A turn used to end with whatever the model chose to say: "I have successfully
implemented…", sometimes over a red test run, sometimes with nothing changed.
The runtime knows better than the model what happened — which files changed and
by how much, which checks ran and how they came out, what the change contract
refused, the riskiest thing that ran, and where the checkpoints are — so it
writes the ending itself, in one fixed shape:

    DONE

    Changed
      M src/auth/session.py          +43 -18
      A src/auth/refresh.py          +82

    Verified
      ✓ python3 -m pytest -q

    Review
      Not reviewed

    Risk
      L1 low · Edit files

    Checkpoint
      2 checkpoints · /rewind code <run>

    Next
      Ready to commit

The status comes from evidence, never from the model's text: a turn that
stopped early, or whose checks are red, is INCOMPLETE however confident its
last message sounds.

A turn that changed nothing, ran no checks and had nothing refused gets no
report: questions and read-only exploration end as they always did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from aria_code.safety.risk import LEVEL_NAMES, assess_tool

from .acceptance import DEFAULT_MUTATING_TOOLS, extract_mutated_paths


@dataclass
class ChangedFile:
    path: str
    added: int = 0
    removed: int = 0
    created: bool = False
    # Definitions touched, from runtime/semantic_diff: "refresh() modified".
    symbols: tuple = ()
    # Test files that reference what changed (repo_map), relative to the root.
    tested_by: tuple = ()

    @property
    def mark(self) -> str:
        return "A" if self.created else "M"


@dataclass(frozen=True)
class DeliveryReport:
    status: str                          # "done" | "incomplete"
    changed: tuple = ()
    checks: tuple = ()                   # ({"command", "passed", "exit_code"}, …)
    verified: Optional[bool] = None
    review: str = "Not reviewed"
    review_lines: tuple = ()
    behaviour: Optional[dict] = None
    risk_level: Optional[int] = None
    risk_summary: str = ""
    checkpoints: tuple = ()
    refused: tuple = ()
    stop_reason: str = "completed"
    next: str = ""
    impact: str = ""                     # what depends on the changed files (project graph)

    @property
    def worth_showing(self) -> bool:
        return bool(self.changed or self.checks or self.refused)

    @property
    def added(self) -> int:
        return sum(item.added for item in self.changed)

    @property
    def removed(self) -> int:
        return sum(item.removed for item in self.changed)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "changed": [vars(item).copy() for item in self.changed],
            "checks": [dict(item) for item in self.checks],
            "verified": self.verified,
            "review": self.review,
            "review_lines": list(self.review_lines),
            "behaviour": dict(self.behaviour) if self.behaviour else None,
            "risk": None if self.risk_level is None else {
                "level": self.risk_level, "name": LEVEL_NAMES[self.risk_level], "summary": self.risk_summary},
            "checkpoints": list(self.checkpoints),
            "refused": [dict(item) for item in self.refused],
            "stop_reason": self.stop_reason,
            "next": self.next,
            "impact": self.impact,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryReport":
        risk = data.get("risk") or {}
        return cls(
            status=str(data.get("status") or "done"),
            changed=tuple(ChangedFile(**{**item, "symbols": tuple(item.get("symbols") or ()),
                                         "tested_by": tuple(item.get("tested_by") or ())})
                          for item in data.get("changed") or ()),
            checks=tuple(dict(item) for item in data.get("checks") or ()),
            verified=data.get("verified"),
            review=str(data.get("review") or "Not reviewed"),
            review_lines=tuple(data.get("review_lines") or ()),
            behaviour=data.get("behaviour") or None,
            risk_level=risk.get("level"),
            risk_summary=str(risk.get("summary") or ""),
            checkpoints=tuple(data.get("checkpoints") or ()),
            refused=tuple(dict(item) for item in data.get("refused") or ()),
            stop_reason=str(data.get("stop_reason") or "completed"),
            next=str(data.get("next") or ""),
            impact=str(data.get("impact") or ""),
        )

    def render(self, *, rewind_hint: str = "", root: Optional[Path | str] = None) -> str:
        lines = [self.status.upper()]
        if self.changed:
            width = max(len(_display(item.path, root)) for item in self.changed)
            lines += ["", "Changed"]
            for item in self.changed:
                stats = f"+{item.added}" + (f" -{item.removed}" if item.removed or not item.created else "")
                lines.append(f"  {item.mark} {_display(item.path, root):<{width}}  {stats}")
                if item.symbols:
                    shown = " · ".join(item.symbols[:4])
                    more = f" · +{len(item.symbols) - 4}" if len(item.symbols) > 4 else ""
                    lines.append(f"      {shown}{more}")
                if item.tested_by:
                    shown = ", ".join(item.tested_by[:3])
                    more = f" +{len(item.tested_by) - 3}" if len(item.tested_by) > 3 else ""
                    lines.append(f"      tested by {shown}{more}")
            if len(self.changed) > 1:
                lines.append(f"  {len(self.changed)} files · +{self.added} / -{self.removed}")
            if self.impact:
                lines.append(f"  impact {self.impact}")
        lines += ["", "Verified"]
        if self.checks:
            for check in self.checks:
                ok = check.get("passed")
                tail = "" if ok else f"  (exit {check.get('exit_code')})"
                if not ok and check.get("preexisting"):
                    tail += " — already failing before this change"
                lines.append(f"  {'✓' if ok else '✗'} {check.get('command', '')}{tail}")
                new = check.get("new_failures") or ()
                if new:
                    shown = ", ".join(new[:3]) + (f" … +{len(new) - 3}" if len(new) > 3 else "")
                    lines.append(f"      new failures: {shown}")
            if self.verified is None and self.changed:
                lines.append("  ⚠ changed again after the last check")
        else:
            lines.append("  — no check ran" + (" (none could be inferred)" if self.changed else ""))
        if self.behaviour:
            lines += ["", "Behaviour"]
            for key in ("before", "after", "why", "impact"):
                if self.behaviour.get(key):
                    lines.append(f"  {key.capitalize():<7}{self.behaviour[key]}")
        lines += ["", "Review"] + [f"  {line}" for line in (self.review_lines or (self.review,))]
        if self.risk_level is not None:
            lines += ["", "Risk", f"  L{self.risk_level} {LEVEL_NAMES[self.risk_level]}"
                      + (f" · {self.risk_summary}" if self.risk_summary else "")]
        if self.refused:
            lines += ["", "Contract"]
            lines += [f"  × {item.get('tool', '')}: {item.get('reason', '')}" for item in self.refused]
        if self.checkpoints:
            count = len(self.checkpoints)
            label = f"{count} checkpoint{'s' if count != 1 else ''}"
            lines += ["", "Checkpoint", f"  {label}" + (f" · {rewind_hint}" if rewind_hint else "")]
        if self.next:
            lines += ["", "Next", f"  {self.next}"]
        return "\n".join(lines)


def _display(path: str, root: Optional[Path | str]) -> str:
    if root:
        try:
            return Path(path).resolve().relative_to(Path(root).expanduser().resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path


_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def _diff_stats(diff: str) -> tuple[int, int, bool]:
    added = removed = 0
    created = False
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            if line.startswith("--- /dev/null"):
                created = True
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed, created


def _data(result: Any) -> Mapping:
    if not isinstance(result, Mapping):
        return {}
    data = result.get("data")
    return data if isinstance(data, Mapping) else result


@dataclass
class DeliveryLedger:
    """What a turn did, recorded as it happens; turned into a report at the end."""

    root: Optional[str] = None
    mutating_tools: frozenset = DEFAULT_MUTATING_TOOLS
    _changed: dict = field(default_factory=dict)
    _checkpoints: list = field(default_factory=list)
    _diffs: list = field(default_factory=list)
    _file_diffs: dict = field(default_factory=dict)
    _risk: Optional[tuple] = None

    def record(self, tool: str, params: Mapping | None, result: Any) -> None:
        if isinstance(result, Mapping) and result.get("contract_violation"):
            return  # refused calls did not run; the contract summary has them
        if isinstance(result, Mapping) and result.get("success") is not False:
            assessment = assess_tool(tool, params or {}, root=self.root)
            if self._risk is None or assessment.level > self._risk[0]:
                self._risk = (assessment.level, assessment.summary)
        paths = extract_mutated_paths(tool, result, mutating_tools=self.mutating_tools)
        if not paths:
            return
        data = _data(result)
        diff = str(data.get("diff") or "")
        if diff.strip():
            self._diffs.append(diff if diff.endswith("\n") else diff + "\n")
        added, removed, created = _diff_stats(diff)
        action = str(data.get("action") or "").lower()
        created = created or action in {"created", "create", "write new file"}
        for index, path in enumerate(paths):
            entry = self._changed.get(path)
            if entry is None:
                entry = self._changed[path] = ChangedFile(path=path, created=created)
            if index == 0:  # one diff per call; attribute it to the first path
                entry.added += added
                entry.removed += removed
                if diff.strip():
                    self._file_diffs.setdefault(path, []).append(diff)
        checkpoint = data.get("checkpoint_id")
        if checkpoint and checkpoint not in self._checkpoints:
            self._checkpoints.append(str(checkpoint))

    @property
    def changed(self) -> bool:
        return bool(self._changed)

    def _symbol_changes(self, path: str) -> list:
        diffs = self._file_diffs.get(path)
        if not diffs:
            return []
        try:
            from .semantic_diff import file_symbol_changes

            return list(file_symbol_changes(path, diffs) or [])
        except Exception:
            return []

    def _tested_by(self, changes: Sequence) -> tuple:
        """Test files that reference a definition this change added or modified.

        A method counts only where its class is referenced too: "get" alone
        appears in half the tests of any project.
        """
        wanted = [c.name.split(".") for c in changes if c.kind != "removed"]
        if not wanted or not self.root:
            return ()
        try:
            from .repo_map import get_repo_map

            refs = get_repo_map(self.root).refs
        except Exception:
            return ()
        found: set = set()
        for parts in wanted:
            files = set(refs.get(parts[-1], ()))
            for outer in parts[:-1]:
                files &= set(refs.get(outer, ()))
            found |= {f for f in files if _is_test_path(f)}
        return tuple(sorted(found))

    @staticmethod
    def _impact(changed) -> str:
        sources = [item.path for item in changed if not _is_test_path(item.path)]
        if not sources:
            return ""
        try:
            from .project_graph import impact_line, impact_of_paths

            return impact_line(impact_of_paths(sources))
        except Exception:
            return ""

    def diff_text(self) -> str:
        """Every applied change's diff, in order — what a reviewer reads."""
        return "".join(self._diffs)

    def report(
        self,
        *,
        acceptance: Optional[Mapping] = None,
        contract: Optional[Mapping] = None,
        review: Optional[Mapping] = None,
        stop_reason: str = "completed",
    ) -> DeliveryReport:
        checks: list = []
        reports = list((acceptance or {}).get("reports") or [])
        if reports:
            checks = [
                {"command": c.get("command", ""), "passed": bool(c.get("passed")), "exit_code": c.get("exit_code"),
                 **({"preexisting": True} if c.get("preexisting") else {}),
                 **({"new_failures": list(c["new_failures"])} if c.get("new_failures") else {})}
                for c in reports[-1].get("checks") or []
            ]
        verified = (acceptance or {}).get("verified")
        refused = tuple(dict(item) for item in (contract or {}).get("refused") or ())
        changed = tuple(self._changed.values())
        for item in changed:
            changes = self._symbol_changes(item.path)
            item.symbols = tuple(change.label() for change in changes)
            item.tested_by = self._tested_by(changes)

        if stop_reason != "completed":
            status = "incomplete"
            next_step = {
                "max_rounds": "Ran out of rounds — continue, or narrow the task",
                "budget_exhausted": "Budget reached — raise it or narrow the task",
                "loop_guard": "Stopped repeating a failing call — look at the error above",
                "checks_failed": "Fix the failing checks",
                "text_tool_calls": "The model wrote tool calls as text — retry the turn",
                "prompt_echo": "The model stopped without an answer — retry the turn",
            }.get(stop_reason, f"Stopped: {stop_reason}")
        elif verified is False and checks and all(c["passed"] or c.get("preexisting") for c in checks):
            # Red only where it was red before this change (runtime/baseline.py).
            status = "done"
            next_step = "Ready to commit — the failing checks already failed before this change"
        elif verified is False or any(not c["passed"] for c in checks):
            status = "incomplete"
            next_step = "Fix the failing checks"
        elif changed:
            status = "done"
            next_step = ("Ready to commit" if verified else
                         "Review the changes — no check ran" if not checks else
                         "Re-run the checks: files changed after the last run")
        else:
            status = "done"
            next_step = ""
        review_blocks = bool(review) and review.get("verdict") == "blocking"
        if review_blocks and status == "done":
            status = "incomplete"
            next_step = "Address the review's blocking findings"
        if refused and status == "done":
            next_step = "Some calls were refused by the change contract — see above" if not changed \
                else next_step + " · some calls were refused by the contract"

        return DeliveryReport(
            status=status,
            changed=changed,
            checks=tuple(checks),
            verified=verified,
            impact=self._impact(changed),
            review=str((review or {}).get("headline") or "Not reviewed"),
            review_lines=tuple((review or {}).get("lines") or ()),
            behaviour=(review or {}).get("behaviour") or None,
            risk_level=self._risk[0] if self._risk else None,
            risk_summary=self._risk[1] if self._risk else "",
            checkpoints=tuple(self._checkpoints),
            refused=refused,
            stop_reason=stop_reason,
            next=next_step,
        )


def report_from_activities(
    activities: Iterable[tuple[str, Mapping, Any]],
    *,
    root: Optional[str] = None,
    acceptance: Optional[Mapping] = None,
    contract: Optional[Mapping] = None,
    review: Optional[Mapping] = None,
    stop_reason: str = "completed",
) -> DeliveryReport:
    ledger = DeliveryLedger(root=root)
    for tool, params, result in activities:
        ledger.record(tool, params, result)
    return ledger.report(acceptance=acceptance, contract=contract, review=review, stop_reason=stop_reason)


__all__ = ["ChangedFile", "DeliveryLedger", "DeliveryReport", "report_from_activities"]
