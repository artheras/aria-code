"""Compile the supplied mascot into terminal pixels, without a runtime image library.

Run with Pillow installed after changing ui/assets/aria-robot.png. Coordinates
describe the silhouette in that original artwork; the face and every colour
are sampled from the image, rather than recreated with eye glyphs.
"""
from pathlib import Path
import base64
import zlib

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/aria_code/ui/assets/aria-robot.png"
TARGET = ROOT / "src/aria_code/ui/robot_pixels.py"
BOUNDS = (389, 417, 865, 854)
REGIONS = (
    (444, 417, 809, 445), (418, 444, 837, 785),
    (389, 552, 419, 641), (836, 552, 865, 641),
    (444, 782, 495, 854), (553, 782, 602, 854),
    (652, 782, 701, 854), (760, 782, 810, 854),
)


def compile_pixels(image, width):
    height = round(width * (BOUNDS[3] - BOUNDS[1]) / (BOUNDS[2] - BOUNDS[0]))
    height += height % 2
    resized = image.crop(BOUNDS).resize((width, height), Image.Resampling.BOX)
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            sx = BOUNDS[0] + (x + .5) * (BOUNDS[2] - BOUNDS[0]) / width
            sy = BOUNDS[1] + (y + .5) * (BOUNDS[3] - BOUNDS[1]) / height
            visible = any(l <= sx < r and t <= sy < b for l, t, r, b in REGIONS)
            pixels.extend((*resized.getpixel((x, y)), 255 if visible else 0))
    return height, base64.b85encode(zlib.compress(pixels)).decode("ascii")


if __name__ == "__main__":
    image = Image.open(SOURCE).convert("RGB")
    data = {width: compile_pixels(image, width) for width in (20, 28)}
    TARGET.write_text(
        '"""Generated from assets/aria-robot.png; see scripts/generate_robot_pixels.py."""\n'
        f"PIXELS = {data!r}\n", encoding="utf-8",
    )
