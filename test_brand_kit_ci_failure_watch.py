import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.error

import pytest

from tools import brand_kit_ci_failure_watch
from tools import release_publish


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40


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


def test_run_watch_exhausts_continuation_pages_before_applying_timestamp_cutoff():
    calls = []
    pages = iter(
        [
            {
                "items": [
                    workflow("brand-kit-ci-old-page-one", "Failed", "2026-09-27T10:59:59Z")
                ],
                "metadata": {"continue": "opaque/page+two"},
            },
            {
                "items": [
                    workflow(
                        "brand-kit-ci-recent-page-two",
                        "Error",
                        "2026-09-27T11:00:00-00:00",
                    ),
                    workflow(
                        "brand-kit-ci-success-page-two",
                        "Succeeded",
                        "2026-09-27T12:45:00Z",
                        commit=COMMIT,
                    ),
                ],
                "metadata": {"continue": ""},
            },
        ]
    )

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(pages)

    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        api_url="https://argo.example",
        now=NOW,
        request=request,
    )

    assert [failure["name"] for failure in report["failures"]] == [
        "brand-kit-ci-recent-page-two"
    ]
    assert report["attestations"][0]["workflow_uid"] == "uid-brand-kit-ci-success-page-two"
    assert len(calls) == 2
    assert "continue=opaque%2Fpage%2Btwo" in calls[1][1]
    assert "labelSelector=workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-ci" in calls[1][1]
    assert "limit=100" in calls[1][1]


def test_run_watch_fails_closed_when_a_continuation_page_is_incomplete():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return {
                "items": [workflow("brand-kit-ci-first-page", "Failed", "2026-09-27T12:45:00Z")],
                "metadata": {"continue": "next-page"},
            }
        return {"metadata": {"continue": "ignored"}}

    with pytest.raises(release_publish.ReleaseError, match="page 2 has no items list"):
        brand_kit_ci_failure_watch.run_watch(
            "argo-secret",
            now=NOW,
            request=request,
        )

    assert len(calls) == 2


def test_run_watch_fails_closed_when_a_continuation_page_omits_pagination_metadata():
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {"items": [], "metadata": {"continue": "next-page"}}
        return {"items": []}

    with pytest.raises(
        release_publish.ReleaseError,
        match="page 2 has no pagination metadata",
    ):
        brand_kit_ci_failure_watch.run_watch(
            "argo-secret",
            now=NOW,
            request=request,
        )

    assert len(calls) == 2


def test_run_watch_fails_closed_on_invalid_or_repeated_continuation_tokens():
    for metadata, message in (
        ({"continue": 123}, "invalid continuation token"),
        ({"continue": "same"}, "repeated a continuation token"),
    ):
        calls = []

        def request(method, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return {"items": [], "metadata": metadata}
            return {"items": [], "metadata": metadata}

        with pytest.raises(release_publish.ReleaseError, match=message):
            brand_kit_ci_failure_watch.run_watch(
                "argo-secret",
                now=NOW,
                request=request,
            )

        assert len(calls) == (1 if isinstance(metadata["continue"], int) else 2)


def test_run_watch_passes_when_no_recent_failure_exists():
    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        now=NOW,
        request=lambda *args, **kwargs: {"items": []},
    )

    assert report["status"] == "pass"
    assert report["failures"] == []


def test_run_watch_extracts_recent_successes_into_minimal_durable_attestations():
    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        now=NOW,
        request=lambda *args, **kwargs: {
            "items": [
                workflow(
                    "brand-kit-ci-success",
                    "Succeeded",
                    "2026-09-27T12:45:00Z",
                    commit=COMMIT,
                ),
                workflow(
                    "brand-kit-ci-old-success",
                    "Succeeded",
                    "2026-09-27T10:59:59Z",
                    commit=COMMIT,
                ),
                workflow(
                    "brand-kit-ci-no-commit",
                    "Succeeded",
                    "2026-09-27T12:50:00Z",
                ),
            ]
        },
    )

    assert report["attestations"] == [
        {
            "commit": COMMIT,
            "workflow_name": "brand-kit-ci-success",
            "workflow_uid": "uid-brand-kit-ci-success",
            "phase": "Succeeded",
            "finished_at": "2026-09-27T12:45:00Z",
        }
    ]


def test_main_writes_a_durable_attestation_index(tmp_path, monkeypatch):
    report_path = tmp_path / "failure-report.json"
    attestations_path = tmp_path / "attestations.json"

    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", "argo-attestation-contract-secret")
    monkeypatch.setattr(
        brand_kit_ci_failure_watch,
        "run_watch",
        lambda *args, **kwargs: {
            "schema": brand_kit_ci_failure_watch.REPORT_SCHEMA,
            "status": "pass",
            "complete": True,
            "observed_at": "2026-09-27T13:00:00Z",
            "cutoff": {
                "at": "2026-09-27T11:00:00Z",
                "lookback_minutes": 120,
                "argo_failure_retention_seconds": 7200,
            },
            "workflow_template": "brand-kit-ci",
            "watcher_workflow_uid": None,
            "attestations_url": None,
            "pagination": {"status": "complete", "pages_read": 1, "error": None},
            "error": None,
            "failures": [],
            "attestations": [
                {
                    "commit": "a" * 40,
                    "workflow_name": "brand-kit-ci-success",
                    "workflow_uid": "uid-success",
                    "phase": "Succeeded",
                    "finished_at": "2026-09-27T12:45:00Z",
                }
            ],
        },
    )

    assert brand_kit_ci_failure_watch.main(
        [
            "--report",
            str(report_path),
            "--attestations",
            str(attestations_path),
            "--watcher-workflow-uid",
            "watcher-uid",
        ]
    ) == 0

    durable = json.loads(attestations_path.read_text(encoding="utf-8"))
    assert durable == {
        "schema": brand_kit_ci_failure_watch.ATTESTATION_SCHEMA,
        "watcher_workflow_uid": "watcher-uid",
        "attestations": [
            {
                "commit": "a" * 40,
                "workflow_name": "brand-kit-ci-success",
                "workflow_uid": "uid-success",
                "phase": "Succeeded",
                "finished_at": "2026-09-27T12:45:00Z",
            }
        ],
    }


def test_run_watch_covers_failures_at_the_argo_retention_boundary():
    retention = brand_kit_ci_failure_watch.ARGO_FAILURE_RETENTION_SECONDS
    boundary = (NOW - timedelta(seconds=retention)).isoformat().replace("+00:00", "Z")
    just_outside = (NOW - timedelta(seconds=retention + 1)).isoformat().replace(
        "+00:00", "Z"
    )
    report = brand_kit_ci_failure_watch.run_watch(
        "argo-secret",
        now=NOW,
        request=lambda *args, **kwargs: {
            "items": [
                workflow("brand-kit-ci-failed-boundary", "Failed", boundary),
                workflow("brand-kit-ci-error-boundary", "Error", boundary),
                workflow("brand-kit-ci-failed-old", "Failed", just_outside),
                workflow("brand-kit-ci-error-old", "Error", just_outside),
                workflow("brand-kit-ci-success", "Succeeded", "2026-09-27T12:59:00Z"),
            ]
        },
    )

    assert [failure["name"] for failure in report["failures"]] == [
        "brand-kit-ci-failed-boundary",
        "brand-kit-ci-error-boundary",
    ]


@pytest.mark.parametrize("lookback_minutes", (1, 119))
def test_run_watch_rejects_a_lookback_shorter_than_argo_retention(lookback_minutes):
    with pytest.raises(
        release_publish.ReleaseError,
        match="lookback minutes must cover Argo failure retention",
    ):
        brand_kit_ci_failure_watch.run_watch(
            "argo-secret",
            lookback_minutes=lookback_minutes,
            request=lambda *args, **kwargs: pytest.fail("Argo must not be queried"),
        )


def test_main_writes_a_sanitized_durable_failure_report(tmp_path, monkeypatch, capsys):
    token = "argo-watch-contract-secret"
    report_path = tmp_path / "failure-report.json"
    calls = []
    real_run_watch = brand_kit_ci_failure_watch.run_watch

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {
            "items": [
                workflow(
                    "brand-kit-ci-failed",
                    "Failed",
                    "2026-09-27T12:45:00Z",
                    commit="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
                )
            ]
        }

    def run_watch(token_arg, **kwargs):
        kwargs["now"] = NOW
        return real_run_watch(token_arg, request=request, **kwargs)

    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", token)
    monkeypatch.setattr(brand_kit_ci_failure_watch, "run_watch", run_watch)

    assert (
        brand_kit_ci_failure_watch.main(
            ["--report", str(report_path), "--argo-api-url", "https://argo.example"]
        )
        == 1
    )

    durable_report = json.loads(report_path.read_text(encoding="utf-8"))
    output = capsys.readouterr()
    assert durable_report["schema"] == brand_kit_ci_failure_watch.REPORT_SCHEMA
    assert durable_report["status"] == "fail"
    assert durable_report["failures"][0]["commit"] == "abcdef1234567890abcdef1234567890abcdef12"
    assert token not in report_path.read_text(encoding="utf-8")
    assert token not in output.out + output.err
    assert calls[0][2]["token"] == token
    assert calls[0][2]["authorization_scheme"] == "Bearer"


def test_main_persists_an_argo_api_error_without_exposing_the_token(
    tmp_path, monkeypatch, capsys
):
    token = "argo-api-contract-secret"
    report_path = tmp_path / "api-error-report.json"
    real_run_watch = brand_kit_ci_failure_watch.run_watch

    def run_watch(token_arg, **kwargs):
        return real_run_watch(
            token_arg,
            request=release_publish.request_json,
            **kwargs,
        )

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(f"Argo rejected credential {token}".encode()),
        )

    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", token)
    monkeypatch.setattr(brand_kit_ci_failure_watch, "run_watch", run_watch)
    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)

    assert (
        brand_kit_ci_failure_watch.main(["--report", str(report_path)])
        == 2
    )

    durable_report = json.loads(report_path.read_text(encoding="utf-8"))
    output = capsys.readouterr()
    assert durable_report["status"] == "incomplete"
    assert durable_report["pagination"]["error"]["code"] == "page_request_failed"
    assert durable_report["failures"] == []
    assert "Argo API" in durable_report["error"]
    assert "[REDACTED]" in durable_report["error"]
    assert token not in report_path.read_text(encoding="utf-8")
    assert token not in output.out + output.err


def test_main_returns_success_and_writes_pass_report_when_no_alert_is_needed(
    tmp_path, monkeypatch, capsys
):
    report_path = tmp_path / "pass-report.json"
    real_run_watch = brand_kit_ci_failure_watch.run_watch

    def run_watch(token_arg, **kwargs):
        return real_run_watch(
            token_arg,
            request=lambda *args, **kwargs: {"items": []},
            **kwargs,
        )

    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", "argo-pass-contract-secret")
    monkeypatch.setattr(brand_kit_ci_failure_watch, "run_watch", run_watch)

    assert brand_kit_ci_failure_watch.main(["--report", str(report_path)]) == 0

    durable_report = json.loads(report_path.read_text(encoding="utf-8"))
    output = capsys.readouterr()
    assert durable_report["status"] == "pass"
    assert durable_report["failures"] == []
    assert output.out == "PASS  no recent brand-kit-ci failures\n"
    assert output.err == ""


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
    assert "brand-kit-workflow-readonly" in workflow
    assert "key: token" in workflow
    assert "ARGO_WORKFLOW_TOKEN" in workflow
    assert "brand-kit-release-tokens" not in workflow
    assert "ARGO_TOKEN" not in workflow
    assert "tools/prune_ci_failure_watch_reports.py" in workflow
    assert "tools/prune_ci_attestations.py" in workflow
    assert "--retention-days 30" in workflow
    assert "--release-evidence-root /brand-kit/release-evidence/v1" in workflow
    assert 'brand-kit.ardenone.com/argo-failure-retention-seconds: "7200"' in workflow
    assert 'brand-kit.ardenone.com/failure-watch-lookback-minutes: "120"' in workflow
    assert "name: needle-ci-artifact-reader" in workflow
    assert "name: needle-ci-artifact-publisher" in workflow
    assert "artifactGC:\n              strategy: Never" in workflow
    assert "failures/brand-kit-ci-failure-watch/v1/{{workflow.uid}}/report.json" in workflow
    assert "attestations/brand-kit-ci/v1/{{workflow.uid}}/attestations.json" in workflow
    assert "brand-kit-ci-attestation/v1" in workflow
    assert '"watcher_workflow_uid": watcher_uid' in workflow
    assert "--watcher-workflow-uid \"{{workflow.uid}}\"" in workflow
    assert "tools/validate_ci_failure_watch_artifacts.py" in workflow
    assert "trap validate_watch_artifacts_on_exit EXIT" in workflow
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in workflow
    assert '"alertname": "BrandKitCIRegressionGate"' in workflow
    assert '"owner": "jedarden"' in workflow
    assert '"follow_up": "regression-gate"' in workflow
    assert '    - "*/15 * * * *"' in cron
    assert "name: brand-kit-ci-failure-watch" in cron
    assert "BrandKitCIRegressionGate" in readme
    assert "brand-kit-ci-failure-watch" in readme
    assert "BrandKitCIRegressionGate" in note
    assert "brand-kit-automation-application.yml" in note
    assert "brand-kit-automation-iad-ci" in note
    assert "check_ci_failure_watch_parity.py" in note
    assert "traefik-iad-ci:8001" in note
    assert "argo-ci.ardenone.com" in note
    assert "30 days" in note
    assert "15-minute schedule" in note
    assert "may return `404`" in note


def test_exit_handler_alert_contract_links_the_durable_report_without_credentials():
    workflow = Path(
        "automation/brand-kit-ci-failure-watch-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    notify_owner = workflow.split("    - name: notify-owner\n", 1)[1]

    assert 'when: "{{workflow.status}} != Succeeded"' in workflow
    assert (
        'REPORT_URL="https://s3.ardenone.com/needle-ci-artifacts/'
        'failures/brand-kit-ci-failure-watch/v1/{{workflow.uid}}/report.json"'
    ) in notify_owner
    assert '"report_url": "${REPORT_URL}"' in notify_owner
    assert 'http://alertmanager.monitoring.svc:9093/api/v1/alerts' in notify_owner
    assert '"alertname": "BrandKitCIRegressionGate"' in notify_owner
    assert "ARGO_TOKEN" not in notify_owner
    assert "ARGO_SUBMIT_TOKEN" not in notify_owner
    assert "secretKeyRef" not in notify_owner
