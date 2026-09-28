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
printf '%s\\n' "$@" >> "$BAO_ARGV_LOG"
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


def release_test_environment():
    environment = os.environ.copy()
    for name in ("FORGEJO_TOKEN", "ARGO_TOKEN", "ARGO_SUBMIT_TOKEN"):
        environment.pop(name, None)
    return environment


def run_loader(tmp_path, mode):
    fake_bao_as(tmp_path)
    argv_log = tmp_path / "argv.log"
    environment = release_test_environment()
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
    environment = release_test_environment()
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


def run_cleanup_scenario(tmp_path, body):
    fake_bao_as(tmp_path)
    report = tmp_path / "cleanup-report"
    signal_report = tmp_path / "signal-report"
    environment = release_test_environment()
    environment.update(
        {
            "BAO_ARGV_LOG": str(tmp_path / "argv.log"),
            "BAO_TEST_MODE": "success",
            "BAO_TEST_VALUE": DUMMY_TOKEN,
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "TRAP_REPORT": str(report),
            "SIGNAL_REPORT": str(signal_report),
        }
    )
    script = f"""
set -u
source "$1"
eval "$(declare -f release_token_cleanup | sed 's/^release_token_cleanup /original_release_token_cleanup /')"
release_token_cleanup() {{
    original_release_token_cleanup
    if [[ -z ${{FORGEJO_TOKEN+x}} && -z ${{ARGO_TOKEN+x}} && -z ${{ARGO_SUBMIT_TOKEN+x}} ]]; then
        printf '%s\\n' cleared >> "$TRAP_REPORT"
    else
        printf '%s\\n' leaked >> "$TRAP_REPORT"
    fi
}}
load_release_token FORGEJO_TOKEN secret/rs-manager/forgejo
load_release_token ARGO_TOKEN secret/rs-manager/argo-read
load_release_token ARGO_SUBMIT_TOKEN secret/rs-manager/argo-submit
[[ $FORGEJO_TOKEN == "$BAO_TEST_VALUE" ]]
[[ $ARGO_TOKEN == "$BAO_TEST_VALUE" ]]
[[ $ARGO_SUBMIT_TOKEN == "$BAO_TEST_VALUE" ]]
{body}
"""
    result = subprocess.run(
        ["bash", "-c", script, "release-token-cleanup-test", str(HELPER)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, report, signal_report, tmp_path / "argv.log"


def test_exit_trap_clears_every_loaded_credential_on_normal_exit(tmp_path):
    result, report, _, argv_log = run_cleanup_scenario(tmp_path, "exit 0")

    assert result.returncode == 0
    assert result.stdout == ""
    assert DUMMY_TOKEN not in result.stderr
    assert report.read_text(encoding="utf-8") == "cleared\n"
    logged_arguments = argv_log.read_text(encoding="utf-8").splitlines()
    assert logged_arguments == [
        "rs-manager",
        "bao",
        "kv",
        "get",
        "-field=token",
        "secret/rs-manager/forgejo",
        "rs-manager",
        "bao",
        "kv",
        "get",
        "-field=token",
        "secret/rs-manager/argo-read",
        "rs-manager",
        "bao",
        "kv",
        "get",
        "-field=token",
        "secret/rs-manager/argo-submit",
    ]
    assert DUMMY_TOKEN not in logged_arguments


def test_exit_trap_clears_every_loaded_credential_after_an_error(tmp_path):
    result, report, _, argv_log = run_cleanup_scenario(
        tmp_path,
        "set -e\nfalse",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert DUMMY_TOKEN not in result.stderr
    assert report.read_text(encoding="utf-8") == "cleared\n"
    assert DUMMY_TOKEN not in argv_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("signal", ("HUP", "INT", "TERM"))
def test_signal_trap_clears_every_loaded_credential(tmp_path, signal):
    result, report, signal_report, argv_log = run_cleanup_scenario(
        tmp_path,
        f"""
kill -{signal} "$BASHPID"
if [[ -z ${{FORGEJO_TOKEN+x}} && -z ${{ARGO_TOKEN+x}} && -z ${{ARGO_SUBMIT_TOKEN+x}} ]]; then
    printf '%s\\n' cleared > "$SIGNAL_REPORT"
else
    printf '%s\\n' leaked > "$SIGNAL_REPORT"
fi
exit 0
""",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert DUMMY_TOKEN not in result.stderr
    assert signal_report.read_text(encoding="utf-8") == "cleared\n"
    cleanup_calls = report.read_text(encoding="utf-8").splitlines()
    assert len(cleanup_calls) >= 2
    assert all(line == "cleared" for line in cleanup_calls)
    assert DUMMY_TOKEN not in argv_log.read_text(encoding="utf-8")


def test_helper_rejects_legacy_or_arbitrary_credential_variable_names(tmp_path):
    fake_bao_as(tmp_path)
    environment = release_test_environment()
    environment.update(
        {
            "BAO_ARGV_LOG": str(tmp_path / "argv.log"),
            "BAO_TEST_MODE": "success",
            "BAO_TEST_VALUE": DUMMY_TOKEN,
            "PATH": f"{tmp_path}:{environment['PATH']}",
        }
    )
    script = """
source "$1"
if load_release_token FORGEJO_API_TOKEN secret/rs-manager/forgejo; then
    exit 19
fi
if load_release_token CUSTOM_TOKEN secret/rs-manager/custom; then
    exit 20
fi
"""

    result = subprocess.run(
        ["bash", "-c", script, "release-token-alias-test", str(HELPER)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert DUMMY_TOKEN not in result.stdout
    assert DUMMY_TOKEN not in result.stderr
    assert not (tmp_path / "argv.log").exists()


def test_helper_rejects_cli_token_invocation_without_echoing_arguments():
    result = subprocess.run(
        ["bash", str(HELPER), "--token", DUMMY_TOKEN],
        env=release_test_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "source tools/load_release_tokens.sh" in result.stderr
    assert DUMMY_TOKEN not in result.stdout + result.stderr
