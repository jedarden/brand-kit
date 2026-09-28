from pathlib import Path
import subprocess
import sys

import pytest

from tools import check_release_preflight


ROOT = Path(__file__).resolve().parent
WORKFLOW = check_release_preflight.PUBLICATION_WORKFLOW_PATH
TOOL = ROOT / "tools" / "check_release_preflight.py"


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
        'VERSION="$(tr -d \'\\r\\n\' < VERSION)"; git tag -a "$VERSION"\n'
        'git push origin "refs/tags/$VERSION"\n'
        'Run tools/release_publish.py with --tag "$VERSION" and --ci-run to create a '
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


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_release_candidate(
    tmp_path: Path,
    *,
    base_tag: str = "published-release-2026-09-27",
    base_version: str = "v7.4.2",
    candidate_version: str = "v7.5.0",
) -> tuple[Path, str]:
    tree = _release_tree(tmp_path, version=base_version)
    (tree / "source").mkdir()
    (tree / "source/logo.svg").write_text("baseline\n", encoding="utf-8")

    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "VERSION", "CHANGELOG.md", "docs", "source", "tools")
    _git(tree, "commit", "-qm", "published release")
    _git(tree, "tag", base_tag)

    _release_tree(tree, version=candidate_version)
    (tree / "source/logo.svg").write_text("candidate\n", encoding="utf-8")
    _git(tree, "add", "VERSION", "CHANGELOG.md", "source/logo.svg")
    _git(tree, "commit", "-qm", "candidate release")
    return tree, base_tag


def _run_cli(tree: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(tree), "--base", base],
        check=False,
        capture_output=True,
        text=True,
    )


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
    ("path", "bucket", "kind"),
    [
        ("source/logo.svg", "source", "release-required"),
        ("avatars/github-460.png", "derived", "release-required"),
        ("docs/notes/release-publication.md", "documentation", "documentation-only"),
        ("consumer_registry.json", "metadata", "metadata-only"),
    ],
)
def test_classify_paths_exposes_each_contract_bucket(path, bucket, kind):
    classification = check_release_preflight.classify_paths([path])

    assert getattr(classification, bucket) == (path,)
    assert classification.kind == kind


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


def test_release_required_change_requires_version_in_the_diff(tmp_path):
    tree = _release_tree(tmp_path)

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="must update VERSION",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


def test_release_required_change_requires_changelog_in_the_diff(tmp_path):
    tree = _release_tree(tmp_path)

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="must update CHANGELOG.md",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION"],
            base_version="v1.0.0",
        )


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
    assert report.required_bump == "minor"
    assert report.changelog_heading == "## [v1.1.0] - 2026-09-28"
    assert report.publication_workflow == WORKFLOW


def test_derived_asset_change_uses_the_patch_bump_rule(tmp_path):
    tree = _release_tree(tmp_path, version="v1.0.1")

    report = check_release_preflight.check_preflight(
        tree,
        ["logo/logo-512.png", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
    )

    assert report.required_bump == "patch"


def test_added_derived_asset_requires_a_minor_bump(tmp_path):
    tree = _release_tree(tmp_path, version="v1.1.0")

    report = check_release_preflight.check_preflight(
        tree,
        ["logo/logo-new.png", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
        change_statuses={"logo/logo-new.png": "A"},
    )

    assert report.required_bump == "minor"


def test_removed_derived_asset_requires_a_major_bump(tmp_path):
    tree = _release_tree(tmp_path, version="v2.0.0")

    report = check_release_preflight.check_preflight(
        tree,
        ["logo/logo-512.png", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
        change_statuses={"logo/logo-512.png": "D"},
    )

    assert report.required_bump == "major"


def test_source_artwork_rejects_a_patch_bump(tmp_path):
    tree = _release_tree(tmp_path, version="v1.0.1")

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="only a patch bump.*minor is required",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


@pytest.mark.parametrize("version", ["v1.0.0", "v0.9.9"])
def test_release_required_change_rejects_non_incrementing_version(tmp_path, version):
    tree = _release_tree(tmp_path, version=version)

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="remains|lower than base",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["logo/logo-512.png", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


@pytest.mark.parametrize("version", ["1.1.0", "v1.01.0", "v1.1"])
def test_release_required_change_rejects_invalid_version(tmp_path, version):
    tree = _release_tree(tmp_path, version=version)

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="VERSION must contain exactly one",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["logo/logo-512.png", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


def test_intentionally_published_documentation_change_uses_patch(tmp_path):
    tree = _release_tree(tmp_path, version="v1.0.1")

    report = check_release_preflight.check_preflight(
        tree,
        ["docs/notes/release-publication.md", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
    )

    assert report.classification.kind == "documentation-only"
    assert report.required_bump == "patch"
    assert report.version == "v1.0.1"


def test_intentionally_published_metadata_change_uses_patch(tmp_path):
    tree = _release_tree(tmp_path, version="v1.0.1")

    report = check_release_preflight.check_preflight(
        tree,
        ["consumer_registry.json", "VERSION", "CHANGELOG.md"],
        base_version="v1.0.0",
    )

    assert report.classification.kind == "metadata-only"
    assert report.required_bump == "patch"


def test_release_tag_must_match_version(tmp_path):
    tree = _release_tree(tmp_path, version="v1.1.0")

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="release tag v1.2.0 does not match VERSION v1.1.0",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
            tag="v1.2.0",
        )


def test_publication_workflow_must_derive_tag_from_version(tmp_path):
    tree = _release_tree(tmp_path)
    (tree / WORKFLOW).write_text(
        "# Forgejo release-publication workflow\n"
        "Run tools/release_publish.py with --tag v1.1.0 and --ci-run to create a "
        "published Forgejo Release.\n",
        encoding="utf-8",
    )

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match='missing: .*--tag "\\$VERSION"',
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


@pytest.mark.parametrize(
    ("changelog", "message"),
    [
        (
            "# Changelog\n\n## [Unreleased]\n\n- next\n\n"
            "## [v1.1.0] - 2026-09-28\n",
            "must not be empty",
        ),
        (
            "# Changelog\n\n## [Unreleased]\n\n- next\n\n"
            "## [v1.1.0] - 2026-02-30\n\n- release\n",
            "invalid date",
        ),
        (
            "# Changelog\n\n## [Unreleased]\n\n- next\n\n"
            "## [1.1.0] - 2026-09-28\n\n- release\n",
            "dated release heading matching VERSION",
        ),
        (
            "# Changelog\n\n## [Unreleased]\n\n- next\n\n"
            "## [v1.1.0] - 2026-09-28\n\n- first\n\n"
            "## [v1.1.0] - 2026-09-29\n\n- duplicate\n",
            "multiple release headings",
        ),
    ],
)
def test_release_required_change_rejects_invalid_changelog_sections(
    tmp_path, changelog, message
):
    tree = _release_tree(tmp_path)
    (tree / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match=message,
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
            base_version="v1.0.0",
        )


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


@pytest.mark.parametrize("missing", [WORKFLOW, check_release_preflight.PUBLICATION_PUBLISHER_PATH])
def test_release_required_diff_rejects_missing_publication_inputs(tmp_path, missing):
    tree = _release_tree(tmp_path)
    (tree / missing).unlink()

    with pytest.raises(
        check_release_preflight.ReleasePreflightError,
        match="published-release workflow is missing|references missing publisher",
    ):
        check_release_preflight.check_preflight(
            tree,
            ["source/logo.svg", "VERSION", "CHANGELOG.md"],
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


def test_cli_accepts_an_arbitrary_published_tag_and_is_safe_to_rerun(tmp_path):
    tree, base_tag = _git_release_candidate(tmp_path)
    before_head = _git(tree, "rev-parse", "HEAD").stdout.strip()
    before_tags = _git(tree, "tag", "--list").stdout
    before_status = _git(tree, "status", "--short").stdout

    first = _run_cli(tree, base_tag)
    second = _run_cli(tree, base_tag)

    for result in (first, second):
        assert result.returncode == 0, result.stderr
        assert "PASS release-preflight: release-required" in result.stdout
        assert "PASS publication-workflow=" in result.stdout
    assert _git(tree, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _git(tree, "tag", "--list").stdout == before_tags
    assert _git(tree, "status", "--short").stdout == before_status


def test_cli_does_not_contact_forgejo_create_tags_or_publish(tmp_path, monkeypatch, capsys):
    tree, base_tag = _git_release_candidate(tmp_path)
    real_run = check_release_preflight.subprocess.run
    commands = []

    def run_without_side_effects(command, *args, **kwargs):
        commands.append(tuple(command))
        assert command[0] == "git"
        assert command[1] in {"diff", "show"}
        assert "push" not in command
        assert "tag" not in command
        assert "release_publish.py" not in command
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(check_release_preflight.subprocess, "run", run_without_side_effects)

    assert check_release_preflight.main(
        ["--root", str(tree), "--base", base_tag]
    ) == 0
    output = capsys.readouterr()

    assert commands
    assert "Forgejo" not in output.err
    assert "published" not in output.err
