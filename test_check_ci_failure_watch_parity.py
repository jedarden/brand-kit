from pathlib import Path
import json
import subprocess

import yaml

from tools import check_ci_failure_watch_parity


ROOT = Path(__file__).resolve().parent
MANIFESTS = (
    ROOT / "automation/brand-kit-ci-failure-watch-workflowtemplate.yml",
    ROOT / "automation/brand-kit-ci-failure-watch-cronworkflow.yml",
)
APPLICATION = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Application",
    "metadata": {
        "name": "brand-kit-automation-iad-ci",
        "namespace": "argocd",
    },
    "spec": {
        "source": {
            "repoURL": "https://github.com/jedarden/brand-kit.git",
            "targetRevision": "main",
            "path": "automation",
            "directory": {"recurse": True, "include": "{*.yaml,*.yml}"},
        },
        "destination": {"namespace": "argo-workflows"},
    },
    "status": {
        "sync": {"status": "Synced"},
        "health": {"status": "Healthy"},
    },
}
READ_ONLY_SUBCOMMANDS = {"get", "diff"}
MUTATING_SUBCOMMANDS = {
    "annotate",
    "apply",
    "create",
    "delete",
    "edit",
    "label",
    "patch",
    "replace",
    "rollout",
    "scale",
}


def _subcommand(command):
    return next(
        argument
        for argument in command
        if argument in READ_ONLY_SUBCOMMANDS | MUTATING_SUBCOMMANDS
    )


def _matching_response(command, manifest):
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(yaml.safe_load(manifest.read_text(encoding="utf-8"))),
        stderr="",
    )


def _application_response(command, application=None):
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(application or APPLICATION),
        stderr="",
    )


def test_parity_check_compares_both_manifests_read_only():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _application_response(command)
        manifest = MANIFESTS[len(calls) - 2]
        return _matching_response(command, manifest)

    assert (
        check_ci_failure_watch_parity.check_parity(
            MANIFESTS,
            server="http://traefik-iad-ci:8001",
            runner=runner,
        )
        == 0
    )
    assert [call[0] for call in calls] == [
        [
            "kubectl",
            "--server",
            "http://traefik-rs-manager:8001",
            "get",
            "application/brand-kit-automation-iad-ci",
            "--namespace",
            "argocd",
            "--output=json",
        ],
        [
            "kubectl",
            "--server",
            "http://traefik-iad-ci:8001",
            "get",
            "workflowtemplate/brand-kit-ci-failure-watch",
            "--namespace",
            "argo-workflows",
            "--output=json",
        ],
        [
            "kubectl",
            "--server",
            "http://traefik-iad-ci:8001",
            "get",
            "cronworkflow/brand-kit-ci-failure-watch",
            "--namespace",
            "argo-workflows",
            "--output=json",
        ],
    ]
    assert all(call[1] == {"check": False, "capture_output": True, "text": True} for call in calls)
    assert [_subcommand(call[0]) for call in calls] == ["get", "get", "get"]
    assert all(not set(call[0]) & MUTATING_SUBCOMMANDS for call in calls)


def test_parity_check_requires_both_objects_to_match():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        manifest = yaml.safe_load(MANIFESTS[len(calls) - 2].read_text())
        if len(calls) == 3:
            manifest["spec"]["schedules"] = ["0 * * * *"]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(manifest), stderr=""
        )

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 1
    assert len(calls) == 3
    assert [_subcommand(command) for command in calls] == ["get", "get", "get"]


def test_parity_check_reports_manifest_drift():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        manifest = yaml.safe_load(MANIFESTS[len(calls) - 2].read_text())
        if len(calls) == 3:
            manifest["spec"]["schedules"] = ["0 * * * *"]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(manifest), stderr=""
        )

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 1


def test_parity_check_reports_missing_live_object():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        if len(calls) == 2:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="NotFound"
            )
        return _matching_response(command, MANIFESTS[1])

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 2
    assert len(calls) == 3


def test_parity_check_reports_kubectl_api_error():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="forbidden"
        )

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 2
    assert len(calls) == 3


def test_parity_check_never_issues_a_mutating_kubectl_command():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        assert _subcommand(command) in READ_ONLY_SUBCOMMANDS
        assert not set(command) & MUTATING_SUBCOMMANDS
        manifest = MANIFESTS[len(calls) - 2]
        return _matching_response(command, manifest)

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 0
    assert len(calls) == 3


def test_parity_check_reports_missing_manifest(tmp_path):
    missing = tmp_path / "missing.yml"

    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _application_response(command)
        return _matching_response(command, MANIFESTS[1])

    assert (
        check_ci_failure_watch_parity.check_parity(
            (missing, MANIFESTS[1]), runner=runner
        )
        == 2
    )
    assert len(calls) == 2
    assert _subcommand(calls[0]) == "get"


def test_application_smoke_check_rejects_configuration_drift_before_parity():
    calls = []
    application = json.loads(json.dumps(APPLICATION))
    application["spec"]["source"]["repoURL"] = (
        "https://github.com/example/not-brand-kit.git"
    )

    def runner(command, **kwargs):
        calls.append(command)
        return _application_response(command, application)

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 1
    assert len(calls) == 1


def test_application_smoke_check_requires_synced_and_healthy_before_parity():
    calls = []
    application = json.loads(json.dumps(APPLICATION))
    application["status"]["sync"]["status"] = "OutOfSync"

    def runner(command, **kwargs):
        calls.append(command)
        return _application_response(command, application)

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 1
    assert len(calls) == 1


def test_application_smoke_check_rejects_malformed_response_before_parity():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 2
    assert len(calls) == 1
