# External API timeout and retry contract

The watcher, liveness check, release gate, consumer audit, and Garage retention
jobs use bounded external requests. A request error is evidence that the check
could not prove its claim; it is never converted into a current, fresh, or
successful result.

## Timeouts and retries

The timeout applies to each network attempt:

| Endpoint | Per-attempt timeout | Read behavior | Mutation behavior |
| --- | ---: | --- | --- |
| Release publisher's Argo and Forgejo JSON APIs | 20 seconds | Up to three total attempts | One attempt |
| Consumer audit/sync Forgejo lookups, public profile, and media URLs | 15 seconds | Up to three total attempts | Not applicable; these are reads |
| Garage S3 listing and artifact reads | 30 seconds | Up to three total attempts | S3 multi-delete is one attempt |

Only `GET` and `HEAD` requests use the read retry policy. A connection, DNS,
TLS, socket-timeout, or other transport error, plus HTTP `429` or `5xx`, may be
retried. The backoff is one second and then two seconds. A numeric
`Retry-After` value is honored but capped at five seconds. Thus a single read
has a finite attempt and delay budget; it cannot wait forever on a server or an
unbounded rate-limit hint. Other HTTP errors, including `4xx` responses other
than `429`, are terminal.

There is no automatic retry for `POST`, `PATCH`, or S3 multi-delete. Those
operations can have committed an external mutation even when the response is
lost, so replaying them would risk a duplicate workflow, release, or deletion.
The Forgejo release publisher is safe to invoke again because it reads and
validates the existing release before creating anything, and reconciles a
`409 Conflict` by reading the named release. An ambiguous Argo submission or
Garage deletion must be inspected before an operator repeats it.

## Fail-closed outcomes

- Argo watcher and workflow-liveness reads that exhaust retries produce an
  error or `indeterminate` report. They cannot produce a false `pass`, `fresh`,
  or successful liveness result.
- Forgejo release reads that exhaust retries block publication or consumer
  synchronization. A missing release (`404`) is not a passing audit.
- Garage listing, attestation, or retention reads that exhaust retries fail
  the operation. Retention does not delete anything after an incomplete list;
  deletion errors also abort the pass.
- Public profile-page and media reads that exhaust retries mark the consumer
  check unavailable, making the consumer-drift report `indeterminate` and its
  exit code `2`. The legacy `consumer_sync.py` helper also exits non-zero for a
  live-fetch failure; only an explicit `--offline` request skips live checks.

The Argo WorkflowTemplates retain an outer active deadline (600 seconds for
the watcher and liveness watchdog, 1,800 seconds for consumer drift). That is
the workflow ceiling, not a reason to omit the per-request timeout above.

The executable policy is shared by `tools/release_publish.py`,
`tools/consumer_drift.py`, `tools/consumer_sync.py`, and
`tools/prune_ci_failure_watch_reports.py`. The contract tests in
`test_external_api_resilience.py` cover successful read retry, capped `429`,
single-attempt mutation behavior, public endpoint timeouts, Garage read retry,
and Garage delete non-retry.
