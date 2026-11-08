# External API timeout and retry contract

The failure watcher, liveness check, release gate, consumer audit, mirror-health,
release-token probe, release handoff consumer submitter, platform-requirement
source health, and Garage retention jobs use bounded external requests. A request error is evidence that the check
could not prove its claim; it is never converted into a current, fresh, or
successful result.

## Timeouts and retries

The timeout applies to each network attempt:

| Endpoint | Per-attempt timeout | Read behavior | Mutation behavior |
| --- | ---: | --- | --- |
| Release publisher's Argo and Forgejo JSON APIs | 20 seconds | Up to three total attempts | One attempt |
| Failure watcher's in-cluster Kubernetes API Workflow list | 20 seconds | Up to three total attempts per page | Not applicable; this is a read |
| Consumer audit/sync Forgejo lookups, public profile, and media URLs | 15 seconds | Up to three total attempts | Not applicable; these are reads |
| Garage S3 listing and artifact reads | 30 seconds | Up to three total attempts | S3 multi-delete is one attempt |
| Mirror-health git clone, `ls-remote`, and ref fetch | 120 seconds per git operation | One attempt; no automatic retry | Not applicable; all remote operations are reads |
| Release-token probe's Forgejo self-lookup, release list, and Argo workflow list | 20 seconds | Up to three total attempts | Argo dry-run submission check is one attempt |
| Consumer submitter's Forgejo release read | 20 seconds | Up to three total attempts | Not applicable; this is a read |
| Consumer submitter's Forgejo release-attachment fetches | 15 seconds | Up to three total attempts | Not applicable; these are reads |
| Consumer submitter's read-only Argo reconciliation GETs | 20 seconds | Up to three total attempts | Not applicable; these are reads |
| Consumer submitter's Argo submission POST | 20 seconds | One attempt; no automatic retry | One attempt |
| Platform-requirement source-health HTTPS fetches | 15 seconds | Up to three total attempts | Not applicable; these are reads |

Only `GET` and `HEAD` requests use the read retry policy. A connection, DNS,
TLS, socket-timeout, or other transport error, plus HTTP `429` or `5xx`, may be
retried. The backoff is one second and then two seconds. A numeric
`Retry-After` value is honored but capped at five seconds. Thus a single read
has a finite attempt and delay budget; it cannot wait forever on a server or an
unbounded rate-limit hint. Other HTTP errors, including `4xx` responses other
than `429`, are terminal.

The watcher applies that read policy to each Kubernetes Workflow-list `GET`.
A retry repeats the same page URL and `metadata.continue` input; only a valid
response advances the continuation token. The watcher captures its UTC cutoff
once before listing and applies it only after every page completes. If a page
exhausts retries, returns an expired continuation token, or cannot be validated,
the list is incomplete and no partial workflow set becomes a `pass` or `fail`
result. The WorkflowTemplate's 600-second active deadline is the outer limit
across all pages.

Mirror health runs its checker from a GitHub bootstrap clone bounded by a
120-second shell timeout (with five seconds to kill after termination). The
checker bounds each git subprocess, including Forgejo and GitHub `ls-remote`
and `fetch`, to 120 seconds of wall-clock time. Forgejo reads use the
`FORGEJO_TOKEN` credential helper; the credential is never placed in a URL.
Git operations have no automatic retry. A confirmed `missing`, `stale`, or
`divergent` ref requires completed remote reads and, where needed, a completed
ancestry check. A timeout, connection or authentication failure, or incomplete
fetch makes the check `indeterminate`, not `stale`; the workflow fails and
retains an indeterminate report. Its 1,800-second active deadline also caps
the total scheduled check.

The release-token probe reuses the publisher's bounded JSON client for three
reads: the Forgejo self-lookup, the Forgejo release-list read, and the Argo
workflow-list read. Each follows the read retry policy at the 20-second
per-attempt timeout. Its fourth request checks submit authorization by
`POST`ing an Argo submission with `serverDryRun: true`: Argo validates the
request server-side and discards it, so it creates no Workflow and is not a
mutation — but it is still a `POST`, so it makes exactly one attempt under
the no-retry rule. An exhausted read budget, a terminal `4xx`, or an
inconclusive response fails that credential's check and the probe exits
non-zero; it never reports `pass` for a credential it could not fully verify,
and it never retries an ambiguous dry-run submission. The probe's
600-second active deadline is the outer limit.

The platform-requirement source-health check fetches each manifest source URL
and evidence citation with a `GET` at the 15-second public timeout and applies
the read retry policy. Transport errors and HTTP `429` or `5xx` are retried;
other `4xx` responses are terminal immediately. An HTTP error that exhausts
the retry budget is `http_failure` and exits `1`; a transport error that
exhausts it is `network_failure`, making the report `indeterminate` with exit
code `2`. Neither outcome can become a pass. A completed fetch whose content
no longer matches its pin (`content_changed`) is a conclusive failure that no
retry can overturn. The check performs no mutations. Its 900-second active
deadline is the outer limit.

The release handoff's consumer submitter (`tools/consumer_drift_submit.py`) is
a local operator process with no workflow deadline; between its Forgejo reads
and its Argo requests sits the bounded mirror-propagation gate, a git polling
loop (300 seconds by default at 5-second intervals) that makes no HTTP
request. The submitter reuses the publisher's bounded JSON client for API
requests and its bounded byte reader for release attachments. Its Forgejo
release read and, when the record carries attachments,
each attachment fetch happen before the mirror gate and follow the read retry
policy at their table timeouts. After the gate passes, the reconciliation
reads made with `ARGO_TOKEN` — the exact
`brand-kit-consumer-drift-<commit>` workflow GET and the template-scoped
workflow-list GET — follow the same read policy at the 20-second timeout. The
submission `POST` made with `ARGO_SUBMIT_TOKEN` is one attempt with no
automatic retry, because it creates a Workflow: a lost or unusable response
is never replayed. Instead the submitter reconciles once with fresh
`ARGO_TOKEN` reads — exact name first, then the template-scoped list — adopts
a found run, and fails closed with an explicit instruction to inspect the
Argo state when reconciliation finds nothing or cannot complete. No Argo
request at all is made until the Forgejo record and both git remotes are
verified.

There is no automatic retry for `POST`, `PATCH`, or S3 multi-delete. Those
operations can have committed an external mutation even when the response is
lost, so replaying them would risk a duplicate workflow, release, or deletion.
The Forgejo release publisher is safe to invoke again because it reads and
validates the existing release before creating anything, and reconciles a
`409 Conflict` by reading the named release. The consumer submitter is safe to
re-invoke under the same rule: it re-reads the published release, re-validates
the mirror gate, and reconciles existing Argo runs before it would post again.
An ambiguous Argo submission or Garage deletion must be inspected before an
operator repeats it; both recovery procedures follow.

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

## Recovering an ambiguous Argo submission

If the consumer submitter's submission `POST` to
`https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows` times out,
returns a transport error or `429`/`5xx`, or responds without the expected
deterministic workflow name, the submission outcome is unknown: the Workflow
may exist. The submitter makes exactly one `POST` and never re-sends it. It
reconciles immediately with fresh read-only `ARGO_TOKEN` requests, in this
order:

1. Read the exact deterministic workflow name
   `brand-kit-consumer-drift-<full release commit>` in namespace
   `argo-workflows`:
   `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/brand-kit-consumer-drift-<commit>`.
   A `404` proves that one name is absent. A successful response is checked
   against the release identity; another error or transport failure is
   inconclusive.
2. If the exact name is absent, read the template-scoped workflow list with
   `labelSelector=workflows.argoproj.io%2Fworkflow-template%3Dbrand-kit-consumer-drift`
   and match a run by release tag or commit from its labels, parameters, or a
   completed daily-fallback run's `selected-release-tag` /
   `selected-release-commit` outputs. A truncated list (`metadata.continue`)
   is an inspection failure, not a gap to submit into.
3. Exactly one match completes the handoff without a second `POST`. No match
   fails the handoff with an instruction not to retry until the Argo state is
   confirmed absent. More than one match fails the handoff outright.

An ambiguous outcome whose reconciliation reads also fail stays ambiguous: the
submitter exits non-zero and an operator must inspect before any resubmission.
A failed submit command is never evidence that nothing was submitted.
Inspection repeats the submitter's read-only checks, plus:

1. Record the observation: UTC time, release tag and commit, endpoint, the
   exact workflow name read, the original `POST` error, whether the exact-name
   read completed, whether the template-scoped list completed without
   truncation, the matching workflow names and their phases, and the
   follow-up action. Do not include credentials or token values.
2. Re-invoke the submitter only when the exact-name read returned `404` and a
   complete template-scoped list contains no run for that release tag or
   commit. A `409 Conflict` on a repeat `POST` means the workflow exists
   under that name: stop, and reconcile by reading instead of posting again.
3. An existing matching run of any phase satisfies the handoff. If that run
   failed, address the run itself — its logs, inputs, or record in the Argo
   UI — rather than re-`POST`ing the same release over it. The deterministic
   name makes a same-commit replay conflict, but an older generated-name run
   for the same release tag can be discovered only by the template-scoped
   list, which is why the list must complete before any repeat.

An inspection that cannot complete leaves the outcome indeterminate:
preserve the record, leave the submission stopped, and retry the read-only
inspection later.

## Fail-closed outcomes

- Failure-watcher Kubernetes API reads and workflow-liveness Garage reads that
  exhaust retries produce an incomplete or `indeterminate` report. They cannot
  produce a false `pass`, `fresh`, or successful liveness result.
- Forgejo release reads that exhaust retries block publication or consumer
  synchronization. A missing release (`404`) is not a passing audit.
- Garage listing, attestation, or retention reads that exhaust retries fail
  the operation. Retention does not delete anything after an incomplete list;
  deletion errors also abort the pass.
- Public profile-page and media reads that exhaust retries mark the consumer
  check unavailable, making the consumer-drift report `indeterminate` and its
  exit code `2`. The legacy `consumer_sync.py` helper also exits non-zero for a
  live-fetch failure; only an explicit `--offline` request skips live checks.
- Mirror-health git transport failures and timeouts produce an `indeterminate`
  report. The checker exits `2`; the bootstrap clone's prewritten result
  parameter is also `2` if setup fails. Only completed comparisons can
  establish mirror drift.
- Release-token probe reads that exhaust retries fail that credential's check
  (probe exit `1`); they cannot produce a `pass`. The dry-run submission check
  stays single-attempt and creates no Workflow.
- Consumer-submitter Forgejo and Argo reads that exhaust retries stop the
  release handoff before any submission. Its submission `POST` is one attempt;
  an ambiguous `POST` is reconciled by read-only `GET`s and never replayed
  automatically, and a reconciliation that cannot complete leaves the outcome
  ambiguous for operator inspection.
- Platform-requirement source reads that exhaust retries produce `http_failure`
  (exit `1`) for HTTP errors and `network_failure` (exit `2`, `indeterminate`)
  for transport errors. Only a completed fetch can pass or prove content
  drift; an exhausted or terminal source read is never a pass.

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

The executable HTTP policy is shared by `tools/release_publish.py`,
`tools/kubernetes_api.py`, `tools/consumer_drift.py`, `tools/consumer_sync.py`,
`tools/check_platform_requirements.py`,
`tools/prune_ci_failure_watch_reports.py`, and — through the publisher's
client — `tools/release_token_probe.py` and `tools/consumer_drift_submit.py`.
The contract tests in
`test_external_api_resilience.py` cover successful read retry, capped `429`,
single-attempt mutation behavior, public endpoint timeouts, Garage read retry,
Garage delete non-retry, Kubernetes list-page retry and failure bounds,
release-token probe read retry and its single-attempt dry-run submission,
platform-requirement source retry and failure bounds, and the consumer
submitter's bounded release and reconciliation reads, its attachment-fetch
public timeout, its single-attempt submission `POST`, and its no-replay
reconciliation.
`test_check_mirror_health.py` checks the git subprocess and bootstrap-clone
timeouts and the indeterminate outcome on transport timeouts.
`test_check_platform_requirements.py` pins the public-source timeout default
and the indeterminate exit for transport failures;
`test_release_token_probe.py` covers the probe's credential reporting and
redaction.
