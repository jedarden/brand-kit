#!/usr/bin/env python3
"""Check that the Forgejo repository and its GitHub push mirror agree.

The check only reads remote Git refs.  It compares every branch and tag in the
canonical Forgejo repository with the read-only GitHub mirror.  A mirror ref
that is behind the canonical ref is reported as ``stale``; unrelated histories
or mirror-only refs are ``divergent``.  The temporary repository used to prove
ancestry is local scratch state and is never pushed anywhere.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable

DEFAULT_CANONICAL_REPOSITORY = "https://git.ardenone.com/jedarden/brand-kit.git"
DEFAULT_MIRROR_REPOSITORY = "https://github.com/jedarden/brand-kit.git"
REPORT_SCHEMA = "brand-kit-mirror-health/v1"
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
REF_PREFIXES = {"branch": "refs/heads/", "tag": "refs/tags/"}


class MirrorHealthError(RuntimeError):
    """A remote read or ancestry check could not establish mirror health."""


GitExecutor = Callable[[list[str], Path | None, bool], subprocess.CompletedProcess[str]]
RefSets = dict[str, dict[str, str]]


def _safe_command(arguments: list[str]) -> str:
    """Keep remote URLs out of diagnostics, including accidental credentials."""
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
        raise MirrorHealthError(f"cannot run git {_safe_command(arguments)}: {error}") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {_safe_detail(detail[:300])}" if detail else ""
        raise MirrorHealthError(f"git {_safe_command(arguments)} failed{suffix}")
    return result


def _validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or not repository.strip() or any(
        character in repository for character in "\r\n"
    ):
        raise MirrorHealthError("repository URL must be a non-blank single-line value")
    return repository.strip()


def _parse_remote_refs(output: str, kind: str) -> dict[str, str]:
    if kind not in REF_PREFIXES:
        raise MirrorHealthError(f"unsupported remote ref kind: {kind}")
    prefix = REF_PREFIXES[kind]
    refs: dict[str, str] = {}
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2:
            raise MirrorHealthError(f"git ls-remote returned a malformed {kind} record")
        object_id, ref = fields
        if not OBJECT_ID_PATTERN.fullmatch(object_id):
            raise MirrorHealthError(f"git ls-remote returned a malformed object id for {ref}")
        if ref.endswith("^{}"):
            ref = ref[:-3]
            target = peeled
        else:
            target = direct
        if not ref.startswith(prefix) or ref == prefix:
            raise MirrorHealthError(f"git ls-remote returned an unexpected {kind} ref {ref!r}")
        normalized = object_id.lower()
        previous = target.get(ref)
        if previous is not None and previous != normalized:
            raise MirrorHealthError(f"git ls-remote returned conflicting values for {ref}")
        target[ref] = normalized
    refs.update(direct)
    refs.update(peeled)
    return refs


def fetch_remote_refs(
    repository: str,
    *,
    execute: GitExecutor = execute_git,
) -> RefSets:
    """Read all heads and tags from one remote without changing that remote."""
    repository = _validate_repository(repository)
    branches = _parse_remote_refs(
        execute(["ls-remote", "--heads", repository], None, True).stdout,
        "branch",
    )
    tags = _parse_remote_refs(
        execute(["ls-remote", "--tags", repository], None, True).stdout,
        "tag",
    )
    return {"branch": branches, "tag": tags}


def _check(
    kind: str,
    ref: str,
    status: str,
    *,
    canonical: str | None,
    mirror: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "status": status,
        "canonical": canonical,
        "mirror": mirror,
        "reason": reason,
    }


def compare_ref_sets(
    canonical: RefSets,
    mirror: RefSets,
    *,
    is_ancestor: Callable[[str, str, str, str], bool],
) -> list[dict[str, Any]]:
    """Compare ref sets, using ``is_ancestor`` only for unequal matching refs."""
    checks: list[dict[str, Any]] = []
    for kind in ("branch", "tag"):
        canonical_refs = canonical.get(kind, {})
        mirror_refs = mirror.get(kind, {})
        for ref in sorted(set(canonical_refs) | set(mirror_refs)):
            canonical_id = canonical_refs.get(ref)
            mirror_id = mirror_refs.get(ref)
            if canonical_id is None:
                checks.append(
                    _check(
                        kind,
                        ref,
                        "divergent",
                        canonical=None,
                        mirror=mirror_id,
                        reason="mirror contains a ref that is absent from canonical Forgejo",
                    )
                )
                continue
            if mirror_id is None:
                checks.append(
                    _check(
                        kind,
                        ref,
                        "missing",
                        canonical=canonical_id,
                        mirror=None,
                        reason="mirror does not contain the canonical ref",
                    )
                )
                continue
            if canonical_id == mirror_id:
                checks.append(
                    _check(
                        kind,
                        ref,
                        "match",
                        canonical=canonical_id,
                        mirror=mirror_id,
                        reason="canonical and mirror object ids agree",
                    )
                )
                continue

            try:
                mirror_is_behind = is_ancestor(mirror_id, canonical_id, kind, ref)
                canonical_is_behind = is_ancestor(canonical_id, mirror_id, kind, ref)
            except MirrorHealthError as error:
                checks.append(
                    _check(
                        kind,
                        ref,
                        "indeterminate",
                        canonical=canonical_id,
                        mirror=mirror_id,
                        reason=f"could not establish ref ancestry: {error}",
                    )
                )
                continue

            if mirror_is_behind:
                status = "stale"
                reason = "mirror ref is an ancestor of the canonical ref"
            elif canonical_is_behind:
                status = "divergent"
                reason = "mirror ref is ahead of the canonical ref"
            else:
                status = "divergent"
                reason = "canonical and mirror refs have unrelated history"
            checks.append(
                _check(
                    kind,
                    ref,
                    status,
                    canonical=canonical_id,
                    mirror=mirror_id,
                    reason=reason,
                )
            )
    return checks


class _AncestryResolver:
    """Fetch mismatched refs into a disposable bare repository for comparison."""

    def __init__(
        self,
        canonical_repository: str,
        mirror_repository: str,
        *,
        execute: GitExecutor,
    ) -> None:
        self.canonical_repository = canonical_repository
        self.mirror_repository = mirror_repository
        self.execute = execute
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.repository: Path | None = None
        self._fetched: dict[tuple[str, str], str] = {}

    def __enter__(self) -> "_AncestryResolver":
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="brand-kit-mirror-health-")
        self.repository = Path(self._temporary_directory.name) / "objects.git"
        self.execute(["init", "--bare", str(self.repository)], None, True)
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None
            self.repository = None

    def _fetch_commit(self, remote: str, label: str, ref: str) -> str:
        if self.repository is None:
            raise MirrorHealthError("ancestry resolver is not active")
        key = (label, ref)
        local_ref = self._fetched.get(key)
        if local_ref is None:
            digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()
            local_ref = f"refs/brand-kit-mirror-health/{label}/{digest}"
            self.execute(
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    remote,
                    f"{ref}:{local_ref}",
                ],
                self.repository,
                True,
            )
            self._fetched[key] = local_ref
        result = self.execute(
            ["rev-parse", f"{local_ref}^{{commit}}"],
            self.repository,
            True,
        )
        commit = result.stdout.strip().lower()
        if not OBJECT_ID_PATTERN.fullmatch(commit):
            raise MirrorHealthError(f"fetched {label} ref {ref} has no commit object")
        return commit

    def is_ancestor(self, older: str, newer: str, kind: str, ref: str) -> bool:
        remote_ref = REF_PREFIXES[kind] + ref.removeprefix(REF_PREFIXES[kind])
        canonical_commit = self._fetch_commit(
            self.canonical_repository,
            "canonical",
            remote_ref,
        )
        mirror_commit = self._fetch_commit(self.mirror_repository, "mirror", remote_ref)
        if canonical_commit not in {older.lower(), newer.lower()} or mirror_commit not in {
            older.lower(),
            newer.lower(),
        }:
            raise MirrorHealthError(f"fetched {kind} ref {ref} did not match ls-remote")
        result = self.execute(
            ["merge-base", "--is-ancestor", older.lower(), newer.lower()],
            self.repository,
            False,
        )
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or result.stdout.strip()
            raise MirrorHealthError(detail or "git merge-base could not compare refs")
        return result.returncode == 0


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_check(
    canonical_repository: str = DEFAULT_CANONICAL_REPOSITORY,
    mirror_repository: str = DEFAULT_MIRROR_REPOSITORY,
    *,
    now: datetime | None = None,
    execute: GitExecutor = execute_git,
) -> dict[str, Any]:
    canonical_repository = _validate_repository(canonical_repository)
    mirror_repository = _validate_repository(mirror_repository)
    if canonical_repository == mirror_repository:
        raise MirrorHealthError("canonical and mirror repositories must be different")

    canonical = fetch_remote_refs(canonical_repository, execute=execute)
    mirror = fetch_remote_refs(mirror_repository, execute=execute)
    with _AncestryResolver(
        canonical_repository,
        mirror_repository,
        execute=execute,
    ) as ancestry:
        checks = compare_ref_sets(canonical, mirror, is_ancestor=ancestry.is_ancestor)

    counts = Counter(check["status"] for check in checks)
    if counts["indeterminate"]:
        status = "indeterminate"
    elif counts["missing"] or counts["stale"] or counts["divergent"]:
        status = "unhealthy"
    else:
        status = "healthy"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "observed_at": _timestamp(now),
        "canonical_repository": canonical_repository,
        "mirror_repository": mirror_repository,
        "summary": {key: counts[key] for key in ("match", "missing", "stale", "divergent", "indeterminate")},
        "checks": checks,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check branch and tag parity between Forgejo and its GitHub mirror."
    )
    parser.add_argument("--canonical-repository", default=DEFAULT_CANONICAL_REPOSITORY)
    parser.add_argument("--mirror-repository", default=DEFAULT_MIRROR_REPOSITORY)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_check(args.canonical_repository, args.mirror_repository)
    except MirrorHealthError as error:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "indeterminate",
            "observed_at": _timestamp(),
            "canonical_repository": args.canonical_repository,
            "mirror_repository": args.mirror_repository,
            "summary": {"match": 0, "missing": 0, "stale": 0, "divergent": 0, "indeterminate": 1},
            "checks": [],
            "error": str(error),
        }

    try:
        write_report(report, args.report)
    except OSError as error:
        print(f"error: cannot write mirror-health report: {error}", file=sys.stderr)
        return 2

    if report["status"] == "healthy":
        print("HEALTHY  Forgejo branches and tags agree with the GitHub mirror")
        return 0
    if report["status"] == "unhealthy":
        print("UNHEALTHY  Forgejo and GitHub mirror refs need follow-up", file=sys.stderr)
        return 1
    print("INDETERMINATE  could not establish Forgejo/GitHub mirror health", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
