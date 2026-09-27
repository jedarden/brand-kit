# Release-token provisioning

This is the credential contract for the two release commands in this
repository. Token values live only in the credential store and the short-lived
process environment; they are never repository data, workflow parameters, URL
userinfo, command arguments, or log output.

## Credential registry

The canonical source is the `rs-manager` OpenBao instance, which owns the
`secret/rs-manager/*` prefix. Provision each value at its own path and keep the
property name `token`:

| Environment variable | OpenBao path | Use and minimum scope |
| --- | --- | --- |
| `FORGEJO_TOKEN` | `secret/rs-manager/brand-kit/release/forgejo` | The Forgejo API for `jedarden/brand-kit`: read the release/tag record and create or publish that repository's release. No organization, administration, mirror-push, or unrelated-repository permission. |
| `ARGO_TOKEN` | `secret/rs-manager/brand-kit/release/argo-read` | Read-only `GET` attestation of the named `brand-kit-ci` Workflow run in namespace `argo-workflows`. No workflow submission, mutation, deletion, or access to another namespace. |
| `ARGO_SUBMIT_TOKEN` | `secret/rs-manager/brand-kit/release/argo-submit` | Submit the `brand-kit-consumer-drift` WorkflowTemplate in namespace `argo-workflows`. It is not a substitute for `ARGO_TOKEN` and must not have workflow delete, suspend, or administrative permission. |

These are provisioning paths, not values to copy into this repository. If a
path is absent or its policy is broader than the table, stop and have the
operator correct the OpenBao/provider policy. Do not reuse the broad CI token
or the read-only consumer-audit token for release publication.

The existing Argo consumer-audit `ExternalSecret` remains a separate,
read-only workload credential. It is not an operator release credential and
does not authorize Forgejo release publication or Argo workflow submission.

## Injection

Load credentials from OpenBao directly into shell variables immediately before
the one operation that needs them. From the repository root, source the tested
[`tools/load_release_tokens.sh`](../../tools/load_release_tokens.sh) helper.
It passes only the path as an argument; the secret value is captured by the
shell and is never printed or placed in an argument. Sourcing the helper also
installs its exit and signal traps to unset all three release-token variables:

```bash
source tools/load_release_tokens.sh

# For tools/release_publish.py, load only these two variables in its shell:
load_release_token FORGEJO_TOKEN \
  secret/rs-manager/brand-kit/release/forgejo
load_release_token ARGO_TOKEN \
  secret/rs-manager/brand-kit/release/argo-read
# In a separate handoff shell, load ARGO_SUBMIT_TOKEN instead of ARGO_TOKEN:
# load_release_token ARGO_SUBMIT_TOKEN \
#   secret/rs-manager/brand-kit/release/argo-submit
```

Use only the variables required by the command:

- `tools/release_publish.py` requires `FORGEJO_TOKEN` and `ARGO_TOKEN`,
  including for `--verify-only`. `ARGO_TOKEN` is sent only as an HTTP Bearer
  header to the read-only Argo endpoint; `FORGEJO_TOKEN` is sent only as the
  Forgejo API authorization header.
- `tools/consumer_drift_submit.py` requires `FORGEJO_TOKEN` and
  `ARGO_SUBMIT_TOKEN`. It reads Forgejo before the mirror gate and sends the
  submit token only as an HTTP Bearer header for the Argo submission.

There are deliberately no token CLI options. `FORGEJO_API_TOKEN` and
`ARGO_API_TOKEN` are not accepted aliases for these release operations, and
`ARGO_TOKEN` is never promoted to `ARGO_SUBMIT_TOKEN`. This prevents a
read-only or wrongly scoped credential from being silently used for a write.

## Provisioning and rotation

An operator provisions or rotates the provider credential, then writes the new
value to the owning OpenBao instance through stdin or an `@file`; never put the
value in an argument, shell history, workflow parameter, or log. For a value
already held in a mode-600 file, the write shape is:

```bash
bao-as rs-manager-provision bao kv put -cas=<current-version> \
  secret/rs-manager/brand-kit/release/<credential> token=- < credential-file
```

The exact provider-specific token creation/revocation command is intentionally
outside this repository. Use a new credential with the minimum scope in the
registry, write a new OpenBao version, and verify only metadata (version,
timestamp, and property names). Never read the value back to verify a write.

Rotation order is:

1. Create the replacement credential with the same narrow scope.
2. Write it as a new version at the same path using CAS.
3. Start a fresh release process so it reads the new version, run the
   scheduled probe (or `tools/release_token_probe.py` directly), and run the
   read-only publisher gate. All three probe checks and a `READY` result are
   success signals; token values themselves must not be recorded.
4. Revoke the old provider credential only after the fresh process succeeds,
   then let the old process exit and clear its environment.

If the replacement fails, leave the last known-good provider credential active,
repair the OpenBao/provider entry, and retry. Do not delete the OpenBao path or
revoke the old credential first. Workload `ExternalSecret` refresh is separate
from this operator flow; a consumer-audit rotation must wait for its
`SecretSynced=True` status before treating a new workflow run as covered.

## Scheduled validity probe

The one-time path and policy check does not detect a provider-side expiry or a
later scope change. The repository therefore ships the daily
`brand-kit-release-token-probe` CronWorkflow, whose source is
[`automation/brand-kit-release-token-probe-cronworkflow.yml`](../../automation/brand-kit-release-token-probe-cronworkflow.yml)
and whose reusable template is
[`automation/brand-kit-release-token-probe-workflowtemplate.yml`](../../automation/brand-kit-release-token-probe-workflowtemplate.yml).
It runs at 06:07 UTC, before the 06:17 UTC consumer-drift fallback, and reads
the three values from the Kubernetes Secret `brand-kit-release-tokens`. That
Secret must be materialized by the deployment's ExternalSecret from the three
OpenBao paths above, with keys named exactly `FORGEJO_TOKEN`, `ARGO_TOKEN`, and
`ARGO_SUBMIT_TOKEN`; it must not be committed to this repository.

The probe runs `tools/release_token_probe.py` and records only credential names,
pass/fail status, and sanitized provider errors in a durable report. It checks:

- `FORGEJO_TOKEN`: Forgejo `GET /user` self-lookup and the repository release
  list endpoint used by the publisher.
- `ARGO_TOKEN`: a read-only list request for the `brand-kit-ci` workflow runs
  in namespace `argo-workflows`.
- `ARGO_SUBMIT_TOKEN`: the exact `brand-kit-consumer-drift` WorkflowTemplate
  submit endpoint with Argo's `serverDryRun` flag. Argo validates the token's
  submit authorization and the template without creating a Workflow.

Forgejo does not expose a non-mutating authorization check for the release
creation verb, so the probe intentionally never attempts a release write. The
publisher's `--verify-only` gate remains the required read-only pre-release
check, and a real publication remains the only controlled check of the
Forgejo create/publish permission. A probe failure still blocks follow-up
release work until the credential or provider policy is repaired.

Any non-success probe run invokes the same in-cluster Alertmanager endpoint and
owner notification path as `BrandKitConsumerDrift`, and retains its sanitized
report at the alert's artifact URL. Follow the rotation order above: provision
the replacement, run the probe until all three checks pass, run the publisher's
read-only gate, then revoke the old credential. Do not put a token in a report,
workflow parameter, URL, command argument, or log.

### Owner alert and recovery

The probe WorkflowTemplate writes a sanitized fallback report before installing
packages or cloning the repository. Its three Kubernetes Secret references are
optional so a missing `brand-kit-release-tokens` Secret or key is reported as a
failed credential check rather than stopping pod admission. Argo retains the
report at
`https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-release-token-probe/v1/<workflow-uid>/report.json`
with `artifactGC: Never`. The workflow exit handler sends
`BrandKitReleaseTokenProbe` to the iad-ci Alertmanager only when the workflow
is not `Succeeded`; the alert contains the fixed report URL, workflow status,
and owner labels, never report contents or credential values.

When the owner receives the alert:

1. Open the durable report and identify the failed credential by name and its
   sanitized provider status. If the report says the probe did not start,
   repair the workflow, Secret materialization, or dependency/network failure
   named by the workflow status, then rerun the same CronWorkflow.
2. For a missing credential, repair the ExternalSecret so all three keys are
   present. For `401`/expired credentials, provision a replacement at the same
   OpenBao path. For `403`/scope failures, restore the documented minimum
   policy or provision a new credential; never widen scope to make the probe
   pass. Keep the last known-good credential active until the replacement is
   validated.
3. Rerun the probe until all three checks pass, run the publisher's read-only
   gate, and only then revoke the replaced provider credential or resume the
   consumer-drift handoff. A failed or indeterminate rerun keeps release and
   consumer operations blocked.

## Failure and observability rules

- Missing or blank required variables fail before the network operation. The
  publisher exits non-zero without a Forgejo write; the submitter exits
  non-zero without an Argo submission.
- Forgejo or Argo `401`/`403`, an invalid token, an expired token, or a policy
  that lacks the required operation is an error. No fallback credential or
  broader scope is attempted, and release/mirror gates remain closed.
- A failed Argo attestation always blocks Forgejo publication. A failed
  Forgejo release read or mirror gate always blocks consumer submission.
- A failed scheduled release-token probe is an owner-facing follow-up event;
  repair or rotate the affected credential and rerun the probe before a
  release or consumer-drift handoff.
- HTTP error bodies and transport errors are sanitized before reaching the
  CLI's stderr. Token values are not logged, printed, embedded in URLs, or
  passed through `argv`; diagnostics contain only the service, status, and
  safe failure context.
- A transient failure is retried with the same release tag and commit after
  the credential or service is repaired. Never publish from a branch, retag an
  existing release, or mark an unauthorized attempt successful.

The tests in `test_release_publish.py` and `test_consumer_drift_submit.py`
exercise the environment-only contract, redacted error behavior, required
credentials, and no-submit/no-publish gates for denied or missing credentials.
