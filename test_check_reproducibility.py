from pathlib import Path

import pytest

from tools import check_reproducibility


ROOT = Path(__file__).resolve().parent


def test_reproducibility_harness_covers_complete_generated_set_and_trace():
    source = (ROOT / "tools/check_reproducibility.py").read_text()

    assert 'GENERATED_DIRECTORIES = ("avatars", "banners", "favicon", "logo")' in source
    assert 'GENERATED_FILES = ("palette.json", "platform-assets.json")' in source
    assert 'TRACE_FILES = ("source/logo.svg", "source/logo.svg.sha256")' in source
    assert '"tools/trace_logo.py"' in source
    assert '"tools/build_assets.py"' in source
    assert '"tools/verify_assets.py"' in source


def test_reproducibility_harness_uses_fresh_locked_toolchains():
    source = (ROOT / "tools/check_reproducibility.py").read_text()

    assert '"--locked"' in source
    assert '"--no-cache-dir"' in source
    assert '"--only-binary=:all:"' in source
    assert '"CARGO_HOME": str(cargo_home)' in source
    assert 'parse_pin_table(document)' in source


def test_generated_paths_are_sorted_and_include_current_inventory():
    paths = check_reproducibility._generated_paths(ROOT)

    assert paths == tuple(sorted(paths))
    assert Path("palette.json") in paths
    assert Path("platform-assets.json") in paths
    assert Path("logo/logo-original.png") in paths
    assert Path("favicon/favicon.ico") in paths
    assert Path("source/logo.svg") in paths
    assert Path("source/logo.svg.sha256") in paths


def test_compare_fingerprints_reports_transitive_or_asset_drift():
    with pytest.raises(
        check_reproducibility.ReproducibilityError,
        match=r"differs for 1 file\(s\): logo/logo-256.png",
    ):
        check_reproducibility._compare_fingerprints(
            {"logo/logo-256.png": "one"},
            {"logo/logo-256.png": "two"},
            label="independent clean regeneration",
        )
