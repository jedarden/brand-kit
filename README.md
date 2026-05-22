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

## Palette

| Name | Hex | Use |
|---|---|---|
| Polo Red | `#DC3127` | Primary brand color — accents, links, highlights |
| Ink | `#0A0A08` | Outlines, text on light surfaces |
| Canvas Cream | `#EFDECC` | Logo background, light surfaces |
| Skin Tan | `#F5B079` | Illustration only |
| Control-Room Black | `#070506` | Dark surfaces, banner backdrop |

## Regenerating

All derived assets are produced from the sources in `source/`:

```bash
python3 tools/trace_logo.py     # raster logo.png -> vector logo.svg (needs vtracer)
python3 tools/build_assets.py   # sources -> every platform asset (needs Pillow; resvg for crisp vector logo)
```

`build_assets.py` renders each logo asset straight from `source/logo.svg` at its
exact target size (via `resvg`), so profile pictures and favicons are crisp at
any resolution. If `resvg` is unavailable it falls back to resizing the raster.

Tooling (one-time): `cargo install vtracer resvg`. Edit `source/logo.svg`/`hero.png`
(or the size tables in the script), re-run, and commit. `source/hero-alt.png` is
an alternate desk composition kept for reference.

## Usage & rights

These are the personal brand assets of Jed Arden. The repository is public so the
assets are easy to reference and self-host, but the logo, likeness, and hero
imagery are **not** licensed for reuse, redistribution, or derivative works.
All rights reserved.
