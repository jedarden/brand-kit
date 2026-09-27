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


def test_parity_check_compares_both_manifests_read_only():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        manifest = MANIFESTS[len(calls) - 1]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(yaml.safe_load(manifest.read_text())),
            stderr="",
        )

    assert (
        check_ci_failure_watch_parity.check_parity(
            MANIFESTS,
            server="http://traefik-iad-ci:8001",
            runner=runner,
        )
        == 0
    )
    assert [call[0][-5:] for call in calls] == [
        [
            "get",
            "workflowtemplate/brand-kit-ci-failure-watch",
            "--namespace",
            "argo-workflows",
            "--output=json",
        ],
        [
            "get",
            "cronworkflow/brand-kit-ci-failure-watch",
            "--namespace",
            "argo-workflows",
            "--output=json",
        ],
    ]
    assert all(call[1] == {"check": False, "capture_output": True, "text": True} for call in calls)
    assert all("--server" in call[0] for call in calls)


def test_parity_check_reports_manifest_drift():
    calls = []

    def runner(command, **kwargs):
        manifest = yaml.safe_load(MANIFESTS[len(calls)].read_text())
        calls.append(command)
        if len(calls) == 2:
            manifest["spec"]["schedules"] = ["0 * * * *"]
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(manifest), stderr=""
        )

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 1


def test_parity_check_reports_kubectl_failure():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="forbidden"
        )

    assert check_ci_failure_watch_parity.check_parity(MANIFESTS, runner=runner) == 2


def test_parity_check_reports_missing_manifest(tmp_path):
    missing = tmp_path / "missing.yml"

    assert check_ci_failure_watch_parity.check_parity((missing,)) == 2
