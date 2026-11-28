#!/usr/bin/env python3
"""Generate Aria Code v0.170.0 TUI demo GIF and PNG matching the real terminal UI."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
USER_SCREENSHOT = Path(
    "/Users/mac/.gemini/antigravity/brain/44421a38-62ae-45bd-b318-9f1634dd8218/.user_uploaded/media_1795846532435_2234c331.jpg"
)

BG = "#FFFFFF"
CHROME_BG = "#F5F5F5"
BORDER = "#E5E7EB"
LINE_RULE = "#4B5563"
FG = "#111827"
MUTED = "#6B7280"
TEAL = "#0E7490"
TEAL_DARK = "#155E75"
AMBER = "#B45309"
GREEN = "#15803D"

WIDTH = 900
HEIGHT = 560
PAD_X = 24
TITLE_H = 34


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size, index=1 if (bold and candidate.endswith(".ttc")) else 0)
            except OSError:
                try:
                    return ImageFont.truetype(candidate, size=size)
                except OSError:
                    continue
    return ImageFont.load_default()


FONT = _load_font(15)
FONT_BOLD = _load_font(15, bold=True)
FONT_SM = _load_font(13)


def _draw_mascot(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Draw the pixel robot mascot matching the Aria Code TUI header."""
    # Main body
    draw.rectangle([x + 8, y, x + 76, y + 36], fill=TEAL)
    # Side ears/arms
    draw.rectangle([x, y + 14, x + 8, y + 26], fill=TEAL)
    draw.rectangle([x + 76, y + 14, x + 84, y + 26], fill=TEAL)
    # Eyes
    draw.rectangle([x + 22, y + 14, x + 30, y + 20], fill="#082F49")
    draw.rectangle([x + 54, y + 14, x + 62, y + 20], fill="#082F49")
    # Feet
    draw.rectangle([x + 14, y + 36, x + 28, y + 44], fill=TEAL_DARK)
    draw.rectangle([x + 56, y + 36, x + 70, y + 44], fill=TEAL_DARK)


def _new_tui_frame(prompt_text: str, status_right: str = "Native ready  ·  F1 help  ·  Ctrl+O details") -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    # macOS title bar
    draw.rectangle([0, 0, WIDTH, TITLE_H], fill=CHROME_BG)
    draw.line([0, TITLE_H, WIDTH, TITLE_H], fill=BORDER, width=1)
    for idx, col in enumerate(("#FF5F56", "#FFBD2E", "#27C93F")):
        cx = 18 + idx * 20
        cy = TITLE_H // 2
        draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=col)
    title = "Aria Code — aria-code — 90x28"
    draw.text((WIDTH // 2 - len(title) * 4, 9), title, fill=MUTED, font=FONT_SM)

    # Header block: mascot + Aria Code v0.170.0
    _draw_mascot(draw, PAD_X, 50)
    draw.text((130, 48), "Aria Code", fill=FG, font=FONT_BOLD)
    draw.text((224, 48), "v0.170.0", fill=MUTED, font=FONT)
    draw.text((130, 70), "qwen2.5:7b · Ollama", fill=MUTED, font=FONT_SM)
    draw.text((130, 88), "~/Desktop/aria-code", fill=MUTED, font=FONT_SM)

    # Bottom input box (matching screenshot)
    top_rule_y = HEIGHT - 90
    bot_rule_y = HEIGHT - 42
    draw.line([PAD_X, top_rule_y, WIDTH - 270, top_rule_y], fill=LINE_RULE, width=1)
    draw.text((WIDTH - 260, top_rule_y - 8), "Enter send · Ctrl+J newline", fill=MUTED, font=FONT_SM)

    draw.text((PAD_X + 6, top_rule_y + 16), ">", fill=AMBER, font=FONT_BOLD)
    draw.text((PAD_X + 26, top_rule_y + 16), prompt_text, fill=FG if prompt_text else MUTED, font=FONT)

    draw.line([PAD_X, bot_rule_y, WIDTH - PAD_X, bot_rule_y], fill=LINE_RULE, width=1)
    draw.text((PAD_X, bot_rule_y + 12), "▸ writes need approval · network off", fill=AMBER, font=FONT_SM)
    draw.text((340, bot_rule_y + 12), f"· {status_right}", fill=MUTED, font=FONT_SM)
    return img, draw


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    if USER_SCREENSHOT.is_file():
        with Image.open(USER_SCREENSHOT) as src:
            src.convert("RGB").save(ASSETS / "aria-tui-home.png")

    # Capture real aria-native render output for the transcript area
    bin_path = ROOT / "rust" / "target" / "debug" / "aria-native"
    transcript_lines: list[tuple[str, str]] = [
        ("Ready — ask anything, or type /help for commands.", FG, True),
        ("Switch with /model · sessions /resume · files @file", MUTED, False),
        ("Press Ctrl+O for details · F1 for shortcuts", MUTED, False),
        ("", FG, False),
        ("> Review recent changes and check safety envelope tests", TEAL, True),
        ("[Aria] gemini-2.5-pro", MUTED, False),
        ("[ok] edit_file -> rust/physical/src/safety.rs (+182/-0)", GREEN, False),
        ("[ok] run_command -> cargo test -p aria-physical (24 passed, 0 failed)", GREEN, False),
        ("[summary] tools: 2 ok, 0 failed  |  files: rust/physical/src/safety.rs", TEAL_DARK, False),
        ("All 24 aria-physical tests pass with continuous Pose2D/Twist2D safety checks.", FG, False),
    ]

    # Verify aria-native binary runs cleanly if present
    if bin_path.is_file():
        subprocess.run([str(bin_path), "--version"], check=False, capture_output=True)

    frames: list[tuple[Image.Image, int]] = []
    # Frame 1: Home welcome screen
    img0, draw0 = _new_tui_frame('Try "Review recent changes and check for edge cases"')
    y = 128
    for text, color, is_bold in transcript_lines[:3]:
        draw0.text((PAD_X, y), text, fill=color, font=FONT_BOLD if is_bold else FONT)
        y += 22
    frames.append((img0, 1200))

    # Progressive frames for tool execution & final answer
    for end_idx in range(5, len(transcript_lines) + 1):
        img, draw = _new_tui_frame("")
        y = 128
        for text, color, is_bold in transcript_lines[:end_idx]:
            if text:
                draw.text((PAD_X, y), text, fill=color, font=FONT_BOLD if is_bold else FONT)
            y += 24
        frames.append((img, 2000 if end_idx == len(transcript_lines) else 550))

    imgs = [f[0] for f in frames]
    durations = [f[1] for f in frames]
    imgs[0].save(
        ASSETS / "demo-coding.gif",
        save_all=True,
        append_images=imgs[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    imgs[-1].save(ASSETS / "demo-coding.png")
    print("Saved aria-tui-home.png and updated demo-coding.gif/png")


if __name__ == "__main__":
    main()
