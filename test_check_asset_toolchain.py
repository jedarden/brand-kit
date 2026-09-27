from pathlib import Path

import pytest

from tools import check_asset_toolchain

ROOT = Path(__file__).resolve().parent
DOCUMENT = ROOT / "docs/notes/asset-toolchain.md"

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


def write_workflow(tmp_path: Path, contents: str = WORKFLOW_TEMPLATE) -> Path:
    workflow = tmp_path / "brand-kit-ci-workflowtemplate.yml"
    workflow.write_text(contents, encoding="utf-8")
    return workflow


def check_document_against(tmp_path: Path, contents: str = WORKFLOW_TEMPLATE) -> None:
    check_asset_toolchain.check_toolchain(DOCUMENT, write_workflow(tmp_path, contents))


def test_accepts_matching_pin_table_and_workflow_installs(tmp_path):
    check_document_against(tmp_path)


def test_rejects_workflow_version_drift(tmp_path):
    workflow = WORKFLOW_TEMPLATE.replace("resvg@0.47.0", "resvg@0.48.0")

    with pytest.raises(
        check_asset_toolchain.ToolchainParityError,
        match="resvg: documented version 0.47.0 does not match WorkflowTemplate version 0.48.0",
    ):
        check_document_against(tmp_path, workflow)


def test_rejects_workflow_installer_drift(tmp_path):
    workflow = WORKFLOW_TEMPLATE.replace(".venv/bin/python -m pip", "pip3")

    with pytest.raises(
        check_asset_toolchain.ToolchainParityError,
        match="Pillow: documented installer '.*' does not match WorkflowTemplate installer 'pip3'",
    ):
        check_document_against(tmp_path, workflow)


def test_rejects_missing_pillow_wheel_only_option(tmp_path):
    workflow = WORKFLOW_TEMPLATE.replace("--only-binary=:all: Pillow", "Pillow")

    with pytest.raises(
        check_asset_toolchain.ToolchainParityError,
        match="Pillow: documented install options",
    ):
        check_document_against(tmp_path, workflow)


def test_rejects_tool_present_only_in_pin_table(tmp_path):
    document = tmp_path / "asset-toolchain.md"
    original = DOCUMENT.read_text(encoding="utf-8")
    document.write_text(
        original.replace(
            "| pytest | `9.0.2` | `.venv/bin/python -m pip install --only-binary=:all: pytest==9.0.2` | Runs the regression suite; it does not affect generated asset bytes. |",
            "| extra-tool | `1.2.3` | `cargo install extra-tool@1.2.3` | test-only |\n"
            "| pytest | `9.0.2` | `.venv/bin/python -m pip install --only-binary=:all: pytest==9.0.2` | Runs the regression suite; it does not affect generated asset bytes. |",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        check_asset_toolchain.ToolchainParityError,
        match="tool set mismatch; missing from WorkflowTemplate: cargo/extra-tool",
    ):
        check_asset_toolchain.check_toolchain(document, write_workflow(tmp_path))


def test_rejects_tool_present_only_in_workflow(tmp_path):
    workflow = WORKFLOW_TEMPLATE.replace(
        "cargo install resvg@0.47.0 vtracer@0.6.5",
        "cargo install resvg@0.47.0 vtracer@0.6.5 extra-tool@1.2.3",
    )

    with pytest.raises(
        check_asset_toolchain.ToolchainParityError,
        match="tool set mismatch; not named by pin table: cargo/extra-tool",
    ):
        check_document_against(tmp_path, workflow)
