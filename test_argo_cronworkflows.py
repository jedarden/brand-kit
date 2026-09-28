from pathlib import Path

import pytest
import yaml

from tools import brand_kit_workflow_liveness


AUTOMATION = Path(__file__).resolve().parent / "automation"
CRONWORKFLOW_CONTRACTS = (
    (
        "brand-kit-ci-failure-watch",
        "*/15 * * * *",
        (),
    ),
    (
        "brand-kit-consumer-drift",
        "17 6 * * *",
        (
            "brand-kit-repository",
            "site-repository",
            "release-tag",
            "release-commit",
        ),
    ),
    ("brand-kit-mirror-health", "17 */6 * * *", ()),
    ("brand-kit-platform-requirements", "27 6 * * *", ()),
    ("brand-kit-release-token-probe", "7 6 * * *", ()),
    ("brand-kit-workflow-liveness", "*/15 * * * *", ()),
)


def _load_manifest(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("workflow_name", "schedule", "required_parameters"),
    CRONWORKFLOW_CONTRACTS,
)
def test_scheduled_cronworkflow_contract(
    workflow_name, schedule, required_parameters
):
    cron_path = AUTOMATION / f"{workflow_name}-cronworkflow.yml"
    cron = _load_manifest(cron_path)
    spec = cron["spec"]
    workflow_spec = spec["workflowSpec"]

    assert cron["kind"] == "CronWorkflow"
    assert cron["metadata"]["name"] == workflow_name
    assert spec["schedules"] == [schedule]
    assert spec["timezone"] == "UTC"
    assert spec["concurrencyPolicy"] == "Forbid"

    template_name = workflow_spec["workflowTemplateRef"]["name"]
    assert template_name == workflow_name
    template = _load_manifest(
        AUTOMATION / f"{template_name}-workflowtemplate.yml"
    )
    assert template["kind"] == "WorkflowTemplate"
    assert template["metadata"]["name"] == template_name

    template_parameters = tuple(
        parameter["name"]
        for parameter in template["spec"].get("arguments", {}).get("parameters", [])
    )
    cron_parameters = tuple(
        parameter["name"]
        for parameter in workflow_spec.get("arguments", {}).get("parameters", [])
    )
    assert template_parameters == required_parameters
    assert cron_parameters == required_parameters


def test_contract_table_covers_every_scheduled_automation_manifest():
    actual_names = {
        path.name.removesuffix("-cronworkflow.yml")
        for path in AUTOMATION.rglob("*-cronworkflow.yml")
    }

    assert actual_names == {name for name, _, _ in CRONWORKFLOW_CONTRACTS}


def test_every_scheduled_cronworkflow_is_in_the_liveness_targets():
    scheduled_names = {
        path.name.removesuffix("-cronworkflow.yml")
        for path in AUTOMATION.rglob("*-cronworkflow.yml")
    }
    watchdog_name = "brand-kit-workflow-liveness"
    monitored_names = {target["name"] for target in brand_kit_workflow_liveness.TARGETS}

    assert watchdog_name in scheduled_names
    assert scheduled_names - {watchdog_name} == monitored_names
    assert all(
        target["workflow_template"] == target["name"]
        and target["max_age_minutes"] > 0
        for target in brand_kit_workflow_liveness.TARGETS
    )


def test_liveness_documentation_lists_every_enforced_target():
    documentation = (
        AUTOMATION.parent / "docs/notes/asset-toolchain.md"
    ).read_text(encoding="utf-8")
    liveness_section = documentation.split("### Scheduled workflow liveness\n", 1)[1]
    liveness_section = liveness_section.split("\n### ", 1)[0]
    documented_targets = {}
    for line in liveness_section.splitlines():
        if not line.startswith("| `brand-kit-"):
            continue
        name, schedule, max_age = (
            cell.strip() for cell in line.strip("|").split("|")
        )
        documented_targets[name.strip("`")] = (
            schedule.strip("`").strip(),
            max_age,
        )

    for target in brand_kit_workflow_liveness.TARGETS:
        cron = _load_manifest(
            AUTOMATION / f"{target['name']}-cronworkflow.yml"
        )
        schedules = ", ".join(cron["spec"]["schedules"])
        minutes = target["max_age_minutes"]
        if minutes % 60 == 0 and minutes > 60:
            max_age = f"{minutes // 60} hours"
        else:
            max_age = f"{minutes} minutes"
        assert documented_targets[target["name"]] == (schedules, max_age)


def test_failure_watch_runs_every_15_minutes_as_documented():
    cron = _load_manifest(
        AUTOMATION / "brand-kit-ci-failure-watch-cronworkflow.yml"
    )
    documentation = (
        AUTOMATION.parent / "docs/notes/asset-toolchain.md"
    ).read_text(encoding="utf-8")

    assert cron["spec"]["schedules"] == ["*/15 * * * *"]
    assert "Every 15 minutes it" in documentation
