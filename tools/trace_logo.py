#!/usr/bin/env python3
"""Vectorize the raster logo into source/logo.svg (true infinite-scale vector).

The logo is flat cartoon line-art (~4 colors), so we:
  1. Snap every pixel to the exact canonical brand palette (no color drift,
     and anti-aliasing fringes collapse into clean regions), then
  2. Trace the flat image to colored SVG paths with vtracer.

Requires the `vtracer` binary (cargo install vtracer).
Run:  python3 tools/trace_logo.py
"""
import subprocess
import tempfile
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"

# Exact canonical brand palette — see README.
PALETTE = [
    0xEF, 0xDE, 0xCC,   # canvas cream (background)
    0xDC, 0x31, 0x27,   # polo red
    0xF5, 0xB0, 0x79,   # skin tan
    0x0A, 0x0A, 0x08,   # ink (outlines + hair)
]


def main():
    im = Image.open(SRC / "logo.png").convert("RGB")
    pal = Image.new("P", (1, 1))
    # Pad to 256 entries by repeating ink, so stray pixels never snap to a
    # color outside the palette (e.g. pure black).
    pal.putpalette(PALETTE + PALETTE[9:12] * (256 - 4))
    flat = im.quantize(palette=pal, dither=Image.NONE).convert("RGB")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        flat.save(tf.name)
        subprocess.run(
            ["vtracer", "--input", tf.name, "--output", str(SRC / "logo.svg"),
             "--colormode", "color", "--mode", "spline",
             "--filter_speckle", "8", "--color_precision", "8",
             "--corner_threshold", "60", "--segment_length", "4",
             "--splice_threshold", "45"],
            check=True,
        )
    print("wrote", SRC / "logo.svg")


if __name__ == "__main__":
    main()
