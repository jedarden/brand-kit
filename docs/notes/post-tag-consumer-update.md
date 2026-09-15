# Post-tag consumer-update workflow

ADR-1 (see `docs/plan/plan.md`) made CI-verified regeneration + versioned tags
the distribution contract, but its own context admits the loop is open on the
consumer side: *"there is no mechanism — automated or even a checklist — that
tells `jedarden.com` (or any future consumer) that its copy is now stale."*
CI proves this repo's committed assets match their sources at a given tag; it
cannot reach into someone else's repo or a platform profile and refresh what
they copied last May.

This document is that checklist; `tools/consumer_sync.py` is the script.
Run the workflow **after every release tag that changes `source/` or the
derived output** — i.e. every tag a consumer could pin to. A tag without the
consumer refresh just re-dates the drift.

## Consumer inventory (verified 2026-09-15)

| Consumer | File(s) | Relationship to this repo |
|---|---|---|
| `jedarden.com` checkout | `public/brand/logo.svg` | byte-copy of `logo/logo.svg` |
| | `public/brand/logo-512.png` | byte-copy of `logo/logo-512.png` |
| | `public/brand/og.jpg` | recompressed JPEG from `source/hero.png` — same crop as `banners/open-graph-1200x630.png` (1200×630, `fy=0.45`), JPEG quality 88; OG card for the `/brand` page |
| | `src/assets/brand-hero.jpg` | recompressed JPEG from `source/hero.png` at native 1536×1024, quality 88; the `/brand` page hero, served through Astro's `/_astro` pipeline. (plan.md's "`hero.jpg`" refers to this file — no `public/brand/hero.jpg` exists; the URL 404s live.) |
| `github.com/jedarden` | profile avatar | manual upload of `avatars/github-460.png`. GitHub recompresses on serve, so equality is perceptual, never byte-exact |
| `jedarden.com` favicon set *(related, manual)* | `public/favicon.svg`, `public/apple-touch-icon.png`, `public/icon-192.png`, `public/icon-512.png` | derived from `public/brand/logo.svg` by jedarden.com's own tooling (its plan P1.6). Not touched by our script — refresh there if the **logo** ever changes |

## The workflow

Prereqs: a **clean** brand-kit checkout at the tag (`git switch --detach vX.Y.Z`),
the jedarden.com checkout (default `~/jedarden.com`, else `--site <path>`),
Pillow, and network access for the live checks.

1. **Refresh the jedarden.com copies:**
   ```bash
   python3 tools/consumer_sync.py --apply
   ```
   This copies the two logo files byte-for-byte and regenerates both hero JPEGs
   from `source/hero.png`. Files already in sync are left untouched, so the
   resulting site diff contains only what actually changed.

2. **Commit and push in jedarden.com** (the script prints the exact commands):
   ```bash
   cd ~/jedarden.com && git status                 # review — should be exactly the 2-4 brand files
   git add public/brand src/assets
   git commit -m 'chore(brand): sync to brand-kit @vX.Y.Z'
   git push                                        # Cloudflare Pages deploys on push
   ```
   The `@vX.Y.Z` in the commit message is the provenance record the hand-copy
   process never had.

3. **Re-verify after the deploy lands:**
   ```bash
   python3 tools/consumer_sync.py --check          # must be all-PASS
   ```
   `--check` compares the site copies against this checkout **and** fetches the
   live `https://jedarden.com/brand/og.jpg` to confirm the Pages build actually
   deployed. Live checks are skipped offline or on fetch failure — a `SKIP`
   line means that verification did **not** happen; don't tick the box until a
   re-run shows `PASS` (or pass `--offline` only when you genuinely intend to
   defer the live half).

4. **Re-verify the GitHub profile avatar.** Same `--check` run covers it: it
   downloads the live avatar and compares against `avatars/github-460.png`
   with a recompression tolerance (mean luma diff ≤ 8; the identical image
   measures ≈ 3.3 through GitHub's re-encode). The avatar is set outside any
   build system and **GitHub has no API for uploading it** — if the check
   reports `STALE`, upload `avatars/github-460.png` by hand at
   *Settings → Public profile → Edit avatar*, then re-run `--check`.

5. **If the logo changed** (not just the hero): also regenerate jedarden.com's
   favicon set from the refreshed `public/brand/logo.svg` using that repo's own
   tooling — see the consumer inventory row above. Hero-only releases skip
   this step.

6. **Record the verification** in this repo's `CHANGELOG.md` under the release,
   date and result, mirroring v1.0.0's "Verified" section. The last-verified
   date for the avatar is otherwise folklore (its previous entry was
   2026-07-20 in plan.md, with nothing since).

## What "in sync" means per asset

- **Logo copies** — byte-identical. Pixels matching isn't the bar: the whole
  point is that the consumer's copy provably came from the tagged release, and
  e.g. this repo's oxipng pass made byte-identical-pixels/byte-different-files
  the *first* thing the check ever caught (site copy 2026-05-22 vs the
  optimized v1.0.0 output). Byte equality is the honest staleness test.
- **Hero JPEGs** — pixel compare against the freshly cropped source, mean luma
  diff ≤ 3.0. Tolerance, not bytes, because JPEG decodes vary slightly across
  Pillow builds; a just-applied file measures ≈ 1.5 (the codec's own loss).
- **Live GitHub avatar** — perceptual, tolerance ≤ 8.0, because GitHub
  re-encodes on serve (see step 4).

## Known state at last run (2026-09-15, `--check` at v1.0.0)

- GitHub avatar **PASS** — live copy still matches `avatars/github-460.png`
  (mean diff 3.29; previous verification 2026-07-20).
- `og.jpg` / `brand-hero.jpg` **PASS** (in tolerance).
- `public/brand/logo.svg` **PASS** (byte-identical).
- `public/brand/logo-512.png` **STALE** — the site's copy predates the v1.0.0
  oxipng optimization (22363 vs 8889 bytes; pixels identical). Closes on the
  next `--apply` + jedarden.com commit.

## Non-goals

- Not automating the jedarden.com commit/push — that's a different repo with
  its own review flow, and plan.md explicitly scopes migrating consumers out
  of brand-kit beads. The script refreshes files and prints the finish-by-hand
  commands; it never commits outside this repo.
- Not automating the avatar upload — GitHub offers no API for it (step 4).
- If consumers multiply past jedarden.com + platform profiles, revisit
  ADR-1's deferred CDN-hotlink option (`@vX.Y.Z` hotlinks would delete this
  checklist); with one web consumer it stays cheaper to run the script.
