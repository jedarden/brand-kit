#!/usr/bin/env python3
"""Read Argo's Workflow objects from the in-cluster Kubernetes API.

argo-server on iad-ci accepts only SSO sessions (``--auth-mode=sso``), so a
scheduled workload cannot present a bearer token to it. Workflows are
Kubernetes custom resources, though, and the Kubernetes API accepts the
projected ServiceAccount token that kubelet mounts into every pod. The
scheduled watchers therefore read Workflow objects there with their own pod
identity: nothing is provisioned, stored in OpenBao, or passed through the
environment, and the namespaced Role on each ServiceAccount
(declarative-config ``k8s/iad-ci/argo-workflows/brand-kit-automation-rbac.yml``)
is the whole authorization boundary.
"""

from __future__ import annotations

import functools
import ssl
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import release_publish

DEFAULT_API_URL = "https://kubernetes.default.svc"
DEFAULT_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
ARGO_API_GROUP = "argoproj.io"
ARGO_API_VERSION = "v1alpha1"
SERVICE = "Kubernetes API"


def api_path(api_url: str, path: str) -> str:
    """Join one API path onto an HTTPS API server URL without credentials."""
    if not isinstance(api_url, str) or not api_url.strip():
        raise release_publish.ReleaseError("Kubernetes API URL must be an absolute HTTPS URL")
    parsed = urllib.parse.urlsplit(api_url)
    try:
        parsed.port
    except ValueError as error:
        raise release_publish.ReleaseError("Kubernetes API URL has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise release_publish.ReleaseError(
            "Kubernetes API URL must be HTTPS without credentials, query, or fragment"
        )
    return f"{api_url.rstrip('/')}/{path.lstrip('/')}"


def argo_collection_url(api_url: str, namespace: str, resource: str) -> str:
    """Return the namespaced collection URL for one Argo custom resource."""
    if not isinstance(namespace, str) or not release_publish.WORKFLOW_NAME_PATTERN.fullmatch(
        namespace
    ):
        raise release_publish.ReleaseError(f"Argo namespace is malformed: {namespace!r}")
    if resource != "workflows":
        raise release_publish.ReleaseError(f"unsupported Argo resource: {resource!r}")
    return api_path(
        api_url,
        f"apis/{ARGO_API_GROUP}/{ARGO_API_VERSION}/namespaces/"
        f"{urllib.parse.quote(namespace, safe='')}/{resource}",
    )


def read_service_account_token(directory: Path = DEFAULT_SERVICE_ACCOUNT_DIR) -> str:
    """Read the pod's projected token; its value never enters an error."""
    path = Path(directory) / "token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise release_publish.ReleaseError(
            f"ServiceAccount token is not mounted at {path}"
        ) from error
    return release_publish.require_token(token, f"ServiceAccount token at {path}")


def service_account_context(
    directory: Path = DEFAULT_SERVICE_ACCOUNT_DIR,
) -> ssl.SSLContext:
    """Trust exactly the cluster CA that kubelet mounts beside the token."""
    path = Path(directory) / "ca.crt"
    try:
        return ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError) as error:
        raise release_publish.ReleaseError(
            f"ServiceAccount CA bundle is not usable at {path}"
        ) from error


def request_function(
    directory: Path = DEFAULT_SERVICE_ACCOUNT_DIR,
) -> Callable[..., Any]:
    """Return ``request_json`` bound to the in-cluster API server's CA."""
    return functools.partial(
        release_publish.request_json,
        context=service_account_context(directory),
    )
