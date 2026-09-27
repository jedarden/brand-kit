import json
from pathlib import Path

from tools import release_token_probe


def test_probe_checks_each_credential_without_exposing_values():
    calls = []

    def request(
        method,
        url,
        payload=None,
        token=None,
        authorization_scheme="token",
        service="Forgejo API",
    ):
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "token": token,
                "authorization_scheme": authorization_scheme,
                "service": service,
            }
        )
        if url.endswith("/user"):
            return {"login": "release-bot"}
        if "/releases?limit=1" in url:
            return []
        if "labelSelector=" in url:
            return {"items": []}
        return {"metadata": {"name": "dry-run-only"}}

    report = release_token_probe.run_probe(
        forgejo_token="forgejo-secret",
        argo_token="argo-read-secret",
        argo_submit_token="argo-submit-secret",
        forgejo_api_url="https://forgejo.example/api/v1",
        argo_api_url="https://argo.example",
        request=request,
    )

    assert report == {
        "schema": "brand-kit-release-token-probe/v1",
        "status": "pass",
        "checks": [
            {"credential": "FORGEJO_TOKEN", "status": "pass"},
            {"credential": "ARGO_TOKEN", "status": "pass"},
            {"credential": "ARGO_SUBMIT_TOKEN", "status": "pass"},
        ],
    }
    assert [call["method"] for call in calls] == ["GET", "GET", "GET", "POST"]
    assert calls[0]["url"] == "https://forgejo.example/api/v1/user"
    assert calls[1]["url"].endswith("/repos/jedarden/brand-kit/releases?limit=1")
    assert calls[2]["url"].startswith("https://argo.example/api/v1/workflows/argo-workflows?")
    assert calls[2]["authorization_scheme"] == "Bearer"
    assert calls[3]["url"] == "https://argo.example/api/v1/workflows/argo-workflows/submit"
    assert calls[3]["payload"]["resourceName"] == "brand-kit-consumer-drift"
    assert calls[3]["payload"]["submitOptions"]["serverDryRun"] is True
    assert "forgejo-secret" not in json.dumps(report)
    assert "argo-read-secret" not in json.dumps(report)
    assert "argo-submit-secret" not in json.dumps(report)


def test_probe_records_missing_credentials_and_continues():
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "GET" and args[1].endswith("/user"):
            return {"login": "release-bot"}
        if "/releases?limit=1" in args[1]:
            return []
        if "labelSelector=" in args[1]:
            return {"items": []}
        return {"metadata": {"name": "dry-run-only"}}

    report = release_token_probe.run_probe(
        forgejo_token="forgejo-secret",
        argo_token=None,
        argo_submit_token="argo-submit-secret",
        request=request,
    )

    assert report["status"] == "fail"
    assert report["checks"][1] == {
        "credential": "ARGO_TOKEN",
        "status": "fail",
        "error": "ARGO_TOKEN is required; provide it through the environment",
    }
    assert len(calls) == 3


def test_report_is_machine_readable(tmp_path):
    path = tmp_path / "report.json"
    report = {"schema": "test", "status": "pass", "checks": []}

    release_token_probe.write_report(report, path)

    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_scheduled_probe_workflows_are_safe_and_route_owner_follow_up():
    workflow = Path(
        "automation/brand-kit-release-token-probe-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    cron = Path(
        "automation/brand-kit-release-token-probe-cronworkflow.yml"
    ).read_text(encoding="utf-8")

    assert "name: brand-kit-release-token-probe" in workflow
    assert "onExit: route-token-probe" in workflow
    assert "tools/release_token_probe.py" in workflow
    assert "--report /tmp/release-token-probe-report.json" in workflow
    for variable in ("FORGEJO_TOKEN", "ARGO_TOKEN", "ARGO_SUBMIT_TOKEN"):
        assert f"- name: {variable}" in workflow
        assert f"key: {variable}" in workflow
    assert "brand-kit-release-tokens" in workflow
    assert "artifactGC:\n                strategy: Never" in workflow
    assert "failures/brand-kit-release-token-probe/v1/{{workflow.uid}}/report.json" in workflow
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in workflow
    assert '"alertname": "BrandKitReleaseTokenProbe"' in workflow
    assert '"follow_up": "consumer-drift"' in workflow
    assert '"owner": "jedarden"' in workflow
    assert 'when: "{{workflow.status}} != Succeeded"' in workflow

    assert "kind: CronWorkflow" in cron
    assert '    - "7 6 * * *"' in cron
    assert "workflowTemplateRef:" in cron
    assert "name: brand-kit-release-token-probe" in cron


def test_token_documentation_covers_probe_and_rotation_follow_up():
    document = Path(
        "docs/notes/release-token-provisioning.md"
    ).read_text(encoding="utf-8")

    assert "## Scheduled validity probe" in document
    assert "tools/release_token_probe.py" in document
    assert "serverDryRun" in document
    assert "brand-kit-release-token-probe-cronworkflow.yml" in document
    assert "brand-kit-release-tokens" in document
    assert "BrandKitConsumerDrift" in document
    assert "run the probe until all three checks pass" in document
