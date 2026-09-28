from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import subprocess
import urllib.error

import pytest
from jsonschema import Draft202012Validator, FormatChecker
import yaml

from test_consumer_drift import (
    invoke_main,
    make_config,
    make_fetcher,
    make_root,
    make_site,
)
from tools import consumer_drift_report


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "consumer-drift-report.schema.json"
WORKFLOW_PATH = ROOT / "automation/brand-kit-consumer-drift-workflowtemplate.yml"


@pytest.fixture(scope="module")
def report_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def assert_schema_conformant(report, report_validator: Draft202012Validator) -> None:
    errors = sorted(report_validator.iter_errors(report), key=lambda error: list(error.path))
    assert not errors, "\n".join(
        f"{'.'.join(str(part) for part in error.path)}: {error.message}"
        for error in errors
    )
    assert consumer_drift_report.validate_report(report) is report


@pytest.mark.parametrize(
    ("outcome", "expected_report_status", "expected_check_status"),
    [
        ("current", "current", "current"),
        ("stale", "stale", "stale"),
        ("unavailable", "indeterminate", "unavailable"),
        ("skipped", "indeterminate", "skipped"),
    ],
)
def test_direct_detector_outcomes_conform_to_the_published_schema(
    outcome,
    expected_report_status,
    expected_check_status,
    tmp_path,
    monkeypatch,
    capsys,
    report_validator,
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    fetcher = make_fetcher(root)
    extra_args = ()

    if outcome == "stale":
        (site / "public/brand/logo.svg").write_bytes(b"stale consumer copy")
    elif outcome == "unavailable":
        base_fetcher = fetcher

        def fetcher(url, headers=None):
            if "avatars.example" in url:
                raise urllib.error.URLError("avatar service unavailable")
            return base_fetcher(url, headers)

    elif outcome == "skipped":
        extra_args = ("--offline",)

    exit_code, report = invoke_main(
        monkeypatch,
        capsys,
        root,
        site,
        fetcher,
        extra_args=extra_args,
        config=make_config(),
    )

    expected_exit = {"current": 0, "stale": 1, "unavailable": 2, "skipped": 2}[outcome]
    assert exit_code == expected_exit
    assert report["status"] == expected_report_status
    assert_schema_conformant(report, report_validator)

    check_statuses = {check["status"] for check in report["checks"]}
    assert expected_check_status in check_statuses
    if outcome == "skipped":
        assert all(
            check["status"] == "skipped"
            for check in report["checks"]
            if "url" in check
        )


def test_direct_release_setup_failure_report_is_schema_conformant(
    tmp_path, monkeypatch, capsys, report_validator
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)

    def fetcher(url, headers=None):
        raise urllib.error.URLError("forgejo unavailable")

    exit_code, report = invoke_main(
        monkeypatch, capsys, root, site, fetcher, config=make_config()
    )

    assert exit_code == 2
    assert report["status"] == "indeterminate"
    assert report["release"]["checkout"] is None
    assert report["checks"] == []
    assert any("cannot read release" in error for error in report["errors"])
    assert_schema_conformant(report, report_validator)


def test_status_semantics_reject_a_schema_valid_but_misleading_report(
    tmp_path, monkeypatch, capsys, report_validator
):
    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    _, current = invoke_main(
        monkeypatch, capsys, root, site, make_fetcher(root), config=make_config()
    )

    invalid = copy.deepcopy(current)
    invalid["checks"][0]["status"] = "skipped"
    invalid["checks"][0]["reason"] = "offline mode was requested"
    invalid["consumers"][invalid["checks"][0]["consumer"]]["status"] = "skipped"
    invalid["status"] = "current"

    # The JSON Schema describes the allowed shape and enums; the producer's
    # status rule supplies the meaning that a current report cannot contain a
    # skipped or unavailable check.
    report_validator.validate(invalid)
    with pytest.raises(consumer_drift_report.ReportError, match="expected 'indeterminate'"):
        consumer_drift_report.validate_report(invalid)


def _workflow_shell() -> str:
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return document["spec"]["templates"][0]["container"]["args"][0]


@pytest.mark.parametrize(
    "message",
    [
        "workflow did not reach the detector",
        "consumer-drift report retention cleanup did not complete",
        "detector produced an invalid report",
    ],
)
def test_scheduled_workflow_fallback_artifacts_conform_to_the_published_schema(
    message, tmp_path, report_validator
):
    shell = _workflow_shell()
    function_start = shell.index("write_fallback_report() {")
    first_call = shell.index('write_fallback_report "workflow did not reach the detector"')
    fallback_function = shell[function_start:first_call]
    report_path = tmp_path / "consumer-drift-report.json"
    fallback_function = fallback_function.replace(
        "/tmp/consumer-drift-report.json", str(report_path)
    )
    script = (
        "set -euo pipefail\n"
        + fallback_function
        + f"\nwrite_fallback_report {shlex.quote(message)}\n"
    )

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "indeterminate"
    assert report["release"] == {
        "tag": None,
        "published_at": None,
        "record_target": None,
        "checkout": None,
    }
    assert report["errors"] == [message]
    assert_schema_conformant(report, report_validator)


def test_scheduled_workflow_validates_the_exact_uploaded_detector_artifact():
    shell = _workflow_shell()

    detector = "python3 /brand-kit/tools/consumer_drift.py"
    validator = "python3 /brand-kit/tools/validate_consumer_drift_report.py"
    assert "--report /tmp/consumer-drift-report.json" in shell
    assert shell.count(validator) >= 3
    detector_index = shell.index(detector)
    assert detector_index < shell.index(validator, detector_index)
    assert "path: /tmp/consumer-drift-report.json" in WORKFLOW_PATH.read_text(
        encoding="utf-8"
    )
