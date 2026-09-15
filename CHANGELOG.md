# Changelog

All notable changes to the brand kit are documented in this file.

## [Unreleased]

### Added
- Post-tag consumer-update workflow (ADR-2): `docs/notes/post-tag-consumer-update.md`
  checklist + `tools/consumer_sync.py`, which refreshes `jedarden.com/public/brand/`
  from a release tag (regenerating the hand-recompressed `og.jpg` /
  `src/assets/brand-hero.jpg` JPEGs from `source/hero.png`) and re-verifies the
  live GitHub profile avatar against `avatars/github-460.png`

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
