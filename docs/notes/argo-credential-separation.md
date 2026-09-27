# Argo credential separation

This repository has three different credential roles. The variable name
`ARGO_TOKEN` is reserved for the release operator's read-only attestation; it
is not the credential injected into the scheduled watcher workloads.

## Role map

| Role | Kubernetes resource and namespace | Service account | Environment/key | Endpoint and allowed operation |
| --- | --- | --- | --- | --- |
| Failure watcher and liveness workload | Secret `brand-kit-workflow-readonly` in `argo-workflows`. The Secret is delivered by ExternalSecret `brand-kit-workflow-readonly` in the same namespace; that ExternalSecret is owned by `declarative-config/k8s/iad-ci/argo-workflows/`. | `argo-workflow` | `ARGO_WORKFLOW_TOKEN` from Secret key `token` | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows?labelSelector=...` and the returned workflow's `GET .../workflows/argo-workflows/<name>` evidence links. No `POST`, `PUT`, `PATCH`, or `DELETE`. |
| Consumer audit workload | ExternalSecret `brand-kit-consumer-drift` in `argo-workflows`, targeting Secret `brand-kit-consumer-drift` in `argo-workflows`. Its source is `rs-manager/iad-ci/forgejo/repo-readonly`, property `token`. | `argo-workflow` | `FORGEJO_TOKEN` from Secret key `token` | `GET https://git.ardenone.com/api/v1/repos/jedarden/brand-kit/releases?limit=1` for release metadata. The audit has no Argo credential and no write-capable Git operation. |
| Release publisher operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_TOKEN` from OpenBao path `secret/rs-manager/brand-kit/release/argo-read` for the local release process. | Not applicable (local operator process) | `ARGO_TOKEN` | `GET https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows/<brand-kit-ci-run>` for the read-only release attestation. |
| Consumer submitter operator | No Kubernetes Secret or workload mount. The operator loads `ARGO_SUBMIT_TOKEN` from OpenBao path `secret/rs-manager/brand-kit/release/argo-submit`. | Not applicable (local operator process) | `ARGO_SUBMIT_TOKEN` | `POST https://argo-ci.ardenone.com/api/v1/workflows/argo-workflows` to submit only `brand-kit-consumer-drift`; it must not delete, suspend, update, or administer workflows. |

The deployment's `brand-kit-workflow-readonly` ExternalSecret must map its
`token` property from the dedicated read-only Argo provider credential (the
`rs-manager/iad-ci/argo/workflow-readonly` path). It must not be populated from
`brand-kit-release-tokens`, `secret/rs-manager/brand-kit/release/argo-read`,
or `secret/rs-manager/brand-kit/release/argo-submit`. The exact ExternalSecret
manifest belongs in `declarative-config`; this repository owns the consumer
and watcher WorkflowTemplates that consume the resulting Secret.

The `brand-kit-release-token-probe` is the one deliberate exception: it is an
operator-credential probe, not a general watcher or consumer workload, and it
reads `brand-kit-release-tokens` only to validate the three operator paths. No
other workload may mount that Secret. Its Argo submission check uses
`ARGO_SUBMIT_TOKEN` with `serverDryRun`, so it validates authorization without
creating a Workflow.

## Verification contract

The failure watcher and liveness code only issue `GET` requests. Their
manifests must use `ARGO_WORKFLOW_TOKEN` and `brand-kit-workflow-readonly`, and
must not mention `ARGO_TOKEN`, `ARGO_SUBMIT_TOKEN`, or
`brand-kit-release-tokens`. The consumer audit must use only its
`brand-kit-consumer-drift` Secret and must not mention either operator Argo
token. The release tools separately require the named operator variable, so an
operator read token cannot be silently promoted to a submit token.

Run the focused contract tests with:

```bash
python3 -m pytest -q test_argo_credential_separation.py
```
