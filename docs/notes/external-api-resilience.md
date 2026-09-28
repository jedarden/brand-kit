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

## Recovering an ambiguous Garage deletion

If a Garage `DeleteObjects` request times out, returns a transport error, or
returns an unusable response, stop the cleanup pass. The request may have
deleted none, some, or all of its keys. Do not resend the failed request body,
and do not blindly restart the old deletion batch.

Reconcile the current object state with a read-only Garage credential:

1. Identify the exact bucket and key prefix used by the failed cleanup. The
   built-in artifact prefixes are:

   | Artifact | Bucket | Exact key prefix |
   | --- | --- | --- |
   | CI failure-watch reports | `needle-ci-artifacts` | `failures/brand-kit-ci-failure-watch/v1/` |
   | CI attestations | `needle-ci-artifacts` | `attestations/brand-kit-ci/v1/` |
   | Consumer-drift reports | `needle-ci-artifacts` | `failures/brand-kit-consumer-drift/v1/` |

   For an invocation with a configured `--prefix`, use that exact value. Do
   not widen the listing to the bucket root or a shared parent such as
   `failures/`.
2. Complete a read-only `ListObjectsV2` for that bucket and exact prefix. Read
   every page and confirm the listing finished successfully. If any page
   fails, the response is truncated without a continuation token, or the
   result otherwise cannot be trusted, stop without issuing another delete.
3. Treat the complete listing as the current state: keys present in it remain
   in Garage; keys absent from it are not present at observation time. Reload
   and validate `retention-holds.json`, and review each remaining key against
   the cleanup's retention cutoff and every release-evidence hold before
   deciding it is still eligible. A listed key is not by itself permission to
   delete it.
4. Record the observation in the incident or workflow record: UTC time,
   workflow/run identifier, endpoint, bucket, exact prefix, original error,
   whether every listing page completed, the remaining keys, which remaining
   keys still meet the retention rule, and the follow-up action. Do not include
   credentials or secret values.
5. Only after the complete listing and eligibility review may cleanup continue.
   Build a fresh deletion set from keys that are still present and still
   eligible under the current retention and hold checks. The shared recovery
   check reloads active holds and returns eligible survivors without issuing a
   delete. Never replay the old multi-delete payload or delete every key
   returned by the listing. If a key may have been rewritten or its identity
   is uncertain, leave it in place and escalate for review.

An incomplete or failed inspection remains indeterminate: preserve the record,
leave deletion stopped, and retry the read-only inspection later. This
procedure is an operator gate; the client continues to make only one
`DeleteObjects` attempt per request.

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
the failure watcher and liveness watchdog, 1,800 seconds for consumer drift).
Each template carries matching deadline annotations so Argo inspection shows
the same contract as this note. These annotations and the active deadline must
leave room for the bounded request and retry work:

| WorkflowTemplate | Active deadline | Minimum request and retry budget |
| --- | ---: | ---: |
| `brand-kit-ci-failure-watch` | 600 seconds | 100 seconds |
| `brand-kit-workflow-liveness` | 600 seconds | 70 seconds |
| `brand-kit-consumer-drift` | 1,800 seconds | 670 seconds |

The budget is the longest applicable read budget (`per-attempt timeout × 3`
plus two retry delays capped at five seconds), together with consumer drift's
600-second mirror-polling allowance. This makes the workflow ceiling an outer
limit for the request and retry budget, not a reason to omit per-request
timeouts. `test_argo_workflow_deadlines.py` checks the manifest values, both
annotations, this table, and the computed budget against the executable retry
policy and consumer-drift polling loop.

The executable policy is shared by `tools/release_publish.py`,
`tools/consumer_drift.py`, `tools/consumer_sync.py`, and
`tools/prune_ci_failure_watch_reports.py`. The contract tests in
`test_external_api_resilience.py` cover successful read retry, capped `429`,
single-attempt mutation behavior, public endpoint timeouts, Garage read retry,
and Garage delete non-retry.
