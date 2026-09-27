from pathlib import Path

import yaml

from tools import check_ci_failure_watch_retention


MANIFEST = Path("automation/brand-kit-ci-failure-watch-workflowtemplate.yml")


def test_current_failure_watch_configuration_covers_argo_retention():
    assert check_ci_failure_watch_retention.check_configuration(MANIFEST) == []


def test_check_rejects_manifest_lookback_drift(tmp_path):
    manifest = tmp_path / "workflowtemplate.yml"
    manifest.write_text(
        MANIFEST.read_text(encoding="utf-8").replace(
            "--lookback-minutes 120", "--lookback-minutes 119"
        ),
        encoding="utf-8",
    )

    errors = check_ci_failure_watch_retention.check_configuration(manifest)

    assert any("command and lookback annotation differ" in error for error in errors)
    assert any("lookback is unsafe" in error for error in errors)


def test_check_compares_the_external_argo_controller_retention(tmp_path):
    argo_config = tmp_path / "argo-workflows-application.yml"
    argo_config.write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "source": {
                        "helm": {
                            "values": yaml.safe_dump(
                                {
                                    "controller": {
                                        "workflowDefaults": {
                                            "spec": {
                                                "ttlStrategy": {
                                                    "secondsAfterFailure": 3600
                                                }
                                            }
                                        }
                                    }
                                }
                            )
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    errors = check_ci_failure_watch_retention.check_configuration(
        MANIFEST, argo_config=argo_config
    )

    assert any("declarative-config Argo failure retention differs" in error for error in errors)
