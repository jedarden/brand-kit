#!/usr/bin/env python3
"""Find recent brand-kit-ci runs for owner routing and durable CI evidence.

The watcher is deliberately read-only.  It lists Workflow records through the
Argo API, keeps only recent ``Failed``/``Error`` runs for the owner report,
and copies validated successful-run metadata into a separate durable
attestation artifact.  Alert delivery is owned by the companion WorkflowTemplate
exit handler so an Alertmanager outage cannot change the observed CI result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import ci_failure_watch_report
from tools import release_publish

DEFAULT_ARGO_API_URL = release_publish.DEFAULT_ARGO_API_URL
DEFAULT_ARGO_NAMESPACE = release_publish.DEFAULT_ARGO_NAMESPACE
DEFAULT_WORKFLOW_TEMPLATE = release_publish.DEFAULT_ARGO_WORKFLOW_TEMPLATE
# This mirrors controller.workflowDefaults.spec.ttlStrategy.secondsAfterFailure
# in declarative-config/k8s/iad-ci/argo-workflows/argo-workflows-application.yml.
# Keep the watcher at least this wide: a shorter window can silently miss a
# failed brand-kit-ci Workflow after Argo has reaped it.
ARGO_FAILURE_RETENTION_SECONDS = ci_failure_watch_report.ARGO_FAILURE_RETENTION_SECONDS
DEFAULT_LOOKBACK_MINUTES = math.ceil(ARGO_FAILURE_RETENTION_SECONDS / 60)
DEFAULT_LIST_LIMIT = 100
MAX_LIST_PAGES = 10_000
REPORT_SCHEMA = ci_failure_watch_report.REPORT_SCHEMA
ATTESTATION_SCHEMA = ci_failure_watch_report.ATTESTATION_SCHEMA
FAILURE_PHASES = frozenset({"Failed", "Error"})
COMMIT_PARAMETER_NAMES = frozenset(
    {"commit", "revision", "sha", "gitcommit", "gitsha", "headcommit", "headsha"}
)


class WatchPaginationError(release_publish.ReleaseError):
    """An Argo list snapshot could not be completed safely."""

    def __init__(self, code: str, page: int, pages_read: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.page = page
        self.pages_read = pages_read


def _redact_text(value: Any, *secrets: str) -> Any:
    """Keep untrusted Argo messages from copying credentials into evidence."""
    if not isinstance(value, str):
        return value
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def argo_api_path(api_url: str, path: str) -> str:
    if not isinstance(api_url, str) or not api_url.strip():
        raise release_publish.ReleaseError("Argo API URL must be an absolute HTTPS URL")
    parsed = urllib.parse.urlsplit(api_url)
    try:
        parsed.port
    except ValueError as error:
        raise release_publish.ReleaseError("Argo API URL has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise release_publish.ReleaseError(
            "Argo API URL must be HTTPS without credentials, query, or fragment"
        )
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return f"{base}/{path.lstrip('/')}"


def argo_workflows_url(api_url: str, namespace: str) -> str:
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    return argo_api_path(
        api_url,
        f"workflows/{urllib.parse.quote(namespace, safe='')}",
    )


def workflow_list_url(
    api_url: str,
    namespace: str,
    workflow_template: str,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    continue_token: str | None = None,
) -> str:
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"Argo workflow template is malformed: {workflow_template!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise release_publish.ReleaseError("Argo workflow list limit must be between 1 and 1000")
    if continue_token is not None and (
        not isinstance(continue_token, str) or not continue_token.strip()
    ):
        raise release_publish.ReleaseError("Argo workflow continuation token must be non-empty")
    label_selector = urllib.parse.quote(
        f"workflows.argoproj.io/workflow-template={workflow_template}",
        safe="",
    )
    url = f"{argo_workflows_url(api_url, namespace)}?labelSelector={label_selector}&limit={limit}"
    if continue_token is not None:
        url += f"&continue={urllib.parse.quote(continue_token, safe='')}"
    return url


def _workflow_list_items(
    token: str,
    *,
    api_url: str,
    namespace: str,
    workflow_template: str,
    request: Callable[..., Any],
) -> tuple[list[dict[str, Any]], int]:
    """Read one complete, consistently paginated Workflow list snapshot.

    Argo exposes Kubernetes list pagination in ``metadata.continue``. A
    missing or empty token is the only terminal condition; pages are not
    assumed to be ordered by any workflow timestamp, so the caller must apply
    its time window after this function has collected every page.
    """
    items: list[dict[str, Any]] = []
    continuation: str | None = None
    seen_tokens: set[str] = set()
    page_number = 0

    while True:
        page_number += 1
        if page_number > MAX_LIST_PAGES:
            raise WatchPaginationError(
                "page_limit_exceeded",
                page_number,
                page_number - 1,
                "Argo workflow-list pagination exceeded the maximum page count",
            )
        page_url = workflow_list_url(
            api_url,
            namespace,
            workflow_template,
            continue_token=continuation,
        )
        try:
            response = request(
                "GET",
                page_url,
                token=token,
                authorization_scheme="Bearer",
                service="Argo API",
            )
        except release_publish.ReleaseError as error:
            detail = _redact_text(str(error), token)
            raise WatchPaginationError(
                "page_request_failed",
                page_number,
                page_number - 1,
                f"Argo API workflow-list page {page_number} request failed: {detail}",
            ) from error
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise WatchPaginationError(
                "missing_items",
                page_number,
                page_number - 1,
                f"Argo workflow-list page {page_number} has no items list",
            )

        page_items = response["items"]
        for item in page_items:
            if not isinstance(item, dict):
                raise WatchPaginationError(
                    "invalid_item",
                    page_number,
                    page_number - 1,
                    f"Argo workflow-list page {page_number} contains a non-object item",
                )
            if not isinstance(item.get("metadata"), dict) or not isinstance(
                item.get("status"), dict
            ):
                raise WatchPaginationError(
                    "incomplete_workflow",
                    page_number,
                    page_number - 1,
                    f"Argo workflow-list page {page_number} contains an incomplete workflow",
                )
        items.extend(page_items)
        pages_read = page_number

        if "metadata" not in response:
            if continuation is not None:
                raise WatchPaginationError(
                    "missing_metadata",
                    page_number,
                    pages_read,
                    f"Argo workflow-list page {page_number} has no pagination metadata",
                )
            next_continuation = None
        elif not isinstance(response["metadata"], dict):
            raise WatchPaginationError(
                "invalid_metadata",
                page_number,
                pages_read,
                f"Argo workflow-list page {page_number} has invalid metadata",
            )
        else:
            metadata = response["metadata"]
            next_continuation = metadata.get("continue")
            if next_continuation in (None, ""):
                next_continuation = None
            elif not isinstance(next_continuation, str) or not next_continuation.strip():
                raise WatchPaginationError(
                    "invalid_continue",
                    page_number,
                    pages_read,
                    f"Argo workflow-list page {page_number} has an invalid continuation token",
                )

        if next_continuation is None:
            return items, pages_read
        if next_continuation in seen_tokens:
            raise WatchPaginationError(
                "repeated_continue",
                page_number,
                pages_read,
                "Argo workflow-list pagination repeated a continuation token",
            )
        seen_tokens.add(next_continuation)
        continuation = next_continuation


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


def _timestamp_text(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed is not None else None


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _normalized_parameter_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[-_]", "", value).lower()


def _commit_from_workflow(workflow: dict[str, Any]) -> str | None:
    status = workflow.get("status")
    if not isinstance(status, dict):
        return None
    outputs = status.get("outputs")
    if not isinstance(outputs, dict):
        return None
    parameters = outputs.get("parameters")
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if _normalized_parameter_name(parameter.get("name")) not in COMMIT_PARAMETER_NAMES:
            continue
        value = parameter.get("value")
        if isinstance(value, str) and release_publish.OBJECT_ID_PATTERN.fullmatch(value):
            return value.lower()
    return None


def _failure_summary(
    workflow: Any,
    *,
    cutoff: datetime,
    api_url: str,
    namespace: str,
    redact_secrets: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if not isinstance(workflow, dict):
        return None
    metadata = workflow.get("metadata")
    status = workflow.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        return None
    phase = status.get("phase")
    if phase not in FAILURE_PHASES:
        return None

    created_at = metadata.get("creationTimestamp")
    started_at = status.get("startedAt")
    finished_at = status.get("finishedAt")
    observed_at = (
        _parse_timestamp(finished_at)
        or _parse_timestamp(started_at)
        or _parse_timestamp(created_at)
    )
    # A failed record without a usable timestamp is retained for manual review;
    # dropping it would make malformed API data hide the very failure this job
    # exists to report.
    if observed_at is not None and observed_at < cutoff:
        return None

    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "<unnamed>"
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(uid):
        uid = None
    message = status.get("message")
    if not isinstance(message, str) or not message:
        message = None
    summary: dict[str, Any] = {
        "name": name,
        "uid": uid,
        "phase": phase,
        "message": _redact_text(message, *redact_secrets),
        "creation_timestamp": _timestamp_text(created_at),
        "started_at": _timestamp_text(started_at),
        "finished_at": _timestamp_text(finished_at),
        "commit": _commit_from_workflow(workflow),
        "api_url": None,
    }
    if name != "<unnamed>" and release_publish.WORKFLOW_NAME_PATTERN.fullmatch(name):
        summary["api_url"] = release_publish.argo_workflow_url(
            api_url,
            namespace,
            name,
        )
    return summary


def _success_attestation(
    workflow: Any,
    *,
    cutoff: datetime,
) -> dict[str, Any] | None:
    """Return the exact fields needed to prove one successful CI run."""
    if not isinstance(workflow, dict):
        return None
    metadata = workflow.get("metadata")
    status = workflow.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        return None
    if status.get("phase") != "Succeeded":
        return None

    finished_at = status.get("finishedAt")
    observed_at = _parse_timestamp(finished_at)
    if observed_at is None or observed_at < cutoff:
        return None
    workflow_uid = metadata.get("uid")
    workflow_name = metadata.get("name")
    commit = _commit_from_workflow(workflow)
    if (
        not isinstance(workflow_name, str)
        or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(workflow_name)
        or not isinstance(workflow_uid, str)
        or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(workflow_uid)
        or commit is None
    ):
        return None
    return {
        "commit": commit,
        "workflow_name": workflow_name,
        "workflow_uid": workflow_uid,
        "phase": "Succeeded",
        "finished_at": observed_at.isoformat().replace("+00:00", "Z"),
    }


def validate_lookback_coverage(lookback_minutes: int) -> None:
    """Reject a watcher window that cannot cover Argo's failure retention."""
    if not isinstance(lookback_minutes, int) or isinstance(lookback_minutes, bool):
        raise release_publish.ReleaseError("lookback minutes must be an integer")
    if not 1 <= lookback_minutes <= 24 * 60:
        raise release_publish.ReleaseError("lookback minutes must be between 1 and 1440")
    if lookback_minutes * 60 < ARGO_FAILURE_RETENTION_SECONDS:
        required_minutes = math.ceil(ARGO_FAILURE_RETENTION_SECONDS / 60)
        raise release_publish.ReleaseError(
            "lookback minutes must cover Argo failure retention: "
            f"{lookback_minutes} < {required_minutes}"
        )


def run_watch(
    token: str | None,
    *,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    now: datetime | None = None,
    request: Callable[..., Any] = release_publish.request_json,
) -> dict[str, Any]:
    """Return recent failures plus successful runs worth durably attesting."""
    token = release_publish.require_token(token, "ARGO_WORKFLOW_TOKEN")
    validate_lookback_coverage(lookback_minutes)

    current = _utc_now(now)
    cutoff = current - timedelta(minutes=lookback_minutes)
    workflows, pages_read = _workflow_list_items(
        token,
        api_url=api_url,
        namespace=namespace,
        workflow_template=workflow_template,
        request=request,
    )
    failures = [
        summary
        for item in workflows
        if (summary := _failure_summary(
            item,
            cutoff=cutoff,
            api_url=api_url,
            namespace=namespace,
            redact_secrets=(token,),
        ))
        is not None
    ]
    attestations = [
        attestation
        for item in workflows
        if (attestation := _success_attestation(item, cutoff=cutoff)) is not None
    ]
    failures.sort(
        key=lambda item: (
            _parse_timestamp(item.get("finished_at"))
            or _parse_timestamp(item.get("started_at"))
            or _parse_timestamp(item.get("creation_timestamp"))
            or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )
    attestations.sort(
        key=lambda item: _parse_timestamp(item["finished_at"])
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {
        "schema": REPORT_SCHEMA,
        "status": "fail" if failures else "pass",
        "complete": True,
        "observed_at": current.isoformat().replace("+00:00", "Z"),
        "cutoff": {
            "at": cutoff.isoformat().replace("+00:00", "Z"),
            "lookback_minutes": lookback_minutes,
            "argo_failure_retention_seconds": ARGO_FAILURE_RETENTION_SECONDS,
        },
        "workflow_template": workflow_template,
        "watcher_workflow_uid": None,
        "attestations_url": None,
        "pagination": {"status": "complete", "pages_read": pages_read, "error": None},
        "error": None,
        "failures": failures,
        "attestations": attestations,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    ci_failure_watch_report.validate_report(report)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_attestations(
    report: dict[str, Any],
    path: Path,
    *,
    watcher_workflow_uid: str | None = None,
) -> None:
    ci_failure_watch_report.validate_report(report)
    if watcher_workflow_uid is None:
        raise release_publish.ReleaseError("durable attestation output requires the watcher workflow UID")
    document: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "attestations": report.get("attestations", []),
        "watcher_workflow_uid": watcher_workflow_uid,
    }
    ci_failure_watch_report.validate_attestations(document)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find recent failed brand-kit-ci Argo workflows and archive "
            "successful-run attestations."
        )
    )
    parser.add_argument("--argo-api-url", default=DEFAULT_ARGO_API_URL)
    parser.add_argument("--argo-namespace", default=DEFAULT_ARGO_NAMESPACE)
    parser.add_argument("--workflow-template", default=DEFAULT_WORKFLOW_TEMPLATE)
    parser.add_argument("--lookback-minutes", type=int, default=DEFAULT_LOOKBACK_MINUTES)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--attestations", type=Path)
    parser.add_argument(
        "--watcher-workflow-uid",
        help="UID of this watcher Workflow, embedded in the durable object envelope",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = _utc_now(None)
    requested_uid = args.watcher_workflow_uid
    if not isinstance(requested_uid, str) or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(
        requested_uid
    ):
        requested_uid = None
    workflow_template = args.workflow_template
    if not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(workflow_template):
        workflow_template = DEFAULT_WORKFLOW_TEMPLATE
    try:
        report = run_watch(
            os.environ.get("ARGO_WORKFLOW_TOKEN"),
            api_url=args.argo_api_url,
            namespace=args.argo_namespace,
            workflow_template=args.workflow_template,
            lookback_minutes=args.lookback_minutes,
            now=current,
        )
    except WatchPaginationError as error:
        cutoff = _cutoff_metadata(current, args.lookback_minutes)
        report = {
            "schema": REPORT_SCHEMA,
            "status": "incomplete",
            "complete": False,
            "observed_at": current.isoformat().replace("+00:00", "Z"),
            "cutoff": cutoff,
            "workflow_template": workflow_template,
            "watcher_workflow_uid": requested_uid,
            "attestations_url": (
                ci_failure_watch_report.attestation_url(requested_uid)
                if requested_uid is not None
                else None
            ),
            "pagination": {
                "status": "incomplete",
                "pages_read": error.pages_read,
                "error": {"code": error.code, "page": error.page},
            },
            "error": str(error),
            "failures": [],
            "attestations": [],
        }
        exit_code = 2
    except release_publish.ReleaseError as error:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "error",
            "complete": False,
            "observed_at": current.isoformat().replace("+00:00", "Z"),
            "cutoff": _cutoff_metadata(current, args.lookback_minutes),
            "workflow_template": workflow_template,
            "watcher_workflow_uid": requested_uid,
            "attestations_url": (
                ci_failure_watch_report.attestation_url(requested_uid)
                if requested_uid is not None
                else None
            ),
            "pagination": {"status": "not_started", "pages_read": 0, "error": None},
            "error": str(error),
            "failures": [],
            "attestations": [],
        }
        exit_code = 2
    else:
        report["watcher_workflow_uid"] = requested_uid
        report["attestations_url"] = (
            ci_failure_watch_report.attestation_url(requested_uid)
            if requested_uid is not None
            else None
        )
        exit_code = 1 if report["failures"] else 0

    try:
        write_report(report, args.report)
        if args.attestations is not None:
            write_attestations(
                report,
                args.attestations,
                watcher_workflow_uid=args.watcher_workflow_uid,
            )
    except (OSError, release_publish.ReleaseError) as error:
        print(f"error: cannot write failure-watch evidence: {error}", file=sys.stderr)
        return 2

    if report["status"] == "pass":
        print("PASS  no recent brand-kit-ci failures")
    elif report["status"] == "fail":
        print(
            f"FAIL  {len(report['failures'])} recent brand-kit-ci workflow failure(s)",
            file=sys.stderr,
        )
        for failure in report["failures"]:
            print(
                f"FAIL  {failure['name']} phase={failure['phase']}",
                file=sys.stderr,
            )
    else:
        print(f"ERROR  {report['error']}", file=sys.stderr)
    return exit_code


def _cutoff_metadata(current: datetime, lookback_minutes: Any) -> dict[str, Any] | None:
    if (
        isinstance(lookback_minutes, bool)
        or not isinstance(lookback_minutes, int)
        or lookback_minutes * 60 < ARGO_FAILURE_RETENTION_SECONDS
        or lookback_minutes > 24 * 60
    ):
        return None
    cutoff = current - timedelta(minutes=lookback_minutes)
    return {
        "at": cutoff.isoformat().replace("+00:00", "Z"),
        "lookback_minutes": lookback_minutes,
        "argo_failure_retention_seconds": ARGO_FAILURE_RETENTION_SECONDS,
    }


if __name__ == "__main__":
    raise SystemExit(main())
