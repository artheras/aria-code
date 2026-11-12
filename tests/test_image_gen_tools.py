"""Tests for image_gen_tools.py — registers real (non-financial) image
generation into aria-code's own interactive chat loop. Regression test for
a real reported bug: a live chat session asked for a landscape photo and
the model said it could only generate stock charts, because generate_image/
edit_image were never registered in LOCAL_TOOLS (aria_cli.py) — only
exposed outward via the MCP server for other clients."""
from __future__ import annotations

from unittest.mock import patch

from aria_code.tools.image_gen_tools import (
    IMAGE_TOOL_SCHEMAS,
    register_image_tools,
    tool_edit_image,
    tool_generate_image,
)


def test_register_image_tools_adds_both_tools():
    registry: dict = {}
    schemas: list = []
    added = register_image_tools(registry, schemas)
    assert added == 2
    assert "generate_image" in registry
    assert "edit_image" in registry
    names = {s["function"]["name"] for s in schemas}
    assert {"generate_image", "edit_image"} <= names


def test_register_image_tools_is_idempotent():
    registry: dict = {"generate_image": ("existing", "existing desc")}
    schemas: list = list(IMAGE_TOOL_SCHEMAS)
    added = register_image_tools(registry, schemas)
    assert added == 1  # only edit_image got added, generate_image already present
    assert registry["generate_image"] == ("existing", "existing desc")
    assert len(schemas) == len(IMAGE_TOOL_SCHEMAS)  # no duplicate schema entries


def test_tool_generate_image_requires_prompt():
    result = tool_generate_image({})
    assert result["success"] is False
    assert "prompt" in result["error"]


def test_tool_generate_image_calls_local_backend_with_dimensions(monkeypatch):
    from aria_code.providers import local_image_provider

    called = {}

    def fake_generate(prompt, **kwargs):
        called["prompt"] = prompt
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(local_image_provider, "generate_image_local", fake_generate)
    result = tool_generate_image({"prompt": "a quiet coastal landscape", "width": 768, "height": 512})
    assert result["success"] is True
    assert called["prompt"] == "a quiet coastal landscape"
    assert called["kwargs"]["width"] == 768
    assert called["kwargs"]["height"] == 512


def test_tool_generate_image_missing_deps_returns_actionable_error():
    with patch("builtins.__import__", side_effect=ImportError("no diffusers")):
        result = tool_generate_image({"prompt": "x"})
    assert result["success"] is False
    assert "本地图片生成不可用" in result["error"]


def test_tool_edit_image_requires_image_path_and_prompt():
    result = tool_edit_image({"prompt": "restyle it"})
    assert result["success"] is False
    assert "image_path" in result["error"] or "prompt" in result["error"]


def test_tool_edit_image_calls_local_backend_with_strength(monkeypatch):
    from aria_code.providers import local_image_provider

    called = {}

    def fake_edit(image_path, prompt, **kwargs):
        called["image_path"] = image_path
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(local_image_provider, "edit_image_local", fake_edit)
    result = tool_edit_image({"image_path": "/tmp/x.jpg", "prompt": "make it duotone", "strength": 0.4})
    assert result["success"] is True
    assert called["image_path"] == "/tmp/x.jpg"
    assert called["kwargs"]["strength"] == 0.4


def test_aria_cli_local_tools_registry_includes_image_tools():
    """Integration check: the actual aria_cli.py module (the real chat
    loop's tool registry) has these tools registered, not just the
    standalone register_image_tools() function in isolation."""
    import aria_cli

    assert "generate_image" in aria_cli.LOCAL_TOOLS
    assert "edit_image" in aria_cli.LOCAL_TOOLS
    names = {s["function"]["name"] for s in aria_cli.LOCAL_TOOL_SCHEMAS}
    assert {"generate_image", "edit_image"} <= names
