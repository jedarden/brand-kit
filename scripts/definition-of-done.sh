#!/usr/bin/env bash
# Repository definition of done for brand-kit.
#
# Usage:
#   scripts/definition-of-done.sh --fast   # the credential/release contract lane
#   scripts/definition-of-done.sh          # the full root pytest suite
#
# brand-kit is a pure-Python repository: its executable surface is tools/*.py,
# checked by root-level test_*_*.py contract suites, so the definition of done
# is pytest. Both lanes are hermetic — mocked HTTP endpoints, tmp_path
# fixtures, no network, no browser, and no .git dependency, so the script is
# safe to run inside a `git archive` extraction of committed work.
#
# --fast runs the credential and release contract suites: the suites a change
# to the Argo credential separation, the release-token registry, or either
# submitter's request surface must keep green (about 250 tests, ~15 s on
# codinghome). Full mode runs the entire root suite (~840 tests, ~1 minute).
#
# Byte-level asset parity is deliberately NOT a lane here: the pinned
# resvg/vtracer/Pillow regen-diff (README "CI acceptance", step 5) needs
# cargo, crates.io, and the exact wheel-flavor venv from
# docs/notes/asset-toolchain.md, and stays the brand-kit-ci Argo workflow's
# job. This gate is the functional suite; the Argo regen-diff is the
# byte-exact one. CI acceptance therefore still requires an Argo phase of
# `Succeeded`, not just a green local run.
#
# Interpreter selection mirrors the README's pinned-toolchain contract:
# BRAND_KIT_PYTHON wins if set, then the repo `.venv` (the documented
# Pillow==12.1.1 wheel flavor), then system python3 with a WARN — system
# flavors have historically produced different asset bytes, so a system
# pass is functional evidence, never regeneration evidence. The pytest
# entry point is `python3 -m pytest` rather than the `pytest` shim, whose
# shebang is stale on NixOS hosts.
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fast=0
case "${1:-}" in
  "") ;;
  --fast) fast=1 ;;
  *)
    echo "usage: $0 [--fast]" >&2
    exit 2
    ;;
esac

fail=0
total_ms=0

run() {
  echo "+ $*"
  local started elapsed
  started=$(date +%s%N)
  if ! "$@"; then
    echo "FAILED: $*" >&2
    fail=1
  fi
  elapsed=$(( ($(date +%s%N) - started) / 1000000 ))
  total_ms=$((total_ms + elapsed))
  printf '[%d.%01ds] %s\n' $((elapsed / 1000)) $(((elapsed % 1000) / 100)) "$*"
}

if [ -n "${BRAND_KIT_PYTHON:-}" ]; then
  PY="${BRAND_KIT_PYTHON}"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
  echo "WARN: no .venv and BRAND_KIT_PYTHON unset — using system python3." >&2
  echo "WARN: asset byte parity is only evidence under the pinned wheel flavor" >&2
  echo "WARN: (docs/notes/asset-toolchain.md); the brand-kit-ci Argo regen-diff" >&2
  echo "WARN: still owns byte-exact acceptance." >&2
fi

echo "== brand-kit definition of done ($([ "$fast" = 1 ] && echo fast || echo full)) with $PY =="

if [ "$fast" = 1 ]; then
  # The credential/release contract cluster. Keep this list to the suites a
  # credential-contract change must keep green; full mode covers everything.
  contract_suites=(
    test_argo_credential_separation.py
    test_brand_kit_ci_submit.py
    test_brand_kit_ci_workflowtemplate.py
    test_release_publish.py
    test_release_token_probe.py
    test_consumer_drift_submit.py
    test_load_release_tokens.py
    test_documentation_smoke.py
  )
  for suite in "${contract_suites[@]}"; do
    if [ ! -f "$suite" ]; then
      echo "FAILED: contract suite $suite is missing" >&2
      fail=1
    fi
  done
  run "$PY" -m pytest "${contract_suites[@]}" -q
else
  # The whole root suite — the same leg the brand-kit-ci Argo workflow runs
  # (README "CI acceptance", step 4).
  run "$PY" -m pytest -q
fi

printf '== definition of done (%s): %d.%01ds total ==\n' \
  "$([ "$fast" = 1 ] && echo fast || echo full)" \
  $((total_ms / 1000)) $(((total_ms % 1000) / 100))

exit "$fail"
