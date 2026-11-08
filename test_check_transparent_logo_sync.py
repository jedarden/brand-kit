import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tools import check_transparent_logo_sync
from tools.check_release_preflight import PathChange

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "tools" / "check_transparent_logo_sync.py"

CREAM = (0xEF, 0xDD, 0xCB)
INK = (0x0A, 0x0A, 0x08)
TAN = (244, 175, 121)
RED = (200, 10, 10)
ARTWORK_BOX = (8, 8, 24, 24)
SIZE = 32

OPAQUE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'data-fill="200,10,10"></svg>'
)
TRANSPARENT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'data-fill="200,10,10" data-alpha="255"></svg>'
)


# ---- synthetic rendered masters ----------------------------------------------


def _background_layer(size=SIZE):
    layer = Image.new("RGBA", (size, size), CREAM + (255,))
    ImageDraw.Draw(layer).rectangle(
        (0, 0, size // 2 - 1, size), fill=INK + (255,)
    )
    return layer


def _artwork_layer(size=SIZE, *, color=TAN, box=ARTWORK_BOX, aa_pixels=()):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # ARTWORK_BOX uses the same half-open convention as Image.size and the
    # report bounding boxes; Pillow's rectangle endpoint is inclusive.
    ImageDraw.Draw(layer).rectangle(
        (box[0], box[1], box[2] - 1, box[3] - 1), fill=color + (255,)
    )
    for position, alpha in aa_pixels:
        layer.putpixel(position, color + (alpha,))
    return layer


def test_compare_renders_reports_layered_background_pair_in_sync():
    artwork = _artwork_layer(aa_pixels=(((4, 16), 100), ((30, 3), 60)))
    opaque = Image.alpha_composite(_background_layer(), artwork).convert("RGB")

    report = check_transparent_logo_sync.compare_renders(
        opaque,
        artwork,
        background_colors=(CREAM, INK),
    )

    assert report.in_sync
    assert report.divergent_pixels == 0
    assert report.divergent_bbox is None
    assert report.excluded_blend_pixels == 0
    width = ARTWORK_BOX[2] - ARTWORK_BOX[0]
    height = ARTWORK_BOX[3] - ARTWORK_BOX[1]
    assert report.opaque_artwork_pixels == width * height + 2


def test_compare_renders_flags_transparent_color_divergence_with_bbox():
    artwork = _artwork_layer()
    opaque = Image.alpha_composite(_background_layer(), artwork).convert("RGB")

    report = check_transparent_logo_sync.compare_renders(
        opaque,
        _artwork_layer(color=RED),
        background_colors=(CREAM, INK),
    )

    assert not report.in_sync
    assert report.divergent_pixels == (ARTWORK_BOX[2] - ARTWORK_BOX[0]) * (
        ARTWORK_BOX[3] - ARTWORK_BOX[1]
    )
    assert report.divergent_fraction == report.divergent_pixels / (SIZE * SIZE)
    assert report.divergent_bbox == ARTWORK_BOX
    assert report.max_margin > check_transparent_logo_sync.DEFAULT_TOLERANCE


def test_compare_renders_flags_opaque_only_artwork_removal():
    artwork = _artwork_layer()
    background = _background_layer()
    ImageDraw.Draw(background).rectangle((24, 24, 30, 30), fill=RED + (255,))
    opaque = Image.alpha_composite(background, artwork).convert("RGB")

    report = check_transparent_logo_sync.compare_renders(
        opaque,
        artwork,
        background_colors=(CREAM, INK),
    )

    assert not report.in_sync
    assert report.divergent_pixels == 7 * 7
    assert report.divergent_bbox == (24, 24, 31, 31)


def test_compare_renders_excludes_background_blend_transitions_only_when_transparent():
    background = _background_layer()
    blend = tuple((left + right) // 2 for left, right in zip(CREAM, INK))
    background.putpixel((2, 2), blend + (255,))
    artwork = _artwork_layer()
    opaque = Image.alpha_composite(background, artwork).convert("RGB")

    excluded = check_transparent_logo_sync.compare_renders(
        opaque,
        artwork,
        background_colors=(CREAM, INK),
    )
    assert excluded.in_sync
    assert excluded.excluded_blend_pixels == 1

    red_canvas = Image.alpha_composite(
        background, Image.new("RGBA", (SIZE, SIZE), RED + (255,))
    ).convert("RGB")
    counted = check_transparent_logo_sync.compare_renders(
        red_canvas,
        artwork,
        background_colors=(CREAM, INK),
    )
    assert not counted.in_sync
    assert counted.excluded_blend_pixels == 0


def test_corner_background_returns_uniform_canvas_color():
    opaque = Image.new("RGB", (SIZE, SIZE), CREAM)
    assert check_transparent_logo_sync._corner_background(opaque) == CREAM


def test_corner_background_refuses_artwork_reaching_a_corner():
    opaque = Image.new("RGB", (SIZE, SIZE), CREAM)
    ImageDraw.Draw(opaque).rectangle((0, 0, 3, 3), fill=RED)
    assert check_transparent_logo_sync._corner_background(opaque) is None


# ---- parsing and trigger status ---------------------------------------------


def test_parse_background_color_accepts_hashed_and_bare_hex():
    assert check_transparent_logo_sync.parse_background_color("#EFDDCB") == CREAM
    assert check_transparent_logo_sync.parse_background_color("0a0a08") == INK


@pytest.mark.parametrize("text", ["", "#FFF", "EFDDCB00", "#ZZZZZZ"])
def test_parse_background_color_rejects_malformed_values(text):
    with pytest.raises(check_transparent_logo_sync.TransparentLogoSyncError):
        check_transparent_logo_sync.parse_background_color(text)


def test_parse_svg_dimensions_reads_declared_size():
    assert check_transparent_logo_sync.parse_svg_dimensions(
        OPAQUE_SVG.encode("utf-8")
    ) == (64, 64)


def test_parse_svg_dimensions_returns_none_without_declared_size():
    assert check_transparent_logo_sync.parse_svg_dimensions(b"<svg></svg>") is None
    assert (
        check_transparent_logo_sync.parse_svg_dimensions(
            b'<svg width="abc" height="64"></svg>'
        )
        is None
    )


def test_trigger_status_reads_candidate_side_of_each_change_kind():
    def status_for(change):
        return check_transparent_logo_sync.trigger_status(
            [change], "source/logo.svg"
        )

    assert status_for(PathChange("M", ("source/logo.svg",))) == "M"
    assert status_for(PathChange("A", ("source/logo.svg",))) == "A"
    assert status_for(PathChange("R100", ("source/old.svg", "source/logo.svg"))) == "R"
    assert status_for(PathChange("C100", ("source/old.svg", "source/logo.svg"))) == "C"
    assert status_for(PathChange("D", ("source/logo.svg",))) == "D"
    assert (
        status_for(PathChange("R100", ("source/logo.svg", "source/moved.svg")))
        == "D"
    )
    assert status_for(PathChange("M", ("source/other.svg",))) is None


# ---- CLI behaviour -----------------------------------------------------------


def _git(tree: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(tree), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _install_resvg_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "resvg-stub"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "arguments = sys.argv[1:]\n"
        "width = int(arguments[arguments.index('--width') + 1])\n"
        "height = int(arguments[arguments.index('--height') + 1])\n"
        "source = Path(arguments[-2]).read_text()\n"
        "if 'data-fill' not in source:\n"
        "    Image.new('RGBA', (width, height), (0, 0, 0, 0)).save(arguments[-1])\n"
        "    raise SystemExit(0)\n"
        "fill = source.split('data-fill=\"')[1].split('\"')[0]\n"
        "red, green, blue = (int(value) for value in fill.split(','))\n"
        "if 'data-alpha' in source:\n"
        "    Image.new('RGBA', (width, height), (red, green, blue, 255)).save(arguments[-1])\n"
        "else:\n"
        "    Image.new('RGB', (width, height), (red, green, blue)).save(arguments[-1])\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _sync_repo(
    tmp_path: Path,
    *,
    opaque_fill: str = "200,10,10",
    transparent_fill: str = "200,10,10",
    include_transparent: bool = True,
) -> Path:
    tree = tmp_path / "repo"
    (tree / "source").mkdir(parents=True)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("200,10,10", opaque_fill), encoding="utf-8"
    )
    if include_transparent:
        (tree / "source/logo-transparent.svg").write_text(
            TRANSPARENT_SVG.replace("200,10,10", transparent_fill), encoding="utf-8"
        )
    (tree / "README.md").write_text("# repo\n", encoding="utf-8")

    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "source", "README.md")
    _git(tree, "commit", "-qm", "baseline")
    _git(tree, "tag", "base")
    return tree


def _run_cli(tree: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(tree), "--base", "base", *extra],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_skips_comparison_when_opaque_master_is_unchanged(tmp_path):
    tree = _sync_repo(tmp_path)

    result = _run_cli(tree, "--resvg", str(tmp_path / "does-not-exist"))

    assert result.returncode == 0
    assert "not-triggered" in result.stdout
    assert "does not fire" in result.stdout


def test_cli_reports_in_sync_for_triggered_matching_designs(tmp_path):
    tree = _sync_repo(tmp_path)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("</svg>", "<!-- reviewed: byte-only touch --></svg>"),
        encoding="utf-8",
    )
    _git(tree, "commit", "-qam", "byte-only opaque touch")
    stub = _install_resvg_stub(tmp_path)

    result = _run_cli(tree, "--resvg", str(stub))

    assert result.returncode == 0
    assert "TRIGGER transparent-logo-sync" in result.stdout
    assert "status M" in result.stdout
    assert "in-sync" in result.stdout
    assert "divergent=0/" in result.stdout


def test_cli_fails_when_design_diverges_without_transparent_change(tmp_path):
    tree = _sync_repo(tmp_path)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("200,10,10", "10,10,200"), encoding="utf-8"
    )
    _git(tree, "commit", "-qam", "opaque redesign")
    stub = _install_resvg_stub(tmp_path)

    result = _run_cli(tree, "--resvg", str(stub))

    assert result.returncode == 1
    assert "divergent=" in result.stdout
    assert "diverges from the rendered opaque design" in result.stdout
    assert "update source/logo-transparent.svg" in result.stdout


def test_cli_accepts_same_diff_transparent_change_as_the_decision(tmp_path):
    tree = _sync_repo(tmp_path)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("200,10,10", "10,10,200"), encoding="utf-8"
    )
    (tree / "source/logo-transparent.svg").write_text(
        TRANSPARENT_SVG.replace("200,10,10", "250,120,40"), encoding="utf-8"
    )
    _git(tree, "commit", "-qam", "deliberate transparent-variant decision")
    stub = _install_resvg_stub(tmp_path)

    result = _run_cli(tree, "--resvg", str(stub))

    assert result.returncode == 0
    assert "REVIEW transparent-logo-sync" in result.stdout
    assert "divergence acknowledged" in result.stdout


def test_cli_fails_when_transparent_master_is_missing_from_candidate(tmp_path):
    tree = _sync_repo(tmp_path, include_transparent=False)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("200,10,10", "10,10,200"), encoding="utf-8"
    )
    _git(tree, "commit", "-qam", "opaque redesign without transparent master")

    result = _run_cli(tree, "--resvg", str(tmp_path / "unused"))

    assert result.returncode == 1
    assert "not present in the candidate tree" in result.stdout


def test_cli_fails_closed_without_resvg(tmp_path):
    tree = _sync_repo(tmp_path)
    (tree / "source/logo.svg").write_text(
        OPAQUE_SVG.replace("200,10,10", "10,10,200"), encoding="utf-8"
    )
    _git(tree, "commit", "-qam", "opaque redesign")

    result = _run_cli(tree, "--resvg", str(tmp_path / "missing-resvg"))

    assert result.returncode == 1
    assert "resvg is not available" in result.stderr


def test_cli_rejects_malformed_background_colors(tmp_path):
    tree = _sync_repo(tmp_path)

    result = _run_cli(tree, "--background-color", "nope")

    assert result.returncode == 1
    assert "invalid background color" in result.stderr


def test_release_docs_define_the_manual_sync_review_path():
    command = ".venv/bin/python tools/check_transparent_logo_sync.py --base"
    for relative in (
        "README.md",
        "docs/notes/asset-toolchain.md",
        "docs/notes/release-publication.md",
        "docs/plan/plan.md",
    ):
        document = (ROOT / relative).read_text(encoding="utf-8")
        assert command in document
        assert "never" in document and "generat" in document


def test_release_docs_tie_the_sync_review_to_the_release_preflight():
    # The preflight is the enforcement point: a triggered opaque redesign must
    # fail there until the transparent-variant decision is recorded, so the
    # release docs must tie the check to the preflight rather than leave it as
    # an optional second command.
    for relative in (
        "README.md",
        "docs/notes/asset-toolchain.md",
        "docs/notes/release-publication.md",
        "docs/plan/plan.md",
    ):
        document = (ROOT / relative).read_text(encoding="utf-8")
        assert "check_release_preflight.py" in document


# ---- committed masters (real pinned resvg when available) -------------------


requires_resvg = pytest.mark.skipif(
    shutil.which("resvg") is None,
    reason="pinned resvg is not installed",
)


def _committed_masters_repo(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    (tree / "source").mkdir(parents=True)
    (tree / "source/logo.svg").write_bytes((ROOT / "source/logo.svg").read_bytes())
    (tree / "source/logo-transparent.svg").write_bytes(
        (ROOT / "source/logo-transparent.svg").read_bytes()
    )
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "source")
    _git(tree, "commit", "-qm", "baseline")
    _git(tree, "tag", "base")
    return tree


@requires_resvg
def test_committed_masters_stay_in_sync_across_byte_only_change(tmp_path):
    tree = _committed_masters_repo(tmp_path)
    candidate = (tree / "source/logo.svg").read_text(encoding="utf-8").replace(
        "</svg>", "<!-- byte-only review marker -->\n</svg>"
    )
    (tree / "source/logo.svg").write_text(candidate, encoding="utf-8")
    _git(tree, "commit", "-qam", "byte-only opaque change")

    result = _run_cli(tree)

    assert result.returncode == 0
    assert "in-sync" in result.stdout
    assert "divergent=0/" in result.stdout


@requires_resvg
def test_committed_masters_detect_a_real_redesign(tmp_path):
    tree = _committed_masters_repo(tmp_path)
    candidate = (tree / "source/logo.svg").read_text(encoding="utf-8").replace(
        "#F4AF78", "#FF0000", 1
    )
    (tree / "source/logo.svg").write_text(candidate, encoding="utf-8")
    _git(tree, "commit", "-qam", "opaque redesign")

    result = _run_cli(tree)

    assert result.returncode == 1
    assert "divergent=" in result.stdout
    assert not result.stdout.strip().startswith("PASS")
