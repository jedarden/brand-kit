from pathlib import Path
import subprocess

import pytest

from tools import release_publish


COMMIT = "a" * 40


def published(tag="v1.1.0", **overrides):
    record = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "target_commitish": COMMIT,
    }
    record.update(overrides)
    return record


def argo_workflow(run="brand-kit-ci-abc123", phase="Succeeded", commit=COMMIT, **overrides):
    workflow = {
        "metadata": {
            "name": run,
            "labels": {
                "workflows.argoproj.io/workflow-template": "brand-kit-ci",
            },
        },
        "status": {
            "phase": phase,
            "outputs": {
                "parameters": [{"name": "commit", "value": commit}],
            },
        },
    }
    workflow.update(overrides)
    return workflow


def git(root, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def initialize_git_repository(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "release-test")
    git(root, "config", "user.email", "release-test@example.test")
    (root / "release.txt").write_text("release\n", encoding="utf-8")
    git(root, "add", "release.txt")
    git(root, "commit", "-q", "-m", "release")
    return git(root, "rev-parse", "HEAD")


def test_local_tag_info_requires_an_annotated_tag_at_head(tmp_path):
    commit = initialize_git_repository(tmp_path)
    git(tmp_path, "tag", "-a", "v1.1.0", "-m", "release")
    assert release_publish.local_tag_info("v1.1.0", tmp_path) == commit

    git(tmp_path, "tag", "v1.1.1")
    with pytest.raises(release_publish.ReleaseError, match="annotated tag"):
        release_publish.local_tag_info("v1.1.1", tmp_path)

    (tmp_path / "release.txt").write_text("new head\n", encoding="utf-8")
    git(tmp_path, "add", "release.txt")
    git(tmp_path, "commit", "-q", "-m", "new head")
    with pytest.raises(release_publish.ReleaseError, match="HEAD"):
        release_publish.local_tag_info("v1.1.0", tmp_path)


def test_tag_validation_requires_an_exact_stable_tag():
    assert release_publish.validate_tag("v1.1.0") == "v1.1.0"
    for tag in ("v1.1", "v1.1.0-rc1", "1.1.0", " v1.1.0", "v1.1.0\n"):
        with pytest.raises(release_publish.ReleaseError):
            release_publish.validate_tag(tag)


def test_release_notes_are_taken_from_the_matching_changelog_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [Unreleased]\n\n### Added\n- current\n\n"
        "## [v1.1.0] - 2026-10-01\n\n### Added\n- release item\n\n"
        "## [v1.0.0]\n\nold\n",
        encoding="utf-8",
    )

    assert release_publish.release_notes_from_changelog(changelog, "v1.1.0") == (
        "### Added\n- release item"
    )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("## [Unreleased]\n\n- not a release\n", "no .*release section"),
        ("## [v1.1.0]\n\n## [v1.0.0]\n", "section .* empty"),
    ],
)
def test_release_notes_extraction_fails_closed_for_missing_or_empty_sections(
    tmp_path, contents, message
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(contents, encoding="utf-8")

    with pytest.raises(release_publish.ReleaseError, match=message):
        release_publish.release_notes_from_changelog(changelog, "v1.1.0")


def test_release_record_validation_fails_closed():
    assert release_publish.validate_release_record(published(), "v1.1.0")
    for record in (
        published(tag="v1.0.0"),
        published(draft=True),
        published(prerelease=True),
        {"tag_name": "v1.1.0", "draft": False},
    ):
        with pytest.raises(release_publish.ReleaseError):
            release_publish.validate_release_record(record, "v1.1.0")


def test_publish_release_creates_exact_non_draft_record(monkeypatch):
    calls = []
    created = False

    def request(method, url, payload=None, token=None, timeout=20.0):
        nonlocal created
        calls.append((method, url, payload, token))
        if method == "GET" and not created:
            raise release_publish.HttpFailure(404, "missing")
        if method == "POST":
            created = True
            return published()
        if method == "GET":
            return published()
        raise AssertionError(method)

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.publish_release(
        "v1.1.0",
        COMMIT,
        "release notes",
        api_url="https://forgejo.example/api/v1",
        repository="jedarden/brand-kit",
        token="secret",
    )

    assert result == published()
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][1] == "https://forgejo.example/api/v1/repos/jedarden/brand-kit/releases"
    assert calls[1][2] == {
        "tag_name": "v1.1.0",
        "target_commitish": COMMIT,
        "name": "v1.1.0",
        "body": "release notes",
        "draft": False,
        "prerelease": False,
    }
    assert calls[1][3] == "secret"


def test_publish_release_is_idempotent_for_an_existing_published_record(monkeypatch):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, url, payload))
        return published()

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.publish_release(
        "v1.1.0", COMMIT, "release notes", token="secret"
    )

    assert result == published()
    assert [call[0] for call in calls] == ["GET", "GET"]
    assert all(call[2] is None for call in calls)


def test_publish_release_accepts_a_409_only_after_reading_the_exact_record(monkeypatch):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, url, payload))
        if len(calls) == 1:
            raise release_publish.HttpFailure(404, "missing")
        if method == "POST":
            raise release_publish.HttpFailure(409, "already exists")
        return published()

    monkeypatch.setattr(release_publish, "request_json", request)

    assert release_publish.publish_release("v1.1.0", COMMIT, "notes", token="secret") == published()
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET"]
    assert all("github.com" not in call[1] for call in calls)


def test_publish_release_rejects_an_existing_prerelease(monkeypatch):
    prerelease = published(prerelease=True)
    monkeypatch.setattr(release_publish, "request_json", lambda *args, **kwargs: prerelease)

    with pytest.raises(release_publish.ReleaseError, match="stable published release"):
        release_publish.publish_release("v1.1.0", COMMIT, "notes", token="secret")


def test_publish_release_publishes_an_existing_draft(monkeypatch):
    calls = []
    draft = published(draft=True, id=17)
    updated = published(id=17)

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, url, payload))
        if len(calls) == 1:
            return draft
        if method == "PATCH":
            return updated
        return updated

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.publish_release(
        "v1.1.0", COMMIT, "release notes", token="secret"
    )

    assert result == updated
    assert [call[0] for call in calls] == ["GET", "PATCH", "GET"]
    assert calls[1][1].endswith("/releases/17")
    assert calls[1][2] == {"draft": False}


def test_publish_release_requires_a_token_before_making_a_request():
    with pytest.raises(release_publish.ReleaseError, match="FORGEJO_TOKEN"):
        release_publish.publish_release("v1.1.0", COMMIT, "release notes")


def test_mirror_gate_requires_exact_peeled_refs(monkeypatch):
    observed = iter([COMMIT, None])

    def remote(remote_name, tag, root=release_publish.ROOT):
        return next(observed)

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)

    with pytest.raises(release_publish.ReleaseError, match="has not caught up"):
        release_publish.wait_for_mirror(
            "v1.1.0",
            COMMIT,
            root=Path("/does/not/matter"),
            timeout=0,
            interval=0,
        )


def test_mirror_gate_rejects_a_canonical_tag_at_the_wrong_commit(monkeypatch):
    observed = []

    def remote(remote_name, tag, root=release_publish.ROOT):
        observed.append(remote_name)
        return "b" * 40

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)

    with pytest.raises(release_publish.ReleaseError, match="origin .* expected"):
        release_publish.wait_for_mirror("v1.1.0", COMMIT, timeout=0, interval=0)
    assert observed == ["origin"]


def test_mirror_gate_accepts_matching_canonical_and_mirror_commits(monkeypatch):
    observed = []

    def remote(remote_name, tag, root=release_publish.ROOT):
        observed.append(remote_name)
        return COMMIT

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)
    release_publish.wait_for_mirror("v1.1.0", COMMIT, timeout=0, interval=0)
    assert observed == ["origin", "github"]


def test_argo_attestation_reads_one_workflow_and_accepts_exact_succeeded_commit(monkeypatch):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0, **kwargs):
        calls.append((method, url, payload, token, kwargs))
        return argo_workflow()

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.attest_argo_ci_run(
        "brand-kit-ci/brand-kit-ci-abc123",
        COMMIT,
        api_url="https://argo.example",
        token="argo-read-only",
    )

    assert result["status"]["phase"] == "Succeeded"
    assert calls == [
        (
            "GET",
            "https://argo.example/api/v1/workflows/argo-workflows/brand-kit-ci-abc123",
            None,
            "argo-read-only",
            {"authorization_scheme": "Bearer", "service": "Argo API"},
        )
    ]


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (argo_workflow(phase="Failed"), "only Succeeded is accepted"),
        (argo_workflow(commit="b" * 40), "attests commit"),
        (
            argo_workflow(status={"phase": "Succeeded"}),
            "no structured full-commit attestation",
        ),
    ],
)
def test_argo_attestation_fails_closed_for_phase_commit_or_missing_evidence(
    monkeypatch, workflow, message
):
    monkeypatch.setattr(release_publish, "request_json", lambda *args, **kwargs: workflow)

    with pytest.raises(release_publish.ReleaseError, match=message):
        release_publish.attest_argo_ci_run("brand-kit-ci-abc123", COMMIT)


def test_argo_attestation_rejects_conflicting_structured_commits(monkeypatch):
    workflow = argo_workflow()
    workflow["status"]["nodes"] = {
        "ci": {
            "outputs": {
                "parameters": [{"name": "revision", "value": "b" * 40}],
            }
        }
    }
    monkeypatch.setattr(release_publish, "request_json", lambda *args, **kwargs: workflow)

    with pytest.raises(release_publish.ReleaseError, match="conflicting commit attestations"):
        release_publish.attest_argo_ci_run("brand-kit-ci-abc123", COMMIT)


def test_verify_only_reads_the_published_release_without_writing_or_pushing(monkeypatch, capsys):
    events = []
    api_calls = []

    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    monkeypatch.setattr(
        release_publish,
        "verify_ready",
        lambda *args, **kwargs: events.append("verify-ready") or COMMIT,
    )
    monkeypatch.setattr(
        release_publish,
        "attest_argo_ci_run",
        lambda *args, **kwargs: events.append("argo") or argo_workflow(),
    )
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: events.append("mirror"),
    )
    monkeypatch.setattr(
        release_publish,
        "publish_release",
        lambda *args, **kwargs: pytest.fail("verify-only must not publish"),
    )

    def request(method, url, payload=None, token=None, timeout=20.0):
        api_calls.append((method, url, payload))
        return published()

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.main(
        [
            "--tag",
            "v1.1.0",
            "--ci-run",
            "brand-kit-ci-abc123",
            "--verify-only",
            "--notes",
            "unused in verify-only mode",
            "--api-url",
            "https://forgejo.example/api/v1",
        ]
    )

    assert result == 0
    assert events == ["verify-ready", "argo", "mirror"]
    assert [(method, payload) for method, _, payload in api_calls] == [("GET", None)]
    assert api_calls[0][1].startswith("https://forgejo.example/api/v1/")
    assert "github.com" not in api_calls[0][1]
    assert "READY" in capsys.readouterr().out


def test_readiness_checks_only_use_read_commands_and_never_push_to_a_remote(monkeypatch):
    commands = []

    def run_git(arguments, root=release_publish.ROOT):
        commands.append(arguments)
        if arguments == ["cat-file", "-t", "refs/tags/v1.1.0"]:
            return "tag"
        if arguments == ["rev-parse", "--verify", "refs/tags/v1.1.0^{commit}"]:
            return COMMIT
        if arguments == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return COMMIT
        if arguments == ["remote", "get-url", "origin"]:
            return "https://git.ardenone.com/jedarden/brand-kit.git"
        if arguments == ["remote", "get-url", "github"]:
            return "https://github.com/jedarden/brand-kit.git"
        raise AssertionError(arguments)

    def run_git_optional(arguments, root=release_publish.ROOT):
        commands.append(arguments)
        assert arguments[:2] == ["ls-remote", "--exit-code"]
        return subprocess.CompletedProcess(
            ["git", *arguments],
            0,
            stdout=f"{COMMIT} refs/tags/v1.1.0^{{}}\n",
            stderr="",
        )

    monkeypatch.setattr(release_publish, "run_git", run_git)
    monkeypatch.setattr(release_publish, "run_git_optional", run_git_optional)

    assert release_publish.verify_ready("v1.1.0", timeout=0, interval=0) == COMMIT
    assert all(command[0] not in {"push", "fetch", "send-pack"} for command in commands)
    assert all("push" not in command for command in commands)


def test_workflow_documentation_keeps_forgejo_authoritative():
    document = Path("docs/notes/release-publication.md").read_text(encoding="utf-8")
    assert "git tag -a" in document
    assert 'git push origin "refs/tags/$VERSION"' in document
    assert "brand-kit-ci" in document
    assert "tools/release_publish.py" in document
    assert "read-only Argo API" in document
    assert "structured" in document
    assert "full SHA" in document
    assert "ARGO_TOKEN" in document
    assert "--verify-only" in document
    assert "READY" in document
    assert "GitHub Releases API" in document
    assert "sole release authority" in document
    assert "tools/consumer_sync.py" in document


def test_workflow_documentation_is_linked_from_the_operator_entrypoints():
    readme = Path("README.md").read_text(encoding="utf-8")
    plan = Path("docs/plan/plan.md").read_text(encoding="utf-8")
    consumer = Path("docs/notes/post-tag-consumer-update.md").read_text(encoding="utf-8")
    assert "docs/notes/release-publication.md" in readme
    assert "notes/release-publication.md" in plan
    assert "release-publication.md" in consumer
    assert "release_publish.py" in readme
    assert "release_publish.py" in plan


def test_workflow_script_does_not_reference_github_release_api():
    source = Path("tools/release_publish.py").read_text(encoding="utf-8")
    assert "api.github.com" not in source
    assert "gh release create" not in source
    assert "git push" not in source
