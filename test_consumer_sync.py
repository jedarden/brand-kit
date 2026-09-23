import io
import sys
from pathlib import Path

from PIL import Image

from tools import consumer_sync


def png_bytes(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def make_site(tmp_path):
    site = tmp_path / "jedarden.com"
    (site / "public/brand").mkdir(parents=True)
    (site / "src/assets").mkdir(parents=True)
    return site


def write_synced_derivatives(site):
    for width, height, fy, site_rel, _ in consumer_sync.HERO_DERIVATIVES:
        destination = site / site_rel
        consumer_sync.hero_crop(width, height, fy).save(
            destination, "JPEG", quality=consumer_sync.JPEG_QUALITY
        )


def test_logo_copies_require_exact_bytes_and_apply_refreshes_only_drift(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "brand-kit"
    site = make_site(tmp_path)
    (root / "logo").mkdir(parents=True)
    svg = b"<svg>current\x00release</svg>"
    png = b"current-png-bytes\xff"
    (root / "logo/logo.svg").write_bytes(svg)
    (root / "logo/logo-512.png").write_bytes(png)
    (site / "public/brand/logo.svg").write_bytes(svg)
    (site / "public/brand/logo-512.png").write_bytes(b"stale")
    monkeypatch.setattr(consumer_sync, "ROOT", root)

    assert consumer_sync.check_copies(site, apply=False) is False
    check_output = capsys.readouterr().out
    assert "PASS  public/brand/logo.svg: byte-identical" in check_output
    assert "STALE public/brand/logo-512.png: differs" in check_output
    assert (site / "public/brand/logo-512.png").read_bytes() == b"stale"

    writes = []
    original_write_bytes = Path.write_bytes

    def record_write_bytes(path, data):
        writes.append(path)
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", record_write_bytes)

    assert consumer_sync.check_copies(site, apply=True) is True
    apply_output = capsys.readouterr().out
    assert "PASS  public/brand/logo.svg: byte-identical" in apply_output
    assert "SYNC  public/brand/logo-512.png: refreshed" in apply_output
    assert writes == [site / "public/brand/logo-512.png"]
    assert (site / "public/brand/logo.svg").read_bytes() == svg
    assert (site / "public/brand/logo-512.png").read_bytes() == png

    (site / "public/brand/logo-512.png").unlink()
    assert consumer_sync.check_copies(site, apply=True) is False
    assert not (site / "public/brand/logo-512.png").exists()


def test_apply_regenerates_documented_jpeg_derivatives(tmp_path, monkeypatch, capsys):
    site = make_site(tmp_path)
    for width, height, _, site_rel, _ in consumer_sync.HERO_DERIVATIVES:
        Image.new("RGB", (width, height), (0, 0, 0)).save(site / site_rel, "JPEG")

    crop_calls = []
    original_crop = Image.Image.crop

    def record_crop(image, box=None):
        crop_calls.append((image.size, box))
        return original_crop(image, box)

    save_calls = []
    original_save = Image.Image.save

    def record_save(image, destination, format=None, **kwargs):
        save_calls.append((image.size, format, kwargs))
        return original_save(image, destination, format, **kwargs)

    monkeypatch.setattr(Image.Image, "crop", record_crop)
    monkeypatch.setattr(Image.Image, "save", record_save)

    assert consumer_sync.check_hero_derivatives(site, apply=True) is True
    assert crop_calls == [
        ((1200, 800), (0, 76, 1200, 706)),
        ((1536, 1024), (0, 0, 1536, 1024)),
    ]
    assert save_calls == [
        ((1200, 630), "JPEG", {"quality": 88}),
        ((1536, 1024), "JPEG", {"quality": 88}),
    ]
    output = capsys.readouterr().out
    assert "SYNC  public/brand/og.jpg" in output
    assert "SYNC  src/assets/brand-hero.jpg" in output

    with Image.open(site / "public/brand/og.jpg") as image:
        og = image.convert("RGB")
    with Image.open(consumer_sync.ROOT / "banners/open-graph-1200x630.png") as image:
        banner = image.convert("RGB")
    with Image.open(site / "src/assets/brand-hero.jpg") as image:
        hero = image.convert("RGB")
    assert og.size == (1200, 630)
    assert hero.size == (1536, 1024)
    assert consumer_sync.mean_luma_diff(og, banner) <= consumer_sync.JPEG_TOLERANCE
    assert consumer_sync.mean_luma_diff(
        hero, consumer_sync.hero_crop(1536, 1024, 0.0)
    ) <= consumer_sync.JPEG_TOLERANCE


def test_apply_leaves_in_sync_hero_files_untouched(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    write_synced_derivatives(site)
    paths = [site / site_rel for _, _, _, site_rel, _ in consumer_sync.HERO_DERIVATIVES]
    before = {path: path.read_bytes() for path in paths}
    save_calls = []
    original_save = Image.Image.save

    def record_save(image, destination, format=None, **kwargs):
        save_calls.append((image.size, format, kwargs))
        return original_save(image, destination, format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", record_save)

    assert consumer_sync.check_hero_derivatives(site, apply=True) is True
    assert save_calls == []
    assert {path: path.read_bytes() for path in paths} == before


def test_avatar_luma_tolerance_is_inclusive_at_eight(tmp_path, monkeypatch, capsys):
    root = tmp_path / "brand-kit"
    (root / "avatars").mkdir(parents=True)
    reference = Image.new("RGB", (20, 20), (100, 100, 100))
    reference.save(root / "avatars/github-460.png", "PNG")
    monkeypatch.setattr(consumer_sync, "ROOT", root)

    monkeypatch.setattr(
        consumer_sync,
        "fetch",
        lambda url: png_bytes(Image.new("RGB", (20, 20), (108, 108, 108))),
    )
    assert consumer_sync.check_live_avatar(offline=False) is True
    assert "PASS  github avatar" in capsys.readouterr().out

    monkeypatch.setattr(
        consumer_sync,
        "fetch",
        lambda url: png_bytes(Image.new("RGB", (20, 20), (109, 109, 109))),
    )
    assert consumer_sync.check_live_avatar(offline=False) is False
    assert "STALE github avatar" in capsys.readouterr().out


def test_live_checks_report_pass_only_after_successful_fetch(tmp_path, monkeypatch, capsys):
    root = tmp_path / "brand-kit"
    site = make_site(tmp_path)
    (root / "avatars").mkdir(parents=True)
    reference = Image.new("RGB", (20, 20), (100, 100, 100))
    reference.save(root / "avatars/github-460.png", "PNG")
    reference.save(site / "public/brand/og.jpg", "JPEG")
    monkeypatch.setattr(consumer_sync, "ROOT", root)
    monkeypatch.setattr(consumer_sync, "fetch", lambda url: png_bytes(reference))

    assert consumer_sync.check_live_avatar(offline=False) is True
    assert consumer_sync.check_live_og(site, offline=False) is True
    output = capsys.readouterr().out
    assert "PASS  github avatar" in output
    assert "PASS  live og.jpg" in output


def test_check_skips_live_resources_on_fetch_failure(tmp_path, monkeypatch, capsys):
    site = make_site(tmp_path)
    for repo_rel, site_rel, _ in consumer_sync.COPIES:
        (site / site_rel).write_bytes((consumer_sync.ROOT / repo_rel).read_bytes())
    write_synced_derivatives(site)

    def fail_fetch(url):
        raise TimeoutError("network unavailable")

    monkeypatch.setattr(consumer_sync, "fetch", fail_fetch)
    monkeypatch.setattr(
        sys, "argv", ["consumer_sync.py", "--check", "--site", str(site)]
    )

    assert consumer_sync.main() == 0
    output = capsys.readouterr().out
    assert "SKIP  github avatar" in output
    assert "SKIP  live og.jpg" in output
    assert "PASS  github avatar" not in output
    assert "PASS  live og.jpg" not in output
