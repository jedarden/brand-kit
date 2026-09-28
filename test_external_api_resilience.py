import io
import json
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.parse

import pytest

from tools import consumer_drift
from tools import http_policy
from tools import prune_ci_failure_watch_reports
from tools import retention_holds
from tools import release_publish


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def _http_error(url, status, *, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return urllib.error.HTTPError(
        url,
        status,
        "temporary failure",
        headers,
        io.BytesIO(b"temporary failure"),
    )


def test_read_api_retries_bounded_transient_failures_with_a_per_attempt_timeout(
    monkeypatch,
):
    calls = []
    responses = iter(
        [
            _http_error("https://forgejo.example/release", 503),
            _Response(b"{}"),
        ]
    )

    def urlopen(request, timeout):
        calls.append((request.method, timeout))
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []

    assert (
        release_publish.request_json(
            "GET",
            "https://forgejo.example/release",
            sleep=delays.append,
        )
        == {}
    )
    assert calls == [
        ("GET", http_policy.API_TIMEOUT_SECONDS),
        ("GET", http_policy.API_TIMEOUT_SECONDS),
    ]
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]


def test_read_api_caps_retry_after_and_fails_closed_after_three_attempts(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(timeout)
        raise _http_error(request.full_url, 429, retry_after=999)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []

    with pytest.raises(release_publish.HttpFailure) as error:
        release_publish.request_json(
            "GET",
            "https://forgejo.example/release",
            sleep=delays.append,
        )

    assert error.value.status == 429
    assert len(calls) == http_policy.MAX_READ_ATTEMPTS
    assert delays == [
        http_policy.MAX_RETRY_DELAY_SECONDS,
        http_policy.MAX_RETRY_DELAY_SECONDS,
    ]


def test_mutating_api_requests_are_single_attempt_even_for_5xx(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append((request.method, timeout))
        raise _http_error(request.full_url, 503)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []

    with pytest.raises(release_publish.HttpFailure) as error:
        release_publish.request_json(
            "POST",
            "https://argo.example/api/v1/workflows/argo-workflows",
            payload={"workflow": {}},
            authorization_scheme="Bearer",
            service="Argo API",
            sleep=delays.append,
        )

    assert error.value.status == 503
    assert calls == [("POST", http_policy.API_TIMEOUT_SECONDS)]
    assert delays == []


def test_request_timeout_must_be_finite_and_positive():
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(release_publish.ReleaseError, match="positive finite"):
            release_publish.request_json(
                "GET", "https://forgejo.example/release", timeout=timeout
            )


def test_public_profile_reader_retries_transport_failure_with_public_timeout(monkeypatch):
    calls = []
    responses = iter([urllib.error.URLError("profile unavailable"), _Response(b"image")])

    def urlopen(request, timeout):
        calls.append(timeout)
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []

    assert (
        consumer_drift.fetch_bytes(
            "https://profile.example/avatar", sleep=delays.append
        )
        == b"image"
    )
    assert calls == [
        http_policy.PUBLIC_TIMEOUT_SECONDS,
        http_policy.PUBLIC_TIMEOUT_SECONDS,
    ]


def test_garage_listing_retries_but_garage_delete_does_not(monkeypatch):
    list_calls = []
    list_responses = iter(
        [
            _http_error("https://garage.example/bucket", 502),
            _Response(
                b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                b"<IsTruncated>false</IsTruncated></ListBucketResult>"
            ),
        ]
    )

    def list_opener(request, timeout):
        list_calls.append((request.method, timeout))
        response = next(list_responses)
        if isinstance(response, BaseException):
            raise response
        return response

    list_delays = []
    client = prune_ci_failure_watch_reports.S3Client(
        "https://garage.example",
        "bucket",
        prune_ci_failure_watch_reports.S3Credentials("reader", "secret"),
        opener=list_opener,
        sleep=list_delays.append,
    )
    assert client.list_objects("prefix/") == []
    assert list_calls == [
        ("GET", http_policy.GARAGE_TIMEOUT_SECONDS),
        ("GET", http_policy.GARAGE_TIMEOUT_SECONDS),
    ]
    assert list_delays == [http_policy.RETRY_BACKOFF_SECONDS]

    delete_calls = []

    def delete_opener(request, timeout):
        delete_calls.append((request.method, timeout))
        raise _http_error(request.full_url, 503)

    delete_delays = []
    publisher = prune_ci_failure_watch_reports.S3Client(
        "https://garage.example",
        "bucket",
        prune_ci_failure_watch_reports.S3Credentials("publisher", "secret"),
        opener=delete_opener,
        sleep=delete_delays.append,
    )
    with pytest.raises(prune_ci_failure_watch_reports.RetentionError):
        publisher.delete_objects(["prefix/report.json"])
    assert delete_calls == [("POST", http_policy.GARAGE_TIMEOUT_SECONDS)]
    assert delete_delays == []


@pytest.mark.parametrize(
    ("prefix", "filename"),
    (
        ("failures/brand-kit-ci-failure-watch/v1/", "report.json"),
        ("attestations/brand-kit-ci/v1/", "attestations.json"),
        ("failures/brand-kit-consumer-drift/v1/", "report.json"),
    ),
)
def test_ambiguous_garage_delete_recovery_rechecks_active_holds(
    tmp_path, prefix, filename
):
    attempted = [f"{prefix}old-a/{filename}", f"{prefix}old-b/{filename}"]
    recent = f"{prefix}recent/report.json"
    objects = {
        attempted[0],
        attempted[1],
        recent,
        "unrelated/artifact/report.json",
    }
    delete_requests = []
    list_requests = []

    def opener(request, timeout):
        if request.method == "POST":
            delete_requests.append(request)
            # Garage applied part of the batch, then the response was lost.
            objects.remove(attempted[0])
            raise _http_error(request.full_url, 503)

        list_requests.append(request)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        expected_query = {"list-type": ["2"], "prefix": [prefix]}
        page_number = len(list_requests) - 1
        if page_number:
            expected_query["continuation-token"] = [f"page-{page_number}"]
        assert query == expected_query
        remaining = sorted(key for key in objects if key.startswith(prefix))
        key = remaining[page_number]
        last_modified = (
            "2026-08-27T12:00:00.000Z" if key in attempted else "2026-09-27T12:00:00.000Z"
        )
        contents = (
            f"<Contents><Key>{key}</Key>"
            f"<LastModified>{last_modified}</LastModified></Contents>"
        ).encode()
        truncated = page_number + 1 < len(remaining)
        continuation = (
            f"<NextContinuationToken>page-{page_number + 1}</NextContinuationToken>"
            if truncated
            else ""
        )
        return _Response(
            b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            + contents
            + f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{continuation}".encode()
            + b"</ListBucketResult>"
        )

    client = prune_ci_failure_watch_reports.S3Client(
        "https://garage.example",
        "needle-ci-artifacts",
        prune_ci_failure_watch_reports.S3Credentials("publisher", "secret"),
        opener=opener,
    )

    with pytest.raises(prune_ci_failure_watch_reports.RetentionError):
        client.delete_objects(attempted)

    # A hold can be recorded while an ambiguous request is being investigated.
    # Recovery reloads it, lists the exact prefix, and never replays the batch.
    holds_path = tmp_path / "retention-holds.json"
    held_key = attempted[1]
    holds_path.write_text(
        json.dumps(
            {
                "schema": retention_holds.SCHEMA,
                "holds": [
                    {
                        "url": f"https://garage.example/needle-ci-artifacts/{held_key}",
                        "reason": "Preserve during ambiguous deletion review",
                        "expires_at": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_root = tmp_path / "release-evidence" / "v1"
    evidence_root.mkdir(parents=True)
    survivors, eligible = retention_holds.reconcile_ambiguous_delete(
        client,
        prefix=prefix,
        attempted_keys=attempted,
        cutoff=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        filename=filename,
        holds_path=holds_path,
        evidence_root=evidence_root,
        endpoint="https://garage.example",
        bucket="needle-ci-artifacts",
        now=datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc),
    )
    remaining_attempted = sorted(item.key for item in survivors)

    assert remaining_attempted == [attempted[1]]
    assert eligible == []
    assert len(delete_requests) == 1
    assert len(list_requests) == 2
    assert all(request.method == "GET" for request in list_requests)


def test_ambiguous_garage_delete_recovery_is_documented():
    note = Path("docs/notes/external-api-resilience.md").read_text(encoding="utf-8")
    note = " ".join(note.split())

    for prefix in (
        "failures/brand-kit-ci-failure-watch/v1/",
        "attestations/brand-kit-ci/v1/",
        "failures/brand-kit-consumer-drift/v1/",
    ):
        assert prefix in note
    assert "Read every page" in note
    assert "the remaining keys" in note
    assert "Record the observation" in note
    assert "Never replay the old" in note


def test_external_api_timeout_and_retry_contract_is_documented():
    readme = Path("README.md").read_text(encoding="utf-8")
    note = Path("docs/notes/external-api-resilience.md").read_text(encoding="utf-8")
    for text in (readme, note):
        assert "429" in text
        assert "5xx" in text
        assert "no automatic retry" in text
        assert "indeterminate" in text
    assert "20 seconds" in note
    assert "15 seconds" in note
    assert "30 seconds" in note
