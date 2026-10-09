"""Turn eval reports into a Markdown scoreboard, and decide whether the run is healthy.

    python3 -m aria_code.evals.summary evals-core.json evals-operations.json [--private evals-holdout.json]

Each suite's score comes with the mean per-task pass rate and its standard
error, so two runs can be told apart from noise. A report passed after
``--private`` (the holdout suites, kept in a private repository) is shown as
counts only: no task ids, details or logs, because the summary is public.

The suites ran in CI only with ``--check``: every fixture still starts red,
but no model ever attempted them, so nothing measured whether Aria can fix
code. The weekly workflow (.github/workflows/evals.yml) runs them with a real
model and writes this summary.

A task the model gets wrong is a result, not a broken build: the exit code is
1 only when a task could not run (ERROR) or no longer measures anything
(INVALID), the two outcomes that mean the harness, not the model, needs work.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def mean_sem(report: dict) -> tuple[float, float, int] | None:
    """Mean per-task pass rate over scored attempts, its standard error, and the task count."""
    passed: dict[str, int] = defaultdict(int)
    scored: dict[str, int] = defaultdict(int)
    for r in report.get("results") or []:
        if str(r.get("outcome", "")).lower() in ("pass", "fail"):
            scored[str(r.get("task_id"))] += 1
            passed[str(r.get("task_id"))] += str(r.get("outcome")).lower() == "pass"
    rates = [passed[t] / scored[t] for t in scored]
    if not rates:
        return None
    mean = sum(rates) / len(rates)
    if len(rates) < 2:
        return mean, 0.0, 1
    var = sum((x - mean) ** 2 for x in rates) / (len(rates) - 1)
    return mean, math.sqrt(var / len(rates)), len(rates)


def summarise(reports: list[dict], model: str = "", private: list[dict] | None = None) -> tuple[str, int]:
    """Markdown for the reports, and the exit code the run should have."""
    lines = [f"## Aria eval scoreboard{f' · {model}' if model else ''}", ""]
    unhealthy = 0
    for report in [*reports, *(private or [])]:
        is_private = any(report is p for p in private or [])
        suite = report.get("suite", "?")
        lines.append(f"### {suite}{' (holdout)' if is_private else ''}: "
                     f"{report.get('passed', 0)}/{report.get('scored', 0)} ({report.get('pass_rate', 0):.0%})")
        stats = mean_sem(report)
        if stats:
            mean, sem, n = stats
            lines.append(f"Mean task pass rate {mean:.0%} ± {sem:.0%} (SEM over {n} task{'s' if n != 1 else ''}).")
        extras = [f"{report[k]} {k}" for k in ("errored", "invalid") if report.get(k)]
        if extras:
            lines.append(f"Not scored: {', '.join(extras)}.")
        unhealthy += int(report.get("errored", 0)) + int(report.get("invalid", 0))
        if is_private:
            lines += ["Task ids, details and logs are not published for holdout suites.", ""]
            continue
        lines += ["", "| Task | Outcome | Seconds | Detail |", "|---|---|---:|---|"]
        for result in report.get("results", []):
            detail = str(result.get("detail") or "").replace("|", "\\|")[:120]
            lines.append(f"| {result.get('task_id')} | {result.get('outcome')} | "
                         f"{result.get('seconds', 0):.0f} | {detail} |")
        for result in report.get("results", []):
            tail = result.get("log_tail") or []
            if tail and str(result.get("outcome", "")).lower() != "pass":
                body = "\n".join(tail).replace("```", "` ` `")
                lines += ["", f"<details><summary>{result.get('task_id')}: last lines of the log</summary>",
                          "", "```", body, "```", "</details>"]
        checks = report.get("behavior") or {}
        if checks:
            lines += ["", "| Behaviour check | Passed |", "|---|---:|"]
            lines += [f"| {name.replace('|', '/')} | {s['passed']}/{s['runs']} |" for name, s in checks.items()]
        by_tag = report.get("by_tag") or {}
        if by_tag:
            tags = ", ".join(f"{tag} {s['passed']}/{s['scored']}" for tag, s in by_tag.items() if s.get("scored"))
            if tags:
                lines += ["", f"By tag: {tags}"]
        lines.append("")
    return "\n".join(lines), (1 if unhealthy else 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    model = ""
    if args[:1] == ["--model"] and len(args) > 1:
        model, args = args[1], args[2:]
    public_names, private_names = args, []
    if "--private" in args:
        i = args.index("--private")
        public_names, private_names = args[:i], args[i + 1:]

    def load(names: list[str]) -> list[dict]:
        out = []
        for name in names:
            path = Path(name)
            if path.exists():
                out.append(json.loads(path.read_text(encoding="utf-8")))
            else:
                print(f"(no report at {name})", file=sys.stderr)
        return out

    reports, private = load(public_names), load(private_names)
    if not reports and not private:
        print("no eval reports to summarise", file=sys.stderr)
        return 1
    text, code = summarise(reports, model, private)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
