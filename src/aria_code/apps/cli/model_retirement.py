"""Models Google has retired, and what Aria moves their users to.

Google's Vertex AI lifecycle page retires gemini-2.5-pro, -flash and
-flash-lite on 20 October 2026 (the release notes give 16 October, so plan
for the earlier date); the Gemini API already admits only users who have
used 2.5 before. gemini-2.0-flash and -flash-lite shut down on 1 June 2026,
and every 1.5 model in 2025. A saved config that still names one of them
would fail on every request with an error that does not say why.

So a retired id is replaced when the config is loaded, and the replacement
is written back so it happens once: a stable model, not a short-term one,
so an install that is rarely updated does not break again within months.
The replacements follow Google's recommendations; 2.5 Flash moves to 3.8
Flash rather than a Lite model so the user loses nothing in capability.
"""

from __future__ import annotations

import re

# bare id -> bare replacement
RETIRED: dict[str, str] = {
    "gemini-2.5-pro": "gemini-3.8-flash",
    "gemini-2.5-flash": "gemini-3.8-flash",
    "gemini-2.5-flash-lite": "gemini-3.1-flash-lite",
    "gemini-2.0-flash": "gemini-3.8-flash",
    "gemini-2.0-flash-lite": "gemini-3.1-flash-lite",
    "gemini-1.5-pro": "gemini-3.8-flash",
    "gemini-1.5-flash": "gemini-3.8-flash",
    "gemini-1.0-pro": "gemini-3.8-flash",
    # The previous default. Google deprecated it on 2026-10-08 and routes it to 3.6 Flash.
    "gemini-3.5-flash": "gemini-3.8-flash",
}

# Dated, versioned and preview spellings of the same models:
# gemini-2.5-pro-preview-06-05, gemini-2.0-flash-001, gemini-1.5-pro-latest.
_SUFFIX = re.compile(r"(-preview(-[\w-]+)?|-exp(-[\w-]+)?|-latest|-\d{3}|-\d{2}-\d{2}|-\d{2}-\d{4})+$")


def retired_replacement(model: str) -> str | None:
    """The id to use instead of *model*, keeping its provider prefix; None if it is not retired."""
    if not model:
        return None
    text = str(model).strip()
    prefix, _, bare = text.rpartition("/")
    key = _SUFFIX.sub("", bare.lower())
    replacement = RETIRED.get(key)
    if replacement is None:
        return None
    return f"{prefix}/{replacement}" if prefix else replacement


def migrate_config(config: dict, keys: tuple[str, ...] = ("model",)) -> list[tuple[str, str, str]]:
    """Replace retired model ids in *config* in place; return (key, old, new) for each."""
    changes = []
    for key in keys:
        old = config.get(key)
        new = retired_replacement(old) if isinstance(old, str) else None
        if new:
            config[key] = new
            changes.append((key, old, new))
    return changes


__all__ = ["RETIRED", "migrate_config", "retired_replacement"]
