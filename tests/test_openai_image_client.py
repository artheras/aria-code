"""Tests for openai_image_client.py — field shapes verified against the
official openai-python SDK source this session (images/generations and
images/edits, since the public docs page 403s to scrapers)."""
from __future__ import annotations

import base64
import pytest
from unittest.mock import MagicMock, patch

from aria_code.clients import openai_image_client as oic

# 可选依赖 guard：这些用例调用的代码路径需要 PIL（files extra）。
# CI 的 test workflow 只装 .[cn,dev]，其注释明确写着"没装 extra 的可选功能
# 会优雅跳过"——但这几个用例此前没有 guard，缺依赖时直接 FAILED 而不是
# SKIPPED，让 pytest (Python 3.12) 长期红灯。改成 importorskip 以符合该契约。
pytest.importorskip("PIL", reason="需要 files extra（pip install 'aria-code[files]'）")


def _fake_b64_png() -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def test_api_key_prefers_env_var(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert oic._api_key() == "sk-from-env"


def test_generate_image_missing_key_raises_actionable_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(oic, "_providers_path") as mock_path:
        mock_path.return_value.exists.return_value = False
        try:
            oic._require_key()
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "/apikey set openai" in str(exc)


def test_generate_image_posts_gpt_image_2_6_sunburst_with_correct_fields(tmp_path):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"created": 1, "data": [{"b64_json": _fake_b64_png()}]}
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return resp

    with patch.object(oic, "_require_key", return_value="sk-test"), \
         patch("requests.post", side_effect=fake_post), \
         patch("artifacts.create_user_artifact") as mock_artifact:
        mock_artifact.return_value.path = tmp_path / "out.png"
        result = oic.generate_image("a minimal poster", size="1024x1024", quality="high", confirmed=True)

    assert captured["url"].endswith("/images/generations")
    assert captured["json"]["model"] == "gpt-image-2.6-sunburst"
    assert captured["json"]["prompt"] == "a minimal poster"
    assert result["success"] is True
    assert result["model"] == "gpt-image-2.6-sunburst"


def test_generate_image_supports_flare_svg_and_complexity(tmp_path):
    svg_b64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"created": 1, "data": [{"b64_json": svg_b64}]}
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return resp

    with patch.object(oic, "_require_key", return_value="sk-test"), \
         patch("requests.post", side_effect=fake_post), \
         patch("artifacts.create_user_artifact") as mock_artifact:
        mock_artifact.return_value.path = tmp_path / "out.svg"
        result = oic.generate_image(
            "vector badge",
            model="flare",
            complexity="medium",
            output_format="svg",
            confirmed=True,
        )

    assert captured["json"]["model"] == "gpt-image-2.6-flare"
    assert captured["json"]["complexity"] == "medium"
    assert captured["json"]["output_format"] == "svg"
    assert result["success"] is True
    assert result["format"] == "svg"


def test_deprecated_gpt_image_2_is_rejected():
    result = oic.generate_image("a poster", model="gpt-image-2", confirmed=True)
    assert result["success"] is False
    assert "2027-02-10" in result["error"]


def test_generate_image_surfaces_api_error():
    resp = MagicMock(status_code=401, text="Incorrect API key provided")
    with patch.object(oic, "_require_key", return_value="sk-bad"), \
         patch("requests.post", return_value=resp):
        result = oic.generate_image("a poster", confirmed=True)
    assert result["success"] is False
    assert "401" in result["error"]


def test_generate_image_refuses_without_confirmed():
    result = oic.generate_image("a poster")
    assert result["success"] is False
    assert "confirmed" in result["error"]


def test_edit_image_refuses_without_confirmed():
    result = oic.edit_image("/nonexistent/photo.jpg", "make it duotone")
    assert result["success"] is False
    assert "confirmed" in result["error"]


def test_edit_image_missing_file():
    result = oic.edit_image("/nonexistent/photo.jpg", "make it duotone", confirmed=True)
    assert result["success"] is False
    assert "not found" in result["error"]


def test_edit_image_posts_multipart_to_edits_endpoint(tmp_path):
    from PIL import Image

    src = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16), color="red").save(src)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"created": 1, "data": [{"b64_json": _fake_b64_png()}]}
    captured = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["files"] = list(files.keys())
        return resp

    with patch.object(oic, "_require_key", return_value="sk-test"), \
         patch("requests.post", side_effect=fake_post), \
         patch("artifacts.create_user_artifact") as mock_artifact:
        mock_artifact.return_value.path = tmp_path / "out.png"
        result = oic.edit_image(str(src), "convert to duotone", confirmed=True)

    assert captured["url"].endswith("/images/edits")
    assert captured["data"]["model"] == "gpt-image-2.6-sunburst"
    assert "image" in captured["files"]
    assert result["success"] is True


def test_edit_image_with_mask_uploads_both_files(tmp_path):
    from PIL import Image

    src = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16), color="red").save(src)
    mask = tmp_path / "mask.png"
    Image.new("RGBA", (16, 16), color=(0, 0, 0, 0)).save(mask)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"created": 1, "data": [{"b64_json": _fake_b64_png()}]}
    captured = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        captured["files"] = list(files.keys())
        return resp

    with patch.object(oic, "_require_key", return_value="sk-test"), \
         patch("requests.post", side_effect=fake_post), \
         patch("artifacts.create_user_artifact") as mock_artifact:
        mock_artifact.return_value.path = tmp_path / "out.png"
        result = oic.edit_image(str(src), "fill in the sky", mask_path=str(mask), confirmed=True)

    assert "image" in captured["files"]
    assert "mask" in captured["files"]
    assert result["success"] is True


def test_edit_image_mask_not_found():
    result = oic.edit_image("/nonexistent/photo.jpg", "make it duotone", mask_path="/nonexistent/mask.png", confirmed=True)
    assert result["success"] is False
    assert "not found" in result["error"]


def test_normalize_image_for_upload_corrects_exif_rotation(tmp_path):
    from PIL import Image

    # A 40x20 image with EXIF orientation=6 ("rotate 90 CW to display
    # correctly") — real-world equivalent of a portrait iPhone photo whose
    # raw pixels are stored landscape. Without exif_transpose, the upload
    # would be genuinely sideways (reproduced against real photos before
    # this fix existed).
    img = Image.new("RGB", (40, 20), color="blue")
    exif = img.getexif()
    exif[274] = 6
    src = tmp_path / "sideways.jpg"
    img.save(src, exif=exif)

    filename, data = oic._normalize_image_for_upload(src)
    assert filename.endswith(".png")
    from io import BytesIO
    corrected = Image.open(BytesIO(data))
    # orientation=6 rotates a 40x20 source to 20x40 once corrected
    assert corrected.size == (20, 40)


def test_normalize_image_for_upload_produces_valid_single_frame_png(tmp_path):
    from PIL import Image

    src = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16), color="green").save(src)

    filename, data = oic._normalize_image_for_upload(src)
    from io import BytesIO
    reopened = Image.open(BytesIO(data))
    assert reopened.format == "PNG"
    assert reopened.mode == "RGB"


def test_normalize_image_for_upload_keeps_alpha_for_mask(tmp_path):
    from PIL import Image

    mask = tmp_path / "mask.png"
    Image.new("RGBA", (16, 16), color=(0, 0, 0, 0)).save(mask)

    _, data = oic._normalize_image_for_upload(mask, keep_alpha=True)
    from io import BytesIO
    reopened = Image.open(BytesIO(data))
    assert reopened.mode == "RGBA"


def test_estimate_cost_known_size_quality(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    result = oic.estimate_cost(size="1024x1024", quality="high")
    assert result["estimated_cost_usd"] == 0.167
    assert result["model"] == "gpt-image-2.6-sunburst"
    flare_res = oic.estimate_cost(size="1024x1024", quality="low", model="flare")
    assert flare_res["estimated_cost_usd"] == 0.005
    assert flare_res["model"] == "gpt-image-2.6-flare"


def test_google_cloud_vertex_imagen_priority_when_gcp_project_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "aria-vertex-prod")
    monkeypatch.setenv("VERTEX_ACCESS_TOKEN", "ya29.test-token")

    with patch.object(oic, "_api_key", return_value=""):
        est = oic.estimate_cost(size="1024x1536", quality="high")
        assert est["provider"] == "google"
        assert est["model"] == "imagen-3.0-generate-002"
        assert est["estimated_cost_usd"] == 0.04

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"predictions": [{"bytesBase64Encoded": _fake_b64_png()}]}
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return resp

        with patch("requests.post", side_effect=fake_post), \
             patch("artifacts.create_user_artifact") as mock_artifact:
            mock_artifact.return_value.path = tmp_path / "vertex_out.png"
            result = oic.generate_image("editorial cover on Vertex AI", size="1024x1536", confirmed=True)

        assert result["success"] is True
        assert result["provider"] == "google"
        assert result["model"] == "imagen-3.0-generate-002"
        assert "projects/aria-vertex-prod/locations/us-central1/publishers/google/models/imagen-3.0-generate-002:predict" in captured["url"]
        assert captured["json"]["parameters"]["aspectRatio"] == "3:4"


def test_estimate_cost_unknown_combo_reports_none():
    result = oic.estimate_cost(size="auto", quality="auto")
    assert result["estimated_cost_usd"] is None
