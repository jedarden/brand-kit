import json
from datetime import datetime, timezone
from pathlib import Path

from tools import brand_kit_workflow_liveness
from tools import release_publish


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)


def workflow(name, phase, finished_at):
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "creationTimestamp": finished_at,
        },
        "status": {
            "phase": phase,
            "startedAt": finished_at,
            "finishedAt": finished_at,
        },
    }


def test_liveness_is_fresh_when_both_targets_have_recent_successes():
    def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["authorization_scheme"] == "Bearer"
        if "brand-kit-ci-failure-watch" in url:
            return {"items": [workflow("failure-watch", "Succeeded", "2026-09-27T12:30:00Z")]}
        assert "brand-kit-consumer-drift" in url
        return {"items": [workflow("consumer-drift", "Succeeded", "2026-09-26T12:00:00Z")]}

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, api_url="https://argo.example", request=request
    )

    assert report["status"] == "fresh"
    assert [check["status"] for check in report["checks"]] == ["fresh", "fresh"]
    assert report["checks"][0]["max_age_minutes"] == 60
    assert report["checks"][1]["max_age_minutes"] == 2880


def test_liveness_is_stale_when_a_target_has_no_recent_success():
    def request(method, url, **kwargs):
        if "brand-kit-ci-failure-watch" in url:
            return {"items": []}
        return {
            "items": [
                workflow("consumer-drift-old", "Succeeded", "2026-09-25T12:00:00Z")
            ]
        }

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    assert report["status"] == "stale"
    assert [check["status"] for check in report["checks"]] == ["stale", "stale"]
    assert report["checks"][0]["reason"] == "no successful run was returned by Argo"


def test_liveness_is_indeterminate_when_argo_cannot_prove_one_target():
    token = "argo-liveness-contract-secret"

    def request(method, url, **kwargs):
        if "brand-kit-ci-failure-watch" in url:
            raise release_publish.ReleaseError("cannot reach Argo API")
        return {"items": [workflow("consumer-drift", "Succeeded", "2026-09-27T12:00:00Z")]}

    report = brand_kit_workflow_liveness.run_liveness(
        token, now=NOW, request=request
    )

    assert report["status"] == "indeterminate"
    assert report["checks"][0]["status"] == "indeterminate"
    assert report["checks"][1]["status"] == "fresh"
    assert token not in json.dumps(report)


def test_success_without_a_timestamp_is_indeterminate():
    def request(method, url, **kwargs):
        return {"items": [{"status": {"phase": "Succeeded"}, "metadata": {}}]}

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    assert report["status"] == "indeterminate"
    assert all(check["status"] == "indeterminate" for check in report["checks"])


def test_liveness_workflow_contract_is_independent_and_retains_evidence():
    template = Path(
        "automation/brand-kit-workflow-liveness-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    cron = Path("automation/brand-kit-workflow-liveness-cronworkflow.yml").read_text(
        encoding="utf-8"
    )

    assert "kind: WorkflowTemplate" in template
    assert "name: brand-kit-workflow-liveness" in template
    assert "tools/brand_kit_workflow_liveness.py" in template
    assert "key: ARGO_TOKEN" in template
    assert "optional: true" in template
    assert "artifactGC:\n                strategy: Never" in template
    assert "failures/brand-kit-workflow-liveness/v1/{{workflow.uid}}/report.json" in template
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in template
    assert '"alertname": "BrandKitWorkflowLiveness"' in template
    assert '"follow_up": "workflow-liveness"' in template
    assert 'when: "{{workflow.status}} != Succeeded"' in template

    assert "kind: CronWorkflow" in cron
    assert '    - "*/15 * * * *"' in cron
    assert "workflowTemplateRef:" in cron
    assert "name: brand-kit-workflow-liveness" in cron
