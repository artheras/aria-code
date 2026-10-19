"""A model scorecard: how each model does on Aria's verifiable tasks, and what routing could save.

    python3 -m aria_code.evals.scorecard run \\
        --model aria=google/gemini-3.8-flash --model claude=anthropic/claude-sonnet-5-5 \\
        --suite evals/suites/core.yaml --suite evals/suites/operations.yaml --repeat 3 --out scorecard/
    python3 -m aria_code.evals.scorecard run ... --yes        # without --yes it only prints the plan
    python3 -m aria_code.evals.scorecard report scorecard/ --cost aria=0.02 --cost claude=0.30

Before training a router (or anything else) the question is whether routing is
worth anything: on the tasks Aria is actually given, does a cheaper model succeed
often enough that sending it there beats always using the strongest one? Nobody
knows until the same tasks have been run on several models and checked by the same
verifiers. This does that, keeps each run's trajectories (the first training
material), and answers the question with a number.

The routing figure is an upper bound. It assumes the router knows in advance which
model will pass; a real one has to guess, and a failed cheap attempt costs money
before the strong model starts. If even the upper bound is small, a learned router
is not worth building.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

#: A model "passes" a task when it passes at least this share of its scored attempts.
PASS_SHARE = 0.5


def wilson(passed: int, scored: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson interval for a pass rate; (0, 1) when nothing was scored."""
    if scored <= 0:
        return 0.0, 1.0
    p = passed / scored
    denom = 1 + z * z / scored
    centre = (p + z * z / (2 * scored)) / denom
    half = z * math.sqrt(p * (1 - p) / scored + z * z / (4 * scored * scored)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def parse_labelled(items: Sequence[str], what: str) -> Dict[str, str]:
    """`["aria=google/x", "claude=anthropic/y"]` as an ordered {label: value}."""
    out: Dict[str, str] = {}
    for item in items:
        label, sep, value = item.partition("=")
        if not sep or not label.strip() or not value.strip():
            raise ValueError(f"{what} must look like label=value, got {item!r}")
        out[label.strip()] = value.strip()
    return out


def load_reports(root: Path) -> Dict[str, Dict[str, dict]]:
    """`root/<label>/<suite>.json` as {label: {suite: report}}, labels in directory order."""
    reports: Dict[str, Dict[str, dict]] = {}
    for label_dir in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        for path in sorted(label_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "results" in data:
                reports.setdefault(label_dir.name, {})[str(data.get("suite") or path.stem)] = data
    return reports


def _scored(result: Mapping) -> bool:
    return str(result.get("outcome", "")).lower() in ("pass", "fail")


def model_stats(by_suite: Mapping[str, dict]) -> dict:
    """Overall and per-domain (tag) numbers for one model across the suites it ran."""
    passed = scored = errored = invalid = 0
    seconds: List[float] = []
    domains: Dict[str, List[int]] = {}
    for report in by_suite.values():
        for result in report.get("results") or []:
            outcome = str(result.get("outcome", "")).lower()
            errored += outcome == "error"
            invalid += outcome == "invalid"
            if not _scored(result):
                continue
            scored += 1
            passed += outcome == "pass"
            seconds.append(float(result.get("seconds") or 0))
            for tag in result.get("tags") or []:
                stats = domains.setdefault(str(tag), [0, 0])
                stats[0] += outcome == "pass"
                stats[1] += 1
    low, high = wilson(passed, scored)
    return {
        "passed": passed, "scored": scored, "errored": errored, "invalid": invalid,
        "pass_rate": passed / scored if scored else 0.0, "ci95": (low, high),
        "mean_seconds": sum(seconds) / len(seconds) if seconds else 0.0,
        "domains": {tag: {"passed": p, "scored": n, "pass_rate": p / n if n else 0.0}
                    for tag, (p, n) in sorted(domains.items())},
    }


def task_matrix(reports: Mapping[str, Mapping[str, dict]]) -> Dict[str, Dict[str, Tuple[int, int]]]:
    """{"suite/task": {label: (passed, scored)}} over every attempt of every model."""
    matrix: Dict[str, Dict[str, List[int]]] = {}
    for label, by_suite in reports.items():
        for suite, report in by_suite.items():
            for result in report.get("results") or []:
                if not _scored(result):
                    continue
                cell = matrix.setdefault(f"{suite}/{result.get('task_id')}", {}).setdefault(label, [0, 0])
                cell[0] += str(result.get("outcome", "")).lower() == "pass"
                cell[1] += 1
    return {task: {label: (p, n) for label, (p, n) in cells.items()} for task, cells in matrix.items()}


def passes(cell: Optional[Tuple[int, int]]) -> bool:
    return bool(cell) and cell[1] > 0 and cell[0] / cell[1] >= PASS_SHARE


def routing_analysis(matrix: Mapping[str, Mapping[str, Tuple[int, int]]], order: Sequence[str],
                     costs: Optional[Mapping[str, float]] = None, strong: str = "") -> dict:
    """What an oracle router would have scored and spent, against always using one model.

    `order` is cheapest first (or by `costs` when given). The oracle sends each task
    to the cheapest model that passes it; `strong` (default: the model with the best
    pass rate) is the always-use-it baseline. Costs are per task attempt, supplied by
    the caller, because the eval reports do not carry token counts.
    """
    labels = sorted({l for cells in matrix.values() for l in cells}, key=lambda l: (
        (costs or {}).get(l, float("inf")) if costs else 0.0, list(order).index(l) if l in order else len(order)))
    if not labels or not matrix:
        return {}
    rates = {l: sum(passes(cells.get(l)) for cells in matrix.values()) for l in labels}
    strong = strong if strong in labels else max(labels, key=lambda l: (rates[l], -labels.index(l)))
    cost = lambda l: float((costs or {}).get(l, 0.0))  # noqa: E731
    chosen: Dict[str, str] = {}
    for task, cells in matrix.items():
        for label in labels:
            if passes(cells.get(label)):
                chosen[task] = label
                break
    tasks = len(matrix)
    # A task nobody solved still gets tried on the strongest model, so it is charged there.
    oracle_cost = sum(cost(l) for l in chosen.values()) + cost(strong) * (tasks - len(chosen))
    strong_solved = sum(passes(cells.get(strong)) for cells in matrix.values())
    by_label: Dict[str, int] = {}
    for label in chosen.values():
        by_label[label] = by_label.get(label, 0) + 1
    return {
        "tasks": tasks, "strong": strong, "order": labels,
        "strong_solved": strong_solved, "oracle_solved": len(chosen),
        "strong_cost": cost(strong) * tasks, "oracle_cost": oracle_cost,
        "sent_to": dict(sorted(by_label.items(), key=lambda kv: labels.index(kv[0]))),
        "unsolved": sorted(t for t in matrix if t not in chosen),
        "only_one_model": sorted(t for t, cells in matrix.items() if sum(passes(c) for c in cells.values()) == 1),
        "has_costs": bool(costs),
    }


def render_markdown(reports: Mapping[str, Mapping[str, dict]], costs: Optional[Mapping[str, float]] = None,
                    strong: str = "") -> str:
    stats = {label: model_stats(by_suite) for label, by_suite in reports.items()}
    domains = sorted({d for s in stats.values() for d in s["domains"]})
    lines = ["## Model scorecard", "",
             "| Model | Pass rate | 95% CI | Scored | Not scored | Mean seconds |", "|---|---:|---:|---:|---:|---:|"]
    for label, s in stats.items():
        low, high = s["ci95"]
        lines.append(f"| {label} | {s['pass_rate']:.0%} | {low:.0%}–{high:.0%} | {s['scored']} | "
                     f"{s['errored'] + s['invalid']} | {s['mean_seconds']:.0f} |")
    if domains:
        lines += ["", "### By domain", "", "| Model | " + " | ".join(domains) + " |", "|---|" + "---:|" * len(domains)]
        for label, s in stats.items():
            cells = [f"{s['domains'][d]['passed']}/{s['domains'][d]['scored']}" if d in s["domains"] else "-" for d in domains]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
    analysis = routing_analysis(task_matrix(reports), list(reports), costs, strong)
    if analysis:
        n = analysis["tasks"]
        lines += ["", "### Would routing pay?", "",
                  f"{n} tasks. Always `{analysis['strong']}`: {analysis['strong_solved']}/{n} solved. "
                  f"An oracle that sends each task to the cheapest model that passes it: {analysis['oracle_solved']}/{n} solved."]
        if analysis["sent_to"]:
            lines.append("Sent to: " + ", ".join(f"{label} {count}" for label, count in analysis["sent_to"].items()) + ".")
        if analysis["has_costs"]:
            saved = analysis["strong_cost"] - analysis["oracle_cost"]
            share = saved / analysis["strong_cost"] if analysis["strong_cost"] else 0.0
            lines.append(f"Cost per run of all tasks: ${analysis['strong_cost']:.2f} always-strong vs "
                         f"${analysis['oracle_cost']:.2f} oracle ({share:.0%} less).")
        else:
            lines.append("No `--cost` given, so no spend comparison; add `--cost label=USD_per_task`.")
        if analysis["only_one_model"]:
            lines.append("Passed by exactly one model (where choosing matters): " + ", ".join(analysis["only_one_model"][:12]) + ".")
        if analysis["unsolved"]:
            lines.append("Solved by none: " + ", ".join(analysis["unsolved"][:12]) + ".")
        lines += ["", "This is an upper bound: the oracle knows which model will pass before trying, "
                      "and a real router pays for the cheap attempts that fail. If it is small here, "
                      "a learned router will not recover it."]
    return "\n".join(lines) + "\n"


# ── running ──────────────────────────────────────────────────────────────────

def plan(models: Mapping[str, str], suites: Sequence[str], repeat: int, out: Path, force: bool) -> List[dict]:
    """Every (model, suite) run to make, skipping the ones already reported unless forced."""
    jobs = []
    for label, model in models.items():
        for suite in suites:
            report = Path(out) / label / f"{Path(suite).stem}.json"
            jobs.append({"label": label, "model": model, "suite": suite, "report": report,
                         "skip": report.exists() and not force})
    return jobs


def task_ids(suite: str) -> List[str]:
    from .harness import load_suite

    return [str(task.id) for task in load_suite(Path(suite))[1]]


def task_count(suite: str) -> int:
    return len(task_ids(suite))


def unknown_tasks(suites: Sequence[str], only: Sequence[str]) -> List[str]:
    """Ids in `only` that no named suite contains. The runner would run nothing and
    report 0/0, which reads as a result; a typo has to be an error instead."""
    known = {tid for suite in suites for tid in task_ids(suite)}
    return [tid for tid in only if tid not in known]


def command_for(job: Mapping, repeat: int, out: Path, timeout: int, runtime: str, only: Sequence[str],
                local: Sequence[str] = ()) -> List[str]:
    command = [sys.executable, "-m", "aria_code.evals.runner", str(job["suite"]), "--model", job["model"],
               "--repeat", str(repeat), "--report", str(job["report"]), "--solve-timeout", str(timeout),
               "--trajectories", str(Path(out) / job["label"] / "trajectories")]
    if runtime != "python":
        command.extend(["--runtime", runtime])
    if job["label"] in local:
        command.append("--local")  # Ollama only, no cloud: for a model on this machine
    if only:
        command.extend(["--only", *only])
    return command


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="aria-scorecard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run the suites on several models (prints the plan unless --yes)")
    run.add_argument("--model", action="append", default=[], metavar="LABEL=MODEL", help="repeatable, cheapest first")
    run.add_argument("--suite", action="append", default=[], metavar="PATH", help="repeatable")
    run.add_argument("--repeat", type=int, default=3)
    run.add_argument("--out", default="scorecard")
    run.add_argument("--only", nargs="*", default=[], help="limit to these task ids (a smoke test)")
    run.add_argument("--solve-timeout", type=int, default=900)
    run.add_argument("--runtime", choices=("python", "native"), default="python")
    run.add_argument("--local", action="append", default=[], metavar="LABEL",
                     help="run this label's model on this machine (Ollama), free; repeatable")
    run.add_argument("--force", action="store_true", help="redo runs that already have a report")
    run.add_argument("--yes", action="store_true", help="actually run; this calls the models and costs money")
    rep = sub.add_parser("report", help="summarise the reports under a folder")
    rep.add_argument("root")
    rep.add_argument("--cost", action="append", default=[], metavar="LABEL=USD", help="estimated USD per task attempt")
    rep.add_argument("--strong", default="", help="the always-use baseline (default: the best pass rate)")
    args = parser.parse_args(argv)

    try:
        if args.action == "report":
            costs = {k: float(v) for k, v in parse_labelled(args.cost, "--cost").items()}
            reports = load_reports(Path(args.root))
            if not reports:
                print(f"no reports under {args.root}", file=sys.stderr)
                return 2
            print(render_markdown(reports, costs or None, args.strong))
            return 0
        models = parse_labelled(args.model, "--model")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not models or not args.suite:
        print("give at least one --model and one --suite", file=sys.stderr)
        return 2
    missing = unknown_tasks(args.suite, args.only)
    if missing:
        print(f"no such task in the suites: {', '.join(missing)}", file=sys.stderr)
        return 2
    out = Path(args.out)
    jobs = plan(models, args.suite, args.repeat, out, args.force)
    attempts = 0
    print(f"\n{len(models)} model(s) x {len(args.suite)} suite(s), {args.repeat} attempt(s) per task:")
    for job in jobs:
        tasks = len(args.only) if args.only else task_count(job["suite"])
        calls = tasks * args.repeat
        attempts += 0 if job["skip"] else calls
        print(f"  {job['label']:<12} {Path(job['suite']).stem:<12} {tasks} tasks x {args.repeat} = {calls} agent runs"
              f"{'  (already done, skipped)' if job['skip'] else ''}")
    print(f"\n{attempts} agent runs in all. Each is a full coding session on the model, so this spends real money.")
    if not args.yes:
        print("Nothing was run. Add --yes to run it (try --only <task-id> first as a smoke test).")
        return 0
    failures = 0
    for job in jobs:
        if job["skip"]:
            continue
        Path(job["report"]).parent.mkdir(parents=True, exist_ok=True)
        print(f"\n== {job['label']} · {Path(job['suite']).stem}")
        # A task the model gets wrong exits 1; only a runner that could not start is a failure here.
        done = subprocess.run(command_for(job, args.repeat, out, args.solve_timeout, args.runtime, args.only, args.local), check=False)
        if done.returncode not in (0, 1) or not Path(job["report"]).exists():
            failures += 1
            print(f"!! {job['label']} · {Path(job['suite']).stem} did not produce a report (exit {done.returncode})")
    print(f"\nnext: python3 -m aria_code.evals.scorecard report {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
