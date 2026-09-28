"""Contract tests for the immutable brand-kit-ci source checkout."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from tools import release_publish


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "tests" / "fixtures" / "brand-kit-ci-workflowtemplate.yml"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _workflow() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _parameters(resource: dict) -> dict[str, str]:
    return {
        parameter["name"]: parameter["value"]
        for parameter in resource["spec"]["arguments"]["parameters"]
    }


def _checkout_source(workflow: dict) -> str:
    return workflow["spec"]["templates"][0]["container"]["args"][0]


def _new_bare_repository(root: Path) -> tuple[Path, Path]:
    origin = root / "brand-kit.git"
    seed = root / "brand-kit-seed"
    _git(root, "init", "--bare", "--initial-branch=main", str(origin))
    _git(root, "init", "--initial-branch=main", str(seed))
    _git(seed, "config", "user.name", "brand-kit contract test")
    _git(seed, "config", "user.email", "brand-kit-contract@example.invalid")
    _git(seed, "remote", "add", "origin", str(origin))
    return origin, seed


def _divergent_revisions(root: Path) -> tuple[Path, str, str]:
    origin, seed = _new_bare_repository(root)

    marker = seed / "revision.txt"
    marker.write_text("base\n", encoding="utf-8")
    _git(seed, "add", "revision.txt")
    _git(seed, "commit", "-m", "base")

    _git(seed, "switch", "-c", "requested")
    marker.write_text("requested revision\n", encoding="utf-8")
    _git(seed, "add", "revision.txt")
    _git(seed, "commit", "-m", "requested revision")
    requested = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "requested")

    _git(seed, "switch", "main")
    marker.write_text("main revision\n", encoding="utf-8")
    _git(seed, "add", "revision.txt")
    _git(seed, "commit", "-m", "main advanced")
    main = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "--set-upstream", "origin", "main")

    assert main != requested
    return origin, requested, main


def _render_checkout_source(
    source: str, *, repo: Path, branch: str, revision: str
) -> str:
    return (
        source.replace("{{inputs.parameters.repo}}", str(repo))
        .replace("{{inputs.parameters.branch}}", branch)
        .replace("{{inputs.parameters.revision}}", revision)
    )


def _run_checkout(
    source: str,
    root: Path,
    *,
    repo: Path,
    branch: str,
    revision: str,
    checkout: Path,
    commit_output: Path,
) -> subprocess.CompletedProcess[str]:
    rendered = _render_checkout_source(
        source, repo=repo, branch=branch, revision=revision
    )
    environment = os.environ.copy()
    environment.update(
        {
            "WORKSPACE": str(checkout),
            "COMMIT_OUTPUT": str(commit_output),
        }
    )
    return subprocess.run(
        ["bash", "-c", rendered],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _attestation(run: str, commit: str) -> dict:
    return {
        "metadata": {
            "name": run,
            "labels": {
                "workflows.argoproj.io/workflow-template": "brand-kit-ci",
            },
        },
        "status": {
            "phase": "Succeeded",
            "outputs": {
                "parameters": [{"name": "commit", "value": commit}],
            },
        },
    }


def test_workflow_requires_revision_and_attests_a_detached_full_sha():
    workflow = _workflow()
    assert workflow["kind"] == "WorkflowTemplate"
    assert _parameters(workflow) == {
        "repo": "jedarden/brand-kit",
        "branch": "main",
        "revision": "",
    }

    template = workflow["spec"]["templates"][0]
    input_parameters = {
        parameter["name"]: parameter["value"]
        for parameter in template["inputs"]["parameters"]
    }
    assert input_parameters == {
        "repo": "{{workflow.parameters.repo}}",
        "branch": "{{workflow.parameters.branch}}",
        "revision": "{{workflow.parameters.revision}}",
    }

    source = _checkout_source(workflow)
    assert "^[0-9a-fA-F]{40}$" in source
    assert 'git -C "$WORKSPACE" fetch --no-tags --depth=1 origin "$REVISION"' in source
    assert 'git -C "$WORKSPACE" checkout --detach --quiet "$REVISION"' in source
    assert 'git -C "$WORKSPACE" symbolic-ref --quiet HEAD' in source
    assert template["outputs"] == {
        "parameters": [
            {
                "name": "commit",
                "valueFrom": {"path": "/tmp/commit"},
            }
        ]
    }


@pytest.mark.parametrize("revision", ["main", "HEAD", "a" * 39, "a" * 41])
def test_checkout_rejects_branch_names_and_non_full_revisions(tmp_path, revision):
    workflow = _workflow()
    origin, _, _ = _divergent_revisions(tmp_path)
    result = _run_checkout(
        _checkout_source(workflow),
        tmp_path,
        repo=origin,
        branch="main",
        revision=revision,
        checkout=tmp_path / "checkout",
        commit_output=tmp_path / "commit",
    )

    assert result.returncode != 0
    assert not (tmp_path / "checkout").exists()


def test_checkout_uses_requested_revision_when_main_differs_and_attests_it(
    tmp_path, monkeypatch
):
    workflow = _workflow()
    origin, requested, main = _divergent_revisions(tmp_path)
    checkout = tmp_path / "checkout"
    commit_output = tmp_path / "commit"

    result = _run_checkout(
        _checkout_source(workflow),
        tmp_path,
        repo=origin,
        branch="main",
        revision=requested,
        checkout=checkout,
        commit_output=commit_output,
    )

    assert result.returncode == 0, result.stderr
    assert main != requested
    assert _git(checkout, "rev-parse", "--verify", "HEAD^{commit}") == requested
    assert _git(checkout, "show", "HEAD:revision.txt") == "requested revision"
    assert commit_output.read_text(encoding="utf-8").strip() == requested
    assert subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "--quiet", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).returncode != 0

    attestation = _attestation("brand-kit-ci-contract", commit_output.read_text().strip())
    monkeypatch.setattr(release_publish, "get_argo_workflow", lambda *args, **kwargs: attestation)
    observed = release_publish.attest_argo_ci_run(
        "brand-kit-ci-contract",
        requested,
    )
    assert observed["status"]["outputs"]["parameters"] == [
        {"name": "commit", "value": requested}
    ]
