import json

import pytest

from tools import verify_assets


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


def test_generated_inventory_matches_documented_assets():
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
