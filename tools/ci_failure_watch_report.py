"""Strict, dependency-free validators for the CI failure-watch v1 artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlsplit

from tools import release_publish


REPORT_SCHEMA = "brand-kit-ci-failure-watch/v1"
ATTESTATION_SCHEMA = release_publish.CI_ATTESTATION_SCHEMA
ARGO_FAILURE_RETENTION_SECONDS = 7200
REPORT_STATUSES = frozenset({"pass", "fail", "error", "incomplete"})
PAGINATION_STATUSES = frozenset({"complete", "incomplete", "not_started"})
PAGINATION_ERROR_CODES = frozenset(
    {
        "page_request_failed",
        "missing_items",
        "invalid_item",
        "incomplete_workflow",
        "missing_metadata",
        "invalid_metadata",
        "invalid_continue",
        "repeated_continue",
        "page_limit_exceeded",
    }
)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class ReportContractError(release_publish.ReleaseError):
    """A failure-watch JSON artifact does not satisfy its published v1 contract."""


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportContractError(f"{name} must be an object")
    return value


def _exact_keys(value: dict[str, Any], name: str, required: set[str]) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing:
        raise ReportContractError(f"{name} is missing required fields: {', '.join(missing)}")
    if unexpected:
        raise ReportContractError(f"{name} has unexpected fields: {', '.join(unexpected)}")


def _string(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReportContractError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    text = _string(value, name)
    assert text is not None
    if not _UTC_TIMESTAMP_PATTERN.fullmatch(text):
        raise ReportContractError(f"{name} must be a UTC ISO-8601 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ReportContractError(f"{name} must be a valid ISO-8601 timestamp") from error
    return parsed.astimezone(timezone.utc)


def _https_url(value: Any, name: str) -> str:
    text = _string(value, name)
    assert text is not None
    if any(character.isspace() for character in text):
        raise ReportContractError(f"{name} must not contain whitespace")
    parsed = urlsplit(text)
    try:
        parsed.port  # Validate malformed port text without restricting valid HTTPS ports.
    except ValueError as error:
        raise ReportContractError(f"{name} is malformed") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReportContractError(f"{name} must be an HTTPS evidence URL without credentials or query")
    return text


def _attestation(record: Any, name: str) -> None:
    value = _object(record, name)
    _exact_keys(
        value,
        name,
        {"commit", "workflow_name", "workflow_uid", "phase", "finished_at"},
    )
    commit = _string(value["commit"], f"{name}.commit")
    assert commit is not None
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise ReportContractError(f"{name}.commit must be a lowercase full object ID")
    workflow_name = _string(value["workflow_name"], f"{name}.workflow_name")
    if workflow_name is None or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(workflow_name):
        raise ReportContractError(f"{name}.workflow_name is malformed")
    workflow_uid = _string(value["workflow_uid"], f"{name}.workflow_uid")
    if workflow_uid is None or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(workflow_uid):
        raise ReportContractError(f"{name}.workflow_uid is malformed")
    if value["phase"] != "Succeeded":
        raise ReportContractError(f"{name}.phase must be Succeeded")
    _timestamp(value["finished_at"], f"{name}.finished_at")


def _cutoff(value: Any, observed_at: datetime) -> None:
    if value is None:
        return
    cutoff = _object(value, "cutoff")
    _exact_keys(
        cutoff,
        "cutoff",
        {"at", "lookback_minutes", "argo_failure_retention_seconds"},
    )
    cutoff_at = _timestamp(cutoff["at"], "cutoff.at")
    lookback = cutoff["lookback_minutes"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 1 <= lookback <= 1440:
        raise ReportContractError("cutoff.lookback_minutes must be between 1 and 1440")
    retention = cutoff["argo_failure_retention_seconds"]
    if retention != ARGO_FAILURE_RETENTION_SECONDS:
        raise ReportContractError(
            "cutoff.argo_failure_retention_seconds does not match the watcher contract"
        )
    if lookback * 60 < retention:
        raise ReportContractError("cutoff.lookback_minutes must cover Argo failure retention")
    if cutoff_at != observed_at - timedelta(minutes=lookback):
        raise ReportContractError("cutoff.at must match observed_at and lookback_minutes")


def validate_report(report: Any) -> dict[str, Any]:
    """Validate a failure-watch report and return it unchanged."""
    root = _object(report, "report")
    _exact_keys(
        root,
        "report",
        {
            "schema",
            "status",
            "complete",
            "observed_at",
            "cutoff",
            "workflow_template",
            "watcher_workflow_uid",
            "attestations_url",
            "pagination",
            "error",
            "failures",
            "attestations",
        },
    )
    if root["schema"] != REPORT_SCHEMA:
        raise ReportContractError(f"report.schema must be {REPORT_SCHEMA!r}")
    status = root["status"]
    if status not in REPORT_STATUSES:
        raise ReportContractError("report.status is not a supported status")
    if not isinstance(root["complete"], bool):
        raise ReportContractError("report.complete must be a boolean")
    observed_at = _timestamp(root["observed_at"], "observed_at")
    _cutoff(root["cutoff"], observed_at)
    template = _string(root["workflow_template"], "workflow_template")
    if template is None or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(template):
        raise ReportContractError("workflow_template is malformed")

    watcher_uid = root["watcher_workflow_uid"]
    if watcher_uid is not None and (
        not isinstance(watcher_uid, str)
        or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(watcher_uid)
    ):
        raise ReportContractError("watcher_workflow_uid is malformed")
    attestation_url = root["attestations_url"]
    if attestation_url is not None:
        _https_url(attestation_url, "attestations_url")
        try:
            release_publish.validate_ci_attestation_url(attestation_url)
        except release_publish.ReleaseError as error:
            raise ReportContractError(f"attestations_url is malformed: {error}") from error
        if watcher_uid != release_publish.ci_attestation_watcher_uid(attestation_url):
            raise ReportContractError("attestations_url UID does not match watcher_workflow_uid")
    elif watcher_uid is not None:
        raise ReportContractError("a watcher_workflow_uid requires attestations_url")

    pagination = _object(root["pagination"], "pagination")
    _exact_keys(pagination, "pagination", {"status", "pages_read", "error"})
    pagination_status = pagination["status"]
    if pagination_status not in PAGINATION_STATUSES:
        raise ReportContractError("pagination.status is not supported")
    pages_read = pagination["pages_read"]
    if isinstance(pages_read, bool) or not isinstance(pages_read, int) or pages_read < 0:
        raise ReportContractError("pagination.pages_read must be a non-negative integer")
    pagination_error = pagination["error"]
    if pagination_status == "complete":
        if pages_read < 1 or pagination_error is not None:
            raise ReportContractError("complete pagination needs a page and no error")
    elif pagination_status == "not_started":
        if pages_read != 0 or pagination_error is not None:
            raise ReportContractError("not_started pagination must have zero pages and no error")
    else:
        error_record = _object(pagination_error, "pagination.error")
        _exact_keys(error_record, "pagination.error", {"code", "page"})
        if error_record["code"] not in PAGINATION_ERROR_CODES:
            raise ReportContractError("pagination.error.code is not supported")
        page = error_record["page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ReportContractError("pagination.error.page must be a positive integer")

    error = root["error"]
    if error is not None:
        _string(error, "error")
    failures = root["failures"]
    if not isinstance(failures, list):
        raise ReportContractError("failures must be an array")
    for index, raw_failure in enumerate(failures):
        name = f"failures[{index}]"
        failure = _object(raw_failure, name)
        _exact_keys(
            failure,
            name,
            {
                "name",
                "uid",
                "phase",
                "message",
                "creation_timestamp",
                "started_at",
                "finished_at",
                "commit",
                "api_url",
            },
        )
        _string(failure["name"], f"{name}.name")
        uid = failure["uid"]
        if uid is not None and (
            not isinstance(uid, str)
            or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(uid)
        ):
            raise ReportContractError(f"{name}.uid is malformed")
        if failure["phase"] not in {"Failed", "Error"}:
            raise ReportContractError(f"{name}.phase must be Failed or Error")
        _string(failure["message"], f"{name}.message", nullable=True)
        for field in ("creation_timestamp", "started_at", "finished_at"):
            if failure[field] is not None:
                _timestamp(failure[field], f"{name}.{field}")
        commit = failure["commit"]
        if commit is not None and (
            not isinstance(commit, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
        ):
            raise ReportContractError(f"{name}.commit must be a lowercase full object ID")
        api_url = failure["api_url"]
        if api_url is not None:
            _https_url(api_url, f"{name}.api_url")
            path_marker = "/api/v1/workflows/"
            if path_marker not in urlsplit(api_url).path:
                raise ReportContractError(f"{name}.api_url must identify an Argo Workflow")
            workflow_identity = urlsplit(api_url).path.split(path_marker, 1)[1]
            if len(workflow_identity.split("/")) != 2 or not all(
                workflow_identity.split("/")
            ):
                raise ReportContractError(f"{name}.api_url must identify one Argo Workflow")

    attestations = root["attestations"]
    if not isinstance(attestations, list):
        raise ReportContractError("attestations must be an array")
    for index, record in enumerate(attestations):
        _attestation(record, f"attestations[{index}]")

    expected_complete = status in {"pass", "fail"}
    if root["complete"] is not expected_complete:
        raise ReportContractError("complete must match the report status")
    if status in {"pass", "fail"}:
        if pagination_status != "complete" or error is not None or root["cutoff"] is None:
            raise ReportContractError("complete results require a cutoff and complete pagination")
        if (status == "pass") != (not failures):
            raise ReportContractError("pass/fail status does not match the failure list")
    elif status == "incomplete":
        if pagination_status != "incomplete" or error is None or root["cutoff"] is None:
            raise ReportContractError("incomplete results require pagination error and cutoff metadata")
        if failures or attestations:
            raise ReportContractError("incomplete results must discard partial evidence")
    else:
        if pagination_status != "not_started" or error is None:
            raise ReportContractError("error results must explain why inspection did not start")
        if failures or attestations:
            raise ReportContractError("error results cannot include workflow evidence")
    return root


def validate_attestations(document: Any) -> dict[str, Any]:
    """Validate the durable attestation envelope and every record in it."""
    root = _object(document, "attestations")
    _exact_keys(root, "attestations", {"schema", "watcher_workflow_uid", "attestations"})
    if root["schema"] != ATTESTATION_SCHEMA:
        raise ReportContractError(f"attestations.schema must be {ATTESTATION_SCHEMA!r}")
    watcher_uid = root["watcher_workflow_uid"]
    if (
        not isinstance(watcher_uid, str)
        or not release_publish.WORKFLOW_UID_PATTERN.fullmatch(watcher_uid)
    ):
        raise ReportContractError("watcher_workflow_uid is malformed")
    records = root["attestations"]
    if not isinstance(records, list):
        raise ReportContractError("attestations.attestations must be an array")
    for index, record in enumerate(records):
        _attestation(record, f"attestations.attestations[{index}]")
    return root


def attestation_url(watcher_workflow_uid: str) -> str:
    if not release_publish.WORKFLOW_UID_PATTERN.fullmatch(watcher_workflow_uid):
        raise ReportContractError("watcher workflow UID is malformed")
    return (
        f"https://{release_publish.DEFAULT_CI_ARTIFACT_HOST}/"
        f"{release_publish.DEFAULT_CI_ARTIFACT_BUCKET}/"
        f"{release_publish.CI_ATTESTATION_PREFIX}{watcher_workflow_uid}/attestations.json"
    )
