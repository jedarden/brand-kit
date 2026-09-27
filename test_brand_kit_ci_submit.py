from pathlib import Path

import pytest

from tools import brand_kit_ci_submit, release_publish


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
ARGO_SUBMIT_TOKEN = "argo-submit-test-token"


def submitted_workflow(commit=COMMIT, **overrides):
    workflow = {
        "metadata": {
            "name": "brand-kit-ci-abc123",
            "labels": {
                "workflows.argoproj.io/workflow-template": "brand-kit-ci",
            },
        },
        "spec": {
            "arguments": {
                "parameters": [
                    {"name": "repo", "value": "jedarden/brand-kit"},
                    {"name": "branch", "value": "main"},
                    {"name": "revision", "value": commit},
                ]
            }
        },
    }
    workflow.update(overrides)
    return workflow


def test_workflow_payload_binds_the_exact_revision():
    payload = brand_kit_ci_submit.workflow_payload(COMMIT)

    assert payload == {
        "namespace": "argo-workflows",
        "resourceKind": "WorkflowTemplate",
        "resourceName": "brand-kit-ci",
        "submitOptions": {
            "parameters": [
                "repo=jedarden/brand-kit",
                "branch=main",
                f"revision={COMMIT}",
            ]
        },
    }


@pytest.mark.parametrize("value", ("HEAD", "a" * 39, "a" * 65, " a" + "a" * 39))
def test_revision_must_be_a_full_sha(value):
    with pytest.raises(release_publish.ReleaseError, match="full commit SHA"):
        brand_kit_ci_submit.validate_commit(value)


def test_submit_uses_argo_workflowtemplate_endpoint_and_preserves_revision():
    calls = []

    def request(method, url, payload=None, token=None, **kwargs):
        calls.append((method, url, payload, token, kwargs))
        return submitted_workflow()

    result = brand_kit_ci_submit.submit_brand_kit_ci(
        COMMIT,
        argo_api_url="https://argo.example",
        argo_token=ARGO_SUBMIT_TOKEN,
        request=request,
    )

    assert result["metadata"]["name"] == "brand-kit-ci-abc123"
    assert calls == [
        (
            "POST",
            "https://argo.example/api/v1/workflows/argo-workflows/submit",
            brand_kit_ci_submit.workflow_payload(COMMIT),
            ARGO_SUBMIT_TOKEN,
            {"authorization_scheme": "Bearer", "service": "Argo API"},
        )
    ]


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (submitted_workflow(OTHER_COMMIT), "carries revision"),
        (
            submitted_workflow(
                **{
                    "spec": {
                        "arguments": {
                            "parameters": [
                                {"name": "branch", "value": "main"},
                            ]
                        }
                    }
                }
            ),
            "no structured full revision",
        ),
    ],
)
def test_mismatched_or_stale_submission_never_passes(workflow, message):
    with pytest.raises(release_publish.ReleaseError, match=message):
        brand_kit_ci_submit.validate_submission(workflow, COMMIT)


def test_argo_failure_is_reported_without_exposing_the_submit_token():
    def request(*args, **kwargs):
        raise release_publish.HttpFailure(
            403,
            f"permission denied: {ARGO_SUBMIT_TOKEN}",
            service="Argo API",
        )

    with pytest.raises(release_publish.ReleaseError, match="cannot submit brand-kit-ci") as error:
        brand_kit_ci_submit.submit_brand_kit_ci(
            COMMIT,
            argo_token=ARGO_SUBMIT_TOKEN,
            request=request,
        )
    assert ARGO_SUBMIT_TOKEN not in str(error.value)


def test_trigger_documentation_describes_main_and_release_commit_flow():
    readme = Path("README.md").read_text(encoding="utf-8")
    publication = Path("docs/notes/release-publication.md").read_text(encoding="utf-8")

    assert "tools/brand_kit_ci_submit.py" in readme
    assert "--commit \"$COMMIT\"" in readme
    assert "revision" in readme
    assert "tools/brand_kit_ci_submit.py" in publication
    assert "git rev-parse --verify HEAD^{commit}" in publication
    assert "ARGO_SUBMIT_TOKEN" in publication
