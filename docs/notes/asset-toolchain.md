# Asset toolchain pins

The committed PNGs in `avatars/`, `banners/`, `favicon/` and `logo/` are
byte-reproducible only with the exact toolchain below. The CI regression gate
(`brand-kit-ci` WorkflowTemplate in `jedarden/declarative-config`,
`k8s/iad-ci/argo-workflows/brand-kit-ci-workflowtemplate.yml`) checks that its
install commands still match this table before installing them, then runs the
full pytest suite and `tools/verify_assets.py`. It fails on pin or install
contract drift, test failures, unexpected generated files, or regenerated
diffs, so a commit whose assets or checks did not pass this toolchain is a
broken commit.
The ordered commands and required Argo log evidence are documented under
[CI regression gate](../../README.md#ci-regression-gate).

## CI failure routing and owner follow-up

The gate has an application-owned companion check in
`automation/brand-kit-ci-failure-watch-workflowtemplate.yml`, scheduled by
`automation/brand-kit-ci-failure-watch-cronworkflow.yml`. Every 15 minutes it
uses the dedicated read-only `ARGO_WORKFLOW_TOKEN` to list `brand-kit-ci` Workflow records
and retains `Failed`/`Error` runs from the preceding two hours. The lookback is
intentional: iad-ci's Argo controller retains failed Workflows for 7200 seconds
through `controller.workflowDefaults.spec.ttlStrategy.secondsAfterFailure` in
`declarative-config/k8s/iad-ci/argo-workflows/argo-workflows-application.yml`.
The token comes from Secret `brand-kit-workflow-readonly` in namespace
`argo-workflows`, delivered by the same-named ExternalSecret; it is not the
release operator's `ARGO_TOKEN` from `brand-kit-release-tokens`. The watcher
runs as dedicated service account `brand-kit-ci-failure-watch` and calls only
the Argo Workflow list and detail `GET` endpoints. See
[`argo-credential-separation.md`](argo-credential-separation.md) for the full
role map and the submit/mutation boundary.
The watcher contract requires its lookback to cover that full retention window,
and retains the exact boundary (`cutoff` is inclusive), so a short-lived failed
run is still detected before its Workflow is reaped. The deployed
WorkflowTemplate records both values as annotations. Check the repository
copy, and optionally the external controller configuration, with:

```bash
python3 tools/check_ci_failure_watch_retention.py \
  --argo-config ../src/declarative-config/k8s/iad-ci/argo-workflows/argo-workflows-application.yml
```

The check fails if the command, annotations, watcher contract, or external
Argo retention drift apart; the watcher also fails closed at runtime rather
than running with a shorter unsafe lookback.

The list is a complete Kubernetes-style snapshot, not a single page. The
watcher requests at most 100 records per page, follows every non-empty
`metadata.continue` token with the original label selector and limit, and only
then applies the local UTC cutoff. It does not stop because a page is old or
short: Argo may return pages in an order that is not timestamp order. A failure
is in-window when its first usable timestamp among `status.finishedAt`,
`status.startedAt`, and `metadata.creationTimestamp` is at or after the cutoff;
an in-window successful attestation requires `status.finishedAt` at or after
the cutoff. A missing timestamp on a failed record remains reportable for
manual review. Missing `items`, missing pagination metadata on a continuation
page, malformed workflow objects or continuation metadata, a repeated token, a
failed page request, or any other incomplete page fails the watcher closed with
an error report and no partial result.

The failure-watch report contract is
[`ci-failure-watch-report.schema.json`](../../ci-failure-watch-report.schema.json);
the separate durable envelope is defined by
[`ci-attestations.schema.json`](../../ci-attestations.schema.json). Both are
strict v1 JSON contracts with no unknown fields. Reports always identify the
schema and watcher Workflow UID, use UTC `Z` timestamps, record `observed_at`
and an inclusive cutoff with the configured lookback and Argo retention, and
include the report's attestation-object URL. A failure row carries its Argo
Workflow URL when the name is valid. `pass` and `fail` are complete snapshots;
`error` means inspection did not start; `incomplete` records a stable
pagination error code and page number. `pages_read` counts pages whose
`items` and workflow entries were structurally valid; the error's `page` names
the response or request that prevented a complete snapshot. Incomplete reports
contain no partial failures or attestations. The attestation envelope binds its list to the
watcher UID in the immutable Garage object path, and each success row contains
the full commit, workflow identity, `Succeeded` phase, and UTC completion
time. The watcher validates both JSON artifacts before writing them, and the
Workflow validates them again on exit immediately before Argo uploads them.
Representative pass, fail, pagination-incomplete, preflight-error, and
attestation fixtures live under `tests/fixtures/ci-failure-watch/`.

### Deployment path and live parity

These two manifests are not copied into `declarative-config`. The
application-owned deployment wiring is
`declarative-config/k8s/iad-ci/argo-workflows/brand-kit-automation-application.yml`.
Its `brand-kit-automation-iad-ci` ArgoCD `Application` reads
`https://github.com/jedarden/brand-kit.git` at `main`, recursively includes
`*.yml` and `*.yaml` below `automation/`, and applies that directory to the
`argo-workflows` namespace in `iad-ci`. A push to the Forgejo `origin` is
mirrored to the GitHub source that ArgoCD reads; there is no second
failure-watch copy in `declarative-config` to edit. The `brand-kit-ci`
regression gate remains the separate template owned directly by
`declarative-config`.

Because GitHub is a read-only mirror rather than the authority, the directory
also contains `brand-kit-forgejo-github-parity-workflow.yml`. ArgoCD runs this
Argo `Workflow` as a `PreSync` hook at sync wave `-1`. It reads the exact
`refs/heads/main` object ID from both Forgejo and GitHub and exits non-zero on
missing, malformed, or unequal values, so no normal automation resource is
applied while the mirror is stale or only partially propagated. A failed hook
is kept for diagnosis; retrying after propagation uses
`BeforeHookCreation` to replace it, and successful hook workflows are deleted.

After the child Application reconciles, compare the repository copies with the
applied objects using the read-only parity check:

```bash
python3 tools/check_ci_failure_watch_parity.py \
  --server http://traefik-iad-ci:8001
```

Before reading either child object, the check reads the
`brand-kit-automation-iad-ci` Application through the default read-only
`http://traefik-rs-manager:8001` proxy. It requires the documented GitHub
mirror, `main` revision, `automation` path, recursive YAML include, and
`argo-workflows` destination namespace, plus live `Synced` and `Healthy`
status. A failed or malformed Application read stops the check, so the
child-object parity result cannot be mistaken for a valid deployment. All
queries use `kubectl get`; the check never applies or mutates resources. Use
`--application-server` when the Application proxy differs. Run it again after
an ArgoCD sync or whenever the failure-watch files change.

Inspect the deployment status through the ArgoCD Application on the
`rs-manager` control plane, or inspect the objects directly through the
read-only `iad-ci` proxy:

```bash
kubectl --server=http://traefik-rs-manager:8001 -n argocd \
  get application brand-kit-automation-iad-ci -o wide
kubectl --server=http://traefik-iad-ci:8001 -n argo-workflows \
  get workflowtemplate brand-kit-ci-failure-watch -o yaml
kubectl --server=http://traefik-iad-ci:8001 -n argo-workflows \
  get cronworkflow brand-kit-ci-failure-watch -o yaml
```

The same live objects and their runs are visible in the VPN-only Argo UI at
`https://argo-ci.ardenone.com`, in namespace `argo-workflows`, under the
`brand-kit-ci-failure-watch` WorkflowTemplate and CronWorkflow names.

Before changing any file in `automation/`, run the repository-side manifest
contract check:

```bash
python3 tools/check_automation_manifests.py
```

It recursively checks every YAML manifest against
`automation/manifest.schema.json`, including the Argo kind/API version,
namespace and name, template references, parameter declarations, schedules,
image pins, and the rule that only the repository's short-lived
WorkflowTemplate, CronWorkflow, and parity Workflow resources may target
`argo-workflows`. With a declarative-config checkout, pass the
application manifest too to verify its recursive YAML inclusion and namespace
boundary:

```bash
python3 tools/check_automation_manifests.py \
  --application ../declarative-config/k8s/iad-ci/argo-workflows/brand-kit-automation-application.yml
```

The watcher writes a sanitized JSON report and uploads it to the
Garage-backed `needle-ci-artifacts` bucket with `artifactGC: Never`:

```text
https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-ci-failure-watch/v1/<watcher-workflow-uid>/report.json
```

`artifactGC: Never` only prevents Argo from deleting the object when this
watcher's Workflow is reaped; it is not an indefinite-retention policy. Before
each Argo inspection, the watcher runs
`tools/prune_ci_failure_watch_reports.py` with the Garage reader credential to
list only this report prefix and the publisher credential to delete only
`report.json` objects older than **30 days**. The pass runs on the same
15-minute schedule, so cleanup can lag the 30-day boundary by one schedule
interval. It does not touch the separate CI attestation prefix.

An owner can realistically fetch the report URL in an alert for at least 30
days after the report upload (normally up to about 30 days and 15 minutes).
After that window the URL may return `404`; an older alert is stale evidence,
not a guarantee that its durable report still exists. Preserve any report
needed for a longer investigation outside this rolling prefix before the
window expires.

### Durable CI attestations

The watcher also writes successful-run attestations to a separate prefix:

```text
https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json
```

The watcher UID is embedded in the JSON envelope and in the object key. Each
entry includes the exact CI workflow name and UID, full commit, `Succeeded`
phase, and timezone-qualified `finished_at`. This makes the UID-scoped object
address a write-once evidence identity: a later watcher has a different UID,
and the cleanup job only deletes objects, never rewrites or repoints them.
`artifactGC: Never` prevents Argo from deleting the object with the watcher
Workflow, but does not make it permanent.

Before each Argo inspection, the same WorkflowTemplate runs
`tools/prune_ci_attestations.py`. It lists only
`attestations/brand-kit-ci/v1/` with the Garage reader credential and deletes
only `attestations.json` objects older than **30 days** with the publisher
credential. An object exactly at the cutoff remains until the next pass. Every
`ci.attestation_url` in every valid `release-evidence/v1/*.json` record is an
indefinite retention hold. The pruner validates all evidence and the exact
endpoint, bucket, and object-key shape before listing or deleting; malformed,
missing, or out-of-prefix evidence fails closed and causes an indeterminate
watcher report.

If the named Argo Workflow has already been garbage-collected, pass the exact
attestation URL to `tools/release_publish.py`. The publisher uses this fallback
only after an Argo `404`; it rejects live failed workflows, malformed or
inaccessible objects, mismatched envelope UIDs, malformed records, duplicate
matches, and records for another workflow or commit. An unheld object may
return `404` after the 30-day window, so copy it to an approved recovery
location before expiry when release recovery or investigation may take longer.

Consumer-drift reports use a separate cleanup policy. The consumer-drift
WorkflowTemplate runs `tools/prune_consumer_drift_reports.py` before the
detector with the Garage reader and publisher credentials. It lists only
`failures/brand-kit-consumer-drift/v1/` and deletes only `report.json` objects
older than **30 days**. The cleanup loads and validates every
`release-evidence/v1/*.json` record first; any `consumer_drift.report_url`
matching this prefix protects that object indefinitely from automated pruning.
Missing or malformed evidence, an endpoint/bucket mismatch, and storage
access failures all fail closed before deletion. The workflow writes an
indeterminate report and alerts the owner when cleanup cannot complete, so the
operator repairs the evidence or storage path and reruns the audit. An
unheld report can return `404` after the 30-day window; copy it to an approved
recovery location before then if it is needed for longer-term investigation.

The release-triggered consumer-drift handoff and its scheduled CronWorkflow
fallback share the `brand-kit-consumer-drift` WorkflowTemplate. For confirmed
drift (exit `1`), an indeterminate audit (exit `2`), or a setup failure before
the detector starts, Argo uploads the sanitized JSON report with
`artifactGC: Never` to:

```text
https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-consumer-drift/v1/<workflow-uid>/report.json
```

When the watcher finds a failed gate — or cannot inspect Argo — its workflow
exit handler posts a `BrandKitCIRegressionGate` alert to
`alertmanager.monitoring.svc:9093/api/v2/alerts`. Alertmanager's configured
ntfy receiver routes the alert to the `jedarden` owner channel. This is the
owner path for pin/install drift, test failures, unexpected generated files,
regenerated diffs, and watcher/API errors; it is intentionally best-effort and
cannot turn the underlying gate result green.

The owner follows the durable report to the failed `brand-kit-ci` run, fixes
the exact commit's toolchain or asset defect, and pushes a new commit. The
gate must be rerun for that commit and reach `Succeeded` before release
publication. A watcher/API error is an operational failure, not evidence that
the asset commit passed, so repair the read path and rerun the check first.

### Scheduled workflow liveness

Scheduled workflows cannot report that they are missing: their Alertmanager
handlers run only after a target Workflow starts. The independent
`brand-kit-workflow-liveness` CronWorkflow therefore runs every 15 minutes and
lists successful runs for every scheduled brand-kit WorkflowTemplate except
itself. This monitored-schedule list is an enforced contract: the tests require
every CronWorkflow (apart from the watchdog itself) to appear in the liveness
targets below and in this documentation.

| WorkflowTemplate | CronWorkflow expression (UTC) | Maximum success age |
| --- | --- | --- |
| `brand-kit-ci-failure-watch` | `*/15 * * * *` | 60 minutes |
| `brand-kit-consumer-drift` | `17 6 * * *` | 48 hours |
| `brand-kit-mirror-health` | `17 */6 * * *` | 12 hours |
| `brand-kit-platform-requirements` | `27 6 * * *` | 48 hours |
| `brand-kit-release-token-probe` | `7 6 * * *` | 48 hours |

Missing/old successes are `stale`; an unavailable or malformed Argo response is
`indeterminate`. Both non-fresh states fail the watchdog and invoke its
`BrandKitWorkflowLiveness` owner alert. Its report is retained at
`failures/brand-kit-workflow-liveness/v1/<workflow-uid>/report.json`, so a
suspended/deleted/mis-scheduled CronWorkflow or broken template reference has a
separate signal even when the target never reaches `onExit`.

### Forgejo-to-GitHub mirror health

The `brand-kit-mirror-health` WorkflowTemplate runs
`tools/check_mirror_health.py` from a read-only checkout of canonical Forgejo
every six hours. The checker uses `git ls-remote` for all branches and tags on
both repositories. When object IDs differ, it fetches the named refs into a
disposable local bare repository only to determine ancestry: a mirror commit
behind Forgejo is `stale`, an unrelated or mirror-only ref is `divergent`, and
an absent mirror ref is `missing`. It never runs `git push`, writes either
remote, or treats an unavailable/inconclusive comparison as healthy.

Reports are retained at
`https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-mirror-health/v1/<workflow-uid>/report.json`.
Confirmed drift and indeterminate reads both fail the Workflow and invoke the
`BrandKitMirrorHealth` Alertmanager route, which sends the owner a durable
report URL before a release or consumer handoff can rely on the mirror.

### Platform requirement source health

The `brand-kit-platform-requirements` WorkflowTemplate runs the local
`tools/check_platform_requirements.py` freshness/data check and its explicit
`--check-reachability` mode once per day. It clones the canonical Forgejo
repository read-only and checks every valid HTTPS source in
`platform-assets.json`. The policy is a maximum age of **180 calendar days**:
an entry is current through day 180 and stale after that. A 2xx or 3xx source
response passes; an HTTP error is a confirmed failure (exit 1), while DNS,
TLS, timeout, and connection failures are indeterminate (exit 2). The latter
must never be treated as proof that a source passed.

The sanitized report is retained at
`https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-platform-requirements/v1/<workflow-uid>/report.json`.
Both confirmed and indeterminate failures invoke the
`BrandKitPlatformRequirements` owner route. The workflow is read-only: it
does not edit the manifest or external sources.

## Alertmanager route verification

The regression-gate, consumer-drift, and release-token-probe handlers have a
contract test in
`test_alertmanager_routing.py`. It parses the JSON heredoc each workflow posts
to Alertmanager, checking the alert names and labels below. The shared
`bucket: brand-kit` label is also the grouping key used by the configured
Alertmanager route.

| Workflow | Alert name | Component | Follow-up | Owner |
|---|---|---|---|---|
| `brand-kit-ci-failure-watch` | `BrandKitCIRegressionGate` | `brand-kit-ci` | `regression-gate` | `jedarden` |
| `brand-kit-consumer-drift` | `BrandKitConsumerDrift` | `consumer-drift` | — | `jedarden` |
| `brand-kit-release-token-probe` | `BrandKitReleaseTokenProbe` | `release-token-probe` | `consumer-drift` | `jedarden` |
| `brand-kit-workflow-liveness` | `BrandKitWorkflowLiveness` | `brand-kit-workflow-liveness` | `workflow-liveness` | `jedarden` |
| `brand-kit-mirror-health` | `BrandKitMirrorHealth` | `forgejo-github-mirror` | `mirror-health` | `jedarden` |
| `brand-kit-platform-requirements` | `BrandKitPlatformRequirements` | `platform-requirements` | `platform-requirements` | `jedarden` |

The complete label payloads also include `bucket: brand-kit` and the
`workflow_status` template value. All six handlers POST to
`http://alertmanager.monitoring.svc:9093/api/v2/alerts`; their `onExit` steps
run only for non-successful workflows and use `continueOn` so an Alertmanager
or ntfy outage cannot change the original failure result. Use the v2 API only:
Alertmanager removed `/api/v1/alerts` in 0.27, and iad-ci's 0.34.1 answered
every v1 POST with HTTP 410, so until 2026-10-10 no brand-kit owner alert was
ever delivered (`continueOn` hid the 410 behind the original failure).

For a live, redacted check in the iad-ci cluster, first inspect only the
non-secret route fields from the rendered config. This command must print the
`ntfy` receiver, the `[alertname, bucket]` grouping, and `max_alerts: 0`; it
does not print the ntfy URL or bearer token:

```bash
kubectl -n monitoring get secret alertmanager-config \
  -o jsonpath='{.data.alertmanager\.yaml}' |
  base64 --decode |
  grep -E '^[[:space:]]+(receiver: ntfy|group_by: \[alertname, bucket\]|- name: ntfy|max_alerts: 0)$'
```

Then send a clearly marked canary through the in-cluster Alertmanager service
and inspect the Alertmanager API/logs for acceptance and notification errors.
The canary uses the same labels as the regression-gate alert, so it exercises
the owner route without reading or printing any credential:

```bash
kubectl -n monitoring port-forward svc/alertmanager 19093:9093 >/dev/null 2>&1 &
PORT_FORWARD_PID=$!
trap 'kill "$PORT_FORWARD_PID" 2>/dev/null || true' EXIT
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  --data '[{"labels":{"alertname":"BrandKitCIRegressionGate","owner":"jedarden","component":"brand-kit-ci","follow_up":"regression-gate","bucket":"brand-kit","workflow_status":"routing-canary"},"annotations":{"summary":"brand-kit Alertmanager routing canary"}}]' \
  http://127.0.0.1:19093/api/v2/alerts
curl --fail --silent --show-error \
  'http://127.0.0.1:19093/api/v2/alerts?filter=alertname%3D%22BrandKitCIRegressionGate%22'
kubectl -n monitoring logs deploy/alertmanager --since=2m |
  grep -Ei 'notify|ntfy|error'
```

The canary is an operational notification and should be run only when an
owner can acknowledge it. A successful API response proves Alertmanager
accepted the alert; the recent logs must show the webhook notification was
attempted without an error. If notification delivery is unavailable, retain
the failed workflow and durable report as the source of truth: delivery is
intentionally best-effort.

## Pinned versions (canonical)

| Tool | Pin | Install command | Notes |
|---|---|---|---|
| resvg | `0.47.0` | `cargo install --locked resvg@0.47.0` | Renders `source/logo.svg` at each target size. `--locked` rejects transitive dependency resolution drift. |
| vtracer | `0.6.5` Cargo CLI | `cargo install --locked vtracer@0.6.5` | `tools/trace_logo.py` invokes the Cargo-installed binary; it does not use the PyPI `vtracer` package. `--locked` rejects transitive dependency resolution drift. |
| Pillow | `12.1.1` — **PyPI wheel build** | `.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1` | Encodes every PNG. The wheel-only flag is part of the pin. |
| pytest | `9.0.2` | `.venv/bin/python -m pip install --only-binary=:all: pytest==9.0.2` | Runs the regression suite; it does not affect generated asset bytes. |

Python and Rust are runtime/build prerequisites rather than byte-sensitive
pins: CI uses the Debian `python3` package and the Rustup `stable` toolchain.
They remain documented below because the checker only enforces the exact
four-tool reproducibility contract above.

### Pin parity check

`tools/check_asset_toolchain.py` parses this table and the actual
`brand-kit-ci` WorkflowTemplate. It compares every tool name, version,
installer, install command, and install option, and specifically requires
Pillow's `--only-binary=:all:` PyPI-wheel contract. It also rejects a tool
added to either side without a matching install on the other side.

Run the same check locally from the brand-kit checkout (with the sibling
`declarative-config` checkout available) before changing either set of pins:

```bash
python3 tools/check_asset_toolchain.py \
  --workflow-template ../declarative-config/k8s/iad-ci/argo-workflows/brand-kit-ci-workflowtemplate.yml
```

The CI WorkflowTemplate runs this command against the manifest cloned from the
canonical Forgejo repository before it installs Python dependencies or runs
the asset regression gate. Keep the table's install-command column and the
manifest's install block in the same commit when bumping a pin.

## How `trace_logo.py` obtains vtracer

`tools/trace_logo.py` is a Python entry point, but it does not import a Python
binding or use the similarly named package from PyPI. It launches the `vtracer`
executable found on `PATH`; the canonical pin is therefore the Cargo crate/CLI
installed by `cargo install --locked vtracer@0.6.5`. Do not substitute a pip-installed
`vtracer` package: that package and build are not covered by this pin.

Both Python entry points use Pillow, so both must run with the venv interpreter.
`resvg` is needed whenever derived assets are regenerated. The vtracer command
is needed only for an explicit raster-to-vector replacement of
`source/logo.svg`.

## Logo source-of-truth contract

`source/logo.svg` is the authoritative source for the opaque logo. The normal
build renders avatars, favicons, opaque logo masters, and `logo/logo.svg` from
that SVG with `resvg`; `source/logo.png` is never a raster fallback.
`source/logo-transparent.svg` is a separate, hand-maintained authoritative
source for the transparent logo outputs.

`source/logo.png` has two narrower roles: it is preserved provenance and is
copied byte-for-byte to `logo/logo-original.png`; it may also be the input to
an explicit `tools/trace_logo.py` run that replaces `source/logo.svg`. The trace
is not part of the normal build from the authoritative SVG. `build_assets.py`
uses a file copy for the original, rather than decoding and re-encoding it.

`source/logo.svg.sha256` is the committed integrity sidecar for the
authoritative SVG. It contains the SVG's bare SHA-256 digest. The normal build
does not read it, but `verify_assets.py` rejects a missing, malformed, or stale
sidecar. Whenever the bytes of `source/logo.svg` change—including a direct
edit—review the SVG and refresh the sidecar in the same commit:

```bash
sha256sum source/logo.svg | awk '{print $1}' > source/logo.svg.sha256
```

`source/logo-transparent.svg.sha256` is the matching committed integrity
sidecar for the independent transparent authoritative SVG. It follows the
same bare-digest contract, and `verify_assets.py` rejects a missing,
malformed, or stale sidecar. Because the transparent SVG is hand-maintained,
refresh it explicitly whenever `source/logo-transparent.svg` changes, in the
same commit:

```bash
sha256sum source/logo-transparent.svg | awk '{print $1}' > source/logo-transparent.svg.sha256
```

`source/logo.png.sha256` and `source/hero.png.sha256` are the matching bare
SHA-256 sidecars for the preserved logo raster and authoritative hero raster.
`verify_assets.py` rejects either sidecar when it is missing, malformed, or
stale, and also rejects any byte difference between `source/logo.png` and
`logo/logo-original.png`. Refresh the relevant raster sidecar whenever an
intentional source change is made, in the same commit:

```bash
sha256sum source/logo.png | awk '{print $1}' > source/logo.png.sha256
sha256sum source/hero.png | awk '{print $1}' > source/hero.png.sha256
```

Run `build_assets.py` before `verify_assets.py` so the exact original copy and
hero-derived assets are regenerated. Do not refresh a sidecar merely to hide
an unexpected source change; review the source and generated diff first.

`trace_logo.py` writes the refreshed sidecar automatically after a successful
trace of `source/logo.svg`. It does not update the transparent sidecar. Do not
refresh either SVG sidecar merely to hide an unexpected SVG change. Before an
ordinary trace, `trace_logo.py` fails closed when the opaque digest is missing
or malformed. It refuses to replace an existing SVG whose digest does not
match and checks that invariant again immediately before replacement.
`trace_logo.py --force` is the explicit escape hatch for deliberately replacing
a modified SVG from `source/logo.png`.

## The Pillow build flavor is part of the pin

This is load-bearing: **the same Pillow version number from a different build
produces different bytes.** Verified 2026-09-15: regenerating from identical
sources with NixOS-system Pillow 12.1.1 vs the PyPI `12.1.1` wheel differed in
**32 of 37** asset files (the encoder's zlib differs between builds).

Consequence: always regenerate with the PyPI wheel installed in `.venv`, **not**
a distro/system Pillow, even at the pinned version number. The setup command
below uses `--only-binary=:all:` so pip fails instead of silently building an
unpinned source distribution.

## Clean-toolchain reproducibility gate

`tools/check_reproducibility.py` is the full clean-environment test. It takes a
committed source snapshot and runs it twice in independent temporary trees,
with an empty `CARGO_HOME`, separate Cargo target directories, a fresh Python
virtualenv, and a no-cache PyPI wheel install. Each run installs the locked
`resvg` and `vtracer` Cargo packages, records both package `Cargo.lock` files,
and records the complete Python/Rust toolchain fingerprint. It then:

1. verifies the committed assets;
2. runs `trace_logo.py`, proving the applicable vtracer output matches the
   committed authoritative SVG and checksum;
3. runs `build_assets.py`, exercising resvg and every Pillow PNG/ICO encoder;
4. verifies the full generated inventory again; and
5. compares every file below `avatars/`, `banners/`, `favicon/`, and `logo/`,
   plus `palette.json` and `platform-assets.json`, against the committed
   snapshot and the other clean run.

The gate compares the locked transitive dependency graphs as well as output
bytes. `--locked` makes a changed package lock fail during installation, while
the cross-run comparison detects any differing lock graph, Python dependency
set, runtime version, or tool version. Renderer executable bytes are not a
criterion: native Rust builds can contain non-semantic build differences even
when their rendered output is identical.

Verified 2026-09-27 with `python3 tools/check_reproducibility.py`: each of two
clean source trees reported `41 generated files match committed bytes`, and
both produced the same complete generated tree and vtracer trace output, with
the pinned Pillow `12.1.1` wheel and matching locked resvg and vtracer
dependency graphs. The command is part of the `brand-kit-ci`
WorkflowTemplate acceptance sequence, after the normal pinned environment is
installed and before the ordinary regeneration drift check. A future failure
must be treated as toolchain drift: inspect the reported lock or byte
difference, then update the pins and regenerate all outputs deliberately.

## Regenerating

Run the one-time setup (or repeat it after a pin bump):

```bash
cargo install --locked resvg@0.47.0 vtracer@0.6.5
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2
```

For a normal edit to `source/logo.svg`, `source/logo-transparent.svg`, or
`source/hero.png`, skip the trace and run:

```bash
.venv/bin/python tools/build_assets.py
.venv/bin/python tools/verify_assets.py
```

Review and commit the changed bytes. On an already-generated commit, repeating
those commands and then running `git diff --exit-code` must produce no diff.

A PNG-only change that is meant to update `logo/logo-original.png` does not
require a trace; use the normal build commands above. To replace the opaque SVG
from the original raster, run this explicit transition in order:

1. Replace `source/logo.png`.
2. Run the trace:

   ```bash
   .venv/bin/python tools/trace_logo.py
   ```

3. Review `source/logo.svg`. Update `source/logo-transparent.svg` separately if
   the transparent artwork changed.
4. Run the build and verification commands:

   ```bash
   .venv/bin/python tools/build_assets.py
   .venv/bin/python tools/verify_assets.py
   ```

5. Refresh `source/logo.png.sha256` for the intentional raster replacement,
   then review the complete diff and commit the traced SVG, updated checksums,
   sources, exact original copy, and generated assets together.

The trace writes `source/logo.svg` and `source/logo.svg.sha256` only. If it
refuses because the current SVG differs from the recorded digest, either review
the SVG, refresh the sidecar, and continue from `build_assets.py`, or run
`.venv/bin/python tools/trace_logo.py --force` only when replacing that SVG is
intentional, then continue with the same build and verification steps.

`tools/build_assets.py` aborts if `resvg` or a source file is missing — there
is no raster-resize fallback. Fallback output is not byte-identical to the
vector renders, so silently degrading would make every environment produce
different bytes for the same commit.

## Bumping a pin

All pins move together, in one commit:

1. Update the table above.
2. Update the install lines in `brand-kit-ci-workflowtemplate.yml` (they name
   this file in a comment).
3. Regenerate every asset with the new toolchain (using the applicable sequence
   above) and include the full byte diff in the same commit.
4. Push and confirm the full CI regression gate passes: asset verification,
   all pytest tests, and the no-diff check.

## Provenance

Every asset byte in the tree as of 2026-09-15 was produced by exactly the
pinned toolchain above (resvg 0.47.0 + PyPI Pillow 12.1.1 wheel): two
consecutive regenerations from `source/` were byte-identical, and the
committed tree matches that output exactly. `source/logo.svg.sha256` records
the SHA-256 of the current authoritative SVG. A later successful trace updates
it automatically; a direct SVG edit must update it in the same commit.
History before 2026-09-15 was generated by unrecorded
tool versions (including an era before `optimize=True`, and raster-fallback
renders before the 2026-05-22 vectorization) and is not reproducible.
