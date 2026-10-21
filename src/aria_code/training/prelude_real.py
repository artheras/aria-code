"""A real-message test for Prelude's intent routing, kept on this computer.

Hand-written test messages only show that a model learned its author's style. Real
messages are the check that matters, and they are private, so this tool never writes them
into a repository and the person who owns them does the reading and the labelling:

    python -m aria_code.training.prelude_real extract --source DIR_OR_FILE
    python -m aria_code.training.prelude_real label
    python -m aria_code.training.prelude_real score --adapter ADAPTER

``extract`` collects candidate messages from a directory of ``*.json`` interaction logs
(the ``instruction`` field) or a text file with one message per line, drops duplicates,
very short messages and anything that looks like a secret, an email address or a phone
number, and writes them to ``~/.aria/prelude/`` with owner-only permissions. ``label``
shows each message and asks for its label. ``score`` runs a model over the labelled
messages and prints counts only: accuracy and an interval, never a message.

Nothing here is ever committed; ``~/.aria/prelude`` is outside every repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from aria_code.evals.scorecard import wilson
from aria_code.training import prelude_eval as P

HOME = Path(os.environ.get("ARIA_PRELUDE_HOME") or Path.home() / ".aria" / "prelude")
CANDIDATES = HOME / "real_candidates.jsonl"
LABELLED = HOME / "real_test.jsonl"
KEYS = {"c": "coding", "a": "analysis", "r": "realtime", "g": "general", "f": "finance"}
MIN_CHARS = 6

_PRIVATE = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),            # email address
    re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)"),  # phone or long account number
    re.compile(r"\b(?:sk|pk|ghp|gho|AIza|AKIA|xox[bap])[-_A-Za-z0-9]{12,}"),  # API keys and tokens
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),               # any long opaque string
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[:=]"),
]


def looks_private(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PRIVATE)


def read_source(source: Path) -> Iterable[str]:
    if source.is_dir():
        for path in sorted(source.rglob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            text = record.get("instruction") if isinstance(record, dict) else None
            if isinstance(text, str):
                yield text
    else:
        for line in source.read_text(encoding="utf-8").splitlines():
            yield line


def candidates(texts: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for text in texts:
        text = " ".join(text.split())
        if len(text) < MIN_CHARS or text in seen or looks_private(text):
            continue
        seen.add(text)
        out.append(text)
    return out


def _write_private(path: Path, rows: List[dict]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def extract(source: Path) -> dict:
    kept = candidates(read_source(source))
    _write_private(CANDIDATES, [{"text": t} for t in kept])
    return {"candidates": len(kept), "written_to": str(CANDIDATES)}


def _load(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def label(ask=input, show=print) -> dict:
    """Walk through unlabelled candidates; resumable, and a skipped message stays out."""
    done = {r["text"] for r in _load(LABELLED)}
    rows = _load(LABELLED)
    todo = [r["text"] for r in _load(CANDIDATES) if r["text"] not in done]
    show("c=coding  a=analysis  r=realtime  g=general  f=finance  s=skip  q=quit")
    for text in todo:
        show(f"\n{text}")
        while True:
            answer = ask("> ").strip().lower()
            if answer == "q":
                _write_private(LABELLED, rows)
                return {"labelled": len(rows)}
            if answer == "s" or answer in KEYS:
                break
        if answer in KEYS:
            rows.append({"text": text, "label": KEYS[answer]})
            _write_private(LABELLED, rows)
    return {"labelled": len(rows)}


def score(adapter: Optional[Path], model: Optional[str]) -> dict:
    """Counts only. A message never leaves this function."""
    from aria_code.training import prelude_train as T
    from mlx_lm import generate, load

    labelled = _load(LABELLED)
    if not labelled:
        raise SystemExit(f"Nothing labelled yet in {LABELLED}. Run `label` first.")
    network, tokenizer = load(T.base_model(model), adapter_path=str(adapter) if adapter else None)
    right = unparsable = 0
    for row in labelled:
        prompt = tokenizer.apply_chat_template(
            [{"role": "system", "content": P.INTENT_SYSTEM}, {"role": "user", "content": row["text"]}],
            add_generation_prompt=True, tokenize=False)
        prediction = P.parse_prediction("intent", generate(network, tokenizer, prompt=prompt, max_tokens=6))
        unparsable += prediction is None
        right += prediction == row["label"]
    low, high = wilson(right, len(labelled))
    return {"adapter": str(adapter) if adapter else None, "n": len(labelled), "correct": right,
            "accuracy": round(right / len(labelled), 4), "ci95": [round(low, 4), round(high, 4)],
            "unparsable": unparsable}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--source", required=True, help="a directory of *.json interaction logs, or a text file")
    sub.add_parser("label")
    s = sub.add_parser("score")
    s.add_argument("--adapter")
    s.add_argument("--model")
    args = parser.parse_args(argv)
    if args.command == "extract":
        print(json.dumps(extract(Path(args.source).expanduser())))
    elif args.command == "label":
        print(json.dumps(label()))
    else:
        print(json.dumps(score(Path(args.adapter) if args.adapter else None, args.model), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
