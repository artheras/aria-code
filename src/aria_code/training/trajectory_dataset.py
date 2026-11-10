"""Consented Work-run trajectories -> a routing dataset, built the way a quant builds a backtest.

Arthera keeps a redacted copy of a Work run only when its owner turned on "Help
improve Aria" (packages/quant_engine/services/trajectory_training_store.py, schema
``aria.agent_trajectory.v1``). A run records the request title, the plan, which tools
ran in which order and whether each succeeded, which artifacts came out, and how it
ended. It deliberately does not record tool arguments or the answer text, so what it
can teach is routing: which tools, in what order, what to do after a failure, when to
stop. That is the first small model the training plan asks for.

The rules are enforced here, in code:

- **Consent and privacy, checked again.** A record without consent, or one in which
  a secret-looking string survived the server's redaction, is dropped, not repaired.
- **Split by person, never by run.** Two runs by one user share their habits and
  wording; one in train and one in test is leakage. The split is a stable hash of the
  owner hash, so it does not move as data arrives.
- **Walk-forward holdout.** With ``--holdout-after DATE`` every run recorded on or
  after that date goes to test whoever made it: the model is judged on the future.
- **No survivorship.** Only runs that succeeded are routing examples. Failed,
  cancelled and interrupted runs go to ``negatives.jsonl`` for preference training
  later; they are never silently thrown away and never taught as the right answer.
- **Exposure is capped and reported.** One heavy user must not become the dataset:
  ``--per-owner-cap`` limits examples per owner per split, and the manifest reports
  counts per split, outcome and drop reason.
- **Provenance.** The manifest records the input's sha256 and the rules, so a tuned
  model can be traced to what it saw.

    python -m aria_code.training.trajectory_dataset export.jsonl --out routing/ \\
        [--holdout-after 2026-11-01] [--per-owner-cap 200]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = "aria.agent_trajectory.v1"
SPLITS = ("train", "validation", "test")
SHARE = {"train": 80, "validation": 10}          # percent of owners; the rest is test
DEFAULT_PER_OWNER_CAP = 200

ROUTING_SYSTEM = (
    "You plan Aria's work. Given a request, reply with the tools to call, in order, as JSON: "
    '{"tools": ["tool_name", ...]}. Use only tools that are needed.'
)

# A second, independent look for secrets. The server redacts; this refuses anything it missed.
_RESIDUAL = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+(?!\[TOKEN\])[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
]


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def residual_secret(record: dict) -> bool:
    return any(pattern.search(text) for text in _strings(record) for pattern in _RESIDUAL)


def owner_split(owner: str) -> str:
    bucket = int(hashlib.sha256(owner.encode("utf-8")).hexdigest(), 16) % 100
    if bucket < SHARE["train"]:
        return "train"
    if bucket < SHARE["train"] + SHARE["validation"]:
        return "validation"
    return "test"


def outcome_class(record: dict) -> str:
    """success, recovered (succeeded after a failed step) or failure."""
    outcome = record.get("outcome") or {}
    labels = record.get("labels") or {}
    if str(outcome.get("status")) != "succeeded" or labels.get("user_feedback") in ("negative", "thumbs_down"):
        return "failure"
    return "recovered" if int(outcome.get("failed_step_count") or 0) > 0 else "success"


def tool_steps(record: dict) -> list[dict]:
    steps = []
    for step in record.get("steps") or []:
        if not isinstance(step, dict):
            continue
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        if step.get("type") == "tool_result" and payload.get("tool"):
            steps.append({"tool": str(payload["tool"]), "ok": bool(payload.get("ok", True))})
        elif step.get("type") == "tool_call" and payload.get("tool"):
            steps.append({"tool": str(payload["tool"]), "ok": None})
    # A call followed by its own result is one step; keep the result's verdict.
    merged: list[dict] = []
    for step in steps:
        if merged and merged[-1]["tool"] == step["tool"] and merged[-1]["ok"] is None and step["ok"] is not None:
            merged[-1] = step
        else:
            merged.append(step)
    return merged


def example(record: dict, split: str) -> dict:
    steps = tool_steps(record)
    plan_event = next((s for s in record.get("steps") or [] if isinstance(s, dict) and s.get("type") == "plan"), None)
    plan = [str(step.get("title") or "") for step in ((plan_event or {}).get("payload") or {}).get("steps") or []
            if isinstance(step, dict) and step.get("title")]
    return {
        "id": record.get("trajectory_id"),
        "split": split,
        "request": str((record.get("task") or {}).get("title") or ""),
        "plan": plan,
        "tools": steps,
        "artifacts": [str((s.get("payload") or {}).get("card_type") or "") for s in record.get("steps") or []
                      if s.get("type") == "artifact"],
        "outcome": outcome_class(record),
        "recorded_at": record.get("recorded_at"),
    }


def vertex_record(item: dict) -> dict:
    """One routing example in the Vertex supervised-tuning format: request in, tool order out."""
    # The answer is the order of tools that succeeded: a failed call that was retried
    # is part of the history, not of the plan to learn.
    tools = [step["tool"] for step in item["tools"] if step["ok"] is not False]
    return {
        "systemInstruction": {"role": "system", "parts": [{"text": ROUTING_SYSTEM}]},
        "contents": [
            {"role": "user", "parts": [{"text": item["request"]}]},
            {"role": "model", "parts": [{"text": json.dumps({"tools": tools}, ensure_ascii=False)}]},
        ],
    }


def build(records: list[dict], *, holdout_after: Optional[str] = None,
          per_owner_cap: int = DEFAULT_PER_OWNER_CAP) -> dict:
    drops: Counter = Counter()
    kept: dict[str, list[dict]] = {split: [] for split in SPLITS}
    negatives: list[dict] = []
    per_owner: Counter = Counter()
    owners: dict[str, set] = defaultdict(set)
    seen: set = set()
    drops["unreadable"] += sum(1 for r in records if not isinstance(r, dict))
    for record in sorted((r for r in records if isinstance(r, dict)), key=lambda r: str(r.get("recorded_at") or "")):
        if record.get("schema") != SCHEMA:
            drops["unknown_schema"] += 1
            continue
        if not (record.get("consent") or {}).get("training_opt_in"):
            drops["no_consent"] += 1
            continue
        owner = str(record.get("owner") or "")
        if not owner:
            drops["no_owner"] += 1
            continue
        if residual_secret(record):
            drops["residual_secret"] += 1
            continue
        if not tool_steps(record):
            drops["no_tools"] += 1
            continue
        if not str((record.get("task") or {}).get("title") or "").strip():
            drops["no_request"] += 1
            continue
        split = owner_split(owner)
        if holdout_after and str(record.get("recorded_at") or "")[:10] >= holdout_after:
            split = "test"
        item = example(record, split)
        key = (owner, item["request"].strip().lower(), tuple(s["tool"] for s in item["tools"]), item["outcome"])
        if key in seen:
            drops["duplicate"] += 1
            continue
        seen.add(key)
        if item["outcome"] == "failure":
            negatives.append(item)
            continue
        if per_owner[(owner, split)] >= per_owner_cap:
            drops["per_owner_cap"] += 1
            continue
        per_owner[(owner, split)] += 1
        owners[split].add(owner)
        kept[split].append(item)
    return {"splits": kept, "negatives": negatives, "drops": drops, "owners": owners}


def write(result: dict, out: Path, *, source: Path, rules: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for split, items in result["splits"].items():
        (out / f"{split}.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
                                            encoding="utf-8")
    (out / "negatives.jsonl").write_text(
        "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in result["negatives"]), encoding="utf-8")
    # Only train and validation become tuning files; test is never handed to a tuner.
    vertex = out / "vertex"
    vertex.mkdir(exist_ok=True)
    for split in ("train", "validation"):
        (vertex / f"{split}.jsonl").write_text(
            "".join(json.dumps(vertex_record(i), ensure_ascii=False) + "\n" for i in result["splits"][split]),
            encoding="utf-8")
    manifest = {
        "schema": SCHEMA,
        "source": {"path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "rules": rules,
        "examples": {split: len(items) for split, items in result["splits"].items()},
        "outcomes": {split: dict(Counter(i["outcome"] for i in items)) for split, items in result["splits"].items()},
        "negatives": len(result["negatives"]),
        "owners": {split: len(result["owners"][split]) for split in SPLITS},
        "dropped": dict(result["drops"]),
        "tools": dict(Counter(s["tool"] for items in result["splits"].values() for i in items for s in i["tools"])),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def load(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append(None)
    return records


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="JSONL exported by trajectory_training_store.export_jsonl")
    parser.add_argument("--out", required=True)
    parser.add_argument("--holdout-after", help="YYYY-MM-DD: runs recorded on or after go to test")
    parser.add_argument("--per-owner-cap", type=int, default=DEFAULT_PER_OWNER_CAP)
    args = parser.parse_args(argv)
    if args.holdout_after and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.holdout_after):
        raise SystemExit("--holdout-after must be YYYY-MM-DD")
    source = Path(args.source)
    result = build(load(source), holdout_after=args.holdout_after, per_owner_cap=args.per_owner_cap)
    rules = {"split": "sha256(owner) % 100: train < 80 <= validation < 90 <= test",
             "holdout_after": args.holdout_after, "per_owner_cap": args.per_owner_cap,
             "examples": "succeeded runs only; failures in negatives.jsonl",
             "dropped": "no consent, no owner, residual secret, no tools, no request, duplicate"}
    print(json.dumps(write(result, Path(args.out), source=source, rules=rules), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
