# Jed Arden — Brand Kit

Canonical logo, hero image, and ready-to-upload social assets for every major
platform. The logo is SVG-first: `source/logo.svg` is authoritative, while
`source/logo.png` is the preserved original raster and optional trace seed.

| | Source | Role |
|---|---|---|
| **Logo (opaque)** | `source/logo.svg` | **Authoritative vector** cartoon avatar — red polo, scales infinitely; used for all **profile pictures** and favicons. |
| **Logo (transparent)** | `source/logo-transparent.svg` | Independently hand-maintained vector for transparent masters and overlays. It is not generated from `source/logo.svg`. |
| **Hero** | `source/hero.png` | Authoritative photoreal triple-monitor desk scene — red polo, used for all **banners / covers**. Raster only — photoreal imagery can't meaningfully vectorize. |
| **Original logo raster** | `source/logo.png` | Provenance and optional raster-to-vector trace input. It is copied to `logo/logo-original.png`, but normal logo rendering never uses it as a fallback. |

> The logo (flat illustration) and the hero (photoreal render) are intentionally
> kept as separate assets rather than composited together — mixing the two styles
> in one frame reads as amateurish. Each platform therefore gets a logo-based
> profile picture **and** a hero-based banner.

## Per-platform assets

Drop these straight into each platform's upload dialog — they're already at the
documented target pixel dimensions recorded in the requirement provenance below.
Those targets are platform-specific recommendations or upload minima, not a
promise that a platform will never crop or resize an upload. Consumers can use
the generated
[`platform-assets.json`](platform-assets.json) manifest instead of parsing this
table; it records each platform, role, path, dimensions, source asset, external
requirement URL, and last-verified date.

| Platform | Profile picture | Banner / cover |
|---|---|---|
| X / Twitter | `avatars/x-400.png` (400×400) | `banners/x-header-1500x500.png` (1500×500) |
| LinkedIn (personal) | `avatars/linkedin-400.png` (400×400) | `banners/linkedin-personal-1584x396.png` (1584×396) |
| LinkedIn (company) | `avatars/linkedin-400.png` (400×400) | `banners/linkedin-company-1128x191.png` (1128×191) |
| GitHub | `avatars/github-460.png` (460×460) | `banners/github-social-1280x640.png` (1280×640, repo social preview) |
| Instagram | `avatars/instagram-320.png` (320×320) | — (no banner) |
| Threads | `avatars/threads-320.png` (320×320) | — |
| Facebook | `avatars/facebook-320.png` (320×320) | `banners/facebook-cover-851x315.png` (851×315) · `banners/facebook-cover-2x-1702x630.png` (1702×630) |
| YouTube | `avatars/youtube-800.png` (800×800) | `banners/youtube-banner-2560x1440.png` (2560×1440, TV-safe) |
| TikTok | `avatars/tiktok-200.png` (200×200) | — |
| Mastodon | `avatars/mastodon-400.png` (400×400) | use `banners/open-graph-1200x630.png` (1200×630) |
| Bluesky | `avatars/bluesky-400.png` (400×400) | use `banners/twitter-card-1200x628.png` (1200×628) |
| Discord | `avatars/discord-512.png` (512×512) | `banners/discord-banner-960x540.png` (960×540) |
| Web / Open Graph | `favicon/` set | `banners/open-graph-1200x630.png` (1200×630) · `banners/twitter-card-1200x628.png` (1200×628) |

### Favicons (`favicon/`)

`favicon/favicon.ico` (16×16, 32×32, 48×48, 64×64, 128×128, 256×256),
`favicon/favicon-16.png` (16×16), `favicon/favicon-32.png` (32×32),
`favicon/favicon-48.png` (48×48), `favicon/favicon-192.png` (192×192),
`favicon/favicon-512.png` (512×512), and
`favicon/apple-touch-icon-180.png` (180×180).

### Logo masters (`logo/`)

`logo.svg` (vector — scale to any size) plus pre-rendered `logo-256/512/1024.png`
and `logo-original.png` (the 640² raster). Use the SVG when a platform isn't
listed above or you need a custom/large size; it never pixelates.

**Transparent variants** (`logo-*-transparent.png` and `logo-transparent.svg`) are
included for overlay use on colored backgrounds, dark surfaces, or print layouts
where the Canvas Cream background should not be baked in. These have full alpha
channels and can be composited onto any surface.

### Platform requirement provenance

`platform-assets.json` keeps one provenance record for every platform named in
the asset table. The URL is the first-party upload guidance reviewed for the
listed target; `last_verified` is the date that guidance was opened and
compared with the README table and generated files. A source may describe a
minimum, a recommendation, or a role-specific surface, so read the linked
page before treating a dimension as a hard limit.

| Platform | Requirement source | Last verified |
|---|---|---|
| X / Twitter | <https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo> | 2026-09-27 |
| LinkedIn (personal) | <https://www.linkedin.com/help/linkedin/answer/a549049> | 2026-09-27 |
| LinkedIn (company) | <https://www.linkedin.com/help/linkedin/answer/a417335> | 2026-09-27 |
| GitHub | <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview> | 2026-09-27 |
| Instagram | <https://help.instagram.com/> | 2026-09-27 |
| Threads | <https://help.instagram.com/> | 2026-09-27 |
| Facebook | <https://www.facebook.com/help/163248423739693> | 2026-09-27 |
| YouTube | <https://support.google.com/youtube/answer/10456525?hl=en> | 2026-09-27 |
| TikTok | <https://support.tiktok.com/en/getting-started/setting-up-your-profile/editing-your-profile> | 2026-09-27 |
| Mastodon | <https://docs.joinmastodon.org/user/profile/> | 2026-09-27 |
| Bluesky | <https://docs.bsky.app/docs/api/app-bsky-actor-profile> | 2026-09-27 |
| Discord | <https://support.discord.com/hc/en-us/articles/4403147417623-Custom-Profiles> | 2026-09-27 |
| Web / Open Graph | <https://ogp.me/> | 2026-09-27 |

### Reviewing changed upload requirements

The default freshness check is intentionally local and deterministic. The
expiry policy is 180 calendar days: a record remains current through day 180
and expires after that. The scheduled Argo check also probes every valid HTTPS
source once per day; it does not silently turn a network failure or a changed
web page into a passing result. Run the deterministic check during release
review and at least every 180 days:

```bash
python3 tools/check_platform_requirements.py
```

To perform the live source check manually, use the same command as the
scheduled workflow:

```bash
python3 tools/check_platform_requirements.py \
  --check-reachability --max-age-days 180 --timeout-seconds 15
```

The command exits `0` only when metadata is current and every source returns
HTTP 2xx/3xx. Metadata errors and HTTP failures exit `1`; DNS, TLS, timeout,
and connection failures exit `2` as `INDETERMINATE`, so an outage cannot look
like a pass. The reachability mode is intentionally opt-in so the normal test
suite remains deterministic.

When it reports an overdue platform, open that platform's linked source and
compare each applicable profile, banner, cover, or favicon target with the
current upload guidance. If the page has moved, changed a minimum or
recommendation, changed a role's surface, or is no longer authoritative:

1. Open the linked first-party source. If it moved, update the URL in
   `tools/build_assets.py`; if a target changed, update the dimensions there,
   the matching README row, and generated assets together.
2. Set that platform's `last_verified` date to the review date only after the
   external comparison is complete. Do not merely bump the date to silence the
   freshness check.
3. Run both checker modes, `.venv/bin/python tools/verify_assets.py`, and the
   full test suite; review the generated-image diff before committing. If the
   live mode exits `2`, retry once connectivity is restored rather than
   recording a new verification date from an indeterminate result.

For a deterministic audit of an older review, pass `--as-of YYYY-MM-DD`; use
`--max-age-days N` only when the review cadence is intentionally different.

## Palette

| Name | Hex | Use |
|---|---|---|
| Polo Red | `#DC3127` | Primary brand color — accents, links, highlights |
| Ink | `#0A0A08` | Outlines, text on light surfaces |
| Canvas Cream | `#EFDECC` | Logo background, light surfaces |
| Skin Tan | `#F5B079` | Illustration only |
| Control-Room Black | `#070506` | Dark surfaces, banner backdrop |

The machine-readable palette is [`palette.json`](palette.json), generated by
`tools/build_assets.py` and verified against this canonical table by
`tools/verify_assets.py`. The verifier requires every documented name and exact
six-digit hex value and rejects missing, extra, or malformed entries, so
consumers do not need to copy values from prose.

### Hero layout regression fixtures

`test_build_assets.py::test_documented_hero_crops_match_committed_fixtures` pins
the three hero layouts that share source text or focal content:

| Asset | Generator setting | Crop box after scale-to-cover |
|---|---|---|
| `banners/open-graph-1200x630.png` | 1200×630, `fy=0.45` | `(0, 76, 1200, 706)` |
| `banners/twitter-card-1200x628.png` | 1200×628, `fy=0.45` | `(0, 77, 1200, 705)` |
| `banners/youtube-banner-2560x1440.png` | 2560×1440, `fy=0.45` | `(0, 120, 2560, 1560)` |

The committed PNGs are the expected image fixtures. The test also pins each
decoded RGB-pixel digest, so PNG encoder differences cannot hide a visual or crop
change. YouTube's asset uses the [TV-recommended 2560×1440 size](https://support.google.com/youtube/answer/10456525?hl=en);
its exact-pixel fixture keeps the centered subject and monitor-bank composition
under review even though YouTube crops the full banner differently across views.

For an intentional hero change:

1. Edit `source/hero.png` or the relevant `tools/build_assets.py` crop setting.
2. Run the pinned build and verifier commands below.
3. Review the Open Graph, Twitter-card, and YouTube outputs at their actual
   platform dimensions before accepting a composition change.
4. With the pinned Pillow environment, run
   `.venv/bin/python -m pytest -q test_build_assets.py -k documented_hero_crops`.
   If the intended pixels changed, replace that case's `pixel_sha256` value in
   `HERO_CROP_FIXTURES` with the actual digest printed in the assertion. If the
   layout changed intentionally, update its settings, scaled size, crop box, and
   the table above in the same review.
5. Run `.venv/bin/python -m pytest -q` and commit the source, generated PNGs,
   generator settings, documentation, and fixture expectations together. Do not
   refresh a digest merely to make a test pass.

## Regenerating

First complete the one-time pinned toolchain setup documented in
[docs/notes/asset-toolchain.md](docs/notes/asset-toolchain.md):

```bash
cargo install --locked resvg@0.47.0 vtracer@0.6.5
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2
```

For a normal edit to `source/logo.svg`, `source/logo-transparent.svg`, or
`source/hero.png`, **do not trace the raster**. Edit the authoritative source,
then run:

```bash
.venv/bin/python tools/build_assets.py
.venv/bin/python tools/verify_assets.py
```

`source/logo.svg.sha256` is the integrity sidecar for the authoritative SVG.
The verifier rejects a missing, malformed, or stale sidecar, so whenever the
bytes of `source/logo.svg` change—including a direct edit—refresh the sidecar
in the same commit before running the verifier:

```bash
sha256sum source/logo.svg | awk '{print $1}' > source/logo.svg.sha256
```

`trace_logo.py` writes the refreshed sidecar automatically after a successful
trace. Do not refresh it merely to hide an unexpected SVG change; review the
SVG and its generated assets first.

`source/logo.png.sha256` and `source/hero.png.sha256` are the matching bare
SHA-256 sidecars for the preserved logo raster and authoritative hero raster.
`verify_assets.py` rejects either sidecar when it is missing, malformed, or
stale. The build copies `source/logo.png` byte-for-byte to
`logo/logo-original.png`, and the verifier checks that exact-copy contract;
Pillow must not decode or re-encode the preserved original. When either raster
source changes intentionally, refresh its sidecar in the same commit:

```bash
sha256sum source/logo.png | awk '{print $1}' > source/logo.png.sha256
sha256sum source/hero.png | awk '{print $1}' > source/hero.png.sha256
```

Run `tools/build_assets.py` before `tools/verify_assets.py` so the original
logo copy and all hero-derived assets are updated before the integrity checks.
Do not refresh a digest merely to hide an unexpected raster change; review the
source, exact logo copy, and regenerated assets first.

`build_assets.py` renders every opaque logo asset straight from
`source/logo.svg` at its target size with `resvg`; it never falls back to
`source/logo.png`. It also renders transparent assets from the independent
`source/logo-transparent.svg` and banners from `source/hero.png`. Review the
resulting diff and commit it. When checking an already-generated commit, the
same commands must leave `git diff --exit-code` empty.

`source/logo.png` is an input to the normal build only for
`logo/logo-original.png`. If a PNG-only change is meant to update that original
raster, skip the trace and use the normal build commands above. Replacing the
authoritative opaque SVG from that raster is a separate, explicit transition:

1. Replace `source/logo.png`.
2. Run the trace:

   ```bash
   .venv/bin/python tools/trace_logo.py
   ```

3. Review the traced `source/logo.svg`. If transparent artwork changed, edit
   `source/logo-transparent.svg` separately.
4. Run the build and verification commands:

   ```bash
   .venv/bin/python tools/build_assets.py
   .venv/bin/python tools/verify_assets.py
   ```

5. Refresh `source/logo.png.sha256`, review the complete diff, and commit the
   traced SVG, updated checksums, sources, raster sidecars, and generated assets
   together.

The trace writes only `source/logo.svg` and `source/logo.svg.sha256`.
`build_assets.py` does not read any checksum, while `verify_assets.py` checks
the SVG sidecars, raster sidecars, and exact preserved-logo copy. The raster
sidecars are updated explicitly as part of the intentional-change procedure
above.

Before tracing, and again before replacing the SVG, `trace_logo.py` requires the
current SVG to match the recorded digest. A mismatch aborts and protects a
committed hand edit from an unintended raster replacement. Use
`.venv/bin/python tools/trace_logo.py --force` only when the current SVG must be
discarded and regenerated from `source/logo.png`; then continue with the build
and verification steps above.

An alternate desk composition (`source/hero-alt.png`) was removed from the repo —
nothing consumed it, so it was 2 MB of dead weight per clone. If the hero ever
needs replacing, recover it from git history:
`git show dc5beac:source/hero-alt.png > source/hero-alt.png`.

**Not compatible with CI: post-processing with oxipng.** Running
[oxipng](https://github.com/shssoichiro/oxipng) over the generated assets would
shrink banners by 30–50%, but the committed bytes would then differ from what
`build_assets.py` produces — the regen-diff would fail on every push. Size
optimization happens inside the script (`optimize=True` on every save); if you
want a stronger compressor, it has to become part of the pinned, CI-replicated
pipeline instead of a manual after-step.

## CI regression gate

The Argo `brand-kit-ci` WorkflowTemplate in `jedarden/declarative-config`
(`k8s/iad-ci/argo-workflows/brand-kit-ci-workflowtemplate.yml`) applies the
pinned toolchain and runs this acceptance sequence in order:

1. Run `python3 tools/check_asset_toolchain.py` against the checked-out
   WorkflowTemplate. This parses the pin table and fails on any tool,
   version, install-command, or Pillow wheel-flavor mismatch.
2. Install `resvg@0.47.0` and `vtracer@0.6.5` with
   `cargo install --locked resvg@0.47.0 vtracer@0.6.5`, create the pinned virtualenv,
   and run `.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1 pytest==9.0.2`.
3. Run `.venv/bin/python tools/verify_assets.py`. It must exit `0`, including the exact
   generated-asset inventory, authoritative-source sidecar, and preserved-logo
   byte-copy checks, so missing or unexpected generated files or changed source
   bytes fail the workflow.
4. Run the full default suite with `.venv/bin/python -m pytest -q`. Any test failure
   exits non-zero and fails the workflow, including the contract tests in
   `test_check_asset_toolchain.py`.
5. Run `python3 tools/check_reproducibility.py`. It creates two independent
   clean Cargo/Python environments, exercises the vtracer trace and the full
   resvg/Pillow build, compares every generated file with the committed tree,
   and fails on any transitive dependency or byte drift.
6. Run `.venv/bin/python tools/build_assets.py` to regenerate every derived asset.
7. Run `git diff --exit-code --quiet`. A regenerated tracked-file difference
   exits non-zero and fails the workflow.

The container uses `set -ex`, so an install, verification, test, build, or diff
failure aborts the Argo run. Acceptance for a revision therefore requires an
Argo phase of `Succeeded` plus logs showing the verifier, full pytest, build,
and final no-drift check all reached exit `0`. A local test pass or the mere
presence of the WorkflowTemplate is not CI acceptance evidence.

### Triggering an exact commit

`brand-kit-ci` is submitted manually through the Argo Workflows API. The
operator-facing command is `tools/brand_kit_ci_submit.py`; it requires a full
commit SHA and passes it as the immutable `revision` parameter to the
WorkflowTemplate. The template checks out that revision detached and exposes
the resulting full SHA as its structured `commit` output. A branch name alone
is not a valid release gate because it can move while a queued workflow is
waiting for capacity.

For a pushed commit on `main` (including the commit being prepared for a
release), run:

```bash
COMMIT="$(git rev-parse --verify HEAD^{commit})"
.venv/bin/python tools/brand_kit_ci_submit.py \
  --commit "$COMMIT" \
  --branch main
```

The command uses `ARGO_SUBMIT_TOKEN` from the release-token provisioning
contract; credentials are environment-only and never command-line options.
Record the returned workflow name, wait for `Succeeded`, and confirm its
structured `commit` output equals `$COMMIT` before running the release
publisher. The canonical template and any Argo Event Sensor must preserve the
same `revision` value; a submission that drops it or returns another revision
is rejected by the submitter and by the release publisher's exact-commit
attestation.

The application-owned `brand-kit-ci-failure-watch` companion in
`automation/` checks the Argo API every 15 minutes for `Failed` or `Error`
`brand-kit-ci` runs from the preceding two hours. That lookback covers the
cluster's two-hour `secondsAfterFailure` Workflow retention window, so the gate
failure is reported before Argo reaps its record. The retention/lookback
contract is encoded in the WorkflowTemplate annotations and checked by
`python3 tools/check_ci_failure_watch_retention.py`; the watcher also rejects
an unsafe shorter runtime window. The watcher uses the existing read-only
`ARGO_TOKEN` from `brand-kit-release-tokens`, keeps a sanitized report at
`failures/brand-kit-ci-failure-watch/v1/<workflow-uid>/report.json` with
`artifactGC: Never`, and its exit handler posts a `BrandKitCIRegressionGate`
alert to the iad-ci Alertmanager. Alertmanager's configured ntfy receiver
delivers that alert to the `jedarden` owner channel. A watcher run is
non-success when it finds a failed gate or cannot inspect Argo; both cases
require follow-up, and the durable report is the first place to look.
The deployment path, live-object inspection commands, and read-only parity
check for these two manifests are documented in
[`docs/notes/asset-toolchain.md`](docs/notes/asset-toolchain.md#deployment-path-and-live-parity).
That parity check first verifies the owning `brand-kit-automation-iad-ci`
Application is correctly wired to the GitHub mirror, `main`, `automation`,
and `argo-workflows`, and is `Synced` and `Healthy`; it then compares the
child objects using read-only `kubectl get` calls and never applies resources.

The same watcher archives recent successful runs as durable CI attestations at
`attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json`. The
object envelope contains the watcher UID, and every record contains the exact
`brand-kit-ci` workflow name, workflow UID, full commit, `Succeeded` phase, and
timezone-qualified completion time. The UID-scoped object address is treated
as write-once evidence: Argo never garbage-collects it, the retention pass
only deletes expired unheld objects, and a release-evidence record holds its
attestation URL indefinitely. Before accepting a fallback URL after Argo
returns `404`, `tools/release_publish.py` requires the exact HTTPS object path
(no query, fragment, credentials, alternate filename, or encoded path), checks
the envelope UID against that path, validates every record, and requires one
and only one record matching both the requested commit and workflow name.
Attestations without a release-evidence hold are retained for 30 days, and
the scheduled watcher runs `tools/prune_ci_attestations.py` before inspection;
preserve an unheld object in an approved recovery location before that window
expires if a longer investigation is required.

### Automation manifest contract

Before deploying any file under `automation/`, run:

```bash
python3 tools/check_automation_manifests.py
```

This recursively validates every `.yml` and `.yaml` file against
`automation/manifest.schema.json`. The contract requires Argo's
`WorkflowTemplate` or `CronWorkflow` kinds, the `argoproj.io/v1alpha1` API,
the `argo-workflows` namespace, filename/name and template-reference parity,
version-pinned container images, declared parameters, valid UTC schedules, and
one matching scheduled pair for each automation. No Deployment, Service,
Ingress, or other long-running Kubernetes resource can pass this check. When
the declarative-config checkout is available, include its deployment wiring as
an additional check:

```bash
python3 tools/check_automation_manifests.py \
  --application ../declarative-config/k8s/iad-ci/argo-workflows/brand-kit-automation-application.yml
```

That application check requires the recursive `automation` source, both YAML
include patterns, and an `argo-workflows` destination, preserving the boundary
between this repository's ephemeral Argo automation and serving workloads.

For a failed gate, inspect the report and the referenced workflow, identify the
exact commit and failed stage, correct the pin/install contract, test/assets,
unexpected generated file, or regeneration drift, and push a new commit.
Rerun the gate for that commit and wait for `Succeeded` before release
publication. If the report says the watcher could not inspect Argo, repair the
API/credential or cluster condition and rerun the same check; do not treat a
missing report as a passing gate.

The separate `brand-kit-workflow-liveness` watchdog closes the gap where a
scheduled detector never starts and therefore cannot run its own exit handler.
Its CronWorkflow runs every 15 minutes and checks the latest successful
`brand-kit-ci-failure-watch` run (fresh within 60 minutes) and
`brand-kit-consumer-drift` run (fresh within 48 hours), plus the scheduled
`brand-kit-mirror-health` run (fresh within 12 hours). A stale or indeterminate
check posts a `BrandKitWorkflowLiveness` Alertmanager notification and retains
the tri-state report at
`failures/brand-kit-workflow-liveness/v1/<workflow-uid>/report.json`. The
WorkflowTemplate and CronWorkflow are sourced from
`automation/brand-kit-workflow-liveness-workflowtemplate.yml` and
`automation/brand-kit-workflow-liveness-cronworkflow.yml`; the checker source
is `tools/brand_kit_workflow_liveness.py`.

The independent `brand-kit-mirror-health` CronWorkflow runs every six hours.
`tools/check_mirror_health.py` reads every `refs/heads/*` and `refs/tags/*`
from canonical Forgejo and the read-only GitHub mirror, reporting missing,
stale, or divergent refs without pushing or changing either repository. It
retains its JSON report at
`failures/brand-kit-mirror-health/v1/<workflow-uid>/report.json` and its
`BrandKitMirrorHealth` exit-handler alert routes to the `jedarden` owner
channel. A healthy report requires exact parity for all branches and tags;
an unavailable remote or inconclusive ancestry comparison is indeterminate and
also fails closed.

The release handoff has an additional exact-tag audit in
`tools/check_release_mirror.py`. It reads only the peeled Forgejo and GitHub
tag refs, treats transient mirror lag as indeterminate, and never queries or
creates a GitHub Release; Forgejo remains the only release authority.

## Forgejo release publication

A pushed tag is not a release. After the Argo `brand-kit-ci` run for the exact
release commit succeeds, use the repeatable publisher documented in
[`docs/notes/release-publication.md`](docs/notes/release-publication.md):

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<successful-run-id>"
```

The publisher performs a read-only Argo API attestation before any Forgejo
write: the named run must be `Succeeded` and expose the exact release commit as
a structured full-SHA output. It then requires the unique, non-empty matching
`CHANGELOG.md` section, the exact annotated `vX.Y.Z` tag at `HEAD`, and a
Forgejo Release whose tag, full target commit, and body exactly match those
inputs. It creates or reuses the non-draft release and confirms that the
canonical Forgejo tag and the read-only GitHub mirror advertise the same
peeled commit. It is idempotent and never creates a GitHub Release. Set
`FORGEJO_TOKEN` and `ARGO_TOKEN` using the
[`release-token-provisioning.md`](docs/notes/release-token-provisioning.md)
contract; both are required and are never accepted as command-line options.
Wait for its `READY` result and run the read-only `--verify-only` form before
starting the consumer workflow below. Forgejo remains the sole release
authority; the mirrored Git tag is only a distribution path.

The scheduled `brand-kit-ci-failure-watch` also archives recent successful-run
attestations in the non-GC'd Garage bucket at
`attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json`. If the
named Argo Workflow has been reaped before publication, rerun the publisher
with `--ci-attestation-url <artifact-url>`. The fallback is accepted only for
an Argo `404` and validates the exact commit, `Succeeded` phase, workflow UID,
and timezone-qualified `finished_at`; a live failed run or ambiguous artifact
still blocks publication. The complete recovery procedure is in
[`release-publication.md`](docs/notes/release-publication.md).

The same token contract is checked before release day by the daily
`brand-kit-release-token-probe` Argo `CronWorkflow`, sourced from
`automation/brand-kit-release-token-probe-cronworkflow.yml`. Its
`tools/release_token_probe.py` checks Forgejo authentication/read access, Argo
read access, and the consumer-template submit permission with Argo server
dry-run; it never publishes a release or creates a workflow. Failures use the
same Alertmanager/ntfy owner follow-up path as the consumer-drift alert and
retain a non-GC'd sanitized report, including when a deployment Secret/key is
missing. The owner repairs or rotates the named credential, reruns the probe
until all checks pass, and reruns the publisher's read-only gate before
resuming release or consumer work. The alert and report never contain token
values. The rotation and deployment-secret requirements are documented in
[`release-token-provisioning.md`](docs/notes/release-token-provisioning.md).

## Downstream consumers (post-tag sync)

Copies of these assets live outside this repo — `jedarden.com/public/brand/`
(logo copies + recompressed hero JPEGs), jedarden.com's site-owned favicon
outputs, the GitHub profile avatar, and the hand-placed platform profiles listed
in the table above. CI here can't see them go stale, so the repository ships a
read-only detector as well as the remediation helper.
After publishing a Forgejo Release, check out
its exact tag and run:

```bash
VERSION=vX.Y.Z
.venv/bin/python tools/consumer_drift.py --release-tag "$VERSION" \
  --site ~/jedarden.com --json
```

`tools/consumer_drift.py` uses the published Forgejo release record, reports
SHA-256 digests for canonical inputs, compares the eight documented
jedarden.com outputs (two logos, two hero JPEGs, and four favicon outputs), and
compares live media for the OG card, GitHub avatar, X profile avatar/header, and
LinkedIn personal/company profile media. X and LinkedIn rules fetch each stable
public profile URL and extract the current CDN media URL before comparing it;
the CDN URL itself is intentionally not pinned because it changes after an
upload. It never applies changes, commits, or pushes. Exit `0` means every
configured check is current, `1` means confirmed stale consumers, and `2` means
the audit was indeterminate (for example, a network or release-record failure).
Set
`FORGEJO_TOKEN` to a read-only Forgejo API token when the instance requires
authentication. `--print-release-tag` resolves the newest stable published
release for a scheduler. If the detector is newer than the tag under audit, use
`--source-root <release-checkout>` so the tool code stays current while the
compared inputs come from the published tag. A green result is not a claim that
every upload in the per-platform table was checked: the machine-readable
`consumer-drift.json` `out_of_scope` list is part of the audit contract and is
also included in JSON reports.

### Platform coverage boundary

The detector checks only surfaces that expose a stable public profile page and
retrievable media without credentials: X (`x.com/jedardencodes`) and LinkedIn
(`linkedin.com/in/jed-arden` and `linkedin.com/company/runsybil`). The profile
page is the stable locator; its current media URL is discovered at audit time.
The existing checks cover the public jedarden.com OG card and GitHub avatar.

The following documented uploads remain manual and deliberately out of scope:

| Platform surface | Why the detector does not check it |
|---|---|
| Instagram, Threads, and TikTok profile pictures | No stable unauthenticated media URL is exposed. |
| Facebook profile picture and covers | No registered public page URL; cover media is dynamic or login-gated. |
| YouTube profile picture and TV-safe banner | No documented channel URL; public pages are consent/client dependent. |
| Mastodon profile picture/banner | No documented instance and account URL. |
| Bluesky profile picture/banner | No documented handle; public API media URLs are instance-specific blobs. |
| Discord profile picture/banner | User media requires an authenticated API or client session. |
| GitHub repository social preview banner | The upload has no stable public URL that can be compared to the source. |

These exclusions are not silent: each reason is represented in
`consumer-drift.json`, and the detector reports the count of excluded platform
groups alongside its configured checks.

Machine-readable consumer registration lives in
[`consumer_registry.json`](consumer_registry.json). It names each consumer's
checkout, provenance manifest, copied/derived files, site-owned related files,
and optional live checks. `consumer_sync.py --apply` writes the manifest with
the exact brand-kit commit, release tag when available, transforms, and
per-file SHA-256 digests; `--check` reports missing or stale manifests. Future
consumers add an entry to this registry, commit the generated manifest in their
own checkout, and run the script with `--consumer <id> --site <checkout>`.

Favicon generation remains site-owned: after `consumer_sync.py --apply` refreshes
a changed logo, run `node scripts/make-favicons.mjs` from jedarden.com before
committing its site diff. The command uses that repository's Sharp toolchain;
`consumer_drift.py` verifies `favicon.svg`, `apple-touch-icon.png`,
`icon-192.png`, and `icon-512.png` against the tagged brand-kit derivatives.

The release-triggered Argo handoff is
`.venv/bin/python tools/consumer_drift_submit.py --release-tag "$VERSION"`.
It requires a published, verifiable Forgejo release, waits for the exact tag to
reach the read-only mirror, and submits that tag to
`automation/brand-kit-consumer-drift-workflowtemplate.yml`. The daily Argo
`CronWorkflow` source remains
`automation/brand-kit-consumer-drift-cronworkflow.yml` as a fallback; it clones
the newest stable release at least 24 hours old whose tag is visible on the
read-only mirror, emits the JSON report, and fails its run on drift. It runs at
06:17 UTC daily. These workflows are intentionally not
GitHub Actions workflows and have no consumer-write step. The complete
remediation checklist — including the manual commit/push in `jedarden.com`, the
GitHub avatar re-upload (no API for it), release-gate failure behavior, and what
"in sync" means per asset — is in
**`docs/notes/post-tag-consumer-update.md`** (ADR-2 and ADR-4).

Non-success runs also invoke the WorkflowTemplate's exit handler, which posts a
`BrandKitConsumerDrift` alert to the iad-ci Alertmanager/ntfy owner channel.
The JSON report is retained as a non-GC'd Garage artifact at the URL included in
that alert, rather than remaining only under the ephemeral pod's `/tmp`. The
same WorkflowTemplate runs `tools/prune_consumer_drift_reports.py` before each
audit. Unreferenced reports are retained for 30 days (the exact cutoff remains
for the next pass); a report URL committed in `release-evidence/v1/*.json` is
an explicit retention hold and is never removed by this pass. The pruner
validates every release-evidence record before listing or deleting anything and
fails closed when evidence is missing, malformed, or points outside the
configured report prefix. A cleanup failure produces an indeterminate durable
report and owner alert, so an operator repairs the evidence or storage read
path and reruns the audit. Reports without a release-evidence hold may return
`404` after the 30-day window; copy one to an approved recovery location before
that window expires when a longer investigation needs it.

### Release evidence record

Every completed release also gets a credential-free record at
release-evidence/v1/<tag>.json. After the Forgejo publication and
consumer-drift handoff, tools/release_evidence.py persists the full release
commit, CI run and durable attestation URL, Forgejo Release,
canonical/mirror agreement, and consumer workflow handoff. The record is
versioned with both the v1 storage directory and schema_version: 1; its strict
contract and operator command are documented in
release-evidence/v1/README.md. Tokens and other credentials are never fields
in the record.

## Usage & rights

These are the personal brand assets of Jed Arden. The repository is public so the
assets are easy to reference and self-host, but the logo, likeness, and hero
imagery are **not** licensed for reuse, redistribution, or derivative works.
All rights reserved.
