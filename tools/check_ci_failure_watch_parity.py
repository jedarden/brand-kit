#!/usr/bin/env python3
"""Compare the failure-watch manifests with their live Argo objects.

The failure watcher is application-owned: ArgoCD reads the manifests from this
repository's ``automation/`` directory.  This check reads the live objects
with ``kubectl get`` and compares their desired fields without applying
anything. It first verifies the owning Application's source, destination, and
health contract. Exit 0 means the Application is ready and both objects match;
exit 1 means a contract or object differs; exit 2 means the comparison could
not be completed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APPLICATION_SERVER = "http://traefik-rs-manager:8001"
APPLICATION_NAME = "brand-kit-automation-iad-ci"
APPLICATION_EXPECTED = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Application",
    "metadata": {"name": APPLICATION_NAME, "namespace": "argocd"},
    "spec": {
        "source": {
            "repoURL": "https://github.com/jedarden/brand-kit.git",
            "targetRevision": "main",
            "path": "automation",
            "directory": {"recurse": True, "include": "{*.yaml,*.yml}"},
        },
        "destination": {"namespace": "argo-workflows"},
    },
    "status": {
        "sync": {"status": "Synced"},
        "health": {"status": "Healthy"},
    },
}
DEFAULT_MANIFESTS = (
    ROOT / "automation/brand-kit-ci-failure-watch-workflowtemplate.yml",
    ROOT / "automation/brand-kit-ci-failure-watch-cronworkflow.yml",
)
OBJECTS = (
    ("workflowtemplate", "brand-kit-ci-failure-watch"),
    ("cronworkflow", "brand-kit-ci-failure-watch"),
)


def _get_command(
    resource: str,
    name: str,
    *,
    kubectl: str,
    server: str | None,
    kubeconfig: str | None,
) -> list[str]:
    command = [kubectl]
    if kubeconfig:
        command.extend(("--kubeconfig", kubeconfig))
    if server:
        command.extend(("--server", server))
    command.extend(
        (
            "get",
            f"{resource}/{name}",
            "--namespace",
            "argo-workflows",
            "--output=json",
        )
    )
    return command


def _get_application_command(
    *,
    kubectl: str,
    server: str | None,
    kubeconfig: str | None,
) -> list[str]:
    command = [kubectl]
    if kubeconfig:
        command.extend(("--kubeconfig", kubeconfig))
    if server:
        command.extend(("--server", server))
    command.extend(
        (
            "get",
            f"application/{APPLICATION_NAME}",
            "--namespace",
            "argocd",
            "--output=json",
        )
    )
    return command


def _project_application(document: object) -> dict:
    if not isinstance(document, dict):
        raise ValueError("kubectl returned a non-object JSON value")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("live Application has no metadata object")

    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("live Application has no spec object")
    source = spec.get("source")
    if not isinstance(source, dict):
        raise ValueError("live Application has no source object")
    directory = source.get("directory")
    if not isinstance(directory, dict):
        raise ValueError("live Application has no source.directory object")
    destination = spec.get("destination")
    if not isinstance(destination, dict):
        raise ValueError("live Application has no destination object")

    status = document.get("status")
    if not isinstance(status, dict):
        raise ValueError("live Application has no status object")
    sync = status.get("sync")
    if not isinstance(sync, dict):
        raise ValueError("live Application has no status.sync object")
    health = status.get("health")
    if not isinstance(health, dict):
        raise ValueError("live Application has no status.health object")

    return {
        "apiVersion": document.get("apiVersion"),
        "kind": document.get("kind"),
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
        },
        "spec": {
            "source": {
                "repoURL": source.get("repoURL"),
                "targetRevision": source.get("targetRevision"),
                "path": source.get("path"),
                "directory": {
                    "recurse": directory.get("recurse"),
                    "include": directory.get("include"),
                },
            },
            "destination": {"namespace": destination.get("namespace")},
        },
        "status": {
            "sync": {"status": sync.get("status")},
            "health": {"status": health.get("status")},
        },
    }


def _check_application(
    *,
    kubectl: str,
    server: str | None,
    kubeconfig: str | None,
    runner,
) -> tuple[int, str]:
    command = _get_application_command(
        kubectl=kubectl,
        server=server,
        kubeconfig=kubeconfig,
    )
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return 2, f"kubectl could not run: {error}"

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        return (
            2,
            f"kubectl get Application failed with exit {result.returncode}"
            + (f": {details}" if details else ""),
        )

    try:
        live = _project_application(json.loads(result.stdout))
    except (ValueError, json.JSONDecodeError) as error:
        return 2, f"could not read live Application: {error}"
    if live != APPLICATION_EXPECTED:
        return (
            1,
            "live Application differs from the documented deployment contract\n"
            + "documented: "
            + json.dumps(APPLICATION_EXPECTED, sort_keys=True)
            + "\nlive: "
            + json.dumps(live, sort_keys=True),
        )
    return 0, "brand-kit-automation-iad-ci Application smoke check passed"


def _controlled_fields(manifest: Path) -> dict:
    try:
        document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"could not parse manifest: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("manifest is not a YAML object")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("manifest has no metadata object")
    controlled_metadata = {
        key: metadata[key]
        for key in ("name", "namespace", "labels", "annotations")
        if key in metadata
    }
    if "spec" not in document:
        raise ValueError("manifest has no spec object")
    return {
        "apiVersion": document.get("apiVersion"),
        "kind": document.get("kind"),
        "metadata": controlled_metadata,
        "spec": document["spec"],
    }


def _project_live(document: object, desired: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("kubectl returned a non-object JSON value")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("live object has no metadata object")
    desired_metadata = desired["metadata"]
    return {
        "apiVersion": document.get("apiVersion"),
        "kind": document.get("kind"),
        "metadata": {
            key: metadata.get(key) for key in desired_metadata
        },
        "spec": document.get("spec"),
    }


def check_parity(
    manifests: tuple[Path, ...] = DEFAULT_MANIFESTS,
    *,
    kubectl: str = "kubectl",
    server: str | None = None,
    application_server: str | None = DEFAULT_APPLICATION_SERVER,
    kubeconfig: str | None = None,
    runner=subprocess.run,
) -> int:
    """Gate the read-only parity check on the healthy, correctly-wired Application."""

    application_status, application_message = _check_application(
        kubectl=kubectl,
        server=application_server,
        kubeconfig=kubeconfig,
        runner=runner,
    )
    if application_status:
        print(
            f"{APPLICATION_NAME}: {application_message}",
            file=sys.stderr,
        )
        return application_status
    print(application_message)

    failures: list[tuple[Path, str, bool]] = []
    for manifest, (resource, name) in zip(manifests, OBJECTS):
        if not manifest.is_file():
            failures.append((manifest, f"manifest does not exist: {manifest}", False))
            continue

        try:
            desired = _controlled_fields(manifest)
        except ValueError as error:
            failures.append((manifest, str(error), False))
            continue

        command = _get_command(
            resource,
            name,
            kubectl=kubectl,
            server=server,
            kubeconfig=kubeconfig,
        )
        try:
            result = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            failures.append((manifest, f"kubectl could not run: {error}", False))
            continue

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            failures.append(
                (
                    manifest,
                    f"kubectl get failed with exit {result.returncode}"
                    + (f": {details}" if details else ""),
                    False,
                )
            )
            continue

        try:
            live = _project_live(json.loads(result.stdout), desired)
        except (ValueError, json.JSONDecodeError) as error:
            failures.append((manifest, f"could not read live object: {error}", False))
            continue
        if live != desired:
            failures.append(
                (
                    manifest,
                    "live object differs from the repository copy\n"
                    + "repository: "
                    + json.dumps(desired, sort_keys=True)
                    + "\nlive: "
                    + json.dumps(live, sort_keys=True),
                    True,
                )
            )

    if not failures:
        print("brand-kit-ci failure-watch parity check passed")
        return 0

    for manifest, message, _ in failures:
        print(f"{manifest}: {message}", file=sys.stderr)

    return 1 if all(is_drift for _, _, is_drift in failures) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare failure-watch manifests with live Argo objects without applying them."
    )
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help="kubectl executable (default: kubectl)",
    )
    parser.add_argument(
        "--server",
        help="Kubernetes API server or read-only proxy passed to kubectl",
    )
    parser.add_argument(
        "--application-server",
        default=DEFAULT_APPLICATION_SERVER,
        help=(
            "Kubernetes API server or read-only proxy for the ArgoCD Application "
            f"(default: {DEFAULT_APPLICATION_SERVER})"
        ),
    )
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig passed to kubectl",
    )
    args = parser.parse_args(argv)
    return check_parity(
        kubectl=args.kubectl,
        server=args.server,
        application_server=args.application_server,
        kubeconfig=args.kubeconfig,
    )


if __name__ == "__main__":
    raise SystemExit(main())
