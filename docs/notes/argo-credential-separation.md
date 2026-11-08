# Argo credential separation

This repository has three different credential roles, plus the read-only Forgejo repo token shared by the consumer audit and mirror-health workloads. The variable name
`ARGO_TOKEN` is reserved for the release operator's read-only attestation; it
is not the credential injected into the scheduled watcher workloads.

## Role map

| Role | Kubernetes resource and namespace | Service account | Environment/key | Endpoint and allowed operation |
| --- | --- | --- | --- | --- |
| Failure watcher workload | No Argo credential. The pod's own projected ServiceAccount token (`/var/run/secrets/kubernetes.io/serviceaccount/token`, verified against the `ca.crt` beside it) authorizes it to the in-cluster Kubernetes API. Garage report access uses Secrets `needle-ci-artifact-reader` / `needle-ci-artifact-publisher`. | `brand-kit-ci-failure-watch` | None for Argo (`tools/kubernetes_api.py` reads the mounted token file) | `GET https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argo-workflows/workflows?labelSelector=workflows.argoproj.io/workflow-template=brand-kit-ci&limit=100` (plus `continue` pages). Reports link each failed run as `https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<name>` for a human with an SSO session; the watcher never calls argo-server. No `POST`, `PUT`, `PATCH`, or `DELETE`. |
| Liveness workload | No Argo or Kubernetes API credential. Secret `needle-ci-artifact-reader` (Garage read-only) via `S3_READER_ACCESS_KEY` / `S3_READER_SECRET_KEY`; `needle-ci-artifact-publisher` only for Argo's own report upload. | `brand-kit-workflow-liveness` | `S3_READER_*` from Secret `needle-ci-artifact-reader` | Garage `ListObjectsV2` on each target's `failures/<target>/<version>/` prefix and `GET` of the newest `report.json` objects in the freshness window. No `POST`, `PUT`, `PATCH`, or `DELETE`. |
| Consumer audit workload | ExternalSecret `brand-kit-consumer-drift` in `argo-workflows`, targeting Secret `brand-kit-consumer-drift` in `argo-workflows`. Its source is `rs-manager/iad-ci/forgejo/repo-readonly`, property `token`. | `brand-kit-consumer-drift` | `FORGEJO_TOKEN` from Secret key `token` | `GET https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1` for release metadata. The audit has no Argo credential and no write-capable Git operation. |
| Mirror-health workload | Secret `brand-kit-consumer-drift` in `argo-workflows` (the same read-only `rs-manager/iad-ci/forgejo/repo-readonly` repo token as the consumer audit; no new credential). Added 2026-10-10 because git.ardenone.com requires sign-in for every git read. | `argo-workflow` | `FORGEJO_TOKEN` from Secret key `token`, offered to git only through a `credential.https://git.ardenone.com.helper` so it never reaches GitHub, a URL, or argv | `git clone`/`ls-remote`/`fetch` of `https://git.ardenone.com/jedarden/brand-kit.git`. No Argo credential, no push, no Forgejo API write. |
| Release publisher operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_TOKEN` from OpenBao path `secret/rs-manager/brand-kit/release/argo-read` for the local release process. | Not applicable (local operator process) | `ARGO_TOKEN` | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<brand-kit-ci-run>` for the read-only release attestation and the consumer submitter's duplicate reconciliation reads. |
| Consumer submitter operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_TOKEN` and `ARGO_SUBMIT_TOKEN` from their separate OpenBao paths `secret/rs-manager/brand-kit/release/argo-read` and `secret/rs-manager/brand-kit/release/argo-submit`. | Not applicable (local operator process) | `ARGO_TOKEN` for reconciliation reads; `ARGO_SUBMIT_TOKEN` for the one submission POST | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<name>` and the template-scoped list use `ARGO_TOKEN`; the single `POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows` uses `ARGO_SUBMIT_TOKEN` to submit only `brand-kit-consumer-drift`. It must not delete, suspend, update, or administer workflows. |
| Brand-kit CI submitter operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_SUBMIT_TOKEN` from the same OpenBao path `secret/rs-manager/brand-kit/release/argo-submit` as the consumer submitter (one shared submit credential, added 2026-11-08 for this role, brandkit-719c8408) with [`tools/load_release_tokens.sh`](../../tools/load_release_tokens.sh), then runs `tools/brand_kit_ci_submit.py` from the repository checkout. | Not applicable (local operator process) | `ARGO_SUBMIT_TOKEN` for the one submission POST | `POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/submit` with `resourceKind: WorkflowTemplate` and `resourceName: brand-kit-ci`, submitting only `brand-kit-ci` for one exact full commit. The submit credential issues no read, list, or reconciliation request — attestation and reconciliation `GET`s belong to the release publisher's `ARGO_TOKEN` — and it must not delete, suspend, resume, update, terminate, or administer workflows, submit any other template, or reach another namespace. |

## Workload rotation, revocation, and compromise response

The workload inventory has three secret-backed credentials and three
Kubernetes identity credentials. The two Garage key pairs and the shared
Forgejo token are rotated at their providers; the ServiceAccounts are rotated
and revoked by Kubernetes. None of these procedures changes a CronWorkflow or
uses an operator release token.

| Credential | Owning source and ESO projection | Normal rotation and use | Revocation boundary |
| --- | --- | --- | --- |
| Garage reader: `needle-ci-artifact-reader` | Garage operator writes `ACCESS_KEY_ID` and `SECRET_ACCESS_KEY` at `ardenone-cluster/garage-operator/needle-ci-artifact-reader-v2-s3-credentials`; replication reaches the `iad-ci` `ExternalSecret`, which projects `access-key` and `secret-key` into the same-named Secret. | The Garage/cluster operator creates a replacement with the same read-only bucket/prefix scope, updates that existing OpenBao record, waits for ESO to report `SecretSynced=True`, then checks a fresh read-only watcher/liveness run. | Revoke the old Garage key, not the Kubernetes Secret. A normal overlap lets existing pods finish; an emergency revoke can make in-flight report reads fail, but the scheduled CronWorkflows remain defined and the next pod uses the refreshed Secret. |
| Garage publisher: `needle-ci-artifact-publisher` | Garage operator writes `ACCESS_KEY_ID` and `SECRET_ACCESS_KEY` at `ardenone-cluster/garage-operator/needle-ci-artifact-publisher-v2-s3-credentials`; ESO projects `access-key` and `secret-key` into the same-named Secret. | Create a replacement with the same write-only artifact/report scope, update the existing OpenBao record, wait for `SecretSynced=True`, and verify a fresh report upload. | Revoke the old Garage key after the overlap window. A pod already uploading with the old pair can fail; no CronWorkflow is suspended or deleted, and new runs recover after ESO has delivered the replacement. |
| Shared Forgejo reader: `rs-manager/iad-ci/forgejo/repo-readonly` | Forgejo creates the replacement repo-scoped read-only token. OpenBao property `token` is projected by the `brand-kit-consumer-drift` `ExternalSecret` into Secret `brand-kit-consumer-drift` in `argo-workflows`; the Forgejo namespace's `forgejo-repo-readonly` projection from the same source must be checked as well. | Create a replacement with only the `jedarden/brand-kit` read scope, write `token` at the same OpenBao path, wait for `SecretSynced=True`, then exercise both the consumer-drift release GET and mirror-health git read. One source rotation covers both scheduled workloads. | Revoke the old Forgejo token at Forgejo. Do not change either CronWorkflow or replace the Secret name. Existing pods may retain the old environment value until they exit; after ESO refresh, newly scheduled audit and mirror-health pods use the replacement. |
| `brand-kit-ci-failure-watch` ServiceAccount | Kubernetes ServiceAccount and namespace Role/RoleBinding in `argo-workflows`; the projected Kubernetes API token is not stored in OpenBao and has no `ExternalSecret`. | Kubelet rotates the projected token automatically. Keep the ServiceAccount name and its least-privilege RoleBinding stable during ordinary operation. | A compromised identity is revoked through a declarative-config RoleBinding/ServiceAccount change. Removing its binding or deleting the ServiceAccount also breaks the watcher's Kubernetes API reads and can prevent new scheduled pods until the matching template/RBAC replacement is reconciled. |
| `brand-kit-workflow-liveness` ServiceAccount | Kubernetes ServiceAccount and namespace Role/RoleBinding in `argo-workflows`; its projected token is not an OpenBao/ESO credential. | Kubelet rotates the projected token automatically; no provider write or Secret refresh is involved. | Revoke through GitOps by changing the RoleBinding or replacing the ServiceAccount. This is an intentional availability-impacting identity change, unlike rotating a Garage or Forgejo Secret in place. |
| `brand-kit-consumer-drift` ServiceAccount | Kubernetes ServiceAccount and namespace Role/RoleBinding in `argo-workflows`; its projected token is separate from the Forgejo `token` Secret and is not managed by OpenBao/ESO. | Kubelet rotates the projected token automatically. Rotate the Forgejo Secret independently when the audit's provider credential changes. | Revoke through declarative-config only. Deleting or rebinding the ServiceAccount can stop new consumer-drift pods until the WorkflowTemplate, CronWorkflow, and RBAC are reconciled together. |

### Secret-backed workload rotation order

The provider owner performs the credential creation and revocation: the Garage
operator for either Garage pair, and the Forgejo administrator for the
repo-readonly token. The cluster/GitOps owner owns the ExternalSecret path and
must not hand-edit the generated Kubernetes Secret. For each rotation:

1. Create a replacement at the provider with the exact existing scope. Keep
   the old credential valid while the replacement is being propagated.
2. Write the replacement fields to the existing authoritative OpenBao path
   and property names. For Garage, update both `ACCESS_KEY_ID` and
   `SECRET_ACCESS_KEY`; for Forgejo, update `token`. Use the provider/OpenBao
   CAS procedure and never put a value in a command argument, report, or log.
3. Wait for source replication and the existing `ExternalSecret` to report
   `Ready=True`/`SecretSynced=True`, then confirm the target Secret still has
   the expected keys. The manifests refresh hourly; an approved GitOps/ESO
   refresh may be used when an earlier check is needed.
4. Start one fresh Workflow from the existing template, or use the next
   scheduled run, and verify the documented read or upload succeeds. Do not
   suspend, delete, or edit the CronWorkflow to rotate a Secret.
5. After the fresh run succeeds and any in-flight old-credential runs have
   drained, revoke the old provider credential. Pods and Workflow inputs that
   started before the refresh can retain the old value until they exit, so
   `SecretSynced=True` is required before treating a new run as covered.

If a replacement is malformed, cannot replicate, or fails its fresh run, leave
the last known-good provider credential active, repair the provider/OpenBao
record, and retry. If a secret is revoked before ESO has synced the
replacement, currently running pods and newly scheduled pods can fail closed
with authentication errors; the CronWorkflow object and its schedule still
remain in place. Never widen the scope, substitute an operator token, or
silently switch to a different Secret name.

For a suspected compromise, revoke the affected provider key immediately even
if that sacrifices in-flight runs. Then create the replacement, write the
same OpenBao path, wait for `SecretSynced=True`, and run the affected workload
again. A compromised Forgejo token is shared by consumer-drift and
mirror-health, so both checks are required before declaring recovery. A
compromised Garage reader can affect the watcher/liveness/consumer reads; a
compromised publisher can affect report uploads. In each case the scheduled
CronWorkflows stay enabled, and the next run uses the replacement once ESO has
reconciled it.

ServiceAccount token rotation is not part of the OpenBao flow. Kubernetes
projected tokens are short-lived and kubelet-managed; full revocation is an
RBAC/ServiceAccount change that must be delivered through declarative-config
and reconciled with the referenced WorkflowTemplates. Treat that change as an
availability-impacting deployment and verify the scheduled Workflow after
reconciliation.

### Why the watchers carry no Argo token (2026-10-11, brandkit-cbbcc260)

Until 2026-10-11 the failure watcher and liveness check read Argo through
argo-server with an `ARGO_WORKFLOW_TOKEN` from Secret
`brand-kit-workflow-readonly` (ExternalSecret source
`rs-manager/iad-ci/argo/workflow-readonly`). That could never work: the
OpenBao path was never provisioned (every watch pod sat in
`CreateContainerConfigError`), and argo-server on iad-ci runs
`--auth-mode=sso` only, which rejects ServiceAccount bearer tokens. The Secret
and its ExternalSecret are retired; no workload may reference
`brand-kit-workflow-readonly` again.

- The **failure watcher** lists `brand-kit-ci` Workflow objects from the
  Kubernetes API, where they are ordinary custom resources, using its own pod
  identity. Nothing is provisioned and no credential exists outside the pod.
- The **liveness check** does not need Workflow objects at all. iad-ci deletes
  a Succeeded Workflow 300 seconds after it finishes and keeps at most 20
  completed Workflows, so Workflow records cannot show when a daily target
  last succeeded. Every target already uploads its own `report.json` for every
  run to `failures/<target>/<version>/<workflow-uid>/report.json`
  (`artifactGC: Never`); a report with the target's own schema and success
  status is the durable success record.
- Every Garage call sends an explicit `User-Agent`. `s3.ardenone.com` is
  behind Cloudflare, whose browser-integrity check answers urllib's default
  `Python-urllib/3.x` agent with HTTP 403 (`error code: 1010`) -- the
  "Garage GET request failed with HTTP 403" that failed every report-pruning
  step before 2026-10-11.

## Kubernetes authorization

Those three workloads use dedicated ServiceAccounts and namespace-scoped
Roles in `declarative-config/k8s/iad-ci/argo-workflows/brand-kit-automation-rbac.yml`
(contract test: `scripts/test-brand-kit-authorization.py` there). Each Role grants:

- `get` on the exact Secret names used by that workload, plus the read-only
  pod inspection required by the Argo executor;
- `argoproj.io` `workflowtaskresults` `create`/`patch` -- the one write grant,
  which the Argo v4 emissary executor's wait container needs to report its own
  task result (without it the wait container exits 64 and every run fails);
- for `brand-kit-ci-failure-watch` only, `argoproj.io` `workflows`
  `get`/`list`.

There is no ClusterRole or ClusterRoleBinding, no other write verb, no grant on
`cronworkflows` or `workflowtemplates`, and no grant for an unrelated Secret.
The RoleBindings and all referenced Secrets are confined to `argo-workflows`;
the general `argo-workflow` account remains for other WorkflowTemplates and is
not used by these credential-sensitive workloads.

The `brand-kit-release-token-probe` is the one deliberate exception: it is an
operator-credential probe, not a general watcher or consumer workload, and it
reads `brand-kit-release-tokens` only to validate the three operator paths. No
other workload may mount that Secret. Its Argo submission check uses
`ARGO_SUBMIT_TOKEN` with `serverDryRun`, so it validates authorization without
creating a Workflow.

The consumer submitter keeps this boundary during reconciliation: `ARGO_TOKEN`
may list or fetch an existing consumer-drift Workflow, while
`ARGO_SUBMIT_TOKEN` may make the one submission `POST`. The submit token is not
used to inspect duplicate state.

## Verification contract

The failure watcher and liveness code only issue `GET` requests. Their
manifests must use their dedicated ServiceAccounts and must not mention
`ARGO_WORKFLOW_TOKEN`, `brand-kit-workflow-readonly`, `ARGO_TOKEN`,
`ARGO_SUBMIT_TOKEN`, or `brand-kit-release-tokens`. The consumer audit must use
`brand-kit-consumer-drift` for its Forgejo credential and only the explicitly
allow-listed artifact-store Secrets; it must not mention either operator Argo
token. The release tools separately require the named operator variable, so an
operator read token cannot be silently promoted to a submit token.

The mirror-health workload carries the same read-only Forgejo repo token, but
it reaches git only through the `credential.https://git.ardenone.com.helper`
environment config: the token must never surface in a remote URL, in process
argv, on disk, or at any host other than git.ardenone.com (GitHub included),
and its git surface stays within `clone`/`ls-remote`/`fetch` plus the
checker's local-only `init`/`rev-parse`/`merge-base` scratch operations --
never a push and never a Forgejo API call.

The brand-kit CI submitter carries that same boundary onto the `brand-kit-ci`
template: `tools/brand_kit_ci_submit.py` requires `ARGO_SUBMIT_TOKEN` and
sends it only as the Bearer header of the single
`POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/submit`
for `resourceName: brand-kit-ci`. It never reads, lists, deletes, suspends,
or administers anything with that credential; attestation and reconciliation
reads stay with the publisher's `ARGO_TOKEN`. The scheduled release-token
probe validates both documented submission legs (`brand-kit-ci` and
`brand-kit-consumer-drift`) with Argo's `serverDryRun`, so a submit token
scoped to only one of the two templates fails the probe before the next
release or CI trigger needs it.

Run the focused contract tests with:

```bash
python3 -m pytest -q test_argo_credential_separation.py
# from the declarative-config checkout:
python3 scripts/test-brand-kit-authorization.py
```
