"""Tests for the analyze_file LLM tool — lets the agent read uploaded
documents/images autonomously (not just via the /file slash command)."""
import base64
import pathlib
import sys

_CLI_DIR = str(pathlib.Path(__file__).parents[1])
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from aria_code.tools.file_analysis_tools import tool_analyze_file  # noqa: E402

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_requires_path():
    assert tool_analyze_file({"path": ""})["success"] is False


def test_missing_file():
    r = tool_analyze_file({"path": "/no/such/file.pdf"})
    assert r["success"] is False and r["error"]


def test_parses_csv(tmp_path):
    f = tmp_path / "pnl.csv"
    f.write_text("date,pnl\n2026-01-01,100\n2026-01-02,-50\n")
    r = tool_analyze_file({"path": str(f)})
    assert r["success"] and r["file_type"] == "csv"
    assert "pnl" in r["content"]
    assert r["char_count"] > 0


def test_caps_long_text(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("A" * 50_000)
    r = tool_analyze_file({"path": str(f), "max_chars": 2000})
    assert r["success"]
    assert len(r["content"]) <= 2000


def test_image_queued_for_vision(tmp_path, monkeypatch):
    f = tmp_path / "chart.png"
    f.write_bytes(_PNG)
    r = tool_analyze_file({"path": str(f)})
    assert r["success"] and r["file_type"] == "image"
    assert r["vision_attached"] is True
    queued = r.get("_vision_image")
    assert queued is not None and "base64," not in queued  # raw b64, prefix stripped


def test_video_routes_and_degrades_gracefully(tmp_path):
    # Without opencv-python, a video must still parse to metadata + an install
    # hint (never crash).
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
    r = tool_analyze_file({"path": str(f)})
    assert r["success"] is True
    assert r["file_type"] == "video"


def test_video_extensions_recognized(tmp_path):
    from aria_code.tools.file_analysis_tools import parse_file
    for ext in ("mp4", "mov", "avi", "mkv", "webm", "m4v"):
        f = tmp_path / f"v.{ext}"
        f.write_bytes(b"\x00" * 64)
        fc = parse_file(str(f))
        assert fc.file_type == "video", ext


def test_optional_parser_detection_refreshes_after_install(monkeypatch):
    from aria_code.tools import file_analysis_tools as tools

    sentinel = object()
    monkeypatch.setattr(tools, "_docx_mod", None)
    original_try = tools._try

    def fake_try(module_name):
        if module_name == "docx":
            return sentinel
        return original_try(module_name)

    monkeypatch.setattr(tools, "_try", fake_try)

    status = tools.refresh_optional_parsers()

    assert status["python-docx"] is True
    assert tools._docx_mod is sentinel

