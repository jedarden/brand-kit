import hashlib
import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from tools import consumer_drift


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


def make_root(tmp_path):
    root = tmp_path / "brand-kit"
    (root / "source").mkdir(parents=True)
    (root / "logo").mkdir()
    (root / "avatars").mkdir()
    (root / "favicon").mkdir()
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


def invoke_main(monkeypatch, capsys, root, site, fetch_bytes, extra_args=()):
    monkeypatch.setattr(consumer_drift, "load_config", lambda path: make_config())
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
        check["status"] == "unavailable"
        for check in report["checks"]
        if "url" in check
    )


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
    site_assets = {entry["name"]: entry for entry in config["site_assets"]}
    assert "github profile avatar" in live_names
    assert config["site_assets"]
    assert config["site"]["repository"].endswith("jedarden.com.git")
    assert site_assets["favicon.svg"] == {
        "name": "favicon.svg",
        "source": "logo/logo.svg",
        "path": "public/favicon.svg",
        "comparison": "bytes",
    }
    for name, source in (
        ("apple-touch-icon.png", "favicon/apple-touch-icon-180.png"),
        ("icon-192.png", "favicon/favicon-192.png"),
        ("icon-512.png", "favicon/favicon-512.png"),
    ):
        assert site_assets[name] == {
            "name": name,
            "source": source,
            "path": f"public/{name}",
            "comparison": "image",
            "tolerance": 1.0,
        }
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
