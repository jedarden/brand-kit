from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from tools import check_release_mirror


COMMIT = "a" * 40
MIRROR_COMMIT = "b" * 40
NOW = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def test_matching_peeled_tags_are_healthy_and_do_not_check_github_releases():
    calls = []

    def execute(arguments, cwd, check):
        calls.append((arguments, cwd, check))
        repository = arguments[2]
        return subprocess.CompletedProcess(
            arguments,
            0,
            f"{COMMIT}\trefs/tags/v1.2.3^{{}}\n",
            "",
        )

    report = check_release_mirror.audit_expected_tag(
        "v1.2.3",
        COMMIT,
        "https://forgejo.example/brand-kit.git",
        "https://github.example/brand-kit.git",
        execute=execute,
        now=NOW,
    )

    assert report["status"] == "healthy"
    assert report["release_authority"] == "Forgejo"
    assert report["github_release_checked"] is False
    assert report["canonical_tag"]["status"] == "match"
    assert report["mirror_tag"]["status"] == "match"
    assert [call[0][0] for call in calls] == ["ls-remote", "ls-remote"]
    assert all("push" not in call[0] for call in calls)
    assert all("fetch" not in call[0] for call in calls)
    assert all("api.github.com" not in " ".join(call[0]) for call in calls)
    assert all(call[2] is False for call in calls)


def test_missing_mirror_tag_is_indeterminate_during_transient_lag():
    def execute(arguments, cwd, check):
        if arguments[2] == "https://forgejo.example/brand-kit.git":
            return subprocess.CompletedProcess(
                arguments,
                0,
                f"{COMMIT}\trefs/tags/v1.2.3^{{}}\n",
                "",
            )
        return subprocess.CompletedProcess(arguments, 2, "", "")

    report = check_release_mirror.audit_expected_tag(
        "v1.2.3",
        COMMIT,
        "https://forgejo.example/brand-kit.git",
        "https://github.example/brand-kit.git",
        execute=execute,
        now=NOW,
    )

    assert report["status"] == "indeterminate"
    assert report["mirror_tag"]["status"] == "lagging"
    assert "not exposed" in report["mirror_tag"]["reason"]


def test_mismatched_canonical_tag_is_unhealthy_and_does_not_authorize_mirror():
    def execute(arguments, cwd, check):
        return subprocess.CompletedProcess(
            arguments,
            0,
            f"{MIRROR_COMMIT}\trefs/tags/v1.2.3^{{}}\n",
            "",
        )

    report = check_release_mirror.audit_expected_tag(
        "v1.2.3",
        COMMIT,
        "https://forgejo.example/brand-kit.git",
        "https://github.example/brand-kit.git",
        execute=execute,
        now=NOW,
    )

    assert report["status"] == "unhealthy"
    assert report["canonical_tag"]["status"] == "mismatch"
    assert report["mirror_tag"]["status"] == "not_evaluated"


def test_mismatched_mirror_tag_is_unhealthy_not_a_release_authorization():
    calls = 0

    def execute(arguments, cwd, check):
        nonlocal calls
        calls += 1
        commit = COMMIT if calls == 1 else MIRROR_COMMIT
        return subprocess.CompletedProcess(
            arguments,
            0,
            f"{commit}\trefs/tags/v1.2.3^{{}}\n",
            "",
        )

    report = check_release_mirror.audit_expected_tag(
        "v1.2.3",
        COMMIT,
        "https://forgejo.example/brand-kit.git",
        "https://github.example/brand-kit.git",
        execute=execute,
        now=NOW,
    )

    assert report["status"] == "unhealthy"
    assert report["mirror_tag"]["status"] == "mismatch"


def test_remote_read_failure_is_indeterminate_and_keeps_credentials_out_of_errors():
    def execute(arguments, cwd, check):
        return subprocess.CompletedProcess(arguments, 128, "", "fatal: auth failed")

    report = check_release_mirror.audit_expected_tag(
        "v1.2.3",
        COMMIT,
        "https://forgejo.example/brand-kit.git",
        "https://github.example/brand-kit.git",
        execute=execute,
        now=NOW,
    )

    assert report["status"] == "indeterminate"
    assert report["canonical_tag"]["status"] == "indeterminate"
    assert "auth failed" in report["canonical_tag"]["reason"]


@pytest.mark.parametrize("tag", ("1.2.3", "v1.2", "v1.2.3 "))
def test_tag_validation_is_strict(tag):
    with pytest.raises(check_release_mirror.ReleaseMirrorError):
        check_release_mirror.audit_expected_tag(tag, COMMIT)


def test_contract_documentation_keeps_forgejo_authoritative_and_read_only():
    release = Path("docs/notes/release-publication.md").read_text(encoding="utf-8")
    mirror = Path(
        "automation/brand-kit-mirror-health-workflowtemplate.yml"
    ).read_text(encoding="utf-8")

    assert "tools/check_release_mirror.py" in release
    assert "GitHub Release" in release
    assert "does not query" in release
    assert "transient mirror lag" in release
    assert "tools/check_release_mirror.py" in mirror
    assert "indeterminate" in mirror
    assert "GitHub Release" in mirror
    assert "git push" not in mirror
