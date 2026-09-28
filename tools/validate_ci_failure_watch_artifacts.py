#!/usr/bin/env python3
"""Validate both CI failure-watch files immediately before Argo uploads them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import ci_failure_watch_report


def _load(path: Path, validator: Callable[[Any], Any], label: str) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ci_failure_watch_report.ReportContractError(
            f"cannot read {label} JSON artifact"
        ) from error
    validator(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--attestations", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _load(args.report, ci_failure_watch_report.validate_report, "failure-watch report")
        _load(args.attestations, ci_failure_watch_report.validate_attestations, "CI attestations")
    except ci_failure_watch_report.ReportContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("PASS  CI failure-watch artifacts satisfy their v1 contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
