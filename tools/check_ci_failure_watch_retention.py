#!/usr/bin/env python3
"""Check the failure-watch lookback against the Argo retention contract.

The watcher lives in this repository, while iad-ci's Argo controller defaults
live in declarative-config.  The WorkflowTemplate carries the expected
retention and lookback as annotations so the deployed object is inspectable;
this check verifies those annotations, the command actually run by the
template, and (when supplied) the external controller Application.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import re
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import brand_kit_ci_failure_watch

DEFAULT_MANIFEST = ROOT / "automation/brand-kit-ci-failure-watch-workflowtemplate.yml"
RETENTION_ANNOTATION = "brand-kit.ardenone.com/argo-failure-retention-seconds"
LOOKBACK_ANNOTATION = "brand-kit.ardenone.com/failure-watch-lookback-minutes"
LOOKBACK_PATTERN = re.compile(r"(?:^|\s)--lookback-minutes\s+(\d+)(?:\s|$)")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"could not parse {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a YAML object")
    return document


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _annotation_integer(annotations: dict[str, Any], name: str) -> int:
    value = annotations.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"annotation {name!r} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"annotation {name!r} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"annotation {name!r} must be positive")
    return parsed


def _validate_lookback_coverage(lookback_minutes: int, retention_seconds: int) -> None:
    """Reject a lookback that cannot cover the configured retention window."""
    if not isinstance(lookback_minutes, int) or isinstance(lookback_minutes, bool):
        raise brand_kit_ci_failure_watch.release_publish.ReleaseError(
            "lookback minutes must be an integer"
        )
    if not 1 <= lookback_minutes <= 24 * 60:
        raise brand_kit_ci_failure_watch.release_publish.ReleaseError(
            "lookback minutes must be between 1 and 1440"
        )
    if lookback_minutes * 60 < retention_seconds:
        required_minutes = math.ceil(retention_seconds / 60)
        raise brand_kit_ci_failure_watch.release_publish.ReleaseError(
            "lookback minutes must cover configured Argo failure retention: "
            f"{lookback_minutes} < {required_minutes}"
        )


def _declared_controller_retention(path: Path) -> int:
    document = _load_yaml(path)
    spec = document.get("spec")
    source = spec.get("source", {}) if isinstance(spec, dict) else {}
    helm = source.get("helm", {}) if isinstance(source, dict) else {}
    if not isinstance(helm, dict):
        raise ValueError(f"{path} has no Helm source configuration")
    values = helm.get("values")
    if isinstance(values, str):
        try:
            values_document = yaml.safe_load(values)
        except yaml.YAMLError as error:
            raise ValueError(f"could not parse Helm values in {path}: {error}") from error
    else:
        values_document = helm.get("valuesObject")
    if not isinstance(values_document, dict):
        raise ValueError(f"{path} has no Helm values object")
    try:
        retention = values_document["controller"]["workflowDefaults"]["spec"][
            "ttlStrategy"
        ]["secondsAfterFailure"]
        if isinstance(retention, bool) or not isinstance(retention, (int, str)):
            raise ValueError("controller failure retention must be an integer")
        retention = int(retention)
        if retention < 1:
            raise ValueError("controller failure retention must be positive")
        return retention
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"{path} does not declare controller failure retention"
        ) from error


def check_configuration(
    manifest: Path = DEFAULT_MANIFEST,
    *,
    argo_config: Path | None = None,
) -> list[str]:
    """Return configuration drift/errors; an empty list means it is covered."""
    try:
        document = _load_yaml(manifest)
    except ValueError as error:
        return [str(error)]

    errors: list[str] = []
    metadata = document.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if not isinstance(annotations, dict):
        return [f"{manifest} has no metadata annotations"]

    try:
        declared_retention = _annotation_integer(annotations, RETENTION_ANNOTATION)
    except ValueError as error:
        errors.append(str(error))
        declared_retention = None
    try:
        declared_lookback = _annotation_integer(annotations, LOOKBACK_ANNOTATION)
    except ValueError as error:
        errors.append(str(error))
        declared_lookback = None

    configured_retention = declared_retention
    if argo_config is not None:
        try:
            actual_retention = _declared_controller_retention(argo_config)
        except ValueError as error:
            errors.append(str(error))
        else:
            configured_retention = actual_retention
            if actual_retention != declared_retention:
                errors.append(
                    "declarative-config Argo failure retention differs from the "
                    f"watcher contract: {actual_retention} != {declared_retention}"
                )

    if declared_retention != brand_kit_ci_failure_watch.ARGO_FAILURE_RETENTION_SECONDS:
        errors.append(
            "manifest retention annotation does not match watcher contract: "
            f"{declared_retention!r} != "
            f"{brand_kit_ci_failure_watch.ARGO_FAILURE_RETENTION_SECONDS}"
        )
    if declared_lookback is not None and configured_retention is not None:
        try:
            _validate_lookback_coverage(declared_lookback, configured_retention)
        except brand_kit_ci_failure_watch.release_publish.ReleaseError as error:
            errors.append(f"manifest lookback is unsafe: {error}")

    command_lookbacks = [
        int(match.group(1))
        for value in _strings(document)
        for match in LOOKBACK_PATTERN.finditer(value)
    ]
    if len(command_lookbacks) != 1:
        errors.append(
            f"expected exactly one --lookback-minutes command, found {len(command_lookbacks)}"
        )
    else:
        command_lookback = command_lookbacks[0]
        if configured_retention is not None:
            try:
                _validate_lookback_coverage(command_lookback, configured_retention)
            except brand_kit_ci_failure_watch.release_publish.ReleaseError as error:
                errors.append(f"WorkflowTemplate lookback is unsafe: {error}")
        if declared_lookback is not None and command_lookback != declared_lookback:
            errors.append(
                "WorkflowTemplate command and lookback annotation differ: "
                f"{command_lookback} != {declared_lookback}"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that failure-watch lookback covers Argo failure retention."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--argo-config",
        type=Path,
        help="optional declarative-config Argo Application to compare",
    )
    args = parser.parse_args(argv)
    errors = check_configuration(args.manifest, argo_config=args.argo_config)
    if errors:
        for error in errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1
    print("PASS  failure-watch lookback covers Argo failure retention")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
