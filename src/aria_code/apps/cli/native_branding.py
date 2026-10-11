"""Serialize the existing mascot without copying or changing its design."""
from __future__ import annotations

import os


def robot_rows(*, hidden: bool = False) -> list:
    if hidden or os.environ.get("ARIA_ROBOT_RENDER", "").strip().lower() == "off":
        return []
    from aria_code.ui.robot import ROBOT_ROW_COUNT, get_robot_row
    return [[{"style": style, "text": text} for style, text in get_robot_row(0, row)]
            for row in range(ROBOT_ROW_COUNT)]
