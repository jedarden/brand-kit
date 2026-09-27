#!/usr/bin/env python3
"""Check that the scheduled brand-kit workflows are still producing successes.

This check is intentionally independent from the workflows it monitors. A
failed or suspended target cannot run its own ``onExit`` handler, so the
watchdog lists the target Workflow records through Argo and reports whether
each target has a recent successful run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import brand_kit_ci_failure_watch
from tools import release_publish

DEFAULT_ARGO_API_URL = release_publish.DEFAULT_ARGO_API_URL
DEFAULT_ARGO_NAMESPACE = release_publish.DEFAULT_ARGO_NAMESPACE
DEFAULT_LIST_LIMIT = 100
REPORT_SCHEMA = "brand-kit-workflow-liveness/v1"

TARGETS = (
    {
        "name": "brand-kit-ci-failure-watch",
        "workflow_template": "brand-kit-ci-failure-watch",
        "max_age_minutes": 60,
    },
    {
        "name": "brand-kit-consumer-drift",
        "workflow_template": "brand-kit-consumer-drift",
        "max_age_minutes": 48 * 60,
    },
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _successful_run_timestamp(workflow: Any) -> datetime | None:
    if not isinstance(workflow, dict):
        return None
    status = workflow.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Succeeded":
        return None
    metadata = workflow.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    for value in (
        status.get("finishedAt"),
        status.get("startedAt"),
        metadata.get("creationTimestamp"),
    ):
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def workflow_liveness_url(
    api_url: str,
    namespace: str,
    workflow_template: str,
) -> str:
    return brand_kit_ci_failure_watch.workflow_list_url(
        api_url,
        namespace,
        workflow_template,
        limit=DEFAULT_LIST_LIMIT,
    )


def _check_template(
    token: str,
    target: dict[str, Any],
    *,
    api_url: str,
    namespace: str,
    now: datetime,
    request: Callable[..., Any],
) -> dict[str, Any]:
    name = target["name"]
    workflow_template = target["workflow_template"]
    max_age_minutes = target["max_age_minutes"]
    response = request(
        "GET",
        workflow_liveness_url(api_url, namespace, workflow_template),
        token=token,
        authorization_scheme="Bearer",
        service="Argo API",
    )
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise release_publish.ReleaseError(
            f"Argo workflow-list response for {name} has no items list"
        )

    successful = []
    malformed_success = False
    for item in response["items"]:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, dict) and status.get("phase") == "Succeeded":
            observed_at = _successful_run_timestamp(item)
            if observed_at is None:
                malformed_success = True
            else:
                successful.append((observed_at, item))

    if not successful:
        if malformed_success:
            reason = "a successful run had no usable timestamp"
            status = "indeterminate"
        else:
            reason = "no successful run was returned by Argo"
            status = "stale"
        return {
            "name": name,
            "workflow_template": workflow_template,
            "max_age_minutes": max_age_minutes,
            "status": status,
            "last_success_at": None,
            "age_minutes": None,
            "reason": reason,
        }

    observed_at, _ = max(successful, key=lambda item: item[0])
    age_minutes = max(0.0, (now - observed_at).total_seconds() / 60)
    status = "fresh" if age_minutes <= max_age_minutes else "stale"
    return {
        "name": name,
        "workflow_template": workflow_template,
        "max_age_minutes": max_age_minutes,
        "status": status,
        "last_success_at": _timestamp_text(observed_at),
        "age_minutes": round(age_minutes, 3),
        "reason": f"latest successful run is {age_minutes:.1f} minutes old",
    }


def run_liveness(
    token: str | None,
    *,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    now: datetime | None = None,
    request: Callable[..., Any] = release_publish.request_json,
) -> dict[str, Any]:
    """Return a tri-state report for both scheduled target workflows."""
    token = release_publish.require_token(token, "ARGO_TOKEN")
    current = _utc_now(now)
    checks = []
    for target in TARGETS:
        try:
            check = _check_template(
                token,
                target,
                api_url=api_url,
                namespace=namespace,
                now=current,
                request=request,
            )
        except release_publish.ReleaseError as error:
            check = {
                "name": target["name"],
                "workflow_template": target["workflow_template"],
                "max_age_minutes": target["max_age_minutes"],
                "status": "indeterminate",
                "last_success_at": None,
                "age_minutes": None,
                "reason": str(error),
            }
        checks.append(check)

    statuses = {check["status"] for check in checks}
    if "stale" in statuses:
        status = "stale"
    elif "indeterminate" in statuses:
        status = "indeterminate"
    else:
        status = "fresh"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "observed_at": _timestamp_text(current),
        "checks": checks,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check that scheduled brand-kit workflows have recent successes."
    )
    parser.add_argument("--argo-api-url", default=DEFAULT_ARGO_API_URL)
    parser.add_argument("--argo-namespace", default=DEFAULT_ARGO_NAMESPACE)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_liveness(
            os.environ.get("ARGO_TOKEN"),
            api_url=args.argo_api_url,
            namespace=args.argo_namespace,
        )
    except release_publish.ReleaseError as error:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "indeterminate",
            "observed_at": _timestamp_text(_utc_now(None)),
            "checks": [],
            "error": str(error),
        }

    try:
        write_report(report, args.report)
    except OSError as error:
        print(f"error: cannot write workflow-liveness report: {error}", file=sys.stderr)
        return 2

    if report["status"] == "fresh":
        print("FRESH  all monitored brand-kit workflows have recent successes")
        return 0
    if report["status"] == "stale":
        print("STALE  one or more monitored brand-kit workflows need follow-up", file=sys.stderr)
        return 1
    print("INDETERMINATE  could not establish workflow liveness", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
