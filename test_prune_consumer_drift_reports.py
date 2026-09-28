import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tools import prune_consumer_drift_reports, release_evidence


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
ENDPOINT = "https://s3.example.test"
BUCKET = "needle-ci-artifacts"
PREFIX = "failures/brand-kit-consumer-drift/v1/"
CUSTOM_PREFIX = "failures/custom-consumer-drift/v9/"
HELD_KEY = f"{PREFIX}held/report.json"
HELD_URL = f"{ENDPOINT}/{BUCKET}/{HELD_KEY}"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _write_evidence(root: Path, report_url: str | None = HELD_URL) -> None:
    root.mkdir(parents=True, exist_ok=True)
    record = release_evidence.build_record(
        tag="v1.2.0",
        commit="a" * 40,
        ci_run="brand-kit-ci/run-123",
        ci_attestation_url=(
            "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
            "brand-kit-ci/v1/watcher/attestations.json"
        ),
        ci_workflow_uid="watcher-uid",
        ci_finished_at="2026-09-27T17:00:00Z",
        forgejo_release_url="https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0",
        consumer_workflow="brand-kit-consumer-drift-release-123",
        mirror_commit="a" * 40,
        consumer_report_url=report_url,
    )
    (root / "v1.2.0.json").write_text(json.dumps(record), encoding="utf-8")


def _clients(opener):
    credentials = prune_consumer_drift_reports.S3Credentials
    reader = prune_consumer_drift_reports.S3Client(
        ENDPOINT,
        BUCKET,
        credentials("reader", "reader-secret"),
        opener=opener,
        now=NOW,
    )
    publisher = prune_consumer_drift_reports.S3Client(
        ENDPOINT,
        BUCKET,
        credentials("publisher", "publisher-secret"),
        opener=opener,
        now=NOW,
    )
    return reader, publisher


def test_prune_reports_keeps_expired_release_evidence_holds_and_only_owns_prefix(
    tmp_path,
):
    evidence_root = tmp_path / "release-evidence" / "v1"
    _write_evidence(evidence_root)
    explicit_held_key = f"{PREFIX}manual-hold/report.json"
    holds_path = tmp_path / "retention-holds.json"
    holds_path.write_text(
        json.dumps(
            {
                "schema": "brand-kit-retention-holds/v1",
                "holds": [
                    {
                        "url": f"{ENDPOINT}/{BUCKET}/{explicit_held_key}",
                        "reason": "Keep this report available for an investigation",
                        "expires_at": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    requests = []
    responses = iter(
        [
            Response(
                (
                    '<?xml version="1.0"?><ListBucketResult '
                    'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<Contents><Key>{PREFIX}old/report.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{HELD_KEY}</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{explicit_held_key}</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}new/report.json</Key>"
                    "<LastModified>2026-09-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}boundary/report.json</Key>"
                    "<LastModified>2026-08-28T13:00:00.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}old/metadata.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    "<Contents><Key>failures/brand-kit-ci-failure-watch/v1/old/report.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    "<IsTruncated>false</IsTruncated></ListBucketResult>"
                ).encode()
            ),
            Response(
                (
                    '<?xml version="1.0"?><DeleteResult '
                    'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<Deleted><Key>{PREFIX}old/report.json</Key></Deleted>"
                    "</DeleteResult>"
                ).encode()
            ),
        ]
    )

    def opener(request, timeout):
        requests.append(request)
        assert timeout == 30
        return next(responses)

    reader, publisher = _clients(opener)
    pruned, cutoff = prune_consumer_drift_reports.prune_reports(
        reader,
        publisher,
        evidence_root=evidence_root,
        endpoint=ENDPOINT,
        bucket=BUCKET,
        prefix=PREFIX,
        retention_days=30,
        now=NOW,
        retention_holds_path=holds_path,
    )

    assert pruned == 1
    assert cutoff == datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    assert requests[0].method == "GET"
    assert parse_qs(urlsplit(requests[0].full_url).query) == {
        "list-type": ["2"],
        "prefix": [PREFIX],
    }
    assert "Credential=reader/" in requests[0].headers["Authorization"]
    assert requests[1].method == "POST"
    assert "Credential=publisher/" in requests[1].headers["Authorization"]
    assert PREFIX.encode() + b"old/report.json" in requests[1].data
    assert HELD_KEY.encode() not in requests[1].data
    assert explicit_held_key.encode() not in requests[1].data
    assert PREFIX.encode() + b"boundary/report.json" not in requests[1].data
    assert b"brand-kit-ci-failure-watch" not in requests[1].data


def test_dry_run_performs_listing_but_does_not_delete(tmp_path):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, report_url=None)
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return Response(
            (
                '<?xml version="1.0"?><ListBucketResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Contents><Key>{PREFIX}old/report.json</Key>"
                "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                "<IsTruncated>false</IsTruncated></ListBucketResult>"
            ).encode()
        )

    reader, publisher = _clients(opener)
    pruned, _ = prune_consumer_drift_reports.prune_reports(
        reader,
        publisher,
        evidence_root=evidence_root,
        endpoint=ENDPOINT,
        bucket=BUCKET,
        prefix=PREFIX,
        now=NOW,
        dry_run=True,
    )

    assert pruned == 1
    assert len(requests) == 1


def test_prune_reports_uses_configured_namespace_and_preserves_non_report_metadata(
    tmp_path,
):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, report_url=None)
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return Response(
                (
                    '<?xml version="1.0"?><ListBucketResult '
                    'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<Contents><Key>{CUSTOM_PREFIX}old/report.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{CUSTOM_PREFIX}old/metadata.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}old/report.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    "<IsTruncated>false</IsTruncated></ListBucketResult>"
                ).encode()
            )
        return Response(
            (
                '<?xml version="1.0"?><DeleteResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Deleted><Key>{CUSTOM_PREFIX}old/report.json</Key></Deleted>"
                "</DeleteResult>"
            ).encode()
        )

    reader, publisher = _clients(opener)
    pruned, _ = prune_consumer_drift_reports.prune_reports(
        reader,
        publisher,
        evidence_root=evidence_root,
        endpoint=ENDPOINT,
        bucket=BUCKET,
        prefix=CUSTOM_PREFIX,
        retention_days=30,
        now=NOW,
    )

    assert pruned == 1
    assert parse_qs(urlsplit(requests[0].full_url).query) == {
        "list-type": ["2"],
        "prefix": [CUSTOM_PREFIX],
    }
    assert CUSTOM_PREFIX.encode() + b"old/report.json" in requests[1].data
    assert CUSTOM_PREFIX.encode() + b"old/metadata.json" not in requests[1].data
    assert PREFIX.encode() + b"old/report.json" not in requests[1].data


def test_list_objects_preserves_last_modified_metadata_for_retention_decisions():
    timestamp = "2026-08-27T12:59:59.000Z"

    def opener(request, timeout):
        return Response(
            (
                '<?xml version="1.0"?><ListBucketResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Contents><Key>{CUSTOM_PREFIX}old/report.json</Key>"
                f"<LastModified>{timestamp}</LastModified></Contents>"
                "<IsTruncated>false</IsTruncated></ListBucketResult>"
            ).encode()
        )

    reader, _ = _clients(opener)
    objects = reader.list_objects(CUSTOM_PREFIX)

    assert [(item.key, item.last_modified) for item in objects] == [
        (
            f"{CUSTOM_PREFIX}old/report.json",
            datetime(2026, 8, 27, 12, 59, 59, tzinfo=timezone.utc),
        )
    ]


@pytest.mark.parametrize(
    "report_url",
    [
        f"{ENDPOINT}/{BUCKET}/{PREFIX}%2e%2e/escape/report.json",
        f"{ENDPOINT}/{BUCKET}/{PREFIX}workflow/%2e%2e/escape/report.json",
        f"{ENDPOINT}/{BUCKET}/{PREFIX}nested/workflow/report.json",
        f"{ENDPOINT}/{BUCKET}/{PREFIX}workflow/./report.json",
    ],
)
def test_prune_rejects_unsafe_report_artifact_paths(report_url, tmp_path):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, report_url=report_url)

    with pytest.raises(
        prune_consumer_drift_reports.RetentionError,
        match="unsafe.*artifact path",
    ):
        prune_consumer_drift_reports.protected_report_keys(
            evidence_root,
            endpoint=ENDPOINT,
            bucket=BUCKET,
            prefix=PREFIX,
        )


@pytest.mark.parametrize("last_modified", (None, "not-a-timestamp"))
def test_prune_fails_closed_when_report_metadata_is_malformed(
    last_modified, tmp_path
):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, report_url=None)
    requests = []
    modified_xml = (
        ""
        if last_modified is None
        else f"<LastModified>{last_modified}</LastModified>"
    )

    def opener(request, timeout):
        requests.append(request)
        return Response(
            (
                '<?xml version="1.0"?><ListBucketResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Contents><Key>{PREFIX}old/report.json</Key>"
                f"{modified_xml}</Contents>"
                "<IsTruncated>false</IsTruncated></ListBucketResult>"
            ).encode()
        )

    reader, publisher = _clients(opener)
    with pytest.raises(prune_consumer_drift_reports.RetentionError, match="LastModified"):
        prune_consumer_drift_reports.prune_reports(
            reader,
            publisher,
            evidence_root=evidence_root,
            endpoint=ENDPOINT,
            bucket=BUCKET,
            prefix=PREFIX,
            now=NOW,
        )

    assert len(requests) == 1


def test_prune_fails_closed_before_listing_when_evidence_is_malformed(tmp_path):
    evidence_root = tmp_path / "release-evidence"
    evidence_root.mkdir()
    (evidence_root / "broken.json").write_text('{"schema_version": 1}', encoding="utf-8")
    listed = False

    class Reader:
        def list_objects(self, prefix):
            nonlocal listed
            listed = True
            return []

    with pytest.raises(prune_consumer_drift_reports.RetentionError, match="broken.json"):
        prune_consumer_drift_reports.prune_reports(
            Reader(),
            object(),
            evidence_root=evidence_root,
            now=NOW,
        )

    assert listed is False


def test_prune_fails_closed_for_a_release_evidence_url_outside_configured_storage(
    tmp_path,
):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, report_url="https://other.example/report.json")

    with pytest.raises(prune_consumer_drift_reports.RetentionError, match="configured S3"):
        prune_consumer_drift_reports.protected_report_keys(
            evidence_root,
            endpoint=ENDPOINT,
            bucket=BUCKET,
            prefix=PREFIX,
        )


@pytest.mark.parametrize("retention_days", (0, 3651))
def test_prune_reports_rejects_an_unusable_retention_window(retention_days, tmp_path):
    with pytest.raises(prune_consumer_drift_reports.RetentionError):
        prune_consumer_drift_reports.prune_reports(
            object(),
            object(),
            evidence_root=tmp_path,
            prefix=PREFIX,
            retention_days=retention_days,
            now=NOW,
        )


def test_missing_evidence_directory_is_not_treated_as_no_holds(tmp_path):
    with pytest.raises(prune_consumer_drift_reports.RetentionError, match="unavailable"):
        prune_consumer_drift_reports.protected_report_keys(tmp_path / "missing")
