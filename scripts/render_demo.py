#!/usr/bin/env python3
"""Render a recorded Aria Code session (scripts/record_demo.py) as a GIF.

Nothing on screen is drawn by hand. The .cast is replayed through a terminal
emulator (pyte), and every cell is painted with the character and colours the
CLI itself sent. Waiting is shortened — a spinner turning for forty seconds
while the model thinks becomes about a second — and approval menus and
results are held long enough to read; nothing else is changed.

    python scripts/record_demo.py coding --cast /tmp/coding.cast
    python scripts/render_demo.py --cast /tmp/coding.cast --gif docs/assets/demo-coding.gif

Needs: python -m pip install pyte Pillow
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont
from wcwidth import wcwidth

ROOT = Path(__file__).resolve().parents[1]

MONO = (Path("/System/Library/Fonts/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"))
MONO_BOLD = (Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"),)
# For the few glyphs a monospace font may lack (√, σ, CJK).
FALLBACK = (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
            Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))

# The recorder pins the CLI's light palette; these are the terminal's own
# colours for anything the CLI leaves at "default" or names by ANSI number.
BACKGROUND = "#FBF8F3"
FOREGROUND = "#1F2328"
NAMED = {
    "black": "#1F2328", "red": "#CF222E", "green": "#1A7F37", "brown": "#9A6700",
    "blue": "#0969DA", "magenta": "#8250DF", "cyan": "#1B7C83", "white": "#6E7781",
    "brightblack": "#57606A", "brightred": "#A40E26", "brightgreen": "#2DA44E",
    "brightbrown": "#BF8700", "brightblue": "#218BFF", "brightmagenta": "#A475F9",
    "brightcyan": "#3192AA", "brightwhite": "#8C959F",
}


class _Screen(pyte.Screen):
    """pyte drops SGR 2 (faint); the CLI prints each result's "Basis:" line faint.

    The CLI never uses italics, so faint is carried in pyte's italics flag and
    painted as faint, not slanted. 38/48 colour arguments are passed through
    untouched, since a 2 there means "24-bit", not "faint".
    """

    def select_graphic_rendition(self, *attrs: int) -> None:
        mapped: list[int] = []
        index = 0
        while index < len(attrs):
            attr = attrs[index]
            if attr in (38, 48) and index + 1 < len(attrs):
                width = 3 if attrs[index + 1] == 5 else 5
                mapped.extend(attrs[index:index + width])
                index += width
                continue
            mapped.extend({2: (3,), 22: (22, 23)}.get(attr, (attr,)))
            index += 1
        super().select_graphic_rendition(*mapped)


@dataclass
class Frame:
    t: float                                  # seconds since the recording started
    cells: list[list]                         # rows of pyte Chars
    cursor: tuple[int, int] | None            # (row, col) while the cursor is shown

    def text(self) -> str:
        return "\n".join("".join(c.data for c in row) for row in self.cells)


def replay(cast: Path) -> tuple[list[Frame], list[tuple[float, str]]]:
    """Every screen state the recording passed through, plus its (time, marker) list."""
    with cast.open(encoding="utf-8") as handle:
        header = json.loads(next(handle))
        width, height = header["width"], header["height"]
        screen = _Screen(width, height)
        stream = pyte.Stream(screen)
        frames: list[Frame] = []
        markers: list[tuple[float, str]] = []
        for line in handle:
            t, kind, data = json.loads(line)
            if kind == "m":
                markers.append((t, data))
                continue
            if kind != "o":
                continue
            stream.feed(data)
            cells = [[screen.buffer[r][c] for c in range(width)] for r in range(height)]
            cursor = None if screen.cursor.hidden else (screen.cursor.y, screen.cursor.x)
            frames.append(Frame(t, cells, cursor))
    for frame in frames:
        visible = frame.text().lower()
        if "http://" in visible or "https://" in visible or "remote control" in visible:
            raise ValueError(f"the frame at {frame.t:.2f}s shows a URL or session information")
    return frames, markers


def _first(paths) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def _blend(color: str, toward: str, amount: float) -> str:
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(toward[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02X}" for x, y in zip(a, b))


def _hex_rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _colour(name: str, default: str) -> str:
    if name == "default":
        return default
    if name in NAMED:
        return NAMED[name]
    if re.fullmatch(r"[0-9a-fA-F]{6}", name):
        return "#" + name.upper()
    return default


# Line-drawing characters a terminal draws itself rather than from the font,
# so that box edges meet: the directions each one reaches, and whether its
# corner is rounded.
_BOX = {
    "─": ("lr", False), "│": ("ud", False),
    "┌": ("rd", False), "┐": ("ld", False), "└": ("ru", False), "┘": ("lu", False),
    "├": ("udr", False), "┤": ("udl", False), "┬": ("lrd", False), "┴": ("lru", False),
    "┼": ("lrud", False),
    "╭": ("rd", True), "╮": ("ld", True), "╰": ("ru", True), "╯": ("lu", True),
}


# Quadrant blocks as (top-left, top-right, bottom-left, bottom-right) — drawn,
# not taken from the font, so the mascot's edges meet exactly.
_QUADRANTS = {
    "▖": (0, 0, 1, 0), "▗": (0, 0, 0, 1), "▘": (1, 0, 0, 0), "▝": (0, 1, 0, 0),
    "▙": (1, 0, 1, 1), "▚": (1, 0, 0, 1), "▛": (1, 1, 1, 0), "▜": (1, 1, 0, 1),
    "▞": (0, 1, 1, 0), "▟": (0, 1, 1, 1),
}


class Painter:
    def __init__(self, cell_w: int = 10, cell_h: int = 20, font_size: int = 15,
                 background: str = BACKGROUND, foreground: str = FOREGROUND):
        self.cell_w, self.cell_h = cell_w, cell_h
        self.background, self.foreground = background, foreground
        mono = _first(MONO)
        if mono is None:
            raise FileNotFoundError("Install Menlo or DejaVu Sans Mono to render this capture")
        self.regular = ImageFont.truetype(str(mono), font_size)
        bold = _first(MONO_BOLD)
        self.bold = (ImageFont.truetype(str(mono), font_size, index=1) if mono.suffix == ".ttc"
                     else ImageFont.truetype(str(bold or mono), font_size))
        self.fallbacks = [ImageFont.truetype(str(p), font_size) for p in FALLBACK if p.exists()]
        ascent, descent = self.regular.getmetrics()
        self.baseline = (cell_h - (ascent + descent)) // 2 + ascent
        self.weight = max(1, round(cell_h / 18))
        self._covered: dict[tuple[int, str], bool] = {}

    def _has(self, font, ch: str) -> bool:
        key = (id(font), ch)
        if key not in self._covered:
            missing = font.getmask("\U0010FFFD")      # a private-use point: the .notdef box
            mask = font.getmask(ch)
            self._covered[key] = not (mask.size == missing.size and bytes(mask) == bytes(missing))
        return self._covered[key]

    def _font(self, text: str, bold: bool):
        primary = self.bold if bold else self.regular
        for font in (primary, *self.fallbacks):
            if all(self._has(font, ch) for ch in text if ord(ch) > 126):
                return font
        return primary

    def _shape(self, draw, ch: str, x: int, y: int, colour: str) -> bool:
        w, h, line = self.cell_w, self.cell_h, self.weight
        code = ord(ch)
        if 0x2581 <= code <= 0x2588:                       # lower n/8 blocks, full block
            top = y + h - round(h * (code - 0x2580) / 8)
            draw.rectangle([x, top, x + w - 1, y + h - 1], fill=colour)
            return True
        if ch == "▀":
            draw.rectangle([x, y, x + w - 1, y + h // 2 - 1], fill=colour)
            return True
        if ch in _QUADRANTS:                               # ▖▗▘▙▚▛▜▝▞▟: 2×2 sub-cells
            for i, on in enumerate(_QUADRANTS[ch]):
                if on:
                    left, top = x + (w // 2) * (i % 2), y + (h // 2) * (i // 2)
                    right = x + w - 1 if i % 2 else x + w // 2 - 1
                    bottom = y + h - 1 if i // 2 else y + h // 2 - 1
                    draw.rectangle([left, top, right, bottom], fill=colour)
            return True
        if ch in "▌▐":
            left = x if ch == "▌" else x + w // 2
            draw.rectangle([left, y, left + w // 2 - 1, y + h - 1], fill=colour)
            return True
        if 0x2801 <= code <= 0x28FF:                       # braille (spinners): bit n is dot n+1
            r = max(1, round(w * 0.11))
            for bit, (col, row) in enumerate([(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3)]):
                if code - 0x2800 & (1 << bit):
                    cx = x + round(w * (0.3 + 0.4 * col))
                    cy = y + round(h * (0.2 + 0.2 * row))
                    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
            return True
        if ch in "⏺●":                                     # status dot; most monospace fonts lack ⏺
            d = round(w * 0.62)
            left, top = x + (w - d) // 2, y + (h - d) // 2
            draw.ellipse([left, top, left + d - 1, top + d - 1], fill=colour)
            return True
        if ch == "▪":
            side = round(w * 0.55)
            left, top = x + (w - side) // 2, y + (h - side) // 2
            draw.rectangle([left, top, left + side - 1, top + side - 1], fill=colour)
            return True
        if ch == "▬":
            bar_w, bar_h = round(w * 0.9), max(2, round(h * 0.28))
            left, top = x + (w - bar_w) // 2, y + (h - bar_h) // 2
            draw.rectangle([left, top, left + bar_w - 1, top + bar_h - 1], fill=colour)
            return True
        if ch not in _BOX:
            return False
        reach, rounded = _BOX[ch]
        cx, cy = x + w // 2, y + h // 2
        half = line // 2
        if rounded:
            r = w // 2
            (dx, dy), (start, end) = {
                "rd": ((1, 1), (180, 270)), "ld": ((-1, 1), (270, 360)),
                "ru": ((1, -1), (90, 180)), "lu": ((-1, -1), (0, 90)),
            }[reach]
            ox, oy = cx + dx * r, cy + dy * r
            draw.arc([ox - r, oy - r, ox + r, oy + r], start, end, fill=colour, width=line)
            horizontal_from = cx + dx * r
            draw.rectangle([min(horizontal_from, x + w * (dx > 0)), cy - half,
                            max(horizontal_from, x + w * (dx > 0)), cy - half + line - 1], fill=colour)
            vertical_from = cy + dy * r
            edge = y + h if dy > 0 else y
            draw.rectangle([cx - half, min(vertical_from, edge), cx - half + line - 1,
                            max(vertical_from, edge)], fill=colour)
            return True
        if "l" in reach:
            draw.rectangle([x, cy - half, cx, cy - half + line - 1], fill=colour)
        if "r" in reach:
            draw.rectangle([cx, cy - half, x + w - 1, cy - half + line - 1], fill=colour)
        if "u" in reach:
            draw.rectangle([cx - half, y, cx - half + line - 1, cy], fill=colour)
        if "d" in reach:
            draw.rectangle([cx - half, cy, cx - half + line - 1, y + h - 1], fill=colour)
        return True

    def paint(self, frame: Frame, rows: range | None = None, cursor: bool = True) -> Image.Image:
        rows = rows if rows is not None else range(len(frame.cells))
        columns = len(frame.cells[0])
        image = Image.new("RGB", (columns * self.cell_w, len(rows) * self.cell_h), self.background)
        draw = ImageDraw.Draw(image)
        for out_row, row in enumerate(rows):
            y = out_row * self.cell_h
            for col, cell in enumerate(frame.cells[row]):
                x = col * self.cell_w
                fg = _colour(cell.fg, self.foreground)
                bg = _colour(cell.bg, self.background)
                if cell.reverse:
                    fg, bg = bg, fg
                if cell.italics:                           # faint (see _Screen)
                    fg = _blend(fg, bg, 0.42)
                span = 2 if cell.data and wcwidth(cell.data[0]) == 2 else 1
                if bg != self.background:
                    draw.rectangle([x, y, x + self.cell_w * span - 1, y + self.cell_h - 1], fill=bg)
                if not cell.data.strip():
                    continue
                if len(cell.data) == 1 and self._shape(draw, cell.data, x, y, fg):
                    continue
                base, marks = cell.data[0], cell.data[1:]
                draw.text((x, y + self.baseline), base, font=self._font(base, cell.bold), fill=fg, anchor="ls")
                for mark in marks:                         # combining marks (the bar in d̄), centred
                    font = self._font(mark, cell.bold)
                    left, _, right, _ = font.getbbox(mark, anchor="ls")
                    draw.text((x + self.cell_w * span / 2 - (left + right) / 2, y + self.baseline),
                              mark, font=font, fill=fg, anchor="ls")
        if cursor and frame.cursor and frame.cursor[0] in rows:
            row, col = frame.cursor
            x, y = col * self.cell_w, (row - rows.start) * self.cell_h
            draw.rectangle([x, y + 2, x + max(2, self.weight * 2) - 1, y + self.cell_h - 3],
                           fill=self.foreground)
        return image


_SPINNER = re.compile(r"[\u2800-\u28ff]")


def _spinner_only(previous: Frame, frame: Frame) -> bool:
    """The only rows that changed are rows with a braille spinner on them."""
    changed = [a != b for a, b in zip(previous.text().split("\n"), frame.text().split("\n"))]
    rows = [r for r, differs in enumerate(changed) if differs]
    return bool(rows) and all(_SPINNER.search("".join(c.data for c in frame.cells[r])) for r in rows)


def timeline(frames: list[Frame], markers: list[tuple[float, str]], *, max_gap: float = 0.8,
             spinner_budget: float = 0.9, approve_hold: float = 1.4, result_hold: float = 2.8,
             first_hold: float = 1.4, final_hold: float = 4.0, steps: slice = slice(None),
             fit: float | None = None) -> list[tuple[Frame, float]]:
    """(frame, seconds on screen) from the first chosen step to the end of the last one.

    Real time, except: gaps over ``max_gap`` are cut to it; a stretch where
    only a spinner moves gets ``spinner_budget`` seconds in all; the frame on
    screen when an approval menu was answered, and when each step ended, is
    held. ``fit`` then scales the unheld time so the whole lasts that long.
    """
    starts = [t for t, name in markers if name.startswith("start ")][steps]
    ends = [t for t, name in markers if name.startswith("end ")][steps]
    if not starts or len(starts) != len(ends):
        raise ValueError("the cast has no complete start/end markers; re-record it")
    holds = {t: approve_hold for t, name in markers if name == "approve" and starts[0] <= t <= ends[-1]}
    holds.update({t: result_hold for t in ends})
    shown = [f for f in frames if f.t <= ends[-1]]
    first = max(i for i, f in enumerate(shown) if f.t <= starts[0])
    shown = shown[first:]

    out: list[list] = []                  # [frame, seconds, held]
    spinner_run: list[int] = []

    def close_spinner_run() -> None:
        if len(spinner_run) > 1:
            total = sum(out[i][1] for i in spinner_run)
            if total > spinner_budget:
                for i in spinner_run:
                    out[i][1] *= spinner_budget / total
        spinner_run.clear()

    for index, frame in enumerate(shown):
        following = shown[index + 1].t if index + 1 < len(shown) else frame.t
        seconds = min(max(following - frame.t, 0.02), max_gap)
        held = False
        if index == 0:
            seconds, held = first_hold, True
        for t, hold in holds.items():
            if frame.t <= t < following or (index == len(shown) - 1 and t >= frame.t):
                seconds, held = max(seconds, hold), True
        if index == len(shown) - 1:
            seconds, held = final_hold, True
        if out and not held and _spinner_only(out[-1][0], frame):
            out.append([frame, seconds, False])
            spinner_run.append(len(out) - 1)
            continue
        close_spinner_run()
        if out and not held and out[-1][0].text() == frame.text() and out[-1][0].cursor == frame.cursor:
            out[-1][1] = out[-1][1] + seconds if out[-1][2] else min(out[-1][1] + seconds, max_gap)
            continue
        out.append([frame, seconds, held])
    close_spinner_run()

    if fit:
        held_time = sum(s for _, s, h in out if h)
        free_time = sum(s for _, s, h in out if not h)
        if free_time > 0 and held_time < fit:
            scale = (fit - held_time) / free_time
            for item in out:
                if not item[2]:
                    item[1] *= scale
        elif held_time >= fit:
            scale = fit / (held_time + free_time)
            for item in out:
                item[1] *= scale
    return [(frame, seconds) for frame, seconds, _ in out]


def write_gif(timeline: list[tuple[Frame, float]], painter: Painter, gif: Path, png: Path) -> None:
    pad = painter.cell_w * 2
    images = []
    for frame, _ in timeline:
        body = painter.paint(frame)
        canvas = Image.new("RGB", (body.width + 2 * pad, body.height + 2 * pad), painter.background)
        canvas.paste(body, (pad, pad))
        images.append(canvas)
    # One palette for every frame, so colours do not shimmer between frames —
    # built from the first, middle and last frames together: the last alone
    # has usually scrolled past the banner, and the robot's orange was lost.
    samples = [images[0], images[len(images) // 2], images[-1]]
    sheet = Image.new("RGB", (samples[0].width, sum(i.height for i in samples)))
    for n, sample in enumerate(samples):
        sheet.paste(sample, (0, n * samples[0].height))
    # Every colour the CLI itself sent gets an exact slot; median cut alone
    # folded small areas — the robot's small orange details — into larger neighbours.
    exact = {_hex_rgb(painter.background), _hex_rgb(painter.foreground)}
    for frame, _ in timeline[::max(1, len(timeline) // 40)]:
        for row in frame.cells:
            for cell in row:
                for name in (cell.fg, cell.bg):
                    colour = _colour(name, "")
                    if colour:
                        exact.add(_hex_rgb(colour))
    exact = sorted(exact)[:200]
    fill = sheet.quantize(colors=256 - len(exact), method=Image.Quantize.MEDIANCUT).getpalette()[:3 * (256 - len(exact))]
    palette = Image.new("P", (1, 1))
    palette.putpalette([v for rgb in exact for v in rgb] + fill)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    durations = [max(20, round(seconds * 100) * 10) for _, seconds in timeline]
    gif.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   optimize=True, disposal=1)
    images[-1].save(png)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cast", type=Path, required=True, help="written by scripts/record_demo.py")
    parser.add_argument("--gif", type=Path, required=True)
    parser.add_argument("--png", type=Path, help="the last frame as a still (default: next to the GIF)")
    parser.add_argument("--fit", type=float, help="scale the unheld time so the GIF lasts this many seconds")
    args = parser.parse_args()
    frames, markers = replay(args.cast)
    shots = timeline(frames, markers, fit=args.fit)
    write_gif(shots, Painter(), args.gif, args.png or args.gif.with_suffix(".png"))
    print(f"{args.gif}  ({len(shots)} frames, {sum(s for _, s in shots):.1f}s)")
    print(args.png or args.gif.with_suffix('.png'))


if __name__ == "__main__":
    main()
