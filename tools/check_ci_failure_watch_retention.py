#!/usr/bin/env python3
"""Check the failure-watch window and cadence against both Argo retention axes.

The watcher lives in this repository, while iad-ci's Argo controller defaults
live in declarative-config.  The WorkflowTemplate carries the expected
retention and lookback as annotations so the deployed object is inspectable;
this check verifies those annotations, the command actually run by the
template, the CronWorkflow cadence, and (when supplied) the controller
Application.
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
DEFAULT_CRON_MANIFEST = ROOT / "automation/brand-kit-ci-failure-watch-cronworkflow.yml"
RETENTION_ANNOTATION = "brand-kit.ardenone.com/argo-failure-retention-seconds"
FAILED_LIMIT_ANNOTATION = "brand-kit.ardenone.com/argo-failed-workflow-limit"
ERRORED_LIMIT_ANNOTATION = "brand-kit.ardenone.com/argo-errored-workflow-limit"
LOOKBACK_ANNOTATION = "brand-kit.ardenone.com/failure-watch-lookback-minutes"
LOOKBACK_PATTERN = re.compile(r"(?:^|\s)--lookback-minutes\s+(\d+)(?:\s|$)")
# The controller recorded 45 failures in a busy hour on 2026-09-22. Double
# that observed rate for the planning check; a burst can still exceed it.
PLANNING_FAILURES_PER_HOUR = 90


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


def _declared_controller_retention(path: Path) -> tuple[int, int, int]:
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
        failed_limit = values_document["controller"]["retentionPolicy"]["failed"]
        if isinstance(failed_limit, bool) or not isinstance(failed_limit, (int, str)):
            raise ValueError("controller failed Workflow limit must be an integer")
        failed_limit = int(failed_limit)
        if failed_limit < 1:
            raise ValueError("controller failed Workflow limit must be positive")
        errored_limit = values_document["controller"]["retentionPolicy"]["errored"]
        if isinstance(errored_limit, bool) or not isinstance(errored_limit, (int, str)):
            raise ValueError("controller errored Workflow limit must be an integer")
        errored_limit = int(errored_limit)
        if errored_limit < 1:
            raise ValueError("controller errored Workflow limit must be positive")
        return retention, failed_limit, errored_limit
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"{path} does not declare controller failure TTL and failed/errored Workflow limits"
        ) from error


def _watcher_cadence_minutes(path: Path) -> int:
    document = _load_yaml(path)
    spec = document.get("spec")
    if not isinstance(spec, dict) or spec.get("timezone") != "UTC":
        raise ValueError(f"{path} must declare a UTC watcher schedule")
    schedules = spec.get("schedules")
    if not isinstance(schedules, list) or len(schedules) != 1:
        raise ValueError(f"{path} must declare exactly one watcher schedule")
    schedule = schedules[0]
    match = (
        re.fullmatch(r"\*/(\d+) \* \* \* \*", schedule)
        if isinstance(schedule, str)
        else None
    )
    if match is None:
        raise ValueError(f"{path} must use a regular minute-step watcher schedule")
    cadence = int(match.group(1))
    if cadence < 1 or cadence > 60 or 60 % cadence:
        raise ValueError(f"{path} watcher cadence must divide 60 minutes")
    return cadence


def _validate_count_coverage(failed_limit: int, cadence_minutes: int) -> None:
    projected = math.ceil(PLANNING_FAILURES_PER_HOUR * cadence_minutes / 60)
    if projected >= failed_limit:
        raise ValueError(
            "Workflow count cap permits eviction before the next nominal watcher run: "
            f"ceil({PLANNING_FAILURES_PER_HOUR}/hour * {cadence_minutes} minutes) "
            f"= {projected} >= cap {failed_limit}"
        )


def check_configuration(
    manifest: Path = DEFAULT_MANIFEST,
    *,
    cron_manifest: Path = DEFAULT_CRON_MANIFEST,
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
        declared_failed_limit = _annotation_integer(annotations, FAILED_LIMIT_ANNOTATION)
    except ValueError as error:
        errors.append(str(error))
        declared_failed_limit = None
    try:
        declared_errored_limit = _annotation_integer(annotations, ERRORED_LIMIT_ANNOTATION)
    except ValueError as error:
        errors.append(str(error))
        declared_errored_limit = None
    try:
        declared_lookback = _annotation_integer(annotations, LOOKBACK_ANNOTATION)
    except ValueError as error:
        errors.append(str(error))
        declared_lookback = None

    configured_retention = declared_retention
    configured_failed_limit = declared_failed_limit
    configured_errored_limit = declared_errored_limit
    if argo_config is not None:
        try:
            (
                actual_retention,
                actual_failed_limit,
                actual_errored_limit,
            ) = _declared_controller_retention(argo_config)
        except ValueError as error:
            errors.append(str(error))
        else:
            configured_retention = actual_retention
            configured_failed_limit = actual_failed_limit
            configured_errored_limit = actual_errored_limit
            # The annotation is the maximum TTL covered by the watcher, not
            # the controller's exact current TTL (currently 1800 seconds).
            if (
                declared_retention is not None
                and actual_retention > declared_retention
            ):
                errors.append(
                    "declarative-config Argo failure retention exceeds the "
                    f"watcher contract: {actual_retention} > {declared_retention}"
                )
            if actual_failed_limit != declared_failed_limit:
                errors.append(
                    "declarative-config Argo failed Workflow limit differs from the "
                    f"watcher contract: {actual_failed_limit} != {declared_failed_limit}"
                )
            if actual_errored_limit != declared_errored_limit:
                errors.append(
                    "declarative-config Argo errored Workflow limit differs from the "
                    f"watcher contract: {actual_errored_limit} != {declared_errored_limit}"
                )

    if declared_retention != brand_kit_ci_failure_watch.ARGO_FAILURE_RETENTION_SECONDS:
        errors.append(
            "manifest retention annotation does not match watcher contract: "
            f"{declared_retention!r} != "
            f"{brand_kit_ci_failure_watch.ARGO_FAILURE_RETENTION_SECONDS}"
        )
    if declared_failed_limit != brand_kit_ci_failure_watch.ARGO_FAILED_WORKFLOW_LIMIT:
        errors.append(
            "manifest failed Workflow limit annotation does not match watcher contract: "
            f"{declared_failed_limit!r} != "
            f"{brand_kit_ci_failure_watch.ARGO_FAILED_WORKFLOW_LIMIT}"
        )
    if (
        declared_errored_limit
        != brand_kit_ci_failure_watch.ARGO_ERRORED_WORKFLOW_LIMIT
    ):
        errors.append(
            "manifest errored Workflow limit annotation does not match watcher contract: "
            f"{declared_errored_limit!r} != "
            f"{brand_kit_ci_failure_watch.ARGO_ERRORED_WORKFLOW_LIMIT}"
        )
    try:
        cadence_minutes = _watcher_cadence_minutes(cron_manifest)
    except ValueError as error:
        errors.append(str(error))
    else:
        if (
            configured_retention is not None
            and cadence_minutes * 60 >= configured_retention
        ):
            errors.append(
                "watcher cadence is unsafe for TTL: "
                f"{cadence_minutes * 60} seconds >= {configured_retention} seconds"
            )
        if configured_failed_limit is not None:
            try:
                _validate_count_coverage(configured_failed_limit, cadence_minutes)
            except ValueError as error:
                errors.append(f"watcher cadence is unsafe for Failed: {error}")
        if configured_errored_limit is not None:
            try:
                _validate_count_coverage(configured_errored_limit, cadence_minutes)
            except ValueError as error:
                errors.append(f"watcher cadence is unsafe for Error: {error}")
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
        description="Verify the failure-watch window and cadence against Argo retention."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cron-manifest", type=Path, default=DEFAULT_CRON_MANIFEST)
    parser.add_argument(
        "--argo-config",
        type=Path,
        help="optional declarative-config Argo Application to compare",
    )
    args = parser.parse_args(argv)
    errors = check_configuration(
        args.manifest, cron_manifest=args.cron_manifest, argo_config=args.argo_config
    )
    if errors:
        for error in errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1
    print("PASS  failure-watch lookback and cadence satisfy Argo retention planning checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
