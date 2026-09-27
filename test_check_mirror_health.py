from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from tools import check_mirror_health


CANONICAL_MAIN = "a" * 40
MIRROR_MAIN = "b" * 40
CANONICAL_TAG = "c" * 40
MIRROR_TAG = "d" * 40
NOW = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def refs(*, branches=None, tags=None):
    return {
        "branch": branches or {},
        "tag": tags or {},
    }


def git(root, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_parse_remote_refs_uses_peeled_tag_commit_for_annotated_tags():
    parsed = check_mirror_health._parse_remote_refs(
        "".join(
            (
                f"{'e' * 40}\trefs/tags/v1.0.0\n",
                f"{CANONICAL_TAG}\trefs/tags/v1.0.0^{{}}\n",
            )
        ),
        "tag",
    )

    assert parsed == {"refs/tags/v1.0.0": CANONICAL_TAG}


def test_matching_branches_and_tags_are_healthy():
    source = refs(
        branches={"refs/heads/main": CANONICAL_MAIN},
        tags={"refs/tags/v1.0.0": CANONICAL_TAG},
    )
    report = check_mirror_health.compare_ref_sets(
        source,
        source,
        is_ancestor=lambda *args: pytest.fail("matching refs do not need ancestry"),
    )

    assert [check["status"] for check in report] == ["match", "match"]


@pytest.mark.parametrize(
    ("kind", "ref", "object_id"),
    (
        ("branch", "refs/heads/main", CANONICAL_MAIN),
        ("tag", "refs/tags/v1.0.0", CANONICAL_TAG),
    ),
)
def test_missing_refs_are_reported(kind, ref, object_id):
    checks = check_mirror_health.compare_ref_sets(
        refs(**{"branches" if kind == "branch" else "tags": {ref: object_id}}),
        refs(),
        is_ancestor=lambda *args: pytest.fail("missing refs do not need ancestry"),
    )

    assert checks == [
        {
            "kind": kind,
            "ref": ref,
            "status": "missing",
            "canonical": object_id,
            "mirror": None,
            "reason": "mirror does not contain the canonical ref",
        }
    ]


@pytest.mark.parametrize(
    ("kind", "ref", "canonical_id", "mirror_id"),
    (
        ("branch", "refs/heads/main", CANONICAL_MAIN, MIRROR_MAIN),
        ("tag", "refs/tags/v1.0.0", CANONICAL_TAG, MIRROR_TAG),
    ),
)
def test_behind_refs_are_stale(kind, ref, canonical_id, mirror_id):
    checks = check_mirror_health.compare_ref_sets(
        refs(**{"branches" if kind == "branch" else "tags": {ref: canonical_id}}),
        refs(**{"branches" if kind == "branch" else "tags": {ref: mirror_id}}),
        is_ancestor=lambda older, newer, *_: (older, newer) == (mirror_id, canonical_id),
    )

    assert checks[0]["status"] == "stale"
    assert checks[0]["reason"] == "mirror ref is an ancestor of the canonical ref"


@pytest.mark.parametrize(
    ("kind", "ref", "canonical_id", "mirror_id"),
    (
        ("branch", "refs/heads/main", CANONICAL_MAIN, MIRROR_MAIN),
        ("tag", "refs/tags/v1.0.0", CANONICAL_TAG, MIRROR_TAG),
    ),
)
def test_unrelated_refs_are_divergent(kind, ref, canonical_id, mirror_id):
    checks = check_mirror_health.compare_ref_sets(
        refs(**{"branches" if kind == "branch" else "tags": {ref: canonical_id}}),
        refs(**{"branches" if kind == "branch" else "tags": {ref: mirror_id}}),
        is_ancestor=lambda *_: False,
    )

    assert checks[0]["status"] == "divergent"
    assert checks[0]["reason"] == "canonical and mirror refs have unrelated history"


@pytest.mark.parametrize(
    ("kind", "ref", "canonical_id", "mirror_id"),
    (
        ("branch", "refs/heads/main", CANONICAL_MAIN, MIRROR_MAIN),
        ("tag", "refs/tags/v1.0.0", CANONICAL_TAG, MIRROR_TAG),
    ),
)
def test_ahead_refs_are_divergent(kind, ref, canonical_id, mirror_id):
    checks = check_mirror_health.compare_ref_sets(
        refs(**{"branches" if kind == "branch" else "tags": {ref: canonical_id}}),
        refs(**{"branches" if kind == "branch" else "tags": {ref: mirror_id}}),
        is_ancestor=lambda older, newer, *_: (older, newer) == (canonical_id, mirror_id),
    )

    assert checks[0]["status"] == "divergent"
    assert checks[0]["reason"] == "mirror ref is ahead of the canonical ref"


def test_mirror_only_ref_is_divergent():
    checks = check_mirror_health.compare_ref_sets(
        refs(),
        refs(branches={"refs/heads/feature": MIRROR_MAIN}),
        is_ancestor=lambda *args: pytest.fail("extra refs do not need ancestry"),
    )

    assert checks[0]["status"] == "divergent"
    assert checks[0]["canonical"] is None


def test_ancestry_failure_is_indeterminate():
    checks = check_mirror_health.compare_ref_sets(
        refs(branches={"refs/heads/main": CANONICAL_MAIN}),
        refs(branches={"refs/heads/main": MIRROR_MAIN}),
        is_ancestor=lambda *args: (_ for _ in ()).throw(
            check_mirror_health.MirrorHealthError("object unavailable")
        ),
    )

    assert checks[0]["status"] == "indeterminate"
    assert "object unavailable" in checks[0]["reason"]


def test_run_check_uses_remote_ancestry_to_find_stale_and_extra_refs(tmp_path):
    canonical = tmp_path / "canonical.git"
    mirror = tmp_path / "mirror.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-q", str(canonical))
    git(tmp_path, "init", "--bare", "-q", str(mirror))
    git(tmp_path, "init", "-q", str(work))
    git(work, "config", "user.name", "mirror-health-test")
    git(work, "config", "user.email", "mirror-health@example.test")
    (work / "state.txt").write_text("initial\n", encoding="utf-8")
    git(work, "add", "state.txt")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "canonical", str(canonical))
    git(work, "remote", "add", "mirror", str(mirror))
    git(work, "push", "-q", "canonical", "HEAD:refs/heads/main")
    git(work, "push", "-q", "mirror", "HEAD:refs/heads/main")
    git(work, "tag", "-a", "v1.0.0", "-m", "release")
    git(work, "push", "-q", "canonical", "refs/tags/v1.0.0")
    git(work, "push", "-q", "mirror", "refs/tags/v1.0.0")
    (work / "state.txt").write_text("canonical update\n", encoding="utf-8")
    git(work, "add", "state.txt")
    git(work, "commit", "-q", "-m", "canonical update")
    git(work, "push", "-q", "canonical", "HEAD:refs/heads/main")
    git(work, "push", "-q", "mirror", "HEAD:refs/heads/mirror-only")

    report = check_mirror_health.run_check(
        str(canonical),
        str(mirror),
        now=NOW,
    )

    by_ref = {check["ref"]: check for check in report["checks"]}
    assert report["status"] == "unhealthy"
    assert by_ref["refs/heads/main"]["status"] == "stale"
    assert by_ref["refs/heads/mirror-only"]["status"] == "divergent"
    assert by_ref["refs/tags/v1.0.0"]["status"] == "match"
    assert report["summary"] == {
        "match": 1,
        "missing": 0,
        "stale": 1,
        "divergent": 1,
        "indeterminate": 0,
    }


def test_remote_ref_fetch_is_read_only():
    calls = []

    def execute(arguments, cwd, check):
        calls.append((arguments, cwd, check))
        if arguments[:2] == ["ls-remote", "--heads"]:
            return subprocess.CompletedProcess(arguments, 0, f"{CANONICAL_MAIN}\trefs/heads/main\n", "")
        if arguments[:2] == ["ls-remote", "--tags"]:
            return subprocess.CompletedProcess(arguments, 0, f"{CANONICAL_TAG}\trefs/tags/v1.0.0\n", "")
        raise AssertionError(arguments)

    result = check_mirror_health.fetch_remote_refs("https://example.test/brand-kit.git", execute=execute)

    assert result == refs(
        branches={"refs/heads/main": CANONICAL_MAIN},
        tags={"refs/tags/v1.0.0": CANONICAL_TAG},
    )
    assert [call[0][0] for call in calls] == ["ls-remote", "ls-remote"]
    assert all("push" not in call[0] for call in calls)
    assert all("commit" not in call[0] for call in calls)
    assert all(call[1] is None for call in calls)
    assert all(call[2] is True for call in calls)


def test_available_but_delayed_mirror_reports_missing_refs_without_ancestry_fetch():
    canonical_repository = "https://forgejo.example/brand-kit.git"
    mirror_repository = "https://github.example/brand-kit.git"
    calls = []

    def execute(arguments, cwd, check):
        calls.append((arguments, cwd, check))
        if arguments[:2] == ["ls-remote", "--heads"]:
            output = (
                f"{CANONICAL_MAIN}\trefs/heads/main\n"
                if arguments[2] == canonical_repository
                else ""
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:2] == ["ls-remote", "--tags"]:
            output = (
                f"{CANONICAL_TAG}\trefs/tags/v1.0.0\n"
                if arguments[2] == canonical_repository
                else ""
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:2] == ["init", "--bare"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    report = check_mirror_health.run_check(
        canonical_repository,
        mirror_repository,
        now=NOW,
        execute=execute,
    )

    assert report["status"] == "unhealthy"
    assert report["observed_at"] == "2026-09-27T17:00:00Z"
    assert report["summary"] == {
        "match": 0,
        "missing": 2,
        "stale": 0,
        "divergent": 0,
        "indeterminate": 0,
    }
    assert {(check["kind"], check["status"]) for check in report["checks"]} == {
        ("branch", "missing"),
        ("tag", "missing"),
    }
    assert not any(arguments[0] in {"fetch", "merge-base"} for arguments, _, _ in calls)


def test_main_records_remote_failure_as_indeterminate_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "mirror-health.json"

    def fail(*args, **kwargs):
        raise check_mirror_health.MirrorHealthError("Forgejo API unavailable")

    monkeypatch.setattr(check_mirror_health, "run_check", fail)

    exit_code = check_mirror_health.main(
        [
            "--canonical-repository",
            "https://forgejo.example/brand-kit.git",
            "--mirror-repository",
            "https://github.example/brand-kit.git",
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "indeterminate"
    assert report["summary"] == {
        "match": 0,
        "missing": 0,
        "stale": 0,
        "divergent": 0,
        "indeterminate": 1,
    }
    assert report["checks"] == []
    assert report["error"] == "Forgejo API unavailable"
    assert "INDETERMINATE" in capsys.readouterr().err


def test_main_persists_unhealthy_failure_report_and_returns_failure(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "mirror-health.json"
    expected = {
        "schema": check_mirror_health.REPORT_SCHEMA,
        "status": "unhealthy",
        "observed_at": "2026-09-27T17:00:00Z",
        "canonical_repository": "https://forgejo.example/brand-kit.git",
        "mirror_repository": "https://github.example/brand-kit.git",
        "summary": {
            "match": 1,
            "missing": 1,
            "stale": 1,
            "divergent": 1,
            "indeterminate": 0,
        },
        "checks": [],
    }
    monkeypatch.setattr(check_mirror_health, "run_check", lambda *args, **kwargs: expected)

    exit_code = check_mirror_health.main(
        [
            "--canonical-repository",
            expected["canonical_repository"],
            "--mirror-repository",
            expected["mirror_repository"],
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == expected
    assert "UNHEALTHY" in capsys.readouterr().err


def test_execute_git_redacts_provider_credentials_from_failure(monkeypatch):
    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            128,
            "",
            "fatal: repository 'https://user:secret@example.test/brand-kit.git' unavailable",
        )

    monkeypatch.setattr(check_mirror_health.subprocess, "run", failed_run)

    with pytest.raises(check_mirror_health.MirrorHealthError) as error:
        check_mirror_health.execute_git(
            ["ls-remote", "--heads", "https://user:secret@example.test/brand-kit.git"]
        )

    assert "secret" not in str(error.value)
    assert "https://[REDACTED]@example.test/brand-kit.git" in str(error.value)


def test_workflow_bootstraps_indeterminate_report_before_setup_failure():
    template = Path(
        "automation/brand-kit-mirror-health-workflowtemplate.yml"
    ).read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' \'{"schema":"brand-kit-mirror-health/v1","status":"indeterminate"' in template
    assert "printf '2\\n' > /tmp/brand-kit-mirror-health-exit-code" in template
    assert "default: \"2\"" in template
    assert 'exit "$CHECK_EXIT"' in template
    assert "artifactGC:\n                strategy: Never" in template
    assert 'when: "{{workflow.status}} != Succeeded"' in template


def test_workflow_contract_is_scheduled_read_only_and_alerts_owner():
    template_path = Path("automation/brand-kit-mirror-health-workflowtemplate.yml")
    cron_path = Path("automation/brand-kit-mirror-health-cronworkflow.yml")
    template = template_path.read_text(encoding="utf-8")
    cron = cron_path.read_text(encoding="utf-8")

    assert "kind: WorkflowTemplate" in template
    assert "name: brand-kit-mirror-health" in template
    assert "tools/check_mirror_health.py" in template
    assert "git.ardenone.com/jedarden/brand-kit.git" in template
    assert "github.com/jedarden/brand-kit.git" in template
    assert "artifactGC:\n                strategy: Never" in template
    assert "failures/brand-kit-mirror-health/v1/{{workflow.uid}}/report.json" in template
    assert "http://alertmanager.monitoring.svc:9093/api/v1/alerts" in template
    assert '"alertname": "BrandKitMirrorHealth"' in template
    assert '"owner": "jedarden"' in template
    assert 'when: "{{workflow.status}} != Succeeded"' in template
    assert "git push" not in template
    assert "git commit" not in template

    assert "kind: CronWorkflow" in cron
    assert '    - "17 */6 * * *"' in cron
    assert "name: brand-kit-mirror-health" in cron


def test_report_contains_all_failure_counts(tmp_path):
    report_path = tmp_path / "report.json"
    report = {
        "schema": check_mirror_health.REPORT_SCHEMA,
        "status": "unhealthy",
        "summary": {"match": 1, "missing": 1, "stale": 1, "divergent": 1, "indeterminate": 0},
        "checks": [],
    }
    check_mirror_health.write_report(report, report_path)

    assert json.loads(report_path.read_text(encoding="utf-8"))["summary"]["stale"] == 1
