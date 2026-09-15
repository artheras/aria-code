"""google_imagen_provider.py — Google Cloud image generation (module name kept for callers).

Images come from the GA Gemini image models on Vertex AI:
- gemini-3.1-flash-image (default)
- gemini-3.1-flash-lite-image (faster; used when the first is rate limited)
- gemini-3-pro-image (on request)

Imagen 3/4 are not reachable from arthera-prod, and Google discontinues
gemini-2.5-flash-image and virtual-try-on-001; neither is ever called.
Authentication: Application Default Credentials + VERTEX_PROJECT.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Imagen 3/4 are not reachable from arthera-prod (404, checked 2026-09-15) and
# Google discontinues gemini-2.5-flash-image; images come from the GA Gemini
# image models, trying the lighter one when the first is rate limited.
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
FAST_IMAGE_MODEL = "gemini-3.1-flash-lite-image"
RETIRED_IMAGE_MODELS = frozenset({"gemini-2.5-flash-image", "virtual-try-on-001"})
IMAGE_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
# Old names callers may still pass; kept so imports do not break.
DEFAULT_IMAGEN_MODEL = DEFAULT_IMAGE_MODEL
FAST_IMAGEN_MODEL = FAST_IMAGE_MODEL


def has_google_imagen_config() -> bool:
    """Return True if Google Cloud Vertex AI or Gemini API is configured."""
    vertex_project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    api_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    return bool(vertex_project or api_key)


def _get_vertex_token() -> Optional[str]:
    """Mint access token from Google Cloud ADC."""
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(GoogleAuthRequest())
        return creds.token
    except Exception as exc:
        logger.debug(f"[GoogleImagen] ADC token unavailable: {exc}")
        return None


def generate_image_google(
    prompt: str,
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = "1:1",
    sample_count: int = 1,
) -> Dict[str, Any]:
    """Generate one image with a Gemini image model on Vertex AI and save it as a user artifact."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "Prompt cannot be empty"}
    if aspect_ratio not in IMAGE_RATIOS:
        aspect_ratio = "1:1"

    vertex_project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if not vertex_project:
        return {"success": False, "error": "Google Cloud is not configured (set VERTEX_PROJECT and Application Default Credentials)."}
    token = _get_vertex_token()
    if not token:
        return {"success": False, "error": "Google Cloud credentials could not be loaded (Application Default Credentials)."}

    requested = (model or "").strip()
    if requested in RETIRED_IMAGE_MODELS or not requested.startswith("gemini-") or "image" not in requested:
        requested = FAST_IMAGE_MODEL if "fast" in requested.lower() or "lite" in requested.lower() else DEFAULT_IMAGE_MODEL
    chain = list(dict.fromkeys([requested, DEFAULT_IMAGE_MODEL, FAST_IMAGE_MODEL]))

    import requests

    last_error = ""
    for target_model in chain:
        endpoint = (
            f"https://aiplatform.googleapis.com/v1/projects/{vertex_project}/locations/global"
            f"/publishers/google/models/{target_model}:generateContent"
        )
        try:
            resp = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": aspect_ratio}},
                },
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue
        if resp.status_code != 200:
            last_error = f"{target_model} HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code == 429 or resp.status_code == 404 or resp.status_code >= 500:
                continue
            break
        candidates = resp.json().get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
        inline = next((part.get("inlineData") for part in parts if part.get("inlineData", {}).get("data")), None)
        if not inline:
            text = " ".join(str(part.get("text") or "") for part in parts).strip()
            last_error = f"{target_model} returned no image{': ' + text[:160] if text else ''}"
            break
        mime = inline.get("mimeType") or "image/png"
        from aria_code.artifacts import create_user_artifact

        artifact = create_user_artifact("image", "generated", "gemini_image", ".jpg" if "jpeg" in mime else ".png")
        artifact.path.write_bytes(base64.b64decode(inline["data"]))
        return {
            "success": True,
            "path": str(artifact.path),
            "model": target_model.replace("gemini-", "Gemini ").replace("-flash-lite-image", " Flash-Lite Image").replace("-flash-image", " Flash Image").replace("-pro-image", " Pro Image"),
            "model_id": target_model,
            "provider": "Google Cloud Vertex AI",
            "aspect_ratio": aspect_ratio,
            "prompt": prompt,
        }
    return {"success": False, "error": f"Google Cloud image generation failed: {last_error or 'no response'}"}
