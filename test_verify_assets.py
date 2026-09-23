import json

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
