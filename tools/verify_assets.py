#!/usr/bin/env python3
"""Verify that committed assets match every testable claim README.md makes.

Six classes of README-vs-repo drift are checked:
  1. Shipped PNG dimensions vs the README per-platform table.
  2. Presence of every repo path README names (plus absence of the file
     it documents as removed).
  3. favicon.ico is multi-resolution 16-256 and every contained frame
     decodes.
  4. The transparent variants have full alpha channels, and the
     transparent SVG master has its background removed relative to the
     opaque one.
  5. The names and hexes in palette.json match the README palette table.
  6. The generated asset inventory is exactly the 37 documented derived
     assets plus palette.json.

Run: .venv/bin/python tools/verify_assets.py
Exits 1 on any mismatch, 0 if all checks pass.
"""
import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PALETTE_RELPATH = "palette.json"

# Expected dimensions mirror the README.md table
# Format: {path: (expected_width, expected_height)}
EXPECTED_DIMENSIONS = {
    # Avatars (profile pictures)
    "avatars/x-400.png": (400, 400),
    "avatars/linkedin-400.png": (400, 400),
    "avatars/github-460.png": (460, 460),
    "avatars/instagram-320.png": (320, 320),
    "avatars/threads-320.png": (320, 320),
    "avatars/facebook-320.png": (320, 320),
    "avatars/youtube-800.png": (800, 800),
    "avatars/tiktok-200.png": (200, 200),
    "avatars/mastodon-400.png": (400, 400),
    "avatars/bluesky-400.png": (400, 400),
    "avatars/discord-512.png": (512, 512),

    # Banners / covers
    "banners/x-header-1500x500.png": (1500, 500),
    "banners/linkedin-personal-1584x396.png": (1584, 396),
    "banners/linkedin-company-1128x191.png": (1128, 191),
    "banners/facebook-cover-851x315.png": (851, 315),
    "banners/facebook-cover-2x-1702x630.png": (1702, 630),
    "banners/youtube-banner-2560x1440.png": (2560, 1440),
    "banners/discord-banner-960x540.png": (960, 540),
    "banners/github-social-1280x640.png": (1280, 640),
    "banners/open-graph-1200x630.png": (1200, 630),
    "banners/twitter-card-1200x628.png": (1200, 628),

    # Favicons
    "favicon/favicon-16.png": (16, 16),
    "favicon/favicon-32.png": (32, 32),
    "favicon/favicon-48.png": (48, 48),
    "favicon/favicon-192.png": (192, 192),
    "favicon/favicon-512.png": (512, 512),
    "favicon/apple-touch-icon-180.png": (180, 180),

    # Logo masters
    "logo/logo-256.png": (256, 256),
    "logo/logo-512.png": (512, 512),
    "logo/logo-1024.png": (1024, 1024),
    "logo/logo-original.png": (640, 640),
}

GENERATED_ASSET_DIRECTORIES = ("avatars", "banners", "favicon", "logo")
EXPECTED_ASSETS = frozenset({
    "avatars/x-400.png",
    "avatars/linkedin-400.png",
    "avatars/github-460.png",
    "avatars/instagram-320.png",
    "avatars/threads-320.png",
    "avatars/facebook-320.png",
    "avatars/youtube-800.png",
    "avatars/tiktok-200.png",
    "avatars/mastodon-400.png",
    "avatars/bluesky-400.png",
    "avatars/discord-512.png",
    "banners/x-header-1500x500.png",
    "banners/linkedin-personal-1584x396.png",
    "banners/linkedin-company-1128x191.png",
    "banners/facebook-cover-851x315.png",
    "banners/facebook-cover-2x-1702x630.png",
    "banners/youtube-banner-2560x1440.png",
    "banners/discord-banner-960x540.png",
    "banners/github-social-1280x640.png",
    "banners/open-graph-1200x630.png",
    "banners/twitter-card-1200x628.png",
    "favicon/favicon-16.png",
    "favicon/favicon-32.png",
    "favicon/favicon-48.png",
    "favicon/favicon-192.png",
    "favicon/favicon-512.png",
    "favicon/apple-touch-icon-180.png",
    "favicon/favicon.ico",
    "logo/logo-256.png",
    "logo/logo-512.png",
    "logo/logo-1024.png",
    "logo/logo-original.png",
    "logo/logo-256-transparent.png",
    "logo/logo-512-transparent.png",
    "logo/logo-1024-transparent.png",
    "logo/logo.svg",
    "logo/logo-transparent.svg",
})
EXPECTED_INVENTORY = EXPECTED_ASSETS | {PALETTE_RELPATH}

# Every repo path README.md names that EXPECTED_DIMENSIONS does not
# already open (a PNG listed there reports MISSING when absent). The
# avatars, banners, sized favicons and logo masters are therefore covered
# by the dimension table above; these are the rest. README's pointers to
# jedarden.com/public/brand/ are external — consumer_sync.py owns those.
EXPECTED_PRESENT = [
    # Sources (README table)
    "source/logo.svg",
    "source/logo.png",
    "source/hero.png",

    # Transparent variants (README: "Transparent variants")
    "logo/logo-256-transparent.png",
    "logo/logo-512-transparent.png",
    "logo/logo-1024-transparent.png",
    "logo/logo-transparent.svg",

    # Tools and docs README tells the reader to run / read
    "tools/trace_logo.py",
    "tools/build_assets.py",
    "tools/consumer_sync.py",
    "docs/notes/post-tag-consumer-update.md",
]

# README documents this file as deliberately removed ("2 MB of dead weight
# per clone"), with git-history recovery instructions should the hero ever
# need replacing. Its presence would contradict the README.
EXPECTED_ABSENT = [
    "source/hero-alt.png",
]

# Transparent PNG renders (README: "have full alpha channels") and the SVG
# masters compared for the removed background (README: transparent exists so
# "the Canvas Cream background should not be baked in").
TRANSPARENT_PNGS = [
    "logo/logo-256-transparent.png",
    "logo/logo-512-transparent.png",
    "logo/logo-1024-transparent.png",
]
OPAQUE_MASTER_SVG = "logo/logo.svg"
TRANSPARENT_MASTER_SVG = "logo/logo-transparent.svg"
FILL_RE = re.compile(r'fill="(#[0-9A-Fa-f]{3,8})"')

# README: favicon.ico is "multi-res 16–256"
ICO_RELPATH = "favicon/favicon.ico"
ICO_MIN_SIZE, ICO_MAX_SIZE = 16, 256


def verify_dimensions():
    """Check all PNG dimensions against expected values.

    Returns:
        list of tuples: (path, expected, actual, status_message)
    """
    results = []
    all_match = True

    for relpath, (expected_w, expected_h) in EXPECTED_DIMENSIONS.items():
        full_path = ROOT / relpath

        if not full_path.exists():
            results.append((relpath, f"{expected_w}×{expected_h}", "MISSING", "file not found"))
            all_match = False
            continue

        try:
            with Image.open(full_path) as img:
                actual_w, actual_h = img.size

                if (actual_w, actual_h) == (expected_w, expected_h):
                    results.append((relpath, f"{expected_w}×{expected_h}", f"{actual_w}×{actual_h}", "✓"))
                else:
                    results.append((relpath, f"{expected_w}×{expected_h}", f"{actual_w}×{actual_h}", "✗ MISMATCH"))
                    all_match = False
        except Exception as e:
            results.append((relpath, f"{expected_w}×{expected_h}", "ERROR", str(e)))
            all_match = False

    return results, all_match


def verify_inventory():
    """Check the exact generated asset inventory, including palette.json."""
    expected = set(EXPECTED_INVENTORY)
    actual = set()
    errors = []

    for directory in GENERATED_ASSET_DIRECTORIES:
        root = ROOT / directory
        if not root.is_dir():
            continue
        try:
            actual.update(
                path.relative_to(ROOT).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
        except OSError as error:
            errors.append(f"{directory}: {error}")

    if (ROOT / PALETTE_RELPATH).is_file():
        actual.add(PALETTE_RELPATH)

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    rows = []

    if errors:
        rows.append(("asset inventory", "read generated files", "ERROR", "; ".join(errors)))

    for relpath in missing:
        rows.append((relpath, "expected generated file", "MISSING", "✗ MISSING"))

    for relpath in unexpected:
        rows.append((relpath, "no unexpected generated file", "UNEXPECTED", "✗ UNEXPECTED"))

    if not rows:
        rows.append((
            "asset inventory",
            "37 derived assets + palette.json",
            f"{len(actual)} files",
            "✓",
        ))

    return rows, not errors and not missing and not unexpected


def read_readme_palette():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.find("## Palette")
    if start == -1:
        return {}
    end = text.find("\n## ", start + len("## Palette"))
    section = text[start:] if end == -1 else text[start:end]
    palette = {}
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name, value = cells[0], cells[1]
        if re.fullmatch(r"`#[0-9A-Fa-f]{6}`", value):
            palette[name] = value[1:-1]
    return palette


def verify_palette():
    claim = "names and hexes match README palette table"
    path = ROOT / PALETTE_RELPATH
    if not path.exists():
        return [(PALETTE_RELPATH, claim, "MISSING", "file not found")], False

    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [(PALETTE_RELPATH, claim, "ERROR", str(e))], False

    try:
        expected = read_readme_palette()
    except OSError as e:
        return [(PALETTE_RELPATH, claim, "README ERROR", str(e))], False

    if not isinstance(actual, dict):
        return [(PALETTE_RELPATH, claim, type(actual).__name__, "✗ not an object")], False
    if not expected:
        return [(PALETTE_RELPATH, claim, "no README rows", "✗ README table missing")], False
    if actual == expected:
        return [(PALETTE_RELPATH, claim, f"{len(actual)} colors", "✓")], True

    expected_text = json.dumps(expected, ensure_ascii=False)
    actual_text = json.dumps(actual, ensure_ascii=False)
    return [(PALETTE_RELPATH, claim, actual_text, f"✗ MISMATCH: expected {expected_text}")], False


def verify_presence():
    """Every README-named path exists; documented-removed paths don't.

    Returns:
        list of tuples: (path, claim, actual, status_message)
    """
    rows = []
    all_match = True

    for relpath in EXPECTED_PRESENT:
        if (ROOT / relpath).exists():
            rows.append((relpath, "exists", "found", "✓"))
        else:
            rows.append((relpath, "exists", "MISSING", "README names this path"))
            all_match = False

    for relpath in EXPECTED_ABSENT:
        if (ROOT / relpath).exists():
            rows.append((relpath, "absent (documented removed)", "found", "✗ re-added"))
            all_match = False
        else:
            rows.append((relpath, "absent (documented removed)", "not found", "✓"))

    return rows, all_match


def ico_contained_sizes(path):
    """Resolutions actually stored inside an .ico container.

    Parsed from the ICONDIR/ICONDIRENTRY structures by hand rather than
    asked of Pillow: the plugin will also serve sizes it merely
    synthesized by resizing the largest frame, which would let a
    single-resolution ico pass as multi-res. A width/height byte of 0
    encodes 256.
    """
    blob = path.read_bytes()
    reserved, ico_type, count = struct.unpack("<HHH", blob[:6])
    if reserved != 0 or ico_type != 1 or count == 0:
        raise ValueError(f"not a valid .ico container (type={ico_type}, entries={count})")
    if len(blob) < 6 + 16 * count:
        raise ValueError("truncated .ico directory")
    return [
        (entry[0] or 256, entry[1] or 256)
        for entry in (blob[6 + 16 * i:22 + 16 * i] for i in range(count))
    ]


def verify_favicon_ico():
    """README: favicon.ico is "multi-res 16–256".

    Confirms the container holds more than one resolution, that 16 and 256
    are the endpoints with nothing outside that range, and that every
    contained frame decodes through Pillow.

    Returns:
        list of tuples: (path, expected, actual, status_message)
    """
    claim = f"multi-res {ICO_MIN_SIZE}-{ICO_MAX_SIZE}"
    path = ROOT / ICO_RELPATH
    if not path.exists():
        return [(ICO_RELPATH, claim, "MISSING", "file not found")], False

    rows = []
    all_match = True
    try:
        sizes = ico_contained_sizes(path)
        rows.append((
            f"{ICO_RELPATH} resolutions", claim,
            f"{len(sizes)}: {', '.join(f'{w}×{h}' for w, h in sizes)}",
            "✓" if len(sizes) > 1 else "✗ single-resolution",
        ))
        all_match &= len(sizes) > 1

        smallest, largest = min(sizes), max(sizes)
        endpoints_ok = (
            smallest == (ICO_MIN_SIZE, ICO_MIN_SIZE)
            and largest == (ICO_MAX_SIZE, ICO_MAX_SIZE)
        )
        rows.append((
            f"{ICO_RELPATH} endpoints", f"{ICO_MIN_SIZE} bottom, {ICO_MAX_SIZE} top",
            f"min {smallest[0]}, max {largest[0]}",
            "✓" if endpoints_ok else "✗ MISMATCH",
        ))
        all_match &= endpoints_ok

        in_range = all(
            ICO_MIN_SIZE <= w <= ICO_MAX_SIZE and ICO_MIN_SIZE <= h <= ICO_MAX_SIZE
            for w, h in sizes
        )
        rows.append((
            f"{ICO_RELPATH} range", f"all within {ICO_MIN_SIZE}-{ICO_MAX_SIZE}",
            "in range" if in_range else f"{[s for s in sizes if not (ICO_MIN_SIZE <= s[0] <= ICO_MAX_SIZE)]}",
            "✓" if in_range else "✗ out of range",
        ))
        all_match &= in_range

        with Image.open(path) as ico:
            for w, h in sizes:
                try:
                    ico.size = (w, h)
                    ico.load()
                    rows.append((f"{ICO_RELPATH} frame", f"{w}×{h} decodes",
                                 f"{ico.size[0]}×{ico.size[1]}", "✓"))
                except Exception as e:
                    rows.append((f"{ICO_RELPATH} frame", f"{w}×{h} decodes", "ERROR", str(e)))
                    all_match = False
    except Exception as e:
        rows.append((ICO_RELPATH, claim, "ERROR", str(e)))
        all_match = False

    return rows, all_match


def verify_transparency():
    """README: the transparent variants "have full alpha channels".

    Each PNG render must carry an alpha channel that actually spans fully
    transparent (background removed) and fully opaque (artwork intact)
    pixels — a transparent-labelled file that is uniformly opaque has
    silently lost the property the README promises. The transparent SVG
    master must be well-formed and must have dropped at least one fill
    colour relative to the opaque master: proof the background is not
    baked in.

    Returns:
        list of tuples: (path, expected, actual, status_message)
    """
    rows = []
    all_match = True

    for relpath in TRANSPARENT_PNGS:
        path = ROOT / relpath
        if not path.exists():
            rows.append((relpath, "full alpha channel", "MISSING", "file not found"))
            all_match = False
            continue
        try:
            with Image.open(path) as img:
                mode = img.mode
                has_alpha = mode in ("RGBA", "LA") or (mode == "P" and "transparency" in img.info)
                if not has_alpha:
                    rows.append((relpath, "full alpha channel", f"mode {mode}", "✗ no alpha band"))
                    all_match = False
                    continue
                lo, hi = img.convert("RGBA").getchannel("A").getextrema()
                if lo < 255 and hi == 255:
                    rows.append((relpath, "full alpha channel", f"mode {mode}, alpha {lo}-{hi}", "✓"))
                else:
                    problem = "fully opaque" if lo == 255 else "never fully opaque"
                    rows.append((relpath, "full alpha channel",
                                 f"mode {mode}, alpha {lo}-{hi}", f"✗ {problem}"))
                    all_match = False
        except Exception as e:
            rows.append((relpath, "full alpha channel", "ERROR", str(e)))
            all_match = False

    try:
        opaque_text = (ROOT / OPAQUE_MASTER_SVG).read_text()
        transparent_text = (ROOT / TRANSPARENT_MASTER_SVG).read_text()
        ET.fromstring(opaque_text)
        ET.fromstring(transparent_text)
        rows.append((TRANSPARENT_MASTER_SVG, "well-formed SVG (both masters)", "parsed", "✓"))

        removed = set(FILL_RE.findall(opaque_text)) - set(FILL_RE.findall(transparent_text))
        if removed:
            rows.append((TRANSPARENT_MASTER_SVG, "background fill removed vs logo.svg",
                         f"absent: {', '.join(sorted(removed))}", "✓"))
        else:
            rows.append((TRANSPARENT_MASTER_SVG, "background fill removed vs logo.svg",
                         "same fill palette as opaque master", "✗ background baked in"))
            all_match = False
    except Exception as e:
        rows.append((TRANSPARENT_MASTER_SVG, "well-formed SVG; background removed", "ERROR", str(e)))
        all_match = False

    return rows, all_match


def print_rows(rows):
    print(f"{'File':<50} {'Expected':>26} {'Actual':<36} {'Status':<20}")
    print("-" * 134)
    for relpath, expected, actual, status in rows:
        print(f"{relpath:<50} {expected:>26} {actual:<36} {status:<20}")
    print()


def main():
    print("Verifying assets against every testable README.md claim...\n")

    sections = [
        ("PNG dimensions vs README table", verify_dimensions()),
        ("Generated asset inventory", verify_inventory()),
        ("README-named files present", verify_presence()),
        ("Palette vs README table", verify_palette()),
        ("favicon.ico container", verify_favicon_ico()),
        ("Transparent variants", verify_transparency()),
    ]

    all_match = True
    for title, (rows, ok) in sections:
        print(f"== {title} ==")
        print_rows(rows)
        all_match &= ok

    if all_match:
        print("✓ All README claims verified")
        return 0
    else:
        print("✗ VERIFICATION FAILURES FOUND")
        print("\nTo fix:")
        print("1. Update README.md to match the actual assets, OR")
        print("2. Run: .venv/bin/python tools/build_assets.py to regenerate assets")
        print("3. Commit the corrected assets/docs")
        return 1


if __name__ == "__main__":
    exit(main())
