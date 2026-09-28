# Brand Kit — Plan

No `plan.md` existed before this file. Rather than fabricate retroactive
history, this doc starts honestly on 2026-07-20, as part of a fleet-wide
"improve what's already shipped" review. For what the repo contains today,
see `README.md` (the source→derived asset table, palette, and regeneration
instructions) and `tools/build_assets.py` / `tools/trace_logo.py` (the actual
pipeline). This file will accumulate architecture decisions (ADRs) as the kit
evolves; it is not a forward-looking feature roadmap for what is a small,
low-churn asset repo.

## What this repo ships

Three authoritative artwork sources drive the platform assets:
`source/logo.svg` for the opaque vector logo, the independently maintained
`source/logo-transparent.svg` for transparent variants, and `source/hero.png`
for banners/covers. `source/logo.png` is preserved provenance, is copied
byte-for-byte to `logo/logo-original.png`, and may explicitly replace the
opaque SVG through
`tools/trace_logo.py`; it is not the source used for normal vector rendering
and there is no raster fallback. The build produces 37 asset files plus
`palette.json`. This repository ships two related surfaces: (1) the static
brand package as committed here, referenced directly by consumers or copied
into downstream repos, and (2) Argo automation that validates and observes
the package. The `brand-kit-ci` WorkflowTemplate is maintained in
`declarative-config`; this repository owns the reusable
`automation/` WorkflowTemplates and CronWorkflows for CI failure-watch,
consumer-drift, Forgejo-to-GitHub mirror health, release-token probing, and
scheduled-workflow liveness. These definitions create short-lived Argo
workflow pods, but they do not serve the assets. There is no serving API,
Deployment, Service, or Ingress in this repository.

### Ownership and deployment boundary

The distinction between application content and cluster automation is part of
the contract:

| Surface | Owner | Deployment boundary |
| --- | --- | --- |
| Authoritative artwork, generated assets, tools, and tests | `brand-kit` | Committed files are the distribution surface; consumers copy or reference them and own how they serve them. |
| `automation/` Argo WorkflowTemplates and CronWorkflows | `brand-kit` | The `brand-kit-automation-iad-ci` ArgoCD child Application in `declarative-config` reconciles only this directory into the shared `argo-workflows` namespace. |
| `brand-kit-ci` WorkflowTemplate, ArgoCD Application, and cluster placement | `declarative-config` | GitOps owns the CI template and reconciliation boundary; the resulting workflows run as ephemeral jobs in the shared Argo installation. |
| Consumer repositories and profile/platform uploads | Each consumer/platform | Consumer deployment and any remediation remain outside this repository; consumer-drift is read-only and reports drift rather than pushing changes. |

Thus “no k8s workload” is too broad: this repo participates in Kubernetes
through Argo workflow workloads. The precise statement is that it has no
long-running serving workload or API; its Kubernetes-facing resources are
ephemeral CI and read-only operational automation.

## ADR-1: 2026-07-20 — CI-verified regeneration and versioned releases as the distribution contract

### Context

`tools/build_assets.py` is the only thing standing between the authoritative
artwork sources and the 37 committed asset files. `tools/trace_logo.py` is a
separate raster-to-vector replacement operation, not part of the normal build.
At the time of this decision, nothing enforced that the committed assets
matched what the scripts produced from the current source — there was no CI in
this repo yet (no `.github/`, no Argo WorkflowTemplate). It was entirely
possible to hand-edit a derived PNG, or edit `source/logo.svg` without
re-running the build, and nothing caught the drift.

Separately, downstream consumers currently get brand assets by manual
copy-paste of whatever `main` looks like at the moment someone remembers to
sync. `jedarden.com/public/brand/` is the confirmed example: `logo.svg` and
`logo-512.png` happen to be byte-identical to this repo's current output
(both dated 2026-05-22, i.e. copied at the same commit that produced them),
but `hero.jpg` and `og.jpg` are hand-recompressed one-off derivatives with no
record of which brand-kit commit they came from. If `source/hero.png` changes
tomorrow, there is no mechanism — automated or even a checklist — that tells
`jedarden.com` (or any future consumer) that its copy is now stale. This is
exactly the kind of drift a personal brand kit is supposed to prevent by
existing as a single source of truth.

### Decision

1. Add a CI gate (Argo Workflow, per this workspace's Argo-only CI policy —
   GitHub Actions stay disabled) that installs the canonical pinned toolchain,
   runs `tools/verify_assets.py` and the full pytest suite, then runs
   `tools/build_assets.py` from the authoritative sources in a clean checkout.
   The gate fails on a failed install, test or asset verification, an unexpected
   generated file, or any regenerated tracked-file diff. The regen invariant is
   build-only: a raster diff does not change which source is authoritative, and
   an explicit raster replacement is committed before that build check. This
   turns "did you remember to re-run the checks and build script" from a
   trust-based README instruction into an enforced invariant.
2. Once that check is green, publish releases on Forgejo for commits that
   change `source/` or the derived output. A release is complete only when an
   annotated `vX.Y.Z` tag has been pushed to the canonical Forgejo remote and a
   Forgejo Release has been published from that existing tag. Consumers get a
   stable, citable reference ("brand-kit @ v1.0.0") instead of "whatever main
   was on the day I copied it."

Both parts are needed together: the CI check guarantees the *content* at any
given commit is internally consistent (source and derived output agree); the
tag plus the release record gives consumers something *stable* to point at and
a reason to notice when a new one exists.

### Release procedure

Release credential source, scope, injection, rotation, and fail-closed
behavior are defined in
[`docs/notes/release-token-provisioning.md`](../notes/release-token-provisioning.md).

`origin` is the canonical Forgejo repository at
`https://git.ardenone.com/jedarden/brand-kit.git`; `github` is its read-only,
server-side push mirror. The repeatable operator procedure is
[`docs/notes/release-publication.md`](notes/release-publication.md), backed by
`tools/release_publish.py`. It runs only after the `brand-kit-ci` Argo run for
the exact release commit has succeeded. The publisher performs a read-only
Argo API attestation of that run: it requires `Succeeded` plus a structured
full-SHA commit output matching the release commit, and fails closed when the
run is failed, missing, malformed, or for another commit.

First prepare `VERSION` and the matching changelog section according to
ADR-5, then derive and push the annotated tag:

```bash
VERSION="$(tr -d '\r\n' < VERSION)"
test "$(git show HEAD:VERSION)" = "$VERSION"
git tag -a "$VERSION" -m "brand-kit $VERSION"
git push origin "refs/tags/$VERSION"
```

Then publish and verify the exact tag with:

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<successful-run-id>"
```

The publisher refuses a tag that is not annotated, is not at `HEAD`, or is not
advertised by both remotes. It creates or reuses a non-draft Forgejo Release
from the exact tag, then queries Forgejo and both remotes again. A published
tag without the corresponding Forgejo Release is incomplete. Do not run `gh
release create` or push a release object to `github`: the server-side mirror
propagates the tag, but release objects are not mirrored and Forgejo remains
the canonical release record.

Before consumers start ADR-2, run the publisher's read-only
`--verify-only` form and require its `READY` result. The mirror's peeled tag is
only a distribution path; it is not a second release record.

> **Follow-through:** the consumer half of this contract — what to do after a
> Forgejo Release is published so downstream copies actually catch up — is
> ADR-2 (`docs/notes/post-tag-consumer-update.md` +
> `tools/consumer_sync.py`).

### Alternatives Considered

- **Status quo (manual regen, manual copy-paste downstream).** Rejected —
  already produced undocumented drift once (`jedarden.com`'s hand-recompressed
  `hero.jpg`/`og.jpg`); nothing scales past one consumer.
- **Git submodule in each consumer repo.** Rejected — heavier DX than this
  low-churn asset repo justifies, and this workspace's other repos don't use
  submodules; shallow-clone/detached-HEAD checkout patterns in CI make
  submodules a recurring source of friction elsewhere.
- **Publish as an npm/GitHub Packages package.** Rejected — only helps
  JS/Astro consumers like `jedarden.com`; does nothing for the GitHub avatar,
  X bio, LinkedIn banner, etc., which are set outside any build system
  regardless of how the files are packaged. Not worth standing up and
  maintaining a registry for ~30 static files that change rarely.
- **Create GitHub Releases through `gh release create`.** Rejected — GitHub is
  a read-only push mirror in this workspace, and a server-side Git push mirrors
  refs rather than release objects. Canonical releases therefore live only on
  Forgejo; their tags reach GitHub through the existing mirror.
- **CDN hotlink (e.g. jsDelivr against a GitHub tag) directly from consumer
  HTML, no local copy at all.** Deferred, not adopted as the primary
  mechanism — it would fully solve drift, but it adds an external runtime
  dependency for assets that change rarely, where a small vendored file is
  free. Worth reconsidering per-consumer later; it composes fine with tagged
  releases from this decision (a consumer could hotlink `@v1.0.0` instead of
  vendoring it).
- **CI regen-check with no versioning.** Rejected as insufficient alone —
  it keeps `main` internally consistent but still gives consumers no signal
  that anything changed or which version they're on.

### Consequences

- Any edit to `source/logo.svg`, `source/logo-transparent.svg`, or
  `source/hero.png` that isn't followed by re-running the build script and
  committing the results now gets caught by CI before it lands, instead of
  silently diverging.
- Consuming repos get an explicit version to pin to and a changelog to check,
  instead of an unrecorded copy-paste timestamp.
- Forgejo owns the release record while GitHub remains a read-only distribution
  mirror. Release publication adds one explicit web-UI step because a Git push
  cannot create a Forgejo Release object.
- Adds a CI dependency: an Argo WorkflowTemplate (in
  `jedarden/declarative-config`, `k8s/iad-ci/argo-workflows/`, per this
  workspace's convention) needs `resvg` + Pillow available in the build
  image.
- Adds process overhead: changes to `source/` now require a regenerate +
  verify step before merge, and periodic tagging discipline, where before it
  was "edit and push." Acceptable for a repo that changes a handful of times
  a year.
- Out of scope for this ADR (tracked separately, not as brand-kit beads,
  since it requires changes in other repos): actually migrating
  `jedarden.com` and any future consumer from ad hoc copy-paste to consuming
  a pinned brand-kit tag.

## ADR-2: 2026-09-15 — Post-tag consumer-update workflow: a checklist and script, not automation (remediation; detection is extended by ADR-4)

### Context

ADR-1 added the CI regen-check and versioned releases, but left its own stated
problem open on the consumer side: nothing — automated or checklist — tells a
consumer its copy is stale once a new Forgejo Release exists. The confirmed
consumers as of this ADR: `jedarden.com` holds four files (two byte-copies of
the logo masters under `public/brand/`, plus two hand-recompressed JPEG
derivatives of `source/hero.png`: the `/brand` OG card and the page hero at
`src/assets/brand-hero.jpg`), and the `github.com/jedarden` profile avatar is
a manual upload of `avatars/github-460.png` whose last verification
(2026-07-20) was recorded in prose and would have been silently out of date
forever. The first check run found real drift already: the site's
`logo-512.png` predates the v1.0.0 oxipng pass — pixel-identical but no longer
byte-identical, invisible to any eyeball check and unfixable by CI, which by
construction only sees this repo.

### Decision

Close the loop with a **documented checklist plus a helper script**, both in
this repo: `docs/notes/post-tag-consumer-update.md` and
`tools/consumer_sync.py`. After every published Forgejo Release that changes
`source/` or derived output, the runner: (1) `--apply`s the refresh —
byte-copies the logo masters into the jedarden.com checkout and regenerates both
hero JPEGs from `source/hero.png` at the exact crop/quality the live files were
verified to use (open-graph crop, quality 88); (2) commits and pushes in
jedarden.com with a `sync to brand-kit @vX.Y.Z` message, which is a convenient
human-readable summary; `consumer_registry.json` and the committed
`brand-kit-provenance.json` provide machine-readable provenance; (3) `--check`s
— comparing site copies
and fetching the live og.jpg and the live GitHub avatar (perceptual compare,
since GitHub recompresses on serve) — which must be all-PASS after the Pages
deploy;
(4) uploads the avatar by hand if the check says it drifted; (5) records the
verification in `CHANGELOG.md`.

Two sync semantics, deliberately: **byte-identical** for the logo copies
(pixels matching isn't the bar — provably-from-the-tag is, and byte equality
is what caught the oxipng drift), and **tolerance-based pixel compare** for
JPEG derivatives and the recompressed live avatar, where byte equality is
unachievable by nature.

The script verifies and refreshes files only; it never commits outside this
repo, and the avatar has no upload API at all. Those two steps stay manual on
purpose.

### Alternatives Considered

- **Automate the whole loop (CI job that pushes to jedarden.com after each
  published release).** Rejected — cross-repo push access from CI, a deploy
  trigger for a site from an asset repo's pipeline, and a failure surface far
  heavier than a once-or-twice-a-year checklist. The bottleneck is remembering
  the checklist exists, and the checklist now lives one `grep` from ADR-1.
- **Extend the CI regen-check to fetch consumers and verify them.** Rejected —
  CI should stay hermetic and repo-internal; consumer state is a
  post-release operational concern, and making the regen-check fail because a
  third-party site drifted couples two repos' health.
- **Subscribe consumers via tags only (document "watch releases").** Rejected
  as the whole mechanism — Forgejo watch notifications don't reach a checklist
  level of reliability for a repo with a release every few months, and they do
  nothing for the avatar, which is set outside any repo.
- **Do nothing until a second consumer appears.** Rejected — the drift is not
  hypothetical (found, in the very first `--check` run) and the fix is one
  script plus one page of documentation.

### Consequences

- Consumer drift is now detectable in one command
  (`python3 tools/consumer_sync.py --release-tag vX.Y.Z --check`) and fixable in
  two (`--apply`, then the printed commit/push commands), instead of by memory.
- The helper now performs a read-only Forgejo release-record lookup before any
  consumer read or write. A missing, draft, malformed, or inaccessible record
  fails closed; `--offline` does not bypass that release gate.
- The remediation workflow still depends on a human (or agent) to refresh and
  review `jedarden.com` after a published Forgejo Release; ADR-4 automates
  detection and reporting but deliberately does not cross that write boundary.
- The JPEG regeneration recipe (crop params, quality 88) is now encoded in the
  script instead of in the muscle memory that produced the originals.
- Avatar verification acquires a recorded trail in `CHANGELOG.md` rather than
  a one-off prose note in plan.md.
- Scope boundary maintained: this repo ships the checklist, remediation script,
  and read-only detector; actually applying the site refresh remains outside
  brand-kit beads per ADR-1's scope note.

## ADR-3: 2026-09-23 — SVG-first logo source and guarded raster trace

### Context

The repository's actual build had an SVG-first implementation:
`build_assets.py` renders opaque logo assets from `source/logo.svg`, while
`source/logo.png` is copied separately as `logo/logo-original.png`. The
documentation, however, presented `trace_logo.py` beside the normal build and
invited hand edits to `source/logo.svg`. That left two incompatible claims:
`source/logo.svg` was editable authoritative artwork, yet running the raster
tracer would overwrite it directly with no ownership check.

The transparent logo is a separate source of truth. `source/logo-transparent.svg`
is maintained by removing the background from the opaque artwork; neither script
derives it automatically.

### Decision

1. `source/logo.svg` is authoritative for the opaque logo. A normal SVG or hero
   edit runs `build_assets.py`, never `trace_logo.py`.
   `source/logo.png` remains preserved provenance, produces the original-raster
   master by byte-for-byte copy, and is an input only to an explicitly
   requested raster-to-vector replacement. `source/hero.png` is independently
   authoritative for banners. `source/logo-transparent.svg` is independently
   authoritative for transparent outputs.
2. The hand-edit order is: edit the authoritative source(s), run
   `build_assets.py`, run `verify_assets.py`, review the complete diff, and
   commit. Editing `source/logo-transparent.svg` follows the same order.
3. A PNG-only change that updates `logo/logo-original.png` does not trigger a
   trace. To replace the vector explicitly, the order is: replace
   `source/logo.png`; run `trace_logo.py`; review the traced SVG; update
   `source/logo-transparent.svg` separately if needed; run
   `build_assets.py` and `verify_assets.py`; review the complete diff; and
   commit all coupled changes. The trace is part of that explicit transition,
   not the clean-checkout regen invariant:

   ```bash
   .venv/bin/python tools/trace_logo.py
   ```

   ```bash
   .venv/bin/python tools/build_assets.py
   .venv/bin/python tools/verify_assets.py
   ```
4. `source/logo.svg.sha256` records the SHA-256 digest of the current
   authoritative SVG. `verify_assets.py` rejects a missing, malformed, or stale
   sidecar, and any direct SVG edit must update the sidecar in the same commit.
   `trace_logo.py` writes it automatically after a successful trace and refuses
   to overwrite an existing SVG whose recorded digest differs, checking that
   invariant again immediately before replacement. `build_assets.py` does not
   read the digest; the verifier owns the integrity check.
5. `source/logo.png.sha256` and `source/hero.png.sha256` record the bare
   SHA-256 digests of the preserved and authoritative raster sources.
   `verify_assets.py` rejects missing, malformed, or stale raster sidecars and
   rejects any byte difference between `source/logo.png` and
   `logo/logo-original.png`. An intentional raster change therefore updates
   the source, runs `build_assets.py`, refreshes the relevant sidecar, runs
   `verify_assets.py`, and commits the source, sidecar, exact copy, and
   regenerated outputs together.
6. Replacing a modified SVG from the raster is possible only through the
   explicit `.venv/bin/python tools/trace_logo.py --force` command. A normal
   trace gives vtracer a temporary output path; the authoritative SVG is not
   replaced unless that process exits successfully, after which the new digest
   is written.

### Consequences

- Hand-edited vector work has a separate safe path: authoritative SVG → build.
  Raster replacement is an explicit, reviewable transition, and a concurrent SVG
  edit is rechecked before the traced file replaces it.
- A malformed, missing, or stale digest fails the verification gate, and the
  ordinary trace path cannot silently discard an SVG modification whose digest
  has not been refreshed.
- `source/logo.png` is no longer ambiguously described as the logo source of
  truth, even though it remains build-visible for the byte-preserved
  original-raster master.
- Transparent artwork still requires an explicit edit because no trace derives
  it; this is visible in the documented order rather than hidden in an
  intermediate-output side effect.

## ADR-4: 2026-09-24 — Read-only release-triggered consumer drift detection

### Context

ADR-2 made stale consumer copies detectable with a helper command, but its
operational consequence still depended on a human remembering to run that
command. A release can therefore land successfully while
`jedarden.com/public/brand/`, its site-owned favicon outputs, the deployed OG
image, or the manually uploaded GitHub avatar remains on an older release. The
detector must answer the
question without granting the brand-kit pipeline permission to mutate another
repository or a platform profile.

### Decision

Ship `tools/consumer_drift.py` with the machine-readable inventory in
`consumer-drift.json`, and provide the reusable Argo
`automation/brand-kit-consumer-drift-workflowtemplate.yml` plus its daily
`automation/brand-kit-consumer-drift-cronworkflow.yml` fallback. The release
handoff uses `tools/consumer_drift_submit.py`: it accepts only a published,
verifiable Forgejo release, waits for the canonical and read-only mirror tags
to agree, and submits the exact `release-tag` workflow parameter. The
workflow then waits again for mirror visibility before checking out that exact
tag. The daily run resolves the newest published stable release after the
configured 24-hour propagation delay whose peeled tag is already visible on
the read-only GitHub mirror, checks out that tag, clones jedarden.com from its
read-only GitHub mirror, and runs the detector. It runs at 06:17 UTC and falls
back to the newest older stable release when the newest release tag is still
propagating.

The detector:

1. Resolves and validates the published Forgejo release record; a missing,
   draft, malformed, or inaccessible record is indeterminate.
2. Requires the brand-kit checkout to be the selected release tag and records
   SHA-256 digests for canonical inputs.
3. Byte-compares jedarden.com's logo copies and `favicon.svg`, compares its
   three favicon PNGs and two hero JPEGs against release-generated reference
   assets, compares the live OG image directly against the release crop,
   perceptually compares the known live GitHub avatar with the release avatar,
   and audits the public X and LinkedIn profile media by resolving each stable
   profile page to its current CDN image.
4. Emits human-readable output and a JSON report, returning `0` only for a
   fully current audit, `1` for confirmed stale consumers, and `2` for any
   indeterminate check. Network failures and unavailable live assets cannot be
   green.

The detector has no apply, commit, or push operation. Remediation remains the
ADR-2 checklist, including the site repository's review flow and the manual
GitHub avatar upload.

### Consequences

- A scheduled or release-triggered run creates an operational signal without
  cross-repository write credentials or automatic consumer mutation.
- The release tag, source digests, observed digests, image metrics, and
  consumer-level status are retained in the report for triage.
- The inventory is extensible: adding a future read-only audit means adding a
  path/URL rule to `consumer-drift.json`; adding a syncable checkout also means
  registering its relative copies and provenance path in
  `consumer_registry.json`. Neither expands the remediation authority of this
  repository beyond the explicitly registered checkout.
- Platform coverage is intentionally bounded. `consumer-drift.json` records
  documented uploads that remain out of scope when a platform has no stable
  unauthenticated media endpoint, no documented account URL, or only a
  manually configured upload with no public comparison URL. A current audit is
  therefore a pass for configured checks, not a claim that every platform row
  in the README was verified.
- A failed or indeterminate run is visible as a failed Argo workflow. The
  manifest is reconciled through the shared `declarative-config` ArgoCD
  application, while this repository remains the owner of the detector and its
  tests.

## ADR-5: 2026-09-27 — SemVer policy and canonical release version

### Context

ADR-1 established annotated tags and published Forgejo Releases as the
distribution contract, but it did not say how to choose the next version.
That left an asset repository's source-artwork changes, generated-toolchain
changes, and documentation-only changes indistinguishable at release time.
The release checklist also used a hand-written `VERSION=vX.Y.Z` placeholder,
which could drift from the changelog or the tag.

### Decision

1. The root [`VERSION`](../../VERSION) file is the single authoritative source
   for the current release version. It contains exactly one line in the form
   `vMAJOR.MINOR.PATCH`; at the time of this ADR its value is `v1.0.0`. The annotated Git tag,
   the `CHANGELOG.md` heading, and the Forgejo Release are consistency checks
   that must agree with `VERSION`, not alternate sources from which to invent a
   version. A release commit must contain the selected value in `VERSION`
   before it is tagged.
2. Select the next version by classifying the complete consumer-visible diff
   since the version in `VERSION`:

   - **MAJOR** is an incompatible asset contract change: removing or renaming
     an existing file, changing an existing asset's format, dimensions, or
     meaning in a way that can break a consumer, or making an incompatible
     manifest/schema change.
   - **MINOR** is a backward-compatible capability or artwork change: adding
     an asset or platform, adding compatible manifest fields, or changing
     authoritative source artwork (`source/logo*.svg` or `source/hero.png`)
     while preserving existing paths, formats, dimensions, and meanings.
   - **PATCH** is a backward-compatible maintenance change: changing a pinned
     generator/toolchain or its generation behavior (including regenerated
     bytes) without changing the published asset contract, correcting
     schema-compatible manifest metadata, or fixing release tooling and docs.
     A docs-only change does not require a release; if it is intentionally
     published as a stable release, it is a patch.

   If one change qualifies for more than one level, use the highest level.
   A source-artwork change is therefore minor even when it is visually small;
   a toolchain pin is patch only when it preserves the consumer contract; and
   a manifest change is major when it is schema-incompatible, minor when it
   adds compatible capability, and patch when it only corrects metadata.
3. Prepare the release commit in this order: classify the diff and choose the
   next SemVer value from `VERSION`; write that exact value to `VERSION`; move
   the completed entries from `CHANGELOG.md`'s `Unreleased` section into a
   unique `## [$VERSION] - YYYY-MM-DD` section and create a fresh `Unreleased`
   section; then run the exact-commit CI gate. Only after that commit passes CI
   may it receive the annotated tag and Forgejo Release. The release command
   reads `VERSION` into its shell variable, so the tag is derived from the
   committed file rather than typed a second time. Post-release operational
   verification may add notes later, but it never changes `VERSION` or the
   immutable tag.
4. A release is not required merely because `main` contains a docs-only or
   metadata-only change. Keep such work under `Unreleased` until there is a
   reason to publish it; if it is published, apply the patch rule above and
   follow the same file/changelog/tag ordering.

### Consequences

- A reviewer can determine the intended release from one file and verify that
  the tag, changelog, and Forgejo record all describe the same version.
- Consumers get predictable compatibility signals: artwork additions or
  revisions are minor, contract breaks are major, and maintenance/doc changes
  are patch-level or remain unreleased.
- Release preparation has one extra commit-time check: `VERSION`, the
  changelog section, and the release candidate must be updated together before
  CI and tagging.

## ADR-6: 2026-09-27 — Release-publication and consumer-drift handoff architecture

### Context

ADR-1 defined the intended release identity — an annotated tag plus a published
Forgejo Release — but the machinery that now enforces the distribution
boundary grew across the publication and consumer-audit tools. A server-side
GitHub mirror introduces propagation delay without mirroring Forgejo Release
objects. The release publisher therefore has to prove both the content that CI
checked and the exact tag visible at both remotes before it can publish or hand
off to consumers. The consumer audit also runs against network resources and
must distinguish a confirmed stale copy from an audit that could not obtain
enough evidence. Without recording those rules here, the operational notes
describe behavior that is easy to mistake for optional ceremony.

### Decision

1. **Forgejo is the sole release authority.** A complete release consists of
   the annotated `vX.Y.Z` tag on the release commit and one published Forgejo
   Release attached to that exact tag. `origin` is the canonical Forgejo
   remote and the only release-publication authority. GitHub is a read-only,
   server-side push mirror for Git refs: its peeled tag must agree with
   Forgejo's, but it must never receive a second GitHub Release object. A
   pushed tag without the matching published Forgejo record is incomplete.
2. **Publication is a fail-closed, idempotent gate.**
   `tools/release_publish.py` requires an annotated tag at `HEAD`, validates
   the canonical Forgejo and GitHub remote identities, requires the canonical
   and mirror `main` refs to agree, waits for the expected canonical and mirror
   peeled refs to agree, and reads the exact
   changelog section for the release body. Before any Forgejo write, it
   performs a read-only Argo attestation of the named `brand-kit-ci` run: the
   run must be `Succeeded` and expose a structured full commit SHA matching
   the release commit. The application-owned failure watcher retains the same
   run's exact name, commit/UID/phase/finished-time record in Garage, and the
   publisher may use its explicit `--ci-attestation-url` only after Argo
   returns `404` for a reaped run. The URL must identify one HTTPS
   `attestations.json` object under the UID-scoped v1 prefix; the envelope UID,
   schema, every entry, and the unique workflow-name/commit match are checked.
   Missing, failed, malformed, mismatched, or inaccessible tag, mirror, Argo,
   attestation, or Forgejo state blocks publication. The publisher reuses an
   exact existing record or publishes an
   exact matching draft, accepts a create race only after an exact reread, and
   verifies the record and both remotes again after the write. It never calls
   the GitHub Releases API or pushes the mirror itself.
   `artifactGC: Never` is only the handoff from Argo to Garage: the watcher
   separately retains unheld attestations for 30 days, runs a prefix-scoped
   pruner, and treats every attestation URL in release evidence as an
   indefinite hold. The pruner validates all evidence and fails closed before
   deleting an object.
3. **The consumer handoff uses the same identity and propagation gates.**
   `tools/consumer_drift_submit.py` first requires a published Forgejo record
   with a verifiable full target commit, revalidates the canonical Forgejo and
   GitHub remote identities, then requires canonical and read-only mirror
   `main` refs plus the exact release tags to agree before submitting the exact
   release tag to the Argo `brand-kit-consumer-drift` WorkflowTemplate. The
   workflow waits again for mirror visibility before checking out that tag.
   This duplicate visibility check is intentional: it protects both the
   submitting operator and the isolated workflow from starting against a
   missing, stale, or partially propagated mirror.
4. **Consumer drift is read-only and tri-state.**
   `tools/consumer_drift.py` audits the selected release checkout and the
   registered consumer/live assets without applying, committing, or pushing
   anything. Exit `0` means every required check is current; exit `1` means
   confirmed consumer drift; exit `2` means the audit is indeterminate because
   the release record, checkout, configuration, network, or a required live
   resource could not be verified. Network failures, unavailable live assets,
   and skipped checks must never be reported as a pass. Remediation remains
   the ADR-2 checklist and the consumer repository's own review boundary.
5. **The daily CronWorkflow is an eventual-consistency fallback.**
   `automation/brand-kit-consumer-drift-cronworkflow.yml` remains enabled at
   06:17 UTC. With the configured 24-hour minimum release age, discovery
   considers only published, stable SemVer releases whose peeled tags are
   visible on the read-only GitHub mirror, and selects the newest eligible
   release. If the newest release is still propagating, it audits the newest
   older eligible release instead of treating temporary propagation as a
   consumer failure. The release-triggered workflow is the fast path; the
   daily run provides recovery when that handoff is delayed or missed.
6. **A non-success audit is routed to the owner and retains evidence.**
   The WorkflowTemplate's `onExit` handler posts a `BrandKitConsumerDrift`
   alert to the iad-ci Alertmanager endpoint, whose configured ntfy receiver is
   the `jedarden` owner channel. Exit `1`, exit `2`, and pre-detector failures
   all notify; the handler is best-effort and never masks the original failed
   workflow. The JSON report is uploaded to the shared Garage artifact bucket
   under `failures/brand-kit-consumer-drift/v1/<workflow-uid>/report.json` with
   `artifactGC: Never`, and the alert links to its public S3 URL. The report
   therefore remains available after the pod and short-lived Argo Workflow are
   removed. The same WorkflowTemplate runs a separate 30-day pruner for
   unreferenced consumer reports. Every non-null `consumer_drift.report_url`
   in `release-evidence/v1/*.json` is a retention hold; the pruner validates
   all evidence before listing or deleting and fails closed on incomplete
   evidence, so a held report remains recoverable. Unheld reports may be
   copied to an approved recovery location before expiry when longer
   investigation is required.

### Alternatives Considered

- **Create a second GitHub Release.** Rejected — the mirror propagates Git
  refs, not Forgejo Release objects, and two release records would create
  divergent authority, notes, and recovery semantics.
- **Start consumer work from the tag push or webhook alone.** Rejected — the
  mirror can lag the canonical tag, and a tag does not prove that CI checked
  the exact release commit or that a published Forgejo record exists.
- **Treat network failures or unavailable live assets as a pass.** Rejected —
  an audit that cannot observe a required resource has no evidence of current
  state; exit `2` preserves that distinction for operators and schedulers.
- **Let the detector refresh or commit consumers automatically.** Rejected —
  read-only detection avoids cross-repository write credentials, preserves the
  consumer's review and deployment flow, and keeps manual avatar management
  outside this repository's authority.

### Consequences

- Every publication and consumer audit has one immutable identity: the exact
  annotated release tag, its full commit, and the single Forgejo Release
  record.
- Mirror propagation is handled explicitly at publication, handoff, and
  scheduled discovery boundaries, so downstream work cannot silently audit a
  different or unavailable release.
- CI evidence is an authorization prerequisite rather than a best-effort
  status check, and an audit failure cannot become a misleading green result
  because the network was unavailable.
- Consumer remediation stays human-reviewed and cross-repository writes remain
  outside the detector and its Argo credentials; the trade-off is a separate
  scheduled fallback and a tri-state result that operators must interpret.
- The implementation and operator procedures are maintained together in
  [`release-publication.md`](../notes/release-publication.md) and
  [`post-tag-consumer-update.md`](../notes/post-tag-consumer-update.md).
