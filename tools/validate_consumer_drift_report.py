#!/usr/bin/env python3
"""Validate a versioned consumer-drift JSON report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.consumer_drift_report import ReportError, validate_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.report.read_text(encoding="utf-8"))
        report = validate_report(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReportError) as error:
        print(f"INVALID consumer-drift report {args.report}: {error}", file=sys.stderr)
        return 1
    print(f"PASS  validated {report['schema']} report ({report['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
