import json
from pathlib import Path

import pytest

from tools import release_token_probe
from tools import release_publish


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
    assert calls[0]["token"] == "forgejo-secret"
    assert calls[1]["token"] == "forgejo-secret"
    assert calls[0]["authorization_scheme"] == "token"
    assert calls[1]["authorization_scheme"] == "token"
    assert calls[2]["url"].startswith("https://argo.example/api/v1/workflows/argo-workflows?")
    assert "workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-ci" in calls[2]["url"]
    assert calls[2]["token"] == "argo-read-secret"
    assert calls[2]["authorization_scheme"] == "Bearer"
    assert calls[3]["url"] == "https://argo.example/api/v1/workflows/argo-workflows/submit"
    assert calls[3]["token"] == "argo-submit-secret"
    assert calls[3]["authorization_scheme"] == "Bearer"
    assert calls[3]["payload"]["namespace"] == "argo-workflows"
    assert calls[3]["payload"]["resourceKind"] == "WorkflowTemplate"
    assert calls[3]["payload"]["resourceName"] == "brand-kit-consumer-drift"
    assert calls[3]["payload"]["submitOptions"]["serverDryRun"] is True
    assert {call["token"] for call in calls} == {
        "forgejo-secret",
        "argo-read-secret",
        "argo-submit-secret",
    }
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


@pytest.mark.parametrize("missing", ("FORGEJO_TOKEN", "ARGO_TOKEN", "ARGO_SUBMIT_TOKEN"))
def test_probe_requires_each_credential_independently(missing):
    tokens = {
        "FORGEJO_TOKEN": "forgejo-secret",
        "ARGO_TOKEN": "argo-read-secret",
        "ARGO_SUBMIT_TOKEN": "argo-submit-secret",
    }

    def request(*args, **kwargs):
        if args[1].endswith("/user"):
            return {"login": "release-bot"}
        if "/releases?limit=1" in args[1]:
            return []
        if "labelSelector=" in args[1]:
            return {"items": []}
        return {"metadata": {"name": "dry-run-only"}}

    values = {name: value for name, value in tokens.items()}
    values[missing] = None
    report = release_token_probe.run_probe(**_probe_tokens(values), request=request)

    assert report["status"] == "fail"
    assert report["checks"] == [
        (
            {"credential": missing, "status": "fail", "error": f"{missing} is required; provide it through the environment"}
            if credential == missing
            else {"credential": credential, "status": "pass"}
        )
        for credential in tokens
    ]


def _probe_tokens(tokens):
    return {
        "forgejo_token": tokens["FORGEJO_TOKEN"],
        "argo_token": tokens["ARGO_TOKEN"],
        "argo_submit_token": tokens["ARGO_SUBMIT_TOKEN"],
    }


@pytest.mark.parametrize(
    ("credential", "status", "detail"),
    (
        ("FORGEJO_TOKEN", 401, "token expired"),
        ("ARGO_TOKEN", 403, "workflow read scope is insufficient"),
        ("ARGO_SUBMIT_TOKEN", 403, "workflow submit scope is insufficient"),
    ),
)
def test_probe_distinguishes_expired_and_insufficiently_scoped_tokens(
    credential, status, detail
):
    tokens = {
        "FORGEJO_TOKEN": "forgejo-secret",
        "ARGO_TOKEN": "argo-read-secret",
        "ARGO_SUBMIT_TOKEN": "argo-submit-secret",
    }

    def request(method, url, token=None, service="Forgejo API", **kwargs):
        if token == tokens[credential]:
            raise release_publish.HttpFailure(status, f"{detail}: {token}", service=service)
        if url.endswith("/user"):
            return {"login": "release-bot"}
        if "/releases?limit=1" in url:
            return []
        if "labelSelector=" in url:
            return {"items": []}
        return {"metadata": {"name": "dry-run-only"}}

    report = release_token_probe.run_probe(**_probe_tokens(tokens), request=request)
    failed = next(check for check in report["checks"] if check["credential"] == credential)

    assert report["status"] == "fail"
    assert failed["status"] == "fail"
    assert f"HTTP {status}" in failed["error"]
    assert detail in failed["error"]
    serialized = json.dumps(report)
    assert all(token not in serialized for token in tokens.values())


@pytest.mark.parametrize(
    ("bad_endpoint", "bad_response", "credential", "message"),
    (
        ("/user", {"message": "not a user"}, "FORGEJO_TOKEN", "self-lookup"),
        ("/releases?limit=1", {"message": "release list unavailable"}, "FORGEJO_TOKEN", "release-list"),
        ("labelSelector=", {"message": "workflow list unavailable"}, "ARGO_TOKEN", "workflow-list"),
        ("/submit", {"message": "dry-run unavailable"}, "ARGO_SUBMIT_TOKEN", "dry-run"),
    ),
)
def test_probe_rejects_inconclusive_success_responses(
    bad_endpoint, bad_response, credential, message
):
    tokens = {
        "FORGEJO_TOKEN": "forgejo-secret",
        "ARGO_TOKEN": "argo-read-secret",
        "ARGO_SUBMIT_TOKEN": "argo-submit-secret",
    }

    def request(method, url, **kwargs):
        if bad_endpoint in url:
            return bad_response
        if url.endswith("/user"):
            return {"login": "release-bot"}
        if "/releases?limit=1" in url:
            return []
        if "labelSelector=" in url:
            return {"items": []}
        return {"metadata": {"name": "dry-run-only"}}

    report = release_token_probe.run_probe(**_probe_tokens(tokens), request=request)
    failed = next(check for check in report["checks"] if check["credential"] == credential)

    assert report["status"] == "fail"
    assert failed["status"] == "fail"
    assert message in failed["error"]


def test_probe_cli_never_prints_provider_error_token(monkeypatch, capsys):
    tokens = {
        "FORGEJO_TOKEN": "forgejo-secret",
        "ARGO_TOKEN": "argo-read-secret",
        "ARGO_SUBMIT_TOKEN": "argo-submit-secret",
    }

    def request(method, url, token=None, **kwargs):
        raise release_publish.ReleaseError(f"provider rejected expired credential {token}")

    real_run_probe = release_token_probe.run_probe

    def run_with_request(**kwargs):
        return real_run_probe(**kwargs, request=request)

    monkeypatch.setattr(release_token_probe, "run_probe", run_with_request)
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)

    assert release_token_probe.main([]) == 1
    output = capsys.readouterr()
    assert all(token not in output.out + output.err for token in tokens.values())


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
        environment_block = workflow.split(f"- name: {variable}", 1)[1].split(
            "          - name:", 1
        )[0]
        assert "name: brand-kit-release-tokens" in environment_block
        assert "optional: true" in environment_block
    assert "brand-kit-release-tokens" in workflow
    assert '"workflow did not reach the probe"' in workflow
    assert "artifactGC:\n              strategy: Never" in workflow
    assert "failures/brand-kit-release-token-probe/v1/{{workflow.uid}}/report.json" in workflow
    assert "http://alertmanager.monitoring.svc:9093/api/v2/alerts" in workflow
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
    for variable, path in {
        "FORGEJO_TOKEN": "secret/rs-manager/brand-kit/release/forgejo",
        "ARGO_TOKEN": "secret/rs-manager/brand-kit/release/argo-read",
        "ARGO_SUBMIT_TOKEN": "secret/rs-manager/brand-kit/release/argo-submit",
    }.items():
        assert f"| `{variable}` | `{path}` |" in document
