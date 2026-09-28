"""Validation and constants for the versioned consumer-drift report contract."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any


REPORT_SCHEMA = "brand-kit-consumer-drift/v1"
REPORT_SCHEMA_RELPATH = "consumer-drift-report.schema.json"
REPORT_STATUSES = frozenset({"current", "stale", "indeterminate"})
CHECK_STATUSES = frozenset({"current", "stale", "unavailable", "skipped"})
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ReportError(ValueError):
    """A consumer-drift report does not satisfy the published v1 contract."""


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportError(f"{name} must be an object")
    return value


def _exact_keys(value: dict[str, Any], name: str, required: set[str]) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing:
        raise ReportError(f"{name} is missing required fields: {', '.join(missing)}")
    if unexpected:
        raise ReportError(f"{name} has unexpected fields: {', '.join(unexpected)}")


def _string(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any, name: str) -> str:
    text = _string(value, name)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportError(f"{name} must be an ISO-8601 date-time") from error
    if parsed.tzinfo is None:
        raise ReportError(f"{name} must include a timezone")
    return text


def _sha256(value: Any, name: str) -> str:
    text = _string(value, name)
    assert text is not None
    if not SHA256_RE.fullmatch(text):
        raise ReportError(f"{name} must be a 64-character hexadecimal SHA-256 digest")
    return text


def _release(value: Any) -> None:
    release = _object(value, "release")
    _exact_keys(release, "release", {"tag", "published_at", "record_target", "checkout"})
    _string(release["tag"], "release.tag", nullable=True)
    _string(release["published_at"], "release.published_at", nullable=True)
    _string(release["record_target"], "release.record_target", nullable=True)
    checkout = release["checkout"]
    if checkout is None:
        return
    checkout = _object(checkout, "release.checkout")
    _exact_keys(checkout, "release.checkout", {"head", "tag_commit", "matches"})
    _string(checkout["head"], "release.checkout.head", nullable=True)
    _string(checkout["tag_commit"], "release.checkout.tag_commit", nullable=True)
    if not isinstance(checkout["matches"], bool):
        raise ReportError("release.checkout.matches must be a boolean")


def _site(value: Any) -> None:
    site = _object(value, "site")
    _exact_keys(site, "site", {"name", "path", "commit"})
    _string(site["name"], "site.name")
    _string(site["path"], "site.path", nullable=True)
    _string(site["commit"], "site.commit", nullable=True)


def _size(value: Any, name: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value)
    ):
        raise ReportError(f"{name} must be a two-item positive integer array")


def _check(value: Any, index: int) -> None:
    check = _object(value, f"checks[{index}]")
    allowed = {
        "consumer",
        "asset",
        "source",
        "status",
        "path",
        "url",
        "media_url",
        "reason",
        "expected_sha256",
        "observed_sha256",
        "observed_size",
        "expected_size",
        "mean_luma",
        "max_channel",
    }
    unexpected = sorted(set(check) - allowed)
    if unexpected:
        raise ReportError(
            f"checks[{index}] has unexpected fields: {', '.join(unexpected)}"
        )
    for field in ("consumer", "asset", "source"):
        _string(check.get(field), f"checks[{index}].{field}")
    status = check.get("status")
    if status not in CHECK_STATUSES:
        raise ReportError(f"checks[{index}].status is not a supported check status")
    has_path = "path" in check
    has_url = "url" in check
    if has_path == has_url:
        raise ReportError(f"checks[{index}] must contain exactly one of path or url")
    if has_path:
        _string(check["path"], f"checks[{index}].path")
    if has_url:
        _string(check["url"], f"checks[{index}].url")
    for field in ("media_url",):
        if field in check:
            _string(check[field], f"checks[{index}].{field}")
    if "reason" in check:
        _string(check["reason"], f"checks[{index}].reason")
    for field in ("expected_sha256", "observed_sha256"):
        if field in check:
            _sha256(check[field], f"checks[{index}].{field}")
    for field in ("observed_size", "expected_size"):
        if field in check:
            _size(check[field], f"checks[{index}].{field}")
    for field in ("mean_luma", "max_channel"):
        if field in check:
            value_number = check[field]
            if isinstance(value_number, bool) or not isinstance(value_number, (int, float)):
                raise ReportError(f"checks[{index}].{field} must be a number")
            if value_number < 0:
                raise ReportError(f"checks[{index}].{field} must not be negative")


def _coverage(value: Any) -> None:
    coverage = _object(value, "coverage")
    _exact_keys(coverage, "coverage", {"out_of_scope"})
    out_of_scope = coverage["out_of_scope"]
    if not isinstance(out_of_scope, list):
        raise ReportError("coverage.out_of_scope must be an array")
    for index, value in enumerate(out_of_scope):
        entry = _object(value, f"coverage.out_of_scope[{index}]")
        _exact_keys(entry, f"coverage.out_of_scope[{index}]", {"platform", "surfaces", "reason"})
        _string(entry.get("platform"), f"coverage.out_of_scope[{index}].platform")
        surfaces = entry.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces:
            raise ReportError(f"coverage.out_of_scope[{index}].surfaces must be a non-empty array")
        for surface_index, surface in enumerate(surfaces):
            _string(surface, f"coverage.out_of_scope[{index}].surfaces[{surface_index}]")
        _string(entry.get("reason"), f"coverage.out_of_scope[{index}].reason")


def validate_report(report: Any) -> dict[str, Any]:
    """Validate one report and return it unchanged.

    This intentionally mirrors the checked-in JSON Schema without requiring the
    optional ``jsonschema`` package. The same function is used by the detector
    before it writes a report and by the workflow before it uploads an artifact.
    """

    root = _object(report, "report")
    _exact_keys(
        root,
        "report",
        {
            "schema",
            "checked_at",
            "status",
            "release",
            "site",
            "source_digests",
            "checks",
            "consumers",
            "coverage",
            "errors",
        },
    )
    if root["schema"] != REPORT_SCHEMA:
        raise ReportError(f"report.schema must be {REPORT_SCHEMA!r}")
    _timestamp(root["checked_at"], "checked_at")
    if root["status"] not in REPORT_STATUSES:
        raise ReportError("report.status is not a supported report status")
    _release(root["release"])
    _site(root["site"])

    source_digests = root["source_digests"]
    if not isinstance(source_digests, list):
        raise ReportError("source_digests must be an array")
    for index, value in enumerate(source_digests):
        digest = _object(value, f"source_digests[{index}]")
        _exact_keys(digest, f"source_digests[{index}]", {"path", "role", "sha256"})
        _string(digest.get("path"), f"source_digests[{index}].path")
        _string(digest.get("role"), f"source_digests[{index}].role")
        _sha256(digest.get("sha256"), f"source_digests[{index}].sha256")

    checks = root["checks"]
    if not isinstance(checks, list):
        raise ReportError("checks must be an array")
    for index, value in enumerate(checks):
        _check(value, index)

    consumers = _object(root["consumers"], "consumers")
    for name, value in consumers.items():
        if not isinstance(name, str) or not name.strip():
            raise ReportError("consumer names must be non-empty strings")
        summary = _object(value, f"consumers[{name!r}]")
        _exact_keys(summary, f"consumers[{name!r}]", {"status", "checks"})
        if summary["status"] not in CHECK_STATUSES:
            raise ReportError(f"consumers[{name!r}].status is not a supported check status")
        count = summary["checks"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ReportError(f"consumers[{name!r}].checks must be a positive integer")

    _coverage(root["coverage"])
    errors = root["errors"]
    if not isinstance(errors, list):
        raise ReportError("errors must be an array")
    for index, error in enumerate(errors):
        _string(error, f"errors[{index}]")
    return root
