# Jed Arden — Brand Kit

Canonical logo, hero image, and ready-to-upload social assets for every major
platform. Everything here is generated from two source files so the brand stays
consistent wherever it appears.

| | Source | Style |
|---|---|---|
| **Logo** | `source/logo.svg` | **Vector** cartoon avatar — red polo, scales infinitely; used for all **profile pictures**. (`source/logo.png` is the original raster it was traced from.) |
| **Hero** | `source/hero.png` | Photoreal triple-monitor desk scene — red polo, used for all **banners / covers**. Raster only — photoreal imagery can't meaningfully vectorize. |

> The logo (flat illustration) and the hero (photoreal render) are intentionally
> kept as separate assets rather than composited together — mixing the two styles
> in one frame reads as amateurish. Each platform therefore gets a logo-based
> profile picture **and** a hero-based banner.

## Per-platform assets

Drop these straight into each platform's upload dialog — they're already at the
exact required pixel dimensions.

| Platform | Profile picture | Banner / cover |
|---|---|---|
| X / Twitter | `avatars/x-400.png` (400×400) | `banners/x-header-1500x500.png` (1500×500) |
| LinkedIn (personal) | `avatars/linkedin-400.png` (400×400) | `banners/linkedin-personal-1584x396.png` (1584×396) |
| LinkedIn (company) | `avatars/linkedin-400.png` | `banners/linkedin-company-1128x191.png` (1128×191) |
| GitHub | `avatars/github-460.png` (460×460) | `banners/github-social-1280x640.png` (1280×640, repo social preview) |
| Instagram | `avatars/instagram-320.png` (320×320) | — (no banner) |
| Threads | `avatars/threads-320.png` (320×320) | — |
| Facebook | `avatars/facebook-320.png` (320×320) | `banners/facebook-cover-851x315.png` (851×315) · 2× `…-2x-1702x630.png` |
| YouTube | `avatars/youtube-800.png` (800×800) | `banners/youtube-banner-2560x1440.png` (2560×1440, TV-safe) |
| TikTok | `avatars/tiktok-200.png` (200×200) | — |
| Mastodon | `avatars/mastodon-400.png` (400×400) | use `banners/open-graph-1200x630.png` |
| Bluesky | `avatars/bluesky-400.png` (400×400) | use `banners/twitter-card-1200x628.png` |
| Discord | `avatars/discord-512.png` (512×512) | `banners/discord-banner-960x540.png` (960×540) |
| Web / Open Graph | `favicon/` set | `banners/open-graph-1200x630.png` (1200×630) · `banners/twitter-card-1200x628.png` |

### Favicons (`favicon/`)

`favicon.ico` (multi-res 16–256), `favicon-16/32/48/192/512.png`,
`apple-touch-icon-180.png`.

### Logo masters (`logo/`)

`logo.svg` (vector — scale to any size) plus pre-rendered `logo-256/512/1024.png`
and `logo-original.png` (the 640² raster). Use the SVG when a platform isn't
listed above or you need a custom/large size; it never pixelates.

**Transparent variants** (`logo-*-transparent.png` and `logo-transparent.svg`) are
included for overlay use on colored backgrounds, dark surfaces, or print layouts
where the Canvas Cream background should not be baked in. These have full alpha
channels and can be composited onto any surface.

## Palette

| Name | Hex | Use |
|---|---|---|
| Polo Red | `#DC3127` | Primary brand color — accents, links, highlights |
| Ink | `#0A0A08` | Outlines, text on light surfaces |
| Canvas Cream | `#EFDECC` | Logo background, light surfaces |
| Skin Tan | `#F5B079` | Illustration only |
| Control-Room Black | `#070506` | Dark surfaces, banner backdrop |

## Regenerating

All derived assets are produced from the sources in `source/`. First complete
the one-time pinned toolchain setup documented in
[docs/notes/asset-toolchain.md](docs/notes/asset-toolchain.md):

```bash
cargo install resvg@0.47.0 vtracer@0.6.5
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: Pillow==12.1.1
```

Then run the applicable generator:

```bash
.venv/bin/python tools/trace_logo.py     # only when source/logo.png changes
.venv/bin/python tools/build_assets.py   # sources -> every platform asset
```

Both commands deliberately use the venv interpreter so Pillow comes from the
pinned PyPI wheel rather than the system Python. `trace_logo.py` invokes the
cargo-installed vtracer 0.6.5 CLI; it does not use a PyPI `vtracer` package.

`build_assets.py` renders each logo asset straight from `source/logo.svg` at its
exact target size (via `resvg`), so profile pictures and favicons are crisp at
any resolution. There is **no raster fallback**: output bytes depend on the
exact tool versions, so the script aborts if `resvg` (or a source file) is
missing rather than silently producing bytes the CI regen-diff would reject.

The exact pins, full regeneration recipe, and provenance are canonical in
[docs/notes/asset-toolchain.md](docs/notes/asset-toolchain.md). Edit
`source/logo.svg`/`hero.png` (or the size tables in the script), re-run, and
commit.

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

## Downstream consumers (post-tag sync)

Copies of these assets live outside this repo — `jedarden.com/public/brand/`
(logo copies + recompressed hero JPEGs) and the GitHub profile avatar. CI here
can't see them go stale, so after publishing a Forgejo Release and waiting for
its tag to propagate through the GitHub mirror, run the consumer sync:

```bash
python3 tools/consumer_sync.py --check    # what's stale? (also re-verifies the live GitHub avatar)
python3 tools/consumer_sync.py --apply    # refresh the jedarden.com copies from this checkout
```

The full checklist — including the manual commit/push in `jedarden.com`, the
GitHub avatar re-upload (no API for it), and what "in sync" means per asset —
is in **`docs/notes/post-tag-consumer-update.md`** (ADR-2).

## Usage & rights

These are the personal brand assets of Jed Arden. The repository is public so the
assets are easy to reference and self-host, but the logo, likeness, and hero
imagery are **not** licensed for reuse, redistribution, or derivative works.
All rights reserved.
