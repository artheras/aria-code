"""The Rust protocol must carry the existing robot, not a second rendition."""
import hashlib
from pathlib import Path

import pytest

from aria_code.apps.cli.native_branding import robot_rows
from aria_code.ui import robot


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_protocol_spans_equal_the_existing_robot(theme, monkeypatch):
    monkeypatch.setattr(robot, "_theme_cache", theme)
    monkeypatch.delenv("ARIA_ROBOT_RENDER", raising=False)
    rows = robot_rows()
    assert rows == [[{"style": style, "text": text} for style, text in robot.get_robot_row(0, row)]
                    for row in range(4)]
    assert ["".join(span["text"] for span in row) for row in rows] == [
        "▗▛▀▀▀▀▀▜▖", "▌▌▗▖ ▂ ▐▐", "▐▙▄▄▄▄▄▟▌", "▝▀▀▀▀▀▀▀▘"]
    assert rows[1][2]["style"] == "#FFFDF5 on #0E0E0E"
    assert rows[1][4]["style"] == "#F9B467 on #0E0E0E"


def test_explicitly_hidden_robot_stays_hidden(monkeypatch):
    assert robot_rows(hidden=True) == []
    monkeypatch.setenv("ARIA_ROBOT_RENDER", "off")
    assert robot_rows() == []


def test_original_artwork_is_unchanged():
    asset = Path(__file__).resolve().parents[1] / "docs/assets/aria-robot.png"
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == "cd51f17ed7c6888ee0e7198f5f21f40f837ff295b2a8ba265ded35988d68dce5"
