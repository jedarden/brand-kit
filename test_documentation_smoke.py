from pathlib import Path

from tools import check_documentation


ROOT = Path(__file__).resolve().parent


def test_repository_documentation_references_and_toolchain_are_valid():
    assert check_documentation.check_repository(ROOT) == []


def test_cross_reference_check_rejects_missing_links_scripts_and_schemas(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "README.md").write_text(
        "[missing guide](docs/missing.md)\n"
        "Run `python3 tools/missing.py` and validate `contracts/missing.schema.json`.\n",
        encoding="utf-8",
    )

    errors = check_documentation.check_cross_references(tmp_path)

    assert any("broken local link 'docs/missing.md'" in error for error in errors)
    assert any("tools/missing.py" in error for error in errors)
    assert any("contracts/missing.schema.json" in error for error in errors)


def test_cross_reference_check_ignores_external_and_sibling_links(tmp_path):
    (tmp_path / "README.md").write_text(
        "[external](https://example.com/docs/missing.md) "
        "[sibling](../declarative-config/docs/guide.md)\n",
        encoding="utf-8",
    )

    assert check_documentation.check_cross_references(tmp_path) == []


def test_toolchain_check_rejects_global_python_for_asset_generator(tmp_path):
    docs = tmp_path / "docs/notes"
    docs.mkdir(parents=True)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text(
        encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        readme.replace(
            ".venv/bin/python tools/build_assets.py",
            "python3 tools/build_assets.py",
            1,
        ),
        encoding="utf-8",
    )
    (docs / "asset-toolchain.md").write_text(toolchain, encoding="utf-8")

    errors = check_documentation.check_toolchain_documentation(tmp_path)

    assert any("tools/build_assets.py must use .venv/bin/python" in error for error in errors)
