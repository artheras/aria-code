"""Score the independent reviewer on fixed changes: false positives and misses.

    python -m aria_code.evals.review_probe evals/review/cases.yaml --model <id> [--repeat 3] [--report out.json]

The suites measure the builder; the reviewer (runtime/review.py) blocks or
passes the builder's diff, and nothing measured whether it is right to. Each
case is a goal and a diff with the verdict it should get: ``expect: pass``
for a correct change it must not block (a false positive costs a repair
round and the user's trust), ``expect: block`` for one that misses the goal
(a miss lets a wrong change through), optionally with ``mentions_any`` — the
finding must name one of these.

A reviewer that errors or answers out of format is an ERROR, not a verdict,
like an agent that never ran. The exit code is 1 only for errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

PASS, FAIL, ERROR = "pass", "fail", "error"


@dataclass(frozen=True)
class Case:
    id: str
    goal: str
    diff: str
    expect: str                      # "pass" | "block"
    mentions_any: tuple[str, ...] = ()


def load_cases(path: str | Path) -> list[Case]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases = []
    for raw in data.get("cases") or []:
        expect = str(raw.get("expect") or "")
        if expect not in ("pass", "block") or not raw.get("goal") or not raw.get("diff"):
            raise ValueError(f"case {raw.get('id')!r}: needs goal, diff and expect: pass|block")
        cases.append(Case(id=str(raw["id"]), goal=str(raw["goal"]), diff=str(raw["diff"]), expect=expect,
                          mentions_any=tuple(str(m) for m in raw.get("mentions_any") or ())))
    return cases


def score(case: Case, report: Any) -> tuple[str, str]:
    """(outcome, detail) for one reviewer answer."""
    if getattr(report, "verdict", "") == "error":
        return ERROR, f"reviewer error: {getattr(report, 'error', '')}"
    blocking = list(getattr(report, "blocking", ()) or ())
    if case.expect == "pass":
        if blocking:
            return FAIL, f"false positive: {len(blocking)} blocking — {blocking[0].issue[:120]}"
        return PASS, "not blocked"
    if not blocking:
        return FAIL, "missed: no blocking finding"
    text = " ".join(f"{getattr(f, 'file', '')} {getattr(f, 'issue', '')}" for f in blocking).lower()
    if case.mentions_any and not any(m.lower() in text for m in case.mentions_any):
        return FAIL, f"blocked, but not for {' / '.join(case.mentions_any)}: {blocking[0].issue[:120]}"
    return PASS, f"blocked: {blocking[0].issue[:120]}"


Review = Callable[..., Awaitable[Any]]


async def run_cases(cases: list[Case], review: Review, *, repeat: int = 1) -> list[dict]:
    results = []
    for attempt in range(1, max(1, repeat) + 1):
        for case in cases:
            try:
                report = await review(goal=case.goal, diff=case.diff)
                outcome, detail = score(case, report)
            except Exception as exc:  # a provider outage is not the reviewer's verdict
                outcome, detail = ERROR, f"{type(exc).__name__}: {exc}"
            results.append({"case": case.id, "expect": case.expect, "attempt": attempt,
                            "outcome": outcome, "detail": detail})
    return results


def summary(results: list[dict]) -> dict:
    def rate(expect: str) -> dict:
        scored = [r for r in results if r["expect"] == expect and r["outcome"] != ERROR]
        return {"passed": sum(r["outcome"] == PASS for r in scored), "scored": len(scored)}

    return {"correct_changes_not_blocked": rate("pass"), "wrong_changes_blocked": rate("block"),
            "errors": sum(r["outcome"] == ERROR for r in results), "results": results}


def _gate_review(model: str) -> Review:
    from aria_code.aria_cli import load_config
    from aria_code.apps.cli.providers.runtime_bridge import build_review_gate

    config = {**load_config(), "review_gate": True}
    if model:
        config["model"] = model
    gate = build_review_gate(config, model=config.get("model", ""), api_url=config.get("api_url"),
                             ollama_url=config.get("ollama_url", "http://localhost:11434"))
    return gate.run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review_probe", description=__doc__.splitlines()[0])
    parser.add_argument("cases")
    parser.add_argument("--model", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    results = asyncio.run(run_cases(cases, _gate_review(args.model), repeat=args.repeat))
    for r in results:
        icon = {PASS: "✓", FAIL: "✗"}.get(r["outcome"], "!")
        print(f"  {icon} {r['case']} (expect {r['expect']}) — {r['detail']}")
    data = summary(results)
    ok, wrong = data["correct_changes_not_blocked"], data["wrong_changes_blocked"]
    print(f"\nreviewer: correct changes not blocked {ok['passed']}/{ok['scored']} · "
          f"wrong changes blocked {wrong['passed']}/{wrong['scored']}"
          + (f" · {data['errors']} error" if data["errors"] else ""))
    if args.report:
        Path(args.report).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if data["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
