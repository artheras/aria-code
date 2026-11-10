"""Declarative task intent. This record never grants tool authority or proves success."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class TaskBrief:
    goal: str
    boundaries: tuple[str, ...] = ()
    rationale: str = ""
    acceptance: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.goal, str) or not self.goal.strip() or len(self.goal.encode()) > 65536:
            raise ValueError("Task goal must be 1..65536 UTF-8 bytes")
        if not isinstance(self.rationale, str) or len(self.rationale.encode()) > 16384:
            raise ValueError("Task rationale must be at most 16384 UTF-8 bytes")
        for name in ("boundaries", "acceptance"):
            items = getattr(self, name)
            if not isinstance(items, (list, tuple)) or len(items) > 32:
                raise ValueError(f"{name} must contain at most 32 items")
            if any(not isinstance(item, str) or not item.strip() or len(item.encode()) > 4096 for item in items):
                raise ValueError(f"{name} entries must be 1..4096 UTF-8 bytes")
            object.__setattr__(self, name, tuple(items))

    def to_dict(self) -> dict:
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping) -> "TaskBrief":
        if not isinstance(value, Mapping) or set(value) != {"version", "goal", "boundaries", "rationale", "acceptance"}:
            raise ValueError("Invalid task brief fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("Unsupported task brief version")
        return cls(**{key: value[key] for key in ("goal", "boundaries", "rationale", "acceptance")})


def brief_digest(value: Mapping) -> str:
    """Detect accidental corruption; this is not a signature or authentication."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
