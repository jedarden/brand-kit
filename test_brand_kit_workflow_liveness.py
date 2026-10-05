import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

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


def test_liveness_is_fresh_when_all_targets_have_recent_successes():
    def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["authorization_scheme"] == "Bearer"
        if "brand-kit-ci-failure-watch" in url:
            return {"items": [workflow("failure-watch", "Succeeded", "2026-09-27T12:30:00Z")]}
        if "brand-kit-consumer-drift" in url:
            return {"items": [workflow("consumer-drift", "Succeeded", "2026-09-26T12:00:00Z")]}
        if "brand-kit-mirror-health" in url:
            return {"items": [workflow("mirror-health", "Succeeded", "2026-09-27T12:00:00Z")]}
        if "brand-kit-platform-requirements" in url:
            return {"items": [workflow("platform-requirements", "Succeeded", "2026-09-26T12:00:00Z")]}
        assert "brand-kit-release-token-probe" in url
        return {"items": [workflow("release-token-probe", "Succeeded", "2026-09-26T12:00:00Z")]}

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, api_url="https://argo.example", request=request
    )

    assert report["status"] == "fresh"
    assert [check["status"] for check in report["checks"]] == [
        "fresh",
        "fresh",
        "fresh",
        "fresh",
        "fresh",
    ]
    assert [check["max_age_minutes"] for check in report["checks"]] == [
        60,
        48 * 60,
        12 * 60,
        48 * 60,
        48 * 60,
    ]


def test_liveness_is_stale_when_a_target_has_no_recent_success():
    def request(method, url, **kwargs):
        if "brand-kit-ci-failure-watch" in url:
            return {"items": []}
        if "brand-kit-consumer-drift" in url:
            return {
                "items": [
                    workflow("consumer-drift-old", "Succeeded", "2026-09-25T12:00:00Z")
                ]
            }
        return {"items": []}

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    assert report["status"] == "stale"
    assert [check["status"] for check in report["checks"]] == [
        "stale",
        "stale",
        "stale",
        "stale",
        "stale",
    ]
    assert report["checks"][0]["reason"] == "no successful run was returned by Argo"


@pytest.mark.parametrize(
    "platform_items",
    (
        pytest.param([], id="missing-run"),
        pytest.param(
            [workflow("platform-requirements-failed", "Failed", "2026-09-27T12:59:00Z")],
            id="stopped-run",
        ),
    ),
)
def test_missing_or_stopped_platform_requirements_run_fails_liveness(
    platform_items,
):
    responses = {
        "brand-kit-ci-failure-watch": {
            "items": [workflow("failure-watch", "Succeeded", "2026-09-27T12:30:00Z")]
        },
        "brand-kit-consumer-drift": {
            "items": [workflow("consumer-drift", "Succeeded", "2026-09-27T12:00:00Z")]
        },
        "brand-kit-mirror-health": {
            "items": [workflow("mirror-health", "Succeeded", "2026-09-27T12:00:00Z")]
        },
        "brand-kit-platform-requirements": {"items": platform_items},
        "brand-kit-release-token-probe": {
            "items": [workflow("release-token-probe", "Succeeded", "2026-09-27T12:00:00Z")]
        },
    }

    def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["authorization_scheme"] == "Bearer"
        for workflow_template, response in responses.items():
            if workflow_template in url:
                return response
        raise AssertionError(f"unexpected Argo URL: {url}")

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    checks = {check["workflow_template"]: check for check in report["checks"]}
    platform_check = checks["brand-kit-platform-requirements"]
    assert report["status"] == "stale"
    assert platform_check["status"] == "stale"
    assert platform_check["last_success_at"] is None
    assert platform_check["reason"] == "no successful run was returned by Argo"
    assert all(
        check["status"] == "fresh"
        for name, check in checks.items()
        if name != "brand-kit-platform-requirements"
    )


def test_liveness_distinguishes_missing_and_late_runs_and_uses_latest_success():
    responses = {
        "brand-kit-ci-failure-watch": {"items": []},
        "brand-kit-consumer-drift": {
            "items": [
                workflow("consumer-drift-failed", "Failed", "2026-09-27T12:59:00Z"),
                workflow("consumer-drift-late", "Succeeded", "2026-09-25T12:00:00Z"),
            ]
        },
        "brand-kit-mirror-health": {
            "items": [
                workflow("mirror-health-old", "Succeeded", "2026-09-27T00:00:00Z"),
                workflow("mirror-health-latest", "Succeeded", "2026-09-27T12:30:00Z"),
            ]
        },
        "brand-kit-platform-requirements": {
            "items": [workflow("platform-requirements", "Succeeded", "2026-09-27T12:00:00Z")]
        },
        "brand-kit-release-token-probe": {
            "items": [workflow("release-token-probe", "Succeeded", "2026-09-27T12:00:00Z")]
        },
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        for template_name, response in responses.items():
            if template_name in url:
                return response
        raise AssertionError(f"unexpected Argo URL: {url}")

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, api_url="https://argo.example", request=request
    )

    checks = {check["workflow_template"]: check for check in report["checks"]}
    assert report["status"] == "stale"
    assert checks["brand-kit-ci-failure-watch"]["status"] == "stale"
    assert checks["brand-kit-ci-failure-watch"]["last_success_at"] is None
    assert checks["brand-kit-ci-failure-watch"]["reason"] == (
        "no successful run was returned by Argo"
    )
    assert checks["brand-kit-consumer-drift"]["status"] == "stale"
    assert checks["brand-kit-consumer-drift"]["last_success_at"] == (
        "2026-09-25T12:00:00Z"
    )
    assert checks["brand-kit-consumer-drift"]["age_minutes"] == 2940.0
    assert checks["brand-kit-mirror-health"]["status"] == "fresh"
    assert checks["brand-kit-mirror-health"]["last_success_at"] == (
        "2026-09-27T12:30:00Z"
    )
    assert checks["brand-kit-mirror-health"]["age_minutes"] == 30.0
    assert len(calls) == len(brand_kit_workflow_liveness.TARGETS)


def test_liveness_is_indeterminate_when_argo_cannot_prove_one_target():
    token = "argo-liveness-contract-secret"
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if "brand-kit-ci-failure-watch" in url:
            raise release_publish.ReleaseError("cannot reach Argo API")
        if "brand-kit-consumer-drift" in url:
            return {"items": [workflow("consumer-drift", "Succeeded", "2026-09-27T12:00:00Z")]}
        return {"items": [workflow("mirror-health", "Succeeded", "2026-09-27T12:00:00Z")]}

    report = brand_kit_workflow_liveness.run_liveness(
        token, now=NOW, request=request
    )

    assert report["status"] == "indeterminate"
    assert report["checks"][0]["status"] == "indeterminate"
    assert report["checks"][1]["status"] == "fresh"
    assert report["checks"][2]["status"] == "fresh"
    assert report["checks"][3]["status"] == "fresh"
    assert report["checks"][4]["status"] == "fresh"
    assert len(calls) == len(brand_kit_workflow_liveness.TARGETS)
    assert "cannot reach Argo API" in report["checks"][0]["reason"]
    assert token not in json.dumps(report)


def test_liveness_treats_a_malformed_api_response_as_indeterminate():
    def request(method, url, **kwargs):
        if "brand-kit-consumer-drift" in url:
            return {"error": "temporarily unavailable"}
        return {
            "items": [
                workflow("healthy", "Succeeded", "2026-09-27T12:00:00Z")
            ]
        }

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    assert report["status"] == "indeterminate"
    assert report["checks"][0]["status"] == "fresh"
    assert report["checks"][1]["status"] == "indeterminate"
    assert report["checks"][1]["reason"] == (
        "Argo workflow-list response for brand-kit-consumer-drift has no items list"
    )
    assert report["checks"][2]["status"] == "fresh"


def test_success_without_a_timestamp_is_indeterminate():
    def request(method, url, **kwargs):
        return {"items": [{"status": {"phase": "Succeeded"}, "metadata": {}}]}

    report = brand_kit_workflow_liveness.run_liveness(
        "argo-secret", now=NOW, request=request
    )

    assert report["status"] == "indeterminate"
    assert all(check["status"] == "indeterminate" for check in report["checks"])


def _load_manifest(name):
    return yaml.safe_load(
        Path("automation", name).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("workflow_name", "schedule"),
    (
        ("brand-kit-ci-failure-watch", "*/15 * * * *"),
        ("brand-kit-consumer-drift", "17 6 * * *"),
        ("brand-kit-mirror-health", "17 */6 * * *"),
        ("brand-kit-platform-requirements", "27 6 * * *"),
        ("brand-kit-release-token-probe", "7 6 * * *"),
        ("brand-kit-workflow-liveness", "*/15 * * * *"),
    ),
)
def test_monitored_cronworkflows_keep_the_expected_utc_schedules(
    workflow_name, schedule
):
    manifest = _load_manifest(f"{workflow_name}-cronworkflow.yml")

    assert manifest["kind"] == "CronWorkflow"
    assert manifest["metadata"]["name"] == workflow_name
    assert manifest["spec"]["schedules"] == [schedule]
    assert manifest["spec"]["timezone"] == "UTC"
    assert manifest["spec"]["workflowSpec"]["workflowTemplateRef"]["name"] == (
        workflow_name
    )


def test_liveness_workflow_contract_is_independent_and_retains_evidence():
    manifest = _load_manifest("brand-kit-workflow-liveness-workflowtemplate.yml")
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    check = templates["check"]

    assert manifest["kind"] == "WorkflowTemplate"
    assert manifest["metadata"]["name"] == "brand-kit-workflow-liveness"
    assert "tools/brand_kit_workflow_liveness.py" in check["container"]["args"][0]
    assert check["container"]["env"] == [
        {
            "name": "ARGO_WORKFLOW_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "brand-kit-workflow-readonly",
                    "key": "token",
                    "optional": True,
                }
            },
        }
    ]
    artifact = check["outputs"]["artifacts"][0]
    assert artifact["artifactGC"] == {"strategy": "Never"}
    assert artifact["s3"]["key"] == (
        "failures/brand-kit-workflow-liveness/v1/{{workflow.uid}}/report.json"
    )


def test_liveness_non_success_route_generates_owner_alert_with_durable_report():
    manifest = _load_manifest("brand-kit-workflow-liveness-workflowtemplate.yml")
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    route = templates["route-liveness"]["steps"][0][0]
    notify = templates["notify-owner"]["container"]["args"][0]

    assert route == {
        "name": "notify-owner",
        "template": "notify-owner",
        "when": "{{workflow.status}} != Succeeded",
        "continueOn": {"failed": True, "error": True},
    }
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in notify
    assert "curl --fail --silent --show-error --retry 3 --retry-delay 5" in notify
    assert '"alertname": "BrandKitWorkflowLiveness"' in notify
    assert '"owner": "jedarden"' in notify
    assert '"component": "brand-kit-workflow-liveness"' in notify
    assert '"follow_up": "workflow-liveness"' in notify
    assert '"bucket": "brand-kit"' in notify
    assert '"workflow_status": "{{workflow.status}}"' in notify
    assert '"summary": "brand-kit scheduled workflow liveness needs follow-up"' in notify
    assert "restore the target CronWorkflow or WorkflowTemplate" in notify
    assert "REPORT_URL=\"https://s3.ardenone.com/needle-ci-artifacts/" in notify
    assert '"report_url": "${REPORT_URL}"' in notify
