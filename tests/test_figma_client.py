"""Tests for figma_client.py — covering URL parsing, node extraction,
image exporting, and local variables.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from aria_code.clients import figma_client


def test_parse_figma_url_handles_various_formats():
    # 1. Full design URL with hyphenated node id
    u1 = "https://www.figma.com/design/AbC123XyZ/Aria-Design-System?node-id=102-45&t=foo"
    res1 = figma_client.parse_figma_url(u1)
    assert res1["file_key"] == "AbC123XyZ"
    assert res1["node_id"] == "102:45"

    # 2. File URL with colon node id
    u2 = "https://www.figma.com/file/XyZ789/Voice-Orb?node-id=1:5"
    res2 = figma_client.parse_figma_url(u2)
    assert res2["file_key"] == "XyZ789"
    assert res2["node_id"] == "1:5"

    # 3. Raw file key with no URL
    res3 = figma_client.parse_figma_url("myRawKey123")
    assert res3["file_key"] == "myRawKey123"
    assert res3["node_id"] is None


def test_get_file_nodes_success():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "name": "Aria UI",
        "lastModified": "2026-09-06T10:00:00Z",
        "nodes": {
            "1:23": {
                "document": {
                    "id": "1:23",
                    "name": "VoiceOrb",
                    "type": "FRAME",
                    "fills": [{"type": "SOLID", "color": {"r": 0.2, "g": 0.4, "b": 0.9}}],
                }
            }
        },
    }

    with patch.object(figma_client, "_token", return_value="fake_token"), \
         patch("requests.get", return_value=fake_resp) as mock_get:
        res = figma_client.get_file_nodes("file123", ["1-23"], depth=2)

    assert res["success"] is True
    assert res["name"] == "Aria UI"
    assert "1:23" in res["nodes"]
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "ids=1%3A23" in args[0] or kwargs.get("params", {}).get("ids") == "1:23"
    assert kwargs.get("params", {}).get("depth") == 2


def test_export_images_success():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "images": {
            "1:23": "https://figma-alpha-api.s3.us-west-2.amazonaws.com/images/123.svg"
        }
    }

    with patch.object(figma_client, "_token", return_value="fake_token"), \
         patch("requests.get", return_value=fake_resp) as mock_get:
        res = figma_client.export_images("file123", ["1:23"], format="svg", scale=2.0)

    assert res["success"] is True
    assert res["format"] == "svg"
    assert "1:23" in res["images"]
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["format"] == "svg"
    assert kwargs["params"]["scale"] == 2.0


def test_get_local_variables_success():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "meta": {
            "variables": {
                "var1": {"name": "colors/primary", "resolvedType": "COLOR"}
            },
            "variableCollections": {
                "col1": {"name": "AriaTokens"}
            }
        }
    }

    with patch.object(figma_client, "_token", return_value="fake_token"), \
         patch("requests.get", return_value=fake_resp):
        res = figma_client.get_local_variables("file123")

    assert res["success"] is True
    assert "var1" in res["variables"]
    assert "col1" in res["variable_collections"]
