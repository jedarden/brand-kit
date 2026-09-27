from pathlib import Path

import pytest

from tools import check_asset_toolchain

ROOT = Path(__file__).resolve().parent

WORKFLOW_TEMPLATE = """\
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: brand-kit-ci
spec:
  templates:
    - name: ci
      container:
        args:
          - |
            set -ex
            cargo install resvg@0.47.0 vtracer@0.6.5
            .venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2
"""


def test_regeneration_commands_use_pinned_venv():
    readme = (ROOT / "README.md").read_text()
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    tools = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "tools/build_assets.py",
            "tools/trace_logo.py",
            "tools/verify_assets.py",
        )
    )
    local_readme = readme.split("## CI regression gate\n", 1)[0]
    documented_commands = local_readme + toolchain + tools

    assert ".venv/bin/python tools/trace_logo.py" in readme
    assert ".venv/bin/python tools/build_assets.py" in readme
    assert "python3 tools/trace_logo.py" not in documented_commands
    assert "python3 tools/build_assets.py" not in documented_commands
    assert (
        ".venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1"
        in readme
    )
    assert (
        ".venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1"
        in toolchain
    )
    assert "docs/notes/asset-toolchain.md" in readme


def test_ci_regression_acceptance_sequence_is_documented():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## CI regression gate\n", 1)[1].split("\n## ", 1)[0]
    commands = (
        "python3 tools/check_asset_toolchain.py",
        "cargo install resvg@0.47.0 vtracer@0.6.5",
        ".venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2",
        ".venv/bin/python tools/verify_assets.py",
        ".venv/bin/python -m pytest -q",
        ".venv/bin/python tools/build_assets.py",
        "git diff --exit-code --quiet",
    )

    positions = [section.index(command) for command in commands]
    assert positions == sorted(positions)
    assert "unexpected generated files" in section
    assert "Argo phase of `Succeeded`" in section
    assert "not CI acceptance evidence" in section


def test_pin_table_matches_workflow_template(tmp_path):
    workflow = tmp_path / "brand-kit-ci-workflowtemplate.yml"
    workflow.write_text(WORKFLOW_TEMPLATE)

    check_asset_toolchain.check_toolchain(
        ROOT / "docs/notes/asset-toolchain.md", workflow
    )


@pytest.mark.parametrize(
    ("replacement", "expected"),
    (
        ("resvg@0.47.0", "resvg@0.48.0"),
        ("--only-binary=:all: ", ""),
        (".venv/bin/python -m pip", "pip3"),
        ("vtracer@0.6.5", "unexpected@0.6.5"),
    ),
)
def test_pin_checker_rejects_workflow_drift(tmp_path, replacement, expected):
    workflow = tmp_path / "brand-kit-ci-workflowtemplate.yml"
    workflow.write_text(WORKFLOW_TEMPLATE.replace(replacement, expected))

    with pytest.raises(check_asset_toolchain.ToolchainParityError):
        check_asset_toolchain.check_toolchain(
            ROOT / "docs/notes/asset-toolchain.md", workflow
        )


def test_pin_checker_rejects_tool_added_only_to_pin_table(tmp_path):
    workflow = tmp_path / "brand-kit-ci-workflowtemplate.yml"
    workflow.write_text(WORKFLOW_TEMPLATE)
    document = tmp_path / "asset-toolchain.md"
    original = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    document.write_text(
        original.replace(
            "\n\nPython and Rust",
            "\n| extra-tool | `1.2.3` | `cargo install extra-tool@1.2.3` | test-only |\n\nPython and Rust",
        )
    )

    with pytest.raises(check_asset_toolchain.ToolchainParityError, match="mismatch"):
        check_asset_toolchain.check_toolchain(document, workflow)


def test_trace_logo_uses_pinned_cargo_vtracer_cli():
    toolchain = (ROOT / "docs/notes/asset-toolchain.md").read_text()
    trace_logo = (ROOT / "tools/trace_logo.py").read_text()

    assert "cargo install vtracer@0.6.5" in toolchain
    assert "cargo install vtracer@0.6.5" in trace_logo
    assert "PyPI `vtracer`" in toolchain
    assert "PyPI `vtracer`" in trace_logo


def test_logo_source_and_regeneration_contract_is_consistent():
    documents = [
        (ROOT / path).read_text()
        for path in (
            "README.md",
            "docs/plan/plan.md",
            "docs/notes/asset-toolchain.md",
        )
    ]
    trace = """```bash
.venv/bin/python tools/trace_logo.py
```"""
    build_verify = """```bash
.venv/bin/python tools/build_assets.py
.venv/bin/python tools/verify_assets.py
```"""

    for document in documents:
        normalized = "\n".join(line.strip() for line in document.splitlines())
        assert (
            "`source/logo.svg` is authoritative" in document
            or "`source/logo.svg` is the authoritative" in document
        )
        assert "`source/logo.svg.sha256`" in document
        assert ".venv/bin/python tools/trace_logo.py --force" in document
        assert trace in normalized
        assert build_verify in normalized
