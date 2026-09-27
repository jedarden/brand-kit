#!/usr/bin/env python3
"""Create or verify a published Forgejo release for an already-pushed tag."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_URL = "https://git.ardenone.com/api/v1"
DEFAULT_REPOSITORY = "jedarden/brand-kit"
DEFAULT_ORIGIN_REMOTE = "origin"
DEFAULT_MIRROR_REMOTE = "github"
DEFAULT_ARGO_API_URL = "https://argo-ci.ardenone.com"
DEFAULT_ARGO_NAMESPACE = "argo-workflows"
DEFAULT_ARGO_WORKFLOW_TEMPLATE = "brand-kit-ci"
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
CHANGELOG_HEADING_PATTERN = re.compile(
    r"^## \[(?P<tag>v[0-9]+\.[0-9]+\.[0-9]+)\]"
    r"(?: - (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2}))?$"
)
CHANGELOG_HEADING_TAG_PREFIX = re.compile(
    r"^##[ \t]+\[?(?P<tag>v[0-9]+\.[0-9]+\.[0-9]+)"
)
WORKFLOW_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
WORKFLOW_RUN_PREFIX = f"{DEFAULT_ARGO_WORKFLOW_TEMPLATE}/"
COMMIT_PARAMETER_NAMES = {
    "commit",
    "revision",
    "sha",
    "gitcommit",
    "gitsha",
    "headcommit",
    "headsha",
    "releasecommit",
}


class ReleaseError(RuntimeError):
    """A release precondition or operation failed."""


class HttpFailure(ReleaseError):
    """An HTTP request failed with a response from the Forgejo API."""

    def __init__(self, status: int, detail: str, service: str = "Forgejo API"):
        self.status = status
        self.detail = detail
        self.service = service
        super().__init__(f"{service} returned HTTP {status}: {detail}")


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or tag != tag.strip() or not TAG_PATTERN.fullmatch(tag):
        raise ReleaseError(f"release tag must be an exact vX.Y.Z value, got {tag!r}")
    return tag


def run_git(arguments: list[str], root: Path = ROOT) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReleaseError(f"cannot run git {' '.join(arguments)}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"git {' '.join(arguments)} failed{suffix}")
    return result.stdout.strip()


def run_git_optional(arguments: list[str], root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReleaseError(f"cannot run git {' '.join(arguments)}: {error}") from error


def local_tag_info(tag: str, root: Path = ROOT) -> str:
    validate_tag(tag)
    reference = f"refs/tags/{tag}"
    object_type = run_git(["cat-file", "-t", reference], root)
    if object_type != "tag":
        raise ReleaseError(f"{tag} must be an annotated tag, not a {object_type}")
    commit = run_git(["rev-parse", "--verify", f"{reference}^{{commit}}"], root)
    head = run_git(["rev-parse", "--verify", "HEAD^{commit}"], root)
    if commit != head:
        raise ReleaseError(
            f"{tag} points to {commit}, but HEAD is {head}; check out the exact release tag"
        )
    return commit


def remote_url(remote: str, root: Path = ROOT) -> str:
    return run_git(["remote", "get-url", remote], root)


def remote_tag_commit(
    remote: str,
    tag: str,
    root: Path = ROOT,
) -> str | None:
    validate_tag(tag)
    reference = f"refs/tags/{tag}^{{}}"
    result = run_git_optional(
        ["ls-remote", "--exit-code", remote, reference],
        root,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(f"cannot read {tag} from remote {remote}: {detail}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ReleaseError(f"remote {remote} returned {len(lines)} records for {tag}")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != reference:
        raise ReleaseError(f"remote {remote} returned an invalid record for {tag}")
    return fields[0]


def validate_remote_roles(
    origin_remote: str,
    mirror_remote: str,
    root: Path = ROOT,
) -> tuple[str, str]:
    if origin_remote == mirror_remote:
        raise ReleaseError("origin and mirror remotes must be different")
    origin = remote_url(origin_remote, root)
    mirror = remote_url(mirror_remote, root)
    if origin == mirror:
        raise ReleaseError("origin and mirror remotes must point to different repositories")
    return origin, mirror


def wait_for_mirror(
    tag: str,
    commit: str,
    origin_remote: str = DEFAULT_ORIGIN_REMOTE,
    mirror_remote: str = DEFAULT_MIRROR_REMOTE,
    root: Path = ROOT,
    timeout: float = 300.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    validate_tag(tag)
    if timeout < 0 or interval < 0:
        raise ReleaseError("mirror timeout and interval must not be negative")
    deadline = time.monotonic() + timeout
    last_mirror = "not visible"
    while True:
        origin_commit = remote_tag_commit(origin_remote, tag, root)
        if origin_commit is None:
            raise ReleaseError(
                f"{tag} is not pushed to the canonical origin remote {origin_remote}"
            )
        if origin_commit != commit:
            raise ReleaseError(
                f"origin {origin_remote} has {tag} at {origin_commit}, expected {commit}"
            )
        mirror_commit = remote_tag_commit(mirror_remote, tag, root)
        if mirror_commit == commit:
            return
        last_mirror = "not visible" if mirror_commit is None else mirror_commit
        if time.monotonic() >= deadline:
            raise ReleaseError(
                f"mirror {mirror_remote} has not caught up with {tag} at {commit} "
                f"(last observed: {last_mirror})"
            )
        sleep(min(interval, max(0.0, deadline - time.monotonic())))


def repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ReleaseError(f"repository must be owner/name, got {repository!r}")
    return urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe="")


def releases_url(api_url: str, repository: str) -> str:
    owner, name = repository_parts(repository)
    return f"{api_url.rstrip('/')}/repos/{owner}/{name}/releases"


def release_url(api_url: str, repository: str, tag: str) -> str:
    owner, name = repository_parts(repository)
    return (
        f"{api_url.rstrip('/')}/repos/{owner}/{name}/releases/tags/"
        f"{urllib.parse.quote(validate_tag(tag), safe='')}"
    )


def release_id_url(api_url: str, repository: str, release_id: str) -> str:
    owner, name = repository_parts(repository)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", release_id):
        raise ReleaseError("Forgejo release id is malformed")
    return f"{api_url.rstrip('/')}/repos/{owner}/{name}/releases/{release_id}"


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 20.0,
    authorization_scheme: str = "token",
    service: str = "Forgejo API",
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "brand-kit-release-publisher"}
    if token:
        headers["Authorization"] = f"{authorization_scheme} {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise HttpFailure(
            error.code,
            detail or error.reason or f"{service} request failed",
            service=service,
        ) from error
    except urllib.error.URLError as error:
        raise ReleaseError(f"cannot reach {service}: {error.reason}") from error
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReleaseError(f"{service} returned invalid JSON: {error}") from error


def validate_release_record(
    record: Any,
    tag: str,
    allow_draft: bool = False,
    expected_commit: str | None = None,
    expected_notes: str | None = None,
) -> dict[str, Any]:
    validate_tag(tag)
    if not isinstance(record, dict):
        raise ReleaseError(f"Forgejo release record for {tag} is not an object")
    if record.get("tag_name") != tag:
        raise ReleaseError(
            f"Forgejo release names {record.get('tag_name')!r}, expected {tag!r}"
        )
    if record.get("prerelease") is not False:
        raise ReleaseError(f"Forgejo release {tag} is not a stable published release")
    draft = record.get("draft")
    if draft is not False and not (allow_draft and draft is True):
        raise ReleaseError(f"Forgejo release {tag} is not published")
    target_commit = record.get("target_commitish")
    if not isinstance(target_commit, str) or not OBJECT_ID_PATTERN.fullmatch(target_commit):
        raise ReleaseError(
            f"Forgejo release {tag} has no verifiable target commit object ID"
        )
    if expected_commit is not None:
        if not OBJECT_ID_PATTERN.fullmatch(expected_commit):
            raise ReleaseError(f"release commit is not a full object ID: {expected_commit!r}")
        if target_commit.lower() != expected_commit.lower():
            raise ReleaseError(
                f"Forgejo release {tag} targets {target_commit}, "
                f"expected {expected_commit}"
            )
    if expected_notes is not None:
        body = record.get("body")
        if not isinstance(body, str) or body != expected_notes:
            raise ReleaseError(
                f"Forgejo release {tag} notes do not exactly match CHANGELOG.md"
            )
    return record


def get_release(
    tag: str,
    api_url: str,
    repository: str,
    token: str | None = None,
    allow_draft: bool = False,
    expected_commit: str | None = None,
    expected_notes: str | None = None,
) -> dict[str, Any] | None:
    try:
        record = request_json("GET", release_url(api_url, repository, tag), token=token)
    except HttpFailure as error:
        if error.status == 404:
            return None
        raise ReleaseError(f"cannot read Forgejo release {tag}: {error}") from error
    return validate_release_record(
        record,
        tag,
        allow_draft=allow_draft,
        expected_commit=expected_commit,
        expected_notes=expected_notes,
    )


def argo_run_name(ci_run: str) -> str:
    """Validate and normalize the operator-supplied Argo workflow name."""
    if not isinstance(ci_run, str) or ci_run != ci_run.strip() or not ci_run:
        raise ReleaseError("Argo CI run identifier must not be empty or padded")
    if ci_run.startswith(WORKFLOW_RUN_PREFIX):
        ci_run = ci_run[len(WORKFLOW_RUN_PREFIX) :]
    elif "/" in ci_run:
        raise ReleaseError(
            "Argo CI run identifier must be a workflow name or brand-kit-ci/<workflow-name>"
        )
    if not WORKFLOW_NAME_PATTERN.fullmatch(ci_run):
        raise ReleaseError(f"Argo CI workflow name is malformed: {ci_run!r}")
    return ci_run


def argo_workflow_url(api_url: str, namespace: str, ci_run: str) -> str:
    """Build the read-only Argo Workflow API URL for a run."""
    run_name = argo_run_name(ci_run)
    if not isinstance(namespace, str) or not WORKFLOW_NAME_PATTERN.fullmatch(namespace):
        raise ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return (
        f"{base}/workflows/{urllib.parse.quote(namespace, safe='')}/"
        f"{urllib.parse.quote(run_name, safe='')}"
    )


def get_argo_workflow(
    ci_run: str,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    token: str | None = None,
) -> dict[str, Any]:
    """Read one Argo Workflow record without submitting or mutating a run."""
    run_name = argo_run_name(ci_run)
    try:
        workflow = request_json(
            "GET",
            argo_workflow_url(api_url, namespace, run_name),
            token=token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except HttpFailure as error:
        if error.status == 404:
            raise ReleaseError(f"Argo CI run {ci_run} was not found") from error
        raise ReleaseError(f"cannot read Argo CI run {ci_run}: {error}") from error
    except ReleaseError as error:
        raise ReleaseError(f"cannot read Argo CI run {ci_run}: {error}") from error
    if not isinstance(workflow, dict):
        raise ReleaseError(f"Argo CI run {ci_run} returned a non-object record")
    return workflow


def _normalized_parameter_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"[-_]", "", name).lower()


def _named_values(value: Any) -> list[tuple[str, Any]]:
    """Read Argo's list-shaped or map-shaped parameter representation."""
    if isinstance(value, list):
        return [
            (item.get("name", ""), item.get("value"))
            for item in value
            if isinstance(item, dict)
        ]
    if isinstance(value, dict):
        named: list[tuple[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                name = item.get("name", key)
                if not isinstance(name, str):
                    name = key if isinstance(key, str) else ""
                named.append((name, item.get("value")))
            else:
                named.append((key, item))
        return named
    return []


def _attested_commit_candidates(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    def collect(location: str, parameters: Any) -> None:
        for name, value in _named_values(parameters):
            if _normalized_parameter_name(name) not in COMMIT_PARAMETER_NAMES:
                continue
            if not isinstance(value, str) or not OBJECT_ID_PATTERN.fullmatch(value):
                raise ReleaseError(
                    f"Argo CI attestation field {location}.{name or '<unnamed>'} "
                    "is not a full commit object ID"
                )
            candidates.append((f"{location}.{name}", value.lower()))

    status = workflow.get("status")
    if isinstance(status, dict):
        outputs = status.get("outputs")
        if isinstance(outputs, dict):
            collect("status.outputs.parameters", outputs.get("parameters"))
        collect("status.parameters", status.get("parameters"))
        nodes = status.get("nodes")
        if isinstance(nodes, dict):
            for node_name, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                node_outputs = node.get("outputs")
                node_inputs = node.get("inputs")
                output_parameters = (
                    node_outputs.get("parameters")
                    if isinstance(node_outputs, dict)
                    else None
                )
                input_parameters = (
                    node_inputs.get("parameters")
                    if isinstance(node_inputs, dict)
                    else None
                )
                collect(
                    f"status.nodes.{node_name}.outputs.parameters",
                    output_parameters,
                )
                collect(
                    f"status.nodes.{node_name}.inputs.parameters",
                    input_parameters,
                )

    spec = workflow.get("spec")
    if isinstance(spec, dict):
        arguments = spec.get("arguments")
        if isinstance(arguments, dict):
            collect("spec.arguments.parameters", arguments.get("parameters"))

    metadata = workflow.get("metadata")
    if isinstance(metadata, dict):
        collect("metadata.labels", metadata.get("labels"))
        collect("metadata.annotations", metadata.get("annotations"))

    return candidates


def attest_argo_ci_run(
    ci_run: str,
    release_commit: str,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    token: str | None = None,
) -> dict[str, Any]:
    """Require a read-only, successful Argo attestation for ``release_commit``."""
    run_name = argo_run_name(ci_run)
    if not isinstance(release_commit, str) or not OBJECT_ID_PATTERN.fullmatch(release_commit):
        raise ReleaseError(f"release commit is not a full object ID: {release_commit!r}")
    workflow = get_argo_workflow(
        run_name,
        api_url=api_url,
        namespace=namespace,
        token=token,
    )

    metadata = workflow.get("metadata")
    if isinstance(metadata, dict):
        returned_name = metadata.get("name")
        if returned_name is not None and returned_name != run_name:
            raise ReleaseError(
                f"Argo returned workflow {returned_name!r}, expected {run_name!r}"
            )
        labels = metadata.get("labels")
        if isinstance(labels, dict):
            template = labels.get("workflows.argoproj.io/workflow-template")
            if template is not None and template != DEFAULT_ARGO_WORKFLOW_TEMPLATE:
                raise ReleaseError(
                    f"Argo run {ci_run} uses workflow template {template!r}, "
                    f"expected {DEFAULT_ARGO_WORKFLOW_TEMPLATE!r}"
                )

    status = workflow.get("status")
    if not isinstance(status, dict):
        raise ReleaseError(f"Argo CI run {ci_run} has no status record")
    phase = status.get("phase")
    if phase != "Succeeded":
        raise ReleaseError(
            f"Argo CI run {ci_run} has phase {phase!r}; only Succeeded is accepted"
        )

    candidates = _attested_commit_candidates(workflow)
    observed = {commit for _, commit in candidates}
    if not observed:
        raise ReleaseError(
            f"Argo CI run {ci_run} has no structured full-commit attestation"
        )
    if len(observed) != 1:
        details = ", ".join(f"{location}={commit}" for location, commit in candidates)
        raise ReleaseError(f"Argo CI run {ci_run} has conflicting commit attestations: {details}")
    attested_commit = next(iter(observed))
    if attested_commit != release_commit.lower():
        raise ReleaseError(
            f"Argo CI run {ci_run} attests commit {attested_commit}, "
            f"expected release commit {release_commit.lower()}"
        )
    return workflow


def release_notes_from_changelog(path: Path, tag: str) -> str:
    validate_tag(tag)
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"cannot read release notes {path}: {error}") from error

    headings = list(re.finditer(r"^##[ \t]+.*$", contents, re.MULTILINE))
    matching: list[re.Match[str]] = []
    for heading in headings:
        heading_text = heading.group(0).rstrip("\r")
        version_prefix = CHANGELOG_HEADING_TAG_PREFIX.match(heading_text)
        if version_prefix is None or version_prefix.group("tag") != tag:
            continue
        if CHANGELOG_HEADING_PATTERN.fullmatch(heading_text) is None:
            raise ReleaseError(
                f"{path} has a malformed release heading for {tag}: {heading_text!r}"
            )
        parsed = CHANGELOG_HEADING_PATTERN.fullmatch(heading_text)
        assert parsed is not None
        release_date = parsed.group("date")
        if release_date is not None:
            try:
                date.fromisoformat(release_date)
            except ValueError as error:
                raise ReleaseError(
                    f"{path} has an invalid release date for {tag}: {release_date!r}"
                ) from error
        matching.append(heading)

    if not matching:
        available = [
            parsed.group("tag")
            for heading in headings
            if (parsed := CHANGELOG_HEADING_PATTERN.fullmatch(heading.group(0).rstrip("\r")))
        ]
        suffix = f"; available release sections: {', '.join(available)}" if available else ""
        raise ReleaseError(f"{path} has no '## [{tag}]' release section{suffix}")
    if len(matching) != 1:
        raise ReleaseError(f"{path} has multiple release sections for {tag}")

    match = matching[0]
    heading_index = headings.index(match)
    start = match.end()
    end = headings[heading_index + 1].start() if heading_index + 1 < len(headings) else len(contents)
    notes = contents[start:end].strip()
    if not notes:
        raise ReleaseError(f"the {path} section for {tag} is empty")
    return notes


def publish_release(
    tag: str,
    commit: str,
    notes: str,
    api_url: str = DEFAULT_API_URL,
    repository: str = DEFAULT_REPOSITORY,
    token: str | None = None,
) -> dict[str, Any]:
    validate_tag(tag)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ReleaseError(f"release commit is not a full object ID: {commit!r}")
    if not isinstance(notes, str) or not notes.strip():
        raise ReleaseError("release notes must not be empty")
    if not token:
        raise ReleaseError("FORGEJO_TOKEN is required to create or publish a release")

    existing = get_release(
        tag,
        api_url,
        repository,
        token,
        allow_draft=True,
        expected_commit=commit,
        expected_notes=notes,
    )
    if existing is None:
        payload = {
            "tag_name": tag,
            "target_commitish": commit,
            "name": tag,
            "body": notes,
            "draft": False,
            "prerelease": False,
        }
        try:
            record = request_json(
                "POST",
                releases_url(api_url, repository),
                payload=payload,
                token=token,
            )
        except HttpFailure as error:
            if error.status != 409:
                raise ReleaseError(f"cannot create Forgejo release {tag}: {error}") from error
            record = get_release(
                tag,
                api_url,
                repository,
                token,
                allow_draft=True,
                expected_commit=commit,
                expected_notes=notes,
            )
            if record is None:
                raise ReleaseError(f"Forgejo reported a conflict for {tag}, but no record exists")
        validate_release_record(
            record,
            tag,
            expected_commit=commit,
            expected_notes=notes,
        )
    elif existing.get("draft") is True:
        release_id = existing.get("id")
        if not isinstance(release_id, (str, int)):
            raise ReleaseError(f"draft Forgejo release {tag} has no usable id")
        record = request_json(
            "PATCH",
            release_id_url(api_url, repository, str(release_id)),
            payload={"draft": False},
            token=token,
        )
        validate_release_record(
            record,
            tag,
            expected_commit=commit,
            expected_notes=notes,
        )
    else:
        record = existing

    final = get_release(
        tag,
        api_url,
        repository,
        token,
        expected_commit=commit,
        expected_notes=notes,
    )
    if final is None:
        raise ReleaseError(f"Forgejo release {tag} disappeared after publication")
    return final


def verify_ready(
    tag: str,
    origin_remote: str = DEFAULT_ORIGIN_REMOTE,
    mirror_remote: str = DEFAULT_MIRROR_REMOTE,
    root: Path = ROOT,
    timeout: float = 300.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    commit = local_tag_info(tag, root)
    validate_remote_roles(origin_remote, mirror_remote, root)
    wait_for_mirror(
        tag,
        commit,
        origin_remote=origin_remote,
        mirror_remote=mirror_remote,
        root=root,
        timeout=timeout,
        interval=interval,
        sleep=sleep,
    )
    return commit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish and verify a stable Forgejo release from an existing annotated tag."
    )
    parser.add_argument("--tag", required=True, help="exact stable tag, for example v1.1.0")
    parser.add_argument(
        "--ci-run",
        required=True,
        help="brand-kit-ci Argo workflow name (or brand-kit-ci/<workflow-name>)",
    )
    parser.add_argument(
        "--argo-api-url",
        default=DEFAULT_ARGO_API_URL,
        help=f"Argo Workflows API base URL (default: {DEFAULT_ARGO_API_URL})",
    )
    parser.add_argument(
        "--argo-namespace",
        default=DEFAULT_ARGO_NAMESPACE,
        help=f"Argo namespace (default: {DEFAULT_ARGO_NAMESPACE})",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an already-published release without writing to Forgejo",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Forgejo API base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help=f"Forgejo repository (default: {DEFAULT_REPOSITORY})",
    )
    parser.add_argument(
        "--origin-remote",
        default=DEFAULT_ORIGIN_REMOTE,
        help=f"canonical tag remote (default: {DEFAULT_ORIGIN_REMOTE})",
    )
    parser.add_argument(
        "--mirror-remote",
        default=DEFAULT_MIRROR_REMOTE,
        help=f"read-only tag mirror remote (default: {DEFAULT_MIRROR_REMOTE})",
    )
    parser.add_argument(
        "--notes",
        help="compatibility input; must exactly match the CHANGELOG.md section",
    )
    parser.add_argument(
        "--notes-file",
        type=Path,
        default=ROOT / "CHANGELOG.md",
        help="changelog used to extract release notes (default: CHANGELOG.md)",
    )
    parser.add_argument(
        "--mirror-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for the mirror (default: 300)",
    )
    parser.add_argument(
        "--mirror-interval",
        type=float,
        default=5.0,
        help="seconds between mirror checks (default: 5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    token = os.environ.get("FORGEJO_TOKEN") or os.environ.get("FORGEJO_API_TOKEN")
    argo_token = os.environ.get("ARGO_TOKEN") or os.environ.get("ARGO_API_TOKEN")
    try:
        changelog_notes = release_notes_from_changelog(args.notes_file, args.tag)
        if args.notes is not None and args.notes != changelog_notes:
            raise ReleaseError(
                "--notes must exactly match the release section in CHANGELOG.md"
            )
        notes = changelog_notes
        commit = verify_ready(
            args.tag,
            origin_remote=args.origin_remote,
            mirror_remote=args.mirror_remote,
            timeout=args.mirror_timeout,
            interval=args.mirror_interval,
        )
        attest_argo_ci_run(
            args.ci_run,
            commit,
            api_url=args.argo_api_url,
            namespace=args.argo_namespace,
            token=argo_token,
        )
        print(f"PASS  Argo CI run {args.ci_run} succeeded for commit {commit}")
        print(f"PASS  exact annotated tag {args.tag} at {commit}")
        print("PASS  origin and mirror advertise the same peeled tag")

        if args.verify_only:
            record = get_release(
                args.tag,
                args.api_url,
                args.repository,
                token,
                expected_commit=commit,
                expected_notes=notes,
            )
            if record is None:
                raise ReleaseError(f"Forgejo has no published release record for {args.tag}")
            print(f"PASS  Forgejo release {args.tag} is published")
        else:
            record = publish_release(
                args.tag,
                commit,
                notes,
                api_url=args.api_url,
                repository=args.repository,
                token=token,
            )
            print(f"PASS  Forgejo release {args.tag} is published (Forgejo is authoritative)")

        wait_for_mirror(
            args.tag,
            commit,
            origin_remote=args.origin_remote,
            mirror_remote=args.mirror_remote,
            timeout=args.mirror_timeout,
            interval=args.mirror_interval,
        )
        print("READY  mirror and tag are ready for consumer synchronization")
        print(
            f"Next: .venv/bin/python tools/consumer_sync.py --release-tag {args.tag} --check"
        )
        return 0
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
