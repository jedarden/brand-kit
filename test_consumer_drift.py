import io
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from tools import consumer_drift


def image_bytes(image, image_format="PNG"):
    output = io.BytesIO()
    image.save(output, image_format)
    return output.getvalue()


def make_root(tmp_path):
    root = tmp_path / "brand-kit"
    (root / "source").mkdir(parents=True)
    (root / "logo").mkdir()
    (root / "avatars").mkdir()
    (root / "source/logo.svg").write_bytes(b"<svg>source</svg>")
    Image.new("RGB", (40, 30), (100, 120, 140)).save(root / "source/hero.png")
    (root / "logo/logo.svg").write_bytes(b"<svg>release</svg>")
    (root / "logo/logo-512.png").write_bytes(b"release-logo\x00")
    Image.new("RGB", (20, 20), (80, 90, 100)).save(root / "avatars/github-460.png")
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


def test_inventory_config_is_machine_readable_and_contains_live_profile_asset():
    config = consumer_drift.load_config()
    live_names = {entry["name"] for entry in config["live_assets"]}
    assert "github profile avatar" in live_names
    assert config["site_assets"]
    assert config["site"]["repository"].endswith("jedarden.com.git")
    assert Path("consumer-drift.json").read_text(encoding="utf-8").endswith("\n")


def test_scheduled_workflow_source_is_read_only_and_resolves_a_release():
    workflow = Path("automation/brand-kit-consumer-drift-cronworkflow.yml").read_text(
        encoding="utf-8"
    )
    assert "kind: CronWorkflow" in workflow
    assert "--print-release-tag" in workflow
    assert "--source-root /release" in workflow
    assert "--report /tmp/consumer-drift-report.json" in workflow
    assert "git push" not in workflow
    assert "FORGEJO_TOKEN" in workflow
