# Post-tag consumer-update workflow

ADR-1 (see `docs/plan/plan.md`) made CI-verified regeneration + published
Forgejo releases the distribution contract, but its own context admits the
consumer loop is open: *"there is no mechanism — automated or even a checklist
— that tells `jedarden.com` (or any future consumer) that its copy is now
stale."* CI proves this repo's committed assets match their sources at a given
tag; it cannot reach into someone else's repo or a platform profile and refresh
what they copied last May.

This document is that checklist; `tools/consumer_sync.py` is the remediation
script. Run the workflow **after every published Forgejo Release that changes
`source/` or the derived output** and after the server-side GitHub push mirror
has caught up. A Git tag by itself is not a release: the canonical release
record is the published entry in Forgejo's **Releases** tab. Starting after only
the tag push, or omitting the consumer refresh, leaves the distribution contract
incomplete or merely re-dates the drift.

The read-only detector is `tools/consumer_drift.py`, configured by
`consumer-drift.json`. It is safe to run independently of the remediation
checklist and is intended for a scheduler or a release event:

```bash
.venv/bin/python tools/consumer_drift.py \
  --release-tag "$VERSION" \
  --site ~/jedarden.com \
  --json \
  --report /tmp/brand-kit-consumer-drift.json
```

The detector resolves the exact published release, checks that the brand-kit
checkout is at that tag, hashes the canonical inputs, and compares the
jedarden.com logo copies, both hero JPEGs, the four site favicon outputs, the
live `https://jedarden.com/brand/og.jpg`, and the known live GitHub profile
avatar. The favicon checks make a stale regenerated output fail the audit; the
live comparisons use the release source directly rather than trusting the
site checkout, so a stale local copy cannot make a stale deployed image look
current. Exit `0` means every check is current, `1` reports confirmed drift, and
`2` reports an indeterminate audit; network failures and skipped checks are
never reported as a pass. The command has no apply, commit, or push operation.
When the detector is newer than the release being audited, pass
`--source-root <checkout-of-the-release-tag>` while keeping the detector itself
on the current checkout; this is what the scheduled workflow does.

`automation/brand-kit-consumer-drift-cronworkflow.yml` is the Argo source
artifact for a daily read-only run. It resolves the newest stable release
(after the configured propagation delay), checks out that tag, clones the
read-only GitHub mirror of jedarden.com, and leaves the JSON report in the
workflow logs. A release-triggered Argo submission can pass the release tag as
the same workflow parameter. Infrastructure manifests are reconciled through
`declarative-config`; this repository keeps the application-owned detector and
its trigger source together without granting the detector write credentials.

## Consumer inventory (verified 2026-09-15)

| Consumer | File(s) | Relationship to this repo |
|---|---|---|
| `jedarden.com` checkout | `public/brand/logo.svg` | byte-copy of `logo/logo.svg` |
| | `public/brand/logo-512.png` | byte-copy of `logo/logo-512.png` |
| | `public/brand/og.jpg` | recompressed JPEG from `source/hero.png` — same crop as `banners/open-graph-1200x630.png` (1200×630, `fy=0.45`), JPEG quality 88; OG card for the `/brand` page |
| | `src/assets/brand-hero.jpg` | recompressed JPEG from `source/hero.png` at native 1536×1024, quality 88; the `/brand` page hero, served through Astro's `/_astro` pipeline. (plan.md's "`hero.jpg`" refers to this file — no `public/brand/hero.jpg` exists; the URL 404s live.) |
| `github.com/jedarden` | profile avatar | manual upload of `avatars/github-460.png`. GitHub recompresses on serve, so equality is perceptual, never byte-exact |
| `jedarden.com` favicon set *(site-owned)* | `public/favicon.svg`, `public/apple-touch-icon.png`, `public/icon-192.png`, `public/icon-512.png` | generated from `public/brand/logo.svg` by `node scripts/make-favicons.mjs` in jedarden.com. `consumer_sync.py` copies the logo first but deliberately does not regenerate these files; the read-only drift detector verifies all four |

## The workflow

Prereqs: a published `vX.Y.Z` entry in Forgejo's **Releases** tab; a **clean**
brand-kit checkout checked out at that exact tag; the jedarden.com checkout
(default `~/jedarden.com`, else `--site <path>`) with its Node dependencies
installed when the logo changes; Pillow (the README's pinned `.venv/bin/python`
is fine — the compare runs on tolerance, so build flavor doesn't matter here);
and network access for the release-record and live checks.
If Forgejo requires API authentication, export a read-only API token as
`FORGEJO_TOKEN` before running the sync. Never put the token in this repository
or pass it on the command line.

In this workspace, `origin` is canonical Forgejo and `github` is the read-only
push mirror. Fetch the release tag from Forgejo, require both remotes to
advertise the same peeled commit, then check out the tag. `consumer_sync.py`
performs its own read-only `GET` against Forgejo's
`/api/v1/repos/jedarden/brand-kit/releases/tags/<tag>` before it reads or writes
any consumer file. The response must be a JSON release object with the exact
`tag_name` and `draft: false`. A missing or draft record, HTTP/API error, invalid
JSON, or inaccessible Forgejo instance exits 1 and `--apply` performs no writes;
`--offline` skips only the live consumer checks and never bypasses this gate.
Every command must exit 0 before continuing:

```bash
VERSION=vX.Y.Z
SITE="${SITE:-$HOME/jedarden.com}"
git fetch origin "refs/tags/$VERSION:refs/tags/$VERSION"
test "$(git rev-parse "$VERSION^{commit}")" = \
  "$(git ls-remote --exit-code origin "refs/tags/$VERSION^{}" | cut -f1)"
test "$(git rev-parse "$VERSION^{commit}")" = \
  "$(git ls-remote --exit-code github "refs/tags/$VERSION^{}" | cut -f1)"
git switch --detach "$VERSION"
```

The GitHub check verifies the mirrored Git ref only. Do not expect or create a
GitHub Release object; Forgejo is the sole release record.

1. **Refresh the jedarden.com copies:**
   ```bash
   .venv/bin/python tools/consumer_sync.py --release-tag "$VERSION" \
     --site "$SITE" --apply
   ```
   The release-record check runs before this command touches the checkout. If it
   fails, publish the Forgejo Release or fix API access and run it again.
   This copies the two logo files byte-for-byte and regenerates both hero JPEGs
   from `source/hero.png`. Files already in sync are left untouched, so the
   resulting site diff contains only what actually changed.

2. **If the logo changed, refresh the site-owned favicon set.** Run the
   consumer repository's own command after step 1 has copied the new
   `public/brand/logo.svg`:
   ```bash
   (
     cd -- "$SITE" || exit
     node scripts/make-favicons.mjs
   )
   ```
   This site-side command is the regeneration owner: it uses jedarden.com's
   pinned Node dependencies and writes `public/favicon.svg`,
   `public/apple-touch-icon.png`, `public/icon-192.png`, `public/icon-512.png`,
   and `public/favicon.ico`. `consumer_sync.py` intentionally does not write
   these site-owned outputs. A hero-only release skips this step.

3. **Commit and push in jedarden.com:**
   ```bash
   (
     cd -- "$SITE" || exit
     git status                 # review — only the expected brand files
     git add public/brand src/assets public/favicon.svg public/favicon.ico \
       public/apple-touch-icon.png public/icon-192.png public/icon-512.png
     git commit -m 'chore(brand): sync to brand-kit @vX.Y.Z'
     git push                    # Cloudflare Pages deploys on push
   )
   ```
   The generator also rewrites `public/favicon.ico`, so stage it with the four
   named PNG/SVG outputs. The subshell leaves the brand-kit checkout as the
   current directory. The `@vX.Y.Z` commit message is the provenance record.

4. **Re-verify after the deploy lands:**
   ```bash
   .venv/bin/python tools/consumer_sync.py --release-tag "$VERSION" \
     --site "$SITE" --check
   .venv/bin/python tools/consumer_drift.py --release-tag "$VERSION" --site "$SITE"
   ```
   Both commands must exit 0. `consumer_sync.py` checks its managed logo/hero
   copies and the live OG image. `consumer_drift.py` is the stale-output gate
   for `favicon.svg`, `apple-touch-icon.png`, `icon-192.png`, and
   `icon-512.png`; it also checks the checkout and live resources. A favicon
   line reported `STALE` means the site generator did not produce current
   outputs: rerun step 2, commit and push the result, then rerun this step.
   Network failures or skipped live resources are indeterminate, not a pass.

5. **Re-verify the GitHub profile avatar.** Both commands above cover it: they
   download the live avatar and compare against `avatars/github-460.png` with a
   recompression tolerance (mean luma diff ≤ 8; the identical image measures
   ≈ 3.3 through GitHub's re-encode). The avatar is set outside any build
   system and **GitHub has no API for uploading it** — if the check reports
   `STALE`, upload `avatars/github-460.png` by hand at *Settings → Public
   profile → Edit avatar*, then re-run both commands.

6. **Record the verification** in this repo's `CHANGELOG.md` under the release,
   date and result, mirroring v1.0.0's "Verified" section. The last-verified
   date for the avatar is otherwise folklore (its previous entry was
   2026-07-20 in plan.md, with nothing since).

## What "in sync" means per asset

- **Logo copies and `favicon.svg`** — byte-identical. Pixels matching isn't
  the bar for the logo copies: the whole point is that the consumer's copy
  provably came from the tagged release, and e.g. this repo's oxipng pass made
  byte-identical-pixels/byte-different-files the *first* thing the check ever
  caught (site copy 2026-05-22 vs the optimized v1.0.0 output). The site copies
  `favicon.svg` directly from its refreshed logo, so exact bytes are expected.
- **Favicon PNGs** — decoded pixel compare against the corresponding committed
  `favicon/` derivative, mean luma diff ≤ 1.0. Bytes are not equal because the
  release references use pinned resvg/Pillow while jedarden.com uses its pinned
  Sharp renderer; dimensions must match.
- **Hero JPEGs** — pixel compare against the freshly cropped source, mean luma
  diff ≤ 3.0. Tolerance, not bytes, because JPEG decodes vary slightly across
  Pillow builds; a just-applied file measures ≈ 1.5 (the codec's own loss).
- **Live GitHub avatar** — perceptual, tolerance ≤ 8.0, because GitHub
  re-encodes on serve (see step 5).

## Known state at last run (2026-09-15, all-PASS after the first executed sync)

- The workflow's own first finding — the site's `logo-512.png` byte-stale
  since 2026-05-22 (pixel-identical throughout: RGBA diff extrema all zero;
  two repo-side byte-generations: v1.0.0 oxipng pass → 8889 bytes,
  pinned-toolchain regen → 21836) — was closed the same day: `--apply`
  refreshed it and jedarden.com commit `0a956e9` pushed the sync (Cloudflare
  Pages deploys on push).
- `--check` after the sync is all-PASS, exit 0: logo.svg / logo-512.png
  byte-identical, `og.jpg` / `brand-hero.jpg` in tolerance, live
  `jedarden.com/brand/og.jpg` exact (mean diff 0.00), GitHub avatar PASS
  (mean diff 3.29; previous verification 2026-07-20).

## Non-goals

- Not automating favicon generation or the jedarden.com commit/push. Favicon
  rendering stays in the consumer's Node toolchain, and committing belongs to
  a different repo with its own review flow. `consumer_sync.py` refreshes its
  managed files and prints the site-owned generator plus finish-by-hand
  commands; it never runs Node or commits outside this repo.
- Not automating the avatar upload — GitHub offers no API for it (step 5).
- If consumers multiply past jedarden.com + platform profiles, revisit
  ADR-1's deferred CDN-hotlink option (`@vX.Y.Z` hotlinks would delete this
  checklist); with one web consumer it stays cheaper to run the script.
