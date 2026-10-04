"""Aria robot mascot — animated terminal character.

States
------
  The mascot stays visually stable at startup. Runtime state is shown by the
  compact status dot so the banner keeps the same low-noise feel as Claude Code.

The canonical artwork is ``assets/aria-robot.png``. Its sampled RGB pixels
are rendered as paired half blocks with transparent space around the original
silhouette. The mascot does not change colour with the terminal theme.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import base64
import zlib
from functools import lru_cache
from enum import Enum


class RobotState(Enum):
    IDLE      = "idle"
    THINKING  = "thinking"
    STREAMING = "streaming"
    ERROR     = "error"
    DONE      = "done"


# ── Shared mutable state (written by aria_cli, read by input_box) ─────────────
_state      = RobotState.IDLE
_state_lock = threading.Lock()
_done_at: float | None = None  # timestamp when DONE state was set


def set_robot_state(state: RobotState) -> None:
    global _state, _done_at
    with _state_lock:
        _state = state
        _done_at = time.monotonic() if state is RobotState.DONE else None


def get_robot_state() -> RobotState:
    with _state_lock:
        # Auto-revert DONE → IDLE after 1.5 s
        if _state is RobotState.DONE and _done_at is not None:
            if time.monotonic() - _done_at > 1.5:
                return RobotState.IDLE
        return _state


# Eye symbols per state. IDLE mirrors the mascot art: one light square eye and
# one copper dash eye.
_EYES = {
    RobotState.IDLE:      ("■", "▬"),
    RobotState.THINKING:  ("◐", "◑"),
    RobotState.STREAMING: ("▸", "▸"),
    RobotState.ERROR:     ("×", "×"),
    RobotState.DONE:      ("✓", "✓"),
}

# Accent colour per state
_COLOUR = {
    RobotState.IDLE:      "#C08050",
    RobotState.THINKING:  "#d29922",
    RobotState.STREAMING: "#3fb950",
    RobotState.ERROR:     "#f85149",
    RobotState.DONE:      "#3fb950",
}

# Status text per state (used by status bar dot)
_STATUS = {
    RobotState.IDLE:      "",
    RobotState.THINKING:  "thinking…",
    RobotState.STREAMING: "generating…",
    RobotState.ERROR:     "error",
    RobotState.DONE:      "done",
}

_theme_cache: str | None = None


def _resolve_theme() -> str:
    pref = os.environ.get("ARIA_THEME", "").strip().lower()
    if pref in ("light", "dark"):
        return pref
    if sys.platform == "darwin":
        try:
            import subprocess
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1,
            )
            # `defaults` prints "Dark" in dark mode and errors (non-zero) in light.
            return "dark" if (r.returncode == 0 and "dark" in r.stdout.lower()) else "light"
        except Exception:
            pass
    # xterm-style COLORFGBG ("fg;bg"): bg 0-6/8 → dark, 7/9-15 → light.
    cfb = os.environ.get("COLORFGBG", "")
    if ";" in cfb:
        try:
            bg = int(cfb.split(";")[-1])
            return "light" if (bg == 7 or bg >= 9) else "dark"
        except Exception:
            pass
    return "dark"


def detect_theme() -> str:
    """Return 'light' or 'dark' for the mascot, auto-detected once and cached.

    Order: ``ARIA_THEME`` env override → macOS system appearance → ``COLORFGBG``
    → default dark.
    """
    global _theme_cache
    if _theme_cache is None:
        _theme_cache = _resolve_theme()
    return _theme_cache


ROBOT_COLUMN_COUNT = 28
ROBOT_ROW_COUNT = 13


@lru_cache(maxsize=2)
def _art_rows(columns: int) -> tuple:
    from .robot_pixels import PIXELS

    height, encoded = PIXELS[columns]
    pixels = memoryview(zlib.decompress(base64.b85decode(encoded)))
    rows = []
    for y in range(0, height, 2):
        fragments = []
        for x in range(columns):
            top = pixels[(y * columns + x) * 4:(y * columns + x) * 4 + 4]
            bottom = pixels[((y + 1) * columns + x) * 4:((y + 1) * columns + x) * 4 + 4]
            tc = "#%02x%02x%02x" % tuple(top[:3])
            bc = "#%02x%02x%02x" % tuple(bottom[:3])
            if top[3] and bottom[3]:
                style, glyph = f"{tc} on {bc}", "▀"
            elif top[3]:
                style, glyph = tc, "▀"
            elif bottom[3]:
                style, glyph = bc, "▄"
            else:
                style, glyph = "", " "
            if fragments and fragments[-1][0] == style:
                fragments[-1] = (style, fragments[-1][1] + glyph)
            else:
                fragments.append((style, glyph))
        rows.append(tuple(fragments))
    return tuple(rows)


def _resolve_eyes(state: RobotState, tick: int) -> tuple[str, str]:
    el, er = _EYES[state]
    if state is RobotState.IDLE:
        if tick % 24 in (0, 1):
            el, er = "·", "·"
    elif state is RobotState.THINKING:
        frames = (("◐", "◑"), ("◓", "◒"), ("◑", "◐"), ("◒", "◓"))
        el, er = frames[tick % len(frames)]
    elif state is RobotState.STREAMING:
        el = er = "▸" if (tick % 4) < 2 else "▹"
    return el, er


def get_robot_row(tick: int, row: int, columns: int = ROBOT_COLUMN_COUNT) -> list:
    """Return one row of the original artwork as true-colour half blocks.

    Rows: cap, shell top, recessed screen, eyes and ears, screen bottom,
    shell bottom, copper base, four feet. Colours stay faithful to the reference
    on both light and dark terminals.
    """
    del tick
    return list(_art_rows(columns)[row])


def get_robot_frame(tick: int) -> list:
    """Legacy single-fragment list (not split by row). Use get_robot_row() instead."""
    out = []
    for r in range(ROBOT_ROW_COUNT):
        out += get_robot_row(tick, r)
    return out


def get_status_dot(tick: int) -> list:
    """Compact inline indicator for the status bar — one animated glyph + state label.

    IDLE:      •               (copper, slow blink)
    THINKING:  ◐  thinking…   (yellow spinner)
    STREAMING: ▶  generating… (green pulse)
    ERROR:     ✕  error        (red)
    DONE:      ✓  done         (green, brief)
    """
    state = get_robot_state()
    col   = _COLOUR[state]
    if detect_theme() == "light":
        col = {
            RobotState.IDLE:      "#9A6700",
            RobotState.THINKING:  "#9A6700",
            RobotState.STREAMING: "#1A7F37",
            RobotState.ERROR:     "#CF222E",
            RobotState.DONE:      "#1A7F37",
        }[state]
    el, _ = _resolve_eyes(state, tick)
    if state is RobotState.IDLE:
        el = "•"
    label = _STATUS[state]

    frags: list = [(f"bold {col}", el)]
    if label:
        frags.append((col, f" {label}"))
    return frags
