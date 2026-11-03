"""Fresh Ollama model discovery; an explicit selection is never substituted."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from urllib.parse import urlsplit, urlunsplit


class OllamaModelError(ValueError):
    """The selected server/model cannot be verified for this request."""

    def __str__(self) -> str:
        # The runtime's error transport retains text, so keep the no-fallback
        # category even through adapters that do not preserve extra metadata.
        return "ollama_model_unavailable: " + super().__str__()


def ollama_base_url(config: dict | None = None) -> str:
    cfg = config or {}
    raw = str(cfg.get("ollama_base_url") or cfg.get("ollama_url") or os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urlsplit(raw)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError
        _ = u.port
    except ValueError:
        raise OllamaModelError("Ollama address must be an http(s) URL without credentials, query or fragment") from None
    path = u.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((u.scheme, u.netloc, path, "", ""))


def installed_models(base_url: str) -> list[str]:
    """No positive cache: models removed since the last turn must be rejected."""
    url = ollama_base_url({"ollama_base_url": base_url}) + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.status != 200:
                raise ValueError
            raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise ValueError
        data = json.loads(raw)
        rows = data["models"]
        if not isinstance(rows, list):
            raise ValueError
        names = []
        for row in rows:
            name = row["name"]
            if not isinstance(name, str) or not name or len(name) > 512 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in name):
                raise ValueError
            if name not in names:
                names.append(name)
        return names
    except Exception as exc:
        raise OllamaModelError("Cannot verify installed Ollama models. Check the service address, run ollama serve, then retry /model.") from exc


def require_installed_model(available: list[str], requested: str) -> str:
    if requested in available:
        return requested
    # Ollama's untagged spelling is only an alias for :latest, not any tag.
    if requested and ":" not in requested.rsplit("/", 1)[-1] and requested + ":latest" in available:
        return requested + ":latest"
    names = ", ".join(available[:10]) or "(none)"
    raise OllamaModelError(f"Ollama model {requested!r} is not installed. Installed models: {names}. Run ollama pull <model>, then select it with /model.")


def resolve_model_sync(base_url: str, requested: str) -> str:
    return require_installed_model(installed_models(base_url), requested)


async def resolve_model_async(base_url: str, requested: str) -> str:
    return await asyncio.to_thread(resolve_model_sync, base_url, requested)
