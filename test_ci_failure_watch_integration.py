import html
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import textwrap
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import yaml

from tools import brand_kit_ci_failure_watch
from tools import prune_ci_failure_watch_reports


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
WATCHER_UID = "watcher-uid-20260927"
REPORT_PREFIX = "failures/brand-kit-ci-failure-watch/v1/"
REPORT_KEY = f"{REPORT_PREFIX}{WATCHER_UID}/report.json"
ARGO_TOKEN = "argo-fixture"
READER_ACCESS_KEY = "reader-id"
READER_SECRET_KEY = "reader-fixture"
PUBLISHER_ACCESS_KEY = "publisher-id"
PUBLISHER_SECRET_KEY = "publisher-fixture"
COMMIT = "a" * 40


def _workflow(name, phase, finished_at, *, message=None, commit=None):
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
            "message": message,
        },
    }
    if commit is not None:
        record["status"]["outputs"] = {
            "parameters": [{"name": "commit", "value": commit}]
        }
    return record


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class _Garage:
    """Small deterministic Garage HTTP double shared by list/delete calls."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.requests = []
        self.deleted = []

    def publish(self, key, body, last_modified):
        self.objects[key] = (last_modified, bytes(body))

    def __call__(self, request, timeout):
        assert timeout == 30
        self.requests.append(request)
        if request.method == "GET":
            query = parse_qs(urlsplit(request.full_url).query)
            prefix = query["prefix"][0]
            assert query["list-type"] == ["2"]
            contents = []
            for key, (last_modified, _body) in sorted(self.objects.items()):
                if not key.startswith(prefix):
                    continue
                timestamp = last_modified.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                )
                contents.append(
                    "<Contents>"
                    f"<Key>{html.escape(key)}</Key>"
                    f"<LastModified>{timestamp}</LastModified>"
                    "</Contents>"
                )
            body = (
                '<?xml version="1.0"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                + "".join(contents)
                + "<IsTruncated>false</IsTruncated></ListBucketResult>"
            )
            return _Response(body.encode())

        assert request.method == "POST"
        assert request.full_url.endswith("?delete=")
        root = ElementTree.fromstring(request.data)
        keys = [
            element.text
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "Key"
        ]
        assert all(isinstance(key, str) for key in keys)
        self.deleted.extend(keys)
        for key in keys:
            self.objects.pop(key, None)
        deleted = "".join(f"<Deleted><Key>{html.escape(key)}</Key></Deleted>" for key in keys)
        return _Response(
            (
                '<?xml version="1.0"?>'
                '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{deleted}</DeleteResult>"
            ).encode()
        )


def _alert_payload(template, workflow_status, report_url):
    endpoint = "http://alertmanager.monitoring.svc:9093/api/v1/alerts"
    match = re.search(
        r"--data-binary @-[ \t]*(?:\\\n[ \t]*)?"
        + re.escape(endpoint)
        + r" <<EOF\n(?P<body>.*?)\n\s*EOF",
        template,
        re.DOTALL,
    )
    assert match is not None
    body = textwrap.dedent(match.group("body"))
    body = body.replace("${REPORT_URL}", report_url)
    body = body.replace("{{workflow.status}}", workflow_status)
    payload = json.loads(body)
    assert len(payload) == 1
    return payload[0]


def test_failure_watch_flow_lists_publishes_prunes_and_alerts_without_leaking_credentials(
    tmp_path,
):
    workflows = [
        _workflow(
            "brand-kit-ci-recent-failed",
            "Failed",
            "2026-09-27T12:45:00Z",
            message=f"regression gate received {ARGO_TOKEN}",
            commit="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        ),
        _workflow(
            "brand-kit-ci-recent-error",
            "Error",
            "2026-09-27T12:30:00Z",
            message="controller error",
        ),
        _workflow(
            "brand-kit-ci-cutoff",
            "Failed",
            "2026-09-27T11:00:00Z",
            message="failure exactly at the two-hour cutoff",
        ),
        _workflow(
            "brand-kit-ci-too-old",
            "Error",
            "2026-09-27T10:59:59Z",
            message="outside the lookback",
        ),
        _workflow(
            "brand-kit-ci-success",
            "Succeeded",
            "2026-09-27T12:50:00Z",
            commit=COMMIT,
        ),
    ]
    argo_calls = []

    def request(method, url, **kwargs):
        argo_calls.append((method, url, kwargs))
        return {"items": workflows}

    report = brand_kit_ci_failure_watch.run_watch(
        ARGO_TOKEN,
        api_url="https://argo.example",
        now=NOW,
        request=request,
    )
    assert report["status"] == "fail"
    assert [failure["name"] for failure in report["failures"]] == [
        "brand-kit-ci-recent-failed",
        "brand-kit-ci-recent-error",
        "brand-kit-ci-cutoff",
    ]
    assert report["failures"][0]["message"] == "regression gate received [REDACTED]"
    assert report["attestations"] == [
        {
            "commit": COMMIT,
            "workflow_uid": "uid-brand-kit-ci-success",
            "phase": "Succeeded",
            "finished_at": "2026-09-27T12:50:00Z",
        }
    ]
    assert argo_calls[0][0] == "GET"
    assert "workflow-template%3Dbrand-kit-ci" in argo_calls[0][1]
    assert argo_calls[0][2]["token"] == ARGO_TOKEN
    assert argo_calls[0][2]["authorization_scheme"] == "Bearer"

    report_path = tmp_path / "brand-kit-ci-failure-report.json"
    brand_kit_ci_failure_watch.write_report(report, report_path)
    report_bytes = report_path.read_bytes()

    template_path = Path(
        "automation/brand-kit-ci-failure-watch-workflowtemplate.yml"
    )
    template_text = template_path.read_text(encoding="utf-8")
    manifest = yaml.safe_load(template_text)
    templates = {template["name"]: template for template in manifest["spec"]["templates"]}
    watch = templates["watch"]
    report_artifact = next(
        artifact
        for artifact in watch["outputs"]["artifacts"]
        if artifact["name"] == "brand-kit-ci-failure-report"
    )
    assert report_artifact["path"] == "/tmp/brand-kit-ci-failure-report.json"
    assert report_artifact["s3"]["key"] == (
        "failures/brand-kit-ci-failure-watch/v1/{{workflow.uid}}/report.json"
    )
    assert report_artifact["s3"]["accessKeySecret"] == {
        "name": "needle-ci-artifact-publisher",
        "key": "access-key",
    }
    assert report_artifact["s3"]["secretKeySecret"] == {
        "name": "needle-ci-artifact-publisher",
        "key": "secret-key",
    }
    report_key = report_artifact["s3"]["key"].replace("{{workflow.uid}}", WATCHER_UID)
    assert report_key == REPORT_KEY

    old_key = f"{REPORT_PREFIX}old/report.json"
    cutoff_key = f"{REPORT_PREFIX}cutoff/report.json"
    new_key = f"{REPORT_PREFIX}new/report.json"
    old_attestation_key = f"{REPORT_PREFIX}old/attestations.json"
    unrelated_key = "failures/brand-kit-consumer-drift/v1/old/report.json"
    garage = _Garage(
        {
            old_key: (datetime(2026, 8, 27, 12, 59, 59, tzinfo=timezone.utc), b"old"),
            cutoff_key: (datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc), b"boundary"),
            new_key: (datetime(2026, 9, 27, 12, 59, 59, tzinfo=timezone.utc), b"new"),
            old_attestation_key: (
                datetime(2026, 8, 27, 12, 59, 59, tzinfo=timezone.utc),
                b"attestation",
            ),
            unrelated_key: (datetime(2026, 8, 27, 12, 59, 59, tzinfo=timezone.utc), b"other"),
        }
    )
    garage.publish(report_key, report_bytes, NOW)

    reader = prune_ci_failure_watch_reports.S3Client(
        "https://s3.example.test",
        "needle-ci-artifacts",
        prune_ci_failure_watch_reports.S3Credentials(
            READER_ACCESS_KEY, READER_SECRET_KEY
        ),
        opener=garage,
        now=NOW,
    )
    publisher = prune_ci_failure_watch_reports.S3Client(
        "https://s3.example.test",
        "needle-ci-artifacts",
        prune_ci_failure_watch_reports.S3Credentials(
            PUBLISHER_ACCESS_KEY, PUBLISHER_SECRET_KEY
        ),
        opener=garage,
        now=NOW,
    )
    pruned, cutoff = prune_ci_failure_watch_reports.prune_reports(
        reader,
        publisher,
        now=NOW,
    )
    assert pruned == 1
    assert cutoff == datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    assert garage.deleted == [old_key]
    assert old_key not in garage.objects
    assert cutoff_key in garage.objects
    assert new_key in garage.objects
    assert old_attestation_key in garage.objects
    assert unrelated_key in garage.objects
    assert json.loads(garage.objects[report_key][1]) == report

    list_request, delete_request = garage.requests
    assert list_request.method == "GET"
    assert parse_qs(urlsplit(list_request.full_url).query) == {
        "list-type": ["2"],
        "prefix": [REPORT_PREFIX],
    }
    assert f"Credential={READER_ACCESS_KEY}/" in list_request.headers["Authorization"]
    assert f"Credential={PUBLISHER_ACCESS_KEY}/" not in list_request.headers["Authorization"]
    assert delete_request.method == "POST"
    assert f"Credential={PUBLISHER_ACCESS_KEY}/" in delete_request.headers["Authorization"]
    assert f"Credential={READER_ACCESS_KEY}/" not in delete_request.headers["Authorization"]
    assert old_key.encode() in delete_request.data
    assert cutoff_key.encode() not in delete_request.data

    notify_owner = templates["notify-owner"]["container"]["args"][0]
    report_url = (
        "https://s3.ardenone.com/needle-ci-artifacts/" + report_key
    )
    alert = _alert_payload(notify_owner, "Failed", report_url)
    assert alert["labels"] == {
        "alertname": "BrandKitCIRegressionGate",
        "owner": "jedarden",
        "component": "brand-kit-ci",
        "follow_up": "regression-gate",
        "bucket": "brand-kit",
        "workflow_status": "Failed",
    }
    assert alert["annotations"]["report_url"] == report_url
    assert alert["annotations"]["summary"] == "brand-kit-ci regression gate needs follow-up"
    assert "alertmanager.monitoring.svc:9093/api/v1/alerts" in notify_owner
    assert "secretKeyRef" not in notify_owner

    evidence = "\n".join(
        [
            report_bytes.decode(),
            json.dumps(alert),
            delete_request.data.decode(),
            notify_owner,
        ]
    )
    for secret in (
        ARGO_TOKEN,
        READER_SECRET_KEY,
        PUBLISHER_SECRET_KEY,
    ):
        assert secret not in evidence
    assert READER_SECRET_KEY not in delete_request.headers["Authorization"]
    assert PUBLISHER_SECRET_KEY not in list_request.headers["Authorization"]
