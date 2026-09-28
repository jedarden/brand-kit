"""Regression checks for the documented Argo workflow deadline contract."""

from pathlib import Path
import re

import yaml

from tools import http_policy


ROOT = Path(__file__).resolve().parent
AUTOMATION = ROOT / "automation"
DOCUMENTATION = ROOT / "docs" / "notes" / "external-api-resilience.md"
ACTIVE_DEADLINE_ANNOTATION = "brand-kit.ardenone.com/active-deadline-seconds"
MINIMUM_BUDGET_ANNOTATION = "brand-kit.ardenone.com/minimum-deadline-budget-seconds"


WORKFLOWS = {
    "brand-kit-ci-failure-watch-workflowtemplate.yml": (
        "brand-kit-ci-failure-watch",
        600,
        "failure-watch",
    ),
    "brand-kit-workflow-liveness-workflowtemplate.yml": (
        "brand-kit-workflow-liveness",
        600,
        "liveness",
    ),
    "brand-kit-consumer-drift-workflowtemplate.yml": (
        "brand-kit-consumer-drift",
        1800,
        "consumer-drift",
    ),
}


def _load_workflow(filename: str) -> dict:
    return yaml.safe_load((AUTOMATION / filename).read_text(encoding="utf-8"))


def _read_budget(timeout_seconds: float) -> int:
    attempts = http_policy.MAX_READ_ATTEMPTS
    retries = attempts - 1
    return int(
        timeout_seconds * attempts
        + http_policy.MAX_RETRY_DELAY_SECONDS * retries
    )


def _consumer_drift_polling_budget(workflow: dict) -> int:
    script = workflow["spec"]["templates"][0]["container"]["args"][0]
    attempts = re.search(r"for attempt in \$\(seq 1 (\d+)\)", script)
    interval = re.search(r"^\s*sleep (\d+)\s*$", script, re.MULTILINE)
    assert attempts is not None
    assert interval is not None
    return int(attempts.group(1)) * int(interval.group(1))


def _minimum_budget(kind: str, workflow: dict) -> int:
    api_read_budget = _read_budget(http_policy.API_TIMEOUT_SECONDS)
    if kind == "failure-watch":
        return max(api_read_budget, _read_budget(http_policy.GARAGE_TIMEOUT_SECONDS))
    if kind == "liveness":
        return api_read_budget
    if kind == "consumer-drift":
        return _consumer_drift_polling_budget(workflow) + api_read_budget
    raise AssertionError(f"unexpected workflow kind: {kind}")


def test_documented_deadlines_match_templates_annotations_and_retry_budgets():
    documentation = DOCUMENTATION.read_text(encoding="utf-8")

    for filename, (name, expected_deadline, kind) in WORKFLOWS.items():
        workflow = _load_workflow(filename)
        deadline = workflow["spec"]["activeDeadlineSeconds"]
        budget = _minimum_budget(kind, workflow)
        annotations = workflow["metadata"]["annotations"]

        assert workflow["kind"] == "WorkflowTemplate"
        assert workflow["metadata"]["name"] == name
        assert deadline == expected_deadline
        assert deadline >= budget
        assert annotations[ACTIVE_DEADLINE_ANNOTATION] == str(deadline)
        assert annotations[MINIMUM_BUDGET_ANNOTATION] == str(budget)
        assert f"| `{name}` | {deadline:,} seconds | {budget} seconds |" in documentation


def test_retry_budget_tracks_runtime_policy_and_consumer_polling_window():
    assert _read_budget(http_policy.API_TIMEOUT_SECONDS) == 70
    assert _read_budget(http_policy.PUBLIC_TIMEOUT_SECONDS) == 55
    assert _read_budget(http_policy.GARAGE_TIMEOUT_SECONDS) == 100

    workflow = _load_workflow("brand-kit-consumer-drift-workflowtemplate.yml")
    assert _consumer_drift_polling_budget(workflow) == 600
