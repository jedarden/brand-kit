import json
from pathlib import Path
import shutil

import pytest
import yaml

from tools import check_automation_manifests


ROOT = Path(__file__).resolve().parent
AUTOMATION = ROOT / "automation"


def _copy_automation(tmp_path: Path) -> Path:
    destination = tmp_path / "automation"
    shutil.copytree(AUTOMATION, destination)
    return destination


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(path: Path, document) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_all_automation_manifests_pass_schema_and_contract_checks():
    assert check_automation_manifests.check_manifests(AUTOMATION) == []
    assert len(check_automation_manifests.discover_manifest_paths(AUTOMATION)) == 12


def test_schema_declares_only_ephemeral_argo_workflow_resources():
    schema = json.loads(
        (AUTOMATION / "manifest.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/workflowTemplate"},
        {"$ref": "#/$defs/cronWorkflow"},
    ]
    assert schema["$defs"]["metadata"]["properties"]["namespace"] == {
        "const": "argo-workflows"
    }
    assert schema["$defs"]["workflowTemplate"]["allOf"][1]["properties"]["kind"] == {
        "const": "WorkflowTemplate"
    }
    assert schema["$defs"]["cronWorkflow"]["allOf"][1]["properties"]["kind"] == {
        "const": "CronWorkflow"
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda document: document.update(apiVersion="v1"),
            "apiVersion",
        ),
        (
            lambda document: document["metadata"].update(namespace="default"),
            "metadata.namespace",
        ),
        (
            lambda document: document.update(kind="Deployment"),
            "kind must be WorkflowTemplate or CronWorkflow",
        ),
        (
            lambda document: document["metadata"].update(name="brand-kit_Invalid"),
            "metadata.name",
        ),
    ],
)
def test_manifest_shape_and_boundary_errors_are_reported(tmp_path, change, message):
    root = _copy_automation(tmp_path)
    path = root / "brand-kit-mirror-health-workflowtemplate.yml"
    document = _load(path)
    change(document)
    _write(path, document)

    errors = check_automation_manifests.check_manifests(root)

    assert any(message in error for error in errors)


def test_filename_kind_and_name_contract_is_enforced(tmp_path):
    root = _copy_automation(tmp_path)
    path = root / "brand-kit-mirror-health-workflowtemplate.yml"
    renamed = root / "brand-kit-renamed-cronworkflow.yml"
    document = _load(path)
    _write(renamed, document)
    path.unlink()

    errors = check_automation_manifests.check_manifests(root)

    assert any("filename requires kind CronWorkflow" in error for error in errors)
    assert any("metadata.name must match filename-derived name" in error for error in errors)


def test_schedule_contract_rejects_invalid_and_drifted_schedules(tmp_path):
    root = _copy_automation(tmp_path)
    path = root / "brand-kit-mirror-health-cronworkflow.yml"
    document = _load(path)
    document["spec"]["schedules"] = ["61 25 * * *"]
    _write(path, document)

    errors = check_automation_manifests.check_manifests(root)

    assert any("invalid cron schedule" in error for error in errors)
    assert any("schedule for brand-kit-mirror-health" in error for error in errors)


def test_template_and_cron_parameters_must_match(tmp_path):
    root = _copy_automation(tmp_path)
    path = root / "brand-kit-consumer-drift-cronworkflow.yml"
    document = _load(path)
    document["spec"]["workflowSpec"]["arguments"]["parameters"].pop()
    _write(path, document)

    errors = check_automation_manifests.check_manifests(root)

    assert any("parameters must match 'brand-kit-consumer-drift'" in error for error in errors)


def test_undefined_workflow_parameter_is_rejected(tmp_path):
    root = _copy_automation(tmp_path)
    path = root / "brand-kit-consumer-drift-workflowtemplate.yml"
    document = _load(path)
    document["spec"]["templates"][0]["container"]["args"][0] += "\necho {{workflow.parameters.unknown}}"
    _write(path, document)

    errors = check_automation_manifests.check_manifests(root)

    assert any("undeclared workflow parameter 'unknown'" in error for error in errors)


def test_recursive_discovery_catches_nested_non_workflow_resources(tmp_path):
    root = _copy_automation(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    _write(
        nested / "brand-kit-deployment.yml",
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "brand-kit-api", "namespace": "argo-workflows"},
            "spec": {},
        },
    )

    errors = check_automation_manifests.check_manifests(root)

    assert any("filename must end" in error for error in errors)
    assert any("kind must be WorkflowTemplate or CronWorkflow" in error for error in errors)


def test_application_contract_requires_recursive_yaml_inclusion_and_boundary(tmp_path):
    application = tmp_path / "brand-kit-automation-application.yml"
    document = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": check_automation_manifests.APPLICATION_NAME,
            "namespace": "argocd",
        },
        "spec": {
            "source": {
                "repoURL": check_automation_manifests.APPLICATION_REPOSITORY,
                "targetRevision": "main",
                "path": "automation",
                "directory": {"recurse": True, "include": "{*.yaml,*.yml}"},
            },
            "destination": {"namespace": "argo-workflows"},
        },
    }
    _write(application, document)

    assert check_automation_manifests.check_application(application) == []

    document["spec"]["source"]["directory"]["recurse"] = False
    document["spec"]["destination"]["namespace"] = "default"
    _write(application, document)
    errors = check_automation_manifests.check_application(application)
    assert any("recurse must be true" in error for error in errors)
    assert any("destination.namespace" in error for error in errors)
