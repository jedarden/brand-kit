import io
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.error

import pytest

from tools import release_publish


COMMIT = "a" * 40
ARGO_READ_TOKEN = "argo-read-test-token"


def published(tag="v1.1.0", **overrides):
    record = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "target_commitish": COMMIT,
        "body": "release notes",
    }
    record.update(overrides)
    return record


def argo_workflow(
    run="brand-kit-ci-abc123",
    phase="Succeeded",
    commit=COMMIT,
    output_name="commit",
    **overrides,
):
    workflow = {
        "metadata": {
            "name": run,
            "labels": {
                "workflows.argoproj.io/workflow-template": "brand-kit-ci",
            },
        },
        "status": {
            "phase": phase,
        },
    }
    if output_name is not None:
        workflow["status"]["outputs"] = {
            "parameters": [{"name": output_name, "value": commit}],
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


@pytest.mark.parametrize(
    ("origin_url", "mirror_url", "message"),
    [
        (
            "https://git.ardenone.com/jedarden/other-repo.git",
            release_publish.DEFAULT_MIRROR_REPOSITORY,
            "origin.*identity",
        ),
        (
            release_publish.DEFAULT_CANONICAL_REPOSITORY,
            "https://github.com/jedarden/other-repo.git",
            "github.*identity",
        ),
        (
            "https://user:secret@git.ardenone.com/jedarden/brand-kit.git",
            release_publish.DEFAULT_MIRROR_REPOSITORY,
            "origin.*identity",
        ),
    ],
)
def test_validate_remote_roles_enforces_the_canonical_and_mirror_identities(
    monkeypatch, origin_url, mirror_url, message
):
    urls = {"origin": origin_url, "github": mirror_url}
    monkeypatch.setattr(
        release_publish,
        "remote_url",
        lambda remote, root=release_publish.ROOT: urls[remote],
    )

    with pytest.raises(release_publish.ReleaseError, match=message):
        release_publish.validate_remote_roles("origin", "github")


def test_validate_remote_roles_accepts_git_suffix_and_trailing_slash(monkeypatch):
    urls = {
        "origin": "https://git.ardenone.com/jedarden/brand-kit/",
        "github": "https://github.com/jedarden/brand-kit",
    }
    monkeypatch.setattr(
        release_publish,
        "remote_url",
        lambda remote, root=release_publish.ROOT: urls[remote],
    )

    assert release_publish.validate_remote_roles("origin", "github") == (
        urls["origin"],
        urls["github"],
    )


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


def test_release_payload_is_derived_from_the_exact_commit_and_is_deterministic(tmp_path):
    commit = initialize_git_repository(tmp_path)
    first = release_publish.build_release_payload("v1.1.0", commit, tmp_path)

    (tmp_path / "release.txt").write_text("working tree drift\n", encoding="utf-8")
    repeated = release_publish.build_release_payload("v1.1.0", commit, tmp_path)

    assert first["manifest"] == repeated["manifest"]
    assert [item.content for item in first["attachments"]] == [
        item.content for item in repeated["attachments"]
    ]
    assert first["manifest"]["commit"] == commit
    assert first["manifest"]["files"] == [
        {
            "path": "release.txt",
            "sha256": "3933e3274b63dc01fd286d6d939a626867c83a4603fc4347e0f8b8856f1b98fd",
            "size": 8,
        }
    ]
    names = release_publish.release_payload_names("v1.1.0")
    assert [item.name for item in first["attachments"]] == list(names.values())

    archive = next(
        item.content for item in first["attachments"] if item.name == names["archive"]
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert tar.extractfile(f"brand-kit-v1.1.0/release.txt").read() == b"release\n"


def test_publish_release_uploads_missing_payload_once_and_reuses_it_on_republish(
    tmp_path, monkeypatch
):
    commit = initialize_git_repository(tmp_path)
    payload = release_publish.build_release_payload("v1.1.0", commit, tmp_path)
    record = published(target_commitish=commit, id=17, body="release notes", assets=[])
    calls = []
    uploads = []

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, url, payload))
        return record

    def upload(api_url, repository, release_id, attachment, token):
        uploads.append(attachment.name)
        record["assets"].append(
            {
                "name": attachment.name,
                "size": attachment.size,
                "sha256": attachment.sha256,
            }
        )
        return {"name": attachment.name}

    monkeypatch.setattr(release_publish, "request_json", request)
    monkeypatch.setattr(release_publish, "upload_release_asset", upload)

    assert release_publish.publish_release(
        "v1.1.0",
        commit,
        "release notes",
        token="secret",
        payload=payload,
    ) == record
    assert len(uploads) == 3

    calls.clear()
    assert release_publish.publish_release(
        "v1.1.0",
        commit,
        "release notes",
        token="secret",
        payload=payload,
    ) == record
    assert uploads == list(release_publish.release_payload_names("v1.1.0").values())
    assert [method for method, _, _ in calls] == ["GET", "GET"]

@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("## [Unreleased]\n\n- not a release\n", "no .*release section"),
        ("## [v1.1.0]\n\n## [v1.0.0]\n\nold\n", "section .* empty"),
    ],
)
def test_release_notes_extraction_fails_closed_for_missing_or_empty_sections(
    tmp_path, contents, message
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(contents, encoding="utf-8")

    with pytest.raises(release_publish.ReleaseError, match=message):
        release_publish.release_notes_from_changelog(changelog, "v1.1.0")


@pytest.mark.parametrize(
    "heading",
    (
        "## v1.1.0",
        "## [v1.1.0] - not-a-date",
        "## [v1.1.0] - 2026-02-30",
        "## [v1.1.0] extra",
    ),
)
def test_release_notes_extraction_rejects_malformed_target_sections(tmp_path, heading):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{heading}\n\n### Added\n- release item\n", encoding="utf-8")

    with pytest.raises(release_publish.ReleaseError, match="malformed|invalid"):
        release_publish.release_notes_from_changelog(changelog, "v1.1.0")


def test_release_notes_extraction_rejects_duplicate_target_sections(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [v1.1.0]\n\nfirst\n\n## [v1.1.0]\n\nsecond\n",
        encoding="utf-8",
    )

    with pytest.raises(release_publish.ReleaseError, match="multiple"):
        release_publish.release_notes_from_changelog(changelog, "v1.1.0")


def test_release_notes_extraction_reports_a_different_version(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [v1.2.0]\n\n- another release\n", encoding="utf-8")

    with pytest.raises(release_publish.ReleaseError, match="v1.1.0.*v1.2.0"):
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


@pytest.mark.parametrize(
    "record",
    (
        published(target_commitish="b" * 40),
        published(body="different notes"),
        published(target_commitish="main"),
        published(body=None),
    ),
)
def test_release_record_validation_requires_exact_target_and_notes(record):
    with pytest.raises(release_publish.ReleaseError):
        release_publish.validate_release_record(
            record,
            "v1.1.0",
            expected_commit=COMMIT,
            expected_notes="release notes",
        )


def test_release_record_validation_accepts_exact_target_and_notes():
    assert release_publish.validate_release_record(
        published(),
        "v1.1.0",
        expected_commit=COMMIT,
        expected_notes="release notes",
    ) == published()


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

    assert release_publish.publish_release(
        "v1.1.0", COMMIT, "release notes", token="secret"
    ) == published()
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET"]
    assert all("github.com" not in call[1] for call in calls)


def test_publish_retry_resolves_an_ambiguous_create_without_a_second_post(monkeypatch):
    calls = []
    release_exists = False

    def request(method, url, payload=None, token=None, timeout=20.0):
        nonlocal release_exists
        calls.append((method, url, payload))
        if method == "GET":
            if not release_exists:
                raise release_publish.HttpFailure(404, "missing")
            return published()
        if method == "POST":
            release_exists = True
            raise release_publish.ReleaseError("cannot reach Forgejo API: timed out")
        raise AssertionError(method)

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="timed out"):
        release_publish.publish_release(
            "v1.1.0", COMMIT, "release notes", token="secret"
        )

    assert release_publish.publish_release(
        "v1.1.0", COMMIT, "release notes", token="secret"
    ) == published()
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET"]
    assert sum(method == "POST" for method, _, _ in calls) == 1


def test_publish_retry_recovers_after_post_write_verification_timeout(monkeypatch):
    calls = []
    get_count = 0

    def request(method, url, payload=None, token=None, timeout=20.0):
        nonlocal get_count
        calls.append((method, url, payload))
        if method == "GET":
            get_count += 1
            if get_count == 1:
                raise release_publish.HttpFailure(404, "missing")
            if get_count == 2:
                raise release_publish.HttpFailure(503, "read timed out")
            return published()
        if method == "POST":
            return published()
        raise AssertionError(method)

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="cannot read Forgejo release"):
        release_publish.publish_release(
            "v1.1.0", COMMIT, "release notes", token="secret"
        )

    assert release_publish.publish_release(
        "v1.1.0", COMMIT, "release notes", token="secret"
    ) == published()
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET", "GET"]
    assert sum(method == "POST" for method, _, _ in calls) == 1


def test_publish_release_rejects_an_existing_prerelease(monkeypatch):
    prerelease = published(prerelease=True)
    monkeypatch.setattr(release_publish, "request_json", lambda *args, **kwargs: prerelease)

    with pytest.raises(release_publish.ReleaseError, match="stable published release"):
        release_publish.publish_release("v1.1.0", COMMIT, "notes", token="secret")


@pytest.mark.parametrize(
    "record",
    (
        published(target_commitish="b" * 40),
        published(body="not the changelog"),
    ),
)
def test_publish_release_rejects_an_existing_record_with_mismatched_consistency(
    monkeypatch, record
):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, url, payload))
        return record

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="target|notes"):
        release_publish.publish_release(
            "v1.1.0", COMMIT, "release notes", token="secret"
        )
    assert [call[0] for call in calls] == ["GET"]


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


def test_denied_forgejo_credential_never_reaches_release_write(monkeypatch):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0):
        calls.append((method, payload))
        raise release_publish.HttpFailure(403, "insufficient repository scope")

    monkeypatch.setattr(release_publish, "request_json", request)
    with pytest.raises(release_publish.ReleaseError, match="cannot read Forgejo release"):
        release_publish.publish_release(
            "v1.1.0", COMMIT, "release notes", token="forgejo-denied-token"
        )

    assert calls == [("GET", None)]


@pytest.mark.parametrize("token", (None, "", "   "))
def test_required_tokens_reject_missing_or_blank_values(token):
    with pytest.raises(release_publish.ReleaseError, match="ARGO_TOKEN"):
        release_publish.require_token(token, "ARGO_TOKEN")


def test_publisher_requires_canonical_environment_tokens_before_running_gates(
    monkeypatch, capsys
):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.delenv("ARGO_TOKEN", raising=False)
    monkeypatch.setenv("FORGEJO_API_TOKEN", "legacy-forgejo-token")
    monkeypatch.setenv("ARGO_API_TOKEN", "legacy-argo-token")

    result = release_publish.main(
        ["--tag", "v1.1.0", "--ci-run", "brand-kit-ci-abc123"]
    )

    assert result == 1
    assert "FORGEJO_TOKEN" in capsys.readouterr().err


def test_request_places_token_in_header_not_url_or_argv(monkeypatch):
    token = "forgejo-header-test-token"
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    release_publish.request_json(
        "GET",
        "https://forgejo.example/api/v1/releases/tags/v1.1.0",
        token=token,
    )

    request = requests[0]
    assert token not in request.full_url
    assert token not in " ".join(sys.argv)
    assert ("Authorization", f"token {token}") in request.header_items()


def test_http_failures_redact_tokens_from_cli_diagnostics(monkeypatch, capsys):
    token = "forgejo-error-test-token"

    def urlopen(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://forgejo.example/api/v1/releases/tags/v1.1.0",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(f"access denied for {token}".encode()),
        )

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    with pytest.raises(release_publish.HttpFailure) as error:
        release_publish.request_json(
            "GET",
            "https://forgejo.example/api/v1/releases/tags/v1.1.0",
            token=token,
        )

    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_denied_argo_credential_blocks_publication_and_does_not_log_token(
    monkeypatch, capsys
):
    token = "argo-denied-test-token"
    monkeypatch.setenv("FORGEJO_TOKEN", "forgejo-test-token")
    monkeypatch.setenv("ARGO_TOKEN", token)
    monkeypatch.setattr(
        release_publish,
        "release_notes_from_changelog",
        lambda *args, **kwargs: "release notes",
    )
    monkeypatch.setattr(
        release_publish,
        "verify_ready",
        lambda *args, **kwargs: COMMIT,
    )
    published = []
    monkeypatch.setattr(
        release_publish,
        "publish_release",
        lambda *args, **kwargs: published.append(args),
    )

    def urlopen(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://argo.example/api/v1/workflows/argo-workflows/brand-kit-ci-abc123",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(f"denied {token}".encode()),
        )

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    result = release_publish.main(
        ["--tag", "v1.1.0", "--ci-run", "brand-kit-ci-abc123"]
    )

    assert result == 1
    assert published == []
    assert token not in capsys.readouterr().err


def test_mirror_gate_requires_exact_peeled_refs(monkeypatch):
    observed = iter([COMMIT, None])

    def remote(remote_name, tag, root=release_publish.ROOT):
        return next(observed)

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="has not caught up"):
        release_publish.wait_for_mirror(
            "v1.1.0",
            COMMIT,
            root=Path("/does/not/matter"),
            timeout=0,
            interval=0,
        )


def test_mirror_timeout_can_be_retried_without_changing_the_release_ref(monkeypatch):
    observed = iter([COMMIT, None, COMMIT, COMMIT])

    monkeypatch.setattr(
        release_publish,
        "remote_tag_commit",
        lambda remote_name, tag, root=release_publish.ROOT: next(observed),
    )
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="has not caught up"):
        release_publish.wait_for_mirror(
            "v1.1.0", COMMIT, timeout=0, interval=0
        )

    release_publish.wait_for_mirror(
        "v1.1.0", COMMIT, timeout=1, interval=0
    )


def test_mirror_gate_rejects_a_canonical_tag_at_the_wrong_commit(monkeypatch):
    observed = []

    def remote(remote_name, tag, root=release_publish.ROOT):
        observed.append(remote_name)
        return "b" * 40

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="origin .* expected"):
        release_publish.wait_for_mirror("v1.1.0", COMMIT, timeout=0, interval=0)
    assert observed == ["origin"]


def test_mirror_gate_accepts_matching_canonical_and_mirror_commits(monkeypatch):
    observed = []

    def remote(remote_name, tag, root=release_publish.ROOT):
        observed.append(remote_name)
        return COMMIT

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote)
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )
    release_publish.wait_for_mirror("v1.1.0", COMMIT, timeout=0, interval=0)
    assert observed == ["origin", "github"]


def test_mirror_gate_blocks_partial_main_propagation_even_when_the_tag_matches(
    monkeypatch,
):
    def branch(remote_name, branch, root=release_publish.ROOT):
        return COMMIT if remote_name == "origin" else "b" * 40

    monkeypatch.setattr(release_publish, "remote_branch_commit", branch)
    monkeypatch.setattr(
        release_publish,
        "remote_tag_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="has not caught up.*main"):
        release_publish.wait_for_mirror(
            "v1.1.0",
            COMMIT,
            timeout=0,
            interval=0,
        )


def test_mirror_gate_requires_the_canonical_main_ref_to_match_the_expected_commit(
    monkeypatch,
):
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: "b" * 40,
    )
    monkeypatch.setattr(
        release_publish,
        "remote_tag_commit",
        lambda *args, **kwargs: pytest.fail("tag should not be checked after main mismatch"),
    )

    with pytest.raises(release_publish.ReleaseError, match="main.*expected"):
        release_publish.wait_for_mirror(
            "v1.1.0",
            COMMIT,
            expected_main_commit=COMMIT,
            timeout=0,
            interval=0,
        )


def test_verify_ready_passes_the_release_commit_as_the_expected_main_commit(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(release_publish, "local_tag_info", lambda *args, **kwargs: COMMIT)
    monkeypatch.setattr(release_publish, "validate_remote_roles", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert release_publish.verify_ready("v1.1.0", timeout=0, interval=0) == COMMIT
    assert calls[0][1]["expected_main_commit"] == COMMIT


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
        token=ARGO_READ_TOKEN,
    )

    assert result["status"]["phase"] == "Succeeded"
    assert calls == [
        (
            "GET",
            "https://argo.example/api/v1/workflows/argo-workflows/brand-kit-ci-abc123",
            None,
            ARGO_READ_TOKEN,
            {"authorization_scheme": "Bearer", "service": "Argo API"},
        )
    ]


def test_argo_attestation_uses_the_durable_record_after_workflow_reaping(monkeypatch):
    calls = []
    attestation_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/"
        "attestations/brand-kit-ci/v1/watcher-uid/attestations.json"
    )

    def request(method, url, payload=None, token=None, timeout=20.0, **kwargs):
        calls.append((method, url, token, kwargs))
        if "/workflows/" in url:
            raise release_publish.HttpFailure(404, "workflow reaped", service="Argo API")
        return {
            "schema": release_publish.CI_ATTESTATION_SCHEMA,
            "watcher_workflow_uid": "watcher-uid",
            "attestations": [
                {
                    "commit": COMMIT,
                    "workflow_name": "brand-kit-ci-abc123",
                    "workflow_uid": "brand-kit-ci-uid",
                    "phase": "Succeeded",
                    "finished_at": "2026-09-27T12:30:00Z",
                }
            ],
        }

    monkeypatch.setattr(release_publish, "request_json", request)

    result = release_publish.attest_argo_ci_run(
        "brand-kit-ci/brand-kit-ci-abc123",
        COMMIT,
        api_url="https://argo.example",
        token=ARGO_READ_TOKEN,
        attestation_url=attestation_url,
    )

    assert result["commit"] == COMMIT
    assert result["phase"] == "Succeeded"
    assert result["finished_at"] == "2026-09-27T12:30:00Z"
    assert [call[1] for call in calls] == [
        "https://argo.example/api/v1/workflows/argo-workflows/brand-kit-ci-abc123",
        attestation_url,
    ]
    assert calls[1][2] is None


@pytest.mark.parametrize(
    "mutate",
    (
        lambda record: record.update(schema=release_publish.CI_ATTESTATION_SCHEMA),
        lambda record: record.update(finished_at="2026-09-27T12:30:00+00:00"),
        lambda record: record.update(commit=COMMIT.upper()),
    ),
)
def test_release_consumer_fails_closed_on_schema_invalid_durable_attestation(
    monkeypatch, mutate
):
    attestation_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
        "brand-kit-ci/v1/watcher-uid/attestations.json"
    )
    document = {
        "schema": release_publish.CI_ATTESTATION_SCHEMA,
        "watcher_workflow_uid": "watcher-uid",
        "attestations": [
            {
                "commit": COMMIT,
                "workflow_name": "brand-kit-ci-abc123",
                "workflow_uid": "brand-kit-ci-uid",
                "phase": "Succeeded",
                "finished_at": "2026-09-27T12:30:00Z",
            }
        ],
    }
    mutate(document["attestations"][0])

    def request(method, url, **kwargs):
        if "/workflows/" in url:
            raise release_publish.HttpFailure(404, "workflow reaped", service="Argo API")
        return document

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="record 0 is invalid"):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci/brand-kit-ci-abc123",
            COMMIT,
            token=ARGO_READ_TOKEN,
            attestation_url=attestation_url,
        )


def test_durable_attestation_requires_the_exact_workflow_when_commits_repeat(monkeypatch):
    attestation_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
        "brand-kit-ci/v1/watcher-uid/attestations.json"
    )

    def request(method, url, **kwargs):
        if "/workflows/" in url:
            raise release_publish.HttpFailure(404, "workflow reaped", service="Argo API")
        return {
            "schema": release_publish.CI_ATTESTATION_SCHEMA,
            "watcher_workflow_uid": "watcher-uid",
            "attestations": [
                {
                    "commit": COMMIT,
                    "workflow_name": "brand-kit-ci-a-different-run",
                    "workflow_uid": "brand-kit-ci-uid",
                    "phase": "Succeeded",
                    "finished_at": "2026-09-27T12:30:00Z",
                }
            ],
        }

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="no record.*workflow"):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci/brand-kit-ci-abc123",
            COMMIT,
            api_url="https://argo.example",
            token=ARGO_READ_TOKEN,
            attestation_url=attestation_url,
        )


def test_durable_attestation_rejects_an_object_replaced_under_another_watcher_url(
    monkeypatch,
):
    attestation_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
        "brand-kit-ci/v1/watcher-uid/attestations.json"
    )

    def request(method, url, **kwargs):
        if "/workflows/" in url:
            raise release_publish.HttpFailure(404, "workflow reaped", service="Argo API")
        return {
            "schema": release_publish.CI_ATTESTATION_SCHEMA,
            "watcher_workflow_uid": "another-watcher-uid",
            "attestations": [],
        }

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="does not match its object URL"):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci/brand-kit-ci-abc123",
            COMMIT,
            token=ARGO_READ_TOKEN,
            attestation_url=attestation_url,
        )


@pytest.mark.parametrize(
    "url",
    (
        "http://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/x.json",
        "https://evil.example/needle-ci-artifacts/attestations/brand-kit-ci/v1/x.json",
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/x.json?raw=1",
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/watcher/other.json",
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/watcher%2Fuid/attestations.json",
    ),
)
def test_durable_ci_attestation_url_is_scoped_to_the_public_garage_prefix(url):
    with pytest.raises(release_publish.ReleaseError, match="CI attestation URL"):
        release_publish.validate_ci_attestation_url(url)


def test_durable_attestation_does_not_override_a_live_failed_workflow(monkeypatch):
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0, **kwargs):
        calls.append(url)
        return argo_workflow(phase="Failed")

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="only Succeeded is accepted"):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci-abc123",
            COMMIT,
            token=ARGO_READ_TOKEN,
            attestation_url=(
                "https://s3.ardenone.com/needle-ci-artifacts/"
                "attestations/brand-kit-ci/v1/watcher-uid/attestations.json"
            ),
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (argo_workflow(phase="Failed"), "only Succeeded is accepted"),
        (argo_workflow(commit="b" * 40), "attests commit"),
        (argo_workflow(output_name=None), "no structured full-commit attestation"),
    ],
)
def test_argo_attestation_fails_closed_for_phase_commit_or_missing_evidence(
    monkeypatch, workflow, message
):
    monkeypatch.setattr(release_publish, "request_json", lambda *args, **kwargs: workflow)

    with pytest.raises(release_publish.ReleaseError, match=message):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci-abc123", COMMIT, token=ARGO_READ_TOKEN
        )


def test_argo_attestation_accepts_the_revision_output_alias(monkeypatch):
    monkeypatch.setattr(
        release_publish,
        "request_json",
        lambda *args, **kwargs: argo_workflow(output_name="revision"),
    )

    assert release_publish.attest_argo_ci_run(
        "brand-kit-ci-abc123", COMMIT, token=ARGO_READ_TOKEN
    )["status"]["phase"] == "Succeeded"


def test_failed_argo_attestation_can_be_retried_for_the_same_run(monkeypatch):
    workflows = iter([argo_workflow(phase="Failed"), argo_workflow()])
    calls = []

    def request(method, url, payload=None, token=None, timeout=20.0, **kwargs):
        calls.append((method, url, payload))
        return next(workflows)

    monkeypatch.setattr(release_publish, "request_json", request)

    with pytest.raises(release_publish.ReleaseError, match="only Succeeded is accepted"):
        release_publish.attest_argo_ci_run(
            "brand-kit-ci-abc123", COMMIT, token=ARGO_READ_TOKEN
        )

    assert release_publish.attest_argo_ci_run(
        "brand-kit-ci-abc123", COMMIT, token=ARGO_READ_TOKEN
    )["status"]["phase"] == "Succeeded"
    assert [method for method, _, _ in calls] == ["GET", "GET"]
    assert calls[0][1] == calls[1][1]


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
        release_publish.attest_argo_ci_run(
            "brand-kit-ci-abc123", COMMIT, token=ARGO_READ_TOKEN
        )


def test_verify_only_reads_the_published_release_without_writing_or_pushing(monkeypatch, capsys):
    events = []
    api_calls = []

    monkeypatch.setenv("FORGEJO_TOKEN", "secret")
    monkeypatch.setenv("ARGO_TOKEN", ARGO_READ_TOKEN)
    monkeypatch.setattr(
        release_publish,
        "verify_ready",
        lambda *args, **kwargs: events.append("verify-ready") or COMMIT,
    )
    monkeypatch.setattr(
        release_publish,
        "release_notes_from_changelog",
        lambda *args, **kwargs: "release notes",
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
            "release notes",
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


def test_main_does_not_allow_notes_to_bypass_the_changelog_gate(monkeypatch, capsys):
    monkeypatch.setenv("FORGEJO_TOKEN", "forgejo-test-token")
    monkeypatch.setenv("ARGO_TOKEN", ARGO_READ_TOKEN)
    monkeypatch.setattr(
        release_publish,
        "release_notes_from_changelog",
        lambda *args, **kwargs: "changelog notes",
    )
    monkeypatch.setattr(
        release_publish,
        "verify_ready",
        lambda *args, **kwargs: pytest.fail("tag checks must not run after a notes mismatch"),
    )

    result = release_publish.main(
        [
            "--tag",
            "v1.1.0",
            "--ci-run",
            "brand-kit-ci-abc123",
            "--notes",
            "unreviewed override",
        ]
    )

    assert result == 1
    assert "must exactly match" in capsys.readouterr().err


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
        reference = arguments[3]
        return subprocess.CompletedProcess(
            ["git", *arguments],
            0,
            stdout=f"{COMMIT} {reference}\n",
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
    assert "--ci-attestation-url" in document
    assert "attestations/brand-kit-ci/v1" in document
    assert "exact HTTPS `attestations.json` object" in document
    assert "indefinite hold" in document
    assert "returns `404`" in document
    assert "ARGO_TOKEN" in document
    assert "--verify-only" in document
    assert "READY" in document
    assert "GitHub Releases API" in document
    assert "sole release authority" in document
    assert "tools/consumer_sync.py" in document
    assert "Mirror timeout or disagreement" in document
    assert "Failed, missing, or unverifiable Argo attestation" in document
    assert "Partial Forgejo publication" in document
    assert "Post-write verification failure" in document
    assert "Consumer handoff or `consumer_sync.py` failure" in document
    assert "Never retag an" in document
    assert "existing name" in document
    assert "force-push" in document
    assert "new annotated tag" in document
    assert "release-token-provisioning.md" in document
    assert "release-evidence/v1/<tag>.json" in document
    assert "tools/release_evidence.py" in document
    assert "credential-shaped fields and values" in document


def test_release_token_documentation_defines_secure_provisioning_contract():
    document = Path(
        "docs/notes/release-token-provisioning.md"
    ).read_text(encoding="utf-8")

    for variable in ("FORGEJO_TOKEN", "ARGO_TOKEN", "ARGO_SUBMIT_TOKEN"):
        assert variable in document
    assert "secret/rs-manager/brand-kit/release/forgejo" in document
    assert "secret/rs-manager/brand-kit/release/argo-read" in document
    assert "secret/rs-manager/brand-kit/release/argo-submit" in document
    assert "minimum scope" in document
    assert "Rotation order" in document
    assert "401`/`403" in document
    assert "argv" in document
    assert "ARGO_TOKEN` is never promoted to `ARGO_SUBMIT_TOKEN" in document
    assert "export FORGEJO_TOKEN='...'" not in document


def test_consumer_recovery_documentation_keeps_the_exact_tag_and_requires_reverify():
    document = Path("docs/notes/post-tag-consumer-update.md").read_text(
        encoding="utf-8"
    )

    assert "## Recovery after a consumer failure" in document
    assert "Keep the release tag fixed" in document
    assert "--verify-only" in document
    assert "consumer_sync.py --apply" in document
    assert "transactional" in document
    assert "staging directory is discarded" in document
    assert "replacement failure is rolled" in document
    assert "idempotent" in document
    assert "force-push" in document
    assert "all-PASS, exit-0 verification" in document


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
