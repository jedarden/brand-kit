import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

from tools import consumer_drift, consumer_sync
from tools import consumer_drift_report


def image_bytes(image, image_format="PNG"):
    output = io.BytesIO()
    image.save(output, image_format)
    return output.getvalue()


def test_image_check_rejects_color_drift_with_equal_red_channel():
    expected = Image.new("RGB", (20, 20), (200, 10, 10))
    observed = Image.new("RGB", (20, 20), (200, 100, 100))

    result = consumer_drift._image_check(
        image_bytes(observed), expected, 1.0, "favicon fixture"
    )

    assert result["status"] == "stale"
    assert result["mean_luma"] > 1.0


def test_live_asset_can_extract_rotating_media_from_a_stable_public_page(tmp_path):
    root = make_root(tmp_path)
    observed = io.BytesIO()
    consumer_drift.hero_crop(root, 20, 20, 0.5).save(observed, "JPEG", quality=88)
    page_url = "https://profile.example/jed"
    media_url = "https://cdn.example/jed/banner-current.jpg"
    config = {
        "live_assets": [
            {
                "name": "profile banner",
                "consumer": "profile.example/jed",
                "source": "source/hero.png",
                "url": page_url,
                "media_url_pattern": r"https://cdn\.example/[^\" ]+",
                "comparison": "crop",
                "crop": {"width": 20, "height": 20, "fy": 0.5},
                "tolerance": 3.0,
            }
        ]
    }
    responses = {
        page_url: f'<meta content="{media_url}">'.encode(),
        media_url: observed.getvalue(),
    }

    checks = consumer_drift.audit_live_assets(
        root, config, fetcher=lambda url, headers=None: responses[url]
    )

    assert checks[0]["status"] == "current"
    assert checks[0]["url"] == page_url
    assert checks[0]["media_url"] == media_url


def test_live_asset_page_without_media_is_indeterminate(tmp_path):
    root = make_root(tmp_path)
    config = {
        "live_assets": [
            {
                "name": "profile banner",
                "consumer": "profile.example/jed",
                "source": "source/hero.png",
                "url": "https://profile.example/jed",
                "media_url_pattern": r"https://cdn\.example/[^\" ]+",
                "comparison": "crop",
                "crop": {"width": 20, "height": 20, "fy": 0.5},
            }
        ]
    }

    checks = consumer_drift.audit_live_assets(
        root,
        config,
        fetcher=lambda url, headers=None: b"<html>no media here</html>",
    )

    assert checks[0]["status"] == "unavailable"
    assert "did not expose media" in checks[0]["reason"]


def make_root(tmp_path):
    root = tmp_path / "brand-kit"
    (root / "source").mkdir(parents=True)
    (root / "logo").mkdir()
    (root / "avatars").mkdir()
    (root / "favicon").mkdir()
    (root / consumer_sync.REGISTRY_RELPATH).write_text(
        (consumer_sync.ROOT / consumer_sync.REGISTRY_RELPATH).read_text(),
        encoding="utf-8",
    )
    (root / "source/logo.svg").write_bytes(b"<svg>source</svg>")
    Image.new("RGB", (40, 30), (100, 120, 140)).save(root / "source/hero.png")
    (root / "logo/logo.svg").write_bytes(b"<svg>release</svg>")
    (root / "logo/logo-512.png").write_bytes(b"release-logo\x00")
    Image.new("RGB", (20, 20), (80, 90, 100)).save(root / "avatars/github-460.png")
    for path, size, color in (
        ("favicon/apple-touch-icon-180.png", 180, (110, 120, 130)),
        ("favicon/favicon-192.png", 192, (120, 130, 140)),
        ("favicon/favicon-512.png", 512, (130, 140, 150)),
    ):
        Image.new("RGB", (size, size), color).save(root / path)
    return root


def make_config():
    return {
        "schema_version": 1,
        "release": {
            "api_url": "https://forgejo.example/api/v1",
            "repository": "jedarden/brand-kit",
            "minimum_age_hours": 0,
            "require_checkout": False,
        },
        "site": {"name": "jedarden.com", "checkout": "unused"},
        "live": {},
        "source_assets": [
            {"path": "source/logo.svg"},
            {"path": "source/hero.png"},
            {"path": "avatars/github-460.png"},
            {"path": "logo/logo-512.png"},
            {"path": "logo/logo.svg"},
            {"path": "favicon/apple-touch-icon-180.png"},
            {"path": "favicon/favicon-192.png"},
            {"path": "favicon/favicon-512.png"},
        ],
        "site_assets": [
            {
                "name": "logo.svg",
                "source": "logo/logo.svg",
                "path": "public/brand/logo.svg",
                "comparison": "bytes",
            },
            {
                "name": "logo-512.png",
                "source": "logo/logo-512.png",
                "path": "public/brand/logo-512.png",
                "comparison": "bytes",
            },
            {
                "name": "favicon.svg",
                "source": "logo/logo.svg",
                "path": "public/favicon.svg",
                "comparison": "bytes",
            },
            {
                "name": "apple-touch-icon.png",
                "source": "favicon/apple-touch-icon-180.png",
                "path": "public/apple-touch-icon.png",
                "comparison": "image",
                "tolerance": 1,
            },
            {
                "name": "icon-192.png",
                "source": "favicon/favicon-192.png",
                "path": "public/icon-192.png",
                "comparison": "image",
                "tolerance": 1,
            },
            {
                "name": "icon-512.png",
                "source": "favicon/favicon-512.png",
                "path": "public/icon-512.png",
                "comparison": "image",
                "tolerance": 1,
            },
            {
                "name": "og.jpg",
                "source": "source/hero.png",
                "path": "public/brand/og.jpg",
                "comparison": "crop",
                "tolerance": 10,
                "crop": {"width": 1200, "height": 630, "fy": 0.45},
            },
            {
                "name": "brand-hero.jpg",
                "source": "source/hero.png",
                "path": "src/assets/brand-hero.jpg",
                "comparison": "crop",
                "tolerance": 10,
                "crop": {"width": 1536, "height": 1024, "fy": 0.0},
            },
        ],
        "live_assets": [
            {
                "name": "github profile avatar",
                "consumer": "github.com/jedarden",
                "source": "avatars/github-460.png",
                "url": "https://avatars.example/jedarden",
                "comparison": "image",
                "tolerance": 10,
                "resize_reference": True,
            },
            {
                "name": "live open-graph card",
                "consumer": "jedarden.com live",
                "source": "source/hero.png",
                "url": "https://jedarden.example/brand/og.jpg",
                "comparison": "crop",
                "tolerance": 10,
                "crop": {"width": 1200, "height": 630, "fy": 0.45},
            },
        ],
    }


def make_site(root, tmp_path):
    site = tmp_path / "jedarden.com"
    (site / "public/brand").mkdir(parents=True)
    (site / "src/assets").mkdir(parents=True)
    (site / "public/brand/logo.svg").write_bytes((root / "logo/logo.svg").read_bytes())
    (site / "public/brand/logo-512.png").write_bytes(
        (root / "logo/logo-512.png").read_bytes()
    )
    (site / "public/favicon.svg").write_bytes((root / "logo/logo.svg").read_bytes())
    for source, destination in (
        ("favicon/apple-touch-icon-180.png", "public/apple-touch-icon.png"),
        ("favicon/favicon-192.png", "public/icon-192.png"),
        ("favicon/favicon-512.png", "public/icon-512.png"),
    ):
        (site / destination).write_bytes((root / source).read_bytes())
    for width, height, fy, relative in (
        (1200, 630, 0.45, "public/brand/og.jpg"),
        (1536, 1024, 0.0, "src/assets/brand-hero.jpg"),
    ):
        consumer_drift.hero_crop(root, width, height, fy).save(
            site / relative, "JPEG", quality=88
        )
    return site


def make_fetcher(root):
    avatar = (root / "avatars/github-460.png").read_bytes()
    og = io.BytesIO()
    consumer_drift.hero_crop(root, 1200, 630, 0.45).save(og, "JPEG", quality=88)
    responses = {
        "https://forgejo.example/api/v1/repos/jedarden/brand-kit/releases/tags/v1.0.0": json.dumps(
            {
                "tag_name": "v1.0.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-09-01T00:00:00Z",
            }
        ).encode(),
        "https://avatars.example/jedarden": avatar,
        "https://jedarden.example/brand/og.jpg": og.getvalue(),
    }

    def fetcher(url, headers=None):
        return responses[url]

    return fetcher


def invoke_main(
    monkeypatch, capsys, root, site, fetch_bytes, extra_args=(), config=None
):
    config = make_config() if config is None else config
    monkeypatch.setattr(consumer_drift, "load_config", lambda path: config)
    monkeypatch.setattr(consumer_drift, "fetch_bytes", fetch_bytes)
    arguments = [
        "--source-root",
        str(root),
        "--site",
        str(site),
        "--release-tag",
        "v1.0.0",
        "--json",
        *extra_args,
    ]
    exit_code = consumer_drift.main(arguments)
    return exit_code, json.loads(capsys.readouterr().out)


def test_fetch_release_resolves_an_exact_published_tag_with_auth():
    calls = []
    record = {
        "tag_name": "v1.0.0",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-01T00:00:00Z",
    }

    def fetcher(url, headers=None):
        calls.append((url, headers))
        return json.dumps(record).encode()

    resolved = consumer_drift.fetch_release(
        "v1.0.0", make_config(), token="read-only-token", fetcher=fetcher
    )

    assert resolved == record
    assert calls == [
        (
            "https://forgejo.example/api/v1/repos/jedarden/brand-kit/releases/tags/v1.0.0",
            {"Authorization": "token read-only-token"},
        )
    ]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {"tag_name": "v1.0.0", "draft": True, "prerelease": False},
            "not published",
        ),
        (
            {"tag_name": "v1.0.0", "draft": False, "prerelease": True},
            "prerelease",
        ),
        (
            {"tag_name": "v1.1.0", "draft": False, "prerelease": False},
            "expected 'v1.0.0'",
        ),
    ],
)
def test_fetch_release_rejects_non_stable_or_mismatched_records(record, message):
    with pytest.raises(consumer_drift.ReleaseError, match=message):
        consumer_drift.fetch_release(
            "v1.0.0",
            make_config(),
            fetcher=lambda url, headers=None: json.dumps(record).encode(),
        )


def test_run_audit_resolves_the_newest_published_release_when_tag_is_omitted(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    records = [
        {
            "tag_name": "nightly",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-24T00:00:00Z",
        },
        {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-09-23T00:00:00Z",
        },
        {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-22T00:00:00Z",
        },
        {
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-01T00:00:00Z",
        },
    ]

    def fetcher(url, headers=None):
        if "releases?limit=50" in url:
            return json.dumps(records).encode()
        return make_fetcher(root)(url, headers)

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        fetcher=fetcher,
    )

    assert report["status"] == "current"
    assert report["release"]["tag"] == "v1.1.0"


def test_source_digests_are_exact_hashes_from_the_requested_source_root(tmp_path):
    root = make_root(tmp_path / "release")
    site = make_site(root, tmp_path)
    (root / "source/logo.svg").write_bytes(b"canonical source bytes\x00")
    config = make_config()

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=config,
        release_tag="v1.0.0",
        fetcher=make_fetcher(root),
    )

    expected = [
        {
            "path": entry["path"],
            "role": entry.get("role", "source"),
            "sha256": hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest(),
        }
        for entry in config["source_assets"]
    ]
    assert report["source_digests"] == expected
    assert report["site"]["path"] == str(site.resolve())


def test_run_audit_reports_current_without_writing_consumers(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    config = make_config()
    before = {path: path.read_bytes() for path in site.rglob("*") if path.is_file()}

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=config,
        release_tag="v1.0.0",
        fetcher=make_fetcher(root),
    )

    assert report["status"] == "current"
    assert report["release"]["tag"] == "v1.0.0"
    assert report["consumers"]["jedarden.com"]["status"] == "current"
    assert report["consumers"]["github.com/jedarden"]["status"] == "current"
    assert all(check["status"] == "current" for check in report["checks"])
    assert {
        path: path.read_bytes() for path in site.rglob("*") if path.is_file()
    } == before
    assert all(len(item["sha256"]) == 64 for item in report["source_digests"])


def test_release_to_consumer_refresh_only_remediation_writes(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    git_identity = [
        "-c",
        "user.name=Brand Kit Test",
        "-c",
        "user.email=brand-kit@example.invalid",
    ]
    git_commands = (
        ["git", "init", "--quiet", str(root)],
        ["git", "-C", str(root), "add", "--all"],
        [
            "git",
            "-C",
            str(root),
            *git_identity,
            "commit",
            "--quiet",
            "-m",
            "published release",
        ],
        [
            "git",
            "-C",
            str(root),
            *git_identity,
            "tag",
            "--annotate",
            "v1.0.0",
            "--message",
            "v1.0.0",
        ],
    )
    for command in git_commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr

    site = make_site(root, tmp_path)
    for relative in ("public/brand/logo.svg", "public/brand/logo-512.png"):
        (site / relative).write_bytes(b"stale consumer copy")
    for width, height, _, relative, _ in consumer_sync.HERO_DERIVATIVES:
        Image.new("RGB", (width, height), (0, 0, 0)).save(
            site / relative, "JPEG"
        )
    sentinel = site / "unrelated.txt"
    sentinel.write_bytes(b"must not change")

    config = make_config()
    config["release"]["require_checkout"] = True
    release_body = json.dumps(
        {
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-01T00:00:00Z",
        }
    ).encode()
    base_fetcher = make_fetcher(root)
    calls = []
    detector_release_url = consumer_drift.release_url("v1.0.0", config)
    sync_release_url = consumer_sync.forgejo_release_url("v1.0.0")

    def fetcher(url, headers=None):
        calls.append(url)
        if url in {detector_release_url, sync_release_url}:
            return release_body
        return base_fetcher(url, headers)

    monkeypatch.setattr(consumer_sync, "ROOT", root)
    monkeypatch.setattr(consumer_sync, "fetch", fetcher)

    def consumer_snapshot():
        return {
            path.relative_to(site): path.read_bytes()
            for path in site.rglob("*")
            if path.is_file()
        }

    protected = {
        relative: (site / relative).read_bytes()
        for relative in (
            "public/favicon.svg",
            "public/apple-touch-icon.png",
            "public/icon-192.png",
            "public/icon-512.png",
            "unrelated.txt",
        )
    }
    before = consumer_snapshot()
    stage = "detect"
    write_events = []
    original_write_bytes = Path.write_bytes
    original_write_text = Path.write_text
    original_save = Image.Image.save

    def record_write_bytes(path, data):
        write_events.append((stage, "write_bytes", path))
        return original_write_bytes(path, data)

    def record_write_text(path, *args, **kwargs):
        write_events.append((stage, "write_text", path))
        return original_write_text(path, *args, **kwargs)

    def record_image_save(image, destination, *args, **kwargs):
        write_events.append((stage, "image.save", Path(destination)))
        return original_save(image, destination, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", record_write_bytes)
    monkeypatch.setattr(Path, "write_text", record_write_text)
    monkeypatch.setattr(Image.Image, "save", record_image_save)

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, fetcher, config=config
    )

    assert exit_code == 1
    assert report["status"] == "stale"
    assert report["release"]["tag"] == "v1.0.0"
    assert report["release"]["checkout"]["matches"] is True
    assert (
        report["release"]["checkout"]["head"]
        == report["release"]["checkout"]["tag_commit"]
    )
    assert {
        check["asset"] for check in report["checks"] if check["status"] == "stale"
    } == {"logo.svg", "logo-512.png", "og.jpg", "brand-hero.jpg"}
    assert all(
        check["status"] == "current" for check in report["checks"] if "url" in check
    )
    assert write_events == []
    assert consumer_snapshot() == before

    stage = "remediate"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "consumer_sync.py",
            "--apply",
            "--offline",
            "--release-tag",
            "v1.0.0",
            "--site",
            str(site),
        ],
    )

    assert consumer_sync.main() == 0
    remediation_output = capsys.readouterr().out
    assert "PASS  Forgejo release: v1.0.0 is published" in remediation_output
    assert remediation_output.count("SYNC  ") == 5
    assert [(stage, event) for stage, event, _ in write_events] == [
        ("remediate", "write_bytes"),
        ("remediate", "write_bytes"),
        ("remediate", "image.save"),
        ("remediate", "image.save"),
        ("remediate", "write_text"),
    ]
    staged_suffixes = [
        Path("files/public/brand/logo.svg"),
        Path("files/public/brand/logo-512.png"),
        Path("files/public/brand/og.jpg"),
        Path("files/src/assets/brand-hero.jpg"),
        Path("files/public/brand/brand-kit-provenance.json"),
    ]
    for (_, _, path), suffix in zip(write_events, staged_suffixes):
        assert ".brand-kit-consumer-sync-" in str(path)
        assert Path(*path.parts[-len(suffix.parts) :]) == suffix
        assert not path.exists()
    assert (site / "public/brand/logo.svg").read_bytes() == (
        root / "logo/logo.svg"
    ).read_bytes()
    assert (site / "public/brand/logo-512.png").read_bytes() == (
        root / "logo/logo-512.png"
    ).read_bytes()
    assert {
        relative: (site / relative).read_bytes() for relative in protected
    } == protected

    remediation_writes = list(write_events)
    stage = "reaudit"
    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, fetcher, config=config
    )

    assert exit_code == 0
    assert report["status"] == "current"
    assert report["release"]["tag"] == "v1.0.0"
    assert report["release"]["checkout"]["matches"] is True
    assert (
        report["release"]["checkout"]["head"]
        == report["release"]["checkout"]["tag_commit"]
    )
    assert all(check["status"] == "current" for check in report["checks"])
    assert write_events == remediation_writes
    assert {
        relative: (site / relative).read_bytes() for relative in protected
    } == protected
    assert [url for url in calls if "/releases/" in url] == [
        detector_release_url,
        sync_release_url,
        detector_release_url,
    ]


def test_run_audit_reports_stale_copy_and_live_asset_separately(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    (site / "public/brand/logo-512.png").write_bytes(b"stale")
    fetcher = make_fetcher(root)

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        release_tag="v1.0.0",
        fetcher=fetcher,
    )

    assert report["status"] == "stale"
    site_checks = {
        check["asset"]: check for check in report["checks"] if "path" in check
    }
    live_checks = {
        check["asset"]: check for check in report["checks"] if "url" in check
    }
    assert site_checks["logo-512.png"]["status"] == "stale"
    assert live_checks["live open-graph card"]["status"] == "current"
    assert report["consumers"]["jedarden.com"]["status"] == "stale"
    assert report["consumers"]["jedarden.com live"]["status"] == "current"


def test_run_audit_detects_stale_site_owned_favicon_outputs(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    (site / "public/favicon.svg").write_bytes(b"stale")
    for name, size in (
        ("apple-touch-icon.png", 180),
        ("icon-192.png", 192),
        ("icon-512.png", 512),
    ):
        Image.new("RGB", (size, size), (0, 0, 0)).save(site / f"public/{name}")

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        release_tag="v1.0.0",
        fetcher=make_fetcher(root),
    )

    site_checks = {
        check["asset"]: check for check in report["checks"] if "path" in check
    }
    assert report["status"] == "stale"
    for name in (
        "favicon.svg",
        "apple-touch-icon.png",
        "icon-192.png",
        "icon-512.png",
    ):
        assert site_checks[name]["status"] == "stale"


def test_missing_live_fetch_is_indeterminate_not_a_pass(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)

    def fetcher(url, headers=None):
        if "avatars.example" in url:
            raise TimeoutError("profile service unavailable")
        return make_fetcher(root)(url, headers)

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        release_tag="v1.0.0",
        fetcher=fetcher,
    )

    assert report["status"] == "indeterminate"
    avatar = next(
        check for check in report["checks"] if check["asset"] == "github profile avatar"
    )
    assert avatar["status"] == "unavailable"
    assert "unavailable" in avatar["reason"]


def test_run_audit_compares_live_asset_bytes_against_the_release_reference(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    stale_avatar = image_bytes(Image.new("RGB", (20, 20), (0, 0, 0)))
    base_fetcher = make_fetcher(root)

    def fetcher(url, headers=None):
        if "avatars.example" in url:
            return stale_avatar
        return base_fetcher(url, headers)

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        release_tag="v1.0.0",
        fetcher=fetcher,
    )

    avatar = next(
        check for check in report["checks"] if check["asset"] == "github profile avatar"
    )
    assert report["status"] == "stale"
    assert avatar["status"] == "stale"
    assert avatar["observed_sha256"] == hashlib.sha256(stale_avatar).hexdigest()
    assert "exceeds" in avatar["reason"]


def test_main_returns_zero_for_a_current_release_and_uses_source_root(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path / "release")
    site = make_site(root, tmp_path)
    (root / "source/logo.svg").write_bytes(b"root-specific source")

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, make_fetcher(root)
    )

    assert exit_code == 0
    assert report["status"] == "current"
    assert report["release"]["tag"] == "v1.0.0"
    assert report["site"]["path"] == str(site.resolve())
    logo_digest = next(
        item for item in report["source_digests"] if item["path"] == "source/logo.svg"
    )
    assert logo_digest["sha256"] == hashlib.sha256(
        (root / "source/logo.svg").read_bytes()
    ).hexdigest()


def test_main_returns_one_for_a_confirmed_stale_consumer(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    (site / "public/brand/logo.svg").write_bytes(b"stale")

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, make_fetcher(root)
    )

    assert exit_code == 1
    assert report["status"] == "stale"
    stale = next(
        check for check in report["checks"] if check["asset"] == "logo.svg"
    )
    assert stale["status"] == "stale"


def test_main_returns_two_when_release_network_access_fails(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)

    def fetcher(url, headers=None):
        raise urllib.error.URLError("forgejo unavailable")

    exit_code, report = invoke_main(monkeypatch, capsys, root, site, fetcher)

    assert exit_code == 2
    assert report["status"] == "indeterminate"
    assert report["checks"] == []
    assert any("cannot read release" in error for error in report["errors"])


def test_main_returns_two_when_live_network_access_fails(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    base_fetcher = make_fetcher(root)

    def fetcher(url, headers=None):
        if "avatars.example" in url:
            raise urllib.error.URLError("avatar service unavailable")
        return base_fetcher(url, headers)

    exit_code, report = invoke_main(monkeypatch, capsys, root, site, fetcher)

    assert exit_code == 2
    assert report["status"] == "indeterminate"
    live = [
        check for check in report["checks"] if check["asset"] == "github profile avatar"
    ][0]
    assert live["status"] == "unavailable"


def test_main_returns_two_when_offline_mode_skips_live_checks(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    calls = []
    base_fetcher = make_fetcher(root)

    def fetcher(url, headers=None):
        calls.append(url)
        return base_fetcher(url, headers)

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, fetcher, extra_args=("--offline",)
    )

    assert exit_code == 2
    assert report["status"] == "indeterminate"
    assert calls == [
        "https://forgejo.example/api/v1/repos/jedarden/brand-kit/releases/tags/v1.0.0"
    ]
    assert all(
        check["status"] == "skipped"
        for check in report["checks"]
        if "url" in check
    )


def test_detector_report_uses_the_versioned_contract_and_validates_all_outcomes(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, make_fetcher(root)
    )

    assert exit_code == 0
    assert report["schema"] == consumer_drift_report.REPORT_SCHEMA
    assert report["status"] == "current"
    assert report["coverage"]["out_of_scope"] == []
    consumer_drift_report.validate_report(report)

    exit_code, offline_report = invoke_main(
        monkeypatch,
        capsys,
        root,
        site,
        make_fetcher(root),
        extra_args=("--offline",),
    )

    assert exit_code == 2
    assert offline_report["status"] == "indeterminate"
    assert all(
        check["status"] == "skipped"
        for check in offline_report["checks"]
        if "url" in check
    )
    consumer_drift_report.validate_report(offline_report)

    invalid = dict(report)
    invalid["schema"] = "brand-kit-consumer-drift/v2"
    with pytest.raises(consumer_drift_report.ReportError, match="schema"):
        consumer_drift_report.validate_report(invalid)


def test_report_schema_file_matches_the_runtime_contract():
    schema = json.loads(
        Path("consumer-drift-report.schema.json").read_text(encoding="utf-8")
    )

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"] == {
        "const": consumer_drift_report.REPORT_SCHEMA
    }
    assert schema["properties"]["status"]["enum"] == [
        "current",
        "stale",
        "indeterminate",
    ]
    assert schema["$defs"]["check"]["properties"]["status"]["enum"] == [
        "current",
        "stale",
        "unavailable",
        "skipped",
    ]
    assert schema["$defs"]["outOfScope"]["required"] == [
        "platform",
        "surfaces",
        "reason",
    ]


def test_detector_report_is_accepted_by_the_checked_in_json_schema(
    tmp_path, monkeypatch, capsys
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, make_fetcher(root)
    )

    assert exit_code == 0
    schema = json.loads(
        Path("consumer-drift-report.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)


def test_saved_report_cli_validates_the_same_artifact_contract(tmp_path):
    report = {
        "schema": consumer_drift_report.REPORT_SCHEMA,
        "checked_at": "2026-09-27T17:00:00Z",
        "status": "indeterminate",
        "release": {
            "tag": None,
            "published_at": None,
            "record_target": None,
            "checkout": None,
        },
        "site": {"name": "jedarden.com", "path": None, "commit": None},
        "source_digests": [],
        "checks": [],
        "consumers": {},
        "coverage": {
            "out_of_scope": [
                {
                    "platform": "Example",
                    "surfaces": ["profile"],
                    "reason": "No stable public endpoint is available.",
                }
            ]
        },
        "errors": ["workflow did not reach the detector"],
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "tools/validate_consumer_drift_report.py", str(report_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "PASS  validated brand-kit-consumer-drift/v1 report" in result.stdout


def test_release_discovery_ignores_drafts_and_waits_for_the_configured_age():
    config = make_config()
    config["release"]["minimum_age_hours"] = 24
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    records = [
        {"tag_name": "v2.0.0", "draft": True, "published_at": "2026-09-20T00:00:00Z"},
        {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-23T12:00:00Z",
        },
        {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-01T00:00:00Z",
        },
    ]

    def fetcher(url, headers=None):
        assert url.endswith("/repos/jedarden/brand-kit/releases?limit=50")
        return json.dumps(records).encode()

    selected = consumer_drift.discover_release(config, fetcher=fetcher, now=now)

    assert selected["tag_name"] == "v1.1.0"


def test_release_discovery_skips_newest_stable_release_until_mirror_catches_up():
    config = make_config()
    records = [
        {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-24T00:00:00Z",
        },
        {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-23T00:00:00Z",
        },
    ]
    checked = []

    def fetcher(url, headers=None):
        return json.dumps(records).encode()

    def mirror_checker(repository, tag):
        checked.append((repository, tag))
        return None if tag == "v1.2.0" else "a" * 40

    selected = consumer_drift.discover_release(
        config,
        fetcher=fetcher,
        mirror_repository="https://github.example/brand-kit.git",
        mirror_checker=mirror_checker,
    )

    assert selected["tag_name"] == "v1.1.0"
    assert checked == [
        ("https://github.example/brand-kit.git", "v1.2.0"),
        ("https://github.example/brand-kit.git", "v1.1.0"),
    ]


def test_release_discovery_fails_when_no_stable_release_is_on_the_mirror():
    config = make_config()
    records = [
        {
            "tag_name": "v1.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-09-23T00:00:00Z",
        }
    ]

    with pytest.raises(consumer_drift.ReleaseError, match="read-only mirror"):
        consumer_drift.discover_release(
            config,
            fetcher=lambda url, headers=None: json.dumps(records).encode(),
            mirror_repository="https://github.example/brand-kit.git",
            mirror_checker=lambda repository, tag: None,
        )


def test_mirror_tag_commit_uses_a_read_only_peeled_tag_query(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{'a' * 40}\trefs/tags/v1.2.0^{{}}\n",
            stderr="",
        )

    monkeypatch.setattr(consumer_drift.subprocess, "run", run)

    assert (
        consumer_drift.mirror_tag_commit(
            "https://github.example/brand-kit.git", "v1.2.0"
        )
        == "a" * 40
    )
    assert calls == [
        (
            [
                "git",
                "ls-remote",
                "--exit-code",
                "https://github.example/brand-kit.git",
                "refs/tags/v1.2.0^{}",
            ],
            {"capture_output": True, "text": True, "check": False},
        )
    ]


def test_explicit_release_gate_rejects_draft_before_consumer_checks(tmp_path):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    destination = site / "public/brand/logo.svg"
    destination.write_bytes(b"unchanged")

    def fetcher(url, headers=None):
        return json.dumps({"tag_name": "v1.0.0", "draft": True}).encode()

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=make_config(),
        release_tag="v1.0.0",
        fetcher=fetcher,
    )

    assert report["status"] == "indeterminate"
    assert any("not published" in error for error in report["errors"])
    assert destination.read_bytes() == b"unchanged"
    assert report["checks"] == []


def test_checkout_state_requires_the_selected_tag_when_configured(tmp_path):
    root = make_root(tmp_path)
    config = make_config()
    config["release"]["require_checkout"] = True
    try:
        consumer_drift.checkout_state(root, "v1.0.0", True)
    except consumer_drift.ReleaseError as error:
        assert "not the published release tag" in str(error)
    else:
        raise AssertionError("a directory without a git checkout must fail closed")


def test_inventory_config_matches_documented_consumer_inventory():
    config = consumer_drift.load_config()

    assert config["site_assets"] == [
        {
            "name": "logo.svg",
            "source": "logo/logo.svg",
            "path": "public/brand/logo.svg",
            "comparison": "bytes",
        },
        {
            "name": "logo-512.png",
            "source": "logo/logo-512.png",
            "path": "public/brand/logo-512.png",
            "comparison": "bytes",
        },
        {
            "name": "favicon.svg",
            "source": "logo/logo.svg",
            "path": "public/favicon.svg",
            "comparison": "bytes",
        },
        {
            "name": "apple-touch-icon.png",
            "source": "favicon/apple-touch-icon-180.png",
            "path": "public/apple-touch-icon.png",
            "comparison": "image",
            "tolerance": 1.0,
        },
        {
            "name": "icon-192.png",
            "source": "favicon/favicon-192.png",
            "path": "public/icon-192.png",
            "comparison": "image",
            "tolerance": 1.0,
        },
        {
            "name": "icon-512.png",
            "source": "favicon/favicon-512.png",
            "path": "public/icon-512.png",
            "comparison": "image",
            "tolerance": 1.0,
        },
        {
            "name": "og.jpg",
            "source": "source/hero.png",
            "path": "public/brand/og.jpg",
            "comparison": "crop",
            "tolerance": 3.0,
            "crop": {"width": 1200, "height": 630, "fy": 0.45},
        },
        {
            "name": "brand-hero.jpg",
            "source": "source/hero.png",
            "path": "src/assets/brand-hero.jpg",
            "comparison": "crop",
            "tolerance": 3.0,
            "crop": {"width": 1536, "height": 1024, "fy": 0.0},
        },
    ]
    assert config["live_assets"] == [
        {
            "name": "github profile avatar",
            "consumer": "github.com/jedarden",
            "source": "avatars/github-460.png",
            "url": "https://avatars.githubusercontent.com/jedarden",
            "comparison": "image",
            "tolerance": 8.0,
            "resize_reference": True,
        },
        {
            "name": "live open-graph card",
            "consumer": "jedarden.com live",
            "source": "source/hero.png",
            "url": "https://jedarden.com/brand/og.jpg",
            "comparison": "crop",
            "tolerance": 3.0,
            "crop": {"width": 1200, "height": 630, "fy": 0.45},
        },
        {
            "name": "X profile avatar",
            "consumer": "x.com/jedardencodes",
            "source": "avatars/x-400.png",
            "url": "https://x.com/jedardencodes",
            "media_url_pattern": r"https://pbs\.twimg\.com/profile_images/[^\"'\\ ]+_400x400\.jpg",
            "comparison": "image",
            "tolerance": 8.0,
            "resize_reference": True,
        },
        {
            "name": "X profile header",
            "consumer": "x.com/jedardencodes",
            "source": "banners/x-header-1500x500.png",
            "url": "https://x.com/jedardencodes",
            "media_url_pattern": r"https://pbs\.twimg\.com/profile_banners/[^\"'\\ ]+/1500x500",
            "comparison": "image",
            "tolerance": 8.0,
        },
        {
            "name": "LinkedIn personal profile picture",
            "consumer": "linkedin.com/in/jed-arden",
            "source": "avatars/linkedin-400.png",
            "url": "https://www.linkedin.com/in/jed-arden/",
            "media_url_pattern": r"https://media\.licdn\.com/[^\"'\\ ]+/profile-displayphoto-(?:shrink|scale)_[^\"'\\ ]+",
            "comparison": "image",
            "tolerance": 8.0,
            "resize_reference": True,
        },
        {
            "name": "LinkedIn personal banner",
            "consumer": "linkedin.com/in/jed-arden",
            "source": "banners/linkedin-personal-1584x396.png",
            "url": "https://www.linkedin.com/in/jed-arden/",
            "media_url_pattern": r"https://media\.licdn\.com/[^\"'\\ ]+/profile-displaybackgroundimage-[^\"'\\ ]+",
            "comparison": "image",
            "tolerance": 8.0,
            "resize_reference": True,
        },
        {
            "name": "LinkedIn company profile picture",
            "consumer": "linkedin.com/company/runsybil",
            "source": "avatars/linkedin-400.png",
            "url": "https://www.linkedin.com/company/runsybil/",
            "media_url_pattern": r"https://media\.licdn\.com/[^\"'\\ ]+/company-logo_(?:100|200)_[^\"'\\ ]+",
            "comparison": "image",
            "tolerance": 8.0,
            "resize_reference": True,
        },
        {
            "name": "LinkedIn company banner",
            "consumer": "linkedin.com/company/runsybil",
            "source": "banners/linkedin-company-1128x191.png",
            "url": "https://www.linkedin.com/company/runsybil/",
            "media_url_pattern": r"https://media\.licdn\.com/[^\"'\\ ]+/image-scale_191_1128/[^\"'\\ ]+",
            "comparison": "image",
            "tolerance": 8.0,
        },
    ]
    assert config["site"]["repository"] == "https://github.com/jedarden/jedarden.com.git"
    assert config["live"] == {
        "og_url": "https://jedarden.com/brand/og.jpg",
        "avatar_url": "https://avatars.githubusercontent.com/jedarden",
        "x_profile_url": "https://x.com/jedardencodes",
        "linkedin_personal_url": "https://www.linkedin.com/in/jed-arden/",
        "linkedin_company_url": "https://www.linkedin.com/company/runsybil/",
    }

    assert {entry["platform"] for entry in config["out_of_scope"]} == {
        "Instagram",
        "Threads",
        "Facebook",
        "YouTube",
        "TikTok",
        "Mastodon",
        "Bluesky",
        "Discord",
        "GitHub",
    }

    source_paths = {entry["path"] for entry in config["source_assets"]}
    assert all((consumer_drift.ROOT / path).is_file() for path in source_paths)
    assert {entry["source"] for entry in config["site_assets"]}.issubset(source_paths)
    assert {entry["source"] for entry in config["live_assets"]}.issubset(source_paths)
    for entry in config["site_assets"]:
        path = Path(entry["path"])
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert entry["comparison"] in {"bytes", "image", "crop"}
    for entry in config["live_assets"]:
        url = urllib.parse.urlparse(entry["url"])
        assert url.scheme == "https"
        assert url.netloc
        assert entry["comparison"] in {"image", "crop"}
        if "media_url_pattern" in entry:
            assert entry["media_url_pattern"]
    assert Path("consumer-drift.json").read_text(encoding="utf-8").endswith("\n")


def test_release_checklist_runs_site_favicon_generator_before_commit():
    workflow = Path("docs/notes/post-tag-consumer-update.md").read_text(
        encoding="utf-8"
    ).split("## The workflow", 1)[1]
    sync = 'tools/consumer_sync.py --release-tag "$VERSION"'
    generate = "node scripts/make-favicons.mjs"
    commit = "git commit -m 'chore(brand): sync to brand-kit @vX.Y.Z'"
    push = "git push"
    detect = 'tools/consumer_drift.py --release-tag "$VERSION"'

    assert 'SITE="${SITE:-$HOME/jedarden.com}"' in workflow
    assert 'cd -- "$SITE" || exit' in workflow
    assert workflow.index(sync) < workflow.index(generate) < workflow.index(commit)
    assert workflow.index(commit) < workflow.index(push) < workflow.index(detect)


def test_scheduled_workflow_contract_is_read_only_and_tag_safe():
    workflow = Path("automation/brand-kit-consumer-drift-workflowtemplate.yml").read_text(
        encoding="utf-8"
    )
    cron = Path("automation/brand-kit-consumer-drift-cronworkflow.yml").read_text(
        encoding="utf-8"
    )
    config = json.loads(Path("consumer-drift.json").read_text(encoding="utf-8"))
    release = config["release"]

    assert "kind: WorkflowTemplate" in workflow
    assert "kind: CronWorkflow" in cron
    assert "workflowTemplateRef:" in cron
    assert 'name: brand-kit-consumer-drift\n  namespace: argo-workflows' in cron
    assert 'name: release-tag\n          value: ""' in cron
    assert release["minimum_age_hours"] == 24
    assert release["mirror_repository"] == "https://github.com/jedarden/brand-kit.git"
    assert release["tag_pattern"] == r"^v\d+\.\d+\.\d+$"

    tag_parameter = 'TAG="{{workflow.parameters.release-tag}}"'
    resolve_start = 'TAG="$(python3 tools/consumer_drift.py '
    mirror_argument = (
        '--mirror-repository "{{workflow.parameters.brand-kit-repository}}"'
    )
    assert tag_parameter in workflow
    assert 'if [ -z "$TAG" ]; then' in workflow
    assert resolve_start in workflow
    assert mirror_argument in workflow
    assert "--print-release-tag)" in workflow
    assert workflow.index(tag_parameter) < workflow.index(resolve_start)
    assert workflow.index(resolve_start) < workflow.index('git clone --filter=blob:none --no-tags "{{workflow.parameters.brand-kit-repository}}" /release')

    # The release checkout must be populated from and detached at the selected
    # tag, rather than auditing whichever branch the mirror happens to serve.
    assert (
        'git -C /release fetch --depth=1 origin '
        '"refs/tags/${TAG}:refs/tags/${TAG}"'
    ) in workflow
    assert 'git -C /release checkout --detach --quiet "$TAG"' in workflow
    assert '--source-root /release' in workflow
    assert '--release-tag "$TAG"' in workflow
    assert 'python3 /brand-kit/tools/consumer_drift.py' in workflow
    assert "python3 /brand-kit/tools/prune_consumer_drift_reports.py" in workflow
    assert "--retention-days 30" in workflow
    assert "--release-evidence-root /brand-kit/release-evidence/v1" in workflow
    assert workflow.index("prune_consumer_drift_reports.py") < workflow.index(
        "python3 /brand-kit/tools/consumer_drift.py"
    )

    # Both repositories are public GitHub mirrors; the site clone is pinned to
    # its read-only main branch and no write-capable GitHub credential is used.
    assert (
        'value: https://github.com/jedarden/brand-kit.git' in workflow
        and 'value: https://github.com/jedarden/jedarden.com.git' in workflow
    )
    assert (
        'git clone --filter=blob:none --depth=1 --branch main '
        '"{{workflow.parameters.site-repository}}" /jedarden.com'
    ) in workflow
    assert "git push" not in workflow
    assert "git commit" not in workflow
    assert "consumer_sync.py" not in workflow
    assert "--apply" not in workflow
    assert "GITHUB_TOKEN" not in workflow
    assert "GH_TOKEN" not in workflow
    assert "WRITE_TOKEN" not in workflow
    assert "FORGEJO_WRITE" not in workflow
    assert "ARGO_SUBMIT_TOKEN" not in workflow
    assert "FORGEJO_TOKEN" in workflow
    assert "- name: FORGEJO_TOKEN" in workflow

    # --json writes the report to the Argo log and --report preserves the same
    # result as a machine-readable artifact for later inspection.
    assert "--json" in workflow
    assert "--report /tmp/consumer-drift-report.json" in workflow
    assert '"schema":"brand-kit-consumer-drift/v1"' in workflow
    assert "tools/validate_consumer_drift_report.py" in workflow
    assert workflow.count("validate_consumer_drift_report.py") >= 3

    # The CronWorkflow delegates to the same versioned template, so scheduled
    # runs receive the detector-side and artifact-side validation guarantees.
    assert "brand-kit-consumer-drift/v1" in workflow
    assert "workflowTemplateRef:" in cron


def test_scheduled_workflow_routes_failures_and_retains_report():
    workflow = Path(
        "automation/brand-kit-consumer-drift-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    toolchain = Path("docs/notes/asset-toolchain.md").read_text(encoding="utf-8")
    assert "  onExit: route-drift" in workflow
    assert "    - name: route-drift" in workflow
    assert "            template: notify-owner" in workflow
    assert '            when: "{{workflow.status}} != Succeeded"' in workflow
    assert "              failed: true\n              error: true" in workflow

    assert "            - name: detector-exit-code" in workflow
    assert (
        "                path: /tmp/consumer-drift-exit-code\n"
        '                default: "2"'
    ) in workflow
    assert "            - name: consumer-drift-report" in workflow
    assert "              path: /tmp/consumer-drift-report.json" in workflow
    assert "              artifactGC:\n                strategy: Never" in workflow
    assert (
        "                key: failures/brand-kit-consumer-drift/v1/{{workflow.uid}}/report.json"
        in workflow
    )
    assert "                endpoint: s3.ardenone.com" in workflow
    assert "                bucket: needle-ci-artifacts" in workflow
    assert "                name: needle-ci-artifact-publisher" in workflow
    assert "                key: access-key" in workflow
    assert "                key: secret-key" in workflow
    assert "          - name: S3_READER_ACCESS_KEY" in workflow
    assert "          - name: S3_READER_SECRET_KEY" in workflow
    assert "          - name: S3_PUBLISHER_ACCESS_KEY" in workflow
    assert "          - name: S3_PUBLISHER_SECRET_KEY" in workflow

    assert "        image: curlimages/curl:8.12.1" in workflow
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in workflow
    assert '"alertname": "BrandKitConsumerDrift"' in workflow
    assert '"owner": "jedarden"' in workflow
    assert (
        "https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-consumer-drift/v1/{{workflow.uid}}/report.json"
        in workflow
    )
    assert (
        "https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-consumer-drift/v1/<workflow-uid>/report.json"
        in toolchain
    )
