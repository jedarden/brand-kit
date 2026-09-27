#!/usr/bin/env python3
"""Compare the failure-watch manifests with their live Argo objects.

The failure watcher is application-owned: ArgoCD reads the manifests from this
repository's ``automation/`` directory.  This check reads the live objects
with ``kubectl get`` and compares their desired fields without applying
anything.  Exit 0 means both objects match; exit 1 means at least one object
differs; exit 2 means the comparison could not be completed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
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
    kubeconfig: str | None = None,
    runner=subprocess.run,
) -> int:
    """Run a read-only live-object comparison for every failure-watch manifest."""

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
        "--kubeconfig",
        help="kubeconfig passed to kubectl",
    )
    args = parser.parse_args(argv)
    return check_parity(
        kubectl=args.kubectl,
        server=args.server,
        kubeconfig=args.kubeconfig,
    )


if __name__ == "__main__":
    raise SystemExit(main())
