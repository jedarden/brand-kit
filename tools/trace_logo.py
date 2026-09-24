#!/usr/bin/env python3
"""Replace source/logo.svg by tracing the original source/logo.png raster.

source/logo.svg is authoritative for the opaque logo. source/logo.png is kept
as provenance, used directly for logo/logo-original.png, and may be used to
replace the vector after an explicit raster-to-vector trace.

The logo is flat cartoon line-art (~4 colors), so the script:
  1. Snaps every pixel to the exact canonical brand palette, then
  2. Traces the flat image to colored SVG paths with vtracer.

Before tracing, the current SVG must match source/logo.svg.sha256, the digest
written by the last successful trace. Use --force only to explicitly discard a
modified SVG and replace it from source/logo.png.

Requires the Pillow 12.1.1 PyPI wheel in `.venv` and the vtracer 0.6.5 CLI
installed with `cargo install vtracer@0.6.5`; the PyPI `vtracer` package is not
used.
Run:  .venv/bin/python tools/trace_logo.py
"""
import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "source"
LOGO_PNG = SRC / "logo.png"
LOGO_SVG = SRC / "logo.svg"
LOGO_SVG_SHA256 = SRC / "logo.svg.sha256"

# Exact canonical brand palette — see README.
PALETTE = [
    0xEF, 0xDE, 0xCC,   # canvas cream (background)
    0xDC, 0x31, 0x27,   # polo red
    0xF5, 0xB0, 0x79,   # skin tan
    0x0A, 0x0A, 0x08,   # ink (outlines + hair)
]


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_checksum():
    if not LOGO_SVG_SHA256.exists():
        raise SystemExit(
            "error: source/logo.svg.sha256 is missing; refusing to trace. "
            "Restore the checksum or rerun with --force if replacement is intentional."
        )
    value = LOGO_SVG_SHA256.read_text(encoding="utf-8").strip()
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise SystemExit(
            "error: source/logo.svg.sha256 is malformed; refusing to trace. "
            "Restore the checksum or rerun with --force if replacement is intentional."
        )
    return value.lower()


def guard_svg(force):
    if force:
        return
    expected = recorded_checksum()
    if LOGO_SVG.exists() and file_sha256(LOGO_SVG) != expected:
        raise SystemExit(
            "error: source/logo.svg differs from its last-traced checksum; "
            "refusing to overwrite it. Edit source/logo.svg directly and run "
            "build_assets.py, or rerun with --force if replacement from "
            "source/logo.png is intentional."
        )


def main(force=False):
    guard_svg(force)
    with Image.open(LOGO_PNG) as source:
        im = source.convert("RGB")
    pal = Image.new("P", (1, 1))
    # Pad to 256 entries by repeating ink, so stray pixels never snap to a
    # color outside the palette (e.g. pure black).
    pal.putpalette(PALETTE + PALETTE[9:12] * (256 - 4))
    flat = im.quantize(palette=pal, dither=Image.NONE).convert("RGB")

    with tempfile.TemporaryDirectory(prefix="trace-logo-", dir=SRC) as tmp:
        tmpdir = Path(tmp)
        quantized_png = tmpdir / "logo-quantized.png"
        traced_svg = tmpdir / "logo.svg"
        checksum = tmpdir / LOGO_SVG_SHA256.name
        flat.save(quantized_png)
        subprocess.run(
            ["vtracer", "--input", str(quantized_png), "--output", str(traced_svg),
             "--colormode", "color", "--mode", "spline",
             "--filter_speckle", "8", "--color_precision", "8",
             "--corner_threshold", "60", "--segment_length", "4",
             "--splice_threshold", "45"],
            check=True,
        )
        traced_checksum = file_sha256(traced_svg)
        checksum.write_text(f"{traced_checksum}\n", encoding="utf-8")
        guard_svg(force)
        traced_svg.replace(LOGO_SVG)
        checksum.replace(LOGO_SVG_SHA256)
    print("wrote", LOGO_SVG)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace source/logo.svg even if it differs from its last-traced checksum",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(force=args.force)
