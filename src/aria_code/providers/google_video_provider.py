"""google_video_provider.py — Google Cloud Vertex AI & Gemini API Veo Video provider.

Generates video clips via Google Cloud Veo 3.1:
- veo-3.1-fast-generate-001 (default: faster and cheaper per second)
- veo-3.1-generate-001 (higher quality)

Checked against arthera-prod on 2026-09-15: only the Veo 3.1 GA models are
reachable; veo-3.0-* and veo-2.0-generate-001 return 404. Veo 3.1 accepts
durations of 4, 6 or 8 seconds and aspect ratios 16:9 / 9:16, is served from a
region (not "global"), and its long-running operation is read with
:fetchPredictOperation.

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

DEFAULT_VEO_MODEL = "veo-3.1-fast-generate-001"
QUALITY_VEO_MODEL = "veo-3.1-generate-001"
VEO_DURATIONS = (4, 6, 8)


def veo_duration(seconds: Any) -> int:
    """The shortest duration Veo 3.1 accepts (4, 6 or 8 s) that covers `seconds`.

    Rounding up: the edit trims a clip to its beat, but cannot stretch one.
    """
    try:
        wanted = float(seconds)
    except (TypeError, ValueError):
        wanted = 6.0
    return next((allowed for allowed in VEO_DURATIONS if allowed >= wanted), VEO_DURATIONS[-1])


def veo_location() -> str:
    """Veo runs in a region; the chat models' "global" location has no Veo endpoint."""
    location = (os.getenv("VEO_LOCATION") or os.getenv("VERTEX_LOCATION") or "").strip()
    return "us-central1" if not location or location == "global" else location


def _video_bytes(payload: Dict[str, Any]) -> Optional[bytes]:
    """The first video's bytes from a finished operation, in any of the shapes Veo has used."""
    response = payload.get("response") or {}
    items = response.get("videos") or response.get("predictions") or response.get("generatedSamples") or []
    for item in items:
        encoded = item.get("bytesBase64Encoded") or (item.get("video") or {}).get("bytesBase64Encoded")
        if encoded:
            return base64.b64decode(encoded)
        gcs_uri = item.get("gcsUri") or (item.get("video") or {}).get("uri")
        if gcs_uri and gcs_uri.startswith("gs://"):
            try:
                from google.cloud import storage

                bucket, _, name = gcs_uri[5:].partition("/")
                return storage.Client().bucket(bucket).blob(name).download_as_bytes()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[GoogleVideo] Could not read {gcs_uri}: {exc}")
    return None


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

    if aspect_ratio not in {"16:9", "9:16"}:
        aspect_ratio = "9:16" if aspect_ratio in {"3:4", "4:5", "2:3"} else "16:9"

    duration_seconds = veo_duration(duration_seconds)

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
            suffix = Path(image_path).suffix.lower()
            mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
            instance["image"] = {"bytesBase64Encoded": base64.b64encode(img_bytes).decode("ascii"), "mimeType": mime}
        except Exception as exc:
            logger.warning(f"[GoogleVideo] Failed to read source image {image_path}: {exc}")

    body = {
        "instances": [instance],
        "parameters": {
            "aspectRatio": aspect_ratio,
            "durationSeconds": duration_seconds,
            "sampleCount": 1,
            # Narration and music are laid over by the edit; Veo's own audio
            # would only be muted, and generating it costs more.
            "generateAudio": False,
        },
    }

    import requests

    token = _get_vertex_token() if vertex_project else None
    display_model = f"Google Cloud Veo 3.1 ({model})"
    last_error = ""

    # 1. Vertex AI long-running prediction, read back with :fetchPredictOperation.
    if token and vertex_project:
        location = veo_location()
        base = f"https://{location}-aiplatform.googleapis.com/v1/projects/{vertex_project}/locations/{location}/publishers/google/models/{model}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            resp = requests.post(f"{base}:predictLongRunning", headers=headers, json=body, timeout=30)
            if resp.status_code in (200, 201, 202) and resp.json().get("name"):
                op_name = resp.json()["name"]
                start_time = time.time()
                while time.time() - start_time < max_wait_seconds:
                    time.sleep(poll_interval)
                    poll = requests.post(f"{base}:fetchPredictOperation", headers=headers, json={"operationName": op_name}, timeout=30)
                    if poll.status_code != 200:
                        continue
                    payload = poll.json()
                    if not payload.get("done"):
                        continue
                    if payload.get("error"):
                        last_error = f"Veo operation failed: {payload['error']}"
                        break
                    video = _video_bytes(payload)
                    if video:
                        from aria_code.artifacts import create_user_artifact

                        artifact = create_user_artifact("video", "veo", "clip", ".mp4")
                        artifact.path.write_bytes(video)
                        return {
                            "success": True,
                            "path": str(artifact.path),
                            "model": display_model,
                            "model_id": model,
                            "provider": "Google Cloud Vertex AI",
                            "duration": duration_seconds,
                            "aspect_ratio": aspect_ratio,
                        }
                    filtered = (payload.get("response") or {}).get("raiMediaFilteredReasons")
                    last_error = f"Veo returned no video{f' (filtered: {filtered})' if filtered else ''}."
                    break
                else:
                    last_error = f"Veo did not finish within {int(max_wait_seconds)} s."
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
