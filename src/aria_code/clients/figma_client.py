"""figma_client.py — read-only Figma REST API client.

Figma has no public API for creating/editing designs from a server-side
script — only the in-app Plugin API can do that, which requires a human
running it inside Figma itself. So this module only reads: file structure
(page/frame names, not the full multi-MB node tree) and comments. Nothing
here writes to Figma.

Auth: a Personal Access Token, stored the same way as every other data-service
key in this codebase — /apikey set figma <token> (see aria_cli.py's
_DATA_KEY_MAP), read back via _get_provider_key("figma")/env FIGMA_API_KEY.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

API_BASE = "https://api.figma.com/v1"


def _token() -> str:
    import os

    token = os.getenv("FIGMA_API_KEY", "")
    if token:
        return token
    try:
        from aria_code.apps.cli.config_paths import resolve_paths
        import json

        path = resolve_paths().providers_file
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = raw.get("data", {}).get("figma", {})
            if entry.get("api_key"):
                return entry["api_key"]
    except Exception:
        pass
    return ""


def _headers() -> Dict[str, str]:
    token = _token()
    if not token:
        raise RuntimeError("Figma not configured — run /apikey set figma <personal_access_token> first.")
    return {"X-Figma-Token": token}


def _summarize_node(node: Dict[str, Any], depth: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"name": node.get("name", ""), "type": node.get("type", "")}
    children = node.get("children") or []
    if children and depth > 0:
        summary["children"] = [_summarize_node(c, depth - 1) for c in children]
    elif children:
        summary["child_count"] = len(children)
    return summary


def get_file_summary(file_key: str, *, depth: int = 2) -> Dict[str, Any]:
    """Return {name, last_modified, pages: [{name, top_level_nodes: [...]}]} for a Figma file.

    Depth-limited on purpose: a raw Figma file's `document` tree can be
    megabytes of deeply nested nodes — this returns page names and the
    first `depth` levels of each page's structure, not the full document.
    """
    import requests

    resp = requests.get(f"{API_BASE}/files/{file_key}", headers=_headers(), timeout=20)
    if resp.status_code != 200:
        return {"success": False, "error": f"Figma API error {resp.status_code}: {resp.text[:300]}"}
    data = resp.json()
    document = data.get("document", {})
    pages = [_summarize_node(page, depth) for page in document.get("children", [])]
    return {
        "success": True,
        "name": data.get("name", ""),
        "last_modified": data.get("lastModified", ""),
        "pages": pages,
    }


def list_comments(file_key: str) -> Dict[str, Any]:
    import requests

    resp = requests.get(f"{API_BASE}/files/{file_key}/comments", headers=_headers(), timeout=20)
    if resp.status_code != 200:
        return {"success": False, "error": f"Figma API error {resp.status_code}: {resp.text[:300]}"}
    comments = resp.json().get("comments", [])
    return {
        "success": True,
        "comments": [
            {
                "message": c.get("message", ""),
                "user": (c.get("user") or {}).get("handle", ""),
                "created_at": c.get("created_at", ""),
                "resolved_at": c.get("resolved_at"),
            }
            for c in comments
        ],
    }


def parse_figma_url(url_or_key: str) -> Dict[str, Optional[str]]:
    """Extract file_key and optional node_id from a Figma URL or raw file key.

    Supported formats:
      - https://www.figma.com/design/<file_key>/<title>?node-id=1-23
      - https://www.figma.com/file/<file_key>/<title>?node-id=1:23
      - <file_key>
    """
    import re
    from urllib.parse import parse_qs, urlparse

    raw = (url_or_key or "").strip()
    if not raw.startswith("http://") and not raw.startswith("https://"):
        return {"file_key": raw, "node_id": None}

    parsed = urlparse(raw)
    path_parts = [p for p in parsed.path.split("/") if p]

    file_key = ""
    for i, part in enumerate(path_parts):
        if part in ("file", "design") and i + 1 < len(path_parts):
            file_key = path_parts[i + 1]
            break

    if not file_key and path_parts:
        file_key = path_parts[0]

    qs = parse_qs(parsed.query)
    raw_node_id = qs.get("node-id", [None])[0]
    node_id = raw_node_id.replace("-", ":") if raw_node_id else None

    return {"file_key": file_key, "node_id": node_id}


def get_file_nodes(file_key: str, node_ids: List[str], *, depth: Optional[int] = None) -> Dict[str, Any]:
    """Fetch exact node trees and styles for specific node IDs in a Figma file."""
    import requests

    if not node_ids:
        return {"success": False, "error": "node_ids is required"}

    # Figma API expects colon-separated IDs like '1:23'
    normalized_ids = [nid.replace("-", ":") for nid in node_ids]
    params: Dict[str, Any] = {"ids": ",".join(normalized_ids)}
    if depth is not None:
        params["depth"] = int(depth)

    resp = requests.get(f"{API_BASE}/files/{file_key}/nodes", headers=_headers(), params=params, timeout=25)
    if resp.status_code != 200:
        return {"success": False, "error": f"Figma API error {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()
    return {
        "success": True,
        "name": data.get("name", ""),
        "last_modified": data.get("lastModified", ""),
        "nodes": data.get("nodes", {}),
    }


def export_images(
    file_key: str,
    node_ids: List[str],
    *,
    format: str = "svg",
    scale: float = 1.0,
) -> Dict[str, Any]:
    """Render and export Figma nodes (components, frames, vectors) as image URLs (SVG, PNG, JPG, PDF)."""
    import requests

    if not node_ids:
        return {"success": False, "error": "node_ids is required"}

    fmt = (format or "svg").lower()
    if fmt not in ("svg", "png", "jpg", "pdf"):
        return {"success": False, "error": f"Unsupported format: {fmt}. Use svg, png, jpg, or pdf."}

    normalized_ids = [nid.replace("-", ":") for nid in node_ids]
    params: Dict[str, Any] = {
        "ids": ",".join(normalized_ids),
        "format": fmt,
        "scale": scale,
    }

    resp = requests.get(f"{API_BASE}/images/{file_key}", headers=_headers(), params=params, timeout=30)
    if resp.status_code != 200:
        return {"success": False, "error": f"Figma API error {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()
    return {
        "success": True,
        "format": fmt,
        "images": data.get("images", {}),
    }


def get_local_variables(file_key: str) -> Dict[str, Any]:
    """Fetch Figma local variables (design tokens for colors, numbers, typography, spacing)."""
    import requests

    resp = requests.get(f"{API_BASE}/files/{file_key}/variables/local", headers=_headers(), timeout=25)
    if resp.status_code != 200:
        return {"success": False, "error": f"Figma API error {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()
    meta = data.get("meta", {})
    return {
        "success": True,
        "variables": meta.get("variables", {}),
        "variable_collections": meta.get("variableCollections", {}),
    }
