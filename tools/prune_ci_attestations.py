#!/usr/bin/env python3
"""Prune expired, unheld brand-kit CI attestation objects from Garage.

Argo's ``artifactGC: Never`` keeps an attestation after its watcher Workflow
is deleted, but it is not a forever-retention setting.  This pass keeps
unreferenced attestation objects for a bounded rolling window and treats every
attestation URL in committed release evidence as an immutable retention hold.
It validates all evidence before listing or deleting anything, and it only
deletes the exact ``attestations.json`` objects in the attestation prefix.
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

from tools import release_evidence, release_publish, retention_holds
from tools.prune_ci_failure_watch_reports import (
    RetentionError,
    S3Client,
    S3Credentials,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENDPOINT = "https://s3.ardenone.com"
DEFAULT_BUCKET = "needle-ci-artifacts"
DEFAULT_PREFIX = release_publish.CI_ATTESTATION_PREFIX
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


def _attestation_key_from_url(
    attestation_url: str,
    *,
    endpoint: str,
    bucket: str,
    prefix: str,
) -> str:
    """Resolve one validated evidence URL to this exact S3 object key."""
    try:
        key = release_publish.ci_attestation_object_key(attestation_url)
    except release_publish.ReleaseError as error:
        raise RetentionError(f"release evidence attestation URL is invalid: {error}") from error

    parsed_url = urllib.parse.urlsplit(attestation_url)
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
            "release evidence attestation URL does not use the configured S3 endpoint"
        )
    expected_path = f"/{bucket}/{key}"
    if parsed_url.path != expected_path or not key.startswith(prefix) or not key.endswith(
        "/attestations.json"
    ):
        raise RetentionError(
            "release evidence attestation URL is outside the CI attestation prefix"
        )
    return key


def protected_attestation_keys(
    evidence_root: Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    bucket: str = DEFAULT_BUCKET,
    prefix: str = DEFAULT_PREFIX,
) -> set[str]:
    """Return attestation keys held by every valid release-evidence record."""
    if not prefix.endswith("/"):
        raise RetentionError("attestation prefix must end with /")
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
        protected.add(
            _attestation_key_from_url(
                record["ci"]["attestation_url"],
                endpoint=endpoint,
                bucket=bucket,
                prefix=prefix,
            )
        )
    return protected


def prune_attestations(
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
    retention_holds_path: Path = retention_holds.DEFAULT_HOLDS_PATH,
) -> tuple[int, datetime]:
    """Delete expired, unheld CI attestations, preserving the exact cutoff."""
    _validate_retention(retention_days)
    if not prefix.endswith("/"):
        raise RetentionError("attestation prefix must end with /")
    protected = protected_attestation_keys(
        evidence_root,
        endpoint=endpoint,
        bucket=bucket,
        prefix=prefix,
    )
    current = _utc_now(now)
    protected.update(
        retention_holds.active_hold_keys(
            retention_holds_path,
            endpoint=endpoint,
            bucket=bucket,
            now=current,
        )
    )
    cutoff = current - timedelta(days=retention_days)
    expired = retention_holds.expired_candidate_keys(
        reader.list_objects(prefix),
        cutoff=cutoff,
        filename="attestations.json",
        held_keys=protected,
    )
    if not dry_run:
        publisher.delete_objects(expired)
    return len(expired), cutoff


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RetentionError(f"{name} is required; provide it through the environment")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune expired, unheld brand-kit CI attestation objects."
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
        "--retention-holds",
        type=Path,
        default=retention_holds.DEFAULT_HOLDS_PATH,
        help="versioned manifest of exact Garage objects under retention hold",
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
        pruned, _ = prune_attestations(
            reader,
            publisher,
            evidence_root=args.release_evidence_root,
            endpoint=args.endpoint,
            bucket=args.bucket,
            prefix=args.prefix,
            retention_days=args.retention_days,
            now=now,
            dry_run=args.dry_run,
            retention_holds_path=args.retention_holds,
        )
    except (RetentionError, ValueError) as error:
        print(f"ERROR  CI attestation retention cleanup did not complete: {error}", file=sys.stderr)
        return 2
    action = "WOULD_PRUNE" if args.dry_run else "PRUNED"
    print(
        f"{action}  {pruned} unheld CI attestation(s) older than "
        f"{args.retention_days} days"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
