"""What the experimental router would have said, without keeping what anyone typed.

In ``ARIA_PRELUDE_MLX=shadow`` mode the fine-tuned Prelude and the current router both
classify each message, the current answer is used, and one line is appended here: whether
they agreed, the two labels, the message length and an eight-character hash (so repeats can
be counted). The message itself is never written. The file is owner-only and lives outside
every repository.

    python -m aria_code.apps.cli.intent_shadow
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional


def path() -> Path:
    home = os.environ.get("ARIA_PRELUDE_HOME") or str(Path.home() / ".aria" / "prelude")
    return Path(home) / "shadow.jsonl"


def record(message: str, candidate: Optional[str], baseline: str) -> None:
    """Append one comparison; never raises, because routing must not depend on logging."""
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        line = {
            "at": int(time.time()), "model": candidate, "default": baseline,
            "agree": candidate == baseline if candidate else None,
            "chars": len(message), "sha8": hashlib.sha256(message.encode("utf-8")).hexdigest()[:8],
        }
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        os.chmod(target, 0o600)
    except OSError:
        pass


def summary(lines: List[dict]) -> dict:
    answered = [r for r in lines if r.get("model")]
    agree = sum(1 for r in answered if r["agree"])
    pairs = Counter(f'{r["default"]} -> {r["model"]}' for r in answered if not r["agree"])
    return {
        "messages": len(lines),
        "model_answered": len(answered),
        "model_silent": len(lines) - len(answered),
        "agree": agree,
        "agreement": round(agree / len(answered), 4) if answered else None,
        "most_common_disagreements": dict(pairs.most_common(8)),
    }


def main() -> int:
    target = path()
    if not target.is_file():
        print(json.dumps({"messages": 0}))
        return 0
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(summary(lines), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
