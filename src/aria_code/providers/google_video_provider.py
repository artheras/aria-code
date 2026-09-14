"""google_video_provider.py — Google Cloud Vertex AI & Gemini API Veo Video provider.

Generates high-fidelity video clips via Google Cloud Veo models:
- veo-2.0-generate-001 (Flagship cinematic quality, text-to-video & image-to-video)
- veo-001-preview (Preview generation)

Dual authentication:
1. Google Cloud Vertex AI: Uses ADC (Application Default Credentials) + VERTEX_PROJECT / GOOGLE_CLOUD_PROJECT
2. Google AI Studio / Gemini API: Uses GEMINI_API_KEY or GOOGLE_API_KEY
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_VEO_MODEL = "veo-2.0-generate-001"
FALLBACK_VEO_MODEL = "veo-001-preview"


def has_google_video_config() -> bool:
    """Return True if Google Cloud Vertex AI or Gemini API is configured for Veo."""
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
        logger.debug(f"[GoogleVideo] ADC token unavailable: {exc}")
        return None


def generate_video_google(
    prompt: str,
    *,
    image_path: Optional[str] = None,
    model: str = DEFAULT_VEO_MODEL,
    aspect_ratio: str = "16:9",
    duration_seconds: int = 5,
    poll_interval: float = 3.0,
    max_wait_seconds: float = 180.0,
) -> Dict[str, Any]:
    """Generate video via Google Cloud Veo (text-to-video or image-to-video).

    Returns {"success": True, "path": str, "model": str, "duration": int, ...} on success.
    """
    prompt = (prompt or "").strip()
    if not prompt and not image_path:
        return {"success": False, "error": "Prompt or image_path is required for video generation"}

    if aspect_ratio not in {"16:9", "9:16", "1:1"}:
        aspect_ratio = "16:9"

    duration_seconds = max(3, min(10, int(duration_seconds or 5)))

    vertex_project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    api_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")

    if not vertex_project and not api_key:
        return {
            "success": False,
            "error": "Google Cloud credentials not found (set VERTEX_PROJECT with ADC, or GEMINI_API_KEY/GOOGLE_API_KEY).",
        }

    instance: Dict[str, Any] = {"prompt": prompt}
    if image_path and Path(image_path).exists():
        try:
            img_bytes = Path(image_path).read_bytes()
            instance["image"] = {"bytesBase64Encoded": base64.b64encode(img_bytes).decode("ascii")}
        except Exception as exc:
            logger.warning(f"[GoogleVideo] Failed to read source image {image_path}: {exc}")

    body = {
        "instances": [instance],
        "parameters": {
            "aspectRatio": aspect_ratio,
            "durationSeconds": duration_seconds,
            "sampleCount": 1,
        },
    }

    import requests

    token = _get_vertex_token() if vertex_project else None
    display_model = f"Google Cloud Veo 2.0 ({model})"
    last_error = ""

    # 1. Try Vertex AI Long Running Prediction
    if token and vertex_project:
        location = os.getenv("VERTEX_LOCATION", "us-central1")
        endpoint = (
            f"https://{location}-aiplatform.googleapis.com/v1/"
            f"projects/{vertex_project}/locations/{location}/publishers/google/models/{model}:predictLongRunning"
        )
        try:
            resp = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            if resp.status_code in (200, 201, 202):
                op_data = resp.json()
                op_name = op_data.get("name")
                if op_name:
                    poll_url = f"https://{location}-aiplatform.googleapis.com/v1/{op_name}"
                    start_time = time.time()
                    while time.time() - start_time < max_wait_seconds:
                        time.sleep(poll_interval)
                        poll_resp = requests.get(
                            poll_url,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=20,
                        )
                        if poll_resp.status_code == 200:
                            p_json = poll_resp.json()
                            if p_json.get("done"):
                                if "error" in p_json:
                                    last_error = f"Veo operation failed: {p_json['error']}"
                                    break
                                response_payload = p_json.get("response", {})
                                predictions = response_payload.get("predictions") or response_payload.get("generatedSamples") or []
                                if predictions:
                                    item = predictions[0]
                                    video_b64 = item.get("bytesBase64Encoded") or (item.get("video", {})).get("bytesBase64Encoded")
                                    if video_b64:
                                        from aria_code.artifacts import create_user_artifact
                                        artifact = create_user_artifact("video", "veo", "clip", ".mp4")
                                        artifact.path.write_bytes(base64.b64decode(video_b64))
                                        return {
                                            "success": True,
                                            "path": str(artifact.path),
                                            "model": display_model,
                                            "provider": "Google Cloud Vertex AI",
                                            "duration": duration_seconds,
                                            "aspect_ratio": aspect_ratio,
                                        }
                                last_error = "Operation completed but no video payload found."
                                break
            else:
                last_error = f"Vertex AI HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception as exc:
            last_error = str(exc)

    # 2. Try Gemini API if API key configured
    if api_key:
        gemini_endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning"
        try:
            resp = requests.post(
                gemini_endpoint,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            if resp.status_code in (200, 201, 202):
                op_data = resp.json()
                op_name = op_data.get("name")
                if op_name:
                    poll_url = f"https://generativelanguage.googleapis.com/v1beta/{op_name}"
                    start_time = time.time()
                    while time.time() - start_time < max_wait_seconds:
                        time.sleep(poll_interval)
                        poll_resp = requests.get(
                            poll_url,
                            headers={"x-goog-api-key": api_key},
                            timeout=20,
                        )
                        if poll_resp.status_code == 200:
                            p_json = poll_resp.json()
                            if p_json.get("done"):
                                if "error" in p_json:
                                    last_error = f"Gemini Veo error: {p_json['error']}"
                                    break
                                response_payload = p_json.get("response", {})
                                predictions = response_payload.get("predictions") or response_payload.get("generatedSamples") or []
                                if predictions:
                                    item = predictions[0]
                                    video_b64 = item.get("bytesBase64Encoded") or (item.get("video", {})).get("bytesBase64Encoded")
                                    if video_b64:
                                        from aria_code.artifacts import create_user_artifact
                                        artifact = create_user_artifact("video", "veo", "clip", ".mp4")
                                        artifact.path.write_bytes(base64.b64decode(video_b64))
                                        return {
                                            "success": True,
                                            "path": str(artifact.path),
                                            "model": f"Gemini API {model}",
                                            "provider": "Google Gemini API",
                                            "duration": duration_seconds,
                                            "aspect_ratio": aspect_ratio,
                                        }
                                break
            else:
                last_error = f"Gemini API HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception as exc:
            last_error = str(exc)

    return {
        "success": False,
        "error": f"Google Veo video generation unavailable: {last_error or 'No response from API'}",
    }
