# Changelog

All notable changes to the brand kit are documented in this file.

The canonical current release version is recorded in [`VERSION`](VERSION).
Choose and bump it according to [ADR-5's SemVer policy](docs/plan/plan.md#adr-5-2026-09-27--semver-policy-and-canonical-release-version)
before moving `Unreleased` entries into a dated release section or creating a
tag; the changelog heading is a consistency check, not a second version
source.

## [Unreleased]

### Added
- The release and consumer handoff gates now verify the expected Forgejo and
  GitHub repository identities, exact `main` propagation, and expected release
  tag commit agreement. Partial or stale server-side mirror propagation blocks
  handoff independently of the scheduled mirror-health monitor.
- Versioned consumer-drift report contract (`brand-kit-consumer-drift/v1`) with
  a strict JSON Schema, detector-side validation, explicit skipped checks, and
  WorkflowTemplate artifact validation for release-triggered and scheduled runs.
- Versioned, credential-free release evidence records under
  release-evidence/v1/<tag>.json, with a strict schema and persistence helper
  covering the release commit, CI attestation, Forgejo publication, mirror
  agreement, and consumer-drift handoff.
- Exact-commit `brand-kit-ci` trigger path: `tools/brand_kit_ci_submit.py`
  submits the Argo WorkflowTemplate with a required full `revision` SHA and
  rejects a submission response that drops or changes that revision; the
  release checklist documents the main-branch and release-commit flow.
- Scheduled Forgejo-to-GitHub mirror health check: the read-only Argo
  `brand-kit-mirror-health` workflow compares every branch and tag, distinguishes
  missing, stale, and divergent refs, retains a durable report, and alerts the
  owner before release or consumer processing relies on the mirror.
- Exact release-tag mirror audit: `tools/check_release_mirror.py` reads the
  peeled Forgejo and GitHub refs only, reports transient mirror lag as
  indeterminate, and preserves Forgejo as the sole release authority without
  consulting or creating a duplicate GitHub Release.
- Scheduled release-token validity probe: the daily Argo workflow checks the
  Forgejo and Argo credentials (including a server-side dry-run consumer
  submission), retains a sanitized report, and routes failures to the owner
  follow-up path used by consumer-drift alerts.
- Release publication now fails closed unless the unique, well-formed
  `CHANGELOG.md` section, annotated `vX.Y.Z` tag, Forgejo release tag, full
  target commit, and release body agree exactly; mismatched existing releases
  are never published or silently repaired.
- The Forgejo release publisher now performs a read-only Argo `brand-kit-ci`
  attestation before publication, requiring a `Succeeded` run with a
  structured full-SHA output for the exact release commit; mismatches,
  failures, missing evidence, and API errors fail closed.
- The CI failure watcher now retains minimal successful-run attestations in
  Garage, and the release publisher can use one after Argo reaps the named
  Workflow without weakening the live-run fail-closed gate.
- Repeatable Forgejo release-publication workflow: `tools/release_publish.py`
  creates or verifies the exact annotated tag after a successful CI run,
  confirms canonical and mirror tag readiness, and records Forgejo as the sole
  release authority; the operator checklist is
  `docs/notes/release-publication.md`
- Argo `brand-kit-ci` regression-gate acceptance contract: the workflow installs
  the pinned toolchain, runs asset verification and the full pytest suite, and
  fails on test failures, unexpected generated files, or regenerated drift;
  README CI acceptance logs are the proof for each revision
- Post-tag consumer-update workflow (ADR-2): `docs/notes/post-tag-consumer-update.md`
  checklist + `tools/consumer_sync.py`, which refreshes `jedarden.com/public/brand/`
  from a release tag (regenerating the hand-recompressed `og.jpg` /
  `src/assets/brand-hero.jpg` JPEGs from `source/hero.png`) and re-verifies the
  live GitHub profile avatar against `avatars/github-460.png`
- Read-only downstream drift detection (ADR-4): `tools/consumer_drift.py` and
  `consumer-drift.json` compare a published release with jedarden.com copies,
  the live OG image, and the known GitHub profile avatar; the daily Argo
  `CronWorkflow` source is `automation/brand-kit-consumer-drift-cronworkflow.yml`
  and has no consumer-write step
- Release-triggered consumer drift handoff: `tools/consumer_drift_submit.py`
  verifies the published Forgejo record, waits for canonical/mirror tag
  agreement, and submits the exact tag to the reusable Argo
  `WorkflowTemplate`; the daily `CronWorkflow` remains as a fallback
- Consumer-drift failure routing: non-success Argo runs notify the owner through
  Alertmanager/ntfy and retain the JSON report as a non-GC'd Garage artifact
  linked from the alert
- Site-owned favicon refresh path: the post-tag workflow runs
  `node scripts/make-favicons.mjs` in jedarden.com after a logo sync and before
  its commit, while `consumer_drift.py` detects stale `favicon.svg`,
  `apple-touch-icon.png`, `icon-192.png`, and `icon-512.png` outputs
- Machine-readable downstream provenance: `consumer_registry.json` registers
  syncable consumers and `consumer_sync.py` records each consumer's brand-kit
  release/commit, transform metadata, and per-file SHA-256 digests while
  reporting stale manifests; the registration contract is documented in
  `docs/notes/post-tag-consumer-update.md`
- Durable CI attestation retention: successful `brand-kit-ci` records are
  archived under the UID-scoped `attestations/brand-kit-ci/v1/` prefix, held
  by release evidence, and pruned safely after 30 days when unreferenced

### Changed
- Documented the SemVer release policy and canonical `VERSION` source in
  ADR-5; release candidates now derive their tag and changelog version from
  that file.
- `tools/consumer_sync.py` now requires a matching, published (non-draft) Forgejo
  release record before inspecting or changing consumers; the read-only API gate
  fails closed on missing, inaccessible, or malformed records and is not bypassed
  by `--offline`
- `tools/consumer_drift.py` reports exit `1` for confirmed stale consumers and
  exit `2` for indeterminate checks, so scheduled runs never turn an unavailable
  release record or live fetch into a green result
- CI attestation recovery validates the exact object URL, watcher envelope,
  workflow name, commit, phase, and unique retrieval match after Argo Workflow
  garbage collection; cleanup fails closed on malformed release evidence

### Verified
- 2026-09-15 — first executed consumer sync (ADR-2 workflow): refreshed
  `jedarden.com/public/brand/logo-512.png` (byte-stale since 2026-05-22,
  pixel-identical throughout; jedarden.com commit `0a956e9`) and re-verified
  live — all-PASS, GitHub profile avatar still matches
  `avatars/github-460.png` (mean diff 3.29; previously verified 2026-07-20)

### Removed
- `source/hero-alt.png` — alternate desk composition, never consumed by
  `tools/build_assets.py`; recoverable from git history
  (`git show dc5beac:source/hero-alt.png`) if the hero ever needs replacing

## [v1.0.0] - 2026-08-15

### Added
- Initial brand kit release
- Vector logo source (`source/logo.svg`) traced from raster original
- Full per-platform asset set:
  - Avatars for GitHub, X, LinkedIn, Instagram, Threads, Facebook, YouTube, TikTok, Mastodon, Bluesky, Discord
  - Banners for Twitter/X, LinkedIn (personal/company), GitHub social preview, Facebook, YouTube, Discord, Open Graph
  - Favicons in multiple sizes (16px to 512px)
  - Logo masters (SVG vector + pre-rendered PNG at 256/512/1024px)
- CI regen-check via Argo Workflow (`brand-kit-ci`) to verify committed assets match regenerated output
- PNG optimization reducing file sizes by ~30%

### Verified
- All regenerated assets match committed files (CI regen-check passed on commit `013617f`)

## Commit references

- v1.0.0 → `013617f` (feat(ci): add PNG dimension verification script)
- Includes PNG optimization from `dc5beac` (opt(build-assets): add PNG optimization to save ~30% file size)

Consumers can pin to this release:
```bash
# Clone at tag
git clone --branch v1.0.0 https://github.com/jedarden/brand-kit.git

# Or reference in downstream repos
brand-kit @ v1.0.0 → commit 013617f
```
