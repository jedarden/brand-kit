#!/usr/bin/env python3
"""Validate and persist credential-free evidence for a published release.

The resulting file is committed at release-evidence/v1/<tag>.json. It
contains identifiers, URLs, statuses, and digests only; release credentials
are deliberately not accepted by this tool or represented in the schema.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import urllib.parse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import release_publish


ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_ROOT = ROOT / "release-evidence" / "v1"
SCHEMA_VERSION = 1
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
CONSUMER_WORKFLOW_PATTERN = re.compile(
    r"^brand-kit-consumer-drift-[A-Za-z0-9][A-Za-z0-9.-]{0,62}$"
)
FORBIDDEN_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|cookie|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)
FORBIDDEN_VALUE_PATTERN = re.compile(
    r"(?:FORGEJO_TOKEN|ARGO_TOKEN|ARGO_SUBMIT_TOKEN|Bearer\s+|Authorization\s*:|"
    r"(?:token|secret|password|passwd|credential)\s*[=:])",
    re.IGNORECASE,
)


class EvidenceError(ValueError):
    """A release evidence record is malformed or unsafe to persist."""


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{name} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], name: str, required: set[str]) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing:
        raise EvidenceError(f"{name} is missing required fields: {', '.join(missing)}")
    if unexpected:
        raise EvidenceError(f"{name} has unexpected fields: {', '.join(unexpected)}")


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value


def _require_commit(value: Any, name: str) -> str:
    commit = _require_string(value, name)
    if not COMMIT_PATTERN.fullmatch(commit):
        raise EvidenceError(f"{name} must be a full hexadecimal object ID")
    return commit.lower()


def _require_tag(value: Any, name: str) -> str:
    try:
        return release_publish.validate_tag(_require_string(value, name))
    except release_publish.ReleaseError as error:
        raise EvidenceError(str(error)) from error


def _validate_https_url(value: Any, name: str) -> str:
    url = _require_string(value, name)
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EvidenceError(f"{name} must be an HTTPS URL without credentials or query data")
    return url


def _validate_forgejo_release_url(
    value: Any, name: str, repository: str, tag: str
) -> str:
    url = _validate_https_url(value, name)
    owner, repository_name = repository.split("/", 1)
    expected_path = "/".join(
        (
            "",
            urllib.parse.quote(owner, safe=""),
            urllib.parse.quote(repository_name, safe=""),
            "releases",
            "tag",
            urllib.parse.quote(tag, safe=""),
        )
    )
    parsed = urllib.parse.urlsplit(url)
    if parsed.path != expected_path:
        raise EvidenceError(
            f"{name} must identify {repository}/releases/tag/{tag}"
        )
    return url


def _reject_credentials(value: Any, location: str = "record") -> None:
    """Enforce the credential-free boundary independently of the field shape."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or FORBIDDEN_KEY_PATTERN.search(key):
                raise EvidenceError(f"{location} contains a credential-shaped field")
            _reject_credentials(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credentials(child, f"{location}[{index}]")
    elif isinstance(value, str) and FORBIDDEN_VALUE_PATTERN.search(value):
        raise EvidenceError(f"{location} contains credential material")


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and normalize one release evidence record."""
    _reject_credentials(record)
    root = _require_object(record, "record")
    _require_exact_keys(
        root,
        "record",
        {"schema_version", "release", "ci", "forgejo", "mirror", "consumer_drift"},
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise EvidenceError(f"schema_version must be {SCHEMA_VERSION}")

    release = _require_object(root["release"], "release")
    _require_exact_keys(release, "release", {"tag", "commit"})
    tag = _require_tag(release["tag"], "release.tag")
    commit = _require_commit(release["commit"], "release.commit")

    ci = _require_object(root["ci"], "ci")
    _require_exact_keys(
        ci,
        "ci",
        {
            "run_name",
            "attestation_url",
            "workflow_uid",
            "phase",
            "finished_at",
            "commit",
        },
    )
    run_name = _require_string(ci["run_name"], "ci.run_name")
    try:
        normalized_run_name = release_publish.argo_run_name(run_name)
    except release_publish.ReleaseError as error:
        raise EvidenceError(str(error)) from error
    if run_name != f"brand-kit-ci/{normalized_run_name}":
        raise EvidenceError("ci.run_name must include the brand-kit-ci/ prefix")
    try:
        attestation_url = release_publish.validate_ci_attestation_url(
            _require_string(ci["attestation_url"], "ci.attestation_url")
        )
    except release_publish.ReleaseError as error:
        raise EvidenceError(str(error)) from error
    workflow_uid = _require_string(ci["workflow_uid"], "ci.workflow_uid")
    if not release_publish.WORKFLOW_UID_PATTERN.fullmatch(workflow_uid):
        raise EvidenceError("ci.workflow_uid is malformed")
    if ci["phase"] != "Succeeded":
        raise EvidenceError("ci.phase must be Succeeded")
    try:
        finished_at = release_publish._attestation_timestamp(ci["finished_at"])
    except release_publish.ReleaseError as error:
        raise EvidenceError(str(error)) from error
    if _require_commit(ci["commit"], "ci.commit") != commit:
        raise EvidenceError("ci.commit must match release.commit")

    forgejo = _require_object(root["forgejo"], "forgejo")
    _require_exact_keys(
        forgejo,
        "forgejo",
        {"repository", "tag", "release_url", "target_commit", "published"},
    )
    repository = _require_string(forgejo["repository"], "forgejo.repository")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise EvidenceError("forgejo.repository must be owner/name")
    if _require_tag(forgejo["tag"], "forgejo.tag") != tag:
        raise EvidenceError("forgejo.tag must match release.tag")
    release_url = _validate_forgejo_release_url(
        forgejo["release_url"], "forgejo.release_url", repository, tag
    )
    if _require_commit(forgejo["target_commit"], "forgejo.target_commit") != commit:
        raise EvidenceError("forgejo.target_commit must match release.commit")
    if forgejo["published"] is not True:
        raise EvidenceError("forgejo.published must be true")

    mirror = _require_object(root["mirror"], "mirror")
    _require_exact_keys(
        mirror,
        "mirror",
        {
            "tag",
            "canonical_remote",
            "canonical_commit",
            "mirror_remote",
            "mirror_commit",
            "agrees",
        },
    )
    if _require_tag(mirror["tag"], "mirror.tag") != tag:
        raise EvidenceError("mirror.tag must match release.tag")
    if mirror["canonical_remote"] != "origin":
        raise EvidenceError("mirror.canonical_remote must be origin")
    if mirror["mirror_remote"] != "github":
        raise EvidenceError("mirror.mirror_remote must be github")
    if _require_commit(mirror["canonical_commit"], "mirror.canonical_commit") != commit:
        raise EvidenceError("mirror.canonical_commit must match release.commit")
    if _require_commit(mirror["mirror_commit"], "mirror.mirror_commit") != commit:
        raise EvidenceError("mirror.mirror_commit must match release.commit")
    if mirror["agrees"] is not True:
        raise EvidenceError("mirror.agrees must be true")

    consumer = _require_object(root["consumer_drift"], "consumer_drift")
    _require_exact_keys(
        consumer,
        "consumer_drift",
        {"release_tag", "workflow_name", "submitted", "status", "report_url"},
    )
    if _require_tag(consumer["release_tag"], "consumer_drift.release_tag") != tag:
        raise EvidenceError("consumer_drift.release_tag must match release.tag")
    if consumer["submitted"] is not True:
        raise EvidenceError("consumer_drift.submitted must be true")
    workflow_name = _require_string(
        consumer["workflow_name"], "consumer_drift.workflow_name"
    )
    if not CONSUMER_WORKFLOW_PATTERN.fullmatch(workflow_name):
        raise EvidenceError("consumer_drift.workflow_name is malformed")
    if consumer["status"] not in {
        "submitted",
        "passed",
        "drift",
        "indeterminate",
        "failed",
    }:
        raise EvidenceError("consumer_drift.status is not a supported handoff status")
    report_url = consumer["report_url"]
    if report_url is not None:
        report_url = _validate_https_url(report_url, "consumer_drift.report_url")

    return {
        "schema_version": SCHEMA_VERSION,
        "release": {"tag": tag, "commit": commit},
        "ci": {
            "run_name": run_name,
            "attestation_url": attestation_url,
            "workflow_uid": workflow_uid,
            "phase": "Succeeded",
            "finished_at": finished_at,
            "commit": commit,
        },
        "forgejo": {
            "repository": repository,
            "tag": tag,
            "release_url": release_url,
            "target_commit": commit,
            "published": True,
        },
        "mirror": {
            "tag": tag,
            "canonical_remote": "origin",
            "canonical_commit": commit,
            "mirror_remote": "github",
            "mirror_commit": commit,
            "agrees": True,
        },
        "consumer_drift": {
            "release_tag": tag,
            "workflow_name": workflow_name,
            "submitted": True,
            "status": consumer["status"],
            "report_url": report_url,
        },
    }


def build_record(
    *,
    tag: str,
    commit: str,
    ci_run: str,
    ci_attestation_url: str,
    ci_workflow_uid: str,
    ci_finished_at: str,
    forgejo_release_url: str,
    consumer_workflow: str,
    mirror_commit: str,
    consumer_status: str = "submitted",
    consumer_report_url: str | None = None,
    forgejo_repository: str = release_publish.DEFAULT_REPOSITORY,
) -> dict[str, Any]:
    """Build a record from the non-secret outputs of the release procedure."""
    try:
        normalized_run = release_publish.argo_run_name(ci_run)
    except release_publish.ReleaseError as error:
        raise EvidenceError(str(error)) from error
    return validate_record(
        {
            "schema_version": SCHEMA_VERSION,
            "release": {"tag": tag, "commit": commit},
            "ci": {
                "run_name": f"brand-kit-ci/{normalized_run}",
                "attestation_url": ci_attestation_url,
                "workflow_uid": ci_workflow_uid,
                "phase": "Succeeded",
                "finished_at": ci_finished_at,
                "commit": commit,
            },
            "forgejo": {
                "repository": forgejo_repository,
                "tag": tag,
                "release_url": forgejo_release_url,
                "target_commit": commit,
                "published": True,
            },
            "mirror": {
                "tag": tag,
                "canonical_remote": "origin",
                "canonical_commit": commit,
                "mirror_remote": "github",
                "mirror_commit": mirror_commit,
                "agrees": True,
            },
            "consumer_drift": {
                "release_tag": tag,
                "workflow_name": consumer_workflow,
                "submitted": True,
                "status": consumer_status,
                "report_url": consumer_report_url,
            },
        }
    )


def evidence_path(tag: str, root: Path = EVIDENCE_ROOT) -> Path:
    """Return the versioned path for a release tag."""
    return root / f"{_require_tag(tag, 'release.tag')}.json"


def load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot read release evidence {path}: {error}") from error
    return validate_record(value)


def write_record(record: dict[str, Any], path: Path) -> Path:
    """Persist one immutable record, atomically and without credentials."""
    normalized = validate_record(record)
    expected_path = evidence_path(normalized["release"]["tag"], path.parent)
    if path.name != expected_path.name:
        raise EvidenceError(
            f"release evidence path must be named {expected_path.name}, got {path.name}"
        )
    if path.exists():
        if load_record(path) != normalized:
            raise EvidenceError(
                f"release evidence already exists with different contents: {path}"
            )
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist one credential-free release evidence record."
    )
    parser.add_argument("--tag", required=True, help="exact release tag, for example v1.2.0")
    parser.add_argument("--commit", required=True, help="full release commit SHA")
    parser.add_argument("--ci-run", required=True, help="brand-kit-ci workflow name")
    parser.add_argument(
        "--ci-attestation-url", required=True, help="durable CI attestation URL"
    )
    parser.add_argument(
        "--ci-workflow-uid", required=True, help="workflow UID from the attestation"
    )
    parser.add_argument(
        "--ci-finished-at", required=True, help="timezone-qualified attestation time"
    )
    parser.add_argument(
        "--forgejo-release-url", required=True, help="published Forgejo Release URL"
    )
    parser.add_argument(
        "--forgejo-repository", default=release_publish.DEFAULT_REPOSITORY
    )
    parser.add_argument(
        "--consumer-workflow",
        required=True,
        help="submitted consumer-drift workflow name",
    )
    parser.add_argument(
        "--mirror-commit",
        required=True,
        help="peeled release-tag commit observed on the read-only github remote",
    )
    parser.add_argument(
        "--consumer-status",
        default="submitted",
        choices=("submitted", "passed", "drift", "indeterminate", "failed"),
    )
    parser.add_argument(
        "--consumer-report-url", help="durable consumer-drift report URL"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="record path; defaults to release-evidence/v1/<tag>.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = build_record(
            tag=args.tag,
            commit=args.commit,
            ci_run=args.ci_run,
            ci_attestation_url=args.ci_attestation_url,
            ci_workflow_uid=args.ci_workflow_uid,
            ci_finished_at=args.ci_finished_at,
            forgejo_release_url=args.forgejo_release_url,
            consumer_workflow=args.consumer_workflow,
            mirror_commit=args.mirror_commit,
            consumer_status=args.consumer_status,
            consumer_report_url=args.consumer_report_url,
            forgejo_repository=args.forgejo_repository,
        )
        path = args.output or evidence_path(args.tag)
        write_record(record, path)
    except EvidenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"RECORDED  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
