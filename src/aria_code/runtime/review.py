"""The review gate — an independent reviewer reads the change before "done".

Asking the model that wrote a change whether it is right gets the same
reasoning that produced it, mistakes included. The reviewer here is a separate
model call with a fresh context and no tools. It sees only what an outside
reviewer would: the user's goal, the change contract, the checks that ran and
their results, and the diff. Not the builder's reasoning, not the transcript.

It answers in JSON — a verdict and findings, each ``blocking`` or
``suggestion``. Blocking means the change is wrong or unsafe for the goal: a
bug, a broken edge case, a security hole, a contract breach. Style never is.
When it blocks and an attempt is left, the findings go back to the builder as
the next message, the same way red checks do; otherwise they go into the
delivery report's *Review* section.

A reviewer that fails — no answer, no JSON — never blocks: the report says the
review did not complete, and the turn ends on its other evidence.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

REVIEWER_SYSTEM = (
    "You are an independent code reviewer. You did not write the change in front "
    "of you, and you cannot run or edit anything: you read and judge. Do not call "
    "tools. Answer with one JSON object and nothing else."
)

_INSTRUCTIONS = """\
Review the change below against the goal.

Report as BLOCKING only what makes the change wrong or unsafe for the goal:
incorrect behaviour, a missed requirement, a broken edge case the goal implies,
a security problem, data loss, or a breach of the change contract. Everything
else — naming, style, structure, extra tests you would like — is a SUGGESTION.
Do not report problems in code the change did not touch. If you are unsure
whether something is a bug, it is a suggestion.

Answer with exactly this JSON shape:
{"verdict": "pass" | "blocking",
 "summary": "<one sentence>",
 "findings": [{"severity": "blocking" | "suggestion",
               "file": "<path>", "line": <number or null>,
               "issue": "<what is wrong and why, one or two sentences>"}],
 "behaviour": {"before": "<what the code did before, one sentence>",
               "after": "<what it does now>",
               "why": "<why the change was needed, from the goal>",
               "impact": "<callers, modules or users affected>"}}
"verdict" is "blocking" exactly when a finding is blocking. "behaviour" describes
the change in terms of behaviour, not lines; leave a field empty if the diff does
not say."""

Reviewer = Callable[[str], Union[str, Awaitable[str]]]


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    issue: str
    file: str = ""
    line: Optional[int] = None

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def where(self) -> str:
        if not self.file:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def as_dict(self) -> dict:
        return {"severity": self.severity, "file": self.file, "line": self.line, "issue": self.issue}


@dataclass(frozen=True)
class ReviewReport:
    verdict: str                 # "pass" | "blocking" | "error"
    summary: str = ""
    findings: tuple = ()
    attempt: int = 1
    error: str = ""
    behaviour: Optional[dict] = None

    @property
    def blocking(self) -> tuple:
        return tuple(f for f in self.findings if f.blocking)

    @property
    def suggestions(self) -> tuple:
        return tuple(f for f in self.findings if not f.blocking)

    def headline(self) -> str:
        if self.verdict == "error":
            return f"Review did not complete: {self.error}"
        if self.blocking:
            count = len(self.blocking)
            return f"Review: {count} blocking finding{'s' if count != 1 else ''}"
        tail = f" · {len(self.suggestions)} suggestion{'s' if len(self.suggestions) != 1 else ''}" \
            if self.suggestions else ""
        return f"Review: no blocking findings{tail}"

    def lines(self, limit: int = 5) -> list[str]:
        """The Review section of the delivery report."""
        if self.verdict == "error":
            return [f"⚠ did not complete: {self.error}"]
        out = [("✗ " if self.blocking else "✓ ") + self.headline().removeprefix("Review: ")]
        for finding in (self.blocking + self.suggestions)[:limit]:
            mark = "✗" if finding.blocking else "·"
            where = f"{finding.where()} — " if finding.where() else ""
            out.append(f"  {mark} {where}{finding.issue}")
        hidden = len(self.findings) - limit
        if hidden > 0:
            out.append(f"  … +{hidden} more")
        return out

    def repair_directive(self) -> str:
        items = "\n".join(
            f"- {finding.where() + ': ' if finding.where() else ''}{finding.issue}" for finding in self.blocking
        )
        return (
            "[Independent review]\n"
            "A reviewer who saw only the goal, the contract, the checks and your diff "
            f"found {len(self.blocking)} blocking problem{'s' if len(self.blocking) != 1 else ''}:\n"
            f"{items}\n\n"
            "Fix them, or — if a finding is wrong — say why in your final message. "
            "The checks will run again after you change files."
        )

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [finding.as_dict() for finding in self.findings],
            "attempt": self.attempt,
            "error": self.error,
            "headline": self.headline(),
            "lines": self.lines(),
            "behaviour": dict(self.behaviour) if self.behaviour else None,
        }


def parse_review(text: str, *, attempt: int = 1) -> ReviewReport:
    """The reviewer's answer as a report. Never raises; unparseable is an error."""
    raw = str(text or "").strip()
    if not raw:
        return ReviewReport("error", attempt=attempt, error="the reviewer gave no answer")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return ReviewReport("error", attempt=attempt, error="the reviewer's answer was not JSON")
    if not isinstance(data, dict):
        return ReviewReport("error", attempt=attempt, error="the reviewer's answer was not a JSON object")
    findings = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict) or not str(item.get("issue") or "").strip():
            continue
        severity = "blocking" if str(item.get("severity", "")).lower() == "blocking" else "suggestion"
        line = item.get("line")
        findings.append(ReviewFinding(
            severity=severity,
            issue=" ".join(str(item["issue"]).split()),
            file=str(item.get("file") or "").strip(),
            line=line if isinstance(line, int) and line > 0 else None,
        ))
    # The verdict follows the findings: a "blocking" verdict with no blocking
    # finding gives the builder nothing to fix, and a "pass" over one would
    # hide it.
    verdict = "blocking" if any(f.blocking for f in findings) else "pass"
    raw_behaviour = data.get("behaviour") or data.get("behavior")
    behaviour = None
    if isinstance(raw_behaviour, dict):
        behaviour = {key: " ".join(str(raw_behaviour.get(key) or "").split())[:240]
                     for key in ("before", "after", "why", "impact")}
        behaviour = {k: v for k, v in behaviour.items() if v} or None
    return ReviewReport(verdict, summary=str(data.get("summary") or "").strip(),
                        findings=tuple(findings), attempt=attempt, behaviour=behaviour)


def build_review_prompt(*, goal: str, diff: str, checks: Sequence[dict] = (), contract: str = "",
                        max_diff_chars: int = 24_000) -> str:
    if len(diff) > max_diff_chars:
        omitted = len(diff) - max_diff_chars
        diff = diff[:max_diff_chars] + f"\n… [{omitted:,} characters of diff omitted]"
    check_lines = "\n".join(
        f"{'PASS' if c.get('passed') else 'FAIL'}  {c.get('command', '')}" for c in checks
    ) or "(no checks ran)"
    parts = [_INSTRUCTIONS, "", "GOAL", goal.strip() or "(not stated)"]
    if contract:
        parts += ["", "CONTRACT", contract.strip()]
    parts += ["", "CHECKS", check_lines, "", "DIFF", diff.strip() or "(empty)"]
    return "\n".join(parts)


@dataclass
class ReviewGate:
    """Arms when a turn has changed files; reviews when the turn says it is done."""

    reviewer: Reviewer
    max_attempts: int = 1
    max_diff_chars: int = 24_000
    reports: list = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.reports)

    @property
    def last(self) -> Optional[ReviewReport]:
        return self.reports[-1] if self.reports else None

    def should_run(self, *, changed: bool, reviewed_diff: str, diff: str) -> bool:
        """Review a change once per distinct diff, up to max_attempts + 1 times."""
        if not changed or not diff.strip():
            return False
        if self.reports and diff == reviewed_diff:
            return False
        return self.attempts <= self.max_attempts

    def can_repair(self) -> bool:
        return self.attempts <= self.max_attempts

    async def run(self, *, goal: str, diff: str, checks: Sequence[dict] = (), contract: str = "") -> ReviewReport:
        prompt = build_review_prompt(goal=goal, diff=diff, checks=checks, contract=contract,
                                     max_diff_chars=self.max_diff_chars)
        try:
            answer = self.reviewer(prompt)
            if inspect.isawaitable(answer):
                answer = await answer
            report = parse_review(str(answer or ""), attempt=self.attempts + 1)
        except Exception as exc:
            report = ReviewReport("error", attempt=self.attempts + 1, error=f"{type(exc).__name__}: {exc}")
        self.reports.append(report)
        return report

    def summary(self) -> Optional[dict]:
        return self.last.as_dict() if self.last else None


__all__ = [
    "REVIEWER_SYSTEM",
    "ReviewFinding",
    "ReviewGate",
    "ReviewReport",
    "build_review_prompt",
    "parse_review",
]
