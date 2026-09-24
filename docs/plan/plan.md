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
for banners/covers. `source/logo.png` is preserved provenance, is copied to
`logo/logo-original.png`, and may explicitly replace the opaque SVG through
`tools/trace_logo.py`; it is not the source used for normal vector rendering
and there is no raster fallback. The build produces 37 asset files plus
`palette.json`. There is no running service, API, or k8s workload; the
"deployment" surface is: (1) the files as committed to this repo, referenced
directly by consumers, and (2) copies hand-placed into downstream repos
(confirmed: `jedarden.com/public/brand/` — `logo.svg` and `logo-512.png` are
currently byte-identical to this repo's `logo/` output; `hero.jpg`/`og.jpg` are
manually recompressed derivatives). The GitHub profile avatar for `jedarden`
is confirmed live-serving this repo's `avatars/github-460.png` (verified
2026-07-20 by diffing the live `avatars.githubusercontent.com` image against
the committed file — same pixels, GitHub-recompressed).

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

`origin` is the canonical Forgejo repository at
`https://git.ardenone.com/jedarden/brand-kit.git`; `github` is its read-only,
server-side push mirror. The repeatable operator procedure is
[`docs/notes/release-publication.md`](notes/release-publication.md), backed by
`tools/release_publish.py`. It runs only after the `brand-kit-ci` Argo run for
the exact release commit has succeeded and records that run identifier.

First create and push the annotated tag:

```bash
git tag -a vX.Y.Z -m "brand-kit vX.Y.Z"
git push origin refs/tags/vX.Y.Z
```

Then publish and verify the exact tag with:

```bash
.venv/bin/python tools/release_publish.py \
  --tag vX.Y.Z \
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
jedarden.com with a `sync to brand-kit @vX.Y.Z` message, which is the provenance
record the hand-copy process never had; (3) `--check`s — comparing site copies
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
   master, and is an input only to an explicitly requested raster-to-vector
   replacement. `source/logo-transparent.svg` is independently authoritative
   for transparent outputs.
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
4. `source/logo.svg.sha256` records the digest from the last successful trace.
   `trace_logo.py` refuses to overwrite an existing SVG whose digest differs
   and checks that invariant again immediately before replacement, but
   `build_assets.py` does not read the digest: a deliberately hand-edited SVG
   remains a valid authoritative build source.
5. Replacing a modified SVG from the raster is possible only through the
   explicit `.venv/bin/python tools/trace_logo.py --force` command. A normal
   trace gives vtracer a temporary output path; the authoritative SVG is not
   replaced unless that process exits successfully, after which the new digest
   is written.

### Consequences

- Hand-edited vector work has a separate safe path: authoritative SVG → build.
  Raster replacement is an explicit, reviewable transition, and a concurrent SVG
  edit is rechecked before the traced file replaces it.
- A malformed or missing digest fails closed, so the ordinary trace path cannot
  silently discard a committed SVG modification.
- `source/logo.png` is no longer ambiguously described as the logo source of
  truth, even though it remains build-visible for the original-raster master.
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
`consumer-drift.json`, and provide the Argo source artifact
`automation/brand-kit-consumer-drift-cronworkflow.yml` for a daily read-only
run. The workflow resolves the newest published stable Forgejo release after a
propagation delay, checks out that exact tag, clones jedarden.com from its
read-only GitHub mirror, and runs the detector. A release-triggered Argo
submission may set the `release-tag` workflow parameter instead.

The detector:

1. Resolves and validates the published Forgejo release record; a missing,
   draft, malformed, or inaccessible record is indeterminate.
2. Requires the brand-kit checkout to be the selected release tag and records
   SHA-256 digests for canonical inputs.
3. Byte-compares jedarden.com's logo copies and `favicon.svg`, compares its
   three favicon PNGs and two hero JPEGs against release-generated reference
   assets, compares the live OG image directly against the release crop, and
   perceptually compares the known live GitHub avatar with the release avatar.
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
- The inventory is extensible: adding a future consumer means adding a
  read-only path/URL rule to `consumer-drift.json`; it does not expand the
  remediation authority of this repository.
- A failed or indeterminate run is visible as a failed Argo workflow. The
  manifest is reconciled through the shared `declarative-config` ArgoCD
  application, while this repository remains the owner of the detector and its
  tests.

