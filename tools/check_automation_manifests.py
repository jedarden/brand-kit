#!/usr/bin/env python3
"""Validate every brand-kit Argo automation manifest before deployment.

The repository owns the reusable WorkflowTemplates and CronWorkflows under
``automation/``.  This check keeps their Argo/Kubernetes shape, names,
parameters, schedules, and deployment boundary aligned.  It can also inspect
the application-owned ArgoCD Application from declarative-config when that
checkout is available::

    python3 tools/check_automation_manifests.py
    python3 tools/check_automation_manifests.py \
      --application ../declarative-config/k8s/iad-ci/argo-workflows/\
brand-kit-automation-application.yml

The default check is local and deterministic; it never talks to a cluster or
applies resources.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parent.parent
AUTOMATION_ROOT = ROOT / "automation"
SCHEMA_PATH = AUTOMATION_ROOT / "manifest.schema.json"
ALLOWED_SUFFIXES = {".yml", ".yaml"}
ARGO_API_VERSION = "argoproj.io/v1alpha1"
WORKFLOW_NAMESPACE = "argo-workflows"
WORKFLOW_SERVICE_ACCOUNT = "argo-workflow"
WORKFLOW_TEMPLATE_SERVICE_ACCOUNTS = {
    "brand-kit-ci-failure-watch": "brand-kit-ci-failure-watch",
    "brand-kit-workflow-liveness": "brand-kit-workflow-liveness",
    "brand-kit-consumer-drift": "brand-kit-consumer-drift",
}
PARITY_WORKFLOW_NAME = "brand-kit-forgejo-github-parity"
APPLICATION_NAME = "brand-kit-automation-iad-ci"
APPLICATION_NAMESPACE = "argocd"
APPLICATION_REPOSITORY = "https://github.com/jedarden/brand-kit.git"
APPLICATION_REVISION = "main"
APPLICATION_PATH = "automation"
APPLICATION_INCLUDE = "{*.yaml,*.yml}"

EXPECTED_SCHEDULES = {
    "brand-kit-ci-failure-watch": ["*/15 * * * *"],
    "brand-kit-consumer-drift": ["17 6 * * *"],
    "brand-kit-mirror-health": ["17 */6 * * *"],
    "brand-kit-platform-requirements": ["27 6 * * *"],
    "brand-kit-release-token-probe": ["7 6 * * *"],
    "brand-kit-workflow-liveness": ["*/15 * * * *"],
}

PARAMETER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RESOURCE_NAME_RE = re.compile(r"^brand-kit-[a-z0-9]+(?:-[a-z0-9]+)*$")
WORKFLOW_PARAMETER_RE = re.compile(r"{{\s*workflow\.parameters\.([a-z][a-z0-9-]*)\s*}}")
INPUT_PARAMETER_RE = re.compile(r"{{\s*inputs\.parameters\.([a-z][a-z0-9-]*)\s*}}")
IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-fA-F]{64}$")
IMAGE_TAG_RE = re.compile(r"[^/]+:[^/]+$")


class _UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML loader that rejects duplicate YAML mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class Manifest:
    path: Path
    document: dict[str, Any]
    relative_path: str


def discover_manifest_paths(root: Path = AUTOMATION_ROOT) -> list[Path]:
    """Return every recursively deployable YAML file under *root*."""

    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES
    )


def _load_yaml_object(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            documents = list(yaml.load_all(handle, Loader=_UniqueKeyLoader))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"could not parse {path}: {error}") from error
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError(f"{path} must contain exactly one YAML object")
    return documents[0]


def _load_manifests(root: Path) -> tuple[list[Manifest], list[str]]:
    paths = discover_manifest_paths(root)
    if not paths:
        return [], [f"{root} contains no YAML automation manifests"]

    manifests: list[Manifest] = []
    errors: list[str] = []
    for path in paths:
        try:
            document = _load_yaml_object(path)
        except ValueError as error:
            errors.append(str(error))
            continue
        manifests.append(
            Manifest(
                path=path,
                document=document,
                relative_path=path.relative_to(root).as_posix(),
            )
        )
    return manifests, errors


def _schema_errors(document: Mapping[str, Any], path: Path) -> list[str]:
    """Run the checked-in JSON Schema when jsonschema is available.

    The explicit contract checks below intentionally remain authoritative so
    the repository check still works in the minimal Python image used by the
    Argo gate, where PyYAML is available but optional schema tooling may not be.
    """

    try:
        import jsonschema
    except ImportError:
        return []

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path}: could not load {SCHEMA_PATH}: {error}"]

    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda item: list(item.path)):
        location = ".".join(str(part) for part in error.path)
        suffix = f" at {location}" if location else ""
        errors.append(f"{path}: schema violation{suffix}: {error.message}")
    return errors


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _string_values(child)


def _path_error(path: Path, message: str) -> str:
    return f"{path}: {message}"


def _parameter_names(
    arguments: Any, *, path: Path, context: str
) -> tuple[set[str], list[str]]:
    if arguments is None:
        return set(), []
    if not isinstance(arguments, Mapping):
        return set(), [_path_error(path, f"{context}.arguments must be an object")]
    parameters = arguments.get("parameters")
    if not isinstance(parameters, list):
        return set(), [_path_error(path, f"{context}.arguments.parameters must be a list")]

    names: set[str] = set()
    errors: list[str] = []
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, Mapping):
            errors.append(
                _path_error(path, f"{context} parameter {index} must be an object")
            )
            continue
        name = parameter.get("name")
        if not isinstance(name, str) or not PARAMETER_NAME_RE.fullmatch(name):
            errors.append(
                _path_error(path, f"{context} parameter {index} has invalid name")
            )
            continue
        if name in names:
            errors.append(_path_error(path, f"{context} repeats parameter {name!r}"))
        names.add(name)
        if set(parameter) != {"name", "value"}:
            errors.append(
                _path_error(
                    path,
                    f"{context} parameter {name!r} must contain only name and value",
                )
            )
        elif not isinstance(parameter["value"], str):
            errors.append(
                _path_error(path, f"{context} parameter {name!r} value must be a string")
            )
    return names, errors


def _cron_field_is_valid(value: str, minimum: int, maximum: int) -> bool:
    if not value or value.startswith(",") or value.endswith(","):
        return False
    for item in value.split(","):
        if item.count("/") > 1:
            return False
        base, separator, step = item.partition("/")
        if separator:
            if not step.isdigit() or int(step) < 1:
                return False
        if base == "*":
            continue
        if base.count("-") > 1:
            return False
        if "-" in base:
            start, end = base.split("-")
            if not start.isdigit() or not end.isdigit():
                return False
            if not minimum <= int(start) <= int(end) <= maximum:
                return False
        elif not base.isdigit() or not minimum <= int(base) <= maximum:
            return False
    return True


def _valid_cron_schedule(schedule: Any) -> bool:
    if not isinstance(schedule, str) or schedule != schedule.strip():
        return False
    fields = schedule.split()
    if len(fields) != 5:
        return False
    return all(
        _cron_field_is_valid(field, minimum, maximum)
        for field, minimum, maximum in zip(
            fields,
            (0, 0, 1, 1, 0),
            (59, 23, 31, 12, 7),
        )
    )


def _image_is_pinned(image: Any) -> bool:
    if not isinstance(image, str) or not image or image.endswith(":latest"):
        return False
    return bool(IMAGE_DIGEST_RE.search(image) or IMAGE_TAG_RE.search(image))


def _template_names(spec: Mapping[str, Any], path: Path) -> tuple[set[str], list[str]]:
    templates = spec.get("templates")
    if not isinstance(templates, list):
        return set(), [_path_error(path, "spec.templates must be a list")]
    names: set[str] = set()
    errors: list[str] = []
    for index, template in enumerate(templates):
        if not isinstance(template, Mapping):
            errors.append(_path_error(path, f"spec.templates[{index}] must be an object"))
            continue
        name = template.get("name")
        if not isinstance(name, str) or not PARAMETER_NAME_RE.fullmatch(name):
            errors.append(_path_error(path, f"spec.templates[{index}] has invalid name"))
            continue
        if name in names:
            errors.append(_path_error(path, f"spec.templates repeats {name!r}"))
        names.add(name)
    return names, errors


def _check_template_body(
    manifest: Manifest,
    spec: Mapping[str, Any],
    names: set[str],
    *,
    require_on_exit: bool = True,
) -> list[str]:
    path = manifest.path
    errors: list[str] = []
    entrypoint = spec.get("entrypoint")
    if not isinstance(entrypoint, str) or entrypoint not in names:
        errors.append(_path_error(path, "spec.entrypoint must name a template"))
    on_exit = spec.get("onExit")
    if require_on_exit and (not isinstance(on_exit, str) or on_exit not in names):
        errors.append(_path_error(path, "spec.onExit must name a template"))

    templates = spec.get("templates")
    if not isinstance(templates, list):
        return errors
    for index, template in enumerate(templates):
        if not isinstance(template, Mapping):
            continue
        context = f"spec.templates[{index}]"
        containers = template.get("container")
        if containers is not None:
            if not isinstance(containers, Mapping):
                errors.append(_path_error(path, f"{context}.container must be an object"))
            elif not _image_is_pinned(containers.get("image")):
                errors.append(
                    _path_error(path, f"{context}.container.image must be version-pinned")
                )

        for step_group in template.get("steps", []) if isinstance(template.get("steps"), list) else []:
            if not isinstance(step_group, list):
                errors.append(_path_error(path, f"{context}.steps entries must be lists"))
                continue
            for step in step_group:
                if not isinstance(step, Mapping):
                    errors.append(_path_error(path, f"{context}.steps item must be an object"))
                    continue
                referenced = step.get("template")
                if not isinstance(referenced, str) or referenced not in names:
                    errors.append(
                        _path_error(path, f"{context}.steps references unknown template")
                    )

        dag = template.get("dag")
        if isinstance(dag, Mapping):
            tasks = dag.get("tasks", [])
            if not isinstance(tasks, list):
                errors.append(_path_error(path, f"{context}.dag.tasks must be a list"))
            else:
                for task in tasks:
                    if not isinstance(task, Mapping):
                        continue
                    referenced = task.get("template")
                    if not isinstance(referenced, str) or referenced not in names:
                        errors.append(
                            _path_error(path, f"{context}.dag references unknown template")
                        )

        if template.get("daemon") is True:
            errors.append(_path_error(path, f"{context} must not run a daemon workload"))

        inputs = template.get("inputs")
        if inputs is None:
            input_names = set()
        elif not isinstance(inputs, Mapping):
            input_names = set()
            errors.append(_path_error(path, f"{context}.inputs must be an object"))
        elif "parameters" not in inputs:
            input_names = set()
        else:
            input_names, input_errors = _parameter_names(
                inputs,
                path=path,
                context=f"{context}.inputs",
            )
            errors.extend(input_errors)
        for text in _string_values(template):
            for parameter in INPUT_PARAMETER_RE.findall(text):
                if parameter not in input_names:
                    errors.append(
                        _path_error(
                            path,
                            f"{context} references undeclared input parameter {parameter!r}",
                        )
                    )
    return errors


def _check_manifest_shape(manifest: Manifest) -> list[str]:
    path = manifest.path
    document = manifest.document
    errors = _schema_errors(document, path)

    api_version = document.get("apiVersion")
    if api_version != ARGO_API_VERSION:
        errors.append(_path_error(path, f"apiVersion must be {ARGO_API_VERSION!r}"))
    kind = document.get("kind")
    if kind not in {"WorkflowTemplate", "CronWorkflow", "Workflow"}:
        errors.append(
            _path_error(path, "kind must be WorkflowTemplate or CronWorkflow or Workflow")
        )
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return errors + [_path_error(path, "metadata must be an object")]
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(name, str) or not RESOURCE_NAME_RE.fullmatch(name):
        errors.append(_path_error(path, "metadata.name is not a brand-kit DNS name"))
    if namespace != WORKFLOW_NAMESPACE:
        errors.append(_path_error(path, f"metadata.namespace must be {WORKFLOW_NAMESPACE!r}"))
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        return errors + [_path_error(path, "spec must be an object")]

    if kind == "WorkflowTemplate":
        expected_service_account = WORKFLOW_TEMPLATE_SERVICE_ACCOUNTS.get(
            name, WORKFLOW_SERVICE_ACCOUNT
        )
        if spec.get("serviceAccountName") != expected_service_account:
            errors.append(
                _path_error(
                    path,
                    f"spec.serviceAccountName must be {expected_service_account!r}",
                )
            )
        deadline = spec.get("activeDeadlineSeconds")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1:
            errors.append(_path_error(path, "spec.activeDeadlineSeconds must be positive"))
        names, template_errors = _template_names(spec, path)
        errors.extend(template_errors)
        errors.extend(_check_template_body(manifest, spec, names))
        parameter_names, parameter_errors = _parameter_names(
            spec.get("arguments"), path=path, context="spec"
        )
        errors.extend(parameter_errors)
        for text in _string_values(document):
            for parameter in WORKFLOW_PARAMETER_RE.findall(text):
                if parameter not in parameter_names:
                    errors.append(
                        _path_error(
                            path,
                            f"references undeclared workflow parameter {parameter!r}",
                        )
                    )
    elif kind == "Workflow":
        annotations = metadata.get("annotations")
        if not isinstance(annotations, Mapping):
            errors.append(_path_error(path, "Workflow metadata.annotations must be an object"))
        else:
            expected_annotations = {
                "argocd.argoproj.io/hook": "PreSync",
                "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
                "argocd.argoproj.io/sync-wave": "-1",
            }
            for key, expected in expected_annotations.items():
                if annotations.get(key) != expected:
                    errors.append(
                        _path_error(path, f"metadata.annotations.{key} must be {expected!r}")
                    )
        if name != PARITY_WORKFLOW_NAME:
            errors.append(
                _path_error(path, f"Workflow metadata.name must be {PARITY_WORKFLOW_NAME!r}")
            )
        if spec.get("serviceAccountName") != WORKFLOW_SERVICE_ACCOUNT:
            errors.append(
                _path_error(
                    path,
                    f"spec.serviceAccountName must be {WORKFLOW_SERVICE_ACCOUNT!r}",
                )
            )
        if spec.get("automountServiceAccountToken") is not False:
            errors.append(_path_error(path, "spec.automountServiceAccountToken must be false"))
        deadline = spec.get("activeDeadlineSeconds")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1:
            errors.append(_path_error(path, "spec.activeDeadlineSeconds must be positive"))
        names, template_errors = _template_names(spec, path)
        errors.extend(template_errors)
        errors.extend(_check_template_body(manifest, spec, names, require_on_exit=False))
        templates = spec.get("templates")
        if isinstance(templates, list) and len(templates) == 1:
            container = templates[0].get("container") if isinstance(templates[0], Mapping) else None
            script = container.get("args", [None])[0] if isinstance(container, Mapping) and isinstance(container.get("args"), list) else None
            if not isinstance(container, Mapping) or not _image_is_pinned(container.get("image")):
                errors.append(_path_error(path, "parity Workflow container.image must be version-pinned"))
            if not isinstance(container, Mapping) or container.get("name") != "parity":
                errors.append(_path_error(path, "parity Workflow container.name must be 'parity'"))
            if not isinstance(container, Mapping) or container.get("command") != ["sh", "-ec"]:
                errors.append(_path_error(path, "parity Workflow container.command must run sh -ec"))
            if not isinstance(script, str):
                errors.append(_path_error(path, "parity Workflow container.args must contain a shell script"))
            else:
                container_text = "\n".join(_string_values(container))
                for required in (
                    "git ls-remote --exit-code --refs --quiet",
                    "refs/heads/main",
                    "https://git.ardenone.com/jedarden/brand-kit.git",
                    "https://github.com/jedarden/brand-kit.git",
                ):
                    if required not in (script if required.startswith("git ") or required.startswith("refs/") else container_text):
                        errors.append(_path_error(path, f"parity Workflow contract must contain {required!r}"))
                if "exit 1" not in script:
                    errors.append(_path_error(path, "parity Workflow script must fail on disagreement"))
    elif kind == "CronWorkflow":
        schedules = spec.get("schedules")
        if not isinstance(schedules, list) or not schedules:
            errors.append(_path_error(path, "spec.schedules must be a non-empty list"))
        else:
            if len(schedules) != 1:
                errors.append(_path_error(path, "spec.schedules must contain one schedule"))
            for schedule in schedules:
                if not _valid_cron_schedule(schedule):
                    errors.append(_path_error(path, f"invalid cron schedule {schedule!r}"))
            if isinstance(name, str) and name in EXPECTED_SCHEDULES and schedules != EXPECTED_SCHEDULES[name]:
                errors.append(
                    _path_error(
                        path,
                        f"schedule for {name} must be {EXPECTED_SCHEDULES[name]!r}",
                    )
                )
        if spec.get("timezone") != "UTC":
            errors.append(_path_error(path, "spec.timezone must be UTC"))
        if spec.get("concurrencyPolicy") != "Forbid":
            errors.append(_path_error(path, "spec.concurrencyPolicy must be Forbid"))
        deadline = spec.get("startingDeadlineSeconds")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1:
            errors.append(
                _path_error(path, "spec.startingDeadlineSeconds must be positive")
            )
        workflow_spec = spec.get("workflowSpec")
        if not isinstance(workflow_spec, Mapping):
            errors.append(_path_error(path, "spec.workflowSpec must be an object"))
        else:
            reference = workflow_spec.get("workflowTemplateRef")
            if not isinstance(reference, Mapping) or not isinstance(reference.get("name"), str):
                errors.append(_path_error(path, "workflowSpec.workflowTemplateRef.name is required"))
            cron_params, parameter_errors = _parameter_names(
                workflow_spec.get("arguments"),
                path=path,
                context="spec.workflowSpec",
            )
            errors.extend(parameter_errors)
            for text in _string_values(workflow_spec):
                for parameter in WORKFLOW_PARAMETER_RE.findall(text):
                    if parameter not in cron_params:
                        errors.append(
                            _path_error(
                                path,
                                f"references undeclared cron workflow parameter {parameter!r}",
                            )
                        )
    return errors


def _check_filename_contract(manifest: Manifest) -> list[str]:
    path = manifest.path
    stem = path.stem
    if stem.endswith("-workflowtemplate"):
        expected_kind = "WorkflowTemplate"
        expected_name = stem[: -len("-workflowtemplate")]
    elif stem.endswith("-cronworkflow"):
        expected_kind = "CronWorkflow"
        expected_name = stem[: -len("-cronworkflow")]
    elif stem.endswith("-workflow"):
        expected_kind = "Workflow"
        expected_name = stem[: -len("-workflow")]
    else:
        return [_path_error(path, "filename must end in -workflowtemplate or -cronworkflow")]

    errors: list[str] = []
    document = manifest.document
    if document.get("kind") != expected_kind:
        errors.append(_path_error(path, f"filename requires kind {expected_kind}"))
    metadata = document.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("name") != expected_name:
        errors.append(
            _path_error(
                path,
                f"metadata.name must match filename-derived name {expected_name!r}",
            )
        )
    return errors


def _check_cross_manifest_contract(manifests: list[Manifest]) -> list[str]:
    errors: list[str] = []
    templates: dict[str, Manifest] = {}
    crons: dict[str, Manifest] = {}
    hooks: dict[str, Manifest] = {}
    for manifest in manifests:
        document = manifest.document
        name = document.get("metadata", {}).get("name") if isinstance(document.get("metadata"), Mapping) else None
        kind = document.get("kind")
        if not isinstance(name, str):
            continue
        target = (
            templates
            if kind == "WorkflowTemplate"
            else crons
            if kind == "CronWorkflow"
            else hooks
            if kind == "Workflow"
            else None
        )
        if target is None:
            continue
        if name in target:
            errors.append(
                _path_error(manifest.path, f"duplicate {kind} metadata.name {name!r}")
            )
        else:
            target[name] = manifest

    expected_names = set(EXPECTED_SCHEDULES)
    if set(templates) != expected_names:
        errors.append(
            "automation must contain exactly one WorkflowTemplate for each scheduled "
            f"workflow: missing={sorted(expected_names - set(templates))}, "
            f"unexpected={sorted(set(templates) - expected_names)}"
        )
    if set(crons) != expected_names:
        errors.append(
            "automation must contain exactly one CronWorkflow for each scheduled "
            f"workflow: missing={sorted(expected_names - set(crons))}, "
            f"unexpected={sorted(set(crons) - expected_names)}"
        )
    if set(hooks) != {PARITY_WORKFLOW_NAME}:
        errors.append(
            "automation must contain exactly one Forgejo/GitHub parity PreSync Workflow: "
            f"missing={sorted({PARITY_WORKFLOW_NAME} - set(hooks))}, "
            f"unexpected={sorted(set(hooks) - {PARITY_WORKFLOW_NAME})}"
        )

    for name, cron in crons.items():
        spec = cron.document.get("spec")
        workflow_spec = spec.get("workflowSpec") if isinstance(spec, Mapping) else None
        reference = workflow_spec.get("workflowTemplateRef") if isinstance(workflow_spec, Mapping) else None
        referenced_name = reference.get("name") if isinstance(reference, Mapping) else None
        if referenced_name != name:
            errors.append(_path_error(cron.path, f"workflowTemplateRef.name must be {name!r}"))
        if referenced_name not in templates:
            errors.append(_path_error(cron.path, f"references missing WorkflowTemplate {referenced_name!r}"))
            continue

        template_spec = templates[name].document.get("spec")
        template_params, _ = _parameter_names(
            template_spec.get("arguments") if isinstance(template_spec, Mapping) else None,
            path=templates[name].path,
            context="spec",
        )
        cron_params, _ = _parameter_names(
            workflow_spec.get("arguments") if isinstance(workflow_spec, Mapping) else None,
            path=cron.path,
            context="spec.workflowSpec",
        )
        if cron_params != template_params:
            errors.append(
                _path_error(
                    cron.path,
                    f"parameters must match {name!r} template: "
                    f"cron={sorted(cron_params)}, template={sorted(template_params)}",
                )
            )
    return errors


def check_manifests(root: Path = AUTOMATION_ROOT) -> list[str]:
    """Return all local schema and contract errors for *root*."""

    manifests, errors = _load_manifests(root)
    for manifest in manifests:
        errors.extend(_check_filename_contract(manifest))
        errors.extend(_check_manifest_shape(manifest))
    errors.extend(_check_cross_manifest_contract(manifests))
    return errors


def _application_contract_errors(document: Mapping[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    if document.get("apiVersion") != ARGO_API_VERSION:
        errors.append(_path_error(path, f"Application apiVersion must be {ARGO_API_VERSION!r}"))
    if document.get("kind") != "Application":
        errors.append(_path_error(path, "deployment object kind must be Application"))
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        return errors + [_path_error(path, "Application metadata must be an object")]
    if metadata.get("name") != APPLICATION_NAME:
        errors.append(_path_error(path, f"Application metadata.name must be {APPLICATION_NAME!r}"))
    if metadata.get("namespace") != APPLICATION_NAMESPACE:
        errors.append(_path_error(path, f"Application metadata.namespace must be {APPLICATION_NAMESPACE!r}"))

    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        return errors + [_path_error(path, "Application spec must be an object")]
    source = spec.get("source")
    if not isinstance(source, Mapping):
        errors.append(_path_error(path, "Application spec.source must be an object"))
    else:
        for key, expected in (
            ("repoURL", APPLICATION_REPOSITORY),
            ("targetRevision", APPLICATION_REVISION),
            ("path", APPLICATION_PATH),
        ):
            if source.get(key) != expected:
                errors.append(_path_error(path, f"Application source.{key} must be {expected!r}"))
        directory = source.get("directory")
        if not isinstance(directory, Mapping):
            errors.append(_path_error(path, "Application source.directory must be an object"))
        else:
            if directory.get("recurse") is not True:
                errors.append(_path_error(path, "Application source.directory.recurse must be true"))
            if directory.get("include") != APPLICATION_INCLUDE:
                errors.append(
                    _path_error(
                        path,
                        f"Application source.directory.include must be {APPLICATION_INCLUDE!r}",
                    )
                )
    destination = spec.get("destination")
    if not isinstance(destination, Mapping):
        errors.append(_path_error(path, "Application spec.destination must be an object"))
    elif destination.get("namespace") != WORKFLOW_NAMESPACE:
        errors.append(
            _path_error(path, f"Application destination.namespace must be {WORKFLOW_NAMESPACE!r}")
        )
    return errors


def check_application(path: Path) -> list[str]:
    """Validate the external ArgoCD Application that recursively deploys us."""

    try:
        document = _load_yaml_object(path)
    except ValueError as error:
        return [str(error)]
    return _application_contract_errors(document, path)


def check_repository(
    root: Path = AUTOMATION_ROOT, *, application: Path | None = None
) -> list[str]:
    errors = check_manifests(root)
    if application is not None:
        errors.extend(check_application(application))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate all brand-kit Argo automation manifests and deployment contracts."
    )
    parser.add_argument("--root", type=Path, default=AUTOMATION_ROOT)
    parser.add_argument(
        "--application",
        type=Path,
        help="optional declarative-config Application manifest to validate",
    )
    args = parser.parse_args(argv)

    errors = check_repository(args.root, application=args.application)
    if errors:
        for error in errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1

    count = len(discover_manifest_paths(args.root))
    print(f"PASS  validated {count} recursive Argo automation manifests")
    if args.application is not None:
        print(f"PASS  validated deployment Application {args.application}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
