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


def _write_argo_config(tmp_path, retention, *, values_object=False):
    values = {
        "controller": {
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
    lookback=None,
    command_lookback=None,
    remove_annotations=(),
):
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    annotations = document["metadata"]["annotations"]
    if retention is not None:
        annotations[RETENTION_ANNOTATION] = str(retention)
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


def test_current_failure_watch_configuration_covers_argo_retention():
    assert check_ci_failure_watch_retention.check_configuration(MANIFEST) == []


def test_checker_accepts_valid_external_argo_retention(tmp_path):
    argo_config = _write_argo_config(tmp_path, 7200)

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
    argo_config = _write_argo_config(tmp_path, 3600)

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("declarative-config Argo failure retention differs" in error for error in errors)


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
            lambda tmp_path: _write_manifest(tmp_path, lookback="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/failure-watch-lookback-minutes'",
        ),
        (
            lambda tmp_path: _write_manifest(tmp_path, retention="not-an-integer"),
            "annotation 'brand-kit.ardenone.com/argo-failure-retention-seconds'",
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

    assert any("does not declare controller failure retention" in error for error in errors)
