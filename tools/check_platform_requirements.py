#!/usr/bin/env python3
"""Check freshness and coverage of external platform requirement metadata.

This check deliberately does not fetch external pages: CI should remain
deterministic and the pages can change without a reliable machine-readable
diff.  A stale record forces a human to open its source URL, compare the
platform's current upload guidance with the README and generated assets, and
then record a new verification date in ``tools/build_assets.py``.

Run: python3 tools/check_platform_requirements.py
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "platform-assets.json"
DEFAULT_MAX_AGE_DAYS = 180
REQUIRED_FIELDS = {"platform", "source_url", "last_verified"}


def _load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    return manifest


def check_platform_requirements(
    manifest: dict,
    *,
    as_of: date | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> list[str]:
    """Return issues, or an empty list when requirement metadata is current."""
    if as_of is None:
        as_of = date.today()
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")

    assets = manifest.get("assets")
    requirements = manifest.get("platform_requirements")
    if not isinstance(assets, list):
        return ["assets is not a list"]
    if not isinstance(requirements, list):
        return ["platform_requirements is not a list"]

    expected_platforms = {
        asset.get("platform")
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("platform"), str)
    }
    issues: list[str] = []
    seen: set[str] = set()
    for index, requirement in enumerate(requirements):
        label = f"platform requirement {index}"
        if not isinstance(requirement, dict):
            issues.append(f"{label} is not an object")
            continue
        if set(requirement) != REQUIRED_FIELDS:
            issues.append(f"{label} has fields {sorted(requirement)}")
            continue

        platform = requirement["platform"]
        source_url = requirement["source_url"]
        last_verified = requirement["last_verified"]
        if not isinstance(platform, str) or not platform:
            issues.append(f"{label} has an invalid platform")
            continue
        if platform in seen:
            issues.append(f"duplicate platform requirement: {platform}")
        seen.add(platform)
        if (
            not isinstance(source_url, str)
            or not source_url.startswith("https://")
            or not urlparse(source_url).netloc
        ):
            issues.append(f"{platform}: source_url must be an HTTPS URL")
        if not isinstance(last_verified, str):
            issues.append(f"{platform}: last_verified must be an ISO date")
            continue
        try:
            verified = date.fromisoformat(last_verified)
        except ValueError:
            issues.append(f"{platform}: last_verified is not an ISO date: {last_verified!r}")
            continue
        if verified > as_of:
            issues.append(f"{platform}: last_verified {last_verified} is in the future")
            continue
        age = (as_of - verified).days
        if age > max_age_days:
            issues.append(
                f"{platform}: last_verified {last_verified} is {age} days old "
                f"(maximum {max_age_days})"
            )

    missing = sorted(expected_platforms - seen)
    unexpected = sorted(seen - expected_platforms)
    if missing:
        issues.append("missing platform requirements: " + ", ".join(missing))
    if unexpected:
        issues.append("requirements have no assets: " + ", ".join(unexpected))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        metavar="YYYY-MM-DD",
        help="date used for deterministic checks (default: today)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"maximum metadata age (default: {DEFAULT_MAX_AGE_DAYS})",
    )
    args = parser.parse_args(argv)

    try:
        manifest = _load_manifest(MANIFEST_PATH)
        issues = check_platform_requirements(
            manifest, as_of=args.as_of, max_age_days=args.max_age_days
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL platform requirement metadata: {error}")
        return 1

    if issues:
        print("FAIL platform requirement metadata:")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    count = len(manifest["platform_requirements"])
    print(
        f"PASS platform requirement metadata: {count} platforms verified "
        f"within {args.max_age_days} days as of {args.as_of}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
