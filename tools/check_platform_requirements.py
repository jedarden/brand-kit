#!/usr/bin/env python3
"""Check source freshness, evidence coverage, reachability, and content drift.

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
import hashlib
from html.parser import HTMLParser
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
REPORT_SCHEMA = "brand-kit-platform-requirements/v2"
EVIDENCE_CLASSIFICATIONS = {
    "upload_minimum",
    "recommendation",
    "specified_size",
    "within_limits",
    "project_choice",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REACHABILITY_CONTENT_CHANGED = "content_changed"
REACHABILITY_UNPINNED = "content_unpinned"


@dataclass(frozen=True)
class SourceCheck:
    """The conclusive or indeterminate result for one valid source URL."""

    platform: str
    source_url: str
    status: str
    detail: str
    content_sha256: str | None = None


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
        if set(requirement) != REQUIRED_FIELDS | {"source_content_sha256"}:
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
        fingerprint = requirement["source_content_sha256"]
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint)
        ):
            issues.append(
                f"{platform}: source_content_sha256 must be a lowercase SHA-256 or null"
            )
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


def check_requirement_evidence(manifest: dict) -> list[str]:
    """Require an evidence record for every distinct platform, role, and output size."""
    assets = manifest.get("assets")
    evidence = manifest.get("requirement_evidence")
    if not isinstance(assets, list):
        return ["assets is not a list"]
    if not isinstance(evidence, list):
        return ["requirement_evidence is not a list"]

    expected: set[tuple[str, str, int, int]] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        platform, role = asset.get("platform"), asset.get("role")
        dimensions = asset.get("dimensions")
        if not isinstance(platform, str) or not isinstance(role, str) or not isinstance(dimensions, dict):
            continue
        sizes = dimensions.get("sizes")
        candidates = sizes if isinstance(sizes, list) else [dimensions]
        for size in candidates:
            if (
                isinstance(size, dict)
                and type(size.get("width")) is int
                and type(size.get("height")) is int
            ):
                expected.add((platform, role, size["width"], size["height"]))

    issues: list[str] = []
    seen: set[tuple[str, str, int, int]] = set()
    required_fields = {
        "platform", "role", "dimensions", "classification", "source_url", "evidence"
    }
    for index, item in enumerate(evidence):
        label = f"requirement evidence {index}"
        if not isinstance(item, dict):
            issues.append(f"{label} is not an object")
            continue
        if set(item) != required_fields | {"source_content_sha256"}:
            issues.append(f"{label} has fields {sorted(item)}")
            continue
        platform, role = item["platform"], item["role"]
        dimensions = item["dimensions"]
        classification, source_url, statement = (
            item["classification"], item["source_url"], item["evidence"]
        )
        if not isinstance(platform, str) or not isinstance(role, str):
            issues.append(f"{label} has an invalid platform or role")
            continue
        if not isinstance(dimensions, dict) or set(dimensions) != {"width", "height"}:
            issues.append(f"{label} dimensions must contain width and height")
            continue
        width, height = dimensions["width"], dimensions["height"]
        if type(width) is not int or width < 1 or type(height) is not int or height < 1:
            issues.append(f"{label} has invalid dimensions")
            continue
        key = (platform, role, width, height)
        if key in seen:
            issues.append(f"duplicate requirement evidence: {platform} {role} {width}x{height}")
        seen.add(key)
        if not isinstance(classification, str) or classification not in EVIDENCE_CLASSIFICATIONS:
            issues.append(f"{label} has an invalid classification")
        if not _is_valid_source_url(source_url):
            issues.append(f"{label} source_url must be an HTTPS URL")
        if not isinstance(statement, str) or not statement.strip():
            issues.append(f"{label} requires a source comparison statement")
        fingerprint = item["source_content_sha256"]
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint)
        ):
            issues.append(f"{label} has an invalid source fingerprint")

    missing, unexpected = sorted(expected - seen), sorted(seen - expected)
    if missing:
        issues.append("missing dimension evidence: " + ", ".join(
            f"{platform}/{role}/{width}x{height}"
            for platform, role, width, height in missing
        ))
    if unexpected:
        issues.append("evidence has no matching asset: " + ", ".join(
            f"{platform}/{role}/{width}x{height}"
            for platform, role, width, height in unexpected
        ))
    return issues


def check_platform_requirement_sources(
    manifest: dict,
    *,
    opener=None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[SourceCheck]:
    """Check reachability and compare successful responses with their content pins.

    A 2xx or 3xx response passes only when its normalized visible text matches
    the reviewed SHA-256. The opener is injectable so tests stay offline.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    requirements = manifest.get("platform_requirements")
    if not isinstance(requirements, list):
        return []
    if opener is None:
        opener = urllib.request.urlopen

    evidence = manifest.get("requirement_evidence")
    checks: list[SourceCheck] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        platform = requirement.get("platform")
        source_url = requirement.get("source_url")
        if not isinstance(platform, str) or not platform or not _is_valid_source_url(source_url):
            continue

        citations = {source_url: requirement.get("source_content_sha256")}
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict) or item.get("platform") != platform:
                    continue
                evidence_url = item.get("source_url")
                if _is_valid_source_url(evidence_url):
                    citations.setdefault(evidence_url, item.get("source_content_sha256"))

        for citation_url, expected_digest in citations.items():
            checks.append(_check_one_source(
                platform, citation_url, expected_digest, opener, timeout_seconds
            ))
    return checks


def _check_one_source(platform, source_url, expected_digest, opener, timeout_seconds):
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
            return SourceCheck(
                platform, source_url, REACHABILITY_NETWORK_FAILURE,
                "network error: response had no HTTP status",
            )
        if not 200 <= status < 400:
            return SourceCheck(platform, source_url, REACHABILITY_HTTP_FAILURE, f"HTTP {status}")

        read = getattr(response, "read", None)
        body = read() if callable(read) else b""
        actual_digest = _source_content_digest(body)
        if expected_digest is None:
            return SourceCheck(
                platform, source_url, REACHABILITY_UNPINNED,
                f"HTTP {status}; source content has no baseline digest",
                actual_digest,
            )
        if actual_digest != expected_digest:
            return SourceCheck(
                platform, source_url, REACHABILITY_CONTENT_CHANGED,
                f"HTTP {status}; source content SHA-256 changed "
                f"(expected {expected_digest}, got {actual_digest})",
                actual_digest,
            )
        return SourceCheck(
            platform, source_url, REACHABILITY_PASS,
            f"HTTP {status}; source content SHA-256 matches",
            actual_digest,
        )
    except urllib.error.HTTPError as error:
        return SourceCheck(platform, source_url, REACHABILITY_HTTP_FAILURE, f"HTTP {error.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return SourceCheck(
            platform, source_url, REACHABILITY_NETWORK_FAILURE,
            f"network error: {_safe_network_detail(error)}",
        )
    except Exception as error:
        return SourceCheck(
            platform, source_url, REACHABILITY_NETWORK_FAILURE,
            f"network error: {_safe_network_detail(error)}",
        )
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


def _source_content_digest(body: bytes) -> str:
    """Hash visible text while ignoring HTML script/style and whitespace formatting."""
    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.skip_depth = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() in {"script", "style", "noscript", "svg"}:
                self.skip_depth += 1

        def handle_endtag(self, tag):
            if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip_depth:
                self.skip_depth -= 1

        def handle_data(self, data):
            if not self.skip_depth:
                self.parts.append(data)

    text = body.decode("utf-8", errors="replace")
    parser = VisibleText()
    parser.feed(text)
    visible = " ".join(" ".join(parser.parts).split())
    content = visible if visible else " ".join(text.split())
    content = re.sub(r"\b[0-9]{8,}\b", "[generated-id]", content)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _report_payload(
    metadata_issues: list[str],
    evidence_issues: list[str],
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
        REACHABILITY_CONTENT_CHANGED: sum(
            check.status == REACHABILITY_CONTENT_CHANGED for check in source_checks
        ),
        REACHABILITY_UNPINNED: sum(
            check.status == REACHABILITY_UNPINNED for check in source_checks
        ),
    }
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "summary": summary,
        "metadata_issues": metadata_issues,
        "evidence_issues": evidence_issues,
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
        help="fetch HTTPS sources and fail on reachability or source-content drift",
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
    evidence_issues: list[str] = []
    try:
        manifest = _load_manifest(MANIFEST_PATH)
        issues = check_platform_requirements(
            manifest, as_of=args.as_of, max_age_days=args.max_age_days
        )
        evidence_issues = check_requirement_evidence(manifest)
        if args.check_reachability:
            source_checks = check_platform_requirement_sources(
                manifest, timeout_seconds=args.timeout_seconds
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL platform requirement metadata: {error}")
        if args.report:
            _write_report(
                args.report,
                _report_payload([str(error)], evidence_issues, source_checks, "fail"),
            )
        return 1

    if issues:
        print("FAIL platform requirement metadata:")
        for issue in issues:
            print(f"  - {issue}")
    if evidence_issues:
        print("FAIL platform requirement evidence:")
        for issue in evidence_issues:
            print(f"  - {issue}")

    http_failures = [
        check for check in source_checks if check.status == REACHABILITY_HTTP_FAILURE
    ]
    network_failures = [
        check for check in source_checks if check.status == REACHABILITY_NETWORK_FAILURE
    ]
    content_changes = [
        check for check in source_checks if check.status == REACHABILITY_CONTENT_CHANGED
    ]
    unpinned_sources = [
        check for check in source_checks if check.status == REACHABILITY_UNPINNED
    ]
    if http_failures:
        print("FAIL platform requirement sources:")
        for check in http_failures:
            print(f"  - {check.platform}: {check.detail}")
    if network_failures:
        print("INDETERMINATE platform requirement sources (network failure):")
        for check in network_failures:
            print(f"  - {check.platform}: {check.detail}")
    if content_changes:
        print("FAIL platform requirement source content changed:")
        for check in content_changes:
            print(f"  - {check.platform}: {check.detail}")
    if unpinned_sources:
        print("INDETERMINATE platform requirement source content (no digest baseline):")
        for check in unpinned_sources:
            print(f"  - {check.platform}: {check.detail}")

    if issues or evidence_issues or http_failures or content_changes:
        status = "fail"
        exit_code = 1
    elif network_failures or unpinned_sources:
        status = "indeterminate"
        exit_code = 2
    else:
        status = "pass"
        exit_code = 0

    if args.report:
        try:
            _write_report(
                args.report,
                _report_payload(issues, evidence_issues, source_checks, status),
            )
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
