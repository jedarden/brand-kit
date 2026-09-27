from pathlib import Path

import pytest

from tools import consumer_drift_submit, release_publish


COMMIT = "a" * 40


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


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (None, "no published release record"),
        (published(draft=True), "not published"),
        (published(prerelease=True), "stable published release"),
        (published(target_commitish="main"), "no verifiable target commit"),
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
            request=lambda *args, **kwargs: submits.append(args),
        )

    assert submits == []


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
