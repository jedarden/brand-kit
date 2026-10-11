#!/usr/bin/env python3
"""Check that the scheduled brand-kit workflows are still producing successes.

This check is intentionally independent from the workflows it monitors. A
suspended, deleted, or never-scheduled target cannot report its own absence,
so the watchdog asks whether each target has produced a recent *successful*
run and alerts when one has not.

The evidence is each target's durable report, not its Workflow object. iad-ci
deletes a Succeeded Workflow 300 seconds after it finishes (controller
``workflowDefaults.ttlStrategy``) and keeps at most 20 completed Workflows
(controller ``retentionPolicy``), so a healthy daily target has no Workflow
object minutes after it succeeds -- judging by Workflow records made this
check false-alarm on every run. Every target instead uploads its own
``report.json`` for every run, success or failure, to
``failures/<target>/<version>/<workflow-uid>/report.json`` in the
``needle-ci-artifacts`` Garage bucket with ``artifactGC: Never``. A report
whose ``schema`` is the target's own and whose ``status`` is that target's
success value is the durable record of a successful run; its S3
``LastModified`` (written as the run finishes) is when it happened.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import prune_ci_failure_watch_reports as s3_reports

REPORT_SCHEMA = "brand-kit-workflow-liveness/v1"
DEFAULT_ENDPOINT = s3_reports.DEFAULT_ENDPOINT
DEFAULT_BUCKET = s3_reports.DEFAULT_BUCKET
# Bound the object reads per target. The newest reports are read first and the
# first success ends the search, so a healthy target costs one read; a target
# failing for longer than this many runs reports "no successful run among the
# newest N" instead of the age of a success that old.
MAX_REPORT_READS_PER_TARGET = 20
REPORT_KEY_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}/report\.json$")

TARGETS = (
    {
        "name": "brand-kit-ci-failure-watch",
        "workflow_template": "brand-kit-ci-failure-watch",
        "report_prefix": "failures/brand-kit-ci-failure-watch/v1/",
        "report_schema": "brand-kit-ci-failure-watch/v1",
        "success_statuses": ("pass",),
        "max_age_minutes": 60,
    },
    {
        "name": "brand-kit-consumer-drift",
        "workflow_template": "brand-kit-consumer-drift",
        "report_prefix": "failures/brand-kit-consumer-drift/v1/",
        "report_schema": "brand-kit-consumer-drift/v1",
        "success_statuses": ("current",),
        "max_age_minutes": 48 * 60,
    },
    {
        "name": "brand-kit-mirror-health",
        "workflow_template": "brand-kit-mirror-health",
        "report_prefix": "failures/brand-kit-mirror-health/v1/",
        "report_schema": "brand-kit-mirror-health/v1",
        "success_statuses": ("healthy",),
        "max_age_minutes": 12 * 60,
    },
    {
        "name": "brand-kit-platform-requirements",
        "workflow_template": "brand-kit-platform-requirements",
        "report_prefix": "failures/brand-kit-platform-requirements/v2/",
        "report_schema": "brand-kit-platform-requirements/v2",
        "success_statuses": ("pass",),
        "max_age_minutes": 48 * 60,
    },
    {
        "name": "brand-kit-release-token-probe",
        "workflow_template": "brand-kit-release-token-probe",
        "report_prefix": "failures/brand-kit-release-token-probe/v1/",
        "report_schema": "brand-kit-release-token-probe/v1",
        "success_statuses": ("pass",),
        "max_age_minutes": 48 * 60,
    },
)


class ReportReader(Protocol):
    def list_objects(self, prefix: str) -> list[s3_reports.S3Object]: ...

    def get_object(self, key: str) -> bytes: ...


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _report_status(body: bytes, schema: str) -> str | None:
    """Return a report's status when it is this target's own report."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("schema") != schema:
        return None
    status = document.get("status")
    return status if isinstance(status, str) else None


def _check_target(
    reader: ReportReader,
    target: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    name = target["name"]
    prefix = target["report_prefix"]
    max_age_minutes = target["max_age_minutes"]
    result = {
        "name": name,
        "workflow_template": target["workflow_template"],
        "report_prefix": prefix,
        "max_age_minutes": max_age_minutes,
        "status": "indeterminate",
        "last_success_at": None,
        "age_minutes": None,
        "reason": "",
    }
    try:
        objects = reader.list_objects(prefix)
    except s3_reports.RetentionError as error:
        result["reason"] = f"cannot list run reports under {prefix}: {error}"
        return result

    reports = sorted(
        (
            item
            for item in objects
            if item.key.startswith(prefix)
            and REPORT_KEY_SUFFIX.fullmatch(item.key[len(prefix) :])
        ),
        key=lambda item: item.last_modified,
        reverse=True,
    )
    newest_status: str | None = None
    for index, item in enumerate(reports[:MAX_REPORT_READS_PER_TARGET]):
        try:
            body = reader.get_object(item.key)
        except s3_reports.RetentionError as error:
            result["reason"] = f"cannot read run report {item.key}: {error}"
            return result
        status = _report_status(body, target["report_schema"])
        if index == 0:
            newest_status = status or "unrecognized"
        if status in target["success_statuses"]:
            age_minutes = max(0.0, (now - item.last_modified).total_seconds() / 60)
            fresh = age_minutes <= max_age_minutes
            reason = f"latest successful run is {age_minutes:.1f} minutes old"
            if not fresh:
                reason += f" (limit {max_age_minutes}; newest status: {newest_status})"
            result.update(
                status="fresh" if fresh else "stale",
                last_success_at=_timestamp_text(item.last_modified),
                age_minutes=round(age_minutes, 3),
                reason=reason,
            )
            return result

    result["status"] = "stale"
    if not reports:
        result["reason"] = (
            f"no run report exists under {prefix}; the CronWorkflow may be "
            "suspended, deleted, or never scheduled"
        )
        return result
    newest_age = max(0.0, (now - reports[0].last_modified).total_seconds() / 60)
    read = min(len(reports), MAX_REPORT_READS_PER_TARGET)
    result["reason"] = (
        f"no successful run among the newest {read} of {len(reports)} run "
        f"report(s); the newest is {newest_age:.1f} minutes old "
        f"(newest status: {newest_status})"
    )
    return result


def run_liveness(
    reader: ReportReader,
    *,
    now: datetime | None = None,
    targets: tuple[dict[str, Any], ...] = TARGETS,
) -> dict[str, Any]:
    """Return a tri-state report for every monitored scheduled workflow."""
    current = _utc_now(now)
    checks = [_check_target(reader, target, now=current) for target in targets]
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


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise s3_reports.RetentionError(f"{name} is required; provide it through the environment")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check that scheduled brand-kit workflows have recent successes."
    )
    parser.add_argument("--endpoint", default=os.environ.get("S3_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--region", default=os.environ.get("S3_REGION", "garage"))
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reader = s3_reports.S3Client(
            args.endpoint,
            args.bucket,
            s3_reports.S3Credentials(
                _required_environment("S3_READER_ACCESS_KEY"),
                _required_environment("S3_READER_SECRET_KEY"),
            ),
            region=args.region,
        )
        report = run_liveness(reader)
    except s3_reports.RetentionError as error:
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

    for check in report["checks"]:
        stream = sys.stdout if check["status"] == "fresh" else sys.stderr
        print(f"{check['status'].upper():<13} {check['name']}: {check['reason']}", file=stream)
    if report["status"] == "fresh":
        print("FRESH  all monitored brand-kit workflows have recent successes")
        return 0
    if report["status"] == "stale":
        print("STALE  one or more monitored brand-kit workflows need follow-up", file=sys.stderr)
        return 1
    print("INDETERMINATE  could not establish workflow liveness", file=sys.stderr)
    if report.get("error"):
        print(f"INDETERMINATE  {report['error']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
