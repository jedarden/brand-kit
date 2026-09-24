# Changelog

All notable changes to the brand kit are documented in this file.

## [Unreleased]

### Added
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
- Site-owned favicon refresh path: the post-tag workflow runs
  `node scripts/make-favicons.mjs` in jedarden.com after a logo sync and before
  its commit, while `consumer_drift.py` detects stale `favicon.svg`,
  `apple-touch-icon.png`, `icon-192.png`, and `icon-512.png` outputs

### Changed
- `tools/consumer_sync.py` now requires a matching, published (non-draft) Forgejo
  release record before inspecting or changing consumers; the read-only API gate
  fails closed on missing, inaccessible, or malformed records and is not bypassed
  by `--offline`
- `tools/consumer_drift.py` reports exit `1` for confirmed stale consumers and
  exit `2` for indeterminate checks, so scheduled runs never turn an unavailable
  release record or live fetch into a green result

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
