from pathlib import Path
import os
import subprocess
import sys

import pytest

from tools import check_release_preflight


ROOT = Path(__file__).resolve().parent
WORKFLOW = check_release_preflight.PUBLICATION_WORKFLOW_PATH
TOOL = ROOT / "tools" / "check_release_preflight.py"

# Renderable SVG placeholders in the resvg-stub protocol (see
# _install_resvg_stub): the stub renders ``data-fill`` as a solid color, and
# ``data-alpha`` marks the transparent master so it renders RGBA.
OPAQUE_FILL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'data-fill="200,10,10"></svg>'
)
TRANSPARENT_FILL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'data-fill="200,10,10" data-alpha="255"></svg>'
)


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


def _install_resvg_stub(tmp_path: Path) -> Path:
    """An executable named ``resvg`` rendering solid ``data-fill`` colors
    (same stub protocol as the transparent-logo sync suite), installed in a
    hidden bin directory so ``shutil.which("resvg")`` finds it without the
    pinned binary."""

    bin_dir = tmp_path / ".resvg-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "resvg"
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


def _stub_resvg_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    stub = _install_resvg_stub(tmp_path)
    return stub, dict(os.environ, PATH=f"{stub.parent}{os.pathsep}{os.environ['PATH']}")


def _git_release_candidate(
    tmp_path: Path,
    *,
    base_tag: str = "published-release-2026-09-27",
    base_version: str = "v7.4.2",
    candidate_version: str = "v7.5.0",
    base_opaque_fill: str = "200,10,10",
    base_transparent_fill: str = "10,10,200",
    candidate_opaque_fill: str | None = None,
    candidate_transparent_fill: str | None = None,
) -> tuple[Path, str]:
    tree = _release_tree(tmp_path, version=base_version)
    (tree / "source").mkdir()
    (tree / "source/logo.svg").write_text(
        OPAQUE_FILL_SVG.replace("200,10,10", base_opaque_fill), encoding="utf-8"
    )
    (tree / "source/logo-transparent.svg").write_text(
        TRANSPARENT_FILL_SVG.replace("200,10,10", base_transparent_fill),
        encoding="utf-8",
    )

    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "VERSION", "CHANGELOG.md", "docs", "source", "tools")
    _git(tree, "commit", "-qm", "published release")
    _git(tree, "tag", base_tag)

    _release_tree(tree, version=candidate_version)
    (tree / "source/logo.svg").write_text(
        OPAQUE_FILL_SVG.replace(
            "200,10,10", candidate_opaque_fill or base_transparent_fill
        ),
        encoding="utf-8",
    )
    staged = ["VERSION", "CHANGELOG.md", "source/logo.svg"]
    if candidate_transparent_fill is not None:
        (tree / "source/logo-transparent.svg").write_text(
            TRANSPARENT_FILL_SVG.replace(
                "200,10,10", candidate_transparent_fill
            ),
            encoding="utf-8",
        )
        staged.append("source/logo-transparent.svg")
    _git(tree, "add", *staged)
    _git(tree, "commit", "-qm", "candidate release")
    return tree, base_tag


def _run_cli(
    tree: Path, base: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(tree), "--base", base],
        check=False,
        capture_output=True,
        text=True,
        env=env,
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
    _, env = _stub_resvg_env(tmp_path)
    before_head = _git(tree, "rev-parse", "HEAD").stdout.strip()
    before_tags = _git(tree, "tag", "--list").stdout
    before_status = _git(tree, "status", "--short").stdout

    first = _run_cli(tree, base_tag, env=env)
    second = _run_cli(tree, base_tag, env=env)

    for result in (first, second):
        assert result.returncode == 0, result.stderr
        assert "PASS release-preflight: release-required" in result.stdout
        assert "PASS publication-workflow=" in result.stdout
        assert "TRIGGER transparent-logo-sync" in result.stdout
        assert "PASS transparent-logo-sync: in-sync" in result.stdout
    assert _git(tree, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _git(tree, "tag", "--list").stdout == before_tags
    assert _git(tree, "status", "--short").stdout == before_status


def test_cli_does_not_contact_forgejo_create_tags_or_publish(tmp_path, monkeypatch, capsys):
    tree, base_tag = _git_release_candidate(tmp_path)
    stub, _ = _stub_resvg_env(tmp_path)
    real_run = check_release_preflight.subprocess.run
    commands = []

    def run_without_side_effects(command, *args, **kwargs):
        commands.append(tuple(command))
        if Path(command[0]).name == "resvg":
            return real_run(command, *args, **kwargs)
        assert command[0] == "git"
        assert command[1] in {"diff", "show"}
        assert "push" not in command
        assert "tag" not in command
        assert "release_publish.py" not in command
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(check_release_preflight.subprocess, "run", run_without_side_effects)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")

    assert check_release_preflight.main(
        ["--root", str(tree), "--base", base_tag]
    ) == 0
    output = capsys.readouterr()

    assert commands
    assert any(Path(command[0]).name == "resvg" for command in commands)
    assert "Forgejo" not in output.err
    assert "published" not in output.err


def test_cli_fails_when_the_opaque_redesign_diverges_without_a_decision(tmp_path):
    tree, base_tag = _git_release_candidate(tmp_path, candidate_opaque_fill="0,0,255")
    _, env = _stub_resvg_env(tmp_path)

    result = _run_cli(tree, base_tag, env=env)

    assert result.returncode == 1
    assert "PASS release-preflight: release-required" in result.stdout
    assert "TRIGGER transparent-logo-sync" in result.stdout
    assert "FAIL transparent-logo-sync" in result.stdout
    assert "diverges from the rendered opaque" in result.stdout
    assert "source/logo-transparent.svg" in result.stdout


def test_cli_accepts_a_same_diff_transparent_change_as_the_decision(tmp_path):
    tree, base_tag = _git_release_candidate(
        tmp_path,
        candidate_opaque_fill="0,0,255",
        candidate_transparent_fill="200,10,10",
    )
    _, env = _stub_resvg_env(tmp_path)

    result = _run_cli(tree, base_tag, env=env)

    assert result.returncode == 0, result.stdout
    assert "REVIEW transparent-logo-sync: divergence acknowledged" in result.stdout


def test_transparent_logo_sync_gate_passes_through_not_triggered_diffs(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "baseline")
    _git(tmp_path, "tag", "base")
    (tmp_path / "README.md").write_text("# changed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "unrelated change")

    lines, failed = check_release_preflight.transparent_logo_sync_gate(
        tmp_path, "base", resvg=str(tmp_path / "unused")
    )

    assert failed is False
    assert any(
        line.startswith("PASS transparent-logo-sync: not-triggered") for line in lines
    )


def test_transparent_logo_sync_gate_reports_in_sync_with_stub_renderer(tmp_path):
    tree, base_tag = _git_release_candidate(tmp_path)
    stub = _install_resvg_stub(tmp_path)

    lines, failed = check_release_preflight.transparent_logo_sync_gate(
        tree, base_tag, resvg=str(stub)
    )

    assert failed is False
    assert any("in-sync" in line for line in lines)


def test_transparent_logo_sync_gate_fails_on_divergence(tmp_path):
    tree, base_tag = _git_release_candidate(tmp_path, candidate_opaque_fill="0,0,255")
    stub = _install_resvg_stub(tmp_path)

    lines, failed = check_release_preflight.transparent_logo_sync_gate(
        tree, base_tag, resvg=str(stub)
    )

    assert failed is True
    assert any("diverges from the rendered opaque" in line for line in lines)


def test_transparent_logo_sync_gate_fails_closed_without_a_renderer(tmp_path):
    tree, base_tag = _git_release_candidate(tmp_path)

    lines, failed = check_release_preflight.transparent_logo_sync_gate(
        tree, base_tag, resvg=str(tmp_path / "missing-resvg")
    )

    assert failed is True
    assert any("resvg is not available" in line for line in lines)
