import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.parse

import pytest

from tools import brand_kit_ci_failure_watch
from tools import check_platform_requirements
from tools import consumer_drift
from tools import consumer_drift_submit
from tools import http_policy
from tools import kubernetes_api
from tools import prune_ci_failure_watch_reports
from tools import release_token_probe
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


def test_watcher_retries_the_same_kubernetes_page_with_a_bounded_timeout(monkeypatch):
    seen = []
    delays = []
    now = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(kubernetes_api, "service_account_context", lambda directory: None)
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    def urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        if len(seen) == 1:
            return _Response(
                json.dumps({"items": [], "metadata": {"continue": "page-2"}}).encode()
            )
        if len(seen) == 2:
            raise _http_error(request.full_url, 503)
        return _Response(json.dumps({"items": [], "metadata": {}}).encode())

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    report = brand_kit_ci_failure_watch.run_watch(
        "projected-token",
        now=now,
        request=kubernetes_api.request_function(Path("/unused/serviceaccount")),
    )

    assert report["status"] == "pass"
    assert report["pagination"]["pages_read"] == 2
    assert report["cutoff"]["at"] == "2026-09-27T11:00:00Z"
    assert seen[1][0] == seen[2][0]
    assert "continue=page-2" in seen[1][0]
    assert all(timeout == http_policy.KUBERNETES_TIMEOUT_SECONDS for _, timeout in seen)
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]


def test_watcher_exhausted_kubernetes_page_never_returns_a_partial_result(monkeypatch):
    seen = []
    delays = []
    monkeypatch.setattr(kubernetes_api, "service_account_context", lambda directory: None)
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    def urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        if len(seen) == 1:
            return _Response(
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "brand-kit-ci-failed",
                                    "uid": "failed-uid",
                                    "creationTimestamp": "2026-09-27T12:30:00Z",
                                },
                                "status": {"phase": "Failed", "finishedAt": "2026-09-27T12:45:00Z"},
                            }
                        ],
                        "metadata": {"continue": "page-2"},
                    }
                ).encode()
            )
        raise _http_error(request.full_url, 503)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    with pytest.raises(brand_kit_ci_failure_watch.WatchPaginationError) as error:
        brand_kit_ci_failure_watch.run_watch(
            "projected-token",
            now=datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc),
            request=kubernetes_api.request_function(Path("/unused/serviceaccount")),
        )

    assert error.value.code == "page_request_failed"
    assert error.value.page == 2
    assert error.value.pages_read == 1
    assert len(seen) == 1 + http_policy.MAX_READ_ATTEMPTS
    assert len({url for url, _ in seen[1:]}) == 1
    assert all(timeout == http_policy.KUBERNETES_TIMEOUT_SECONDS for _, timeout in seen)
    assert delays == [1.0, 2.0]


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


def test_token_probe_reads_retry_with_the_publisher_api_budget(monkeypatch):
    calls = []
    responses = iter(
        [
            _http_error("https://forgejo.example/api/v1/user", 503),
            _Response(json.dumps({"login": "release-bot"}).encode()),
            _Response(b"[]"),
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
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    release_token_probe.probe_forgejo(
        "forgejo-secret",
        api_url="https://forgejo.example/api/v1",
        request=release_publish.request_json,
    )

    assert calls == [
        ("GET", http_policy.API_TIMEOUT_SECONDS),
        ("GET", http_policy.API_TIMEOUT_SECONDS),
        ("GET", http_policy.API_TIMEOUT_SECONDS),
    ]
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]


def test_token_probe_exhausted_read_fails_the_credential_and_is_capped(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append((request.method, timeout))
        raise _http_error(request.full_url, 429, retry_after=999)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(release_publish.HttpFailure) as error:
        release_token_probe.probe_forgejo(
            "forgejo-secret",
            api_url="https://forgejo.example/api/v1",
            request=release_publish.request_json,
        )

    assert error.value.status == 429
    assert calls == [("GET", http_policy.API_TIMEOUT_SECONDS)] * http_policy.MAX_READ_ATTEMPTS
    assert delays == [
        http_policy.MAX_RETRY_DELAY_SECONDS,
        http_policy.MAX_RETRY_DELAY_SECONDS,
    ]


def test_token_probe_dry_run_submission_is_a_single_attempt_without_a_workflow(
    monkeypatch,
):
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        raise _http_error(request.full_url, 503)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(release_publish.HttpFailure) as error:
        release_token_probe.probe_argo_submit(
            "argo-submit-secret",
            api_url="https://argo.example",
            request=release_publish.request_json,
        )

    request, timeout = requests[0]
    assert error.value.status == 503
    assert len(requests) == 1
    assert request.method == "POST"
    assert timeout == http_policy.API_TIMEOUT_SECONDS
    assert b'"serverDryRun": true' in request.data
    assert delays == []


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


class _SourceResponse:
    def __init__(self, status, body=b"<p>source text</p>"):
        self.status = status
        self.body = body
        self.closed = False

    def getcode(self):
        return self.status

    def read(self):
        return self.body

    def close(self):
        self.closed = True


def _platform_manifest():
    digest = check_platform_requirements._source_content_digest(b"source text")
    return {
        "platform_requirements": [
            {
                "platform": "Example",
                "source_url": "https://help.example.test/requirements",
                "last_verified": "2026-09-28",
                "source_content_sha256": digest,
            }
        ],
        "requirement_evidence": [],
    }


def test_platform_source_reads_retry_transport_failure_with_public_timeout(
    monkeypatch,
):
    calls = []
    responses = iter(
        [urllib.error.URLError("source unavailable"), _SourceResponse(200)]
    )

    def urlopen(request, timeout):
        calls.append(timeout)
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", urlopen)
    delays = []

    checks = check_platform_requirements.check_platform_requirement_sources(
        _platform_manifest(), sleep=delays.append
    )

    assert [check.status for check in checks] == [
        check_platform_requirements.REACHABILITY_PASS
    ]
    assert calls == [
        http_policy.PUBLIC_TIMEOUT_SECONDS,
        http_policy.PUBLIC_TIMEOUT_SECONDS,
    ]
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]


def test_platform_source_retry_budget_is_capped_and_fails_closed(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(timeout)
        raise _http_error(request.full_url, 429, retry_after=999)

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", urlopen)
    delays = []

    checks = check_platform_requirements.check_platform_requirement_sources(
        _platform_manifest(), sleep=delays.append
    )

    assert [check.status for check in checks] == [
        check_platform_requirements.REACHABILITY_HTTP_FAILURE
    ]
    assert len(calls) == http_policy.MAX_READ_ATTEMPTS
    assert delays == [
        http_policy.MAX_RETRY_DELAY_SECONDS,
        http_policy.MAX_RETRY_DELAY_SECONDS,
    ]


def test_platform_source_transport_exhaustion_stays_indeterminate(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(timeout)
        raise urllib.error.URLError("DNS unavailable")

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", urlopen)
    delays = []

    checks = check_platform_requirements.check_platform_requirement_sources(
        _platform_manifest(), sleep=delays.append
    )

    assert [check.status for check in checks] == [
        check_platform_requirements.REACHABILITY_NETWORK_FAILURE
    ]
    assert len(calls) == http_policy.MAX_READ_ATTEMPTS
    assert delays == [
        http_policy.RETRY_BACKOFF_SECONDS,
        http_policy.RETRY_BACKOFF_SECONDS * 2,
    ]


def test_platform_source_terminal_4xx_is_never_retried(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(timeout)
        raise _http_error(request.full_url, 404)

    monkeypatch.setattr(check_platform_requirements.urllib.request, "urlopen", urlopen)
    delays = []

    checks = check_platform_requirements.check_platform_requirement_sources(
        _platform_manifest(), sleep=delays.append
    )

    assert checks[0].status == check_platform_requirements.REACHABILITY_HTTP_FAILURE
    assert checks[0].detail == "HTTP 404"
    assert calls == [http_policy.PUBLIC_TIMEOUT_SECONDS]
    assert delays == []


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
    assert (
        f"| Failure watcher's in-cluster Kubernetes API Workflow list | "
        f"{int(http_policy.KUBERNETES_TIMEOUT_SECONDS)} seconds | "
        "Up to three total attempts per page |"
    ) in note
    assert "same page URL and `metadata.continue` input" in note
    assert "UTC cutoff" in note
    assert "no partial workflow set" in note
    assert (
        "| Mirror-health git clone, `ls-remote`, and ref fetch | "
        "120 seconds per git operation | One attempt; no automatic retry |"
    ) in note
    assert "`indeterminate`, not `stale`" in note
    assert (
        "| Release-token probe's Forgejo self-lookup, release list, and Argo "
        f"workflow list | {int(http_policy.API_TIMEOUT_SECONDS)} seconds | "
        "Up to three total attempts | Argo dry-run submission check is one attempt |"
    ) in note
    assert (
        "| Platform-requirement source-health HTTPS fetches | "
        f"{int(http_policy.PUBLIC_TIMEOUT_SECONDS)} seconds | "
        "Up to three total attempts | Not applicable; these are reads |"
    ) in note
    prose = " ".join(note.split())
    assert "`serverDryRun: true`" in prose
    assert "creates no Workflow and is not a mutation" in prose
    assert "it never retries an ambiguous dry-run submission" in prose
    assert "The check performs no mutations" in prose
    assert "`http_failure` and exits `1`" in prose
    assert "`network_failure`, making the report `indeterminate` with exit code `2`" in prose
    assert "tools/release_token_probe.py" in prose
    assert "tools/check_platform_requirements.py" in prose


CONSUMER_SUBMITTER_COMMIT = "a" * 40
CONSUMER_SUBMITTER_WORKFLOW = f"brand-kit-consumer-drift-{CONSUMER_SUBMITTER_COMMIT}"
CONSUMER_FORGEJO_RELEASE_URL = (
    "https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases/tags/v1.1.0"
)
CONSUMER_ARGO_BASE_URL = "https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows"
CONSUMER_ARGO_LIST_URL = (
    f"{CONSUMER_ARGO_BASE_URL}?labelSelector=workflows.argoproj.io%2Fworkflow-template%3D"
    "brand-kit-consumer-drift&limit=1000"
)
CONSUMER_ARGO_EXACT_URL = f"{CONSUMER_ARGO_BASE_URL}/{CONSUMER_SUBMITTER_WORKFLOW}"


def _published_release_record():
    return {
        "tag_name": "v1.1.0",
        "draft": False,
        "prerelease": False,
        "target_commitish": CONSUMER_SUBMITTER_COMMIT,
        "published_at": "2026-09-27T00:00:00Z",
    }


def _gate_submitter_git_steps(monkeypatch):
    """Keep the submitter's local git gates out of the HTTP contract tests."""
    monkeypatch.setattr(
        release_publish,
        "validate_remote_roles",
        lambda *args, **kwargs: (
            release_publish.DEFAULT_CANONICAL_REPOSITORY,
            release_publish.DEFAULT_MIRROR_REPOSITORY,
        ),
    )
    monkeypatch.setattr(release_publish, "wait_for_mirror", lambda *args, **kwargs: None)


def _submitter_urlopen(monkeypatch, calls, responses):
    def urlopen(request, timeout):
        calls.append((request, timeout))
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)


def _run_consumer_submitter(**overrides):
    arguments = {
        "forgejo_token": "forgejo-submitter-token",
        "argo_token": "argo-submit-token",
        "argo_read_token": "argo-read-token",
    }
    arguments.update(overrides)
    return consumer_drift_submit.submit_consumer_drift("v1.1.0", **arguments)


def test_submitter_reads_retry_with_the_publisher_budget_and_posts_once(monkeypatch):
    _gate_submitter_git_steps(monkeypatch)
    calls = []
    responses = iter(
        [
            _http_error(CONSUMER_FORGEJO_RELEASE_URL, 503),
            _Response(json.dumps(_published_release_record()).encode()),
            _http_error(CONSUMER_ARGO_EXACT_URL, 404),
            _Response(json.dumps({"items": []}).encode()),
            _Response(
                json.dumps({"metadata": {"name": CONSUMER_SUBMITTER_WORKFLOW}}).encode()
            ),
        ]
    )
    _submitter_urlopen(monkeypatch, calls, responses)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    result = _run_consumer_submitter()

    assert result["metadata"]["name"] == CONSUMER_SUBMITTER_WORKFLOW
    assert [
        (request.get_full_url(), request.get_method(), timeout, request.get_header("Authorization"))
        for request, timeout in calls
    ] == [
        (
            CONSUMER_FORGEJO_RELEASE_URL,
            "GET",
            http_policy.API_TIMEOUT_SECONDS,
            "token forgejo-submitter-token",
        ),
        (
            CONSUMER_FORGEJO_RELEASE_URL,
            "GET",
            http_policy.API_TIMEOUT_SECONDS,
            "token forgejo-submitter-token",
        ),
        (
            CONSUMER_ARGO_EXACT_URL,
            "GET",
            http_policy.API_TIMEOUT_SECONDS,
            "Bearer argo-read-token",
        ),
        (
            CONSUMER_ARGO_LIST_URL,
            "GET",
            http_policy.API_TIMEOUT_SECONDS,
            "Bearer argo-read-token",
        ),
        (
            CONSUMER_ARGO_BASE_URL,
            "POST",
            http_policy.API_TIMEOUT_SECONDS,
            "Bearer argo-submit-token",
        ),
    ]
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]
    post = calls[-1][0]
    assert json.loads(post.data)["workflow"]["metadata"]["name"] == (
        CONSUMER_SUBMITTER_WORKFLOW
    )


def test_submitter_exhausted_release_read_blocks_the_argo_gate(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append((request.get_full_url(), request.get_method(), timeout))
        raise _http_error(request.get_full_url(), 429, retry_after=999)

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(
        release_publish.ReleaseError, match="cannot read Forgejo release"
    ):
        _run_consumer_submitter()

    assert calls == [
        (CONSUMER_FORGEJO_RELEASE_URL, "GET", http_policy.API_TIMEOUT_SECONDS)
    ] * http_policy.MAX_READ_ATTEMPTS
    assert delays == [
        http_policy.MAX_RETRY_DELAY_SECONDS,
        http_policy.MAX_RETRY_DELAY_SECONDS,
    ]


def test_submitter_exhausted_reconciliation_read_never_submits(monkeypatch):
    _gate_submitter_git_steps(monkeypatch)
    calls = []
    responses = iter(
        [_Response(json.dumps(_published_release_record()).encode())]
        + [
            _http_error(CONSUMER_ARGO_EXACT_URL, 503)
            for _ in range(http_policy.MAX_READ_ATTEMPTS)
        ]
    )
    _submitter_urlopen(monkeypatch, calls, responses)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(
        release_publish.ReleaseError, match="cannot inspect Argo workflow"
    ):
        _run_consumer_submitter()

    assert [request.get_full_url() for request, _ in calls] == [
        CONSUMER_FORGEJO_RELEASE_URL
    ] + [CONSUMER_ARGO_EXACT_URL] * http_policy.MAX_READ_ATTEMPTS
    assert all(
        request.get_method() == "GET" and timeout == http_policy.API_TIMEOUT_SECONDS
        for request, timeout in calls
    )
    assert all(
        request.get_header("Authorization") == "Bearer argo-read-token"
        for request, _ in calls[1:]
    )
    assert delays == [
        http_policy.RETRY_BACKOFF_SECONDS,
        http_policy.RETRY_BACKOFF_SECONDS * 2,
    ]


def test_submitter_exhausted_workflow_list_read_never_submits(monkeypatch):
    _gate_submitter_git_steps(monkeypatch)
    calls = []
    responses = iter(
        [
            _Response(json.dumps(_published_release_record()).encode()),
            _http_error(CONSUMER_ARGO_EXACT_URL, 404),
        ]
        + [
            _http_error(CONSUMER_ARGO_LIST_URL, 429, retry_after=999)
            for _ in range(http_policy.MAX_READ_ATTEMPTS)
        ]
    )
    _submitter_urlopen(monkeypatch, calls, responses)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(
        release_publish.ReleaseError, match="cannot inspect existing consumer-drift workflows"
    ):
        _run_consumer_submitter()

    assert [request.get_full_url() for request, _ in calls] == [
        CONSUMER_FORGEJO_RELEASE_URL,
        CONSUMER_ARGO_EXACT_URL,
    ] + [CONSUMER_ARGO_LIST_URL] * http_policy.MAX_READ_ATTEMPTS
    assert all(
        request.get_method() == "GET" and timeout == http_policy.API_TIMEOUT_SECONDS
        for request, timeout in calls
    )
    assert all(
        request.get_header("Authorization") == "Bearer argo-read-token"
        for request, _ in calls[1:]
    )
    assert delays == [http_policy.MAX_RETRY_DELAY_SECONDS] * (
        http_policy.MAX_READ_ATTEMPTS - 1
    )


def test_submitter_attachment_fetches_use_the_public_reader_budget(monkeypatch):
    names = release_publish.release_payload_names("v1.1.0")
    attachment_urls = {
        name: (
            "https://git.ardenone.com/jedarden/brand-kit/releases/download/"
            f"v1.1.0/{name}"
        )
        for name in names.values()
    }
    record = _published_release_record()
    record["assets"] = [
        {"name": name, "browser_download_url": url}
        for name, url in attachment_urls.items()
    ]
    calls = []
    responses = iter(
        [
            _Response(json.dumps(record).encode()),
            _http_error(attachment_urls[names["manifest"]], 503),
            _Response(b""),
            _Response(b""),
            _Response(b""),
        ]
    )
    _submitter_urlopen(monkeypatch, calls, responses)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    with pytest.raises(
        release_publish.ReleaseError, match="checksum attachment"
    ):
        _run_consumer_submitter()

    assert [(request.get_full_url(), timeout) for request, timeout in calls[1:]] == [
        (
            attachment_urls[names["manifest"]],
            http_policy.PUBLIC_TIMEOUT_SECONDS,
        ),
        (
            attachment_urls[names["manifest"]],
            http_policy.PUBLIC_TIMEOUT_SECONDS,
        ),
        (
            attachment_urls[names["checksums"]],
            http_policy.PUBLIC_TIMEOUT_SECONDS,
        ),
        (
            attachment_urls[names["archive"]],
            http_policy.PUBLIC_TIMEOUT_SECONDS,
        ),
    ]
    assert delays == [http_policy.RETRY_BACKOFF_SECONDS]
    assert all("argo-ci" not in request.get_full_url() for request, _ in calls)


def test_submitter_post_is_single_attempt_and_reconciles_without_replay(monkeypatch):
    _gate_submitter_git_steps(monkeypatch)
    accepted = {
        "metadata": {
            "name": CONSUMER_SUBMITTER_WORKFLOW,
            "labels": {
                "workflows.argoproj.io/workflow-template": "brand-kit-consumer-drift",
                "brand-kit.ardenone.com/release-tag": "v1.1.0",
                "brand-kit.ardenone.com/release-commit": CONSUMER_SUBMITTER_COMMIT,
            },
        }
    }
    calls = []
    responses = iter(
        [
            _Response(json.dumps(_published_release_record()).encode()),
            _http_error(CONSUMER_ARGO_EXACT_URL, 404),
            _Response(json.dumps({"items": []}).encode()),
            _http_error(CONSUMER_ARGO_BASE_URL, 503),
            _Response(json.dumps(accepted).encode()),
        ]
    )
    _submitter_urlopen(monkeypatch, calls, responses)
    delays = []
    monkeypatch.setattr(release_publish.time, "sleep", delays.append)

    result = _run_consumer_submitter()

    assert result == accepted
    assert [request.get_method() for request, _ in calls] == [
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
    ]
    assert sum(request.get_method() == "POST" for request, _ in calls) == 1
    assert all(timeout == http_policy.API_TIMEOUT_SECONDS for _, timeout in calls)
    assert calls[-1][0].get_header("Authorization") == "Bearer argo-read-token"
    assert calls[-2][0].get_header("Authorization") == "Bearer argo-submit-token"
    # One POST attempt with no retry delay anywhere: a lost submission
    # response is reconciled by reads, never replayed.
    assert delays == []


def test_ambiguous_argo_submission_recovery_is_documented():
    note = Path("docs/notes/external-api-resilience.md").read_text(encoding="utf-8")
    prose = " ".join(note.split())

    assert (
        "| Consumer submitter's Forgejo release read | 20 seconds | "
        "Up to three total attempts | Not applicable; this is a read |"
    ) in note
    assert (
        "| Consumer submitter's Forgejo release-attachment fetches | 15 seconds | "
        "Up to three total attempts | Not applicable; these are reads |"
    ) in note
    assert (
        "| Consumer submitter's read-only Argo reconciliation GETs | 20 seconds | "
        "Up to three total attempts | Not applicable; these are reads |"
    ) in note
    assert (
        "| Consumer submitter's Argo submission POST | 20 seconds | "
        "One attempt; no automatic retry | One attempt |"
    ) in note
    assert "Recovering an ambiguous Argo submission" in prose
    assert "brand-kit-consumer-drift-<full release commit>" in prose
    assert (
        "GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/"
        "brand-kit-consumer-drift-<commit>" in prose
    )
    assert "A truncated list (`metadata.continue`)" in prose
    assert "without a second `POST`" in prose
    assert "never re-sends it" in prose
    assert "the Argo state is confirmed absent" in prose
    assert "A failed submit command is never evidence that nothing was submitted" in prose
    assert "Record the observation" in prose
    assert "follow the same read policy at the 20-second timeout" in prose
    assert "one attempt with no automatic retry, because it creates a Workflow" in prose
    assert "tools/consumer_drift_submit.py" in prose
