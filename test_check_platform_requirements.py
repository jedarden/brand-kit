from datetime import date
import json
from pathlib import Path

import pytest

from tools import check_platform_requirements


ROOT = Path(__file__).resolve().parent


def _manifest():
    return json.loads((ROOT / "platform-assets.json").read_text(encoding="utf-8"))


def test_current_platform_requirement_metadata_is_covered_and_fresh():
    manifest = _manifest()

    assert check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27)
    ) == []


def test_checker_reports_stale_requirement_metadata():
    manifest = _manifest()
    manifest["platform_requirements"][0]["last_verified"] = "2025-01-01"

    issues = check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27), max_age_days=180
    )

    assert issues == [
        "X / Twitter: last_verified 2025-01-01 is 634 days old (maximum 180)"
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        (lambda manifest: manifest["platform_requirements"].pop(), "missing platform requirements: Web / Open Graph"),
        (lambda manifest: manifest["platform_requirements"].append({
            "platform": "Unknown",
            "source_url": "https://example.com/requirements",
            "last_verified": "2026-09-27",
        }), "requirements have no assets: Unknown"),
    ),
)
def test_checker_reports_platform_coverage_drift(change, expected):
    manifest = _manifest()
    change(manifest)

    assert expected in check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27)
    )


def test_checker_reports_future_dates_and_invalid_urls():
    manifest = _manifest()
    requirement = manifest["platform_requirements"][0]
    requirement["source_url"] = "http://example.com/requirements"
    requirement["last_verified"] = "2026-09-28"

    assert check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27)
    ) == [
        "X / Twitter: source_url must be an HTTPS URL",
        "X / Twitter: last_verified 2026-09-28 is in the future",
    ]
