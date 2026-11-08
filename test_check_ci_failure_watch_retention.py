from pathlib import Path

import pytest
import yaml

from tools import check_ci_failure_watch_retention


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "automation/brand-kit-ci-failure-watch-workflowtemplate.yml"
RETENTION_ANNOTATION = (
    "brand-kit.ardenone.com/argo-failure-retention-seconds"
)
LOOKBACK_ANNOTATION = "brand-kit.ardenone.com/failure-watch-lookback-minutes"
FAILED_LIMIT_ANNOTATION = "brand-kit.ardenone.com/argo-failed-workflow-limit"
ERRORED_LIMIT_ANNOTATION = "brand-kit.ardenone.com/argo-errored-workflow-limit"
CRON_MANIFEST = ROOT / "automation/brand-kit-ci-failure-watch-cronworkflow.yml"


def _write_argo_config(
    tmp_path, retention, *, failed_limit=20, errored_limit=10, values_object=False
):
    values = {
        "controller": {
            "retentionPolicy": {"failed": failed_limit, "errored": errored_limit},
            "workflowDefaults": {
                "spec": {"ttlStrategy": {"secondsAfterFailure": retention}}
            }
        }
    }
    helm = {"valuesObject": values} if values_object else {"values": yaml.safe_dump(values)}
    path = tmp_path / "argo-workflows-application.yml"
    path.write_text(
        yaml.safe_dump({"spec": {"source": {"helm": helm}}}),
        encoding="utf-8",
    )
    return path


def _write_manifest(
    tmp_path,
    *,
    retention=None,
    failed_limit=None,
    errored_limit=None,
    lookback=None,
    command_lookback=None,
    remove_annotations=(),
):
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    annotations = document["metadata"]["annotations"]
    if retention is not None:
        annotations[RETENTION_ANNOTATION] = str(retention)
    if failed_limit is not None:
        annotations[FAILED_LIMIT_ANNOTATION] = str(failed_limit)
    if errored_limit is not None:
        annotations[ERRORED_LIMIT_ANNOTATION] = str(errored_limit)
    if lookback is not None:
        annotations[LOOKBACK_ANNOTATION] = str(lookback)
    for name in remove_annotations:
        del annotations[name]
    if command_lookback is not None:
        command = document["spec"]["templates"][0]["container"]["args"][0]
        document["spec"]["templates"][0]["container"]["args"][0] = command.replace(
            "--lookback-minutes 120", f"--lookback-minutes {command_lookback}"
        )
    path = tmp_path / "workflowtemplate.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _write_cron_manifest(tmp_path, schedule):
    document = yaml.safe_load(CRON_MANIFEST.read_text(encoding="utf-8"))
    document["spec"]["schedules"] = [schedule]
    path = tmp_path / "cronworkflow.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_current_failure_watch_configuration_covers_argo_retention():
    assert check_ci_failure_watch_retention.check_configuration(MANIFEST) == []


def test_checker_accepts_valid_external_argo_retention(tmp_path):
    argo_config = _write_argo_config(tmp_path, 1800)

    assert (
        check_ci_failure_watch_retention.check_configuration(
            MANIFEST, argo_config=argo_config
        )
        == []
    )


def test_checker_accepts_the_inclusive_lookback_boundary(tmp_path):
    manifest = _write_manifest(tmp_path, lookback=120, command_lookback=120)
    argo_config = _write_argo_config(tmp_path, 7200, values_object=True)

    assert (
        check_ci_failure_watch_retention.check_configuration(
            manifest, argo_config=argo_config
        )
        == []
    )


def test_checker_rejects_a_lookback_one_second_short_of_configured_retention(tmp_path):
    manifest = _write_manifest(tmp_path, retention=7201, lookback=120, command_lookback=120)
    argo_config = _write_argo_config(tmp_path, 7201)

    errors = check_ci_failure_watch_retention.check_configuration(
        manifest, argo_config=argo_config
    )

    assert any("manifest lookback is unsafe" in error for error in errors)
    assert any("WorkflowTemplate lookback is unsafe" in error for error in errors)


def test_check_rejects_manifest_lookback_drift(tmp_path):
    manifest = _write_manifest(tmp_path, command_lookback=119)

    errors = check_ci_failure_watch_retention.check_configuration(manifest)

    assert any("command and lookback annotation differ" in error for error in errors)
    assert any("lookback is unsafe" in error for error in errors)


def test_checker_rejects_drifted_retention_annotation(tmp_path):
    manifest = _write_manifest(tmp_path, retention=3600)

    errors = check_ci_failure_watch_retention.check_configuration(manifest)

    assert any("retention annotation does not match watcher contract" in error for error in errors)


def test_check_compares_the_external_argo_controller_retention(tmp_path):
    argo_config = _write_argo_config(tmp_path, 7201)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("declarative-config Argo failure retention exceeds" in error for error in errors)


def test_checker_rejects_ttl_that_expires_before_the_next_nominal_scan(tmp_path):
    argo_config = _write_argo_config(tmp_path, 300)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("watcher cadence is unsafe for TTL" in error for error in errors)


def test_checker_rejects_count_cap_drift_from_the_controller(tmp_path):
    argo_config = _write_argo_config(tmp_path, 7200, failed_limit=12)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("Argo failed Workflow limit differs" in error for error in errors)


def test_checker_rejects_count_cap_drift_from_watcher_contract(tmp_path):
    manifest = _write_manifest(tmp_path, failed_limit=12)

    errors = check_ci_failure_watch_retention.check_configuration(manifest)

    assert any("failed Workflow limit annotation does not match" in error for error in errors)


def test_checker_rejects_errored_cap_drift_from_the_controller(tmp_path):
    argo_config = _write_argo_config(tmp_path, 7200, errored_limit=9)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("Argo errored Workflow limit differs" in error for error in errors)


def test_checker_rejects_errored_cap_drift_from_watcher_contract(tmp_path):
    manifest = _write_manifest(tmp_path, errored_limit=9)

    errors = check_ci_failure_watch_retention.check_configuration(manifest)

    assert any("errored Workflow limit annotation does not match" in error for error in errors)


@pytest.mark.parametrize("schedule", ("*/15 * * * *", "*/20 * * * *"))
def test_checker_rejects_cadence_exposing_failures_to_count_eviction(tmp_path, schedule):
    cron_manifest = _write_cron_manifest(tmp_path, schedule)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, cron_manifest=cron_manifest
    )

    assert any("watcher cadence is unsafe" in error for error in errors)
    assert any("cap 20" in error for error in errors)


def test_checker_accepts_cadence_below_count_cap_threshold(tmp_path):
    cron_manifest = _write_cron_manifest(tmp_path, "*/5 * * * *")

    assert check_ci_failure_watch_retention.check_configuration(
        MANIFEST, cron_manifest=cron_manifest
    ) == []


def test_count_cap_arithmetic_rejects_the_projected_boundary():
    check_ci_failure_watch_retention._validate_count_coverage(9, 5)

    with pytest.raises(ValueError, match="8 >= cap 8"):
        check_ci_failure_watch_retention._validate_count_coverage(8, 5)


@pytest.mark.parametrize("schedule", ("0 * * * *", "*/7 * * * *", "bad"))
def test_checker_rejects_unbounded_or_unsupported_cadence(tmp_path, schedule):
    cron_manifest = _write_cron_manifest(tmp_path, schedule)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, cron_manifest=cron_manifest
    )

    assert any("watcher cadence" in error or "watcher schedule" in error for error in errors)


@pytest.mark.parametrize(
    ("manifest_factory", "expected"),
    [
        (
            lambda tmp_path: _write_manifest(
                tmp_path, remove_annotations=(RETENTION_ANNOTATION,)
            ),
            "annotation 'brand-kit.ardenone.com/argo-failure-retention-seconds'",
        ),
        (
            lambda tmp_path: _write_manifest(
                tmp_path, remove_annotations=(FAILED_LIMIT_ANNOTATION,)
            ),
            "annotation 'brand-kit.ardenone.com/argo-failed-workflow-limit'",
        ),
        (
            lambda tmp_path: _write_manifest(
                tmp_path, remove_annotations=(ERRORED_LIMIT_ANNOTATION,)
            ),
            "annotation 'brand-kit.ardenone.com/argo-errored-workflow-limit'",
        ),
        (
            lambda tmp_path: _write_manifest(tmp_path, lookback="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/failure-watch-lookback-minutes'",
        ),
        (
            lambda tmp_path: _write_manifest(tmp_path, retention="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/argo-failure-retention-seconds'",
        ),
        (
            lambda tmp_path: _write_manifest(tmp_path, failed_limit="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/argo-failed-workflow-limit'",
        ),
        (
            lambda tmp_path: _write_manifest(tmp_path, errored_limit="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/argo-errored-workflow-limit'",
        ),
    ],
)
def test_checker_rejects_missing_or_malformed_watcher_annotations(
    tmp_path, manifest_factory, expected
):
    errors = check_ci_failure_watch_retention.check_configuration(manifest_factory(tmp_path))

    assert any(expected in error for error in errors)


def test_checker_rejects_missing_manifest(tmp_path):
    errors = check_ci_failure_watch_retention.check_configuration(
        tmp_path / "missing-workflowtemplate.yml"
    )

    assert any("could not parse" in error for error in errors)


def test_checker_rejects_malformed_or_missing_argo_configuration(tmp_path):
    malformed = tmp_path / "malformed-argo.yml"
    malformed.write_text("spec: [", encoding="utf-8")
    missing = tmp_path / "missing-argo.yml"

    malformed_errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=malformed
    )
    missing_errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=missing
    )

    assert any("could not parse" in error for error in malformed_errors)
    assert any("could not parse" in error for error in missing_errors)


def test_checker_rejects_argo_configuration_without_retention(tmp_path):
    argo_config = tmp_path / "incomplete-argo.yml"
    argo_config.write_text(
        yaml.safe_dump({"spec": {"source": {"helm": {"values": "{}"}}}}),
        encoding="utf-8",
    )

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("does not declare controller failure TTL and failed/errored Workflow limits" in error for error in errors)


def test_checker_rejects_argo_configuration_without_count_cap(tmp_path):
    argo_config = _write_argo_config(tmp_path, 7200)
    document = yaml.safe_load(argo_config.read_text(encoding="utf-8"))
    values = yaml.safe_load(document["spec"]["source"]["helm"]["values"])
    del values["controller"]["retentionPolicy"]
    document["spec"]["source"]["helm"]["values"] = yaml.safe_dump(values)
    argo_config.write_text(yaml.safe_dump(document), encoding="utf-8")

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("does not declare controller failure TTL and failed/errored Workflow limits" in error for error in errors)
