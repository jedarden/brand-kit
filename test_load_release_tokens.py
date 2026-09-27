from pathlib import Path
import os
import subprocess

import pytest


HELPER = Path(__file__).parent / "tools" / "load_release_tokens.sh"
DUMMY_TOKEN = "dummy-release-token-for-shell-test"


def fake_bao_as(tmp_path):
    command = tmp_path / "bao-as"
    command.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\\n' "$@" > "$BAO_ARGV_LOG"
case "$BAO_TEST_MODE" in
    success)
        printf '%s\\n' "$BAO_TEST_VALUE"
        ;;
    empty)
        printf '\\n'
        ;;
    missing)
        exit 17
        ;;
    *)
        exit 18
        ;;
esac
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command


def run_loader(tmp_path, mode):
    fake_bao_as(tmp_path)
    argv_log = tmp_path / "argv.log"
    environment = os.environ.copy()
    environment.update(
        {
            "BAO_ARGV_LOG": str(argv_log),
            "BAO_TEST_MODE": mode,
            "BAO_TEST_VALUE": DUMMY_TOKEN,
            "PATH": f"{tmp_path}:{environment['PATH']}",
        }
    )
    script = f"""
set -u
source "$1"
case "$BAO_TEST_MODE" in
    success)
        load_release_token FORGEJO_TOKEN secret/rs-manager/test
        [[ ${{FORGEJO_TOKEN-}} == "$BAO_TEST_VALUE" ]]
        ;;
    missing|empty)
        if load_release_token FORGEJO_TOKEN secret/rs-manager/test; then
            exit 19
        fi
        [[ -z ${{FORGEJO_TOKEN+x}} ]]
        ;;
esac
"""
    result = subprocess.run(
        ["bash", "-c", script, "release-token-test", str(HELPER)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, argv_log


@pytest.mark.parametrize("mode", ("success", "missing", "empty"))
def test_loader_never_emits_or_passes_the_token(tmp_path, mode):
    result, argv_log = run_loader(tmp_path, mode)

    assert result.returncode == 0
    assert result.stdout == ""
    assert DUMMY_TOKEN not in result.stdout
    assert DUMMY_TOKEN not in result.stderr
    assert DUMMY_TOKEN not in argv_log.read_text(encoding="utf-8")


def test_successful_lookup_exports_the_captured_value_without_stdout(tmp_path):
    result, _ = run_loader(tmp_path, "success")

    assert result.returncode == 0
    assert result.stdout == ""


def test_exit_trap_clears_all_release_token_variables(tmp_path):
    report = tmp_path / "cleanup-report"
    environment = os.environ.copy()
    environment["TRAP_REPORT"] = str(report)
    script = f"""
set -u
source "$1"
eval "$(declare -f release_token_cleanup | sed 's/^release_token_cleanup /original_release_token_cleanup /')"
release_token_cleanup() {{
    original_release_token_cleanup
    if [[ -z ${{FORGEJO_TOKEN+x}} && -z ${{ARGO_TOKEN+x}} && -z ${{ARGO_SUBMIT_TOKEN+x}} ]]; then
        printf '%s\\n' cleared > "$TRAP_REPORT"
    else
        printf '%s\\n' leaked > "$TRAP_REPORT"
    fi
}}
export FORGEJO_TOKEN=one ARGO_TOKEN=two ARGO_SUBMIT_TOKEN=three
exit 0
"""

    result = subprocess.run(
        ["bash", "-c", script, "release-token-trap-test", str(HELPER)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert report.read_text(encoding="utf-8") == "cleared\n"
