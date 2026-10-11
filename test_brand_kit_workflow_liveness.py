import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tools import brand_kit_workflow_liveness
from tools import prune_ci_failure_watch_reports as s3_reports


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
SUCCESS = {
    target["name"]: target["success_statuses"][0]
    for target in brand_kit_workflow_liveness.TARGETS
}


def _target(name):
    return next(
        target for target in brand_kit_workflow_liveness.TARGETS if target["name"] == name
    )


class FakeReader:
    """In-memory stand-in for the Garage report bucket."""

    def __init__(self, reports=(), *, fail_list=(), fail_get=()):
        self.objects = {}
        self.fail_list = set(fail_list)
        self.fail_get = set(fail_get)
        self.reads = []
        for name, uid, status, minutes_ago, *schema in reports:
            self.add(name, uid, status, NOW - timedelta(minutes=minutes_ago), *schema)

    def add(self, name, uid, status, modified, schema=None):
        target = _target(name)
        body = {"schema": schema or target["report_schema"], "status": status}
        key = f"{target['report_prefix']}{uid}/report.json"
        self.objects[key] = (json.dumps(body).encode("utf-8"), modified)

    def list_objects(self, prefix):
        if prefix in self.fail_list:
            raise s3_reports.RetentionError("Garage GET request failed with HTTP 403")
        return [
            s3_reports.S3Object(key=key, last_modified=modified)
            for key, (_, modified) in self.objects.items()
            if key.startswith(prefix)
        ]

    def get_object(self, key):
        self.reads.append(key)
        if key in self.fail_get:
            raise s3_reports.RetentionError("Garage GET request failed with HTTP 503")
        return self.objects[key][0]


def _all_fresh(minutes_ago=5):
    return [
        (target["name"], f"uid-{target['name']}", SUCCESS[target["name"]], minutes_ago)
        for target in brand_kit_workflow_liveness.TARGETS
    ]


def _checks(report):
    return {check["name"]: check for check in report["checks"]}


def test_liveness_is_fresh_when_all_targets_have_recent_successful_reports():
    report = brand_kit_workflow_liveness.run_liveness(FakeReader(_all_fresh()), now=NOW)

    assert report["schema"] == "brand-kit-workflow-liveness/v1"
    assert report["status"] == "fresh"
    assert report["observed_at"] == "2026-09-27T13:00:00Z"
    for check in report["checks"]:
        assert check["status"] == "fresh"
        assert check["last_success_at"] == "2026-09-27T12:55:00Z"
        assert check["age_minutes"] == 5.0


def test_each_target_is_judged_by_its_own_schema_and_success_status():
    expected = {
        "brand-kit-ci-failure-watch": ("brand-kit-ci-failure-watch/v1", "pass"),
        "brand-kit-consumer-drift": ("brand-kit-consumer-drift/v1", "current"),
        "brand-kit-mirror-health": ("brand-kit-mirror-health/v1", "healthy"),
        "brand-kit-platform-requirements": ("brand-kit-platform-requirements/v2", "pass"),
        "brand-kit-release-token-probe": ("brand-kit-release-token-probe/v1", "pass"),
    }
    assert {
        target["name"]: (target["report_schema"], target["success_statuses"][0])
        for target in brand_kit_workflow_liveness.TARGETS
    } == expected
    for target in brand_kit_workflow_liveness.TARGETS:
        version = target["report_schema"].rsplit("/", 1)[1]
        assert target["report_prefix"] == f"failures/{target['name']}/{version}/"


def test_a_suspended_or_unscheduled_target_without_reports_is_stale():
    reports = [item for item in _all_fresh() if item[0] != "brand-kit-consumer-drift"]

    report = brand_kit_workflow_liveness.run_liveness(FakeReader(reports), now=NOW)

    check = _checks(report)["brand-kit-consumer-drift"]
    assert report["status"] == "stale"
    assert check["status"] == "stale"
    assert check["last_success_at"] is None
    assert check["reason"].startswith(
        "no run report exists under failures/brand-kit-consumer-drift/v1/"
    )


@pytest.mark.parametrize("status", ("fail", "indeterminate", "error"))
def test_a_target_whose_recent_runs_all_failed_is_stale(status):
    reader = FakeReader(_all_fresh())
    reader.add(
        "brand-kit-platform-requirements",
        "uid-brand-kit-platform-requirements",
        status,
        NOW - timedelta(minutes=5),
    )

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    check = _checks(report)["brand-kit-platform-requirements"]
    assert report["status"] == "stale"
    assert check["status"] == "stale"
    assert f"(newest status: {status})" in check["reason"]
    assert all(
        other["status"] == "fresh"
        for name, other in _checks(report).items()
        if name != "brand-kit-platform-requirements"
    )


def test_latest_success_counts_even_when_newer_runs_failed():
    reader = FakeReader(
        [item for item in _all_fresh() if item[0] != "brand-kit-mirror-health"]
    )
    reader.add("brand-kit-mirror-health", "uid-new-1", "unhealthy", NOW - timedelta(minutes=10))
    reader.add("brand-kit-mirror-health", "uid-new-2", "unhealthy", NOW - timedelta(minutes=20))
    reader.add("brand-kit-mirror-health", "uid-old", "healthy", NOW - timedelta(minutes=40))

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    check = _checks(report)["brand-kit-mirror-health"]
    assert check["status"] == "fresh"
    assert check["last_success_at"] == "2026-09-27T12:20:00Z"
    assert [key for key in reader.reads if "mirror-health" in key] == [
        "failures/brand-kit-mirror-health/v1/uid-new-1/report.json",
        "failures/brand-kit-mirror-health/v1/uid-new-2/report.json",
        "failures/brand-kit-mirror-health/v1/uid-old/report.json",
    ]


def test_a_success_older_than_its_limit_is_stale_and_reports_its_age():
    reader = FakeReader(_all_fresh())
    target = _target("brand-kit-ci-failure-watch")
    key = f"{target['report_prefix']}uid-{target['name']}/report.json"
    reader.objects[key] = (reader.objects[key][0], NOW - timedelta(minutes=61))

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    check = _checks(report)["brand-kit-ci-failure-watch"]
    assert check["status"] == "stale"
    assert check["last_success_at"] == "2026-09-27T11:59:00Z"
    assert check["age_minutes"] == 61.0
    assert check["reason"] == (
        "latest successful run is 61.0 minutes old (limit 60; newest status: pass)"
    )


def test_a_report_with_another_schema_or_malformed_body_is_not_a_success():
    reader = FakeReader(_all_fresh())
    reader.add(
        "brand-kit-release-token-probe",
        "uid-brand-kit-release-token-probe",
        "pass",
        NOW - timedelta(minutes=5),
        schema="brand-kit-mirror-health/v1",
    )
    target = _target("brand-kit-ci-failure-watch")
    key = f"{target['report_prefix']}uid-{target['name']}/report.json"
    reader.objects[key] = (b"not json", reader.objects[key][1])

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    checks = _checks(report)
    assert checks["brand-kit-release-token-probe"]["status"] == "stale"
    assert "(newest status: unrecognized)" in checks["brand-kit-release-token-probe"]["reason"]
    assert checks["brand-kit-ci-failure-watch"]["status"] == "stale"


def test_keys_outside_the_report_layout_are_ignored():
    reader = FakeReader(_all_fresh())
    target = _target("brand-kit-consumer-drift")
    body = json.dumps({"schema": target["report_schema"], "status": "current"}).encode()
    reader.objects = {
        key: value
        for key, value in reader.objects.items()
        if not key.startswith(target["report_prefix"])
    }
    reader.objects[f"{target['report_prefix']}uid/nested/report.json"] = (body, NOW)
    reader.objects[f"{target['report_prefix']}uid/other.json"] = (body, NOW)

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    assert _checks(report)["brand-kit-consumer-drift"]["status"] == "stale"
    assert not [key for key in reader.reads if key.startswith(target["report_prefix"])]


def test_storage_errors_make_one_target_indeterminate_without_hiding_others():
    mirror = _target("brand-kit-mirror-health")
    reader = FakeReader(_all_fresh(), fail_list={mirror["report_prefix"]})

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    check = _checks(report)["brand-kit-mirror-health"]
    assert report["status"] == "indeterminate"
    assert check["status"] == "indeterminate"
    assert "HTTP 403" in check["reason"]

    probe = _target("brand-kit-release-token-probe")
    reader = FakeReader(
        _all_fresh(),
        fail_get={f"{probe['report_prefix']}uid-{probe['name']}/report.json"},
    )
    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)
    assert _checks(report)["brand-kit-release-token-probe"]["status"] == "indeterminate"

    reports = [item for item in _all_fresh() if item[0] != "brand-kit-consumer-drift"]
    reader = FakeReader(reports, fail_list={mirror["report_prefix"]})
    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)
    assert report["status"] == "stale"


def test_report_reads_per_target_are_bounded():
    reader = FakeReader(_all_fresh())
    for index in range(brand_kit_workflow_liveness.MAX_REPORT_READS_PER_TARGET + 5):
        reader.add(
            "brand-kit-ci-failure-watch",
            f"uid-failed-{index}",
            "fail",
            NOW - timedelta(seconds=index + 1),
        )

    report = brand_kit_workflow_liveness.run_liveness(reader, now=NOW)

    prefix = _target("brand-kit-ci-failure-watch")["report_prefix"]
    assert len([key for key in reader.reads if key.startswith(prefix)]) == (
        brand_kit_workflow_liveness.MAX_REPORT_READS_PER_TARGET
    )
    assert _checks(report)["brand-kit-ci-failure-watch"]["status"] == "stale"


def test_main_without_reader_credentials_writes_an_indeterminate_report(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("S3_READER_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_READER_SECRET_KEY", raising=False)
    report_path = tmp_path / "report.json"

    assert brand_kit_workflow_liveness.main(["--report", str(report_path)]) == 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "indeterminate"
    assert "S3_READER_ACCESS_KEY is required" in report["error"]
    assert "INDETERMINATE" in capsys.readouterr().err


def _load_manifest(name):
    return yaml.safe_load(
        Path("automation", name).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("workflow_name", "schedule"),
    (
        ("brand-kit-ci-failure-watch", "*/15 * * * *"),
        ("brand-kit-consumer-drift", "17 6 * * *"),
        ("brand-kit-mirror-health", "17 */6 * * *"),
        ("brand-kit-platform-requirements", "27 6 * * *"),
        ("brand-kit-release-token-probe", "7 6 * * *"),
        ("brand-kit-workflow-liveness", "*/15 * * * *"),
    ),
)
def test_monitored_cronworkflows_keep_the_expected_utc_schedules(
    workflow_name, schedule
):
    manifest = _load_manifest(f"{workflow_name}-cronworkflow.yml")

    assert manifest["kind"] == "CronWorkflow"
    assert manifest["metadata"]["name"] == workflow_name
    assert manifest["spec"]["schedules"] == [schedule]
    assert manifest["spec"]["timezone"] == "UTC"
    assert manifest["spec"]["workflowSpec"]["workflowTemplateRef"]["name"] == (
        workflow_name
    )


def test_liveness_workflow_contract_is_independent_and_retains_evidence():
    manifest = _load_manifest("brand-kit-workflow-liveness-workflowtemplate.yml")
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    check = templates["check"]

    assert manifest["kind"] == "WorkflowTemplate"
    assert manifest["metadata"]["name"] == "brand-kit-workflow-liveness"
    assert "tools/brand_kit_workflow_liveness.py" in check["container"]["args"][0]
    assert check["container"]["env"] == [
        {
            "name": "S3_READER_ACCESS_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "needle-ci-artifact-reader", "key": "access-key"}
            },
        },
        {
            "name": "S3_READER_SECRET_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "needle-ci-artifact-reader", "key": "secret-key"}
            },
        },
        {"name": "S3_ENDPOINT", "value": "https://s3.ardenone.com"},
        {"name": "S3_REGION", "value": "garage"},
    ]
    assert manifest["spec"]["serviceAccountName"] == "brand-kit-workflow-liveness"
    artifact = check["outputs"]["artifacts"][0]
    assert artifact["artifactGC"] == {"strategy": "Never"}
    assert artifact["s3"]["key"] == (
        "failures/brand-kit-workflow-liveness/v1/{{workflow.uid}}/report.json"
    )


def test_liveness_non_success_route_generates_owner_alert_with_durable_report():
    manifest = _load_manifest("brand-kit-workflow-liveness-workflowtemplate.yml")
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    route = templates["route-liveness"]["steps"][0][0]
    notify = templates["notify-owner"]["container"]["args"][0]

    assert route == {
        "name": "notify-owner",
        "template": "notify-owner",
        "when": "{{workflow.status}} != Succeeded",
        "continueOn": {"failed": True, "error": True},
    }
    assert "http://alertmanager.monitoring.svc:9093/api/v2/alerts" in notify
    assert "curl --fail --silent --show-error --retry 3 --retry-delay 5" in notify
    assert '"alertname": "BrandKitWorkflowLiveness"' in notify
    assert '"owner": "jedarden"' in notify
    assert '"component": "brand-kit-workflow-liveness"' in notify
    assert '"follow_up": "workflow-liveness"' in notify
    assert '"bucket": "brand-kit"' in notify
    assert '"workflow_status": "{{workflow.status}}"' in notify
    assert '"summary": "brand-kit scheduled workflow liveness needs follow-up"' in notify
    assert "restore the target CronWorkflow or WorkflowTemplate" in notify
    assert "REPORT_URL=\"https://s3.ardenone.com/needle-ci-artifacts/" in notify
    assert '"report_url": "${REPORT_URL}"' in notify
