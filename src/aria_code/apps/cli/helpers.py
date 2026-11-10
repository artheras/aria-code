"""Stateless helpers lifted out of aria_cli.py.

These twelve functions never touched aria_cli's module state — no console, no
HAS_RICH, no tool registry — yet twelve command mixins reached back into a
7000-line module to call them, because that is simply where they happened to
be defined. Nothing about them needs to live there.

aria_cli re-exports them under their original names, so its own callers and the
globals-rebind for the modules that still rely on it are unaffected.
"""

from __future__ import annotations

import json
import pathlib
from typing import Optional

__all__ = [
    "detect_ollama_models",
    "detect_ollama_models_rich",
    "_load_project_context",
    "_display_value",
    "_chart_display_label",
    "_chart_period_from_ta_days",
    "_copy_text_to_clipboard",
    "_reveal_path_in_finder",
    "_open_path_or_url",
    "_extract_code_block",
    "_clean_tool_error_message",
    "_fuzzy_match",
    "_is_ashare_symbol",
    "format_sparkline",
]


# Per directory: the AGENTS.md family (the convention Codex and other agents
# share) and Aria's own ARIA.md are both read; CLAUDE.md only when neither is
# there. AGENTS.override.md stands in for AGENTS.md, as in Codex.
_AGENTS_NAMES = ("AGENTS.override.md", "AGENTS.md")
_ARIA_NAMES = ("ARIA.md", ".aria.md")
_FALLBACK_NAMES = ("CLAUDE.md",)


def _project_rule_files(directory: pathlib.Path) -> list[pathlib.Path]:
    picked = []
    for family in (_AGENTS_NAMES, _ARIA_NAMES):
        hit = next((directory / n for n in family if (directory / n).is_file()), None)
        if hit:
            picked.append(hit)
    if not picked:
        picked = [directory / n for n in _FALLBACK_NAMES if (directory / n).is_file()][:1]
    return picked


def _load_project_context() -> str:
    """Load AGENTS.md / ARIA.md / CLAUDE.md by walking up from cwd.

    Only ARIA.md and CLAUDE.md were read, so a repository set up for Codex
    and other agents through AGENTS.md gave Aria none of its rules. The walk
    now stops at the repository root (a parent of the repository is not part
    of the project), files nearer the working directory come first and get
    first claim on the size cap, and ~/.arthera/ARIA.md comes last. It came
    first, so a long global profile could crowd the project's rules out.
    """
    _MAX_BYTES = 12288
    _MAX_LEVELS = 5

    home = pathlib.Path.home()
    cwd  = pathlib.Path.cwd().resolve()

    found: list[tuple[pathlib.Path, str]] = []  # nearest first
    seen_text: set[str] = set()

    def add(path: pathlib.Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return
        key = content.strip()
        if key and key not in seen_text:      # a CLAUDE.md copied to AGENTS.md, a symlink
            seen_text.add(key)
            found.append((path, content))

    current = cwd
    for _ in range(_MAX_LEVELS):
        for path in _project_rule_files(current):
            add(path)
        if current == home or current.parent == current or (current / ".git").exists():
            break
        current = current.parent

    # Global user file — lowest priority; /memory edit global edits it.
    from aria_code.account_session import profile_path, read_config, account_id
    _global_aria = profile_path()
    # Signed-in personal facts come from the cloud snapshot/outbox, so a fact
    # deleted on Web cannot be reintroduced by an old local profile file.
    _include_personal_file = account_id(read_config()) is None
    if _include_personal_file and _global_aria.is_file() and not any(f == _global_aria for f, _ in found):
        add(_global_aria)

    if not found:
        return ""

    blocks: list[str] = []
    total = 0
    for fpath, content in found:
        rel = fpath.relative_to(home) if fpath.is_relative_to(home) else fpath
        snippet = content[:(_MAX_BYTES - total)]
        blocks.append(f"### {rel}\n{snippet}")
        total += len(snippet)
        if total >= _MAX_BYTES:
            break

    header = ("\n\n## Project Context\n"
              "Where these files disagree, the one listed first (nearest the working directory) wins.\n\n")
    return header + "\n\n".join(blocks)


def _display_value(value, digits: int = 2, suffix: str = "") -> str:
    try:
        if value in (None, "", "N/A", "-", "nan"):
            return "—"
        if isinstance(value, (int, float)):
            return f"{float(value):,.{digits}f}{suffix}"
        return str(value)
    except Exception:
        return "—"


def _chart_display_label(raw: str, resolved: str, result: dict | None = None) -> str:
    result = result or {}
    name = str(result.get("name") or result.get("display_name") or "").strip()
    resolved = str(resolved or "").strip().upper()
    raw_label = str(raw or "").strip().upper()
    if name and name.upper() != resolved:
        return f"{name} ({resolved})"
    if raw_label and raw_label != resolved and not raw_label.startswith(resolved):
        return f"{raw_label} ({resolved})"
    return resolved or raw_label or "chart"


def _chart_period_from_ta_days(days: int) -> str:
    try:
        d = int(days)
    except Exception:
        return "1y"
    if d <= 45:
        return "1mo"
    if d <= 110:
        return "3mo"
    if d <= 220:
        return "6mo"
    if d <= 430:
        return "1y"
    if d <= 800:
        return "2y"
    return "3y"


def _copy_text_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        import subprocess as _sp
        _sp.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=3)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _reveal_path_in_finder(path: str) -> tuple[bool, str]:
    try:
        import subprocess as _sp
        _sp.Popen(["open", "-R", path])
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _open_path_or_url(target: str) -> tuple[bool, str]:
    try:
        import subprocess as _sp
        _sp.Popen(["open", target])
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _extract_code_block(text: str) -> Optional[str]:
    """Extract the first code block from markdown-formatted text."""
    import re
    # Match ```python ... ``` or ``` ... ```
    pattern = r'```(?:python|py)?\s*\n(.*?)```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: try to find any code block
    pattern2 = r'```\w*\s*\n(.*?)```'
    match2 = re.search(pattern2, text, re.DOTALL)
    if match2:
        return match2.group(1).strip()
    return None


def _clean_tool_error_message(error: object) -> str:
    from ui.render.output import clean_tool_error_message as _ctm
    return _ctm(error)


def _fuzzy_match(query: str, candidates: list, max_results: int = 3) -> list:
    """Find closest matches using simple edit distance."""
    def _edit_dist(a, b):
        if len(a) > len(b):
            a, b = b, a
        dists = range(len(a) + 1)
        for j, cb in enumerate(b):
            new_dists = [j + 1]
            for i, ca in enumerate(a):
                cost = 0 if ca == cb else 1
                new_dists.append(min(new_dists[-1] + 1, dists[i + 1] + 1, dists[i] + cost))
            dists = new_dists
        return dists[-1]

    scored = [(c, _edit_dist(query.lower(), c.lower())) for c in candidates]
    scored.sort(key=lambda x: x[1])
    # Only suggest if edit distance is reasonable (< half the length)
    threshold = max(3, len(query) // 2)
    return [c for c, d in scored[:max_results] if d <= threshold]


def _is_ashare_symbol(symbol: str) -> bool:
    """Quick check whether a symbol looks like a Chinese A-share code."""
    s = symbol.strip().lower()
    return (
        s.startswith("sh") or s.startswith("sz")
        or (len(s) == 6 and s.isdigit())
        or s.endswith(".ss") or s.endswith(".sz")
    )


def format_sparkline(prices: list, width: int = 30) -> str:
    """Generate Unicode sparkline from price data."""
    if not prices or len(prices) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    mn, mx = min(prices), max(prices)
    rng = mx - mn or 1
    result = ""
    step = max(1, len(prices) // width)
    for i in range(0, len(prices), step):
        idx = int((prices[i] - mn) / rng * (len(blocks) - 1))
        result += blocks[idx]
    return result[:width]


def detect_ollama_models(ollama_url: str = "http://localhost:11434") -> list:
    """Query Ollama /api/tags and return list of available model names.

    Always bypasses HTTP_PROXY so localhost is reached directly even when a
    system proxy (VPN / clash / surge) is active.
    """
    import urllib.request
    # Force direct connection — bypass any HTTP_PROXY / HTTPS_PROXY env vars
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _opener.open(f"{ollama_url}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        # Also try 127.0.0.1 if hostname is "localhost" (IPv6 resolution fallback)
        if "localhost" in ollama_url:
            try:
                fallback = ollama_url.replace("localhost", "127.0.0.1")
                with _opener.open(f"{fallback}/api/tags", timeout=5) as r:
                    data = json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
            except Exception:
                pass
        return []


def detect_ollama_models_rich(ollama_url: str = "http://localhost:11434") -> tuple:
    """Return (models_list, error_str) where each entry in models_list is a dict:
        {"name": str, "size_label": str, "family": str, "quant": str,
         "execution": "local" | "remote", "remote_host": str,
         "context_window": int, "capabilities": list[str]}
    error_str is None on success, or a short human-readable reason on failure.
    """
    import urllib.request
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _try(url: str):
        with _opener.open(f"{url}/api/tags", timeout=5) as r:
            return json.loads(r.read())

    data = None
    last_err = None
    for u in [ollama_url] + ([ollama_url.replace("localhost", "127.0.0.1")]
                              if "localhost" in ollama_url else []):
        try:
            data = _try(u)
            break
        except OSError as e:
            last_err = str(e)
        except Exception as e:
            last_err = str(e)

    if data is None:
        return [], last_err or "connection failed"

    results = []
    for m in data.get("models", []):
        det  = m.get("details", {})
        size = det.get("parameter_size", "")
        fam  = det.get("family", "")
        qnt  = det.get("quantization_level", "")
        results.append({
            "name":       m["name"],
            "size_label": size,    # e.g. "1.5B", "7B", "671.0B"
            "family":     fam,     # e.g. "qwen2", "deepseek2"
            "quant":      qnt,     # e.g. "Q4_K_M", "MXFP4"
            "execution":  "remote" if m.get("remote_host") else "local",
            "remote_model": m.get("remote_model", ""),
            "remote_host": m.get("remote_host", ""),
            "context_window": int(det.get("context_length") or 0),
            "capabilities": list(m.get("capabilities") or []),
        })
    return results, None
