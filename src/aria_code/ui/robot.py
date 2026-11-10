"""Aria robot mascot — the terminal rendition of the robot artwork.

Drawn the way Claude Code draws its mascot: a few flat colours and Unicode
quadrant blocks (▗▖▘▝▛▜▙▟), each character cell holding 2×2 sub-pixels. That
keeps the robot small (4 rows × 9 columns, beside four lines of text) and its
edges exact in any terminal and font, with no image and no blending.

The grid is the artwork's own: 18 units wide × 16 tall (ear, body, ear; cap,
screen, base, feet), each unit one sub-pixel across and half a sub-pixel down
— which, with terminal cells twice as tall as wide, keeps the units square.
Each eye is two quadrants (a square) and the two are identical,
the cap corners ▛ ▜, the ears ▌ ▐, the four feet ▀. Colours are sampled from
the artwork (docs/assets/aria-robot.png) on this grid; only the light-theme
greys and body are deepened, where the artwork's would vanish against a
white background. The orange seam between body and base is left out: at
this size it would share a cell with two other colours, and a cell has two.

Runtime state is shown by the compact status dot, not by the mascot.
"""

from __future__ import annotations

import os
import sys
import threading
import time
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


# Eye symbols per state. IDLE mirrors the mascot art: two matching square eyes.
_EYES = {
    RobotState.IDLE:      ("■", "■"),
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

# ── Theme-aware palette ───────────────────────────────────────────────────────
# Colours sampled from the robot artwork. Roles name what a cell shows; each
# resolves to a Rich style ("<fg> on <bg>").
_COLOURS = {
    "dark": {
        "body":   "#F4EBE4",   # cream shell
        "screen": "#0E0E0E",   # black screen
        "eye":    "#FFFDF5",   # square eye, brighter than the shell
        "ear":    "#B6ADA4",   # grey ear nub
        "base":   "#B6ADA4",   # grey underside
        "leg":    "#D5CCC3",   # light grey feet
    },
    "light": {
        "body":   "#E6DDD0",   # deeper cream: the artwork's would vanish on white
        "screen": "#0E0E0E",
        "eye":    "#FFFDF5",
        "ear":    "#9C948B",
        "base":   "#9C948B",
        "leg":    "#B8AFA6",
    },
}


def _palette(theme: str) -> dict:
    c = _COLOURS[theme]
    return {
        "body":        c["body"],
        "body_screen": f"{c['body']} on {c['screen']}",
        "eye":         f"{c['eye']} on {c['screen']}",
        "screen":      f"on {c['screen']}",
        "ear_body":    f"{c['ear']} on {c['body']}",
        "base":        c["base"],
        "base_leg":    f"{c['base']} on {c['leg']}",
    }


_PALETTES = {theme: _palette(theme) for theme in _COLOURS}

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


# 9 columns × 4 rows; each cell is (palette-role, glyphs).
_MASCOT_TEMPLATE = [
    [("body", "▗"), ("body_screen", "▛▀▀▀▀▀▜"), ("body", "▖")],                       # cap, screen top
    [("ear_body", "▌"), ("body_screen", "▌"), ("eye", "▗▖"), ("screen", " "),          # ears, two matching eyes
     ("eye", "▗▖"), ("body_screen", "▐"), ("ear_body", "▐")],
    [("body", "▐"), ("body_screen", "▙▄▄▄▄▄▟"), ("body", "▌")],                       # screen bottom, body
    [("base", "▝"), ("base_leg", "▀"), ("base", "▀"), ("base_leg", "▀"), ("base", "▀"),  # base and four feet
     ("base_leg", "▀"), ("base", "▀"), ("base_leg", "▀"), ("base", "▘")],
]

ROBOT_ROW_COUNT = len(_MASCOT_TEMPLATE)


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


def get_robot_row(tick: int, row: int) -> list:
    """Return (style, text) fragments for one robot row, themed.

    Rows: cap and screen top; ears and two eyes; screen bottom and body;
    base and four feet. Roles follow the active light/dark theme
    (see detect_theme()).
    """
    del tick
    pal = _PALETTES[detect_theme()]
    return [(pal[key] if key else "", text) for key, text in _MASCOT_TEMPLATE[row]]


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
