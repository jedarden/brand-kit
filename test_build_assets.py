import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from tools import build_assets


HERO_CROP_FIXTURES = {
    "banners/open-graph-1200x630.png": (
        (1200, 630, 0.45),
        (1200, 800),
        (0, 76, 1200, 706),
        "cab9fb861fca53d2fd0a7d8e43e48b7872f9da9a2d9fb3c17ada6c1ff1d58fb6",
    ),
    "banners/twitter-card-1200x628.png": (
        (1200, 628, 0.45),
        (1200, 800),
        (0, 77, 1200, 705),
        "45ba755d800e0dc6662dd4a697341c6619d2b733de5a4274fee673d233f1da6d",
    ),
    "banners/youtube-banner-2560x1440.png": (
        (2560, 1440, 0.45),
        (2560, 1707),
        (0, 120, 2560, 1560),
        "1d8281d77fc0258e289d8abc8570cf4c41c3bcab48ebcea98c26644c68586713",
    ),
}


@pytest.fixture
def asset_fixture(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    src = root / "source"
    src.mkdir(parents=True)
    logo_svg = src / "logo.svg"
    logo_transparent_svg = src / "logo-transparent.svg"
    logo_png = src / "logo.png"
    hero = src / "hero.png"
    logo_svg.write_bytes(b"<svg>opaque</svg>")
    logo_transparent_svg.write_bytes(b"<svg>transparent</svg>")
    Image.new("RGB", (3, 2), "#101820").save(logo_png)
    Image.new("RGB", (4, 3), "#264653").save(hero)

    monkeypatch.setattr(build_assets, "ROOT", root)
    monkeypatch.setattr(build_assets, "SRC", src)
    monkeypatch.setattr(build_assets, "LOGO_SVG", logo_svg)
    monkeypatch.setattr(build_assets, "LOGO_TRANSPARENT_SVG", logo_transparent_svg)
    monkeypatch.setattr(build_assets, "LOGO_PNG", logo_png)
    monkeypatch.setattr(build_assets, "HERO", hero)
    monkeypatch.setattr(
        build_assets,
        "REQUIRED_SOURCES",
        (logo_svg, logo_transparent_svg, hero, logo_png),
    )
    monkeypatch.setattr(build_assets, "AVATARS", {"avatars/avatar.png": 9})
    monkeypatch.setattr(build_assets, "LOGO_SIZES", {"logo/logo-9.png": 9})
    monkeypatch.setattr(build_assets, "FAVICON_SIZES", {"favicon/favicon-9.png": 9})
    monkeypatch.setattr(build_assets, "BANNERS", {"banners/banner.png": (10, 6, 0.25)})
    resvg = str(tmp_path / "resvg")
    monkeypatch.setattr(build_assets.shutil, "which", lambda executable: resvg)
    return SimpleNamespace(
        root=root,
        src=src,
        logo_svg=logo_svg,
        logo_transparent_svg=logo_transparent_svg,
        logo_png=logo_png,
        hero=hero,
        resvg=resvg,
    )


def install_renderer(monkeypatch, fixture):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return SimpleNamespace(stdout="resvg 0.47.0\n")
        source = Path(command[-2])
        output = Path(command[-1])
        width = int(command[command.index("--width") + 1])
        height = int(command[command.index("--height") + 1])
        if source == fixture.logo_transparent_svg:
            Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(output)
        else:
            Image.new("RGB", (width, height), "#DC3127").save(output)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(build_assets.subprocess, "run", run)
    return calls


def test_build_generates_from_authoritative_fixture_sources(
    monkeypatch, asset_fixture, capsys
):
    calls = install_renderer(monkeypatch, asset_fixture)
    png_saves = []
    original_save = Image.Image.save

    def record_save(image, destination, format=None, **kwargs):
        if isinstance(destination, (str, Path)):
            path = Path(destination)
            if asset_fixture.root in path.parents and path.suffix == ".png":
                png_saves.append((path.relative_to(asset_fixture.root), kwargs))
        return original_save(image, destination, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", record_save)

    build_assets.main()

    assert calls[0] == ([asset_fixture.resvg, "--version"], {
        "capture_output": True,
        "text": True,
        "check": True,
    })
    render_calls = calls[1:]
    assert [Path(call[0][-2]) for call in render_calls] == [
        asset_fixture.logo_svg,
        asset_fixture.logo_svg,
        asset_fixture.logo_transparent_svg,
        asset_fixture.logo_svg,
        asset_fixture.logo_svg,
    ]
    for command, kwargs in render_calls:
        assert command[0] == asset_fixture.resvg
        assert command[1] == "--width"
        assert command[3] == "--height"
        assert kwargs == {
            "check": True,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        assert str(asset_fixture.logo_png) not in command

    expected_pngs = {
        Path("avatars/avatar.png"),
        Path("logo/logo-9.png"),
        Path("logo/logo-9-transparent.png"),
        Path("logo/logo-original.png"),
        Path("favicon/favicon-9.png"),
        Path("banners/banner.png"),
    }
    assert {path for path, _ in png_saves} == expected_pngs
    assert all(options == {"optimize": True} for _, options in png_saves)
    assert (asset_fixture.root / "favicon/favicon.ico").is_file()
    assert (asset_fixture.root / "logo/logo.svg").read_bytes() == (
        asset_fixture.logo_svg.read_bytes()
    )
    assert (asset_fixture.root / "logo/logo-transparent.svg").read_bytes() == (
        asset_fixture.logo_transparent_svg.read_bytes()
    )
    assert (asset_fixture.root / "palette.json").read_text(encoding="utf-8") == (
        json.dumps(build_assets.PALETTE, indent=2) + "\n"
    )
    assert not (asset_fixture.src / "logo.svg.sha256").exists()
    with Image.open(asset_fixture.root / "avatars/avatar.png") as avatar:
        assert avatar.convert("RGB").getpixel((0, 0)) == (220, 49, 39)
    with Image.open(
        asset_fixture.root / "logo/logo-9-transparent.png"
    ) as transparent:
        assert transparent.getpixel((0, 0))[3] == 0
    with Image.open(asset_fixture.root / "banners/banner.png") as banner:
        assert banner.convert("RGB").getpixel((0, 0)) == (38, 70, 83)
    assert "logo source: vector (resvg)" in capsys.readouterr().out


def test_documented_hero_crops_match_committed_fixtures(monkeypatch):
    expected_settings = {
        path: fixture[0] for path, fixture in HERO_CROP_FIXTURES.items()
    }
    assert {
        path: build_assets.BANNERS[path] for path in HERO_CROP_FIXTURES
    } == expected_settings
    assert build_assets.HERO == build_assets.ROOT / "source/hero.png"
    saved_banners = {}
    crop_calls = []
    original_crop = Image.Image.crop

    def record_crop(image, box=None):
        crop_calls.append((image.size, box))
        return original_crop(image, box)

    def record_save(image, relpath):
        saved_banners[relpath] = image

    monkeypatch.setattr(Image.Image, "crop", record_crop)
    monkeypatch.setattr(build_assets, "BANNERS", expected_settings)
    monkeypatch.setattr(build_assets, "save", record_save)
    with Image.open(build_assets.HERO) as source:
        hero = source.convert("RGB")
    build_assets.save_banners(hero)

    assert list(saved_banners) == list(HERO_CROP_FIXTURES)
    assert crop_calls == [
        (fixture[1], fixture[2]) for fixture in HERO_CROP_FIXTURES.values()
    ]
    for asset_path, (_, _, _, pixel_sha256) in HERO_CROP_FIXTURES.items():
        actual = saved_banners[asset_path]
        with Image.open(build_assets.ROOT / asset_path) as fixture_image:
            expected = fixture_image.convert("RGB")

        actual_sha256 = hashlib.sha256(actual.tobytes()).hexdigest()
        assert actual_sha256 == pixel_sha256, (
            f"{asset_path} changed; review the new platform crop before replacing "
            f"its fixture digest with {actual_sha256}"
        )
        assert actual.size == expected.size
        assert ImageChops.difference(actual, expected).getbbox() is None


def test_build_aborts_without_raster_fallback_when_resvg_is_missing(
    monkeypatch, asset_fixture
):
    monkeypatch.setattr(build_assets.shutil, "which", lambda executable: None)
    monkeypatch.setattr(
        build_assets.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("no command may run without resvg"),
    )

    with pytest.raises(SystemExit, match="resvg not found on PATH") as error:
        build_assets.main()

    assert "no raster fallback" in str(error.value)
    assert not (asset_fixture.root / "palette.json").exists()


@pytest.mark.parametrize(
    "source_name", ["logo.svg", "logo-transparent.svg", "hero.png", "logo.png"]
)
def test_build_aborts_when_any_canonical_source_is_missing(
    monkeypatch, asset_fixture, source_name
):
    (asset_fixture.src / source_name).unlink()
    monkeypatch.setattr(
        build_assets.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("no command may run with missing sources"),
    )

    with pytest.raises(
        SystemExit, match=f"required source {source_name} not found"
    ) as error:
        build_assets.main()

    assert "no fallback path" in str(error.value)
    assert not (asset_fixture.root / "palette.json").exists()


def test_build_propagates_renderer_failure_instead_of_using_raster(
    monkeypatch, asset_fixture
):
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        if command[1:] == ["--version"]:
            return SimpleNamespace(stdout="resvg 0.47.0\n")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_assets.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        build_assets.main()

    assert [Path(command[-2]) for command in calls[1:]] == [
        asset_fixture.logo_svg
    ]
    assert not (asset_fixture.root / "avatars/avatar.png").exists()
    assert not (asset_fixture.root / "logo/logo-original.png").exists()


def test_build_propagates_toolchain_query_failure_before_output(
    monkeypatch, asset_fixture
):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_assets.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        build_assets.main()

    assert not (asset_fixture.root / "palette.json").exists()
