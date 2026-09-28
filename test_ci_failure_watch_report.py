import copy
import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker
import yaml

from tools import brand_kit_ci_failure_watch
from tools import ci_failure_watch_report


ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "tests/fixtures/ci-failure-watch"
REPORT_SCHEMA_PATH = ROOT / "ci-failure-watch-report.schema.json"
ATTESTATION_SCHEMA_PATH = ROOT / "ci-attestations.schema.json"


def _fixture(name):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report_validator():
    schema = _fixture_schema(REPORT_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture(scope="module")
def attestations_validator():
    schema = _fixture_schema(ATTESTATION_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _fixture_schema(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    (
        "pass-report.json",
        "fail-report.json",
        "incomplete-pagination-report.json",
        "preflight-error-report.json",
    ),
)
def test_versioned_report_fixtures_satisfy_json_schema_and_runtime_contract(
    name, report_validator
):
    report = _fixture(name)
    assert list(report_validator.iter_errors(report)) == []
    assert ci_failure_watch_report.validate_report(report) is report


def test_durable_attestation_fixture_satisfies_both_contracts(attestations_validator):
    document = _fixture("attestations.json")
    assert list(attestations_validator.iter_errors(document)) == []
    assert ci_failure_watch_report.validate_attestations(document) is document


@pytest.mark.parametrize(
    ("mutate", "reason", "schema_rejects"),
    (
        (
            lambda report: report["failures"].append(
                copy.deepcopy(_fixture("fail-report.json")["failures"][0])
            ),
            "discard partial evidence",
            True,
        ),
        (
            lambda report: report.update(
                cutoff={**report["cutoff"], "at": "2026-09-27T10:59:00Z"}
            ),
            "cutoff.at must match",
            False,
        ),
        (
            lambda report: report.update(
                attestations_url=report["attestations_url"] + "?token=unsafe"
            ),
            "attestations_url",
            True,
        ),
        (
            lambda report: report.update(
                observed_at="2026-09-27T13:00:00+00:00"
            ),
            "UTC ISO-8601",
            True,
        ),
    ),
)
def test_incomplete_or_misleading_results_fail_closed(
    mutate, reason, schema_rejects, report_validator
):
    report = _fixture("incomplete-pagination-report.json")
    mutate(report)

    with pytest.raises(ci_failure_watch_report.ReportContractError, match=reason):
        ci_failure_watch_report.validate_report(report)
    assert bool(list(report_validator.iter_errors(report))) is schema_rejects


@pytest.mark.parametrize(
    "mutate",
    (
        lambda document: document.update(watcher_workflow_uid=""),
        lambda document: document["attestations"][0].update(phase="Failed"),
        lambda document: document["attestations"][0].update(
            finished_at="2026-09-27T12:45:00"
        ),
        lambda document: document["attestations"][0].update(unexpected=True),
    ),
)
def test_malformed_attestation_fixtures_are_rejected(mutate, attestations_validator):
    document = _fixture("attestations.json")
    mutate(document)

    with pytest.raises(ci_failure_watch_report.ReportContractError):
        ci_failure_watch_report.validate_attestations(document)
    assert list(attestations_validator.iter_errors(document))


def test_report_writer_refuses_an_invalid_partial_snapshot(tmp_path):
    report = _fixture("incomplete-pagination-report.json")
    report["attestations"].append(_fixture("attestations.json")["attestations"][0])
    path = tmp_path / "report.json"

    with pytest.raises(ci_failure_watch_report.ReportContractError):
        brand_kit_ci_failure_watch.write_report(report, path)
    assert not path.exists()


def test_preupload_validator_checks_both_files_and_fails_closed(tmp_path):
    report_path = tmp_path / "report.json"
    attestations_path = tmp_path / "attestations.json"
    report_path.write_text(json.dumps(_fixture("pass-report.json")), encoding="utf-8")
    attestations_path.write_text(json.dumps(_fixture("attestations.json")), encoding="utf-8")

    valid = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/validate_ci_failure_watch_artifacts.py"),
            "--report",
            str(report_path),
            "--attestations",
            str(attestations_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr

    report_path.write_text(
        json.dumps(_fixture("incomplete-pagination-report.json") | {"complete": True}),
        encoding="utf-8",
    )
    invalid = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/validate_ci_failure_watch_artifacts.py"),
            "--report",
            str(report_path),
            "--attestations",
            str(attestations_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 1
    assert "complete must match" in invalid.stderr


def test_main_persists_incomplete_pagination_without_partial_results(
    tmp_path, monkeypatch, capsys, report_validator, attestations_validator
):
    report_path = tmp_path / "report.json"
    attestations_path = tmp_path / "attestations.json"
    token = "watcher-pagination-token"
    pages = iter(
        [
            {
                "items": [
                    {
                        "metadata": {
                            "name": "brand-kit-ci-failed",
                            "uid": "uid-brand-kit-ci-failed",
                            "creationTimestamp": "2026-09-27T12:30:00Z",
                        },
                        "status": {"phase": "Failed", "finishedAt": "2026-09-27T12:45:00Z"},
                    }
                ],
                "metadata": {"continue": "next-page"},
            },
            {"metadata": {"continue": "must-not-be-followed"}},
        ]
    )
    real_run_watch = brand_kit_ci_failure_watch.run_watch

    def request(*args, **kwargs):
        return next(pages)

    def run_watch(token_arg, **kwargs):
        return real_run_watch(token_arg, request=request, **kwargs)

    monkeypatch.setenv("ARGO_WORKFLOW_TOKEN", token)
    monkeypatch.setattr(brand_kit_ci_failure_watch, "run_watch", run_watch)
    exit_code = brand_kit_ci_failure_watch.main(
        [
            "--report",
            str(report_path),
            "--attestations",
            str(attestations_path),
            "--watcher-workflow-uid",
            "watcher-incomplete-case",
        ]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    attestations = json.loads(attestations_path.read_text(encoding="utf-8"))
    capsys.readouterr()

    assert exit_code == 2
    assert report["status"] == "incomplete"
    assert report["pagination"] == {
        "status": "incomplete",
        "pages_read": 1,
        "error": {"code": "missing_items", "page": 2},
    }
    assert report["failures"] == []
    assert report["attestations"] == []
    assert list(report_validator.iter_errors(report)) == []
    assert list(attestations_validator.iter_errors(attestations)) == []
    assert ci_failure_watch_report.validate_report(report) is report
    assert ci_failure_watch_report.validate_attestations(attestations) is attestations


def test_workflow_preflight_fallback_is_a_valid_error_artifact(tmp_path):
    workflow = yaml.safe_load(
        (ROOT / "automation/brand-kit-ci-failure-watch-workflowtemplate.yml").read_text(
            encoding="utf-8"
        )
    )
    script = workflow["spec"]["templates"][0]["container"]["args"][0]
    start = script.index("write_fallback_evidence() {")
    stop = script.index("\nwrite_fallback_evidence \\", start)
    function = script[start:stop].replace("{{workflow.uid}}", "fallback-watcher-uid")
    report_path = tmp_path / "fallback-report.json"
    attestations_path = tmp_path / "fallback-attestations.json"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + function
            + "\nwrite_fallback_evidence \"$1\" \"$2\" \"setup failed\"\n",
            "fallback-test",
            str(report_path),
            str(attestations_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    attestations = json.loads(attestations_path.read_text(encoding="utf-8"))
    assert ci_failure_watch_report.validate_report(report) is report
    assert ci_failure_watch_report.validate_attestations(attestations) is attestations


def test_watcher_success_and_failure_results_match_the_report_schema(report_validator):
    observed_at = datetime.fromisoformat("2026-09-27T13:00:00+00:00")
    reports = [
        brand_kit_ci_failure_watch.run_watch(
            "watcher-token",
            now=observed_at,
            request=lambda *args, **kwargs: {"items": []},
        ),
        brand_kit_ci_failure_watch.run_watch(
            "watcher-token",
            now=observed_at,
            request=lambda *args, **kwargs: {
                "items": [
                    {
                        "metadata": {
                            "name": "brand-kit-ci-failed",
                            "uid": "uid-brand-kit-ci-failed",
                            "creationTimestamp": "2026-09-27T12:30:00Z",
                        },
                        "status": {
                            "phase": "Failed",
                            "startedAt": "2026-09-27T12:31:00Z",
                            "finishedAt": "2026-09-27T12:45:00Z",
                            "message": "failure fixture",
                        },
                    }
                ]
            },
        )
    ]
    assert [report["status"] for report in reports] == ["pass", "fail"]
    for report in reports:
        assert list(report_validator.iter_errors(report)) == []
        assert ci_failure_watch_report.validate_report(report) is report
        assert report["pagination"] == {
            "status": "complete",
            "pages_read": 1,
            "error": None,
        }
