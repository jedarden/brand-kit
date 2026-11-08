import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tools import (
    brand_kit_ci_failure_watch,
    brand_kit_ci_submit,
    brand_kit_workflow_liveness,
    check_mirror_health,
    consumer_drift,
    consumer_drift_submit,
    prune_ci_failure_watch_reports,
)


ROOT = Path(__file__).parent
WORKFLOW_NAMESPACE = "argo-workflows"
# Retired 2026-10-11 (brandkit-cbbcc260): argo-server is SSO-only, so the
# watchers authenticate to the Kubernetes API with their own ServiceAccount
# and no workload may reference an Argo API token Secret again.
RETIRED_WORKFLOW_SECRET = "brand-kit-workflow-readonly"
RELEASE_SECRET = "brand-kit-release-tokens"
KUBERNETES_API_URL = "https://kubernetes.example"

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
            "needle-ci-artifact-reader",
            "needle-ci-artifact-publisher",
        },
        "brand-kit-workflow-liveness-workflowtemplate.yml": {
            "needle-ci-artifact-reader",
            "needle-ci-artifact-publisher",
        },
        "brand-kit-consumer-drift-workflowtemplate.yml": {
            "brand-kit-consumer-drift",
            "needle-ci-artifact-reader",
            "needle-ci-artifact-publisher",
        },
        "brand-kit-mirror-health-workflowtemplate.yml": {
            "brand-kit-consumer-drift",
            "needle-ci-artifact-publisher",
        },
    }

    for filename, allowed in expected.items():
        assert _secret_ref_names(_manifest(filename)) == allowed


def test_watch_workloads_carry_no_argo_api_token():
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
        assert "ARGO_WORKFLOW_TOKEN" not in env
        assert "ARGO_TOKEN" not in env
        assert "ARGO_SUBMIT_TOKEN" not in env
        assert RELEASE_SECRET not in filename
        serialized = (ROOT / "automation" / filename).read_text(encoding="utf-8")
        assert RELEASE_SECRET not in serialized
        assert RETIRED_WORKFLOW_SECRET not in serialized
        assert "argo-ci.ardenone.com" not in serialized


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


WORKFLOW_LIST_PATH = "/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/workflows"


def _argo_workflow_list_url(workflow_template):
    return (
        f"{KUBERNETES_API_URL}{WORKFLOW_LIST_PATH}?"
        "labelSelector=workflows.argoproj.io%2Fworkflow-template%3D"
        f"{workflow_template}&limit=100"
    )


def _assert_failure_watcher_argo_request(method, url, kwargs, *, api_url, token):
    """Enforce the documented, read-only Workflow list boundary for the watcher."""
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
    assert parsed.path == WORKFLOW_LIST_PATH

    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    assert set(query) in ({"labelSelector", "limit"}, {"labelSelector", "limit", "continue"})
    assert query["labelSelector"] == [
        "workflows.argoproj.io/workflow-template=brand-kit-ci"
    ]
    assert query["limit"] == ["100"]
    if "continue" in query:
        assert len(query["continue"]) == 1
        assert query["continue"][0]

    assert kwargs == {
        "token": token,
        "authorization_scheme": "Bearer",
        "service": "Kubernetes API",
    }


def _assert_failure_watcher_argo_contract(calls, *, api_url, token):
    assert calls, "the failure watcher should list Workflow objects"
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
            api_url=KUBERNETES_API_URL,
            token="workload-readonly",
        )
        return next(responses)

    report = brand_kit_ci_failure_watch.run_watch(
        "workload-readonly",
        api_url=KUBERNETES_API_URL,
        request=request,
    )

    assert report["status"] == "pass"
    _assert_failure_watcher_argo_contract(
        calls,
        api_url=KUBERNETES_API_URL,
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
        (
            "GET",
            f"{KUBERNETES_API_URL}/apis/argoproj.io/v1alpha1/namespaces/other/workflows"
            "?labelSelector=workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-ci&limit=100",
        ),
        (
            "GET",
            f"{KUBERNETES_API_URL}/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/cronworkflows"
            "?labelSelector=workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-ci&limit=100",
        ),
        (
            "GET",
            "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows"
            "?labelSelector=workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-ci&limit=100",
        ),
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
                "service": "Kubernetes API",
            },
            api_url=KUBERNETES_API_URL,
            token="workload-readonly",
        )


class _RecordingReportReader:
    """Fake Garage reader that records every call the liveness check makes."""

    def __init__(self):
        self.calls = []

    def list_objects(self, prefix):
        self.calls.append(("list_objects", prefix))
        return []

    def get_object(self, key):
        self.calls.append(("get_object", key))
        raise AssertionError("no report exists to read")


def test_liveness_reads_only_its_targets_report_prefixes():
    reader = _RecordingReportReader()

    report = brand_kit_workflow_liveness.run_liveness(reader)

    assert report["status"] == "stale"
    assert reader.calls == [
        ("list_objects", target["report_prefix"])
        for target in brand_kit_workflow_liveness.TARGETS
    ]
    for target in brand_kit_workflow_liveness.TARGETS:
        assert target["report_prefix"].startswith(f"failures/{target['name']}/v")
        assert target["report_prefix"].endswith("/")


def test_liveness_main_uses_only_the_read_only_artifact_credential(tmp_path, monkeypatch):
    created = []

    class Client:
        def __init__(self, endpoint, bucket, credentials, **kwargs):
            created.append((endpoint, bucket, credentials))

        def list_objects(self, prefix):
            return []

        def get_object(self, key):
            raise AssertionError("no report exists to read")

    monkeypatch.setenv("S3_READER_ACCESS_KEY", "reader-access")
    monkeypatch.setenv("S3_READER_SECRET_KEY", "reader-secret")
    monkeypatch.setenv("S3_PUBLISHER_ACCESS_KEY", "publisher-access")
    monkeypatch.setenv("S3_PUBLISHER_SECRET_KEY", "publisher-secret")
    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", "must-not-be-used")
    monkeypatch.setattr(brand_kit_workflow_liveness.s3_reports, "S3Client", Client)

    exit_code = brand_kit_workflow_liveness.main(["--report", str(tmp_path / "report.json")])

    assert exit_code == 1
    assert created == [
        (
            "https://s3.ardenone.com",
            "needle-ci-artifacts",
            prune_ci_failure_watch_reports.S3Credentials("reader-access", "reader-secret"),
        )
    ]


def _consumer_audit_forgejo_release_url(tag):
    return (
        "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/"
        f"releases/tags/{tag}"
    )


def _assert_consumer_audit_forgejo_request(method, url, authorization):
    """Enforce the consumer audit's single read-only Forgejo release lookup."""
    assert method == "GET"
    assert url == _consumer_audit_forgejo_release_url("v1.1.0")
    assert authorization == "token consumer-audit-readonly"


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
        method = request.get_method()
        url = request.full_url
        authorization = request.get_header("Authorization")
        _assert_consumer_audit_forgejo_request(method, url, authorization)
        requests.append((method, url, authorization))
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
        token="consumer-audit-readonly",
    )["tag_name"] == "v1.1.0"
    assert requests == [
        (
            "GET",
            _consumer_audit_forgejo_release_url("v1.1.0"),
            "token consumer-audit-readonly",
        )
    ]


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", _consumer_audit_forgejo_release_url("v1.1.0")),
        ("PATCH", _consumer_audit_forgejo_release_url("v1.1.0")),
        ("PUT", _consumer_audit_forgejo_release_url("v1.1.0")),
        ("DELETE", _consumer_audit_forgejo_release_url("v1.1.0")),
        (
            "GET",
            "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1",
        ),
    ],
)
def test_consumer_audit_forgejo_contract_rejects_unexpected_method_or_endpoint(
    method, url
):
    with pytest.raises(AssertionError):
        _assert_consumer_audit_forgejo_request(
            method,
            url,
            "token consumer-audit-readonly",
        )


# ---------------------------------------------------------------------------
# Mirror-health workload: the same read-only Forgejo repo token, but it must
# reach git only through the host-scoped credential helper and its git surface
# is limited to the documented read-only subcommands (no push, no Forgejo API).
# ---------------------------------------------------------------------------

MIRROR_HEALTH_TEMPLATE = "brand-kit-mirror-health-workflowtemplate.yml"
MIRROR_HEALTH_CANONICAL_REPOSITORY = "https://git.ardenone.com/jedarden/brand-kit.git"
MIRROR_HEALTH_MIRROR_REPOSITORY = "https://github.com/jedarden/brand-kit.git"
MIRROR_HEALTH_FORGEJO_CREDENTIAL_KEY = "credential.https://git.ardenone.com.helper"
MIRROR_HEALTH_FORGEJO_HELPER = (
    '!f() { test "$1" = get && echo "username=x-token" '
    '&& echo "password=$FORGEJO_TOKEN"; }; f'
)
# clone/ls-remote/fetch are the documented read-only git surface; init,
# rev-parse and merge-base are the checker's local-only scratch operations.
MIRROR_HEALTH_READ_ONLY_GIT_SUBCOMMANDS = {
    "clone",
    "ls-remote",
    "fetch",
    "init",
    "rev-parse",
    "merge-base",
}
MIRROR_HEALTH_GIT_HOSTS = {"git.ardenone.com", "github.com"}
MIRROR_HEALTH_ENV_NAMES = {
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "FORGEJO_TOKEN",
}


def _mirror_health_check_container():
    manifest = _manifest(MIRROR_HEALTH_TEMPLATE)
    check = next(
        template
        for template in manifest["spec"]["templates"]
        if template["name"] == "check"
    )
    return manifest, check["container"]


def _mirror_health_env(container):
    return _env_by_name(container["env"])


def _assert_mirror_health_credential_env(env):
    """The Forgejo token may reach git only through the host-scoped helper.

    A global ``credential.helper`` (or any second config entry) would offer the
    token to every host git touches, GitHub included; a literal value would put
    it in the manifest and in ``kubectl get`` output; terminal prompting would
    give a failed helper a second chance to move the token.
    """
    assert set(env) == MIRROR_HEALTH_ENV_NAMES
    assert env["GIT_TERMINAL_PROMPT"]["value"] == "0"
    assert env["GIT_CONFIG_COUNT"]["value"] == "1"
    assert env["GIT_CONFIG_KEY_0"]["value"] == MIRROR_HEALTH_FORGEJO_CREDENTIAL_KEY
    assert env["GIT_CONFIG_VALUE_0"]["value"] == MIRROR_HEALTH_FORGEJO_HELPER
    secret_ref = env["FORGEJO_TOKEN"].get("valueFrom", {}).get("secretKeyRef")
    assert secret_ref == {"name": "brand-kit-consumer-drift", "key": "token"}


def test_mirror_health_offers_its_forgejo_token_only_through_the_scoped_helper():
    manifest, container = _mirror_health_check_container()
    assert manifest["metadata"]["namespace"] == WORKFLOW_NAMESPACE
    assert manifest["spec"]["serviceAccountName"] == "argo-workflow"
    _assert_mirror_health_credential_env(_mirror_health_env(container))

    # The executed script never references the token env; the helper value is
    # its only consumer, and the Forgejo API (/api/v1) is never mentioned.
    script = container["args"][0]
    assert "FORGEJO_TOKEN" not in script
    assert "api/v1" not in (ROOT / "automation" / MIRROR_HEALTH_TEMPLATE).read_text(
        encoding="utf-8"
    )


def _mutated_mirror_health_env(mutator):
    _, container = _mirror_health_check_container()
    env = _mirror_health_env(container)
    mutator(env)
    return env


@pytest.mark.parametrize(
    "mutator",
    [
        # A global helper would hand the token to every host, GitHub included.
        pytest.param(
            lambda env: env.update(
                {
                    "GIT_CONFIG_KEY_0": {
                        "name": "GIT_CONFIG_KEY_0",
                        "value": "credential.helper",
                    }
                }
            ),
            id="global-credential-helper",
        ),
        pytest.param(
            lambda env: env.update(
                {
                    "GIT_CONFIG_KEY_0": {
                        "name": "GIT_CONFIG_KEY_0",
                        "value": "credential.https://github.com.helper",
                    }
                }
            ),
            id="github-scoped-helper",
        ),
        pytest.param(
            lambda env: env.update(
                {
                    "GIT_CONFIG_KEY_0": {
                        "name": "GIT_CONFIG_KEY_0",
                        "value": "credential.http://git.ardenone.com.helper",
                    }
                }
            ),
            id="cleartext-http-scope",
        ),
        pytest.param(
            lambda env: env.update(
                {
                    "GIT_CONFIG_COUNT": {"name": "GIT_CONFIG_COUNT", "value": "2"},
                    "GIT_CONFIG_KEY_1": {
                        "name": "GIT_CONFIG_KEY_1",
                        "value": "credential.https://github.com.helper",
                    },
                    "GIT_CONFIG_VALUE_1": {
                        "name": "GIT_CONFIG_VALUE_1",
                        "value": MIRROR_HEALTH_FORGEJO_HELPER,
                    },
                }
            ),
            id="second-config-entry-for-github",
        ),
        pytest.param(
            lambda env: env.update(
                {"FORGEJO_TOKEN": {"name": "FORGEJO_TOKEN", "value": "literal-token"}}
            ),
            id="literal-token-instead-of-secret-ref",
        ),
        pytest.param(
            lambda env: env["FORGEJO_TOKEN"]["valueFrom"]["secretKeyRef"].update(
                {"name": "forgejo-webhook-token"}
            ),
            id="write-capable-token-secret",
        ),
        pytest.param(
            lambda env: env.update(
                {"GIT_TERMINAL_PROMPT": {"name": "GIT_TERMINAL_PROMPT", "value": "1"}}
            ),
            id="terminal-prompting-enabled",
        ),
        pytest.param(
            lambda env: env.pop("GIT_TERMINAL_PROMPT"),
            id="missing-prompt-guard",
        ),
        pytest.param(
            lambda env: env.update(
                {
                    "GIT_CONFIG_VALUE_0": {
                        "name": "GIT_CONFIG_VALUE_0",
                        "value": (
                            '!f() { echo "username=x-token"; '
                            'echo "password=$FORGEJO_TOKEN"; }; f'
                        ),
                    }
                }
            ),
            id="helper-answers-every-action",
        ),
    ],
)
def test_mirror_health_credential_env_rejects_helper_scope_or_source_changes(
    mutator,
):
    with pytest.raises(AssertionError):
        _assert_mirror_health_credential_env(_mutated_mirror_health_env(mutator))


def _git_credential_fill(tmp_path, token, url):
    """Run git's credential layer with the workload's helper and nothing else.

    Returns plain (returncode, stdout, stderr) scalars so a failing assertion
    can never repr a CompletedProcess carrying a host credential. Every env
    var that could answer prompts or inject config on this developer box
    (GIT_ASKPASS, SSH_ASKPASS, ambient GIT_CONFIG_* entries) is stripped from
    the child environment, and the -c resets drop any file-configured helper.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_CONFIG_", "VSCODE_GIT"))
        and key not in {"GIT_ASKPASS", "SSH_ASKPASS", "GIT_USERNAME", "GIT_PASSWORD"}
    }
    environment.update(
        {
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "FORGEJO_TOKEN": token,
        }
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            f"{MIRROR_HEALTH_FORGEJO_CREDENTIAL_KEY}={MIRROR_HEALTH_FORGEJO_HELPER}",
            "credential",
            "fill",
        ],
        input=f"url={url}\n",
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.returncode, result.stdout, result.stderr


def test_git_delivers_the_helper_credential_only_to_git_ardenone_com(tmp_path):
    token = "mirror-health-readonly-token"

    forgejo_code, forgejo_out, forgejo_err = _git_credential_fill(
        tmp_path, token, MIRROR_HEALTH_CANONICAL_REPOSITORY
    )

    assert forgejo_code == 0
    attributes = dict(
        line.split("=", 1) for line in forgejo_out.splitlines() if "=" in line
    )
    assert attributes["username"] == "x-token"
    assert attributes["password"] == token
    assert token not in forgejo_err

    # No credential is offered to GitHub or to a host-shaped lookalike: the
    # scoped helper does not match, prompting is disabled, and git fails
    # rather than leaking. These are asserted on booleans only, never on the
    # raw streams, so a regression cannot print a real host credential here.
    for url in (
        MIRROR_HEALTH_MIRROR_REPOSITORY,
        "https://git.ardenone.com.evil.test/jedarden/brand-kit.git",
    ):
        refused_code, refused_out, refused_err = _git_credential_fill(
            tmp_path, token, url
        )
        assert refused_code != 0, f"a credential source answered for {url}"
        assert not any(
            line.startswith("password=") for line in refused_out.splitlines()
        ), f"a credential was offered for {url}"
        assert token not in refused_out
        assert token not in refused_err


def test_the_helper_answers_only_get_and_never_persists_or_logs_the_token(
    tmp_path, monkeypatch
):
    token = "mirror-health-readonly-token"
    monkeypatch.setenv("FORGEJO_TOKEN", token)

    # git runs a bang-prefixed helper as `sh -c '<script> "$@"' <script>
    # <action>`; replicate that invocation exactly.
    helper_script = MIRROR_HEALTH_FORGEJO_HELPER[1:] + ' "$@"'

    def run_helper(action):
        return subprocess.run(
            ["bash", "-c", helper_script, "git-credential-helper", action],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    get = run_helper("get")
    assert get.returncode == 0
    assert get.stdout == f"username=x-token\npassword={token}\n"
    assert get.stderr == ""
    assert token not in " ".join(
        ["bash", "-c", helper_script, "git-credential-helper", "get"]
    )

    for action in ("store", "erase"):
        result = run_helper(action)
        # The function's `test "$1" = get && ...` chain exits 1 for these
        # actions, which git tolerates for store/erase. What matters: they
        # produce no output and persist nothing.
        assert result.stdout == ""
        assert result.stderr == ""
        assert token not in result.stdout

    assert list(tmp_path.iterdir()) == []  # nothing was ever written to disk


def test_mirror_health_script_clones_anonymously_and_reads_forgejo_over_plain_git():
    _, container = _mirror_health_check_container()
    script = container["args"][0]
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )

    git_verbs = {
        verb
        for line in code.splitlines()
        for verb in re.findall(r"\bgit[ \t]+([a-z][a-z-]+)", line)
    }
    assert git_verbs == {"clone"}  # its own code, anonymously from GitHub

    urls = [urlsplit(url) for url in re.findall(r"https?://\S+", code)]
    assert {(url.hostname, url.path) for url in urls} == {
        ("github.com", "/jedarden/brand-kit.git"),
        ("git.ardenone.com", "/jedarden/brand-kit.git"),
    }
    assert all(url.username is None and url.password is None for url in urls)


def _assert_mirror_health_git_call(arguments, cwd, *, check):
    """Enforce the documented read-only git surface for one checker git call.

    Subcommands stay within clone/ls-remote/fetch plus the checker's
    local-only init/rev-parse/merge-base scratch operations; URLs carry no
    embedded credential and name only the two documented hosts; anything
    touching a working tree runs inside the disposable bare repository, never
    the workflow's own checkout.
    """
    assert arguments[0] in MIRROR_HEALTH_READ_ONLY_GIT_SUBCOMMANDS, arguments
    for argument in arguments[1:]:
        if "://" in argument:
            parsed = urlsplit(argument)
            assert parsed.scheme == "https"
            assert parsed.username is None
            assert parsed.password is None
            assert parsed.hostname in MIRROR_HEALTH_GIT_HOSTS
    if cwd is not None:
        assert cwd.name == "objects.git"
        assert cwd.parent.name.startswith("brand-kit-mirror-health-")
    if arguments[0] == "merge-base":
        assert check is False  # its status is data, not a failure
    else:
        assert check is True  # every other call fails closed


def _recording_mirror_health_git(calls):
    """Fake executor serving a stale branch and a missing tag on the mirror."""
    fetched = {}

    def execute(arguments, cwd, check):
        calls.append((arguments, cwd, check))
        if arguments[:2] == ["ls-remote", "--heads"]:
            commit = (
                MIRROR_HEALTH_CANONICAL_MAIN
                if arguments[2] == MIRROR_HEALTH_CANONICAL_REPOSITORY
                else MIRROR_HEALTH_MIRROR_MAIN
            )
            return subprocess.CompletedProcess(
                arguments, 0, f"{commit}\trefs/heads/main\n", ""
            )
        if arguments[:2] == ["ls-remote", "--tags"]:
            output = (
                f"{MIRROR_HEALTH_CANONICAL_TAG}\trefs/tags/v1.0.0\n"
                if arguments[2] == MIRROR_HEALTH_CANONICAL_REPOSITORY
                else ""
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:1] == ["init"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:1] == ["fetch"]:
            local_ref = arguments[-1].rsplit(":", 1)[1]
            label = local_ref.split("/")[2]
            fetched[local_ref] = (
                MIRROR_HEALTH_CANONICAL_MAIN
                if label == "canonical"
                else MIRROR_HEALTH_MIRROR_MAIN
            )
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:1] == ["rev-parse"]:
            local_ref = arguments[1][: -len("^{commit}")]
            return subprocess.CompletedProcess(
                arguments, 0, fetched[local_ref] + "\n", ""
            )
        if arguments[:2] == ["merge-base", "--is-ancestor"]:
            older, newer = arguments[2], arguments[3]
            behind = (older, newer) == (
                MIRROR_HEALTH_MIRROR_MAIN,
                MIRROR_HEALTH_CANONICAL_MAIN,
            )
            return subprocess.CompletedProcess(arguments, 0 if behind else 1, "", "")
        raise AssertionError(f"unexpected git call: {arguments}")

    return execute


MIRROR_HEALTH_CANONICAL_MAIN = "a" * 40
MIRROR_HEALTH_MIRROR_MAIN = "b" * 40
MIRROR_HEALTH_CANONICAL_TAG = "c" * 40


def test_mirror_health_checker_git_surface_stays_within_the_read_only_allowlist(
    monkeypatch,
):
    token = "mirror-health-readonly-token"
    monkeypatch.setenv("FORGEJO_TOKEN", token)
    calls = []

    report = check_mirror_health.run_check(
        MIRROR_HEALTH_CANONICAL_REPOSITORY,
        MIRROR_HEALTH_MIRROR_REPOSITORY,
        execute=_recording_mirror_health_git(calls),
    )

    assert report["status"] == "unhealthy"
    assert {(entry["kind"], entry["status"]) for entry in report["checks"]} == {
        ("branch", "stale"),
        ("tag", "missing"),
    }
    executed = {arguments[0] for arguments, _, _ in calls}
    assert executed <= MIRROR_HEALTH_READ_ONLY_GIT_SUBCOMMANDS
    for arguments, cwd, check in calls:
        _assert_mirror_health_git_call(arguments, cwd, check=check)
        assert token not in " ".join(arguments)

    ls_remote_remotes = {
        arguments[2] for arguments, _, _ in calls if arguments[:1] == ["ls-remote"]
    }
    assert ls_remote_remotes == {
        MIRROR_HEALTH_CANONICAL_REPOSITORY,
        MIRROR_HEALTH_MIRROR_REPOSITORY,
    }
    fetched_remotes = {
        arguments[3] for arguments, _, _ in calls if arguments[:1] == ["fetch"]
    }
    assert fetched_remotes == {
        MIRROR_HEALTH_CANONICAL_REPOSITORY,
        MIRROR_HEALTH_MIRROR_REPOSITORY,
    }


@pytest.mark.parametrize(
    ("arguments", "cwd", "check"),
    [
        (
            ["push", MIRROR_HEALTH_MIRROR_REPOSITORY, "main"],
            None,
            True,
        ),
        (
            [
                "ls-remote",
                "--heads",
                "https://x-token:secret@git.ardenone.com/jedarden/brand-kit.git",
            ],
            None,
            True,
        ),
        (
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "https://gitlab.example/jedarden/brand-kit.git",
                "refs/heads/main:refs/brand-kit-mirror-health/canonical/x",
            ],
            Path("/tmp/brand-kit-mirror-health-abc/objects.git"),
            True,
        ),
        (
            ["rev-parse", "refs/brand-kit-mirror-health/canonical/x^{commit}"],
            Path("/brand-kit"),
            True,
        ),
        (
            ["merge-base", "--is-ancestor", "a" * 40, "b" * 40],
            None,
            True,
        ),
        (
            ["config", "credential.helper", "store"],
            None,
            True,
        ),
        (
            [
                "clone",
                "--filter=blob:none",
                "http://git.ardenone.com/jedarden/brand-kit.git",
                "/tmp/scratch",
            ],
            None,
            True,
        ),
    ],
)
def test_mirror_health_git_contract_rejects_out_of_allowlist_calls(
    arguments, cwd, check
):
    with pytest.raises(AssertionError):
        _assert_mirror_health_git_call(arguments, cwd, check=check)


def test_mirror_health_checker_has_no_http_client_so_forgejo_stays_git_only():
    source = Path(check_mirror_health.__file__).read_text(encoding="utf-8")
    for needle in ("urllib", "http.client", "requests", "api/v1"):
        assert needle not in source
    assert (
        check_mirror_health.DEFAULT_CANONICAL_REPOSITORY
        == MIRROR_HEALTH_CANONICAL_REPOSITORY
    )
    assert (
        check_mirror_health.DEFAULT_MIRROR_REPOSITORY
        == MIRROR_HEALTH_MIRROR_REPOSITORY
    )


ARGO_SUBMIT_URL = "https://argo.example/api/v1/workflows/argo-workflows/submit"
CI_SUBMIT_TOKEN = "argo-submit-test-token"
CI_COMMIT = "c" * 40


def _submitted_ci_workflow():
    return {
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
                    {"name": "revision", "value": CI_COMMIT},
                ]
            }
        },
    }


def _assert_ci_submitter_argo_request(
    method, url, payload, token, kwargs, *, api_url, expected_token, commit
):
    """Enforce the documented single brand-kit-ci submission POST boundary."""
    assert method == "POST"

    expected_origin = urlsplit(api_url)
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc) == (
        expected_origin.scheme,
        expected_origin.netloc,
    )
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.fragment
    assert not parsed.query
    assert parsed.path == "/api/v1/workflows/argo-workflows/submit"

    assert payload == brand_kit_ci_submit.workflow_payload(commit)
    assert payload["resourceKind"] == "WorkflowTemplate"
    assert payload["resourceName"] == "brand-kit-ci"
    assert payload["namespace"] == "argo-workflows"
    # Only the scheduled probe dry-runs; the real trigger posts for effect.
    assert "serverDryRun" not in payload["submitOptions"]
    revision = [
        parameter
        for parameter in payload["submitOptions"]["parameters"]
        if parameter.startswith("revision=")
    ]
    assert revision == [f"revision={commit}"]

    # The submission POST carries exactly the submit credential, as a Bearer
    # header, to the Argo API -- never the read token or a Forgejo-style scheme.
    assert token == expected_token
    assert kwargs == {
        "authorization_scheme": "Bearer",
        "service": "Argo API",
    }


def test_ci_submitter_issues_only_the_documented_brand_kit_ci_submit_post():
    calls = []

    def request(method, url, payload=None, token=None, **kwargs):
        calls.append((method, url, payload, token, kwargs))
        return _submitted_ci_workflow()

    submitted = brand_kit_ci_submit.submit_brand_kit_ci(
        CI_COMMIT,
        argo_api_url="https://argo.example",
        argo_token=CI_SUBMIT_TOKEN,
        request=request,
    )

    assert submitted["metadata"]["name"] == "brand-kit-ci-abc123"
    assert len(calls) == 1
    method, url, payload, token, kwargs = calls[0]
    _assert_ci_submitter_argo_request(
        method,
        url,
        payload,
        token,
        kwargs,
        api_url="https://argo.example",
        expected_token=CI_SUBMIT_TOKEN,
        commit=CI_COMMIT,
    )


CI_SUBMIT_PAYLOAD = brand_kit_ci_submit.workflow_payload(CI_COMMIT)


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("GET", ARGO_SUBMIT_URL, CI_SUBMIT_PAYLOAD),
        ("PATCH", ARGO_SUBMIT_URL, CI_SUBMIT_PAYLOAD),
        ("PUT", ARGO_SUBMIT_URL, CI_SUBMIT_PAYLOAD),
        ("DELETE", ARGO_SUBMIT_URL, CI_SUBMIT_PAYLOAD),
        (
            "POST",
            "https://argo.example/api/v1/workflows/other-namespace/submit",
            CI_SUBMIT_PAYLOAD,
        ),
        (
            "POST",
            "https://argo.example/api/v1/workflows/argo-workflows/brand-kit-ci-abc123",
            CI_SUBMIT_PAYLOAD,
        ),
        (
            "POST",
            "https://argo.example/api/v1/workflows/argo-workflows/cronworkflows/submit",
            CI_SUBMIT_PAYLOAD,
        ),
        (
            "POST",
            "https://kubernetes.example/api/v1/workflows/argo-workflows/submit",
            CI_SUBMIT_PAYLOAD,
        ),
        (
            "POST",
            ARGO_SUBMIT_URL,
            dict(CI_SUBMIT_PAYLOAD, resourceName="brand-kit-consumer-drift"),
        ),
        (
            "POST",
            ARGO_SUBMIT_URL,
            dict(
                CI_SUBMIT_PAYLOAD,
                submitOptions=dict(
                    CI_SUBMIT_PAYLOAD["submitOptions"], serverDryRun=True
                ),
            ),
        ),
        (
            "POST",
            ARGO_SUBMIT_URL,
            {
                "namespace": "argo-workflows",
                "resourceKind": "WorkflowTemplate",
                "resourceName": "brand-kit-ci",
                "submitOptions": {
                    "parameters": [
                        "repo=jedarden/brand-kit",
                        "branch=main",
                    ]
                },
            },
        ),
    ],
)
def test_ci_submitter_contract_rejects_unexpected_method_or_endpoint(method, url, payload):
    with pytest.raises(AssertionError):
        _assert_ci_submitter_argo_request(
            method,
            url,
            payload,
            CI_SUBMIT_TOKEN,
            {
                "authorization_scheme": "Bearer",
                "service": "Argo API",
            },
            api_url="https://argo.example",
            expected_token=CI_SUBMIT_TOKEN,
            commit=CI_COMMIT,
        )


@pytest.mark.parametrize(
    ("token", "kwargs"),
    [
        (CI_SUBMIT_TOKEN, {"authorization_scheme": "token", "service": "Argo API"}),
        ("argo-read-secret", {"authorization_scheme": "Bearer", "service": "Argo API"}),
        (CI_SUBMIT_TOKEN, {}),
        (
            CI_SUBMIT_TOKEN,
            {
                "authorization_scheme": "Bearer",
                "service": "Argo API",
                "server": "https://argo.example",
            },
        ),
    ],
)
def test_ci_submitter_contract_rejects_unexpected_authorization(token, kwargs):
    with pytest.raises(AssertionError):
        _assert_ci_submitter_argo_request(
            "POST",
            ARGO_SUBMIT_URL,
            CI_SUBMIT_PAYLOAD,
            token,
            kwargs,
            api_url="https://argo.example",
            expected_token=CI_SUBMIT_TOKEN,
            commit=CI_COMMIT,
        )


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
        "ARGO_TOKEN",
        "ARGO_SUBMIT_TOKEN",
        "argo-workflows",
        "brand-kit-ci-failure-watch",
        "brand-kit-workflow-liveness",
        "brand-kit-consumer-drift",
        "argo-workflow",
        "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows",
        "https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/workflows",
        "workflowtaskresults",
        "needle-ci-artifact-reader",
        "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1",
        "secret/rs-manager/brand-kit/release/argo-read",
        "secret/rs-manager/brand-kit/release/argo-submit",
        "rs-manager/iad-ci/forgejo/repo-readonly",
        "credential.https://git.ardenone.com.helper",
        "No `POST`, `PUT`, `PATCH`, or `DELETE`",
        "Brand-kit CI submitter operator",
        "tools/brand_kit_ci_submit.py",
        "`POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/submit`",
        "submitting only `brand-kit-ci`",
        "`resourceName: brand-kit-ci`",
    ):
        assert value in document


def test_workload_rotation_and_revocation_contract_is_documented():
    separation = (ROOT / "docs" / "notes" / "argo-credential-separation.md").read_text(
        encoding="utf-8"
    )
    provisioning = (ROOT / "docs" / "notes" / "release-token-provisioning.md").read_text(
        encoding="utf-8"
    )

    for value in (
        "## Workload rotation, revocation, and compromise response",
        "needle-ci-artifact-reader-v2-s3-credentials",
        "needle-ci-artifact-publisher-v2-s3-credentials",
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "rs-manager/iad-ci/forgejo/repo-readonly",
        "property `token`",
        "SecretSynced=True",
        "Ready=True",
        "existing OpenBao record",
        "existing pods",
        "in-flight",
        "scheduled CronWorkflows remain",
        "Do not\n   suspend, delete, or edit the CronWorkflow",
        "brand-kit-ci-failure-watch",
        "brand-kit-workflow-liveness",
        "brand-kit-consumer-drift",
        "not an OpenBao/ESO credential",
        "projected tokens are short-lived and kubelet-managed",
    ):
        assert value in separation

    for value in (
        "## Workload credential rotation",
        "same-scope replacement",
        "existing authoritative OpenBao path",
        "Ready=True`/`SecretSynced=True",
        "consumer-drift and mirror-health",
        "do not widen scope or fall back to an operator release token",
        "not rotated by OpenBao or ESO",
    ):
        assert value in provisioning


@pytest.mark.parametrize(
    ("cron_filename", "template_filename", "template_name", "service_account"),
    [
        (
            "brand-kit-ci-failure-watch-cronworkflow.yml",
            "brand-kit-ci-failure-watch-workflowtemplate.yml",
            "brand-kit-ci-failure-watch",
            "brand-kit-ci-failure-watch",
        ),
        (
            "brand-kit-workflow-liveness-cronworkflow.yml",
            "brand-kit-workflow-liveness-workflowtemplate.yml",
            "brand-kit-workflow-liveness",
            "brand-kit-workflow-liveness",
        ),
        (
            "brand-kit-consumer-drift-cronworkflow.yml",
            "brand-kit-consumer-drift-workflowtemplate.yml",
            "brand-kit-consumer-drift",
            "brand-kit-consumer-drift",
        ),
        (
            "brand-kit-mirror-health-cronworkflow.yml",
            "brand-kit-mirror-health-workflowtemplate.yml",
            "brand-kit-mirror-health",
            "argo-workflow",
        ),
    ],
)
def test_secret_rotation_keeps_scheduled_workflows_enabled_and_on_the_same_template(
    cron_filename, template_filename, template_name, service_account
):
    cron = _manifest(cron_filename)
    template = _manifest(template_filename)

    assert cron["metadata"]["namespace"] == WORKFLOW_NAMESPACE
    assert cron["spec"].get("suspend") is not True
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["workflowSpec"]["workflowTemplateRef"] == {
        "name": template_name
    }
    assert template["metadata"]["namespace"] == WORKFLOW_NAMESPACE
    assert template["spec"]["serviceAccountName"] == service_account


def test_shared_forgejo_rotation_keeps_both_scheduled_consumers_on_one_secret():
    consumer = _manifest("brand-kit-consumer-drift-workflowtemplate.yml")
    mirror = _manifest("brand-kit-mirror-health-workflowtemplate.yml")

    consumer_env = _env_by_name(_container_env(consumer, "audit"))
    mirror_env = _env_by_name(_container_env(mirror, "check"))
    assert consumer_env["FORGEJO_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "brand-kit-consumer-drift",
        "key": "token",
    }
    assert mirror_env["FORGEJO_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "brand-kit-consumer-drift",
        "key": "token",
    }
