#!/usr/bin/env python3
"""Prune consumer-drift reports without deleting release evidence.

Consumer-drift reports are stored outside Argo's Workflow retention window.
This policy keeps unreferenced reports for a bounded rolling window, while a
report URL recorded in release evidence is a retention hold.  Evidence is
loaded and validated before the object list is read; any missing or malformed
evidence fails closed so an operator can repair the evidence and retry without
having deleted an object on an incomplete view of the holds.

The S3 transport is shared with the failure-watch pruner, but the prefix,
retention policy, and release-evidence hold set are intentionally separate.
Listing uses read credentials and deletion uses publisher credentials.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import urllib.parse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import release_evidence
from tools.prune_ci_failure_watch_reports import (
    RetentionError,
    S3Client,
    S3Credentials,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENDPOINT = "https://s3.ardenone.com"
DEFAULT_BUCKET = "needle-ci-artifacts"
DEFAULT_PREFIX = "failures/brand-kit-consumer-drift/v1/"
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
DEFAULT_EVIDENCE_ROOT = ROOT / "release-evidence" / "v1"


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _validate_retention(retention_days: int) -> None:
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        raise RetentionError("retention days must be an integer")
    if not MIN_RETENTION_DAYS <= retention_days <= MAX_RETENTION_DAYS:
        raise RetentionError(
            f"retention days must be between {MIN_RETENTION_DAYS} and "
            f"{MAX_RETENTION_DAYS}"
        )


def _report_key_from_url(
    report_url: str,
    *,
    endpoint: str,
    bucket: str,
    prefix: str,
) -> str:
    """Resolve one evidence URL to a key in this exact S3 report prefix."""
    parsed_url = urllib.parse.urlsplit(report_url)
    parsed_endpoint = urllib.parse.urlsplit(endpoint.rstrip("/"))
    if (
        parsed_url.scheme != parsed_endpoint.scheme
        or parsed_url.netloc != parsed_endpoint.netloc
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise RetentionError(
            "release evidence report URL does not use the configured S3 endpoint"
        )

    endpoint_path = parsed_endpoint.path.rstrip("/")
    bucket_path = f"{endpoint_path}/{bucket}/" if endpoint_path else f"/{bucket}/"
    if not parsed_url.path.startswith(bucket_path):
        raise RetentionError(
            "release evidence report URL does not use the configured S3 bucket"
        )
    key = urllib.parse.unquote(parsed_url.path[len(bucket_path) :])
    if not key.startswith(prefix):
        raise RetentionError(
            "release evidence report URL is outside the consumer-drift report prefix"
        )
    relative = key[len(prefix) :]
    components = relative.split("/")
    if (
        len(components) != 2
        or not components[0]
        or components[0] in {".", ".."}
        or components[1] != "report.json"
    ):
        raise RetentionError(
            "release evidence report URL contains an unsafe consumer-drift artifact path"
        )
    return key


def protected_report_keys(
    evidence_root: Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    bucket: str = DEFAULT_BUCKET,
    prefix: str = DEFAULT_PREFIX,
) -> set[str]:
    """Return report keys held by every valid release-evidence record.

    The evidence directory is a required input, not an optional best-effort
    hint.  That makes a failed checkout or malformed record safe: the caller
    gets no deletion candidates instead of accidentally pruning an unobserved
    hold.
    """
    if not prefix.endswith("/"):
        raise RetentionError("report prefix must end with /")
    if not evidence_root.is_dir():
        raise RetentionError(f"release evidence directory is unavailable: {evidence_root}")

    protected: set[str] = set()
    for path in sorted(evidence_root.glob("*.json")):
        if not path.is_file():
            raise RetentionError(f"release evidence is not a regular file: {path}")
        try:
            record = release_evidence.load_record(path)
        except (OSError, UnicodeError, release_evidence.EvidenceError) as error:
            raise RetentionError(f"cannot validate release evidence {path.name}: {error}") from error
        report_url = record["consumer_drift"]["report_url"]
        if report_url is not None:
            protected.add(
                _report_key_from_url(
                    report_url,
                    endpoint=endpoint,
                    bucket=bucket,
                    prefix=prefix,
                )
            )
    return protected


def prune_reports(
    reader: S3Client,
    publisher: S3Client,
    *,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    endpoint: str = DEFAULT_ENDPOINT,
    bucket: str = DEFAULT_BUCKET,
    prefix: str = DEFAULT_PREFIX,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> tuple[int, datetime]:
    """Delete expired, unreferenced consumer-drift reports.

    The age boundary is strict: an object exactly at the cutoff remains for
    the next pass.  ``dry_run`` performs the complete evidence and list checks
    but does not send a delete request.
    """
    _validate_retention(retention_days)
    if not prefix.endswith("/"):
        raise RetentionError("report prefix must end with /")
    protected = protected_report_keys(
        evidence_root,
        endpoint=endpoint,
        bucket=bucket,
        prefix=prefix,
    )
    cutoff = _utc_now(now) - timedelta(days=retention_days)
    expired = [
        item
        for item in reader.list_objects(prefix)
        if item.key.endswith("/report.json")
        and item.last_modified < cutoff
        and item.key not in protected
    ]
    if not dry_run:
        publisher.delete_objects([item.key for item in expired])
    return len(expired), cutoff


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RetentionError(f"{name} is required; provide it through the environment")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune expired, unheld brand-kit consumer-drift reports."
    )
    parser.add_argument("--endpoint", default=os.environ.get("S3_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--region", default=os.environ.get("S3_REGION", "garage"))
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument(
        "--release-evidence-root",
        type=Path,
        default=DEFAULT_EVIDENCE_ROOT,
        help="directory containing versioned release-evidence JSON records",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list deletion candidates without deleting them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = _utc_now(None)
        reader = S3Client(
            args.endpoint,
            args.bucket,
            S3Credentials(
                _required_environment("S3_READER_ACCESS_KEY"),
                _required_environment("S3_READER_SECRET_KEY"),
            ),
            region=args.region,
            now=now,
        )
        if args.dry_run:
            publisher = reader
        else:
            publisher = S3Client(
                args.endpoint,
                args.bucket,
                S3Credentials(
                    _required_environment("S3_PUBLISHER_ACCESS_KEY"),
                    _required_environment("S3_PUBLISHER_SECRET_KEY"),
                ),
                region=args.region,
                now=now,
            )
        pruned, _ = prune_reports(
            reader,
            publisher,
            evidence_root=args.release_evidence_root,
            endpoint=args.endpoint,
            bucket=args.bucket,
            prefix=args.prefix,
            retention_days=args.retention_days,
            now=now,
            dry_run=args.dry_run,
        )
    except (RetentionError, ValueError) as error:
        print(
            f"ERROR  consumer-drift report pruning did not complete: {error}",
            file=sys.stderr,
        )
        return 2
    action = "WOULD_PRUNE" if args.dry_run else "PRUNED"
    print(
        f"{action}  {pruned} unreferenced consumer-drift report(s) older than "
        f"{args.retention_days} days"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
