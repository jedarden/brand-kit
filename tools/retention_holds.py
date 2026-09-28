"""Shared Garage retention holds and deletion eligibility rules.

Manual holds live in ``retention-holds.json`` as exact Garage object URLs.
Release-evidence references are collected by their owning pruners and merged
with these manual holds before any cleanup or ambiguous-delete recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.parse


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOLDS_PATH = ROOT / "retention-holds.json"
DEFAULT_EVIDENCE_ROOT = ROOT / "release-evidence" / "v1"
SCHEMA = "brand-kit-retention-holds/v1"

FAILURE_WATCH_PREFIX = "failures/brand-kit-ci-failure-watch/v1/"
CI_ATTESTATION_PREFIX = "attestations/brand-kit-ci/v1/"
CONSUMER_DRIFT_PREFIX = "failures/brand-kit-consumer-drift/v1/"
_TARGETS = (
    (FAILURE_WATCH_PREFIX, "report.json"),
    (CI_ATTESTATION_PREFIX, "attestations.json"),
    (CONSUMER_DRIFT_PREFIX, "report.json"),
)
_WORKFLOW_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class RetentionError(RuntimeError):
    """Raised when a hold source or cleanup eligibility cannot be trusted."""


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_expiration(value: object, *, path: Path, index: int) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RetentionError(
            f"retention hold {index} in {path.name} has an invalid expires_at; "
            "use a UTC timestamp ending in Z or null"
        )
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as error:
        raise RetentionError(
            f"retention hold {index} in {path.name} has an invalid expires_at"
        ) from error


def _object_key_from_url(
    value: object,
    *,
    endpoint: str,
    bucket: str,
    path: Path,
    index: int,
) -> str:
    label = f"retention hold {index} in {path.name}"
    if not isinstance(value, str) or not value or value != value.strip():
        raise RetentionError(f"{label} URL must be a non-empty, unpadded string")
    parsed = urllib.parse.urlsplit(value)
    configured = urllib.parse.urlsplit(endpoint.rstrip("/"))
    if (
        parsed.scheme != configured.scheme
        or parsed.netloc != configured.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RetentionError(f"{label} URL does not use the configured Garage endpoint")

    base_path = configured.path.rstrip("/")
    bucket_path = f"{base_path}/{bucket}/" if base_path else f"/{bucket}/"
    if not parsed.path.startswith(bucket_path):
        raise RetentionError(f"{label} URL does not use the configured Garage bucket")
    encoded_key = parsed.path[len(bucket_path) :]
    key = urllib.parse.unquote(encoded_key)
    if not key or urllib.parse.quote(key, safe="/-_.~") != encoded_key:
        raise RetentionError(
            f"{label} URL must use the canonical, unencoded object key"
        )

    for prefix, filename in _TARGETS:
        if not key.startswith(prefix):
            continue
        relative = key[len(prefix) :].split("/")
        if (
            len(relative) == 2
            and _WORKFLOW_UID.fullmatch(relative[0])
            and relative[1] == filename
        ):
            return key
    raise RetentionError(
        f"{label} URL is not an exact failure-watch report, CI attestation, "
        "or consumer-drift report object"
    )


def active_hold_keys(
    holds_path: Path = DEFAULT_HOLDS_PATH,
    *,
    endpoint: str,
    bucket: str,
    now: datetime | None = None,
) -> set[str]:
    """Load and validate all manual holds, returning keys active at ``now``.

    Missing or malformed hold data fails closed before Garage is listed. A
    null expiration is indefinite; a hold expires exactly at ``expires_at``.
    """
    path = Path(holds_path)
    if not path.is_file():
        raise RetentionError(f"retention hold manifest is unavailable: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RetentionError(
            f"cannot read retention hold manifest {path}: {error}"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "holds"}
        or document.get("schema") != SCHEMA
        or not isinstance(document.get("holds"), list)
    ):
        raise RetentionError(
            f"retention hold manifest {path.name} must contain schema {SCHEMA!r} and holds"
        )

    current = _utc_now(now)
    active: set[str] = set()
    seen: set[str] = set()
    for index, hold in enumerate(document["holds"], start=1):
        if not isinstance(hold, dict) or set(hold) != {"url", "reason", "expires_at"}:
            raise RetentionError(
                f"retention hold {index} in {path.name} must contain only "
                "url, reason, and expires_at"
            )
        reason = hold.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise RetentionError(
                f"retention hold {index} in {path.name} needs a reason of 1 to 500 characters"
            )
        key = _object_key_from_url(
            hold.get("url"),
            endpoint=endpoint,
            bucket=bucket,
            path=path,
            index=index,
        )
        if key in seen:
            raise RetentionError(
                f"retention hold manifest {path.name} repeats object {key}"
            )
        seen.add(key)
        expires_at = _parse_expiration(hold.get("expires_at"), path=path, index=index)
        if expires_at is None or current < expires_at:
            active.add(key)
    return active


def release_evidence_hold_keys(
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    *,
    endpoint: str,
    bucket: str,
    prefix: str,
) -> set[str]:
    """Return exact Garage keys referenced by valid release evidence."""
    from tools import release_evidence

    root = Path(evidence_root)
    if not root.is_dir():
        raise RetentionError(f"release evidence directory is unavailable: {root}")
    held: set[str] = set()
    for path in sorted(root.glob("*.json")):
        if not path.is_file():
            raise RetentionError(f"release evidence is not a regular file: {path}")
        try:
            record = release_evidence.load_record(path)
        except (OSError, UnicodeError, release_evidence.EvidenceError) as error:
            raise RetentionError(
                f"cannot validate release evidence {path.name}: {error}"
            ) from error
        if prefix == CI_ATTESTATION_PREFIX:
            urls = (record["ci"]["attestation_url"],)
        elif prefix == CONSUMER_DRIFT_PREFIX:
            urls = (record["consumer_drift"]["report_url"],)
        else:
            urls = ()
        for url in urls:
            if url is not None:
                held.add(
                    _object_key_from_url(
                        url,
                        endpoint=endpoint,
                        bucket=bucket,
                        path=path,
                        index=1,
                    )
                )
    return held


def expired_candidate_keys(
    objects,
    *,
    cutoff: datetime,
    filename: str,
    held_keys: set[str],
) -> list[str]:
    """Select old matching objects while excluding every active hold."""
    return [
        item.key
        for item in objects
        if item.key.endswith(f"/{filename}")
        and item.last_modified < cutoff
        and item.key not in held_keys
    ]


def reconcile_ambiguous_delete(
    reader,
    *,
    prefix: str,
    attempted_keys: list[str],
    cutoff: datetime,
    filename: str,
    holds_path: Path = DEFAULT_HOLDS_PATH,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    endpoint: str,
    bucket: str,
    now: datetime | None = None,
) -> tuple[list, list[str]]:
    """Inspect an ambiguous delete without replaying it.

    Returns the attempted objects that still exist and the subset that remains
    eligible after a fresh hold check. The caller can record the observation;
    this helper never sends a delete request.
    """
    held = active_hold_keys(holds_path, endpoint=endpoint, bucket=bucket, now=now)
    held.update(
        release_evidence_hold_keys(
            evidence_root,
            endpoint=endpoint,
            bucket=bucket,
            prefix=prefix,
        )
    )
    objects = reader.list_objects(prefix)
    attempted = set(attempted_keys)
    survivors = [item for item in objects if item.key in attempted]
    eligible = expired_candidate_keys(
        survivors,
        cutoff=cutoff,
        filename=filename,
        held_keys=held,
    )
    return survivors, eligible
