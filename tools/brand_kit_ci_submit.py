#!/usr/bin/env python3
"""Submit brand-kit-ci for one immutable brand-kit commit.

The WorkflowTemplate is owned by declarative-config, while this repository
owns the operator-facing trigger.  The template must accept ``revision`` and
check out that value detached before it runs the gate.  Passing a moving
branch alone is not sufficient evidence for release publication.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import release_publish


DEFAULT_WORKFLOW_TEMPLATE = release_publish.DEFAULT_ARGO_WORKFLOW_TEMPLATE
DEFAULT_REPOSITORY = "jedarden/brand-kit"
DEFAULT_BRANCH = "main"
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def validate_commit(commit: str) -> str:
    """Require a full SHA; branch names, abbreviated SHAs, and HEAD are invalid."""
    if not isinstance(commit, str) or commit != commit.strip():
        raise release_publish.ReleaseError(
            "brand-kit-ci revision must be an unpadded full commit SHA"
        )
    if not release_publish.OBJECT_ID_PATTERN.fullmatch(commit):
        raise release_publish.ReleaseError(
            f"brand-kit-ci revision must be a full commit SHA, got {commit!r}"
        )
    return commit.lower()


def validate_branch(branch: str) -> str:
    if not isinstance(branch, str) or branch != branch.strip() or not BRANCH_PATTERN.fullmatch(
        branch
    ):
        raise release_publish.ReleaseError(f"brand-kit-ci branch is malformed: {branch!r}")
    return branch


def argo_submit_url(api_url: str, namespace: str) -> str:
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return (
        f"{base}/workflows/{urllib.parse.quote(namespace, safe='')}/submit"
    )


def _validate_repository(repository: str) -> str:
    owner, separator, name = repository.partition("/")
    if (
        not separator
        or not owner
        or not name
        or "/" in name
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name)
    ):
        raise release_publish.ReleaseError(
            f"brand-kit repository must be owner/name, got {repository!r}"
        )
    return repository


def workflow_payload(
    commit: str,
    *,
    branch: str = DEFAULT_BRANCH,
    repository: str = DEFAULT_REPOSITORY,
    namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
) -> dict[str, Any]:
    """Build the Argo WorkflowTemplate submission for one exact revision."""
    commit = validate_commit(commit)
    branch = validate_branch(branch)
    repository = _validate_repository(repository)
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"brand-kit CI workflow template is malformed: {workflow_template!r}"
        )
    return {
        "namespace": namespace,
        "resourceKind": "WorkflowTemplate",
        "resourceName": workflow_template,
        "submitOptions": {
            "parameters": [
                f"repo={repository}",
                f"branch={branch}",
                f"revision={commit}",
            ]
        },
    }


def _parameter_values(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            item["name"]: item.get("value")
            for item in value
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, dict):
                result[item.get("name", key)] = item.get("value")
            else:
                result[key] = item
        return result
    return {}


def validate_submission(
    workflow: Any,
    commit: str,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
) -> dict[str, Any]:
    """Require Argo to return the exact submitted revision and template."""
    commit = validate_commit(commit)
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"brand-kit CI workflow template is malformed: {workflow_template!r}"
        )
    if not isinstance(workflow, dict):
        raise release_publish.ReleaseError(
            "Argo brand-kit-ci submission returned a non-object record"
        )
    metadata = workflow.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise release_publish.ReleaseError(
            "Argo brand-kit-ci submission returned no workflow name"
        )
    labels = metadata.get("labels")
    if isinstance(labels, dict):
        template = labels.get("workflows.argoproj.io/workflow-template")
        if template is not None and template != workflow_template:
            raise release_publish.ReleaseError(
                f"Argo brand-kit-ci submission uses workflow template {template!r}"
            )
    spec = workflow.get("spec")
    arguments = spec.get("arguments") if isinstance(spec, dict) else None
    parameters = arguments.get("parameters") if isinstance(arguments, dict) else None
    observed = _parameter_values(parameters).get("revision")
    if not isinstance(observed, str) or not release_publish.OBJECT_ID_PATTERN.fullmatch(observed):
        raise release_publish.ReleaseError(
            "Argo brand-kit-ci submission returned no structured full revision"
        )
    if observed.lower() != commit:
        raise release_publish.ReleaseError(
            f"Argo brand-kit-ci submission carries revision {observed.lower()}, "
            f"expected {commit}"
        )
    return workflow


def submit_brand_kit_ci(
    commit: str,
    *,
    branch: str = DEFAULT_BRANCH,
    repository: str = DEFAULT_REPOSITORY,
    argo_api_url: str = release_publish.DEFAULT_ARGO_API_URL,
    argo_namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    argo_token: str | None = None,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
    request: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Submit and validate one exact brand-kit-ci WorkflowTemplate run."""
    commit = validate_commit(commit)
    payload = workflow_payload(
        commit,
        branch=branch,
        repository=repository,
        namespace=argo_namespace,
        workflow_template=workflow_template,
    )
    token = release_publish.require_token(argo_token, "ARGO_SUBMIT_TOKEN")
    request = request or release_publish.request_json
    try:
        workflow = request(
            "POST",
            argo_submit_url(argo_api_url, argo_namespace),
            payload=payload,
            token=token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except release_publish.ReleaseError as error:
        raise release_publish.ReleaseError(
            "cannot submit brand-kit-ci for commit "
            f"{commit}: {release_publish._redact(error, token)}"
        ) from error
    return validate_submission(workflow, commit, workflow_template=workflow_template)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit brand-kit-ci for one exact full commit SHA."
    )
    parser.add_argument(
        "--commit",
        required=True,
        help="full commit SHA to check; use git rev-parse HEAD^{commit}",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help=f"source branch containing the commit (default: {DEFAULT_BRANCH})",
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help=f"GitHub clone repository in owner/name form (default: {DEFAULT_REPOSITORY})",
    )
    parser.add_argument(
        "--argo-api-url",
        default=release_publish.DEFAULT_ARGO_API_URL,
        help=f"Argo Workflows API base URL (default: {release_publish.DEFAULT_ARGO_API_URL})",
    )
    parser.add_argument(
        "--argo-namespace",
        default=release_publish.DEFAULT_ARGO_NAMESPACE,
        help=f"Argo namespace (default: {release_publish.DEFAULT_ARGO_NAMESPACE})",
    )
    parser.add_argument(
        "--workflow-template",
        default=DEFAULT_WORKFLOW_TEMPLATE,
        help=f"Argo WorkflowTemplate (default: {DEFAULT_WORKFLOW_TEMPLATE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        submitted = submit_brand_kit_ci(
            args.commit,
            branch=args.branch,
            repository=args.repository,
            argo_api_url=args.argo_api_url,
            argo_namespace=args.argo_namespace,
            argo_token=release_publish.environment_token("ARGO_SUBMIT_TOKEN"),
            workflow_template=args.workflow_template,
        )
    except release_publish.ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"SUBMITTED  {submitted['metadata']['name']} "
        f"for brand-kit commit {validate_commit(args.commit)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
