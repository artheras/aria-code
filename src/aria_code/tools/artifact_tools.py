"""``publish_artifact`` — the tool that makes the canvas an agent capability.

What was missing
----------------
Every piece of the artifact story already existed: ``artifacts.py`` records
and versions them, ``preview_server.py`` serves them with live SSE updates and
a version stepper, and ``/canvas`` opens the panel.  What did not exist was a
way for the *model* to put something on it.  The agent could only write an HTML
file and hope a human had typed ``/canvas`` and would think to look.

That gap is the whole difference between "the tool can produce files" and "the
assistant makes you a thing you can look at". The mechanic worth copying is not
the renderer — it is that publishing is a **tool call**, so the model decides a
visual answer is the right answer and the artifact simply appears, versioned,
next to the conversation.

What it deliberately does not do
--------------------------------
It does not start the preview server on its own.  ``preview_server`` is opt-in
by design — nothing in it runs until a human types ``/canvas`` — and a tool the
model can call at will is exactly the wrong thing to hang an auto-starting
local HTTP server off.  With no canvas running, publishing still records and
versions the artifact and tells the model where it went, and the human sees it
the moment they open the panel.

Safety
------
Artifact HTML is model-generated and is rendered in a browser, so the server
frames it with ``sandbox="allow-scripts"`` (an opaque origin, no
``allow-same-origin``) under a ``connect-src 'none'`` CSP. This tool adds the
half the server cannot see: it refuses to publish a file from outside the
workspace, so "publish this artifact" can never become a way to read
``~/.ssh/id_rsa`` into a browser tab.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

__all__ = [
    "ARTIFACT_TOOL_SCHEMAS",
    "ARTIFACT_TOOLS",
    "register_artifact_tools",
    "tool_publish_artifact",
]

# Suffixes the canvas can actually render. Publishing anything else would put a
# download prompt on the panel instead of a preview.
_RENDERABLE = {
    ".html", ".htm", ".md", ".markdown", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}

_MAX_BYTES = 8 * 1024 * 1024


def _workspace_root() -> Path:
    return Path(os.getcwd()).resolve()


def _within_workspace(path: Path) -> bool:
    """True when *path* is inside the workspace.

    Checked against the resolved path so a symlink out of the tree is caught
    with everything else.
    """
    try:
        path.resolve().relative_to(_workspace_root())
        return True
    except (ValueError, OSError):
        return False


def _source_bytes(params: Dict[str, Any]) -> tuple[bytes, str, str, str]:
    """Return ``(payload, suffix, stem, error)`` for whatever is being published."""
    raw_path = str(params.get("path") or "").strip()
    content = params.get("content")

    if raw_path and content is None:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = _workspace_root() / path
        if not path.is_file():
            return b"", "", "", f"No such file: {raw_path}"
        if not _within_workspace(path):
            return b"", "", "", (
                f"Refusing to publish {raw_path}: it is outside the workspace. "
                "Publishing renders a file in a browser; only workspace files may be published."
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            return b"", "", "", f"Could not read {raw_path}: {exc}"
        if len(payload) > _MAX_BYTES:
            return b"", "", "", f"{raw_path} exceeds {_MAX_BYTES // (1024 * 1024)}MB"
        return payload, path.suffix.lower(), path.stem, ""

    if content is None:
        return b"", "", "", "Provide either 'path' (an existing file) or 'content' plus 'filename'"

    filename = str(params.get("filename") or "").strip()
    if not filename:
        return b"", "", "", "Missing 'filename' — needed to know how to render the content"
    if Path(filename).name != filename:
        return b"", "", "", "'filename' must be a bare file name, not a path"

    payload = (content if isinstance(content, str) else str(content)).encode("utf-8")
    if len(payload) > _MAX_BYTES:
        return b"", "", "", f"Content exceeds {_MAX_BYTES // (1024 * 1024)}MB"
    name = Path(filename)
    return payload, name.suffix.lower(), name.stem, ""


def _free_record(topic: str, stem: str, suffix: str):
    """An artifact record whose path is not already taken.

    ``create_user_artifact`` names files with second granularity, and
    ``publish_artifact`` is a tool the model can call twice in a row — two
    publishes inside the same second would resolve to one path, so the second
    would overwrite the first and the version history would silently lose an
    entry. Walking to a free stem keeps every published version on disk.
    """
    from aria_code import artifacts

    record = artifacts.create_user_artifact("canvas", topic, stem, suffix)
    attempt = 2
    while record.path.exists() and attempt < 100:
        record = artifacts.create_user_artifact("canvas", topic, f"{stem}-{attempt}", suffix)
        attempt += 1
    return record


def tool_publish_artifact(params: Dict[str, Any]) -> Dict[str, Any]:
    """Publish a file to the canvas as a new version of a named artifact."""
    try:
        payload, suffix, stem, error = _source_bytes(params)
        if error:
            return {"success": False, "error": error}

        if suffix not in _RENDERABLE:
            return {
                "success": False,
                "error": (
                    f"{suffix or 'this file type'} cannot be rendered on the canvas. "
                    f"Renderable: {', '.join(sorted(_RENDERABLE))}"
                ),
            }

        title = str(params.get("title") or stem).strip() or stem
        # The topic groups versions into one thread, so re-publishing the same
        # report appends to its history instead of starting a new artifact
        # each time. Defaulting it to the title is what makes "update the
        # dashboard" behave the way a person expects.
        topic = str(params.get("topic") or title).strip()

        from aria_code import artifacts

        record = _free_record(topic, stem or "artifact", suffix)
        record.path.write_bytes(payload)

        # This is the live push: write_artifact_metadata notifies a running
        # canvas session, which is what puts the new version on screen.
        metadata_path = artifacts.write_artifact_metadata(record, {
            "title": title,
            "topic": topic,
            "kind": suffix.lstrip("."),
            "description": str(params.get("description") or ""),
            "source": "publish_artifact",
        })

        try:
            canvas_discovery = Path.home() / ".arthera" / "canvas.json"
            live = canvas_discovery.is_file()
        except Exception:
            live = False

        return {"success": True, "data": {
            "path": str(record.path),
            "title": title,
            "topic": topic,
            "metadata": str(metadata_path),
            "live": live,
            "hint": (
                "Published — it is already showing on the open canvas."
                if live else
                "Recorded. Tell the user to run /canvas to open the live preview panel."
            ),
        }}
    except Exception as exc:
        return {"success": False, "error": f"publish_artifact failed: {exc}"}


ARTIFACT_TOOLS = {
    "publish_artifact": (
        tool_publish_artifact,
        "Publish an HTML/Markdown/image file to the live canvas as a new version",
    ),
}

ARTIFACT_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "publish_artifact",
        "description": (
            "Publish a self-contained HTML page, Markdown document, or image to the "
            "user's live canvas panel, where it renders next to the terminal and keeps "
            "a version history. Use this when the answer is something to LOOK at — a "
            "dashboard, a report, a chart, a comparison table — rather than something "
            "to read in the terminal. Publishing the same 'topic' again adds a new "
            "version to the same artifact instead of creating another one, so iterate "
            "by re-publishing. HTML must be self-contained: inline the CSS and JS and "
            "embed images as data: URIs, because the page is rendered in a sandbox that "
            "blocks all network access except Google Fonts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to an existing file in the workspace to publish",
                },
                "content": {
                    "type": "string",
                    "description": "File contents to write and publish (use with 'filename' instead of 'path')",
                },
                "filename": {
                    "type": "string",
                    "description": "File name for 'content', e.g. revenue-dashboard.html",
                },
                "title": {"type": "string", "description": "Display title for the artifact"},
                "topic": {
                    "type": "string",
                    "description": (
                        "Version thread key. Re-publish with the same topic to add a version "
                        "to the existing artifact. Defaults to the title."
                    ),
                },
                "description": {"type": "string", "description": "One-line summary"},
            },
            "required": [],
        },
    },
]


def register_artifact_tools(tools_dict: Dict[str, Any], schemas_list: List[Dict[str, Any]]) -> int:
    """Register into LOCAL_TOOLS / LOCAL_TOOL_SCHEMAS."""
    tools_dict.update(ARTIFACT_TOOLS)
    schemas_list.extend(ARTIFACT_TOOL_SCHEMAS)
    return len(ARTIFACT_TOOLS)
