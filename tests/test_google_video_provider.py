"""Veo 3.1 request shape, region, polling and response parsing (no network)."""
import base64
from types import SimpleNamespace

from aria_code.providers import google_video_provider as veo


def test_durations_round_up_to_what_veo_accepts():
    assert [veo.veo_duration(s) for s in (1, 4, 4.5, 6, 7, 8, 12, None)] == [4, 4, 6, 6, 8, 8, 8, 6]


def test_region_is_never_global(monkeypatch):
    monkeypatch.setenv("VERTEX_LOCATION", "global")
    monkeypatch.delenv("VEO_LOCATION", raising=False)
    assert veo.veo_location() == "us-central1"
    monkeypatch.setenv("VEO_LOCATION", "europe-west4")
    assert veo.veo_location() == "europe-west4"


def test_generates_through_fetch_predict_operation(monkeypatch, tmp_path):
    monkeypatch.setenv("VERTEX_PROJECT", "proj")
    monkeypatch.setenv("VERTEX_LOCATION", "global")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(veo, "_get_vertex_token", lambda: "t")
    monkeypatch.setattr(veo.time, "sleep", lambda _: None)
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG")
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        if url.endswith(":predictLongRunning"):
            return SimpleNamespace(status_code=200, json=lambda: {"name": "op/1"}, text="")
        return SimpleNamespace(status_code=200, text="", json=lambda: {"done": True, "response": {"videos": [{"bytesBase64Encoded": base64.b64encode(b"MP4").decode()}]}})

    import requests
    monkeypatch.setattr(requests, "post", post)
    artifact = tmp_path / "clip.mp4"
    monkeypatch.setattr("aria_code.artifacts.create_user_artifact", lambda *a: SimpleNamespace(path=artifact))

    result = veo.generate_video_google("waves", image_path=str(image), aspect_ratio="3:4", duration_seconds=5)
    assert result["success"] and artifact.read_bytes() == b"MP4" and result["model_id"] == "veo-3.1-fast-generate-001"
    submit_url, body = calls[0]
    assert submit_url.startswith("https://us-central1-aiplatform.googleapis.com/") and "veo-3.1-fast-generate-001:predictLongRunning" in submit_url
    assert body["parameters"] == {"aspectRatio": "9:16", "durationSeconds": 6, "sampleCount": 1, "generateAudio": False}
    assert body["instances"][0]["image"]["mimeType"] == "image/png"
    assert calls[1][0].endswith(":fetchPredictOperation") and calls[1][1] == {"operationName": "op/1"}
