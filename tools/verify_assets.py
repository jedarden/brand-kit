#!/usr/bin/env python3
"""Verify that committed assets match every testable claim README.md makes.

Seven classes of README-vs-repo drift are checked:
  1. Shipped PNG dimensions vs the README per-platform table.
  2. The generated platform manifest matches the README table, committed
     dimensions, and declared source assets.
  3. Presence of every repo path README names (plus absence of the file
     it documents as removed).
  4. favicon.ico is multi-resolution 16-256 and every contained frame
     decodes.
  5. The transparent variants have full alpha channels, and the
     transparent SVG master has its background removed relative to the
     opaque one.
  6. The required names and exact six-digit hex values in palette.json match
     the canonical palette table.
  7. The generated asset inventory is exactly the 37 documented derived
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
PLATFORM_MANIFEST_RELPATH = "platform-assets.json"
PLATFORM_MANIFEST_SCHEMA_VERSION = 1
PALETTE_RELPATH = "palette.json"
CANONICAL_PALETTE = {
    "Polo Red": "#DC3127",
    "Ink": "#0A0A08",
    "Canvas Cream": "#EFDECC",
    "Skin Tan": "#F5B079",
    "Control-Room Black": "#070506",
}
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

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
# jedarden.com/public/brand/ are external — the consumer tools own those.
EXPECTED_PRESENT = [
    # Sources (README table)
    "source/logo.svg",
    "source/logo.png",
    "source/hero.png",
    "platform-assets.json",

    # Transparent variants (README: "Transparent variants")
    "logo/logo-256-transparent.png",
    "logo/logo-512-transparent.png",
    "logo/logo-1024-transparent.png",
    "logo/logo-transparent.svg",

    # Tools and docs README tells the reader to run / read
    "tools/trace_logo.py",
    "tools/build_assets.py",
    "tools/consumer_sync.py",
    "tools/consumer_drift.py",
    "consumer-drift.json",
    "automation/brand-kit-consumer-drift-cronworkflow.yml",
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
MARKDOWN_ASSET_RE = re.compile(r"`([^`]+)`(?:\s*\(([^)]*)\))?")
DIMENSION_RE = re.compile(r"(?<!\d)(\d+)\s*[×x]\s*(\d+)")


def _readme_section(text, heading, next_heading_pattern=r"^#{1,6}\s+"):
    """Return one Markdown section, excluding its next heading."""
    start = text.find(heading)
    if start == -1:
        raise ValueError(f"README section missing: {heading}")
    body_start = start + len(heading)
    next_heading = re.search(next_heading_pattern, text[body_start:], re.MULTILINE)
    end = body_start + next_heading.start() if next_heading else len(text)
    return text[body_start:end]


def _expected_source_for_path(path):
    if path.startswith(("avatars/", "favicon/")):
        return "source/logo.svg"
    if path.startswith("banners/"):
        return "source/hero.png"
    raise ValueError(f"README platform asset has unknown generated family: {path}")


def _table_cells(line):
    if not line.lstrip().startswith("|"):
        return None
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _readme_dimensions(annotation, path):
    matches = [
        {"width": int(width), "height": int(height)}
        for width, height in DIMENSION_RE.findall(annotation or "")
    ]
    if path == ICO_RELPATH:
        if not matches:
            raise ValueError(f"missing favicon dimensions for {path}")
        return {
            "width": max(size["width"] for size in matches),
            "height": max(size["height"] for size in matches),
            "sizes": matches,
        }
    if len(matches) != 1:
        raise ValueError(f"expected one dimension pair for {path}")
    return matches[0]


def _readme_favicon_assets(text):
    """Expand the README's concise favicon-set documentation."""
    section = _readme_section(text, "### Favicons")
    assets = []
    for match in MARKDOWN_ASSET_RE.finditer(section):
        path = match.group(1)
        if path == "favicon/":
            continue
        if not path.startswith("favicon/"):
            continue
        assets.append({
            "role": "favicon",
            "path": path,
            "dimensions": _readme_dimensions(match.group(2), path),
            "source": _expected_source_for_path(path),
        })
    if not assets:
        raise ValueError("README favicon section has no concrete assets")
    return assets


def read_readme_platform_assets():
    """Parse the README platform table into manifest-shaped asset entries."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _readme_section(text, "## Per-platform assets")
    favicon_assets = _readme_favicon_assets(text)
    entries = []
    saw_header = False

    for line in section.splitlines():
        cells = _table_cells(line)
        if not cells:
            continue
        if cells[:3] == ["Platform", "Profile picture", "Banner / cover"]:
            saw_header = True
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) < 3:
            raise ValueError("malformed per-platform asset table row")
        if not saw_header:
            raise ValueError("per-platform asset table header missing")

        platform = cells[0]
        for role, cell in (("profile_picture", cells[1]), ("banner", cells[2])):
            for match in MARKDOWN_ASSET_RE.finditer(cell):
                path = match.group(1)
                if path == "favicon/":
                    if role != "profile_picture":
                        raise ValueError("favicon set must be in the profile column")
                    entries.extend(
                        {
                            "platform": platform,
                            **asset,
                        }
                        for asset in favicon_assets
                    )
                    continue
                if path.startswith("…"):
                    raise ValueError(f"README uses an abbreviated asset path: {path}")
                entries.append({
                    "platform": platform,
                    "role": role,
                    "path": path,
                    "dimensions": _readme_dimensions(match.group(2), path),
                    "source": _expected_source_for_path(path),
                })

    if not saw_header or not entries:
        raise ValueError("README per-platform asset table has no asset rows")
    return entries


def _manifest_object(pairs):
    manifest = {}
    for key, value in pairs:
        if key in manifest:
            raise ValueError(f"duplicate manifest key: {key}")
        manifest[key] = value
    return manifest


def _manifest_dimensions_are_valid(dimensions):
    if not isinstance(dimensions, dict):
        return False
    if not all(
        isinstance(dimensions.get(key), int) and not isinstance(dimensions.get(key), bool)
        and dimensions[key] > 0
        for key in ("width", "height")
    ):
        return False
    sizes = dimensions.get("sizes")
    if sizes is None:
        return True
    return (
        isinstance(sizes, list)
        and all(_manifest_dimensions_are_valid(size) and set(size) == {"width", "height"}
                for size in sizes)
    )


def _committed_dimensions(path):
    if path == ROOT / ICO_RELPATH:
        sizes = [
            {"width": width, "height": height}
            for width, height in ico_contained_sizes(path)
        ]
        return {
            "width": max(size["width"] for size in sizes),
            "height": max(size["height"] for size in sizes),
            "sizes": sizes,
        }
    with Image.open(path) as image:
        width, height = image.size
    return {"width": width, "height": height}


def verify_platform_manifest():
    """Validate platform-assets.json against README, files, and sources."""
    path = ROOT / PLATFORM_MANIFEST_RELPATH
    claim = "README platform table, committed files, and source assets"
    if not path.exists():
        return [(PLATFORM_MANIFEST_RELPATH, claim, "MISSING", "file not found")], False

    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_manifest_object
        )
    except (OSError, UnicodeError, ValueError) as error:
        return [(
            PLATFORM_MANIFEST_RELPATH,
            claim,
            "ERROR",
            f"invalid JSON: {error}",
        )], False

    if not isinstance(manifest, dict):
        return [(PLATFORM_MANIFEST_RELPATH, claim, type(manifest).__name__, "✗ not an object")], False
    if manifest.get("schema_version") != PLATFORM_MANIFEST_SCHEMA_VERSION:
        return [(
            PLATFORM_MANIFEST_RELPATH,
            claim,
            repr(manifest.get("schema_version")),
            f"✗ schema_version must be {PLATFORM_MANIFEST_SCHEMA_VERSION}",
        )], False
    actual = manifest.get("assets")
    if not isinstance(actual, list):
        return [(PLATFORM_MANIFEST_RELPATH, claim, type(actual).__name__, "✗ assets is not a list")], False

    rows = []
    all_match = True
    try:
        expected = read_readme_platform_assets()
    except (OSError, UnicodeError, ValueError) as error:
        return [(PLATFORM_MANIFEST_RELPATH, claim, "README ERROR", str(error))], False

    if actual != expected:
        rows.append((
            PLATFORM_MANIFEST_RELPATH,
            "exact entries parsed from README",
            f"{len(actual)} entries",
            f"✗ MISMATCH (README has {len(expected)} entries)",
        ))
        all_match = False

    seen = set()
    for index, asset in enumerate(actual):
        label = f"asset {index}"
        if not isinstance(asset, dict):
            rows.append((label, "manifest asset object", type(asset).__name__, "✗ not an object"))
            all_match = False
            continue
        required = {"platform", "role", "path", "dimensions", "source"}
        if set(asset) != required:
            rows.append((
                label,
                "platform, role, path, dimensions, source",
                ", ".join(sorted(asset)),
                "✗ invalid fields",
            ))
            all_match = False
            continue
        if not all(isinstance(asset[field], str) and asset[field] for field in ("platform", "role", "path", "source")):
            rows.append((label, "non-empty string fields", repr(asset), "✗ malformed strings"))
            all_match = False
            continue
        identity = (asset["platform"], asset["role"], asset["path"])
        if identity in seen:
            rows.append((label, "unique platform role/path", repr(identity), "✗ duplicate"))
            all_match = False
        seen.add(identity)
        if not _manifest_dimensions_are_valid(asset["dimensions"]):
            rows.append((label, "valid dimensions object", repr(asset["dimensions"]), "✗ malformed dimensions"))
            all_match = False
        asset_path = ROOT / asset["path"]
        source_path = ROOT / asset["source"]
        if not asset_path.is_file():
            rows.append((asset["path"], "committed file exists", "MISSING", "✗ missing"))
            all_match = False
            continue
        if not source_path.is_file():
            rows.append((asset["source"], "source asset exists", "MISSING", "✗ missing"))
            all_match = False
        try:
            expected_source = _expected_source_for_path(asset["path"])
        except ValueError:
            expected_source = None
        if expected_source is None or asset["source"] != expected_source:
            rows.append((
                asset["path"],
                "source matches generated asset family",
                asset["source"],
                "✗ wrong source",
            ))
            all_match = False
        try:
            committed = _committed_dimensions(asset_path)
        except Exception as error:
            rows.append((asset["path"], "file dimensions readable", "ERROR", str(error)))
            all_match = False
            continue
        if asset["dimensions"] != committed:
            rows.append((
                asset["path"],
                repr(asset["dimensions"]),
                repr(committed),
                "✗ committed dimensions mismatch",
            ))
            all_match = False

    if all_match:
        rows.append((
            PLATFORM_MANIFEST_RELPATH,
            claim,
            f"{len(actual)} entries",
            "✓",
        ))
    return rows, all_match


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
    next_heading = re.search(r"^#{1,6}\s+", text[start + len("## Palette"):], re.MULTILINE)
    end = (
        start + len("## Palette") + next_heading.start()
        if next_heading is not None
        else -1
    )
    section = text[start:] if end == -1 else text[start:end]
    palette = {}
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            raise ValueError("malformed palette table row")
        if cells[:2] == ["Name", "Hex"]:
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        name, value = cells[0], cells[1]
        if not re.fullmatch(r"`#[0-9A-Fa-f]{6}`", value):
            raise ValueError(f"malformed palette color for {name or 'unnamed entry'}")
        if name in palette:
            raise ValueError(f"duplicate palette name: {name}")
        palette[name] = value[1:-1]
    return palette


def _palette_with_unique_keys(pairs):
    palette = {}
    for name, value in pairs:
        if name in palette:
            raise ValueError(f"duplicate palette name: {name}")
        palette[name] = value
    return palette


def verify_palette():
    claim = "names and exact hexes match the canonical palette"
    path = ROOT / PALETTE_RELPATH
    if not path.exists():
        return [(PALETTE_RELPATH, claim, "MISSING", "file not found")], False

    try:
        actual = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_palette_with_unique_keys,
        )
    except (OSError, UnicodeError, ValueError) as e:
        return [(PALETTE_RELPATH, claim, "ERROR", f"invalid JSON: {e}")], False

    try:
        documented = read_readme_palette()
    except (OSError, UnicodeError, ValueError) as e:
        return [(PALETTE_RELPATH, claim, "README ERROR", str(e))], False

    if not isinstance(actual, dict):
        return [(PALETTE_RELPATH, claim, type(actual).__name__, "✗ not an object")], False
    if not documented:
        return [(PALETTE_RELPATH, claim, "no README rows", "✗ README table missing")], False
    if documented != CANONICAL_PALETTE:
        return [
            (
                PALETTE_RELPATH,
                claim,
                json.dumps(documented, ensure_ascii=False),
                "✗ README MISMATCH: documented palette is not canonical",
            )
        ], False

    actual_names = set(actual)
    canonical_names = set(CANONICAL_PALETTE)
    missing = sorted(canonical_names - actual_names)
    extra = sorted(actual_names - canonical_names)
    malformed = sorted(
        name
        for name, value in actual.items()
        if not isinstance(value, str) or HEX_COLOR_RE.fullmatch(value) is None
    )
    mismatched = sorted(
        name
        for name, value in actual.items()
        if name in CANONICAL_PALETTE
        and isinstance(value, str)
        and HEX_COLOR_RE.fullmatch(value) is not None
        and value != CANONICAL_PALETTE[name]
    )

    issues = []
    if missing:
        issues.append(f"missing names: {', '.join(missing)}")
    if extra:
        issues.append(f"extra names: {', '.join(extra)}")
    if malformed:
        issues.append(f"malformed colors: {', '.join(malformed)}")
    if mismatched:
        issues.append(f"hex mismatches: {', '.join(mismatched)}")

    if not issues:
        return [(PALETTE_RELPATH, claim, f"{len(actual)} colors", "✓")], True

    actual_text = json.dumps(actual, ensure_ascii=False)
    return [
        (
            PALETTE_RELPATH,
            claim,
            actual_text,
            f"✗ MISMATCH: {'; '.join(issues)}",
        )
    ], False


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
        ("Platform asset manifest", verify_platform_manifest()),
        ("Generated asset inventory", verify_inventory()),
        ("README-named files present", verify_presence()),
        ("Canonical palette", verify_palette()),
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
