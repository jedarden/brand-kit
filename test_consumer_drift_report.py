from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess
import textwrap
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
from tools import consumer_drift, consumer_drift_report, prune_consumer_drift_reports
from tools import release_evidence
from tools.prune_ci_failure_watch_reports import S3Object


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "consumer-drift-report.schema.json"
WORKFLOW_PATH = ROOT / "automation/brand-kit-consumer-drift-workflowtemplate.yml"
CRONWORKFLOW_PATH = ROOT / "automation/brand-kit-consumer-drift-cronworkflow.yml"
ARTIFACT_ENDPOINT = "https://s3.example.test"
ARTIFACT_BUCKET = "needle-ci-artifacts"
ARTIFACT_PREFIX = "failures/brand-kit-consumer-drift/v1/"


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


class _DurableArtifactStore:
    """Small object-store double with the same surface used by the pruner."""

    def __init__(self, now: datetime):
        self.now = now
        self.objects = {}
        self.fail_delete = False

    def publish(self, key: str, content: bytes, modified: datetime | None = None) -> None:
        self.objects[key] = (bytes(content), modified or self.now)

    def read(self, key: str) -> bytes:
        return self.objects[key][0]

    def list_objects(self, prefix: str):
        return [
            S3Object(key, modified)
            for key, (_, modified) in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def delete_objects(self, keys: list[str]) -> None:
        if self.fail_delete:
            raise prune_consumer_drift_reports.RetentionError(
                "artifact delete response was unavailable"
            )
        for key in keys:
            self.objects.pop(key, None)


def _publish_and_reload_report(
    store: _DurableArtifactStore,
    key: str,
    report: dict,
    report_validator: Draft202012Validator,
) -> dict:
    serialized = json.dumps(report, sort_keys=True).encode("utf-8")
    store.publish(key, serialized)
    durable = json.loads(store.read(key).decode("utf-8"))
    assert_schema_conformant(durable, report_validator)
    return durable


def _write_workflow_fallback_report(message: str, report_path: Path) -> dict:
    """Run the fallback function copied from the checked-in WorkflowTemplate."""
    shell = _workflow_shell()
    function_start = shell.index("write_fallback_report() {")
    first_call = shell.index('write_fallback_report "workflow did not reach the detector"')
    fallback_function = shell[function_start:first_call].replace(
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
    return json.loads(report_path.read_text(encoding="utf-8"))


def _owner_alert(workflow_status: str, workflow_name: str, report_url: str) -> dict | None:
    """Render the exact Alertmanager heredoc from the WorkflowTemplate."""
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    templates = {template["name"]: template for template in document["spec"]["templates"]}
    route = templates["route-drift"]["steps"][0][0]
    assert route["when"] == '{{workflow.status}} != Succeeded'
    if workflow_status == "Succeeded":
        return None

    notify = templates["notify-owner"]["container"]["args"][0]
    match = re.search(r"<<EOF\n(?P<body>.*?)\n\s*EOF", notify, re.DOTALL)
    assert match is not None
    body = textwrap.dedent(match.group("body")).replace(
        "{{workflow.status}}", workflow_status
    ).replace("{{workflow.name}}", workflow_name).replace("${REPORT_URL}", report_url)
    payload = json.loads(body)
    assert len(payload) == 1
    return payload[0]


def _write_release_evidence_hold(evidence_root: Path, report_url: str) -> None:
    evidence_root.mkdir(parents=True)
    record = release_evidence.build_record(
        tag="v1.0.0",
        commit="a" * 40,
        ci_run="e2e-consumer-drift",
        ci_attestation_url=(
            "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
            "brand-kit-ci/v1/e2e-consumer-drift/attestations.json"
        ),
        ci_workflow_uid="e2e-consumer-drift",
        ci_finished_at="2026-09-27T17:00:00Z",
        forgejo_release_url=(
            "https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.0.0"
        ),
        consumer_workflow="brand-kit-consumer-drift-release-e2e",
        mirror_commit="a" * 40,
        consumer_status="passed",
        consumer_report_url=report_url,
    )
    (evidence_root / "v1.0.0.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


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


@pytest.mark.parametrize(
    ("trigger", "outcome"),
    [
        ("release", "current"),
        ("release", "stale"),
        ("daily", "current"),
        ("daily", "unavailable"),
    ],
)
def test_scheduled_consumer_drift_end_to_end_report_retention_and_owner_alert(
    trigger, outcome, tmp_path, report_validator, monkeypatch
):
    """Exercise both schedule inputs all the way to an owner-visible artifact."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    cron = yaml.safe_load(CRONWORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["kind"] == "WorkflowTemplate"
    assert cron["spec"]["workflowSpec"]["workflowTemplateRef"] == {
        "name": "brand-kit-consumer-drift"
    }
    templates = {template["name"]: template for template in workflow["spec"]["templates"]}
    artifact = templates["audit"]["container"]["outputs"]["artifacts"][0]
    assert artifact["artifactGC"] == {"strategy": "Never"}
    assert artifact["s3"]["key"] == f"{ARTIFACT_PREFIX}{{{{workflow.uid}}}}/report.json"

    root = make_root(tmp_path)
    site = make_site(root, tmp_path)
    config = make_config()
    base_fetcher = make_fetcher(root)
    mirror_repository = None
    mirror_checker = None

    if trigger == "release":
        release_tag = "v1.0.0"
        fetcher = base_fetcher
        if outcome == "stale":
            (site / "public/brand/logo.svg").write_bytes(b"stale consumer copy")
    else:
        release_tag = None
        mirror_repository = "https://github.example/brand-kit.git"
        config["release"]["minimum_age_hours"] = 24
        records = [
            {
                "tag_name": "v1.2.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-09-28T00:00:00Z",
            },
            {
                "tag_name": "v1.1.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-09-26T00:00:00Z",
            },
        ]

        def fetcher(url, headers=None):
            if url.endswith("/releases?limit=50"):
                return json.dumps(records).encode("utf-8")
            if outcome == "unavailable" and "avatars.example" in url:
                raise urllib.error.URLError("avatar service unavailable")
            return base_fetcher(url, headers)

        mirror_checker = lambda repository, tag: "a" * 40
        monkeypatch.setattr(consumer_drift, "mirror_tag_commit", mirror_checker)

    report = consumer_drift.run_audit(
        root=root,
        site=site,
        config=config,
        release_tag=release_tag,
        fetcher=fetcher,
        now=datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc),
        mirror_repository=mirror_repository,
    )

    expected_status = "indeterminate" if outcome == "unavailable" else outcome
    assert report["status"] == expected_status
    assert_schema_conformant(report, report_validator)
    if outcome == "unavailable":
        assert any(check["status"] == "unavailable" for check in report["checks"])
        assert report["status"] != "current"
        with pytest.raises(consumer_drift_report.ReportError, match="expected 'indeterminate'"):
            misleading = json.loads(json.dumps(report))
            misleading["status"] = "current"
            consumer_drift_report.validate_report(misleading)

    if trigger == "release":
        parameters = {
            parameter["name"]: parameter["value"]
            for parameter in workflow["spec"]["arguments"]["parameters"]
        }
        assert {"release-tag", "release-commit"} <= parameters.keys()
    else:
        parameters = {
            parameter["name"]: parameter["value"]
            for parameter in cron["spec"]["workflowSpec"]["arguments"]["parameters"]
        }
        assert parameters["release-tag"] == ""
        assert parameters["release-commit"] == ""
        assert report["release"]["tag"] == "v1.1.0"

    now = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
    store = _DurableArtifactStore(now)
    workflow_uid = f"e2e-{trigger}-{outcome}"
    report_key = f"{ARTIFACT_PREFIX}{workflow_uid}/report.json"
    report_url = f"{ARTIFACT_ENDPOINT}/{ARTIFACT_BUCKET}/{report_key}"
    durable = _publish_and_reload_report(store, report_key, report, report_validator)
    assert durable == report

    old_key = f"{ARTIFACT_PREFIX}expired/report.json"
    held_key = f"{ARTIFACT_PREFIX}held/report.json"
    old_time = now - timedelta(days=31)
    store.publish(old_key, b"expired", old_time)
    store.publish(held_key, b"held", old_time)
    evidence_root = tmp_path / "release-evidence" / "v1"
    _write_release_evidence_hold(
        evidence_root, f"{ARTIFACT_ENDPOINT}/{ARTIFACT_BUCKET}/{held_key}"
    )
    pruned, cutoff = prune_consumer_drift_reports.prune_reports(
        store,
        store,
        evidence_root=evidence_root,
        endpoint=ARTIFACT_ENDPOINT,
        bucket=ARTIFACT_BUCKET,
        prefix=ARTIFACT_PREFIX,
        now=now,
    )
    assert pruned == 1
    assert cutoff == now - timedelta(days=30)
    assert old_key not in store.objects
    assert held_key in store.objects
    assert report_key in store.objects

    workflow_status = "Succeeded" if expected_status == "current" else "Failed"
    alert = _owner_alert(workflow_status, workflow_uid, report_url)
    if expected_status == "current":
        assert alert is None
    else:
        assert alert is not None
        assert alert["labels"] == {
            "alertname": "BrandKitConsumerDrift",
            "owner": "jedarden",
            "component": "consumer-drift",
            "bucket": "brand-kit",
            "workflow_status": "Failed",
        }
        assert alert["annotations"]["report_url"] == report_url


def test_scheduled_consumer_drift_retention_failure_uses_indeterminate_fallback_artifact_and_alert(
    tmp_path, report_validator
):
    now = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
    store = _DurableArtifactStore(now)
    expired_key = f"{ARTIFACT_PREFIX}retention-failure/report.json"
    store.publish(expired_key, b"expired", now - timedelta(days=31))
    evidence_root = tmp_path / "release-evidence" / "v1"
    evidence_root.mkdir(parents=True)
    store.fail_delete = True

    with pytest.raises(prune_consumer_drift_reports.RetentionError, match="delete"):
        prune_consumer_drift_reports.prune_reports(
            store,
            store,
            evidence_root=evidence_root,
            endpoint=ARTIFACT_ENDPOINT,
            bucket=ARTIFACT_BUCKET,
            prefix=ARTIFACT_PREFIX,
            now=now,
        )

    fallback_path = tmp_path / "consumer-drift-report.json"
    report = _write_workflow_fallback_report(
        "consumer-drift report retention cleanup did not complete", fallback_path
    )
    assert report["status"] == "indeterminate"
    assert report["errors"] == [
        "consumer-drift report retention cleanup did not complete"
    ]
    assert_schema_conformant(report, report_validator)

    store.fail_delete = False
    workflow_uid = "e2e-retention-failure"
    report_key = f"{ARTIFACT_PREFIX}{workflow_uid}/report.json"
    report_url = f"{ARTIFACT_ENDPOINT}/{ARTIFACT_BUCKET}/{report_key}"
    _publish_and_reload_report(store, report_key, report, report_validator)
    alert = _owner_alert("Failed", workflow_uid, report_url)
    assert alert is not None
    assert alert["labels"]["owner"] == "jedarden"
    assert alert["annotations"]["report_url"] == report_url
    assert expired_key in store.objects
