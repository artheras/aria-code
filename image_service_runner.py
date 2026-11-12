"""JSON-stdio runner for Arthera's shared image-generation bridge.

It intentionally lives next to aria-code's providers so it executes inside
aria-code's own virtual environment and reuses its model/dependency/API-key
configuration.  The caller supplies JSON on stdin and receives one JSON object
on stdout; no shell interpolation or arbitrary command execution is involved.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _failure(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return _failure("prompt is required")

    backend = str(payload.get("backend") or "local")
    if backend == "local":
        from aria_code.providers.local_image_provider import generate_image_local
        return generate_image_local(
            prompt,
            width=int(payload.get("width") or 1024),
            height=int(payload.get("height") or 1536),
        )
    if backend == "openai":
        if not bool(payload.get("confirmed")):
            return _failure("confirmed=true is required for paid OpenAI image generation")
        from aria_code.clients.openai_image_client import generate_image
        return generate_image(
            prompt,
            size=str(payload.get("size") or "1024x1536"),
            quality=str(payload.get("quality") or "high"),
            confirmed=True,
        )
    return _failure(f"unsupported image backend: {backend}")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        result = run(payload)
    except Exception as exc:
        result = _failure(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
