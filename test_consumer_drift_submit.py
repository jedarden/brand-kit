from pathlib import Path

import pytest

from tools import consumer_drift_submit, release_publish


COMMIT = "a" * 40
FORGEJO_TOKEN = "forgejo-test-token"
ARGO_SUBMIT_TOKEN = "argo-submit-test-token"


def published(**overrides):
    record = {
        "tag_name": "v1.1.0",
        "draft": False,
        "prerelease": False,
        "target_commitish": COMMIT,
        "published_at": "2026-09-27T00:00:00Z",
    }
    record.update(overrides)
    return record


def test_workflow_payload_propagates_the_exact_release_tag():
    payload = consumer_drift_submit.workflow_payload("v1.1.0")

    assert payload["workflow"]["spec"]["workflowTemplateRef"] == {
        "name": "brand-kit-consumer-drift"
    }
    assert payload["workflow"]["spec"]["arguments"] == {
        "parameters": [{"name": "release-tag", "value": "v1.1.0"}]
    }


def test_submit_waits_for_mirror_before_posting_the_exact_tag(monkeypatch):
    events = []

    def get_release(*args, **kwargs):
        events.append("release")
        return published()

    def wait_for_mirror(tag, commit, **kwargs):
        events.append(("mirror", tag, commit))

    def request(method, url, payload=None, token=None, **kwargs):
        events.append(("submit", method, url, payload, token, kwargs))
        return {"metadata": {"name": "brand-kit-consumer-drift-release-abc"}}

    monkeypatch.setattr(release_publish, "get_release", get_release)
    monkeypatch.setattr(release_publish, "wait_for_mirror", wait_for_mirror)

    result = consumer_drift_submit.submit_consumer_drift(
        "v1.1.0",
        forgejo_token="forgejo-read-only",
        argo_token="argo-submit",
        argo_api_url="https://argo.example",
        request=request,
    )

    assert result["metadata"]["name"] == "brand-kit-consumer-drift-release-abc"
    assert events[:2] == ["release", ("mirror", "v1.1.0", COMMIT)]
    submit = events[2]
    assert submit[0:3] == (
        "submit",
        "POST",
        "https://argo.example/api/v1/workflows/argo-workflows",
    )
    assert submit[4] == "argo-submit"
    assert submit[5] == {
        "authorization_scheme": "Bearer",
        "service": "Argo API",
    }
    assert submit[3]["workflow"]["spec"]["arguments"] == {
        "parameters": [{"name": "release-tag", "value": "v1.1.0"}]
    }


def test_submit_requires_dedicated_argo_submit_credential(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", FORGEJO_TOKEN)
    monkeypatch.setenv("ARGO_TOKEN", "read-only-token-must-not-be-reused")
    monkeypatch.delenv("ARGO_SUBMIT_TOKEN", raising=False)
    monkeypatch.setattr(
        consumer_drift_submit,
        "submit_consumer_drift",
        lambda *args, **kwargs: pytest.fail("submission must stop at credential validation"),
    )

    result = consumer_drift_submit.main(["--release-tag", "v1.1.0"])

    assert result == 1


def test_submit_retries_until_canonical_and_mirror_tags_agree(monkeypatch):
    events = []
    observed_commits = iter([COMMIT, None, COMMIT, COMMIT])

    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )

    def remote_tag_commit(remote, tag, root=release_publish.ROOT):
        events.append((remote, tag))
        return next(observed_commits)

    monkeypatch.setattr(release_publish, "remote_tag_commit", remote_tag_commit)
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    def request(method, url, payload=None, **kwargs):
        events.append(("submit", method, payload))
        return {"metadata": {"name": "brand-kit-consumer-drift-release-abc"}}

    result = consumer_drift_submit.submit_consumer_drift(
        "v1.1.0",
        forgejo_token=FORGEJO_TOKEN,
        argo_token=ARGO_SUBMIT_TOKEN,
        timeout=1,
        interval=0,
        sleep=lambda delay: events.append(("sleep", delay)),
        request=request,
    )

    assert result["metadata"]["name"] == "brand-kit-consumer-drift-release-abc"
    assert events[:5] == [
        ("origin", "v1.1.0"),
        ("github", "v1.1.0"),
        ("sleep", 0),
        ("origin", "v1.1.0"),
        ("github", "v1.1.0"),
    ]
    assert events[5][0:2] == ("submit", "POST")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (None, "no published release record"),
        (published(draft=True), "not published"),
        (published(prerelease=True), "stable published release"),
        (published(target_commitish="main"), "no verifiable target commit"),
        (published(tag_name="v1.0.0"), "names"),
    ],
)
def test_failed_or_unverifiable_release_never_submits(
    monkeypatch, record, message
):
    submits = []

    def get_release(*args, **kwargs):
        if record is None:
            return None
        return release_publish.validate_release_record(record, "v1.1.0")

    monkeypatch.setattr(release_publish, "get_release", get_release)
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: pytest.fail("mirror must not be checked"),
    )

    with pytest.raises(release_publish.ReleaseError, match=message):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_forgejo_api_failure_never_checks_tags_or_submits(monkeypatch):
    submits = []

    def request_json(*args, **kwargs):
        raise release_publish.HttpFailure(503, "temporarily unavailable")

    monkeypatch.setattr(release_publish, "request_json", request_json)
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: pytest.fail("mirror must not be checked"),
    )

    with pytest.raises(release_publish.ReleaseError, match="cannot read Forgejo release"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_canonical_tag_mismatch_never_submits(monkeypatch):
    submits = []
    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )
    monkeypatch.setattr(
        release_publish,
        "remote_tag_commit",
        lambda *args, **kwargs: "b" * 40,
    )
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="origin .* expected"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_mirror_timeout_never_submits(monkeypatch):
    submits = []
    observed = iter([COMMIT, None])
    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )
    monkeypatch.setattr(
        release_publish,
        "remote_tag_commit",
        lambda *args, **kwargs: next(observed),
    )
    monkeypatch.setattr(
        release_publish,
        "remote_branch_commit",
        lambda *args, **kwargs: COMMIT,
    )

    with pytest.raises(release_publish.ReleaseError, match="has not caught up"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            timeout=0,
            interval=0,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_argo_api_failure_is_reported_after_all_read_only_gates(monkeypatch):
    submits = []
    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: None,
    )

    def request(*args, **kwargs):
        submits.append((args, kwargs))
        raise release_publish.HttpFailure(
            502,
            "upstream unavailable",
            service="Argo API",
        )

    with pytest.raises(
        release_publish.ReleaseError,
        match=r"cannot submit consumer-drift workflow for v1\.1\.0.*HTTP 502",
    ):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=request,
        )

    assert len(submits) == 1


def test_malformed_argo_success_response_fails_closed(monkeypatch):
    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(release_publish.ReleaseError, match="no workflow name"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: {"metadata": {}},
        )


def test_mirror_failure_never_submits(monkeypatch):
    submits = []
    monkeypatch.setattr(
        release_publish, "get_release", lambda *args, **kwargs: published()
    )
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            release_publish.ReleaseError("mirror has not caught up")
        ),
    )

    with pytest.raises(release_publish.ReleaseError, match="mirror has not caught up"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_remote_identity_failure_never_submits_a_consumer_workflow(monkeypatch):
    submits = []
    monkeypatch.setattr(
        release_publish,
        "get_release",
        lambda *args, **kwargs: published(),
    )
    monkeypatch.setattr(
        release_publish,
        "validate_remote_roles",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            release_publish.ReleaseError("github remote is not the expected repository identity")
        ),
    )
    monkeypatch.setattr(
        release_publish,
        "wait_for_mirror",
        lambda *args, **kwargs: pytest.fail("mirror propagation must not run after identity failure"),
    )

    with pytest.raises(release_publish.ReleaseError, match="repository identity"):
        consumer_drift_submit.submit_consumer_drift(
            "v1.1.0",
            forgejo_token=FORGEJO_TOKEN,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


def test_workflow_template_is_release_triggerable_and_cron_is_the_fallback():
    template = Path(
        "automation/brand-kit-consumer-drift-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    cron = Path("automation/brand-kit-consumer-drift-cronworkflow.yml").read_text(
        encoding="utf-8"
    )

    assert "kind: WorkflowTemplate" in template
    assert "name: brand-kit-consumer-drift" in template
    assert 'TAG="{{workflow.parameters.release-tag}}"' in template
    assert "git ls-remote --exit-code" in template
    assert "sleep 10" in template
    assert 'git -C /release checkout --detach --quiet "$TAG"' in template
    assert '--release-tag "$TAG"' in template
    assert "--source-root /release" in template
    assert "FORGEJO_TOKEN" in template

    assert "kind: CronWorkflow" in cron
    assert '    - "17 6 * * *"' in cron
    assert "workflowTemplateRef:" in cron
    assert "name: brand-kit-consumer-drift" in cron
    assert 'name: release-tag\n          value: ""' in cron


def test_submission_documentation_names_the_release_handoff():
    publication = Path("docs/notes/release-publication.md").read_text(
        encoding="utf-8"
    )
    consumer = Path("docs/notes/post-tag-consumer-update.md").read_text(
        encoding="utf-8"
    )

    command = "tools/consumer_drift_submit.py"
    assert command in publication
    assert command in consumer
    assert '--release-tag "$VERSION"' in publication
    assert '--release-tag "$VERSION"' in consumer
    assert "Forgejo release" in publication
    assert "published" in publication
    assert "waits for the canonical and read-only mirror tags" in consumer
