"""OpenAI image generation/editing client — the execution backend for prompts
compiled by the `minimal-editorial-poster` / `minimal-editorial-exports`
skills.

Two capabilities, matching OpenAI's two endpoints (verified against the
official openai-python SDK source, since the public docs page 403s to
scrapers):
    generate_image() -> POST /v1/images/generations (text -> new image)
    edit_image()     -> POST /v1/images/edits       (existing photo + prompt
                          -> transformed image; multipart, not JSON)

Both use gpt-image-1, which always returns base64 (no response_format /
url option for GPT image models, only for dall-e-2/3).

Key resolution mirrors aria_cli.py's _get_provider_key(): env var
OPENAI_API_KEY first, then providers.json's "llm" then "data" sections
under "openai" — so a key already set via `/apikey set openai sk-...` for
chat is reused here, no separate setup needed.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

API_BASE = "https://api.openai.com/v1"
VERTEX_API_BASE = "https://us-central1-aiplatform.googleapis.com/v1"
DEFAULT_MODEL = "gpt-image-2.6-sunburst"
SUNBURST_MODEL = "gpt-image-2.6-sunburst"
FLARE_MODEL = "gpt-image-2.6-flare"
LEGACY_MODEL = "gpt-image-1"
GOOGLE_IMAGEN_MODEL = "imagen-3.0-generate-002"
GOOGLE_IMAGEN_FAST_MODEL = "imagen-3.0-fast-generate-001"
DEPRECATED_MODELS = {"gpt-image-2", "gpt-image-2-2026-04-21"}

# Per-image USD cost by (quality, size), verified against OpenAI's published
# output-token pricing — output tokens only; input text/image tokens add a few
# thousandths of a cent and are ignored here as noise.
_COST_USD: Dict[str, Dict[str, float]] = {
    "low": {"1024x1024": 0.011, "1024x1536": 0.016, "1536x1024": 0.016},
    "medium": {"1024x1024": 0.042, "1024x1536": 0.063, "1536x1024": 0.063},
    "high": {"1024x1024": 0.167, "1024x1536": 0.25, "1536x1024": 0.25},
}

_COST_USD_BY_MODEL: Dict[str, Dict[str, Dict[str, float]]] = {
    SUNBURST_MODEL: _COST_USD,
    LEGACY_MODEL: _COST_USD,
    FLARE_MODEL: {
        "low": {"1024x1024": 0.005, "1024x1536": 0.008, "1536x1024": 0.008},
        "medium": {"1024x1024": 0.018, "1024x1536": 0.027, "1536x1024": 0.027},
        "high": {"1024x1024": 0.065, "1024x1536": 0.098, "1536x1024": 0.098},
    },
    GOOGLE_IMAGEN_MODEL: {
        "low": {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
        "medium": {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
        "high": {"1024x1024": 0.04, "1024x1536": 0.04, "1536x1024": 0.04},
    },
    GOOGLE_IMAGEN_FAST_MODEL: {
        "low": {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
        "medium": {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
        "high": {"1024x1024": 0.02, "1024x1536": 0.02, "1536x1024": 0.02},
    },
}


def resolve_image_provider_and_model(
    model: Optional[str] = None,
    provider: Optional[str] = None,
    quality: str = "high",
) -> tuple[str, str]:
    prov = (provider or os.getenv("ARIA_IMAGE_PROVIDER", "")).strip().lower()
    raw = (model or os.getenv("ARIA_OPENAI_IMAGE_MODEL", "")).strip()
    lowered = raw.lower()
    if lowered in ("imagen", "imagen-3", GOOGLE_IMAGEN_MODEL):
        return ("google", GOOGLE_IMAGEN_MODEL)
    if lowered in ("imagen-fast", GOOGLE_IMAGEN_FAST_MODEL):
        return ("google", GOOGLE_IMAGEN_FAST_MODEL)
    if prov in ("google", "vertex", "vertexai"):
        if not raw:
            return ("google", GOOGLE_IMAGEN_FAST_MODEL if quality == "low" else GOOGLE_IMAGEN_MODEL)
        raise ValueError(
            f"Unsupported Google Cloud image model {raw!r}; "
            f"use {GOOGLE_IMAGEN_MODEL} or {GOOGLE_IMAGEN_FAST_MODEL}."
        )
    if (
        not raw
        and not prov
        and not _api_key()
        and (
            os.getenv("VERTEX_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
    ):
        return ("google", GOOGLE_IMAGEN_MODEL)
    return ("openai", resolve_image_model(model))


def resolve_image_model(model: Optional[str] = None) -> str:
    raw = (model or os.getenv("ARIA_OPENAI_IMAGE_MODEL", "")).strip()
    if not raw:
        return DEFAULT_MODEL
    lowered = raw.lower()
    if lowered in ("sunburst", SUNBURST_MODEL, "gpt-image-2.6"):
        return SUNBURST_MODEL
    if lowered in ("flare", FLARE_MODEL):
        return FLARE_MODEL
    if lowered == LEGACY_MODEL:
        return LEGACY_MODEL
    if lowered in DEPRECATED_MODELS:
        raise ValueError(
            "gpt-image-2 is deprecated (retiring 2027-02-10); "
            "migrate to gpt-image-2.6-sunburst or gpt-image-2.6-flare."
        )
    raise ValueError(
        f"Unsupported OpenAI image model {raw!r}; "
        f"use {SUNBURST_MODEL}, {FLARE_MODEL}, or {LEGACY_MODEL}."
    )


def estimate_cost(
    size: str = "1024x1536",
    quality: str = "high",
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Rough cost estimate — call before generate_image()/edit_image(),
    not a live quote. "auto" quality/size can't be priced ahead of time
    (OpenAI picks the actual value), so it's reported as unknown rather
    than guessed."""
    resolved_provider, resolved_model = resolve_image_provider_and_model(
        model=model, provider=provider, quality=quality
    )
    model_table = _COST_USD_BY_MODEL.get(resolved_model, _COST_USD)
    per_image = (model_table.get(quality) or {}).get(size)
    return {
        "provider": resolved_provider,
        "model": resolved_model,
        "size": size,
        "quality": quality,
        "estimated_cost_usd": per_image,
        "note": (
            "Illustrative estimate from published per-image pricing; "
            "verify current pricing before relying on it."
            if per_image is not None
            else "'auto' size/quality is resolved at request time and can't be priced ahead of submission."
        ),
    }


def _providers_path() -> Path:
    from apps.cli.config_paths import resolve_paths
    return resolve_paths().providers_file


def _api_key() -> str:
    env = os.getenv("OPENAI_API_KEY", "").strip()
    if env:
        return env
    path = _providers_path()
    if not path.exists():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for section in ("llm", "data"):
        entry = raw.get(section, {}).get("openai", {}) or {}
        if entry.get("api_key"):
            return entry["api_key"]
    return ""


def _require_key() -> str:
    key = _api_key()
    if not key:
        raise RuntimeError(
            "No OpenAI API key found. Set one with `/apikey set openai sk-...` "
            "or export OPENAI_API_KEY."
        )
    return key


def _save_b64_image(b64_data: str, dest: Path) -> Path:
    dest.write_bytes(base64.b64decode(b64_data))
    return dest


def _normalize_image_for_upload(path: Path, *, keep_alpha: bool = False) -> tuple[str, bytes]:
    """Re-encode an arbitrary local image file into a clean single-frame PNG
    before uploading to OpenAI's edits endpoint.

    Two real problems this fixes, not just defensive coding — both
    reproduced directly against real iPhone photos:
      1. iPhones commonly save JPEGs as MPO (Multi-Picture Object — an
         embedded-stereo-frame container, not a plain single-frame JPEG)
         even with a plain .jpg extension. OpenAI's edit endpoint rejects
         these with a 400 "Invalid image file or mode" even though the
         extension and declared docs (png/webp/jpg) suggest it should work.
      2. A portrait photo is commonly stored as landscape pixels plus an
         EXIF orientation tag telling viewers to rotate on display — the
         raw bytes, uploaded as-is, are genuinely sideways. Reproduced: a
         raw PIL open() of a real orientation=6 photo, no exif_transpose,
         is visibly sideways.
    Re-saving through PIL with exif_transpose applied produces a single-
    frame PNG immune to both, regardless of the source container/orientation.
    """
    import io

    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(path))
    img = img.convert("RGBA" if keep_alpha else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"{path.stem}.png", buf.getvalue()


def _resolve_output_ext(output_format: Optional[str]) -> str:
    if not output_format:
        return ".png"
    fmt = output_format.strip().lower().lstrip(".")
    if fmt == "jpeg":
        return ".jpg"
    if fmt in ("png", "jpg", "webp", "gif", "svg"):
        return f".{fmt}"
    raise ValueError("output_format must be png, jpeg, webp, gif, or svg")


def _size_to_aspect_ratio(size: str) -> str:
    if size == "1536x1024":
        return "4:3"
    if size == "1024x1536":
        return "3:4"
    return "1:1"


def _generate_image_google(
    prompt: str,
    model: str,
    size: str,
    ext: str,
) -> Dict[str, Any]:
    import requests

    from artifacts import create_user_artifact

    project = (os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "default-project").strip()
    location = (os.getenv("VERTEX_LOCATION") or os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1").strip()
    if location == "global":
        location = "us-central1"
    token = (
        os.getenv("VERTEX_ACCESS_TOKEN")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()

    url = (
        f"{VERTEX_API_BASE.rstrip('/')}/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:predict"
    )
    headers = {"Content-Type": "application/json", "x-goog-user-project": project}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(
        url,
        headers=headers,
        json={
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": _size_to_aspect_ratio(size)},
        },
        timeout=120,
    )
    if resp.status_code != 200:
        return {"success": False, "error": f"Google Cloud Vertex AI image generation failed: {resp.status_code} {resp.text[:300]}"}
    body = resp.json()
    preds = body.get("predictions") or []
    b64 = (preds[0].get("bytesBase64Encoded") if preds else None) or ((body.get("data") or [{}])[0].get("b64_json"))
    if not b64:
        return {"success": False, "error": "Google Cloud Vertex AI response had no image data"}

    artifact = create_user_artifact("image", "generated", "poster", ext)
    _save_b64_image(b64, artifact.path)
    return {
        "success": True,
        "path": str(artifact.path),
        "provider": "google",
        "model": model,
        "format": ext.lstrip("."),
    }


def generate_image(
    prompt: str,
    *,
    size: str = "1024x1536",
    quality: str = "high",
    model: Optional[str] = None,
    provider: Optional[str] = None,
    complexity: Optional[str] = None,
    output_format: Optional[str] = None,
    confirmed: bool = False,
) -> Dict[str, Any]:
    """Generate a new image from a text prompt via Google Cloud Vertex AI Imagen 3
    (when configured) or OpenAI GPT Image 2.6 — real per-call cost, billed the
    instant this succeeds (see estimate_cost()).
    """
    if not confirmed:
        return {
            "success": False,
            "error": (
                "confirmed must be true to generate a real (paid) image. "
                "Call estimate_cost() first to see the cost, then resubmit with confirmed=true."
            ),
        }

    try:
        resolved_provider, resolved_model = resolve_image_provider_and_model(
            model=model, provider=provider, quality=quality
        )
        ext = _resolve_output_ext(output_format)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    if resolved_provider == "google":
        return _generate_image_google(prompt, resolved_model, size, ext)

    import requests

    from artifacts import create_user_artifact

    key = _require_key()
    payload: Dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
    }
    if complexity:
        payload["complexity"] = complexity
    if output_format:
        payload["output_format"] = output_format.strip().lower()

    resp = requests.post(
        f"{API_BASE}/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code != 200:
        return {"success": False, "error": f"OpenAI image generation failed: {resp.status_code} {resp.text[:300]}"}
    data = (resp.json().get("data") or [{}])[0]
    b64 = data.get("b64_json")
    if not b64:
        return {"success": False, "error": "OpenAI response had no b64_json image data"}

    artifact = create_user_artifact("image", "generated", "poster", ext)
    _save_b64_image(b64, artifact.path)
    return {"success": True, "path": str(artifact.path), "provider": "openai", "model": resolved_model, "format": ext.lstrip(".")}


def edit_image(
    image_path: str,
    prompt: str,
    *,
    size: str = "1024x1536",
    quality: str = "high",
    mask_path: Optional[str] = None,
    region_hint: Optional[str] = None,
    model: Optional[str] = None,
    complexity: Optional[str] = None,
    output_format: Optional[str] = None,
    confirmed: bool = False,
) -> Dict[str, Any]:
    """Transform an existing local photo per `prompt` (e.g. "convert to duotone,
    simplify background to negative space, add scan-noise texture...") via
    POST /v1/images/edits — the execution path for a real user-provided photo
    run through the minimal-editorial-poster skill. Real per-call cost,
    billed by OpenAI the instant this succeeds (see estimate_cost()).

    Blocking (uses requests) — same threading note as generate_image.
    """
    if not confirmed:
        return {
            "success": False,
            "error": (
                "confirmed must be true to generate a real (paid) image edit. "
                "Call estimate_cost() first to see the cost, then resubmit with confirmed=true."
            ),
        }

    try:
        resolved_model = resolve_image_model(model)
        ext = _resolve_output_ext(output_format)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    import requests

    from artifacts import create_user_artifact

    src = Path(image_path)
    if not src.exists():
        return {"success": False, "error": f"File not found: {image_path}"}

    key = _require_key()
    try:
        files = {"image": _normalize_image_for_upload(src)}
    except Exception as exc:
        return {"success": False, "error": f"Could not read image_path: {exc}"}
    if mask_path:
        mask = Path(mask_path)
        if not mask.exists():
            return {"success": False, "error": f"Mask file not found: {mask_path}"}
        try:
            files["mask"] = _normalize_image_for_upload(mask, keep_alpha=True)
        except Exception as exc:
            return {"success": False, "error": f"Could not read mask_path: {exc}"}

    form_data: Dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
    }
    if complexity:
        form_data["complexity"] = complexity
    if output_format:
        form_data["output_format"] = output_format.strip().lower()
    if region_hint:
        form_data["region_hint"] = region_hint.strip()

    resp = requests.post(
        f"{API_BASE}/images/edits",
        headers={"Authorization": f"Bearer {key}"},
        data=form_data,
        files=files,
        timeout=180,
    )
    if resp.status_code != 200:
        return {"success": False, "error": f"OpenAI image edit failed: {resp.status_code} {resp.text[:300]}"}
    data = (resp.json().get("data") or [{}])[0]
    b64 = data.get("b64_json")
    if not b64:
        return {"success": False, "error": "OpenAI response had no b64_json image data"}

    artifact = create_user_artifact("image", "edited", f"{src.stem}_edited", ext)
    _save_b64_image(b64, artifact.path)
    return {"success": True, "path": str(artifact.path), "model": resolved_model, "format": ext.lstrip(".")}
