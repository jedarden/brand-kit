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

Run the focused contract tests with:

```bash
python3 -m pytest -q test_argo_credential_separation.py
# from the declarative-config checkout:
python3 scripts/test-brand-kit-authorization.py
```
