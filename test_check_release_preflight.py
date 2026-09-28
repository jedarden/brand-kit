from pathlib import Path
import subprocess

import pytest

from tools import check_release_preflight


ROOT = Path(__file__).resolve().parent
WORKFLOW = check_release_preflight.PUBLICATION_WORKFLOW_PATH


def _release_tree(tmp_path: Path, *, version: str = "v1.1.0") -> Path:
    (tmp_path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n### Changed\n- next\n\n"
        f"## [{version}] - 2026-09-28\n\n### Added\n- release\n",
        encoding="utf-8",
    )
    (tmp_path / WORKFLOW).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / WORKFLOW).write_text(
        "# Forgejo release-publication workflow\n"
        "Run tools/release_publish.py with --tag and --ci-run to create a "
        "published Forgejo Release.\n",
        encoding="utf-8",
    )
    (tmp_path / check_release_preflight.PUBLICATION_PUBLISHER_PATH).parent.mkdir(
        parents=True, exist_ok=True
    )
    (tmp_path / check_release_preflight.PUBLICATION_PUBLISHER_PATH).write_text(
        "publisher\n", encoding="utf-8"
    )
    return tmp_path


def test_classify_paths_keeps_source_and_complete_derived_inventory_release_relevant():
    classification = check_release_preflight.classify_paths(
        [
            "source/logo.svg",
            "avatars/github-460.png",
            "banners/site.png",
            "favicon/favicon.ico",
            "logo/logo.svg",
            "palette.json",
            "platform-assets.json",
        ]
    )

    assert classification.release_required
    assert classification.source == ("source/logo.svg",)
    assert classification.derived == (
        "avatars/github-460.png",
        "banners/site.png",
        "favicon/favicon.ico",
        "logo/logo.svg",
        "palette.json",
        "platform-assets.json",
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md", "docs/notes/release-publication.md"],
        ["consumer_registry.json", "platform-assets.schema.json"],
        ["VERSION"],
    ],
)
def test_documentation_and_metadata_only_changes_may_remain_unreleased(tmp_path, paths):
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- documentation or metadata\n",
        encoding="utf-8",
    )

    report = check_release_preflight.check_preflight(tmp_path, paths)

    assert not report.classification.release_required
    assert report.classification.kind in {"documentation-only", "metadata-only"}
    assert report.version is None


def test_source_change_requires_version_and_changelog_in_same_diff(tmp_path):
    tree = _release_tree(tmp_path)
    (tree / "source").mkdir()

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="must update VERSION, CHANGELOG.md",
    ):
        check_release_preflight.check_preflight(tree, ["source/logo.svg"])


def test_source_change_requires_a_new_version(tmp_path):
    tree = _release_tree(tmp_path, version="v1.0.0")

    with pytest.raises(check_release_preflight.ReleasePreflightError, match="remains v1.0.0"):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


def test_source_and_derived_changes_pass_with_matching_release_inputs(tmp_path):
    tree = _release_tree(tmp_path)

    report = check_release_preflight.check_preflight(
        tree,
        ["source/logo.svg", "logo/logo-512.png", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
    )

    assert report.classification.kind == "release-required"
    assert report.version == "v1.1.0"
    assert report.changelog_heading == "## [v1.1.0] - 2026-09-28"
    assert report.publication_workflow == WORKFLOW


def test_release_required_diff_rejects_incomplete_publication_workflow(tmp_path):
    tree = _release_tree(tmp_path)
    (tree / WORKFLOW).write_text("# Forgejo release-publication workflow\n", encoding="utf-8")

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="published-release workflow is incomplete",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["avatars/github-460.png", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


def test_changed_paths_includes_both_sides_of_a_rename(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    (tmp_path / "source").mkdir()
    (tmp_path / "source/logo.svg").write_text("logo", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source/logo.svg"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    (tmp_path / "source/logo-renamed.svg").write_text("logo", encoding="utf-8")
    (tmp_path / "source/logo.svg").unlink()
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-A", "source"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "rename"], check=True)

    assert check_release_preflight.changed_paths(tmp_path, "HEAD^", "HEAD") == (
        "source/logo-renamed.svg",
        "source/logo.svg",
    )
