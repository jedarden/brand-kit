import json
from datetime import datetime, timezone
from pathlib import Path

from tools import brand_kit_ci_failure_watch


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)


def workflow(name, phase, finished_at, *, commit=None):
    record = {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "creationTimestamp": finished_at,
        },
        "status": {
            "phase": phase,
            "startedAt": finished_at,
            "finishedAt": finished_at,
            "message": "regression gate failed" if phase != "Succeeded" else None,
        },
    }
    if commit is not None:
        record["status"]["outputs"] = {
            "parameters": [{"name": "commit", "value": commit}]
        }
    return record


def test_run_watch_reports_recent_failures_and_ignores_successes_and_old_runs():
    calls = []
    recent_failed = workflow("brand-kit-ci-failed", "Failed", "2026-09-27T12:45:00Z")
    recent_failed["status"]["outputs"] = {
        "parameters": [
            {
                "name": "commit",
                "value": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
            }
        ]
    }

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {
            "items": [
                recent_failed,
                workflow("brand-kit-ci-error", "Error", "2026-09-27T12:30:00Z"),
                workflow("brand-kit-ci-success", "Succeeded", "2026-09-27T12:50:00Z"),
                workflow("brand-kit-ci-old", "Failed", "2026-09-27T10:30:00Z"),
            ]
        }

    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        api_url="https://argo.example",
        now=NOW,
        request=request,
    )

    assert report["status"] == "fail"
    assert [failure["name"] for failure in report["failures"]] == [
        "brand-kit-ci-failed",
        "brand-kit-ci-error",
    ]
    assert report["failures"][0]["commit"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert report["failures"][0]["api_url"].endswith(
        "/workflows/argo-workflows/brand-kit-ci-failed"
    )
    assert calls[0][0] == "GET"
    assert "workflow-template%3Dbrand-kit-ci" in calls[0][1]
    assert calls[0][2]["authorization_scheme"] == "Bearer"
    assert "argo-secret" not in json.dumps(report)


def test_run_watch_passes_when_no_recent_failure_exists():
    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        now=NOW,
        request=lambda *args, **kwargs: {"items": []},
    )

    assert report["status"] == "pass"
    assert report["failures"] == []


def test_failure_without_timestamp_is_retained_for_follow_up():
    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        now=NOW,
        request=lambda *args, **kwargs: {
            "items": [
                {
                    "metadata": {"name": "brand-kit-ci-unknown-age", "uid": "uid-unknown"},
                    "status": {"phase": "Error", "message": "controller error"},
                }
            ]
        },
    )

    assert report["failures"] == [
        {
            "name": "brand-kit-ci-unknown-age",
            "uid": "uid-unknown",
            "phase": "Error",
            "message": "controller error",
            "creation_timestamp": None,
            "started_at": None,
            "finished_at": None,
            "commit": None,
            "api_url": "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/brand-kit-ci-unknown-age",
        }
    ]


def test_workflow_contract_and_documentation_define_owner_routing():
    workflow = Path("automation/brand-kit-ci-failure-watch-workflowtemplate.yml").read_text()
    cron = Path("automation/brand-kit-ci-failure-watch-cronworkflow.yml").read_text()
    readme = Path("README.md").read_text()
    note = Path("docs/notes/asset-toolchain.md").read_text()

    assert "onExit: route-ci-failure-watch" in workflow
    assert "brand-kit-release-tokens" in workflow
    assert "key: ARGO_TOKEN" in workflow
    assert "artifactGC:\n              strategy: Never" in workflow
    assert "failures/brand-kit-ci-failure-watch/v1/{{workflow.uid}}/report.json" in workflow
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in workflow
    assert '"alertname": "BrandKitCIRegressionGate"' in workflow
    assert '"owner": "jedarden"' in workflow
    assert '"follow_up": "regression-gate"' in workflow
    assert '    - "*/15 * * * *"' in cron
    assert "name: brand-kit-ci-failure-watch" in cron
    assert "BrandKitCIRegressionGate" in readme
    assert "brand-kit-ci-failure-watch" in readme
    assert "BrandKitCIRegressionGate" in note
