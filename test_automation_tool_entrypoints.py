"""Every tool a WorkflowTemplate runs must start as a script from any cwd.

The templates invoke ``python3 /brand-kit/tools/<tool>.py`` from outside the
checkout, where ``from tools import ...`` only resolves if the script puts the
repository root on ``sys.path`` itself. A missing insert is invisible to the
unit tests (pytest runs from the repository root) and failed the first
unpaused brand-kit-ci-failure-watch run on 2026-10-11.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
INVOKED = sorted(
    {
        match
        for manifest in (ROOT / "automation").glob("*.yml")
        for match in re.findall(r"tools/[a-z_]+\.py", manifest.read_text(encoding="utf-8"))
    }
)


def test_automation_invokes_tools():
    assert "tools/prune_ci_failure_watch_reports.py" in INVOKED
    assert "tools/brand_kit_workflow_liveness.py" in INVOKED


@pytest.mark.parametrize("tool", INVOKED)
def test_tool_runs_as_a_script_outside_the_checkout(tool, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / tool), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
