# Argo credential separation

This repository has three different credential roles. The variable name
`ARGO_TOKEN` is reserved for the release operator's read-only attestation; it
is not the credential injected into the scheduled watcher workloads.

## Role map

| Role | Kubernetes resource and namespace | Service account | Environment/key | Endpoint and allowed operation |
| --- | --- | --- | --- | --- |
| Failure watcher workload | Secret `brand-kit-workflow-readonly` in `argo-workflows`. The Secret is delivered by ExternalSecret `brand-kit-workflow-readonly` in the same namespace; that ExternalSecret is owned by `declarative-config/k8s/iad-ci/argo-workflows/`. | `brand-kit-ci-failure-watch` | `ARGO_WORKFLOW_TOKEN` from Secret key `token` | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows?labelSelector=...` and the returned workflow's `GET .../workflows/argo-workflows/<name>` evidence links. No `POST`, `PUT`, `PATCH`, or `DELETE`. |
| Liveness workload | Secret `brand-kit-workflow-readonly` in `argo-workflows`, delivered by the same-named ExternalSecret. | `brand-kit-workflow-liveness` | `ARGO_WORKFLOW_TOKEN` from Secret key `token` | The same Argo API `GET` boundary as the failure watcher; no `POST`, `PUT`, `PATCH`, or `DELETE`. |
| Consumer audit workload | ExternalSecret `brand-kit-consumer-drift` in `argo-workflows`, targeting Secret `brand-kit-consumer-drift` in `argo-workflows`. Its source is `rs-manager/iad-ci/forgejo/repo-readonly`, property `token`. | `brand-kit-consumer-drift` | `FORGEJO_TOKEN` from Secret key `token` | `GET https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1` for release metadata. The audit has no Argo credential and no write-capable Git operation. |
| Release publisher operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_TOKEN` from OpenBao path `secret/rs-manager/brand-kit/release/argo-read` for the local release process. | Not applicable (local operator process) | `ARGO_TOKEN` | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<brand-kit-ci-run>` for the read-only release attestation and the consumer submitter's duplicate reconciliation reads. |
| Consumer submitter operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_TOKEN` and `ARGO_SUBMIT_TOKEN` from their separate OpenBao paths `secret/rs-manager/brand-kit/release/argo-read` and `secret/rs-manager/brand-kit/release/argo-submit`. | Not applicable (local operator process) | `ARGO_TOKEN` for reconciliation reads; `ARGO_SUBMIT_TOKEN` for the one submission POST | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<name>` and the template-scoped list use `ARGO_TOKEN`; the single `POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows` uses `ARGO_SUBMIT_TOKEN` to submit only `brand-kit-consumer-drift`. It must not delete, suspend, update, or administer workflows. |

The deployment's `brand-kit-workflow-readonly` ExternalSecret must map its
`token` property from the dedicated read-only Argo provider credential (the
`rs-manager/iad-ci/argo/workflow-readonly` path). It must not be populated from
`brand-kit-release-tokens`, `secret/rs-manager/brand-kit/release/argo-read`,
or `secret/rs-manager/brand-kit/release/argo-submit`. The exact ExternalSecret
manifest belongs in `declarative-config`; this repository owns the consumer
and watcher WorkflowTemplates that consume the resulting Secret.

Those three workloads use dedicated ServiceAccounts and namespace-scoped
Roles in `declarative-config/k8s/iad-ci/argo-workflows/`. Each Role grants
only `get` on the exact Secret names used by that workload (plus the read-only
pod inspection required by the Argo executor). There is no ClusterRole or
ClusterRoleBinding, no write verb, and no grant for an unrelated Secret. The
RoleBindings and all referenced Secrets are confined to `argo-workflows`; the
general `argo-workflow` account remains for other WorkflowTemplates and is not
used by these credential-sensitive workloads.

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
manifests must use `ARGO_WORKFLOW_TOKEN`, `brand-kit-workflow-readonly`, and
their dedicated ServiceAccounts, and
must not mention `ARGO_TOKEN`, `ARGO_SUBMIT_TOKEN`, or
`brand-kit-release-tokens`. The consumer audit must use
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
