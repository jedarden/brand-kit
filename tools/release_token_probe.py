#!/usr/bin/env python3
"""Periodically verify the three credentials used by the release handoff.

Every provider request in this module is read-only, except for the Argo
WorkflowTemplate submission check, which uses Argo's server-side dry-run flag.
That request exercises the same submit authorization as the consumer handoff
without creating a Workflow.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import release_publish

DEFAULT_FORGEJO_API_URL = release_publish.DEFAULT_API_URL
DEFAULT_REPOSITORY = release_publish.DEFAULT_REPOSITORY
DEFAULT_ARGO_API_URL = release_publish.DEFAULT_ARGO_API_URL
DEFAULT_ARGO_NAMESPACE = release_publish.DEFAULT_ARGO_NAMESPACE
DEFAULT_CI_WORKFLOW_TEMPLATE = release_publish.DEFAULT_ARGO_WORKFLOW_TEMPLATE
DEFAULT_CONSUMER_WORKFLOW_TEMPLATE = "brand-kit-consumer-drift"
REPORT_SCHEMA = "brand-kit-release-token-probe/v1"


def argo_api_path(api_url: str, path: str) -> str:
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return f"{base}/{path.lstrip('/')}"


def argo_workflows_url(api_url: str, namespace: str) -> str:
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    return argo_api_path(
        api_url,
        f"workflows/{urllib.parse.quote(namespace, safe='')}",
    )


def argo_submit_url(api_url: str, namespace: str) -> str:
    return f"{argo_workflows_url(api_url, namespace)}/submit"


def probe_forgejo(
    token: str | None,
    *,
    api_url: str = DEFAULT_FORGEJO_API_URL,
    repository: str = DEFAULT_REPOSITORY,
    request: Callable[..., Any] = release_publish.request_json,
) -> None:
    """Check token authentication and release-record read access in Forgejo."""
    token = release_publish.require_token(token, "FORGEJO_TOKEN")
    user = request("GET", f"{api_url.rstrip('/')}/user", token=token)
    if not isinstance(user, dict) or not isinstance(user.get("login"), str) or not user[
        "login"
    ].strip():
        raise release_publish.ReleaseError(
            "Forgejo token self-lookup returned an inconclusive response"
        )

    releases = request(
        "GET",
        f"{release_publish.releases_url(api_url, repository)}?limit=1",
        token=token,
    )
    if not isinstance(releases, list):
        raise release_publish.ReleaseError(
            "Forgejo release-list probe returned a non-list response"
        )


def probe_argo_read(
    token: str | None,
    *,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_CI_WORKFLOW_TEMPLATE,
    request: Callable[..., Any] = release_publish.request_json,
) -> None:
    """Check read access to the namespace's CI workflow records in Argo."""
    token = release_publish.require_token(token, "ARGO_TOKEN")
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"Argo workflow template is malformed: {workflow_template!r}"
        )
    label_selector = urllib.parse.quote(
        f"workflows.argoproj.io/workflow-template={workflow_template}",
        safe="",
    )
    workflows = request(
        "GET",
        f"{argo_workflows_url(api_url, namespace)}"
        f"?labelSelector={label_selector}&limit=1",
        token=token,
        authorization_scheme="Bearer",
        service="Argo API",
    )
    if not isinstance(workflows, dict) or not isinstance(workflows.get("items"), list):
        raise release_publish.ReleaseError(
            "Argo workflow-list probe returned an inconclusive response"
        )


def probe_argo_submit(
    token: str | None,
    *,
    api_url: str = DEFAULT_ARGO_API_URL,
    namespace: str = DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_CONSUMER_WORKFLOW_TEMPLATE,
    request: Callable[..., Any] = release_publish.request_json,
) -> None:
    """Check consumer submission authorization using Argo server dry-run."""
    token = release_publish.require_token(token, "ARGO_SUBMIT_TOKEN")
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"Argo workflow template is malformed: {workflow_template!r}"
        )
    response = request(
        "POST",
        argo_submit_url(api_url, namespace),
        payload={
            "namespace": namespace,
            "resourceKind": "WorkflowTemplate",
            "resourceName": workflow_template,
            "submitOptions": {
                "parameters": ["release-tag=token-probe"],
                "serverDryRun": True,
            },
        },
        token=token,
        authorization_scheme="Bearer",
        service="Argo API",
    )
    metadata = response.get("metadata") if isinstance(response, dict) else None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise release_publish.ReleaseError(
            "Argo WorkflowTemplate dry-run returned an inconclusive response"
        )


def run_probe(
    *,
    forgejo_token: str | None,
    argo_token: str | None,
    argo_submit_token: str | None,
    forgejo_api_url: str = DEFAULT_FORGEJO_API_URL,
    repository: str = DEFAULT_REPOSITORY,
    argo_api_url: str = DEFAULT_ARGO_API_URL,
    argo_namespace: str = DEFAULT_ARGO_NAMESPACE,
    ci_workflow_template: str = DEFAULT_CI_WORKFLOW_TEMPLATE,
    consumer_workflow_template: str = DEFAULT_CONSUMER_WORKFLOW_TEMPLATE,
    request: Callable[..., Any] = release_publish.request_json,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    probes = (
        (
            "FORGEJO_TOKEN",
            lambda: probe_forgejo(
                forgejo_token,
                api_url=forgejo_api_url,
                repository=repository,
                request=request,
            ),
        ),
        (
            "ARGO_TOKEN",
            lambda: probe_argo_read(
                argo_token,
                api_url=argo_api_url,
                namespace=argo_namespace,
                workflow_template=ci_workflow_template,
                request=request,
            ),
        ),
        (
            "ARGO_SUBMIT_TOKEN",
            lambda: probe_argo_submit(
                argo_submit_token,
                api_url=argo_api_url,
                namespace=argo_namespace,
                workflow_template=consumer_workflow_template,
                request=request,
            ),
        ),
    )
    tokens = (forgejo_token, argo_token, argo_submit_token)
    for name, probe in probes:
        try:
            probe()
        except release_publish.ReleaseError as error:
            checks.append(
                {
                    "credential": name,
                    "status": "fail",
                    "error": release_publish._redact(str(error), *tokens),
                }
            )
        else:
            checks.append({"credential": name, "status": "pass"})
    return {
        "schema": REPORT_SCHEMA,
        "status": "pass" if all(check["status"] == "pass" for check in checks) else "fail",
        "checks": checks,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe the release credentials without publishing or submitting a workflow."
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_FORGEJO_API_URL,
        help="Forgejo API base URL",
    )
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help="Forgejo repository in owner/name form",
    )
    parser.add_argument(
        "--argo-api-url",
        default=DEFAULT_ARGO_API_URL,
        help="Argo Workflows API base URL",
    )
    parser.add_argument(
        "--argo-namespace",
        default=DEFAULT_ARGO_NAMESPACE,
        help="Argo namespace",
    )
    parser.add_argument(
        "--ci-workflow-template",
        default=DEFAULT_CI_WORKFLOW_TEMPLATE,
        help=f"CI workflow template used by ARGO_TOKEN (default: {DEFAULT_CI_WORKFLOW_TEMPLATE})",
    )
    parser.add_argument(
        "--consumer-workflow-template",
        default=DEFAULT_CONSUMER_WORKFLOW_TEMPLATE,
        help=f"consumer template used by ARGO_SUBMIT_TOKEN (default: {DEFAULT_CONSUMER_WORKFLOW_TEMPLATE})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional JSON report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_probe(
        forgejo_token=os.environ.get("FORGEJO_TOKEN"),
        argo_token=os.environ.get("ARGO_TOKEN"),
        argo_submit_token=os.environ.get("ARGO_SUBMIT_TOKEN"),
        forgejo_api_url=args.api_url,
        repository=args.repository,
        argo_api_url=args.argo_api_url,
        argo_namespace=args.argo_namespace,
        ci_workflow_template=args.ci_workflow_template,
        consumer_workflow_template=args.consumer_workflow_template,
    )
    if args.report is not None:
        try:
            write_report(report, args.report)
        except OSError as error:
            print(f"error: cannot write probe report: {error}", file=sys.stderr)
            return 1
    for check in report["checks"]:
        if check["status"] == "pass":
            print(f"PASS  {check['credential']}")
        else:
            print(f"FAIL  {check['credential']}: {check['error']}", file=sys.stderr)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
