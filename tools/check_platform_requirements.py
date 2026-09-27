#!/usr/bin/env python3
"""Check freshness, coverage, and optional reachability of platform sources.

The default check is deterministic and local.  ``--check-reachability`` adds
an explicit network check for the HTTPS sources in the manifest.  HTTP
responses are conclusive; DNS, TLS, timeout, and connection errors are
reported as indeterminate and exit with status 2 rather than looking like a
passing check.

Run: python3 tools/check_platform_requirements.py
Reachability: python3 tools/check_platform_requirements.py --check-reachability
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "platform-assets.json"
DEFAULT_MAX_AGE_DAYS = 180
DEFAULT_TIMEOUT_SECONDS = 15.0
REQUIRED_FIELDS = {"platform", "source_url", "last_verified"}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REACHABILITY_PASS = "pass"
REACHABILITY_HTTP_FAILURE = "http_failure"
REACHABILITY_NETWORK_FAILURE = "network_failure"
REPORT_SCHEMA = "brand-kit-platform-requirements/v1"


@dataclass(frozen=True)
class SourceCheck:
    """The conclusive or indeterminate result for one valid source URL."""

    platform: str
    status: str
    detail: str


def _load_manifest(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    return manifest


def _is_valid_source_url(value: object) -> bool:
    """Return whether *value* is a safe, absolute HTTPS source URL."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlparse(value)
        # Accessing ``port`` also validates malformed port numbers and brackets.
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def _is_valid_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _safe_network_detail(error: BaseException) -> str:
    """Keep credentials out of network diagnostics if a bad fixture slips in."""
    return re.sub(
        r"(https?://)(?:[^/\s:@]+(?::[^/\s@]*)?@)",
        r"\1[REDACTED]@",
        str(error),
    )[:300]


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
        if not isinstance(platform, str) or not platform or platform != platform.strip():
            issues.append(f"{label} has an invalid platform")
            continue
        if platform in seen:
            issues.append(f"duplicate platform requirement: {platform}")
        seen.add(platform)
        if not _is_valid_source_url(source_url):
            issues.append(f"{platform}: source_url must be an HTTPS URL")
        if not isinstance(last_verified, str):
            issues.append(f"{platform}: last_verified must be an ISO date")
            continue
        if not _is_valid_iso_date(last_verified):
            issues.append(f"{platform}: last_verified is not an ISO date: {last_verified!r}")
            continue
        verified = date.fromisoformat(last_verified)
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


def check_platform_requirement_sources(
    manifest: dict,
    *,
    opener=None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[SourceCheck]:
    """Check valid HTTPS sources without making malformed data look healthy.

    A 2xx or 3xx HTTP response is a pass, any other HTTP response is a
    conclusive failure, and transport errors are indeterminate.  The opener
    is injectable so tests never need to contact the public Internet.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    requirements = manifest.get("platform_requirements")
    if not isinstance(requirements, list):
        return []
    if opener is None:
        opener = urllib.request.urlopen

    checks: list[SourceCheck] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        platform = requirement.get("platform")
        source_url = requirement.get("source_url")
        if not isinstance(platform, str) or not platform or not _is_valid_source_url(source_url):
            continue

        request = urllib.request.Request(
            source_url,
            headers={"User-Agent": "brand-kit-platform-requirements/1"},
        )
        response = None
        try:
            response = opener(request, timeout=timeout_seconds)
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else getattr(response, "status", None)
            if not isinstance(status, int):
                checks.append(
                    SourceCheck(
                        platform,
                        REACHABILITY_NETWORK_FAILURE,
                        "network error: response had no HTTP status",
                    )
                )
            elif 200 <= status < 400:
                checks.append(SourceCheck(platform, REACHABILITY_PASS, f"HTTP {status}"))
            else:
                checks.append(
                    SourceCheck(platform, REACHABILITY_HTTP_FAILURE, f"HTTP {status}")
                )
        except urllib.error.HTTPError as error:
            checks.append(
                SourceCheck(platform, REACHABILITY_HTTP_FAILURE, f"HTTP {error.code}")
            )
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            checks.append(
                SourceCheck(
                    platform,
                    REACHABILITY_NETWORK_FAILURE,
                    f"network error: {_safe_network_detail(error)}",
                )
            )
        except Exception as error:
            # A custom opener, proxy, or TLS implementation can surface a
            # transport error outside urllib's narrow exception hierarchy.
            checks.append(
                SourceCheck(
                    platform,
                    REACHABILITY_NETWORK_FAILURE,
                    f"network error: {_safe_network_detail(error)}",
                )
            )
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
    return checks


def _report_payload(
    metadata_issues: list[str],
    source_checks: list[SourceCheck],
    status: str,
) -> dict:
    summary = {
        "metadata_failures": len(metadata_issues),
        REACHABILITY_PASS: sum(
            check.status == REACHABILITY_PASS for check in source_checks
        ),
        REACHABILITY_HTTP_FAILURE: sum(
            check.status == REACHABILITY_HTTP_FAILURE for check in source_checks
        ),
        REACHABILITY_NETWORK_FAILURE: sum(
            check.status == REACHABILITY_NETWORK_FAILURE for check in source_checks
        ),
    }
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "summary": summary,
        "metadata_issues": metadata_issues,
        "source_checks": [asdict(check) for check in source_checks],
    }


def _write_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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
    parser.add_argument(
        "--check-reachability",
        "--reachability",
        action="store_true",
        help="make HTTPS requests and fail on HTTP errors or indeterminate network failures",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-source network timeout (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write a sanitized JSON result report to this path",
    )
    args = parser.parse_args(argv)

    source_checks: list[SourceCheck] = []
    try:
        manifest = _load_manifest(MANIFEST_PATH)
        issues = check_platform_requirements(
            manifest, as_of=args.as_of, max_age_days=args.max_age_days
        )
        if args.check_reachability:
            source_checks = check_platform_requirement_sources(
                manifest, timeout_seconds=args.timeout_seconds
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL platform requirement metadata: {error}")
        if args.report:
            _write_report(
                args.report,
                _report_payload([str(error)], source_checks, "fail"),
            )
        return 1

    if issues:
        print("FAIL platform requirement metadata:")
        for issue in issues:
            print(f"  - {issue}")

    http_failures = [
        check for check in source_checks if check.status == REACHABILITY_HTTP_FAILURE
    ]
    network_failures = [
        check for check in source_checks if check.status == REACHABILITY_NETWORK_FAILURE
    ]
    if http_failures:
        print("FAIL platform requirement sources:")
        for check in http_failures:
            print(f"  - {check.platform}: {check.detail}")
    if network_failures:
        print("INDETERMINATE platform requirement sources (network failure):")
        for check in network_failures:
            print(f"  - {check.platform}: {check.detail}")

    if issues or http_failures:
        status = "fail"
        exit_code = 1
    elif network_failures:
        status = "indeterminate"
        exit_code = 2
    else:
        status = "pass"
        exit_code = 0

    if args.report:
        try:
            _write_report(args.report, _report_payload(issues, source_checks, status))
        except OSError as error:
            print(f"FAIL platform requirement report: {error}")
            return 1

    if exit_code:
        return exit_code

    count = len(manifest["platform_requirements"])
    if args.check_reachability:
        print(
            f"PASS platform requirement metadata and sources: {count} platforms "
            f"verified within {args.max_age_days} days as of {args.as_of}"
        )
    else:
        print(
            f"PASS platform requirement metadata: {count} platforms verified "
            f"within {args.max_age_days} days as of {args.as_of}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
