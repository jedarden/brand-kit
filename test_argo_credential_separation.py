from pathlib import Path

import yaml

from tools import brand_kit_ci_failure_watch, brand_kit_workflow_liveness, consumer_drift_submit


ROOT = Path(__file__).parent
WORKFLOW_NAMESPACE = "argo-workflows"
WORKFLOW_SERVICE_ACCOUNT = "argo-workflow"
WORKFLOW_SECRET = "brand-kit-workflow-readonly"
RELEASE_SECRET = "brand-kit-release-tokens"


def _manifest(filename):
    return yaml.safe_load((ROOT / "automation" / filename).read_text(encoding="utf-8"))


def _container_env(manifest, template_name):
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    return templates[template_name]["container"]["env"]


def _env_by_name(env):
    return {item["name"]: item for item in env}


def test_watch_workloads_use_only_the_dedicated_readonly_secret():
    manifests = (
        (
            "brand-kit-ci-failure-watch-workflowtemplate.yml",
            "watch",
        ),
        (
            "brand-kit-workflow-liveness-workflowtemplate.yml",
            "check",
        ),
    )

    for filename, template_name in manifests:
        manifest = _manifest(filename)
        assert manifest["metadata"]["namespace"] == WORKFLOW_NAMESPACE
        assert manifest["spec"]["serviceAccountName"] == WORKFLOW_SERVICE_ACCOUNT
        env = _env_by_name(_container_env(manifest, template_name))
        workload_token = env["ARGO_WORKFLOW_TOKEN"]
        assert workload_token["valueFrom"]["secretKeyRef"] == {
            "name": WORKFLOW_SECRET,
            "key": "token",
        } | (
            {"optional": True}
            if filename.endswith("workflow-liveness-workflowtemplate.yml")
            else {}
        )
        assert "ARGO_TOKEN" not in env
        assert "ARGO_SUBMIT_TOKEN" not in env
        assert RELEASE_SECRET not in filename
        serialized = (ROOT / "automation" / filename).read_text(encoding="utf-8")
        assert RELEASE_SECRET not in serialized


def test_consumer_audit_uses_its_external_secret_and_no_operator_argo_token():
    manifest = _manifest("brand-kit-consumer-drift-workflowtemplate.yml")
    assert manifest["metadata"]["namespace"] == WORKFLOW_NAMESPACE
    assert manifest["spec"]["serviceAccountName"] == WORKFLOW_SERVICE_ACCOUNT
    env = _env_by_name(_container_env(manifest, "audit"))
    assert env["FORGEJO_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "brand-kit-consumer-drift",
        "key": "token",
    }
    assert "ARGO_TOKEN" not in env
    assert "ARGO_SUBMIT_TOKEN" not in env
    serialized = (ROOT / "automation" / "brand-kit-consumer-drift-workflowtemplate.yml").read_text(
        encoding="utf-8"
    )
    assert RELEASE_SECRET not in serialized
    assert "https://git.ardenone.com" not in serialized


def test_workload_credentials_are_used_only_for_get_requests():
    watcher_calls = []

    def watcher_request(method, url, **kwargs):
        watcher_calls.append((method, url, kwargs))
        assert method == "GET"
        assert kwargs["token"] == "workload-readonly"
        assert kwargs["authorization_scheme"] == "Bearer"
        return {"items": []}

    report = brand_kit_ci_failure_watch.run_watch(
        "workload-readonly",
        api_url="https://argo.example",
        request=watcher_request,
    )
    assert report["status"] == "pass"
    assert watcher_calls[0][1].startswith(
        "https://argo.example/api/v1/workflows/argo-workflows?"
    )

    liveness_calls = []

    def liveness_request(method, url, **kwargs):
        liveness_calls.append((method, url, kwargs))
        assert method == "GET"
        assert kwargs["token"] == "workload-readonly"
        assert kwargs["authorization_scheme"] == "Bearer"
        return {"items": []}

    report = brand_kit_workflow_liveness.run_liveness(
        "workload-readonly",
        api_url="https://argo.example",
        request=liveness_request,
    )
    assert report["status"] == "stale"
    assert len(liveness_calls) == 3
    assert all(call[0] == "GET" for call in liveness_calls)


def test_workload_token_cannot_be_promoted_to_a_submit_credential(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "forgejo-readonly")
    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", "workload-readonly")
    monkeypatch.delenv("ARGO_SUBMIT_TOKEN", raising=False)
    monkeypatch.setattr(
        consumer_drift_submit,
        "submit_consumer_drift",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a workload token must not reach the submitter")
        ),
    )

    assert consumer_drift_submit.main(["--release-tag", "v1.1.0"]) == 1


def test_documentation_names_every_credential_boundary():
    document = (ROOT / "docs" / "notes" / "argo-credential-separation.md").read_text(
        encoding="utf-8"
    )
    for value in (
        "brand-kit-workflow-readonly",
        "brand-kit-consumer-drift",
        "brand-kit-release-tokens",
        "ARGO_WORKFLOW_TOKEN",
        "ARGO_TOKEN",
        "ARGO_SUBMIT_TOKEN",
        "argo-workflows",
        "argo-workflow",
        "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows",
        "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1",
        "secret/rs-manager/brand-kit/release/argo-read",
        "secret/rs-manager/brand-kit/release/argo-submit",
        "rs-manager/iad-ci/forgejo/repo-readonly",
        "No `POST`, `PUT`, `PATCH`, or `DELETE`",
    ):
        assert value in document
