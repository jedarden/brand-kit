#!/usr/bin/env python3
"""Submit an exact published release to the consumer-drift Argo workflow.

The release publisher owns publication; this small handoff verifies that the
published Forgejo record is stable and exact, waits for the canonical and
read-only mirror tags to agree, and only then submits the read-only audit.
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

DEFAULT_WORKFLOW_TEMPLATE = "brand-kit-consumer-drift"
DEFAULT_GENERATE_NAME = "brand-kit-consumer-drift-release-"
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def argo_workflows_url(api_url: str, namespace: str) -> str:
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    base = api_url.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return f"{base}/workflows/{urllib.parse.quote(namespace, safe='')}"


def workflow_payload(
    release_tag: str,
    namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
) -> dict[str, Any]:
    release_publish.validate_tag(release_tag)
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    if not isinstance(workflow_template, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        workflow_template
    ):
        raise release_publish.ReleaseError(
            f"consumer workflow template is malformed: {workflow_template!r}"
        )
    return {
        "workflow": {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": DEFAULT_GENERATE_NAME,
                "namespace": namespace,
            },
            "spec": {
                "workflowTemplateRef": {"name": workflow_template},
                "arguments": {
                    "parameters": [
                        {"name": "release-tag", "value": release_tag},
                    ]
                },
            },
        }
    }


def _release_commit(record: dict[str, Any], release_tag: str) -> str:
    target = record.get("target_commitish")
    if not isinstance(target, str) or not OBJECT_ID_PATTERN.fullmatch(target):
        raise release_publish.ReleaseError(
            f"Forgejo release {release_tag} has no verifiable target commit"
        )
    return target.lower()


def submit_consumer_drift(
    release_tag: str,
    *,
    forgejo_api_url: str = release_publish.DEFAULT_API_URL,
    repository: str = release_publish.DEFAULT_REPOSITORY,
    forgejo_token: str | None = None,
    argo_api_url: str = release_publish.DEFAULT_ARGO_API_URL,
    argo_namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    argo_token: str | None = None,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
    origin_remote: str = release_publish.DEFAULT_ORIGIN_REMOTE,
    mirror_remote: str = release_publish.DEFAULT_MIRROR_REMOTE,
    root: Path = ROOT,
    timeout: float = 300.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] = release_publish.time.sleep,
    request: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Verify a release and submit one audit workflow for its exact tag.

    No Argo request is made until the Forgejo record is published, contains a
    full target commit, and both remotes advertise that commit for the tag.
    """
    release_publish.validate_tag(release_tag)
    forgejo_token = release_publish.require_token(forgejo_token, "FORGEJO_TOKEN")
    argo_token = release_publish.require_token(argo_token, "ARGO_SUBMIT_TOKEN")
    record = release_publish.get_release(
        release_tag,
        forgejo_api_url,
        repository,
        forgejo_token,
    )
    if record is None:
        raise release_publish.ReleaseError(
            f"Forgejo has no published release record for {release_tag}"
        )
    commit = _release_commit(record, release_tag)
    if "assets" in record:
        release_publish.verify_release_payload(
            record,
            release_tag,
            commit,
            lambda asset_url: release_publish.request_bytes(
                asset_url,
                headers={"Authorization": f"token {forgejo_token}"},
                service="Forgejo release attachment",
            ),
            api_url=forgejo_api_url,
            repository=repository,
        )

    # This is the release handoff gate, not the scheduled mirror-health
    # monitor. Validate the configured repository identities before accepting
    # any ref from either remote, then require exact main/tag propagation.
    release_publish.validate_remote_roles(origin_remote, mirror_remote, root)
    release_publish.wait_for_mirror(
        release_tag,
        commit,
        origin_remote=origin_remote,
        mirror_remote=mirror_remote,
        root=root,
        timeout=timeout,
        interval=interval,
        sleep=sleep,
    )

    request = request or release_publish.request_json
    try:
        submitted = request(
            "POST",
            argo_workflows_url(argo_api_url, argo_namespace),
            payload=workflow_payload(
                release_tag,
                namespace=argo_namespace,
                workflow_template=workflow_template,
            ),
            token=argo_token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except release_publish.ReleaseError as error:
        raise release_publish.ReleaseError(
            f"cannot submit consumer-drift workflow for {release_tag}: {error}"
        ) from error
    if not isinstance(submitted, dict):
        raise release_publish.ReleaseError(
            f"Argo submission for {release_tag} returned a non-object record"
        )
    metadata = submitted.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise release_publish.ReleaseError(
            f"Argo submission for {release_tag} returned no workflow name"
        )
    return submitted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a published Forgejo release to the consumer-drift audit."
    )
    parser.add_argument(
        "--release-tag",
        "--tag",
        dest="release_tag",
        required=True,
        help="exact stable release tag, for example v1.1.0",
    )
    parser.add_argument(
        "--api-url",
        default=release_publish.DEFAULT_API_URL,
        help="Forgejo API base URL",
    )
    parser.add_argument(
        "--repository",
        default=release_publish.DEFAULT_REPOSITORY,
        help="Forgejo repository in owner/name form",
    )
    parser.add_argument(
        "--argo-api-url",
        default=release_publish.DEFAULT_ARGO_API_URL,
        help="Argo Workflows API base URL",
    )
    parser.add_argument(
        "--argo-namespace",
        default=release_publish.DEFAULT_ARGO_NAMESPACE,
        help="Argo namespace",
    )
    parser.add_argument(
        "--workflow-template",
        default=DEFAULT_WORKFLOW_TEMPLATE,
        help=f"Argo WorkflowTemplate (default: {DEFAULT_WORKFLOW_TEMPLATE})",
    )
    parser.add_argument(
        "--origin-remote",
        default=release_publish.DEFAULT_ORIGIN_REMOTE,
        help="canonical Forgejo remote",
    )
    parser.add_argument(
        "--mirror-remote",
        default=release_publish.DEFAULT_MIRROR_REMOTE,
        help="read-only mirror remote",
    )
    parser.add_argument(
        "--mirror-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for mirror propagation",
    )
    parser.add_argument(
        "--mirror-interval",
        type=float,
        default=5.0,
        help="seconds between mirror checks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Submission must use its dedicated credential.  Never silently reuse
        # the read-only attestation token for a write operation.
        forgejo_token = release_publish.environment_token("FORGEJO_TOKEN")
        argo_token = release_publish.environment_token("ARGO_SUBMIT_TOKEN")
        submitted = submit_consumer_drift(
            args.release_tag,
            forgejo_api_url=args.api_url,
            repository=args.repository,
            forgejo_token=forgejo_token,
            argo_api_url=args.argo_api_url,
            argo_namespace=args.argo_namespace,
            argo_token=argo_token,
            workflow_template=args.workflow_template,
            origin_remote=args.origin_remote,
            mirror_remote=args.mirror_remote,
            timeout=args.mirror_timeout,
            interval=args.mirror_interval,
        )
    except release_publish.ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"SUBMITTED  consumer-drift workflow {submitted['metadata']['name']} "
        f"for published release {args.release_tag}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
