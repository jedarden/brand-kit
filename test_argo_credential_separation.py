import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tools import (
    brand_kit_ci_failure_watch,
    brand_kit_workflow_liveness,
    consumer_drift,
    consumer_drift_submit,
)


ROOT = Path(__file__).parent
WORKFLOW_NAMESPACE = "argo-workflows"
WORKFLOW_SECRET = "brand-kit-workflow-readonly"
RELEASE_SECRET = "brand-kit-release-tokens"

WORKLOAD_SERVICE_ACCOUNTS = {
    "brand-kit-ci-failure-watch-workflowtemplate.yml": "brand-kit-ci-failure-watch",
    "brand-kit-workflow-liveness-workflowtemplate.yml": "brand-kit-workflow-liveness",
    "brand-kit-consumer-drift-workflowtemplate.yml": "brand-kit-consumer-drift",
}


def _manifest(filename):
    return yaml.safe_load((ROOT / "automation" / filename).read_text(encoding="utf-8"))


def _container_env(manifest, template_name):
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    return templates[template_name]["container"]["env"]


def _env_by_name(env):
    return {item["name"]: item for item in env}


def _secret_ref_names(value):
    names = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"secretKeyRef", "accessKeySecret", "secretKeySecret"}:
                if isinstance(child, dict) and "name" in child:
                    names.add(child["name"])
            names.update(_secret_ref_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_secret_ref_names(child))
    return names


def test_each_workload_references_only_its_namespaced_secret_allow_list():
    expected = {
        "brand-kit-ci-failure-watch-workflowtemplate.yml": {
            "brand-kit-workflow-readonly",
            "needle-ci-artifact-reader",
            "needle-ci-artifact-publisher",
        },
        "brand-kit-workflow-liveness-workflowtemplate.yml": {
            "brand-kit-workflow-readonly",
            "needle-ci-artifact-publisher",
        },
        "brand-kit-consumer-drift-workflowtemplate.yml": {
            "brand-kit-consumer-drift",
            "needle-ci-artifact-reader",
            "needle-ci-artifact-publisher",
        },
    }

    for filename, allowed in expected.items():
        assert _secret_ref_names(_manifest(filename)) == allowed


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
        assert manifest["spec"]["serviceAccountName"] == WORKLOAD_SERVICE_ACCOUNTS[filename]
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
    assert manifest["spec"]["serviceAccountName"] == WORKLOAD_SERVICE_ACCOUNTS[
        "brand-kit-consumer-drift-workflowtemplate.yml"
    ]
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


def _argo_workflow_list_url(workflow_template):
    return (
        "https://argo.example/api/v1/workflows/argo-workflows?"
        "labelSelector=workflows.argoproj.io%2Fworkflow-template%3D"
        f"{workflow_template}&limit=100"
    )


def _assert_failure_watcher_argo_request(method, url, kwargs, *, api_url, token):
    """Enforce the documented, read-only Argo API boundary for the watcher."""
    assert method == "GET"

    expected_origin = urlsplit(api_url)
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc) == (
        expected_origin.scheme,
        expected_origin.netloc,
    )
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.fragment

    list_path = "/api/v1/workflows/argo-workflows"
    if parsed.path == list_path:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        assert set(query) in ({"labelSelector", "limit"}, {"labelSelector", "limit", "continue"})
        assert query["labelSelector"] == [
            "workflows.argoproj.io/workflow-template=brand-kit-ci"
        ]
        assert query["limit"] == ["100"]
        if "continue" in query:
            assert len(query["continue"]) == 1
            assert query["continue"][0]
    else:
        detail_prefix = f"{list_path}/"
        detail_name = parsed.path.removeprefix(detail_prefix)
        assert parsed.path.startswith(detail_prefix)
        assert detail_name
        assert "/" not in detail_name
        assert detail_name[0].islower() or detail_name[0].isdigit()
        assert all(char.islower() or char.isdigit() or char == "-" for char in detail_name)
        assert not parsed.query

    assert kwargs == {
        "token": token,
        "authorization_scheme": "Bearer",
        "service": "Argo API",
    }


def _assert_failure_watcher_argo_contract(calls, *, api_url, token):
    assert calls, "the failure watcher should query Argo"
    for method, url, kwargs in calls:
        _assert_failure_watcher_argo_request(
            method,
            url,
            kwargs,
            api_url=api_url,
            token=token,
        )


def test_failure_watcher_uses_only_its_documented_argo_get_endpoint():
    calls = []
    responses = iter(
        [
            {"items": [], "metadata": {"continue": "next-page"}},
            {"items": [], "metadata": {}},
        ]
    )

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        _assert_failure_watcher_argo_request(
            method,
            url,
            kwargs,
            api_url="https://argo.example",
            token="workload-readonly",
        )
        return next(responses)

    report = brand_kit_ci_failure_watch.run_watch(
        "workload-readonly",
        api_url="https://argo.example",
        request=request,
    )

    assert report["status"] == "pass"
    _assert_failure_watcher_argo_contract(
        calls,
        api_url="https://argo.example",
        token="workload-readonly",
    )
    assert [url for _, url, _ in calls] == [
        _argo_workflow_list_url("brand-kit-ci"),
        _argo_workflow_list_url("brand-kit-ci") + "&continue=next-page",
    ]


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", _argo_workflow_list_url("brand-kit-ci")),
        ("PATCH", _argo_workflow_list_url("brand-kit-ci")),
        ("PUT", _argo_workflow_list_url("brand-kit-ci")),
        ("DELETE", _argo_workflow_list_url("brand-kit-ci")),
        ("GET", "https://argo.example/api/v1/workflows/other-namespace"),
    ],
)
def test_failure_watcher_argo_contract_rejects_unexpected_method_or_endpoint(method, url):
    with pytest.raises(AssertionError):
        _assert_failure_watcher_argo_request(
            method,
            url,
            {
                "token": "workload-readonly",
                "authorization_scheme": "Bearer",
                "service": "Argo API",
            },
            api_url="https://argo.example",
            token="workload-readonly",
        )


def test_liveness_uses_only_documented_argo_get_endpoints():
    calls = []

    def liveness_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        _assert_liveness_argo_request(
            method,
            url,
            kwargs,
            api_url="https://argo.example",
            token="workload-readonly",
        )
        return {"items": []}

    report = brand_kit_workflow_liveness.run_liveness(
        "workload-readonly",
        api_url="https://argo.example",
        request=liveness_request,
    )

    assert report["status"] == "stale"
    _assert_liveness_argo_contract(
        calls,
        api_url="https://argo.example",
        token="workload-readonly",
    )
    assert calls == [
        (
            "GET",
            _argo_workflow_list_url(target["workflow_template"]),
            {
                "token": "workload-readonly",
                "authorization_scheme": "Bearer",
                "service": "Argo API",
            },
        )
        for target in brand_kit_workflow_liveness.TARGETS
    ]


def _assert_liveness_argo_request(method, url, kwargs, *, api_url, token):
    """Enforce the documented, read-only Argo API boundary for liveness."""
    assert method == "GET"

    expected_origin = urlsplit(api_url)
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc) == (
        expected_origin.scheme,
        expected_origin.netloc,
    )
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.fragment
    assert parsed.path == "/api/v1/workflows/argo-workflows"

    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    assert set(query) == {"labelSelector", "limit"}
    assert len(query["labelSelector"]) == 1
    template = query["labelSelector"][0].removeprefix(
        "workflows.argoproj.io/workflow-template="
    )
    assert template in {
        target["workflow_template"] for target in brand_kit_workflow_liveness.TARGETS
    }
    assert query["labelSelector"] == [
        f"workflows.argoproj.io/workflow-template={template}"
    ]
    assert query["limit"] == ["100"]
    assert kwargs == {
        "token": token,
        "authorization_scheme": "Bearer",
        "service": "Argo API",
    }


def _assert_liveness_argo_contract(calls, *, api_url, token):
    assert calls, "the liveness workload should query Argo"
    assert len(calls) == len(brand_kit_workflow_liveness.TARGETS)
    for method, url, kwargs in calls:
        _assert_liveness_argo_request(
            method,
            url,
            kwargs,
            api_url=api_url,
            token=token,
        )


@pytest.mark.parametrize(
    ("method", "url", "token"),
    [
        (
            "POST",
            _argo_workflow_list_url("brand-kit-workflow-liveness"),
            "workload-readonly",
        ),
        (
            "PATCH",
            _argo_workflow_list_url("brand-kit-workflow-liveness"),
            "workload-readonly",
        ),
        (
            "PUT",
            _argo_workflow_list_url("brand-kit-workflow-liveness"),
            "workload-readonly",
        ),
        (
            "DELETE",
            _argo_workflow_list_url("brand-kit-workflow-liveness"),
            "workload-readonly",
        ),
        (
            "GET",
            "https://argo.example/api/v1/workflows/other-namespace?labelSelector=x&limit=100",
            "workload-readonly",
        ),
        (
            "GET",
            _argo_workflow_list_url("brand-kit-workflow-liveness") + "&unexpected=true",
            "workload-readonly",
        ),
        (
            "GET",
            _argo_workflow_list_url("brand-kit-workflow-liveness"),
            "wrong-credential",
        ),
    ],
)
def test_liveness_argo_contract_rejects_unexpected_method_endpoint_or_credential(
    method, url, token
):
    with pytest.raises(AssertionError):
        _assert_liveness_argo_request(
            method,
            url,
            {
                "token": token,
                "authorization_scheme": "Bearer",
                "service": "Argo API",
            },
            api_url="https://argo.example",
            token="workload-readonly",
        )


def test_consumer_audit_uses_only_the_documented_forgejo_release_get(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "tag_name": "v1.1.0",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-09-27T00:00:00Z",
                }
            ).encode()

    def urlopen(request, timeout):
        requests.append((request.get_method(), request.full_url, request, timeout))
        return Response()

    monkeypatch.setattr(consumer_drift.release_publish.urllib.request, "urlopen", urlopen)
    config = {
        "release": {
            "api_url": "https://git.ardenone.com/api/v1",
            "repository": "jedarden/brand-kit",
        }
    }

    assert consumer_drift.fetch_release(
        "v1.1.0",
        config,
        token="forgejo-readonly",
    )["tag_name"] == "v1.1.0"
    assert len(requests) == 1
    method, url, request, _ = requests[0]
    assert method == "GET"
    assert url == (
        "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/"
        "releases/tags/v1.1.0"
    )
    assert ("Authorization", "token forgejo-readonly") in request.header_items()


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
        "brand-kit-ci-failure-watch",
        "brand-kit-workflow-liveness",
        "brand-kit-consumer-drift",
        "argo-workflow",
        "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows",
        "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1",
        "secret/rs-manager/brand-kit/release/argo-read",
        "secret/rs-manager/brand-kit/release/argo-submit",
        "rs-manager/iad-ci/forgejo/repo-readonly",
        "No `POST`, `PUT`, `PATCH`, or `DELETE`",
    ):
        assert value in document
