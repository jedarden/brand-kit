import io
from datetime import datetime, timezone

import pytest

from tools import prune_ci_failure_watch_reports


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
PREFIX = "failures/brand-kit-ci-failure-watch/v1/"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def test_prune_reports_lists_with_reader_and_deletes_only_expired_reports():
    requests = []
    responses = iter(
        [
            Response(
                (
                    "<?xml version=\"1.0\"?><ListBucketResult "
                    "xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                    f"<Contents><Key>{PREFIX}old/report.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}new/report.json</Key>"
                    "<LastModified>2026-09-27T12:59:59.000Z</LastModified></Contents>"
                    f"<Contents><Key>{PREFIX}old/attestations.json</Key>"
                    "<LastModified>2026-08-27T12:59:59.000Z</LastModified></Contents>"
                    "<IsTruncated>false</IsTruncated></ListBucketResult>"
                ).encode()
            ),
            Response(
                (
                    "<?xml version=\"1.0\"?><DeleteResult "
                    "xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
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

    reader = prune_ci_failure_watch_reports.S3Client(
        "https://s3.example.test",
        "needle-ci-artifacts",
        prune_ci_failure_watch_reports.S3Credentials("reader", "reader-secret"),
        opener=opener,
        now=NOW,
    )
    publisher = prune_ci_failure_watch_reports.S3Client(
        "https://s3.example.test",
        "needle-ci-artifacts",
        prune_ci_failure_watch_reports.S3Credentials("publisher", "publisher-secret"),
        opener=opener,
        now=NOW,
    )

    pruned, cutoff = prune_ci_failure_watch_reports.prune_reports(
        reader,
        publisher,
        prefix=PREFIX,
        retention_days=30,
        now=NOW,
    )

    assert pruned == 1
    assert cutoff == datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    assert requests[0].method == "GET"
    assert "list-type=2" in requests[0].full_url
    assert requests[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert requests[1].method == "POST"
    assert requests[1].full_url.endswith("/needle-ci-artifacts?delete=")
    assert PREFIX.encode() + b"old/report.json" in requests[1].data
    assert PREFIX.encode() + b"old/attestations.json" not in requests[1].data
    assert requests[1].headers["Content-md5"]
    assert "publisher-secret" not in requests[1].headers["Authorization"]


@pytest.mark.parametrize("retention_days", (0, 3651))
def test_prune_reports_rejects_an_unusable_retention_window(retention_days):
    client = object()

    with pytest.raises(prune_ci_failure_watch_reports.RetentionError):
        prune_ci_failure_watch_reports.prune_reports(
            client,
            client,
            prefix=PREFIX,
            retention_days=retention_days,
            now=NOW,
        )
