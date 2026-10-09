"""Compile the original mascot into a small, dependency-free terminal grid.

Pixels and proportions come from ui/assets/aria-robot.png. This is a fallback
for terminals without inline PNG support; it does not replace the artwork.
"""
from pathlib import Path
import base64
import hashlib
import zlib

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/aria_code/ui/assets/aria-robot.png"
TARGET = ROOT / "src/aria_code/ui/robot_pixels.py"
BOUNDS = (389, 417, 865, 854)
WIDTH = 20


def compile_pixels():
    with Image.open(SOURCE) as source:
        height = round(WIDTH * (BOUNDS[3] - BOUNDS[1]) / (BOUNDS[2] - BOUNDS[0]))
        height += height % 2
        image = source.convert("RGB").crop(BOUNDS).resize((WIDTH, height), Image.Resampling.BOX)
        return height, base64.b85encode(zlib.compress(image.tobytes())).decode("ascii")


if __name__ == "__main__":
    height, pixels = compile_pixels()
    TARGET.write_text(
        '"""Generated from the original PNG; regenerate with scripts/generate_robot_pixels.py."""\n'
        f"SOURCE_SHA256 = {hashlib.sha256(SOURCE.read_bytes()).hexdigest()!r}\n"
        f"BOUNDS = {BOUNDS!r}\nWIDTH = {WIDTH}\nHEIGHT = {height}\nPIXELS = {pixels!r}\n",
        encoding="utf-8",
    )
