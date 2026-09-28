import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tools import prune_ci_attestations, release_evidence


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
ENDPOINT = "https://s3.ardenone.com"
BUCKET = "needle-ci-artifacts"
PREFIX = "attestations/brand-kit-ci/v1/"
HELD_KEY = f"{PREFIX}held-watcher/attestations.json"
HELD_URL = f"{ENDPOINT}/{BUCKET}/{HELD_KEY}"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _write_evidence(root: Path, attestation_url: str = HELD_URL) -> None:
    root.mkdir(parents=True, exist_ok=True)
    record = release_evidence.build_record(
        tag="v1.2.0",
        commit="a" * 40,
        ci_run="brand-kit-ci/run-123",
        ci_attestation_url=attestation_url,
        ci_workflow_uid="ci-uid",
        ci_finished_at="2026-09-27T17:00:00Z",
        forgejo_release_url="https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0",
        consumer_workflow="brand-kit-consumer-drift-release-123",
        mirror_commit="a" * 40,
        consumer_report_url=None,
    )
    (root / "v1.2.0.json").write_text(json.dumps(record), encoding="utf-8")


def _clients(opener):
    credentials = prune_ci_attestations.S3Credentials
    reader = prune_ci_attestations.S3Client(
        ENDPOINT,
        BUCKET,
        credentials("reader", "reader-secret"),
        opener=opener,
        now=NOW,
    )
    publisher = prune_ci_attestations.S3Client(
        ENDPOINT,
        BUCKET,
        credentials("publisher", "publisher-secret"),
        opener=opener,
        now=NOW,
    )
    return reader, publisher


def test_prune_keeps_release_holds_and_only_deletes_expired_attestations(tmp_path):
    evidence_root = tmp_path / "release-evidence" / "v1"
    _write_evidence(evidence_root)
    explicit_held_key = f"{PREFIX}manual-hold/attestations.json"
    holds_path = tmp_path / "retention-holds.json"
    holds_path.write_text(
        json.dumps(
            {
                "schema": "brand-kit-retention-holds/v1",
                "holds": [
                    {
                        "url": f"{ENDPOINT}/{BUCKET}/{explicit_held_key}",
                        "reason": "Keep this run available for an investigation",
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
                    f"<Contents><Key>{PREFIX}old-watcher/attestations.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{HELD_KEY}</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{explicit_held_key}</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}new-watcher/attestations.json</Key>"
                    "<LastModified>2026-09-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}boundary-watcher/attestations.json</Key>"
                    "<LastModified>2026-08-28T13:00:00.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}old-watcher/metadata.json</Key>"
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
                    f"<Deleted><Key>{PREFIX}old-watcher/attestations.json</Key></Deleted>"
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
    pruned, cutoff = prune_ci_attestations.prune_attestations(
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
    assert f"{PREFIX}old-watcher/attestations.json".encode() in requests[1].data
    assert HELD_KEY.encode() not in requests[1].data
    assert explicit_held_key.encode() not in requests[1].data
    assert f"{PREFIX}boundary-watcher/attestations.json".encode() not in requests[1].data
    assert b"failures/brand-kit-ci-failure-watch" not in requests[1].data


def test_dry_run_lists_without_deleting(tmp_path):
    evidence_root = tmp_path / "release-evidence"
    _write_evidence(evidence_root, attestation_url=HELD_URL)
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return Response(
            (
                '<?xml version="1.0"?><ListBucketResult '
                'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Contents><Key>{PREFIX}old-watcher/attestations.json</Key>"
                "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                "<IsTruncated>false</IsTruncated></ListBucketResult>"
            ).encode()
        )

    reader, publisher = _clients(opener)
    pruned, _ = prune_ci_attestations.prune_attestations(
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

    with pytest.raises(prune_ci_attestations.RetentionError, match="broken.json"):
        prune_ci_attestations.prune_attestations(
            Reader(),
            object(),
            evidence_root=evidence_root,
            now=NOW,
        )

    assert listed is False


def test_prune_rejects_a_hold_outside_the_configured_endpoint(tmp_path):
    evidence_root = tmp_path / "release-evidence"
    record = release_evidence.build_record(
        tag="v1.2.0",
        commit="a" * 40,
        ci_run="brand-kit-ci/run-123",
        ci_attestation_url=HELD_URL,
        ci_workflow_uid="ci-uid",
        ci_finished_at="2026-09-27T17:00:00Z",
        forgejo_release_url="https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0",
        consumer_workflow="brand-kit-consumer-drift-release-123",
        mirror_commit="a" * 40,
        consumer_report_url=None,
    )
    evidence_root.mkdir(parents=True)
    (evidence_root / "v1.2.0.json").write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(prune_ci_attestations.RetentionError, match="configured S3"):
        prune_ci_attestations.protected_attestation_keys(
            evidence_root,
            endpoint="https://s3.other.example",
            bucket=BUCKET,
            prefix=PREFIX,
        )


@pytest.mark.parametrize("retention_days", (0, 3651))
def test_prune_rejects_an_unusable_retention_window(retention_days, tmp_path):
    with pytest.raises(prune_ci_attestations.RetentionError):
        prune_ci_attestations.prune_attestations(
            object(),
            object(),
            evidence_root=tmp_path,
            prefix=PREFIX,
            retention_days=retention_days,
            now=NOW,
        )


def test_missing_evidence_directory_is_not_treated_as_no_holds(tmp_path):
    with pytest.raises(prune_ci_attestations.RetentionError, match="unavailable"):
        prune_ci_attestations.protected_attestation_keys(tmp_path / "missing")
