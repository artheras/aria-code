"""Terminal image rendering — show a real PNG in the terminal, with fallback.

Three layered techniques, best-first (exactly what chafa/viu/timg do):

  1. iTerm2 inline images  (OSC 1337)         — iTerm2, WezTerm, ghostty
  2. Kitty graphics protocol                   — kitty, ghostty
  3. Half-block + truecolor downscale (``▀``)   — any 24-bit colour terminal

Layer 3 is the universal floor: it resizes the image and prints one ``▀`` per
cell, packing two vertical pixels into each character (foreground = top pixel,
background = bottom pixel). Pixel-art sources render almost perfectly this way.

Used for the startup mascot banner, and reusable for ``/vision`` previews,
``/screenshot`` echoes, and inline quant charts.

CLI:  python3 -m ui.image_render <path> [width] [--half|--iterm|--kitty]
"""

from __future__ import annotations

import base64
import os
import sys

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:  # pragma: no cover - PIL is a hard dep for this module
    _HAS_PIL = False


# ── Terminal capability detection ────────────────────────────────────────────
def _in_tmux() -> bool:
    # Image protocols need passthrough wrapping inside tmux; play safe and fall
    # back to half-blocks rather than spraying escape bytes the pane won't eat.
    return bool(os.environ.get("TMUX"))


def supports_iterm() -> bool:
    if _in_tmux():
        return False
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "ghostty"):
        return True
    return bool(os.environ.get("ITERM_SESSION_ID"))


def supports_kitty() -> bool:
    if _in_tmux():
        return False
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM_PROGRAM") == "ghostty":
        return True
    return os.environ.get("TERM") == "xterm-kitty"


def supports_truecolor() -> bool:
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return True
    # Most modern terminals are truecolor even without advertising it; only the
    # genuinely ancient (TERM=dumb / linux console) are not.
    return os.environ.get("TERM", "") not in ("", "dumb", "linux")


def best_method() -> str:
    """Return the best available render method for the current terminal."""
    if supports_kitty():
        return "kitty"
    if supports_iterm():
        return "iterm"
    if supports_truecolor():
        return "half"
    return "none"


# ── Protocol emitters ────────────────────────────────────────────────────────
def _iterm_sequence(png_bytes: bytes, cells_wide: int, cells_high: int | None = None) -> str:
    """iTerm2 OSC 1337 inline image. Width in character cells, height auto."""
    b64 = base64.b64encode(png_bytes).decode()
    return (
        f"\x1b]1337;File=inline=1;width={cells_wide};"
        + (f"height={cells_high};" if cells_high else "")
        + f"preserveAspectRatio=1:{b64}\x07"
    )


def _kitty_sequence(png_bytes: bytes, cells_wide: int, cells_high: int | None = None) -> str:
    """Kitty graphics protocol, PNG payload (f=100), chunked at 4096 bytes."""
    b64 = base64.b64encode(png_bytes).decode()
    chunk = 4096
    parts: list[str] = []
    i = 0
    first = True
    while i < len(b64):
        piece = b64[i : i + chunk]
        i += chunk
        more = 1 if i < len(b64) else 0
        if first:
            # a=T transmit+display, f=100 PNG, c=columns to scale into
            ctrl = f"a=T,f=100,c={cells_wide},m={more}"
            if cells_high:
                ctrl += f",r={cells_high},C=1"
            first = False
        else:
            ctrl = f"m={more}"
        parts.append(f"\x1b_G{ctrl};{piece}\x1b\\")
    return "".join(parts)


# ── Half-block fallback (universal) ──────────────────────────────────────────
_UPPER = "▀"  # ▀ upper half block


def autocrop(img: "Image.Image", tol: int = 18, pad: int = 1) -> "Image.Image":
    """Trim a uniform border (e.g. the robot's black canvas) so the subject
    fills the frame. Background colour is sampled from the top-left pixel;
    pixels within ``tol`` of it are treated as border. Falls back to the
    original image if nothing distinct is found.
    """
    from PIL import ImageChops, Image as _I

    rgb = img.convert("RGB")
    bg = _I.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg).convert("L")
    box = diff.point(lambda p: 255 if p > tol else 0).getbbox()
    if not box:
        return img
    l, t, r, b = box
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(img.width, r + pad), min(img.height, b + pad)
    return img.crop((l, t, r, b))


def half_block_render(img: "Image.Image", cells_wide: int = 36) -> str:
    """Render a PIL image as ``▀`` half-blocks with 24-bit colour.

    Each character is 1 pixel wide and 2 pixels tall, so a square source maps to
    ``cells_wide`` columns × ``cells_wide // 2`` rows. Foreground paints the top
    pixel, background the bottom one.
    """
    img = img.convert("RGB")
    w, h = img.size
    # Sample at cells_wide × (2 px per row). Rows chosen to preserve aspect once
    # the 1:2 cell shape is accounted for, so on-screen proportions match.
    px_w = max(1, cells_wide)
    px_h = max(2, round(px_w * h / w))
    if px_h % 2:
        px_h += 1  # even rows so every cell has a top+bottom pixel
    small = img.resize((px_w, px_h), Image.LANCZOS)
    px = small.load()

    lines: list[str] = []
    for row in range(0, px_h, 2):
        cells: list[str] = []
        for x in range(px_w):
            tr, tg, tb = px[x, row]
            br, bg, bb = px[x, row + 1]
            cells.append(
                f"\x1b[38;2;{tr};{tg};{tb};48;2;{br};{bg};{bb}m{_UPPER}"
            )
        cells.append("\x1b[0m")
        lines.append("".join(cells))
    return "\n".join(lines)


# ── Public entry point ───────────────────────────────────────────────────────
def render_image(
    path: str,
    cells_wide: int = 36,
    method: str | None = None,
    crop: bool | tuple[int, int, int, int] = True,
    cells_high: int | None = None,
) -> str | None:
    """Return a printable string that draws ``path`` in the terminal.

    ``method`` forces one of ``iterm`` / ``kitty`` / ``half``; default auto-detects.
    ``crop`` trims a uniform border first so the subject fills the frame.
    Returns ``None`` if the image can't be loaded or no method is usable.
    """
    chosen = method or best_method()

    # Protocol path (iTerm2/Kitty) only needs the raw PNG bytes — the terminal
    # scales them. PIL is optional here: with it we autocrop so the subject
    # fills the frame; without it we send the file as-is. This means the real
    # image still shows even when Pillow isn't installed in the venv.
    if chosen in ("iterm", "kitty"):
        try:
            if _HAS_PIL:
                import io

                with Image.open(path) as source:
                    img = source.crop(crop) if isinstance(crop, tuple) else (autocrop(source) if crop else source)
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="PNG")
                data = buf.getvalue()
            else:
                with open(path, "rb") as fh:
                    data = fh.read()
            if chosen == "iterm":
                return _iterm_sequence(data, cells_wide, cells_high)
            return _kitty_sequence(data, cells_wide, cells_high)
        except Exception:
            chosen = "half"  # fall through to the universal path

    # Half-block fallback needs PIL to resize.
    if chosen == "half" and _HAS_PIL:
        try:
            with Image.open(path) as source:
                img = source.crop(crop) if isinstance(crop, tuple) else (autocrop(source) if crop else source)
                return half_block_render(img, cells_wide)
        except Exception:
            return None
    return None


def _main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if not args:
        print("usage: python3 -m ui.image_render <image> [width] [--half|--iterm|--kitty]")
        return 2
    path = args[0]
    width = int(args[1]) if len(args) > 1 and args[1].isdigit() else 36
    method = None
    if "--half" in flags:
        method = "half"
    elif "--iterm" in flags:
        method = "iterm"
    elif "--kitty" in flags:
        method = "kitty"
    out = render_image(path, width, method)
    if out is None:
        print(f"(cannot render {path}; method={method or best_method()})")
        return 1
    sys.stdout.write(out + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
