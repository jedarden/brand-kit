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
WORKFLOW_NAME_PREFIX = "brand-kit-consumer-drift-"
WORKFLOW_TEMPLATE_LABEL = "workflows.argoproj.io/workflow-template"
IDENTITY_LABEL_PREFIX = "brand-kit.ardenone.com"
RELEASE_TAG_LABEL = f"{IDENTITY_LABEL_PREFIX}/release-tag"
RELEASE_COMMIT_LABEL = f"{IDENTITY_LABEL_PREFIX}/release-commit"
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


def argo_workflow_url(api_url: str, namespace: str, workflow_name: str) -> str:
    """Return the read-only URL for one workflow by its deterministic name."""
    if not isinstance(workflow_name, str) or not workflow_name:
        raise release_publish.ReleaseError("Argo workflow name is malformed")
    return (
        f"{argo_workflows_url(api_url, namespace)}/"
        f"{urllib.parse.quote(workflow_name, safe='')}"
    )


def workflow_name(commit: str) -> str:
    """Return the stable Argo name shared by every trigger for one commit."""
    if not isinstance(commit, str) or not OBJECT_ID_PATTERN.fullmatch(commit):
        raise release_publish.ReleaseError(
            f"release commit is not a full object ID: {commit!r}"
        )
    return f"{WORKFLOW_NAME_PREFIX}{commit.lower()}"


def workflow_payload(
    release_tag: str,
    commit: str,
    namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
) -> dict[str, Any]:
    release_publish.validate_tag(release_tag)
    if not isinstance(commit, str) or not OBJECT_ID_PATTERN.fullmatch(commit):
        raise release_publish.ReleaseError(
            f"release commit is not a full object ID: {commit!r}"
        )
    commit = commit.lower()
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
                "name": workflow_name(commit),
                "namespace": namespace,
                "labels": {
                    RELEASE_TAG_LABEL: release_tag,
                    RELEASE_COMMIT_LABEL: commit,
                },
            },
            "spec": {
                "workflowTemplateRef": {"name": workflow_template},
                "arguments": {
                    "parameters": [
                        {"name": "release-tag", "value": release_tag},
                        {"name": "release-commit", "value": commit},
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


def _parameter_values(value: Any) -> dict[str, str]:
    if isinstance(value, list):
        return {
            item["name"]: item["value"]
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        }
    if isinstance(value, dict):
        return {
            key: item
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }
    return {}


def _workflow_parameters(workflow: dict[str, Any]) -> dict[str, str]:
    spec = workflow.get("spec")
    arguments = spec.get("arguments") if isinstance(spec, dict) else None
    parameters = arguments.get("parameters") if isinstance(arguments, dict) else None
    values = _parameter_values(parameters)
    status = workflow.get("status")
    outputs = status.get("outputs") if isinstance(status, dict) else None
    output_parameters = outputs.get("parameters") if isinstance(outputs, dict) else None
    values.update(_parameter_values(output_parameters))
    return values


def _workflow_labels(workflow: dict[str, Any]) -> dict[str, str]:
    metadata = workflow.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {
        key: value
        for key, value in labels.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _workflow_name_from_record(workflow: dict[str, Any]) -> str:
    metadata = workflow.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name:
        raise release_publish.ReleaseError(
            "Argo workflow inspection returned a record without a name"
        )
    return name


def _is_consumer_workflow(workflow: dict[str, Any], workflow_template: str) -> bool:
    labels = _workflow_labels(workflow)
    label_template = labels.get(WORKFLOW_TEMPLATE_LABEL)
    if label_template is not None and label_template != workflow_template:
        return False
    spec = workflow.get("spec")
    template_ref = spec.get("workflowTemplateRef") if isinstance(spec, dict) else None
    referenced_template = (
        template_ref.get("name") if isinstance(template_ref, dict) else None
    )
    return referenced_template is None or referenced_template == workflow_template


def _matches_identity(
    workflow: dict[str, Any],
    release_tag: str,
    commit: str,
    workflow_template: str,
) -> bool:
    if not _is_consumer_workflow(workflow, workflow_template):
        return False
    labels = _workflow_labels(workflow)
    parameters = _workflow_parameters(workflow)
    observed_tag = labels.get(RELEASE_TAG_LABEL) or parameters.get(
        "release-tag"
    ) or parameters.get("selected-release-tag")
    observed_commit = labels.get(RELEASE_COMMIT_LABEL) or parameters.get(
        "release-commit"
    ) or parameters.get("selected-release-commit")
    return observed_tag == release_tag or observed_commit == commit


def _read_workflow(
    workflow_name_value: str,
    *,
    argo_api_url: str,
    argo_namespace: str,
    argo_token: str,
    request: Callable[..., Any],
) -> dict[str, Any] | None:
    try:
        workflow = request(
            "GET",
            argo_workflow_url(argo_api_url, argo_namespace, workflow_name_value),
            token=argo_token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except release_publish.HttpFailure as error:
        if error.status == 404:
            return None
        raise release_publish.ReleaseError(
            f"cannot inspect Argo workflow {workflow_name_value}: {error}"
        ) from error
    except release_publish.ReleaseError as error:
        raise release_publish.ReleaseError(
            f"cannot inspect Argo workflow {workflow_name_value}: {error}"
        ) from error
    if not isinstance(workflow, dict):
        raise release_publish.ReleaseError(
            f"Argo workflow {workflow_name_value} inspection returned a non-object"
        )
    observed_name = _workflow_name_from_record(workflow)
    if observed_name != workflow_name_value:
        raise release_publish.ReleaseError(
            f"Argo workflow inspection returned {observed_name!r}, "
            f"expected {workflow_name_value!r}"
        )
    return workflow


def _list_consumer_workflows(
    *,
    argo_api_url: str,
    argo_namespace: str,
    argo_token: str,
    workflow_template: str,
    request: Callable[..., Any],
) -> list[dict[str, Any]]:
    url = f"{argo_workflows_url(argo_api_url, argo_namespace)}?"
    url += urllib.parse.urlencode(
        {
            "labelSelector": f"{WORKFLOW_TEMPLATE_LABEL}={workflow_template}",
            "limit": "1000",
        }
    )
    try:
        response = request(
            "GET",
            url,
            token=argo_token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except release_publish.ReleaseError as error:
        raise release_publish.ReleaseError(
            f"cannot inspect existing consumer-drift workflows: {error}"
        ) from error
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise release_publish.ReleaseError(
            "Argo workflow-list inspection returned no items list"
        )
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata.get("continue"):
        raise release_publish.ReleaseError(
            "Argo workflow-list inspection was truncated; do not submit until "
            "the existing-run list can be reconciled completely"
        )
    workflows = []
    for item in response["items"]:
        if not isinstance(item, dict):
            raise release_publish.ReleaseError(
                "Argo workflow-list inspection returned a malformed item"
            )
        _workflow_name_from_record(item)
        workflows.append(item)
    return workflows


def reconcile_existing_workflow(
    release_tag: str,
    commit: str,
    *,
    argo_api_url: str,
    argo_namespace: str,
    argo_token: str,
    workflow_template: str,
    request: Callable[..., Any],
) -> dict[str, Any] | None:
    """Find one accepted run without creating or mutating an Argo workflow.

    The exact deterministic name covers same-commit races.  The template-scoped
    list also recognizes older generated-name runs for the same release tag,
    which lets the release trigger reconcile with a daily fallback run.  A
    failed read is fatal: the caller must not POST when duplicate state cannot
    be inspected.
    """
    exact_name = workflow_name(commit)
    exact = _read_workflow(
        exact_name,
        argo_api_url=argo_api_url,
        argo_namespace=argo_namespace,
        argo_token=argo_token,
        request=request,
    )
    if exact is not None:
        if not _matches_identity(exact, release_tag, commit, workflow_template):
            raise release_publish.ReleaseError(
                f"Argo workflow {exact_name} exists with a different release identity"
            )
        return exact

    matches = [
        workflow
        for workflow in _list_consumer_workflows(
            argo_api_url=argo_api_url,
            argo_namespace=argo_namespace,
            argo_token=argo_token,
            workflow_template=workflow_template,
            request=request,
        )
        if _matches_identity(workflow, release_tag, commit, workflow_template)
    ]
    if len(matches) > 1:
        names = ", ".join(_workflow_name_from_record(item) for item in matches)
        raise release_publish.ReleaseError(
            f"multiple existing consumer-drift workflows match {release_tag}/{commit}: "
            f"{names}; do not submit another run"
        )
    return matches[0] if matches else None


def _recover_after_submission_error(
    error: release_publish.ReleaseError,
    release_tag: str,
    commit: str,
    *,
    argo_api_url: str,
    argo_namespace: str,
    argo_read_token: str,
    workflow_template: str,
    request: Callable[..., Any],
) -> dict[str, Any] | None:
    try:
        return reconcile_existing_workflow(
            release_tag,
            commit,
            argo_api_url=argo_api_url,
            argo_namespace=argo_namespace,
            argo_token=argo_read_token,
            workflow_template=workflow_template,
            request=request,
        )
    except release_publish.ReleaseError as inspection_error:
        raise release_publish.ReleaseError(
            f"consumer-drift POST for {release_tag} has an ambiguous outcome; "
            f"read-only reconciliation failed: {inspection_error}; do not retry "
            "until the Argo workflow state is inspected"
        ) from inspection_error


def submit_consumer_drift(
    release_tag: str,
    *,
    forgejo_api_url: str = release_publish.DEFAULT_API_URL,
    repository: str = release_publish.DEFAULT_REPOSITORY,
    forgejo_token: str | None = None,
    argo_api_url: str = release_publish.DEFAULT_ARGO_API_URL,
    argo_namespace: str = release_publish.DEFAULT_ARGO_NAMESPACE,
    argo_token: str | None = None,
    argo_read_token: str | None = None,
    workflow_template: str = DEFAULT_WORKFLOW_TEMPLATE,
    origin_remote: str = release_publish.DEFAULT_ORIGIN_REMOTE,
    mirror_remote: str = release_publish.DEFAULT_MIRROR_REMOTE,
    root: Path = ROOT,
    timeout: float = 300.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] = release_publish.time.sleep,
    request: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Verify a release and reconcile one audit workflow for its exact tag.

    No Argo request is made until the Forgejo record is published, contains a
    full target commit, and both remotes advertise that commit for the tag.
    Existing runs are read with ``ARGO_TOKEN`` before the single
    non-retryable POST made with ``ARGO_SUBMIT_TOKEN``; an ambiguous POST is
    inspected but never replayed automatically.
    """
    release_publish.validate_tag(release_tag)
    forgejo_token = release_publish.require_token(forgejo_token, "FORGEJO_TOKEN")
    argo_token = release_publish.require_token(argo_token, "ARGO_SUBMIT_TOKEN")
    argo_read_token = release_publish.require_token(argo_read_token, "ARGO_TOKEN")
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
    existing = reconcile_existing_workflow(
        release_tag,
        commit,
        argo_api_url=argo_api_url,
        argo_namespace=argo_namespace,
        argo_token=argo_read_token,
        workflow_template=workflow_template,
        request=request,
    )
    if existing is not None:
        return existing

    payload = workflow_payload(
        release_tag,
        commit,
        namespace=argo_namespace,
        workflow_template=workflow_template,
    )
    try:
        submitted = request(
            "POST",
            argo_workflows_url(argo_api_url, argo_namespace),
            payload=payload,
            token=argo_token,
            authorization_scheme="Bearer",
            service="Argo API",
        )
    except release_publish.ReleaseError as error:
        recovered = _recover_after_submission_error(
            error,
            release_tag,
            commit,
            argo_api_url=argo_api_url,
            argo_namespace=argo_namespace,
            argo_read_token=argo_read_token,
            workflow_template=workflow_template,
            request=request,
        )
        if recovered is not None:
            return recovered
        raise release_publish.ReleaseError(
            f"consumer-drift POST for {release_tag} failed without a matching "
            f"workflow ({error}); do not retry until the Argo state is confirmed absent"
        ) from error
    try:
        if not isinstance(submitted, dict):
            raise release_publish.ReleaseError(
                f"Argo submission for {release_tag} returned a non-object record"
            )
        metadata = submitted.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            raise release_publish.ReleaseError(
                f"Argo submission for {release_tag} returned no workflow name"
            )
        if metadata["name"] != workflow_name(commit):
            raise release_publish.ReleaseError(
                f"Argo submission for {release_tag} returned workflow "
                f"{metadata['name']!r}, expected {workflow_name(commit)!r}"
            )
    except release_publish.ReleaseError as error:
        recovered = _recover_after_submission_error(
            error,
            release_tag,
            commit,
            argo_api_url=argo_api_url,
            argo_namespace=argo_namespace,
            argo_read_token=argo_read_token,
            workflow_template=workflow_template,
            request=request,
        )
        if recovered is not None:
            return recovered
        raise release_publish.ReleaseError(
            f"consumer-drift POST for {release_tag} returned an unverifiable "
            f"response ({error}); do not retry until the Argo state is confirmed absent"
        ) from error
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
        # Reconciliation and submission use separate credentials. Never
        # silently reuse the read-only token for a write operation.
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
            argo_read_token=release_publish.environment_token("ARGO_TOKEN"),
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
