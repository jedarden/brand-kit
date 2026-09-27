from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import urllib.error

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


def test_checker_accepts_metadata_on_the_expiry_boundary():
    manifest = _manifest()
    manifest["platform_requirements"][0]["last_verified"] = "2026-03-31"

    assert check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27), max_age_days=180
    ) == []


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


class _Response:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


def _one_requirement_manifest():
    manifest = _manifest()
    manifest["platform_requirements"] = manifest["platform_requirements"][:1]
    platform = manifest["platform_requirements"][0]["platform"]
    manifest["assets"] = [
        asset for asset in manifest["assets"] if asset["platform"] == platform
    ]
    return manifest


@pytest.mark.parametrize(
    "source_url",
    (
        "http://example.test/requirements",
        "https://user:password@example.test/requirements",
        "https://example.test/requirements with spaces",
    ),
)
def test_invalid_requirement_urls_fail_without_mutating_manifest(source_url):
    manifest = _one_requirement_manifest()
    manifest["platform_requirements"][0]["source_url"] = source_url
    before = deepcopy(manifest)

    assert check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27)
    ) == ["X / Twitter: source_url must be an HTTPS URL"]
    assert manifest == before


@pytest.mark.parametrize(
    ("last_verified", "expected"),
    (
        (
            "2026-02-30",
            "X / Twitter: last_verified is not an ISO date: '2026-02-30'",
        ),
        (
            "2026-09-28",
            "X / Twitter: last_verified 2026-09-28 is in the future",
        ),
        (
            "2025-01-01",
            "X / Twitter: last_verified 2025-01-01 is 634 days old (maximum 180)",
        ),
    ),
)
def test_verification_date_failures_are_reported_without_mutating_manifest(
    last_verified, expected
):
    manifest = _one_requirement_manifest()
    manifest["platform_requirements"][0]["last_verified"] = last_verified
    before = deepcopy(manifest)

    assert check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27), max_age_days=180
    ) == [expected]
    assert manifest == before


def test_source_reachability_uses_injected_opener_and_accepts_success():
    calls = []
    response = _Response(204)

    def opener(request, timeout):
        calls.append((request, timeout))
        return response

    checks = check_platform_requirements.check_platform_requirement_sources(
        _one_requirement_manifest(), opener=opener, timeout_seconds=3
    )

    assert [(check.platform, check.status, check.detail) for check in checks] == [
        ("X / Twitter", check_platform_requirements.REACHABILITY_PASS, "HTTP 204")
    ]
    assert calls[0][0].full_url == (
        "https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo"
    )
    assert calls[0][1] == 3
    assert response.closed


def test_source_reachability_accepts_redirects_without_mutating_manifest():
    manifest = _one_requirement_manifest()
    before = deepcopy(manifest)
    response = _Response(302)

    checks = check_platform_requirements.check_platform_requirement_sources(
        manifest, opener=lambda request, timeout: response
    )

    assert [(check.status, check.detail) for check in checks] == [
        (check_platform_requirements.REACHABILITY_PASS, "HTTP 302")
    ]
    assert response.closed
    assert manifest == before


def test_source_reachability_distinguishes_http_failure_from_network_failure():
    manifest = _one_requirement_manifest()

    http_checks = check_platform_requirements.check_platform_requirement_sources(
        manifest,
        opener=lambda request, timeout: _Response(404),
    )
    network_checks = check_platform_requirements.check_platform_requirement_sources(
        manifest,
        opener=lambda request, timeout: (_ for _ in ()).throw(
            urllib.error.URLError("DNS unavailable")
        ),
    )

    assert http_checks[0].status == check_platform_requirements.REACHABILITY_HTTP_FAILURE
    assert http_checks[0].detail == "HTTP 404"
    assert network_checks[0].status == check_platform_requirements.REACHABILITY_NETWORK_FAILURE
    assert network_checks[0].detail == "network error: <urlopen error DNS unavailable>"


def test_source_reachability_skips_invalid_urls_and_reports_metadata_separately():
    manifest = _one_requirement_manifest()
    manifest["platform_requirements"][0]["source_url"] = "https://user:password@example.test/"
    calls = []

    checks = check_platform_requirements.check_platform_requirement_sources(
        manifest, opener=lambda *args, **kwargs: calls.append(args)
    )

    assert checks == []
    assert calls == []
    assert "source_url must be an HTTPS URL" in check_platform_requirements.check_platform_requirements(
        manifest, as_of=date(2026, 9, 27)
    )[0]


def test_main_returns_indeterminate_for_network_failure(monkeypatch, capsys):
    def offline(request, timeout):
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", offline)

    assert check_platform_requirements.main(
        ["--check-reachability", "--as-of", "2026-09-27"]
    ) == 2
    output = capsys.readouterr().out
    assert "INDETERMINATE platform requirement sources (network failure):" in output
    assert "PASS platform requirement metadata and sources" not in output


def test_main_reports_unreachable_source_and_preserves_manifest(
    monkeypatch, capsys, tmp_path
):
    manifest = _one_requirement_manifest()
    manifest_path = tmp_path / "platform-assets.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    before = manifest_path.read_bytes()
    report = tmp_path / "platform-requirements.json"
    monkeypatch.setattr(check_platform_requirements, "MANIFEST_PATH", manifest_path)

    def offline(request, timeout):
        raise urllib.error.URLError("DNS unavailable")

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", offline)

    assert check_platform_requirements.main(
        [
            "--check-reachability",
            "--as-of",
            "2026-09-27",
            "--report",
            str(report),
        ]
    ) == 2
    assert "INDETERMINATE platform requirement sources (network failure):" in capsys.readouterr().out
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "indeterminate"
    assert payload["summary"][check_platform_requirements.REACHABILITY_NETWORK_FAILURE] == 1
    assert manifest_path.read_bytes() == before


def test_main_reports_stale_provenance_and_preserves_manifest(monkeypatch, capsys, tmp_path):
    manifest = _one_requirement_manifest()
    manifest["platform_requirements"][0]["last_verified"] = "2025-01-01"
    manifest_path = tmp_path / "platform-assets.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    before = manifest_path.read_bytes()
    report = tmp_path / "platform-requirements.json"
    monkeypatch.setattr(check_platform_requirements, "MANIFEST_PATH", manifest_path)

    assert check_platform_requirements.main(
        [
            "--as-of",
            "2026-09-27",
            "--report",
            str(report),
        ]
    ) == 1
    output = capsys.readouterr().out
    assert "FAIL platform requirement metadata:" in output
    assert "X / Twitter: last_verified 2025-01-01 is 634 days old (maximum 180)" in output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert payload["summary"]["metadata_failures"] == 1
    assert manifest_path.read_bytes() == before


def test_main_returns_pass_when_all_sources_are_reachable(monkeypatch, capsys):
    monkeypatch.setattr(
        check_platform_requirements.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(200),
    )

    assert check_platform_requirements.main(
        ["--check-reachability", "--as-of", "2026-09-27"]
    ) == 0
    assert "PASS platform requirement metadata and sources: 13 platforms" in capsys.readouterr().out


def test_main_returns_conclusive_failure_for_http_failure_and_writes_report(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(
        check_platform_requirements.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(503),
    )
    report = tmp_path / "platform-requirements.json"

    assert check_platform_requirements.main(
        [
            "--check-reachability",
            "--as-of",
            "2026-09-27",
            "--report",
            str(report),
        ]
    ) == 1
    assert "FAIL platform requirement sources:" in capsys.readouterr().out
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert payload["summary"][check_platform_requirements.REACHABILITY_HTTP_FAILURE] == 13


def test_workflow_contract_runs_reachability_check_and_retains_result():
    template = Path(
        "automation/brand-kit-platform-requirements-workflowtemplate.yml"
    ).read_text(encoding="utf-8")
    cron = Path("automation/brand-kit-platform-requirements-cronworkflow.yml").read_text(
        encoding="utf-8"
    )

    assert "kind: WorkflowTemplate" in template
    assert "name: brand-kit-platform-requirements" in template
    assert "tools/check_platform_requirements.py" in template
    assert "--check-reachability" in template
    assert "--max-age-days 180" in template
    assert "--timeout-seconds 15" in template
    assert "git.ardenone.com/jedarden/brand-kit.git" in template
    assert "artifactGC:\n                strategy: Never" in template
    assert "failures/brand-kit-platform-requirements/v1/{{workflow.uid}}/report.json" in template
    assert '"alertname": "BrandKitPlatformRequirements"' in template
    assert 'when: "{{workflow.status}} != Succeeded"' in template
    assert "git push" not in template
    assert "git commit" not in template

    assert "kind: CronWorkflow" in cron
    assert '    - "27 6 * * *"' in cron
    assert "workflowTemplateRef:" in cron
    assert "name: brand-kit-platform-requirements" in cron
