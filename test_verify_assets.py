import hashlib
import json
import shutil

import pytest
from PIL import Image

from tools import verify_assets


@pytest.fixture
def dimension_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    root.mkdir()
    dimensions = {
        "avatars/avatar.png": (4, 4),
        "banners/banner.png": (6, 3),
    }
    for relpath, size in dimensions.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "#DC3127").save(path)
    monkeypatch.setattr(verify_assets, "ROOT", root)
    monkeypatch.setattr(verify_assets, "EXPECTED_DIMENSIONS", dimensions)
    return root


@pytest.fixture
def presence_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    root.mkdir()
    expected_present = ("source/logo.svg", "tools/verify_assets.py")
    expected_absent = ("source/hero-alt.png",)
    for relpath in expected_present:
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    monkeypatch.setattr(verify_assets, "ROOT", root)
    monkeypatch.setattr(verify_assets, "EXPECTED_PRESENT", expected_present)
    monkeypatch.setattr(verify_assets, "EXPECTED_ABSENT", expected_absent)
    return root


@pytest.fixture
def logo_checksum_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    svg = root / verify_assets.LOGO_SVG_RELPATH
    svg.parent.mkdir(parents=True, exist_ok=True)
    svg.write_bytes(b"<svg>authoritative</svg>\n")
    checksum = root / verify_assets.LOGO_SVG_SHA256_RELPATH
    checksum.write_text(
        f"{hashlib.sha256(svg.read_bytes()).hexdigest()}\n",
        encoding="ascii",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return svg, checksum


@pytest.fixture
def raster_checksum_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    files = {
        verify_assets.LOGO_PNG_RELPATH: b"preserved logo raster\x00",
        verify_assets.HERO_PNG_RELPATH: b"authoritative hero raster\x00",
    }
    checksums = {}
    for relpath, contents in files.items():
        source = root / relpath
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(contents)
        checksum = root / f"{relpath}.sha256"
        checksum.write_text(
            f"{hashlib.sha256(contents).hexdigest()}\n",
            encoding="ascii",
        )
        checksums[relpath] = checksum
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return root, checksums


@pytest.fixture
def logo_original_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    source = root / verify_assets.LOGO_PNG_RELPATH
    original = root / verify_assets.LOGO_ORIGINAL_RELPATH
    source.parent.mkdir(parents=True, exist_ok=True)
    original.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"preserved logo bytes\x00\xff\n")
    original.write_bytes(source.read_bytes())
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return source, original


def _write_ico(path, sizes):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (256, 256), (220, 49, 39, 255)).save(path, sizes=sizes)


@pytest.fixture
def favicon_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    root.mkdir()
    path = root / verify_assets.ICO_RELPATH
    _write_ico(path, [(16, 16), (32, 32), (256, 256)])
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return path


@pytest.fixture
def transparency_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    root.mkdir()
    for relpath in verify_assets.TRANSPARENT_PNGS:
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (2, 2), (220, 49, 39, 255))
        image.putpixel((0, 0), (220, 49, 39, 0))
        image.save(path)

    (root / verify_assets.OPAQUE_MASTER_SVG).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect fill="#EFDECC"/><path fill="#DC3127"/></svg>',
        encoding="utf-8",
    )
    (root / verify_assets.TRANSPARENT_MASTER_SVG).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<path fill="#DC3127"/></svg>',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return root


def test_dimensions_accept_valid_fixture(dimension_fixture):
    rows, ok = verify_assets.verify_dimensions()

    assert ok
    assert rows == [
        ("avatars/avatar.png", "4×4", "4×4", "✓"),
        ("banners/banner.png", "6×3", "6×3", "✓"),
    ]


def test_dimensions_report_missing_file(dimension_fixture):
    (dimension_fixture / "avatars/avatar.png").unlink()

    rows, ok = verify_assets.verify_dimensions()

    assert not ok
    assert rows[0] == ("avatars/avatar.png", "4×4", "MISSING", "file not found")


def test_dimensions_report_incorrect_dimensions(dimension_fixture):
    Image.new("RGB", (5, 4), "#DC3127").save(
        dimension_fixture / "avatars/avatar.png"
    )

    rows, ok = verify_assets.verify_dimensions()

    assert not ok
    assert rows[0] == ("avatars/avatar.png", "4×4", "5×4", "✗ MISMATCH")


def test_platform_manifest_matches_readme_and_committed_files():
    rows, ok = verify_assets.verify_platform_manifest()

    assert ok
    assert rows[-1] == (
        verify_assets.PLATFORM_MANIFEST_RELPATH,
        "README platform table, committed files, and source assets",
        "31 entries",
        "✓",
    )


def test_platform_manifest_reports_committed_dimension_drift(monkeypatch, tmp_path):
    source_root = verify_assets.ROOT
    root = tmp_path / "brand-kit"
    root.mkdir()
    shutil.copy(source_root / "README.md", root / "README.md")
    shutil.copy(
        source_root / verify_assets.PLATFORM_MANIFEST_RELPATH,
        root / verify_assets.PLATFORM_MANIFEST_RELPATH,
    )
    manifest = json.loads(
        (root / verify_assets.PLATFORM_MANIFEST_RELPATH).read_text(encoding="utf-8")
    )
    for asset in manifest["assets"]:
        path = root / asset["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "favicon.ico":
            _write_ico(path, [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        else:
            Image.new(
                "RGB",
                (asset["dimensions"]["width"], asset["dimensions"]["height"]),
                "#DC3127",
            ).save(path)
        source = root / asset["source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
    drifted = root / "avatars/x-400.png"
    Image.new("RGB", (399, 400), "#DC3127").save(drifted)
    monkeypatch.setattr(verify_assets, "ROOT", root)

    rows, ok = verify_assets.verify_platform_manifest()

    assert not ok
    assert any(
        row[0] == "avatars/x-400.png"
        and row[3] == "✗ committed dimensions mismatch"
        for row in rows
    )


def test_presence_accepts_valid_fixture(presence_fixture):
    rows, ok = verify_assets.verify_presence()

    assert ok
    assert rows == [
        ("source/logo.svg", "exists", "found", "✓"),
        ("tools/verify_assets.py", "exists", "found", "✓"),
        ("source/hero-alt.png", "absent (documented removed)", "not found", "✓"),
    ]


def test_presence_reports_missing_file(presence_fixture):
    (presence_fixture / "source/logo.svg").unlink()

    rows, ok = verify_assets.verify_presence()

    assert not ok
    assert rows[0] == (
        "source/logo.svg",
        "exists",
        "MISSING",
        "README names this path",
    )


def test_logo_checksum_accepts_matching_sidecar(logo_checksum_fixture):
    _, checksum = logo_checksum_fixture

    rows, ok = verify_assets.verify_logo_checksum()

    assert ok
    assert rows == [
        (
            verify_assets.LOGO_SVG_SHA256_RELPATH,
            f"SHA-256 digest of {verify_assets.LOGO_SVG_RELPATH}",
            checksum.read_text(encoding="ascii").strip(),
            "✓",
        )
    ]


def test_logo_checksum_reports_missing_sidecar(logo_checksum_fixture):
    _, checksum = logo_checksum_fixture
    checksum.unlink()

    rows, ok = verify_assets.verify_logo_checksum()

    assert not ok
    assert rows == [
        (
            verify_assets.LOGO_SVG_SHA256_RELPATH,
            f"SHA-256 digest of {verify_assets.LOGO_SVG_RELPATH}",
            "MISSING",
            "file not found",
        )
    ]


def test_logo_checksum_reports_malformed_sidecar(logo_checksum_fixture):
    _, checksum = logo_checksum_fixture
    checksum.write_text("not-a-sha256\n", encoding="ascii")

    rows, ok = verify_assets.verify_logo_checksum()

    assert not ok
    assert rows == [
        (
            verify_assets.LOGO_SVG_SHA256_RELPATH,
            "64 hexadecimal characters",
            "not-a-sha256",
            "✗ malformed SHA-256 digest",
        )
    ]


def test_logo_checksum_reports_stale_sidecar(logo_checksum_fixture):
    svg, checksum = logo_checksum_fixture
    stale = hashlib.sha256(b"<svg>previous</svg>\n").hexdigest()
    checksum.write_text(f"{stale}\n", encoding="ascii")

    rows, ok = verify_assets.verify_logo_checksum()

    assert not ok
    assert rows == [
        (
            verify_assets.LOGO_SVG_SHA256_RELPATH,
            hashlib.sha256(svg.read_bytes()).hexdigest(),
            stale,
            "✗ stale digest",
        )
    ]


@pytest.fixture
def transparent_logo_checksum_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    svg = root / verify_assets.TRANSPARENT_LOGO_SVG_RELPATH
    svg.parent.mkdir(parents=True, exist_ok=True)
    svg.write_bytes(b"<svg>transparent authoritative</svg>\n")
    checksum = root / verify_assets.TRANSPARENT_LOGO_SVG_SHA256_RELPATH
    checksum.write_text(
        f"{hashlib.sha256(svg.read_bytes()).hexdigest()}\n",
        encoding="ascii",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)
    return svg, checksum


def test_transparent_logo_checksum_accepts_matching_sidecar(
    transparent_logo_checksum_fixture,
):
    _, checksum = transparent_logo_checksum_fixture

    rows, ok = verify_assets.verify_transparent_logo_checksum()

    assert ok
    assert rows == [
        (
            verify_assets.TRANSPARENT_LOGO_SVG_SHA256_RELPATH,
            f"SHA-256 digest of {verify_assets.TRANSPARENT_LOGO_SVG_RELPATH}",
            checksum.read_text(encoding="ascii").strip(),
            "✓",
        )
    ]


def test_transparent_logo_checksum_reports_stale_sidecar(
    transparent_logo_checksum_fixture,
):
    svg, checksum = transparent_logo_checksum_fixture
    stale = hashlib.sha256(b"<svg>previous transparent</svg>\n").hexdigest()
    checksum.write_text(f"{stale}\n", encoding="ascii")

    rows, ok = verify_assets.verify_transparent_logo_checksum()

    assert not ok
    assert rows == [
        (
            verify_assets.TRANSPARENT_LOGO_SVG_SHA256_RELPATH,
            hashlib.sha256(svg.read_bytes()).hexdigest(),
            stale,
            "✗ stale digest",
        )
    ]


@pytest.mark.parametrize(
    ("source_relpath", "verify"),
    [
        (verify_assets.LOGO_PNG_RELPATH, verify_assets.verify_logo_png_checksum),
        (verify_assets.HERO_PNG_RELPATH, verify_assets.verify_hero_checksum),
    ],
)
def test_raster_checksum_accepts_matching_sidecar(
    raster_checksum_fixture, source_relpath, verify
):
    _, checksums = raster_checksum_fixture

    rows, ok = verify()

    assert ok
    checksum = checksums[source_relpath]
    assert rows == [
        (
            f"{source_relpath}.sha256",
            f"SHA-256 digest of {source_relpath}",
            checksum.read_text(encoding="ascii").strip(),
            "✓",
        )
    ]


@pytest.mark.parametrize(
    ("source_relpath", "verify"),
    [
        (verify_assets.LOGO_PNG_RELPATH, verify_assets.verify_logo_png_checksum),
        (verify_assets.HERO_PNG_RELPATH, verify_assets.verify_hero_checksum),
    ],
)
def test_raster_checksum_reports_missing_sidecar(
    raster_checksum_fixture, source_relpath, verify
):
    _, checksums = raster_checksum_fixture
    checksums[source_relpath].unlink()

    rows, ok = verify()

    assert not ok
    assert rows == [
        (
            f"{source_relpath}.sha256",
            f"SHA-256 digest of {source_relpath}",
            "MISSING",
            "file not found",
        )
    ]


@pytest.mark.parametrize(
    ("source_relpath", "verify"),
    [
        (verify_assets.LOGO_PNG_RELPATH, verify_assets.verify_logo_png_checksum),
        (verify_assets.HERO_PNG_RELPATH, verify_assets.verify_hero_checksum),
    ],
)
def test_raster_checksum_reports_malformed_sidecar(
    raster_checksum_fixture, source_relpath, verify
):
    _, checksums = raster_checksum_fixture
    checksums[source_relpath].write_text("not-a-sha256\n", encoding="ascii")

    rows, ok = verify()

    assert not ok
    assert rows == [
        (
            f"{source_relpath}.sha256",
            "64 hexadecimal characters",
            "not-a-sha256",
            "✗ malformed SHA-256 digest",
        )
    ]


@pytest.mark.parametrize(
    ("source_relpath", "verify"),
    [
        (verify_assets.LOGO_PNG_RELPATH, verify_assets.verify_logo_png_checksum),
        (verify_assets.HERO_PNG_RELPATH, verify_assets.verify_hero_checksum),
    ],
)
def test_raster_checksum_reports_stale_sidecar(
    raster_checksum_fixture, source_relpath, verify
):
    root, checksums = raster_checksum_fixture
    source = root / source_relpath
    stale = hashlib.sha256(b"previous raster\n").hexdigest()
    checksums[source_relpath].write_text(f"{stale}\n", encoding="ascii")

    rows, ok = verify()

    assert not ok
    assert rows == [
        (
            f"{source_relpath}.sha256",
            hashlib.sha256(source.read_bytes()).hexdigest(),
            stale,
            "✗ stale digest",
        )
    ]


def test_logo_original_copy_accepts_exact_bytes(logo_original_fixture):
    source, _ = logo_original_fixture

    rows, ok = verify_assets.verify_logo_original_copy()

    assert ok
    assert rows == [
        (
            verify_assets.LOGO_ORIGINAL_RELPATH,
            f"byte-for-byte copy of {verify_assets.LOGO_PNG_RELPATH}",
            f"{source.stat().st_size} bytes",
            "✓",
        )
    ]


def test_logo_original_copy_reports_different_bytes(logo_original_fixture):
    _, original = logo_original_fixture
    original.write_bytes(b"same pixels, different bytes")

    rows, ok = verify_assets.verify_logo_original_copy()

    assert not ok
    assert rows == [
        (
            verify_assets.LOGO_ORIGINAL_RELPATH,
            f"byte-for-byte copy of {verify_assets.LOGO_PNG_RELPATH}",
            "28 bytes (source: 23 bytes)",
            "✗ different bytes",
        )
    ]


def test_logo_original_copy_reports_missing_copy(logo_original_fixture):
    _, original = logo_original_fixture
    original.unlink()

    rows, ok = verify_assets.verify_logo_original_copy()

    assert not ok
    assert rows == [
        (
            verify_assets.LOGO_ORIGINAL_RELPATH,
            f"byte-for-byte copy of {verify_assets.LOGO_PNG_RELPATH}",
            "MISSING",
            "file not found",
        )
    ]


def test_favicon_accepts_valid_layers(favicon_fixture):
    rows, ok = verify_assets.verify_favicon_ico()

    assert ok
    assert rows[0][3] == "✓"
    assert rows[1][3] == "✓"
    assert rows[2][3] == "✓"
    assert all(row[3] == "✓" for row in rows[3:])


@pytest.mark.parametrize(
    "sizes, expected_status",
    [
        ([(16, 16)], "✗ single-resolution"),
        ([(16, 16), (32, 32)], "✗ MISMATCH"),
    ],
)
def test_favicon_reports_layer_mismatch(favicon_fixture, sizes, expected_status):
    _write_ico(favicon_fixture, sizes)

    rows, ok = verify_assets.verify_favicon_ico()

    assert not ok
    assert any(row[3] == expected_status for row in rows)


def test_favicon_reports_malformed_container(favicon_fixture):
    favicon_fixture.write_bytes(b"not an ico")

    rows, ok = verify_assets.verify_favicon_ico()

    assert not ok
    assert rows[0][:3] == ("favicon/favicon.ico", "multi-res 16-256", "ERROR")
    assert "not a valid .ico container" in rows[0][3]


def test_transparency_accepts_valid_fixture(transparency_fixture):
    rows, ok = verify_assets.verify_transparency()

    assert ok
    assert len(rows) == 5
    assert all(row[3] == "✓" for row in rows)


def test_transparency_reports_missing_alpha_channel(transparency_fixture):
    path = transparency_fixture / verify_assets.TRANSPARENT_PNGS[0]
    Image.new("RGB", (2, 2), "#DC3127").save(path)

    rows, ok = verify_assets.verify_transparency()

    assert not ok
    assert rows[0] == (
        verify_assets.TRANSPARENT_PNGS[0],
        "full alpha channel",
        "mode RGB",
        "✗ no alpha band",
    )


def test_transparency_reports_fully_opaque_alpha_channel(transparency_fixture):
    path = transparency_fixture / verify_assets.TRANSPARENT_PNGS[0]
    Image.new("RGBA", (2, 2), (220, 49, 39, 255)).save(path)

    rows, ok = verify_assets.verify_transparency()

    assert not ok
    assert rows[0][3] == "✗ fully opaque"


def test_transparency_reports_alpha_without_opaque_pixels(transparency_fixture):
    path = transparency_fixture / verify_assets.TRANSPARENT_PNGS[0]
    Image.new("RGBA", (2, 2), (220, 49, 39, 0)).save(path)

    rows, ok = verify_assets.verify_transparency()

    assert not ok
    assert rows[0][3] == "✗ never fully opaque"


@pytest.mark.parametrize("svg_name", [
    verify_assets.OPAQUE_MASTER_SVG,
    verify_assets.TRANSPARENT_MASTER_SVG,
])
def test_transparency_reports_invalid_svg(transparency_fixture, svg_name):
    (transparency_fixture / svg_name).write_text("<svg>", encoding="utf-8")

    rows, ok = verify_assets.verify_transparency()

    assert not ok
    assert rows[-1][0] == verify_assets.TRANSPARENT_MASTER_SVG
    assert rows[-1][2] == "ERROR"


def test_transparency_reports_baked_in_svg_background(transparency_fixture):
    (transparency_fixture / verify_assets.TRANSPARENT_MASTER_SVG).write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect fill="#EFDECC"/><path fill="#DC3127"/></svg>',
        encoding="utf-8",
    )

    rows, ok = verify_assets.verify_transparency()

    assert not ok
    assert rows[-1][3] == "✗ background baked in"


def test_palette_matches_readme_table():
    rows, ok = verify_assets.verify_palette()

    assert ok
    assert rows[0][0] == "palette.json"
    assert rows[0][3] == "✓"


def _write_canonical_palette(root, palette):
    rows = "\n".join(
        f"| {name} | `{value}` | Test |"
        for name, value in verify_assets.CANONICAL_PALETTE.items()
    )
    (root / "README.md").write_text(
        f"# Brand\n\n## Palette\n\n| Name | Hex | Use |\n|---|---|---|\n{rows}\n\n## Other\n",
        encoding="utf-8",
    )
    (root / "palette.json").write_text(json.dumps(palette), encoding="utf-8")


def test_palette_accepts_valid_fixture(monkeypatch, tmp_path):
    _write_canonical_palette(tmp_path, verify_assets.CANONICAL_PALETTE)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert ok
    assert rows == [
        (
            "palette.json",
            "names and exact hexes match the canonical palette",
            "5 colors",
            "✓",
        )
    ]


def test_palette_reports_missing_file(monkeypatch, tmp_path):
    _write_canonical_palette(tmp_path, verify_assets.CANONICAL_PALETTE)
    (tmp_path / "palette.json").unlink()
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert rows == [
        (
            "palette.json",
            "names and exact hexes match the canonical palette",
            "MISSING",
            "file not found",
        )
    ]


@pytest.mark.parametrize(
    "payload, expected_actual, expected_detail",
    [
        ("{", "ERROR", "invalid JSON:"),
        ("[]", "list", "✗ not an object"),
    ],
)
def test_palette_reports_malformed_json_data(
    monkeypatch, tmp_path, payload, expected_actual, expected_detail
):
    _write_canonical_palette(tmp_path, verify_assets.CANONICAL_PALETTE)
    (tmp_path / "palette.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert rows[0][2] == expected_actual
    assert expected_detail in rows[0][3]


def test_palette_mismatch_is_reported(monkeypatch, tmp_path):
    palette = dict(verify_assets.CANONICAL_PALETTE)
    palette["Polo Red"] = "#DC3128"
    _write_canonical_palette(tmp_path, palette)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert rows[0][0] == "palette.json"
    assert "hex mismatches: Polo Red" in rows[0][3]


def test_palette_reports_missing_and_extra_names(monkeypatch, tmp_path):
    palette = dict(verify_assets.CANONICAL_PALETTE)
    del palette["Skin Tan"]
    palette["Extra"] = "#FFFFFF"
    _write_canonical_palette(tmp_path, palette)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert "missing names: Skin Tan" in rows[0][3]
    assert "extra names: Extra" in rows[0][3]


def test_palette_reports_exact_hex_mismatch(monkeypatch, tmp_path):
    palette = dict(verify_assets.CANONICAL_PALETTE)
    palette["Ink"] = "#0A0A09"
    _write_canonical_palette(tmp_path, palette)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert "hex mismatches: Ink" in rows[0][3]


@pytest.mark.parametrize("value", ["red", "#DC312", "#DC31277", None, 42])
def test_palette_reports_malformed_colors(monkeypatch, tmp_path, value):
    palette = dict(verify_assets.CANONICAL_PALETTE)
    palette["Polo Red"] = value
    _write_canonical_palette(tmp_path, palette)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert "malformed colors: Polo Red" in rows[0][3]


def test_palette_rejects_duplicate_json_names(tmp_path, monkeypatch):
    palette = json.dumps(verify_assets.CANONICAL_PALETTE)
    duplicate = palette[:-1] + ',"Polo Red":"#DC3128"}'
    _write_canonical_palette(tmp_path, verify_assets.CANONICAL_PALETTE)
    (tmp_path / "palette.json").write_text(duplicate, encoding="utf-8")
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert "duplicate palette name: Polo Red" in rows[0][3]


def test_palette_generator_uses_the_same_canonical_values():
    from tools import build_assets

    assert build_assets.PALETTE == verify_assets.CANONICAL_PALETTE


@pytest.mark.parametrize(
    "row",
    [
        "| Extra | not-a-color | Invalid |\n",
        "| Ink | `#0A0A08` | Duplicate |\n",
    ],
)
def test_palette_rejects_malformed_or_duplicate_readme_rows(
    monkeypatch, tmp_path, row
):
    _write_canonical_palette(tmp_path, verify_assets.CANONICAL_PALETTE)
    readme_path = tmp_path / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme_path.write_text(
        readme.replace("\n## Other", f"\n{row}\n## Other"),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert rows[0][3]


def _write_expected_inventory(root):
    for relpath in verify_assets.EXPECTED_INVENTORY:
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def test_generated_inventory_matches_documented_assets(monkeypatch, tmp_path):
    _write_expected_inventory(tmp_path)
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_inventory()

    assert len(verify_assets.EXPECTED_ASSETS) == 37
    assert len(verify_assets.EXPECTED_INVENTORY) == 38
    assert ok
    assert rows == [
        ("asset inventory", "37 derived assets + palette.json", "38 files", "✓")
    ]


@pytest.mark.parametrize("missing", ["avatars/x-400.png", "palette.json"])
def test_generated_inventory_reports_missing_file(monkeypatch, tmp_path, missing):
    _write_expected_inventory(tmp_path)
    (tmp_path / missing).unlink()
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_inventory()

    assert not ok
    assert any(
        path == missing and status == "✗ MISSING" for path, _, _, status in rows
    )


def test_generated_inventory_reports_unexpected_stale_asset(monkeypatch, tmp_path):
    _write_expected_inventory(tmp_path)
    stale = tmp_path / "banners/stale.png"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale")
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_inventory()

    assert not ok
    assert (
        "banners/stale.png",
        "no unexpected generated file",
        "UNEXPECTED",
        "✗ UNEXPECTED",
    ) in rows
