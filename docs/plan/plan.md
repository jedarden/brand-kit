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

Two canonical sources (`source/logo.svg`, `source/hero.png`) and every
per-platform derived asset rendered from them (avatars, banners, favicons,
logo masters — 30 files, ~14MB). There is no running service, API, or k8s
workload; the "deployment" surface is: (1) the files as committed to this
repo, referenced directly by consumers, and (2) copies hand-placed into
downstream repos (confirmed: `jedarden.com/public/brand/` — `logo.svg` and
`logo-512.png` are currently byte-identical to this repo's `logo/` output;
`hero.jpg`/`og.jpg` are manually recompressed derivatives). The GitHub
profile avatar for `jedarden` is confirmed live-serving this repo's
`avatars/github-460.png` (verified 2026-07-20 by diffing the live
`avatars.githubusercontent.com` image against the committed file — same
pixels, GitHub-recompressed).

## ADR-1: 2026-07-20 — CI-verified regeneration and versioned releases as the distribution contract

### Context

`tools/build_assets.py` and `tools/trace_logo.py` are the only thing standing
between `source/logo.svg` / `source/hero.png` and the 30 committed derived
files. Nothing enforces that the committed PNGs actually match what those
scripts would produce from the current source — there is no CI in this repo
at all (no `.github/`, no Argo WorkflowTemplate, confirmed by search). It is
entirely possible to hand-edit a derived PNG, or edit `source/logo.svg`
without re-running the build, and nothing would catch the drift.

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

1. Add a CI check (Argo Workflow, per this workspace's Argo-only CI policy —
   GitHub Actions stay disabled) that runs `tools/build_assets.py` (and
   `tools/trace_logo.py` when `source/logo.png` changes) in a clean checkout
   and fails the run if the regenerated files differ from what's committed.
   This turns "did you remember to re-run the build script" from a trust-based
   README instruction into an enforced invariant.
2. Once that check is green, start tagging releases (`git tag vX.Y.Z` +
   GitHub Release) on commits that change `source/` or the derived output.
   Consumers get a stable, citable reference ("brand-kit @ v1.0.0") instead of
   "whatever main was on the day I copied it."

Both parts are needed together: the CI check guarantees the *content* at any
given commit is internally consistent (source and derived output agree); the
tag gives consumers something *stable* to point at and a reason to notice
when a new one exists.

> **Follow-through:** the consumer half of this contract — what to do after a
> tag so downstream copies actually catch up — is ADR-2
> (`docs/notes/post-tag-consumer-update.md` + `tools/consumer_sync.py`).

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

- Any edit to `source/logo.svg` / `source/hero.png` that isn't followed by
  re-running the build script and committing the results now gets caught by
  CI before it lands, instead of silently diverging.
- Consuming repos get an explicit version to pin to and a changelog to check,
  instead of an unrecorded copy-paste timestamp.
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

## ADR-2: 2026-09-15 — Post-tag consumer-update workflow: a checklist and script, not automation

### Context

ADR-1 added the CI regen-check and release tags, but left its own stated
problem open on the consumer side: nothing — automated or checklist — tells a
consumer its copy is stale once a new tag exists. The confirmed consumers as
of this ADR: `jedarden.com` holds four files (two byte-copies of the logo
masters under `public/brand/`, plus two hand-recompressed JPEG derivatives of
`source/hero.png`: the `/brand` OG card and the page hero at
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
`tools/consumer_sync.py`. After every release tag that changes `source/` or
derived output, the runner: (1) `--apply`s the refresh — byte-copies the logo
masters into the jedarden.com checkout and regenerates both hero JPEGs from
`source/hero.png` at the exact crop/quality the live files were verified to
use (open-graph crop, quality 88); (2) commits and pushes in jedarden.com with
a `sync to brand-kit @vX.Y.Z` message, which is the provenance record the
hand-copy process never had; (3) `--check`s — comparing site copies and
fetching the live og.jpg and the live GitHub avatar (perceptual compare, since
GitHub recompresses on serve) — which must be all-PASS after the Pages deploy;
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
  tag).** Rejected — cross-repo push access from CI, a deploy trigger for a
  site from an asset repo's pipeline, and a failure surface far heavier than a
  once-or-twice-a-year checklist. The bottleneck is remembering the checklist
  exists, and the checklist now lives one `grep` from ADR-1.
- **Extend the CI regen-check to fetch consumers and verify them.** Rejected —
  CI should stay hermetic and repo-internal; consumer state is a
  post-release operational concern, and making the regen-check fail because a
  third-party site drifted couples two repos' health.
- **Subscribe consumers via tags only (document "watch releases").** Rejected
  as the whole mechanism — GitHub watch notifications don't reach a checklist
  level of reliability for a repo with a release every few months, and they do
  nothing for the avatar, which is set outside any repo.
- **Do nothing until a second consumer appears.** Rejected — the drift is not
  hypothetical (found, in the very first `--check` run) and the fix is one
  script plus one page of documentation.

### Consequences

- Consumer drift is now detectable in one command
  (`python3 tools/consumer_sync.py --check`) and fixable in two (`--apply`,
  then the printed commit/push commands), instead of by memory.
- The workflow still depends on a human (or agent) remembering to run it
  after a tag — it's a checklist, not an enforcer. Accepted: release cadence
  here is a handful per year, and the checklist is referenced from README,
  ADR-1's decision text, and the CHANGELOG entry so it can't be forgotten for
  lack of a pointer.
- The JPEG regeneration recipe (crop params, quality 88) is now encoded in the
  script instead of in the muscle memory that produced the originals.
- Avatar verification acquires a recorded trail in `CHANGELOG.md` rather than
  a one-off prose note in plan.md.
- Scope boundary maintained: this repo ships the checklist and script; actually
  running them mutates `jedarden.com`, which remains outside brand-kit beads
  per ADR-1's scope note.

