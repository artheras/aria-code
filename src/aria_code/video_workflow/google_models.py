"""Google Cloud (Vertex AI) models used by the video workflow.

Every model call in the workflow goes through here, so the pipeline has one
place that knows endpoints, credentials and response shapes:

- Gemini (native :generateContent) — reads images and scripts, writes the
  edit plan with a JSON response schema.
- Gemini TTS (gemini-2.5-flash-tts) — narration. Returns bare 24 kHz / 16-bit
  mono PCM, wrapped here in a WAV header. The same request shape is already in
  production in Arthera's voice routes.
- Veo — generated shots. Paid per second, so it is only reachable through
  `generate_shot(..., confirmed=True)`; see pipeline.render_video.

Credentials: Application Default Credentials plus VERTEX_PROJECT (or
GOOGLE_CLOUD_PROJECT). No API keys are read here.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PLANNER_MODEL = "gemini-3.8-flash"
DEFAULT_TTS_MODEL = "gemini-2.5-flash-tts"
DEFAULT_TTS_VOICE = "Aoede"
# Listed in the Vertex publisher catalog (checked 2026-09-14). Generation itself
# has not been exercised from this workflow yet: it costs money per second.
# veo-3.0-* is not reachable from arthera-prod (404, checked 2026-09-15); Veo 3.1 is.
DEFAULT_VEO_MODEL = "veo-3.1-fast-generate-001"

MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_SUFFIX_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class GoogleModelError(RuntimeError):
    """A Google Cloud model call failed; the message says which and why."""


def _adc_config_project() -> str:
    """The project named on disk by `gcloud auth application-default login`.

    `google.auth.default()` finds the project for a user credential by running
    `gcloud`, so it returns nothing whenever gcloud is not on PATH — which is
    every run launched from a desktop app rather than a shell. Both files it
    would consult are plain text, so they are read directly instead.
    """
    config = Path(os.getenv("CLOUDSDK_CONFIG") or Path.home() / ".config" / "gcloud")
    try:
        adc = json.loads((config / "application_default_credentials.json").read_text())
        if adc.get("quota_project_id"):
            return str(adc["quota_project_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        for line in (config / "configurations" / "config_default").read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "project" and value.strip():
                return value.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def vertex_project() -> str:
    """The Google Cloud project: VERTEX_PROJECT, GOOGLE_CLOUD_PROJECT, then what ADC names.

    A desktop app launches this workflow without the shell's environment, so
    the project configured with `gcloud` is the useful last resort.
    """
    configured = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if configured:
        return configured
    try:
        import google.auth

        _, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if project:
            return str(project)
    except Exception:  # noqa: BLE001
        pass
    return _adc_config_project()


def _access_token() -> str:
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(GoogleAuthRequest())
    except Exception as exc:  # noqa: BLE001
        raise GoogleModelError(f"Google Cloud credentials unavailable (run `gcloud auth application-default login`): {exc}") from exc
    if not creds.token:
        raise GoogleModelError("Google Cloud credentials returned no access token")
    return creds.token


# Vertex's shared endpoints shed load: 429 and 503 mean "busy now", not
# "wrong request", and a plan that took a minute to set up should not be
# thrown away because of one of them.
_BUSY_STATUSES = frozenset({429, 503})
_BUSY_ATTEMPTS = 4
_BUSY_BACKOFF = (2.0, 6.0, 15.0)


def _post(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    import requests

    project = vertex_project()
    if not project:
        raise GoogleModelError(
            "No Google Cloud project: run `gcloud auth application-default login` "
            "or set VERTEX_PROJECT"
        )
    model = url.rsplit("/", 1)[-1]
    for attempt in range(_BUSY_ATTEMPTS):
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json", "x-goog-user-project": project},
            json=body,
            timeout=timeout,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in _BUSY_STATUSES and attempt < _BUSY_ATTEMPTS - 1:
            time.sleep(_BUSY_BACKOFF[attempt])
            continue
        if response.status_code in _BUSY_STATUSES:
            raise GoogleModelError(f"{model} is busy right now (HTTP {response.status_code}); try again in a minute")
        raise GoogleModelError(f"HTTP {response.status_code} from {model}: {response.text[:300]}")
    raise GoogleModelError(f"{model} is busy right now; try again in a minute")


def _generate_content_url(model: str) -> str:
    return (
        f"https://aiplatform.googleapis.com/v1/projects/{vertex_project()}"
        f"/locations/global/publishers/google/models/{model}:generateContent"
    )


def image_part(path: str) -> Dict[str, Any]:
    """An inline image part. Large images are downscaled with ffmpeg first."""
    source = Path(path)
    mime = IMAGE_SUFFIX_MIME.get(source.suffix.lower())
    data = source.read_bytes()
    if mime is None or len(data) > MAX_INLINE_IMAGE_BYTES:
        if shutil.which("ffmpeg") is None:
            raise GoogleModelError(f"{source.name}: unsupported or too large, and ffmpeg is not available to convert it")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "image.jpg"
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-vf", "scale='min(1600,iw)':-2", "-q:v", "3", str(out)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0 or not out.exists():
                raise GoogleModelError(f"{source.name}: could not convert image: {result.stderr[-200:]}")
            data, mime = out.read_bytes(), "image/jpeg"
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


def generate_json(
    parts: List[Dict[str, Any]],
    response_schema: Dict[str, Any],
    *,
    system: str = "",
    model: Optional[str] = None,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Ask Gemini for a JSON object matching `response_schema` (OpenAPI subset)."""
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": 0.4,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    model_id = model or os.getenv("ARIA_VIDEO_PLANNER_MODEL", DEFAULT_PLANNER_MODEL)
    payload = _post(_generate_content_url(model_id), body, timeout)
    try:
        text = "".join(part.get("text", "") for part in payload["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise GoogleModelError(f"{model_id} returned no usable JSON: {str(payload)[:300]}") from exc


def _wav_from_pcm(pcm: bytes, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", len(pcm))
    )
    return header + pcm


def synthesize_speech(text: str, output_path: str, *, voice: Optional[str] = None, model: Optional[str] = None) -> str:
    """Narrate `text` with Gemini TTS into a WAV file; returns the path."""
    text = (text or "").strip()
    if not text:
        raise GoogleModelError("Nothing to narrate")
    model_id = model or os.getenv("ARIA_VIDEO_TTS_MODEL", DEFAULT_TTS_MODEL)
    body = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or DEFAULT_TTS_VOICE}}},
        },
    }
    payload = _post(_generate_content_url(model_id), body, timeout=90.0)
    try:
        inline = payload["candidates"][0]["content"]["parts"][0]["inlineData"]
        pcm = base64.b64decode(inline["data"])
    except (KeyError, IndexError) as exc:
        raise GoogleModelError(f"{model_id} returned no audio: {str(payload)[:300]}") from exc
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_wav_from_pcm(pcm))
    return str(out)


def generate_shot(
    prompt: str,
    output_path: str,
    *,
    duration_seconds: int,
    aspect_ratio: str,
    image_path: Optional[str] = None,
    model: Optional[str] = None,
    confirmed: bool = False,
) -> str:
    """Generate one shot with Veo. Refuses unless `confirmed` — it is billed per second."""
    if not confirmed:
        raise GoogleModelError("Veo generation is paid per second; it needs explicit confirmation")
    from aria_code.providers.google_video_provider import generate_video_google

    model_id = model or os.getenv("ARIA_VIDEO_VEO_MODEL", DEFAULT_VEO_MODEL)
    result = generate_video_google(
        prompt, image_path=image_path, model=model_id, aspect_ratio=aspect_ratio, duration_seconds=duration_seconds,
        max_wait_seconds=600.0,
    )
    if not result.get("success") or not result.get("path"):
        raise GoogleModelError(f"Veo ({model_id}) failed: {result.get('error') or result}")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(result["path"], out)
    return str(out)
