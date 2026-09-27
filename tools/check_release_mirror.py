#!/usr/bin/env python3
"""Audit one Forgejo release tag against its read-only GitHub mirror.

This audit deliberately checks Git refs only.  Forgejo is the release
authority, so a GitHub Release is neither required nor consulted; the GitHub
repository is only expected to expose the exact peeled tag propagated by the
server-side mirror.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable


DEFAULT_CANONICAL_REPOSITORY = "https://git.ardenone.com/jedarden/brand-kit.git"
DEFAULT_MIRROR_REPOSITORY = "https://github.com/jedarden/brand-kit.git"
REPORT_SCHEMA = "brand-kit-release-mirror/v1"
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


class ReleaseMirrorError(RuntimeError):
    """A remote read or release-mirror input was invalid."""


GitExecutor = Callable[
    [list[str], Path | None, bool], subprocess.CompletedProcess[str]
]


def _safe_command(arguments: list[str]) -> str:
    """Keep remote URLs out of diagnostics."""
    displayed = []
    for argument in arguments:
        if argument.startswith(("http://", "https://", "ssh://")) or "@" in argument:
            displayed.append("[remote]")
        else:
            displayed.append(argument)
    return " ".join(displayed)


def _safe_detail(detail: str) -> str:
    """Remove embedded HTTP basic-auth values from Git's error text."""
    return re.sub(
        r"(https?://)([^/\s:@]+):([^@\s]+)@",
        r"\1[REDACTED]@",
        detail,
    )


def execute_git(
    arguments: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReleaseMirrorError(f"cannot run git {_safe_command(arguments)}: {error}") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {_safe_detail(detail[:300])}" if detail else ""
        raise ReleaseMirrorError(f"git {_safe_command(arguments)} failed{suffix}")
    return result


def _validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or not repository.strip() or any(
        character in repository for character in "\r\n"
    ):
        raise ReleaseMirrorError("repository URL must be a non-blank single-line value")
    return repository.strip()


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or tag != tag.strip() or not TAG_PATTERN.fullmatch(tag):
        raise ReleaseMirrorError(f"release tag must be an exact vX.Y.Z value, got {tag!r}")
    return tag


def validate_commit(commit: str) -> str:
    if not isinstance(commit, str) or not OBJECT_ID_PATTERN.fullmatch(commit):
        raise ReleaseMirrorError(f"release commit is not a full object ID: {commit!r}")
    return commit.lower()


def read_tag_commit(
    repository: str,
    tag: str,
    *,
    execute: GitExecutor = execute_git,
) -> str | None:
    """Read one peeled tag from a remote without fetching or changing it."""
    repository = _validate_repository(repository)
    validate_tag(tag)
    reference = f"refs/tags/{tag}^{{}}"
    result = execute(
        ["ls-remote", "--exit-code", repository, reference],
        None,
        False,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {_safe_detail(detail[:300])}" if detail else ""
        raise ReleaseMirrorError(
            f"cannot read {tag} from the mirror remote{suffix}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ReleaseMirrorError(
            f"git ls-remote returned {len(lines)} records for {reference}"
        )
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != reference:
        raise ReleaseMirrorError("git ls-remote returned an invalid peeled tag record")
    if not OBJECT_ID_PATTERN.fullmatch(fields[0]):
        raise ReleaseMirrorError("git ls-remote returned a malformed tag object ID")
    return fields[0].lower()


def _tag_observation(status: str, commit: str | None, reason: str) -> dict[str, Any]:
    return {"status": status, "commit": commit, "reason": reason}


def audit_expected_tag(
    tag: str,
    expected_commit: str,
    canonical_repository: str = DEFAULT_CANONICAL_REPOSITORY,
    mirror_repository: str = DEFAULT_MIRROR_REPOSITORY,
    *,
    execute: GitExecutor = execute_git,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare one expected Forgejo tag with the read-only GitHub mirror.

    A canonical mismatch is definitive and unhealthy.  A mirror tag that is
    absent or still points at another object is indeterminate because a
    server-side mirror may be propagating; it is never treated as permission
    to create or accept a GitHub Release.
    """
    tag = validate_tag(tag)
    expected_commit = validate_commit(expected_commit)
    canonical_repository = _validate_repository(canonical_repository)
    mirror_repository = _validate_repository(mirror_repository)
    if canonical_repository == mirror_repository:
        raise ReleaseMirrorError(
            "canonical and mirror repositories must be different"
        )

    try:
        canonical_commit = read_tag_commit(
            canonical_repository,
            tag,
            execute=execute,
        )
        mirror_commit = read_tag_commit(
            mirror_repository,
            tag,
            execute=execute,
        )
    except ReleaseMirrorError as error:
        report_status = "indeterminate"
        canonical_commit = locals().get("canonical_commit")
        mirror_commit = locals().get("mirror_commit")
        error_reason = str(error)
        canonical_observation = _tag_observation(
            "indeterminate",
            canonical_commit,
            error_reason,
        )
        mirror_observation = _tag_observation(
            "indeterminate",
            mirror_commit,
            error_reason,
        )
    else:
        if canonical_commit is None:
            report_status = "unhealthy"
            canonical_observation = _tag_observation(
                "missing",
                None,
                "canonical Forgejo tag is absent",
            )
        elif canonical_commit != expected_commit:
            report_status = "unhealthy"
            canonical_observation = _tag_observation(
                "mismatch",
                canonical_commit,
                "canonical Forgejo tag does not point at the expected release commit",
            )
        else:
            canonical_observation = _tag_observation(
                "match",
                canonical_commit,
                "canonical Forgejo tag points at the expected release commit",
            )

        if canonical_observation["status"] != "match":
            mirror_observation = _tag_observation(
                "not_evaluated",
                mirror_commit,
                "GitHub tag cannot authorize a release when the canonical tag is not exact",
            )
        elif mirror_commit is None:
            report_status = "indeterminate"
            mirror_observation = _tag_observation(
                "lagging",
                None,
                "read-only GitHub mirror has not exposed the expected tag yet",
            )
        elif mirror_commit != expected_commit:
            report_status = "unhealthy"
            mirror_observation = _tag_observation(
                "mismatch",
                mirror_commit,
                "read-only GitHub mirror tag does not point at the expected release commit",
            )
        else:
            report_status = "healthy"
            mirror_observation = _tag_observation(
                "match",
                mirror_commit,
                "read-only GitHub tag points at the expected release commit",
            )

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return {
        "schema": REPORT_SCHEMA,
        "status": report_status,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "tag": tag,
        "expected_commit": expected_commit,
        "canonical_repository": canonical_repository,
        "mirror_repository": mirror_repository,
        "release_authority": "Forgejo",
        "github_release_checked": False,
        "canonical_tag": canonical_observation,
        "mirror_tag": mirror_observation,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit one Forgejo release tag against its read-only GitHub mirror."
    )
    parser.add_argument("--tag", required=True, help="exact stable tag, for example v1.1.0")
    parser.add_argument(
        "--commit",
        required=True,
        help="full commit object ID expected behind the tag",
    )
    parser.add_argument("--canonical-repository", default=DEFAULT_CANONICAL_REPOSITORY)
    parser.add_argument("--mirror-repository", default=DEFAULT_MIRROR_REPOSITORY)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_expected_tag(
            args.tag,
            args.commit,
            args.canonical_repository,
            args.mirror_repository,
        )
    except ReleaseMirrorError as error:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "indeterminate",
            "tag": args.tag,
            "expected_commit": args.commit,
            "canonical_repository": args.canonical_repository,
            "mirror_repository": args.mirror_repository,
            "release_authority": "Forgejo",
            "github_release_checked": False,
            "error": str(error),
        }

    try:
        write_report(report, args.report)
    except OSError as error:
        print(f"error: cannot write release-mirror report: {error}", file=sys.stderr)
        return 2

    if report["status"] == "healthy":
        print(f"HEALTHY  Forgejo tag {args.tag} is mirrored exactly on GitHub")
        return 0
    if report["status"] == "unhealthy":
        print(
            f"UNHEALTHY  Forgejo tag {args.tag} is not the expected release tag",
            file=sys.stderr,
        )
        return 1
    print(
        f"INDETERMINATE  GitHub mirror has not been proven for {args.tag}; retry later",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
