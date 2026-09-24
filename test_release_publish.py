from pathlib import Path

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


def test_workflow_documentation_keeps_forgejo_authoritative():
    document = Path("docs/notes/release-publication.md").read_text(encoding="utf-8")
    assert "git tag -a" in document
    assert 'git push origin "refs/tags/$VERSION"' in document
    assert "brand-kit-ci" in document
    assert "tools/release_publish.py" in document
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
