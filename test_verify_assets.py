import json

import pytest

from tools import verify_assets


def test_palette_matches_readme_table():
    rows, ok = verify_assets.verify_palette()

    assert ok
    assert rows[0][0] == "palette.json"
    assert rows[0][3] == "✓"


def test_palette_mismatch_is_reported(monkeypatch, tmp_path):
    (tmp_path / "README.md").write_text(
        """# Brand\n\n## Palette\n\n| Name | Hex | Use |\n|---|---|---|\n| Polo Red | `#DC3127` | Primary |\n| Ink | `#0A0A08` | Text |\n\n## Other\n""",
        encoding="utf-8",
    )
    (tmp_path / "palette.json").write_text(
        json.dumps({"Polo Red": "#DC3128", "Ink": "#0A0A08"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", tmp_path)

    rows, ok = verify_assets.verify_palette()

    assert not ok
    assert rows[0][0] == "palette.json"
    assert "MISMATCH" in rows[0][3]


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
