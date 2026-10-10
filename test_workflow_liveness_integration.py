import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess

import yaml

from tools import brand_kit_workflow_liveness


ROOT = Path(__file__).parent
ALERTMANAGER_ENDPOINT = "http://alertmanager.monitoring.svc:9093/api/v2/alerts"
LIVENESS_POLL_MINUTES = 15


def _workflow(name, finished_at):
    timestamp = finished_at.isoformat().replace("+00:00", "Z")
    return {
        "metadata": {"name": name, "uid": f"uid-{name}"},
        "status": {
            "phase": "Succeeded",
            "startedAt": timestamp,
            "finishedAt": timestamp,
        },
    }


def _workflow_records(now, last_successes):
    return {
        target["workflow_template"]: [
            _workflow(target["name"], last_successes[target["workflow_template"]])
        ]
        for target in brand_kit_workflow_liveness.TARGETS
    }


def _run_watchdog(now, last_successes, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    records = _workflow_records(now, last_successes)
    requests = []

    def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["authorization_scheme"] == "Bearer"
        requests.append(url)
        for workflow_template, items in records.items():
            if workflow_template in url:
                return {"items": items}
        raise AssertionError(f"unexpected Argo workflow list URL: {url}")

    report = brand_kit_workflow_liveness.run_liveness(
        "test-readonly-token",
        api_url="https://argo.example",
        now=now,
        request=request,
    )
    report_path = tmp_path / "liveness-report.json"
    brand_kit_workflow_liveness.write_report(report, report_path)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert len(requests) == len(brand_kit_workflow_liveness.TARGETS)
    return report, report_path


def _workflow_manifest():
    path = ROOT / "automation" / "brand-kit-workflow-liveness-workflowtemplate.yml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        template["name"]: template
        for template in manifest["spec"]["templates"]
    }


def _dispatch_on_exit(report, workflow_uid, tmp_path):
    templates = _workflow_manifest()
    route = templates["route-liveness"]["steps"][0][0]
    notify_script = templates["notify-owner"]["container"]["args"][0]
    workflow_status = "Succeeded" if report["status"] == "fresh" else "Failed"

    assert route["when"] == "{{workflow.status}} != Succeeded"
    if workflow_status == "Succeeded":
        return []

    script = notify_script.replace("{{workflow.status}}", workflow_status)
    script = script.replace("{{workflow.uid}}", workflow_uid)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    capture_path = tmp_path / "alertmanager-request.json"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!" + os.sys.executable + "\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['ALERT_CAPTURE']).write_text(\n"
        "    json.dumps({'args': sys.argv[1:], 'body': sys.stdin.read()}),\n"
        "    encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["ALERT_CAPTURE"] = str(capture_path)
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    args = captured["args"]
    assert "--fail" in args
    assert "Content-Type: application/json" in args
    assert args[-1] == ALERTMANAGER_ENDPOINT

    payload_match = re.search(r"\[\s*\{.*?\}\s*\]", captured["body"], re.DOTALL)
    assert payload_match is not None
    payload = json.loads(payload_match.group(0))
    assert len(payload) == 1
    return payload


def _fresh_successes(now):
    return {
        target["workflow_template"]: now - timedelta(minutes=5)
        for target in brand_kit_workflow_liveness.TARGETS
    }


def _next_liveness_tick(value):
    rounded_minute = value.minute - value.minute % LIVENESS_POLL_MINUTES
    tick = value.replace(minute=rounded_minute, second=0, microsecond=0)
    if tick <= value:
        tick += timedelta(minutes=LIVENESS_POLL_MINUTES)
    return tick


def _target(name):
    return next(
        target
        for target in brand_kit_workflow_liveness.TARGETS
        if target["workflow_template"] == name
    )


def _assert_owner_alert(alerts, report, workflow_uid):
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["labels"] == {
        "alertname": "BrandKitWorkflowLiveness",
        "owner": "jedarden",
        "component": "brand-kit-workflow-liveness",
        "follow_up": "workflow-liveness",
        "bucket": "brand-kit",
        "workflow_status": "Failed",
    }
    expected_report_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/failures/"
        f"brand-kit-workflow-liveness/v1/{workflow_uid}/report.json"
    )
    assert alert["annotations"]["report_url"] == expected_report_url
    assert alert["annotations"]["summary"] == (
        "brand-kit scheduled workflow liveness needs follow-up"
    )
    assert "stale" in alert["annotations"]["description"]
    assert report["status"] == "stale"


def test_missed_scheduled_workflows_are_detected_on_the_next_liveness_tick(
    tmp_path,
):
    liveness_cron = yaml.safe_load(
        (ROOT / "automation" / "brand-kit-workflow-liveness-cronworkflow.yml").read_text(
            encoding="utf-8"
        )
    )
    assert liveness_cron["spec"]["schedules"] == ["*/15 * * * *"]
    assert liveness_cron["spec"]["timezone"] == "UTC"

    scenarios = (
        {
            "template": "brand-kit-ci-failure-watch",
            "last_success": datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
            "missed_scheduled_runs": (
                "2026-09-27T12:15:00Z",
                "2026-09-27T12:30:00Z",
                "2026-09-27T12:45:00Z",
                "2026-09-27T13:00:00Z",
                "2026-09-27T13:15:00Z",
            ),
        },
        {
            "template": "brand-kit-consumer-drift",
            "last_success": datetime(2026, 9, 27, 6, 17, tzinfo=timezone.utc),
            "missed_scheduled_runs": (
                "2026-09-28T06:17:00Z",
                "2026-09-29T06:17:00Z",
            ),
        },
        {
            "template": "brand-kit-mirror-health",
            "last_success": datetime(2026, 9, 27, 0, 17, tzinfo=timezone.utc),
            "missed_scheduled_runs": (
                "2026-09-27T06:17:00Z",
                "2026-09-27T12:17:00Z",
            ),
        },
        {
            "template": "brand-kit-platform-requirements",
            "last_success": datetime(2026, 9, 27, 6, 27, tzinfo=timezone.utc),
            "missed_scheduled_runs": (
                "2026-09-28T06:27:00Z",
                "2026-09-29T06:27:00Z",
            ),
        },
        {
            "template": "brand-kit-release-token-probe",
            "last_success": datetime(2026, 9, 27, 6, 7, tzinfo=timezone.utc),
            "missed_scheduled_runs": (
                "2026-09-28T06:07:00Z",
                "2026-09-29T06:07:00Z",
            ),
        },
    )

    for index, scenario in enumerate(scenarios):
        target = _target(scenario["template"])
        threshold = scenario["last_success"] + timedelta(
            minutes=target["max_age_minutes"]
        )
        detection_tick = _next_liveness_tick(threshold)
        before_threshold = detection_tick - timedelta(minutes=LIVENESS_POLL_MINUTES)
        assert threshold <= detection_tick <= threshold + timedelta(
            minutes=LIVENESS_POLL_MINUTES
        )

        before_successes = _fresh_successes(before_threshold)
        before_successes[scenario["template"]] = scenario["last_success"]
        fresh_report, _ = _run_watchdog(
            before_threshold, before_successes, tmp_path / f"fresh-{index}"
        )
        assert fresh_report["status"] == "fresh"
        assert _dispatch_on_exit(
            fresh_report, f"liveness-fresh-{index}", tmp_path / f"route-fresh-{index}"
        ) == []

        outage_successes = _fresh_successes(detection_tick)
        outage_successes[scenario["template"]] = scenario["last_success"]
        stale_report, report_path = _run_watchdog(
            detection_tick, outage_successes, tmp_path / f"stale-{index}"
        )
        checks = {check["workflow_template"]: check for check in stale_report["checks"]}
        assert checks[scenario["template"]]["status"] == "stale"
        assert {
            name for name, check in checks.items() if check["status"] == "stale"
        } == {scenario["template"]}
        assert checks[scenario["template"]]["age_minutes"] <= (
            target["max_age_minutes"] + LIVENESS_POLL_MINUTES
        )
        assert json.loads(report_path.read_text(encoding="utf-8"))["checks"] == (
            stale_report["checks"]
        )

        records = _workflow_records(detection_tick, outage_successes)
        observed_successes = {
            item["status"]["finishedAt"]
            for item in records[scenario["template"]]
        }
        assert all(slot not in observed_successes for slot in scenario["missed_scheduled_runs"])

        alerts = _dispatch_on_exit(
            stale_report, f"liveness-outage-{index}", tmp_path / f"route-stale-{index}"
        )
        _assert_owner_alert(alerts, stale_report, f"liveness-outage-{index}")


def test_simultaneous_watcher_and_consumer_outages_send_one_liveness_alert(tmp_path):
    now = datetime(2026, 9, 29, 6, 30, tzinfo=timezone.utc)
    last_successes = _fresh_successes(now)
    last_successes["brand-kit-ci-failure-watch"] = datetime(
        2026, 9, 29, 5, 15, tzinfo=timezone.utc
    )
    last_successes["brand-kit-consumer-drift"] = datetime(
        2026, 9, 27, 6, 17, tzinfo=timezone.utc
    )

    report, _ = _run_watchdog(now, last_successes, tmp_path / "simultaneous")
    stale = {
        check["workflow_template"]
        for check in report["checks"]
        if check["status"] == "stale"
    }
    assert stale == {
        "brand-kit-ci-failure-watch",
        "brand-kit-consumer-drift",
    }
    assert next(
        check
        for check in report["checks"]
        if check["workflow_template"] == "brand-kit-mirror-health"
    )["status"] == "fresh"

    alerts = _dispatch_on_exit(report, "liveness-both-missed", tmp_path / "route-both")
    _assert_owner_alert(alerts, report, "liveness-both-missed")
