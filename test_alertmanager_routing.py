import json
import re
import textwrap
from pathlib import Path


ALERTMANAGER_ENDPOINT = (
    "http://alertmanager.monitoring.svc:9093/api/v1/alerts"
)
ALERT_PAYLOAD_RE = re.compile(
    r"--data-binary @-[ \t]*(?:\\\n[ \t]*)?"
    + re.escape(ALERTMANAGER_ENDPOINT)
    + r" <<EOF\n(?P<body>.*?)\n\s*EOF",
    re.DOTALL,
)


EXPECTED_ALERTS = {
    "automation/brand-kit-ci-failure-watch-workflowtemplate.yml": {
        "alertname": "BrandKitCIRegressionGate",
        "owner": "jedarden",
        "component": "brand-kit-ci",
        "follow_up": "regression-gate",
        "bucket": "brand-kit",
        "workflow_status": "{{workflow.status}}",
    },
    "automation/brand-kit-consumer-drift-workflowtemplate.yml": {
        "alertname": "BrandKitConsumerDrift",
        "owner": "jedarden",
        "component": "consumer-drift",
        "bucket": "brand-kit",
        "workflow_status": "{{workflow.status}}",
    },
}


def alert_from_workflow(path):
    workflow = Path(path).read_text(encoding="utf-8")
    matches = list(ALERT_PAYLOAD_RE.finditer(workflow))
    assert len(matches) == 1, f"expected one Alertmanager payload in {path}"
    payload = json.loads(textwrap.dedent(matches[0].group("body")))
    assert len(payload) == 1
    return workflow, payload[0]


def test_failure_alert_payloads_have_stable_names_and_labels():
    for path, expected_labels in EXPECTED_ALERTS.items():
        workflow, alert = alert_from_workflow(path)

        assert alert["labels"] == expected_labels
        assert set(alert["annotations"]) == {"summary", "description", "report_url"}
        assert ALERTMANAGER_ENDPOINT in workflow
        assert "Content-Type: application/json" in workflow


def test_failure_alert_delivery_is_best_effort():
    for path in EXPECTED_ALERTS:
        workflow, _ = alert_from_workflow(path)

        assert 'when: "{{workflow.status}} != Succeeded"' in workflow
        assert "continueOn:\n              failed: true\n              error: true" in workflow
        assert "curl --fail --silent --show-error --retry 3 --retry-delay 5" in workflow


def test_documented_route_verification_covers_both_alerts_without_secrets():
    procedure = Path("docs/notes/asset-toolchain.md").read_text(encoding="utf-8")

    assert "## Alertmanager route verification" in procedure
    assert "BrandKitCIRegressionGate" in procedure
    assert "BrandKitConsumerDrift" in procedure
    assert "alertmanager-config" in procedure
    assert "port-forward svc/alertmanager" in procedure
    assert "ntfy" in procedure
    assert "does not print the ntfy URL or bearer token" in procedure
