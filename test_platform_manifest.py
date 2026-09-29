import json
import shutil
from pathlib import Path

import pytest

from tools import build_assets, verify_assets


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "platform-assets.json"
SCHEMA_PATH = ROOT / "platform-assets.schema.json"


def _asset(platform, role, path, width, height, source):
    return {
        "platform": platform,
        "role": role,
        "path": path,
        "dimensions": {"width": width, "height": height},
        "source": source,
    }


def _favicon(path, width, height=None):
    height = width if height is None else height
    return _asset(
        "Web / Open Graph",
        "favicon",
        path,
        width,
        height,
        "source/logo.svg",
    )


EXPECTED_PLATFORM_ASSETS = [
    _asset("X / Twitter", "profile_picture", "avatars/x-400.png", 400, 400, "source/logo.svg"),
    _asset("X / Twitter", "banner", "banners/x-header-1500x500.png", 1500, 500, "source/hero.png"),
    _asset("LinkedIn (personal)", "profile_picture", "avatars/linkedin-400.png", 400, 400, "source/logo.svg"),
    _asset("LinkedIn (personal)", "banner", "banners/linkedin-personal-1584x396.png", 1584, 396, "source/hero.png"),
    _asset("LinkedIn (company)", "profile_picture", "avatars/linkedin-400.png", 400, 400, "source/logo.svg"),
    _asset("LinkedIn (company)", "banner", "banners/linkedin-company-1128x191.png", 1128, 191, "source/hero.png"),
    _asset("GitHub", "profile_picture", "avatars/github-460.png", 460, 460, "source/logo.svg"),
    _asset("GitHub", "banner", "banners/github-social-1280x640.png", 1280, 640, "source/hero.png"),
    _asset("Instagram", "profile_picture", "avatars/instagram-320.png", 320, 320, "source/logo.svg"),
    _asset("Threads", "profile_picture", "avatars/threads-320.png", 320, 320, "source/logo.svg"),
    _asset("Facebook", "profile_picture", "avatars/facebook-320.png", 320, 320, "source/logo.svg"),
    _asset("Facebook", "banner", "banners/facebook-cover-851x315.png", 851, 315, "source/hero.png"),
    _asset("Facebook", "banner", "banners/facebook-cover-2x-1702x630.png", 1702, 630, "source/hero.png"),
    _asset("YouTube", "profile_picture", "avatars/youtube-800.png", 800, 800, "source/logo.svg"),
    _asset("YouTube", "banner", "banners/youtube-banner-2560x1440.png", 2560, 1440, "source/hero.png"),
    _asset("TikTok", "profile_picture", "avatars/tiktok-200.png", 200, 200, "source/logo.svg"),
    _asset("Mastodon", "profile_picture", "avatars/mastodon-400.png", 400, 400, "source/logo.svg"),
    _asset("Mastodon", "banner", "banners/mastodon-header-1500x500.png", 1500, 500, "source/hero.png"),
    _asset("Bluesky", "profile_picture", "avatars/bluesky-400.png", 400, 400, "source/logo.svg"),
    _asset("Bluesky", "banner", "banners/twitter-card-1200x628.png", 1200, 628, "source/hero.png"),
    _asset("Discord", "profile_picture", "avatars/discord-512.png", 512, 512, "source/logo.svg"),
    _asset("Discord", "banner", "banners/discord-banner-960x540.png", 960, 540, "source/hero.png"),
    {
        "platform": "Web / Open Graph",
        "role": "favicon",
        "path": "favicon/favicon.ico",
        "dimensions": {
            "width": 256,
            "height": 256,
            "sizes": [
                {"width": 16, "height": 16},
                {"width": 32, "height": 32},
                {"width": 48, "height": 48},
                {"width": 64, "height": 64},
                {"width": 128, "height": 128},
                {"width": 256, "height": 256},
            ],
        },
        "source": "source/logo.svg",
    },
    _favicon("favicon/favicon-16.png", 16),
    _favicon("favicon/favicon-32.png", 32),
    _favicon("favicon/favicon-48.png", 48),
    _favicon("favicon/favicon-192.png", 192),
    _favicon("favicon/favicon-512.png", 512),
    _favicon("favicon/apple-touch-icon-180.png", 180),
    _asset("Web / Open Graph", "banner", "banners/open-graph-1200x630.png", 1200, 630, "source/hero.png"),
    _asset("Web / Open Graph", "banner", "banners/twitter-card-1200x628.png", 1200, 628, "source/hero.png"),
]

EXPECTED_PLATFORM_REQUIREMENTS = [
    {
        "platform": requirement["platform"],
        "source_url": requirement["source_url"],
        "last_verified": requirement["last_verified"],
        "source_content_sha256": requirement["source_content_sha256"],
    }
    for requirement in build_assets.PLATFORM_REQUIREMENTS
]


def test_platform_manifest_is_exact_readme_contract():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest == {
        "schema_version": 4,
        "upload_constraints": build_assets.PLATFORM_UPLOAD_CONSTRAINTS,
        "platform_requirements": EXPECTED_PLATFORM_REQUIREMENTS,
        "assets": EXPECTED_PLATFORM_ASSETS,
        "requirement_evidence": build_assets.PLATFORM_REQUIREMENT_EVIDENCE,
    }
    assert verify_assets.read_readme_platform_assets() == EXPECTED_PLATFORM_ASSETS

    # These are intentional README omissions, not missing manifest entries.
    assert not {
        (asset["platform"], asset["role"])
        for asset in EXPECTED_PLATFORM_ASSETS
        if asset["role"] == "banner"
    } & {
        (platform, "banner")
        for platform in ("Instagram", "Threads", "TikTok")
    }

    # Shared generated files are aliases, but each platform role remains explicit.
    assert [
        asset["path"]
        for asset in EXPECTED_PLATFORM_ASSETS
        if asset["role"] == "profile_picture"
        and asset["platform"] in {"LinkedIn (personal)", "LinkedIn (company)"}
    ] == ["avatars/linkedin-400.png", "avatars/linkedin-400.png"]
    assert [
        asset["path"]
        for asset in EXPECTED_PLATFORM_ASSETS
        if asset["role"] == "banner"
        and asset["platform"] in {"Mastodon", "Web / Open Graph"}
    ] == [
        "banners/mastodon-header-1500x500.png",
        "banners/open-graph-1200x630.png",
        "banners/twitter-card-1200x628.png",
    ]
    assert [
        asset["path"]
        for asset in EXPECTED_PLATFORM_ASSETS
        if asset["role"] == "banner"
        and asset["platform"] in {"Bluesky", "Web / Open Graph"}
    ] == [
        "banners/twitter-card-1200x628.png",
        "banners/open-graph-1200x630.png",
        "banners/twitter-card-1200x628.png",
    ]


def test_platform_manifest_schema_declares_strict_asset_shape():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": 4}
    assert schema["required"] == [
        "schema_version",
        "upload_constraints",
        "platform_requirements",
        "assets",
        "requirement_evidence",
    ]
    assert schema["properties"]["upload_constraints"] == {
        "$ref": "#/$defs/upload_constraints"
    }
    assert schema["$defs"]["upload_constraints"] == {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "formats",
            "color_mode",
            "alpha",
            "max_file_size_bytes",
        ],
        "properties": {
            "formats": {
                "const": ["PNG", "ICO"],
            },
            "color_mode": {"const": "RGB"},
            "alpha": {"const": "forbidden"},
            "max_file_size_bytes": {"const": 5242880},
        },
    }
    assert schema["properties"]["platform_requirements"]["minItems"] == 1
    assert schema["properties"]["platform_requirements"]["uniqueItems"] is True
    assert schema["properties"]["platform_requirements"]["items"] == {
        "$ref": "#/$defs/platform_requirement"
    }
    assert schema["$defs"]["platform_requirement"]["additionalProperties"] is False
    assert schema["$defs"]["platform_requirement"]["required"] == [
        "platform",
        "source_url",
        "last_verified",
        "source_content_sha256",
    ]
    assert schema["$defs"]["platform_requirement"]["properties"]["source_url"]["format"] == "uri"
    assert schema["$defs"]["platform_requirement"]["properties"]["last_verified"]["format"] == "date"
    assert schema["$defs"]["platform_requirement"]["properties"]["source_content_sha256"]["oneOf"][0] == {"type": "null"}
    assert schema["properties"]["requirement_evidence"]["items"] == {"$ref": "#/$defs/requirement_evidence"}
    assert schema["$defs"]["requirement_evidence"]["properties"]["classification"]["enum"] == [
        "upload_minimum", "recommendation", "specified_size", "within_limits", "project_choice"
    ]
    assert "source_content_sha256" in schema["$defs"]["requirement_evidence"]["required"]
    assert schema["$defs"]["platform_requirement"]["properties"]["source_url"]["pattern"] == (
        r"^https://[^\s/@?#]+(?:[/?#][^\s]*)?$"
    )
    assert schema["$defs"]["platform_requirement"]["properties"]["platform"]["enum"] == [
        "X / Twitter",
        "LinkedIn (personal)",
        "LinkedIn (company)",
        "GitHub",
        "Instagram",
        "Threads",
        "Facebook",
        "YouTube",
        "TikTok",
        "Mastodon",
        "Bluesky",
        "Discord",
        "Web / Open Graph",
    ]
    assert schema["properties"]["assets"]["minItems"] == 1
    assert schema["properties"]["assets"]["uniqueItems"] is True
    assert schema["properties"]["assets"]["items"] == {"$ref": "#/$defs/asset"}
    assert schema["$defs"]["asset"]["additionalProperties"] is False
    assert schema["$defs"]["asset"]["required"] == [
        "platform",
        "role",
        "path",
        "dimensions",
        "source",
    ]
    assert schema["$defs"]["asset"]["properties"]["role"]["enum"] == [
        "profile_picture",
        "banner",
        "favicon",
    ]
    assert schema["$defs"]["asset"]["properties"]["source"]["enum"] == [
        "source/logo.svg",
        "source/hero.png",
    ]
    assert schema["$defs"]["dimensions"]["additionalProperties"] is False
    assert schema["$defs"]["dimensions"]["properties"]["sizes"]["minItems"] == 1
    assert schema["$defs"]["dimensions"]["properties"]["sizes"]["uniqueItems"] is True


def test_readme_documents_requirement_provenance_and_review_check():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "### Platform requirement provenance" in readme
    assert "### Reviewing changed upload requirements" in readme
    assert "platform-assets.schema.json" in readme
    assert "schema version `4`" in readme
    assert "PNG or ICO formats" in readme
    assert "RGB color mode" in readme
    assert "no alpha channel" in readme
    assert "5 MiB (5,242,880 bytes)" in readme
    assert "absolute HTTPS `source_url` without credentials" in readme
    assert "real calendar `last_verified` date in `YYYY-MM-DD` form" in readme
    assert "tools/verify_assets.py" in readme
    assert "python3 tools/check_platform_requirements.py" in readme
    assert "--check-reachability" in readme
    assert "SHA-256" in readme
    assert "recommendation" in readme
    assert "upload minimum" in readme
    assert "exit `2` as `INDETERMINATE`" in readme
    assert "180 calendar days" in readme
    assert "--as-of YYYY-MM-DD" in readme
    assert "Do not merely bump the date" in readme
    for requirement in EXPECTED_PLATFORM_REQUIREMENTS:
        assert requirement["platform"] in readme
        assert requirement["source_url"] in readme
        assert requirement["last_verified"] in readme


@pytest.mark.parametrize("drift", ["missing", "unexpected", "stale"])
def test_platform_manifest_rejects_entry_drift(tmp_path, monkeypatch, drift):
    root = tmp_path / "brand-kit"
    root.mkdir()
    shutil.copy(ROOT / "README.md", root / "README.md")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if drift == "missing":
        manifest["assets"].pop()
    elif drift == "unexpected":
        manifest["assets"].append(dict(manifest["assets"][0]))
    else:
        manifest["assets"][0]["path"] = "avatars/stale.png"

    (root / "platform-assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)

    rows, ok = verify_assets.verify_platform_manifest()

    assert not ok
    assert rows[0][0] == "platform-assets.json"
    assert any(row[3].startswith("✗ MISMATCH") for row in rows)


def test_platform_manifest_rejects_unexpected_top_level_field(tmp_path, monkeypatch):
    root = tmp_path / "brand-kit"
    root.mkdir()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    (root / "platform-assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)

    rows, ok = verify_assets.verify_platform_manifest()

    assert not ok
    assert rows == [
        (
        "platform-assets.json",
            "schema_version, upload_constraints, platform_requirements, assets, and requirement_evidence only",
            "assets, platform_requirements, requirement_evidence, schema_version, unexpected, upload_constraints",
            "✗ invalid top-level fields",
        )
    ]


@pytest.mark.parametrize(
    ("change", "status"),
    (
        (lambda asset: asset.pop("dimensions"), "✗ invalid fields"),
        (lambda asset: asset.update({"role": "thumbnail"}), "✗ invalid role"),
        (lambda asset: asset.update({"path": "../outside.png"}), "✗ invalid path"),
        (lambda asset: asset.update({"source": "source/unknown.png"}), "✗ invalid source"),
        (
            lambda asset: asset.update({"dimensions": {"width": 0, "height": 400}}),
            "✗ malformed dimensions",
        ),
        (
            lambda asset: asset.update({"dimensions": {"width": 400, "height": 400, "extra": 1}}),
            "✗ malformed dimensions",
        ),
        (
            lambda asset: asset.update({"dimensions": {"width": 256, "height": 256, "sizes": []}}),
            "✗ malformed dimensions",
        ),
    ),
)
def test_platform_manifest_rejects_schema_invalid_asset_records(
    tmp_path, monkeypatch, change, status
):
    root = tmp_path / "brand-kit"
    root.mkdir()
    shutil.copy(ROOT / "README.md", root / "README.md")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    change(manifest["assets"][0])
    (root / "platform-assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)

    rows, ok = verify_assets.verify_platform_manifest()

    assert not ok
    assert any(row[3] == status for row in rows)


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        (
            lambda requirement: requirement.update({"source_url": "https://user:pass@example.test"}),
            "✗ invalid requirement provenance",
        ),
        (
            lambda requirement: requirement.update({"last_verified": "2026-02-30"}),
            "✗ invalid requirement provenance",
        ),
        (
            lambda requirement: requirement.update({"platform": " X / Twitter"}),
            "✗ invalid requirement provenance",
        ),
    ),
)
def test_platform_manifest_rejects_schema_invalid_requirement_records(
    tmp_path, monkeypatch, change, expected
):
    root = tmp_path / "brand-kit"
    root.mkdir()
    shutil.copy(ROOT / "README.md", root / "README.md")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    change(manifest["platform_requirements"][0])
    (root / "platform-assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_assets, "ROOT", root)

    rows, ok = verify_assets.verify_platform_manifest()

    assert not ok
    assert any(row[3] == expected for row in rows)
