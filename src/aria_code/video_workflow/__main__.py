"""Command-line entry for desktop clients: one JSON request in, JSON lines out.

    python -m aria_code.video_workflow plan   '{"script": ..., "asset_paths": [...], "project_dir": ...}'
    python -m aria_code.video_workflow render '{"project_dir": ..., "confirm_generation": false}'

stdout carries one JSON object per line: {"type": "progress", "stage", "message"}
while working, then exactly one {"type": "result", ...} or {"type": "error", "error"}.
Nothing else is written to stdout, so a client can parse every line.
"""
from __future__ import annotations

import json
import sys


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    from aria_code.video_workflow import VideoWorkflowError, plan_video, render_video
    from aria_code.video_workflow.google_models import GoogleModelError

    if len(argv) != 2 or argv[0] not in {"plan", "render"}:
        _emit({"type": "error", "error": "usage: python -m aria_code.video_workflow plan|render '<json>'"})
        return 2
    try:
        request = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        _emit({"type": "error", "error": f"request is not JSON: {exc}"})
        return 2
    progress = lambda stage, message: _emit({"type": "progress", "stage": stage, "message": message})  # noqa: E731
    try:
        if argv[0] == "plan":
            result = plan_video(
                str(request.get("script") or ""),
                [str(p) for p in request.get("asset_paths") or []],
                str(request.get("project_dir") or ""),
                platform=str(request.get("platform") or "douyin"),
                aspect_ratio=request.get("aspect_ratio"),
                language=str(request.get("language") or "zh"),
                target_seconds=int(request.get("target_seconds") or 30),
                allow_generation=bool(request.get("allow_generation")),
                progress=progress,
            )
        else:
            result = render_video(
                str(request.get("project_dir") or ""),
                confirm_generation=request.get("confirm_generation") is True,
                progress=progress,
            )
    except (VideoWorkflowError, GoogleModelError, OSError, ValueError) as exc:
        _emit({"type": "error", "error": str(exc)})
        return 1
    _emit({"type": "result", **result})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
