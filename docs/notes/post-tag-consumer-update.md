# Post-tag consumer-update workflow

ADR-1 (see `docs/plan/plan.md`) made CI-verified regeneration + published
Forgejo releases the distribution contract, but its own context admits the
consumer loop is open: *"there is no mechanism — automated or even a checklist
— that tells `jedarden.com` (or any future consumer) that its copy is now
stale."* CI proves this repo's committed assets match their sources at a given
tag; it cannot reach into someone else's repo or a platform profile and refresh
what they copied last May.

The implemented release-publication, mirror-propagation, and tri-state audit
architecture is captured in
[`ADR-6`](../plan/plan.md#adr-6-2026-09-27--release-publication-and-consumer-drift-handoff-architecture).

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
live `https://jedarden.com/brand/og.jpg`, the known live GitHub profile avatar,
and the public X and LinkedIn profile media registered in
`consumer-drift.json`. For X and LinkedIn it fetches the stable profile page,
discovers the current public CDN media URL, and compares that media to the
release asset; the CDN URL is not pinned because uploads rotate it. The favicon
checks make a stale regenerated output fail the audit; the live comparisons use
the release source directly rather than trusting the site checkout, so a stale
local copy cannot make a stale deployed image look current. Exit `0` means every
configured check is current, `1` reports confirmed drift, and `2` reports an
indeterminate audit; network failures and skipped checks are never reported as
a pass. The command has no apply, commit, or push operation. When the detector
is newer than the release being audited, pass
`--source-root <checkout-of-the-release-tag>` while keeping the detector itself
on the current checkout; this is what the scheduled workflow does.

### Consumer-drift report contract

The detector's JSON output and the scheduled artifact use the versioned
contract in [`consumer-drift-report.schema.json`](../../consumer-drift-report.schema.json).
Every report has `schema: "brand-kit-consumer-drift/v1"` and the same required
top-level fields: `checked_at`, `status`, `release`, `site`, `source_digests`,
`checks`, `consumers`, `coverage`, and `errors`. A report is valid even when
release setup fails: the fallback report retains the same shape with unresolved
release/site fields set to `null`, empty check collections, and an explanatory
`errors` entry.

The JSON `status` values are deliberately stable and lower-case for machine
consumers. `current` is the PASS outcome (exit `0`), `stale` is the DRIFT
outcome (exit `1`), and `indeterminate` is the INDETERMINATE outcome (exit
`2`). A check has `current`, `stale`, `unavailable`, or `skipped` status.
`unavailable` means the check could not establish a result (for example, a
network failure); `skipped` is reserved for an intentional omission such as
`--offline`. Either state makes the report `indeterminate`; neither can make a
run PASS. Every `coverage.out_of_scope` entry retains its `platform`, affected
`surfaces`, and non-empty human-readable `reason`, so PASS never implies full
platform coverage.

Version 1 is strict: producers must emit only fields defined by the schema,
and consumers must reject an unknown `schema` identifier or an invalid v1
shape. Required fields, status meanings, and the meaning of an omitted or
out-of-scope check cannot change within v1. A backward-incompatible field,
enum, or meaning change creates `brand-kit-consumer-drift/v2` and a new
artifact prefix; v1 readers are not required to interpret v2. The detector
validates before writing, and the WorkflowTemplate validates the exact file
again immediately before Argo uploads the durable artifact. Validate a saved
report manually with:

```bash
python3 tools/validate_consumer_drift_report.py /tmp/brand-kit-consumer-drift.json
```

This is deliberately not full platform coverage. Instagram, Threads, TikTok,
Facebook, YouTube, Mastodon, Bluesky, Discord, and the GitHub repository social
preview remain out of scope because their documented uploads do not have a
stable unauthenticated public media endpoint (or, for Mastodon/Bluesky, no
account/instance URL is documented). The exact exclusion reasons are retained
in the inventory's `out_of_scope` section and in JSON audit reports; a green
audit means only that all configured checks passed.

`automation/brand-kit-consumer-drift-workflowtemplate.yml` is the reusable Argo
source artifact for a release-triggered read-only run. Run
`tools/consumer_drift_submit.py --release-tag "$VERSION"` after the published
Forgejo release is verified. It refuses failed or unverifiable release records,
revalidates the Forgejo/GitHub repository identities, waits for the canonical and read-only mirror tags
to agree with the published commit, requires their `main` refs to agree, and
submits the exact tag as the `release-tag` workflow parameter and the exact
release commit as its deterministic identity. A missing, stale, or partially
propagated mirror therefore cannot receive a consumer handoff. The workflow
itself waits again for mirror visibility before checking out the tag. The companion
`automation/brand-kit-consumer-drift-cronworkflow.yml` retains the daily
read-only run as a fallback. It runs at **06:17 UTC** and uses the configured
**24-hour propagation delay** (`release.minimum_age_hours` in
`consumer-drift.json`). Discovery considers only published, non-prerelease
semantic-version releases, then excludes any tag whose peeled ref is not yet
visible on the read-only GitHub mirror; it therefore audits the newest stable
release that both gates have cleared instead of failing on a just-published tag
that is still propagating. The current checkout supplies the detector and
configuration, while the selected tag is checked out separately at
`/release` and passed as `--source-root /release`. Infrastructure manifests are
reconciled through `declarative-config`; this repository keeps the
application-owned detector and its trigger source together without granting
the detector write credentials.

### Failure routing and report retention

The detector's non-zero result is now an owner-facing event. The
`WorkflowTemplate` has a workflow-level `onExit: route-drift` handler. When the
run is not `Succeeded` — including detector exit `1` (confirmed drift), exit
`2` (indeterminate), and failures before the detector starts — the handler
posts a `BrandKitConsumerDrift` alert to the in-cluster
`alertmanager.monitoring.svc:9093/api/v2/alerts` endpoint. Alertmanager's
configured `ntfy` receiver delivers it to the `jedarden` owner channel. The
handler is best-effort and cannot turn the detector's failed workflow green;
if the notification hop is unavailable, the Argo run and durable report still
carry the failure.

The owner follows the recovery section below: exit `1` means repair the stale
consumer at the exact reported release tag, while exit `2` means repair the
missing evidence or network condition and rerun that same tag. A report URL is
included in the alert. The detector writes its JSON report to
`/tmp/consumer-drift-report.json` only as the pod-local staging path; Argo
uploads it as the `consumer-drift-report` artifact to Garage before the pod is
reaped and marks the artifact `artifactGC: Never` so it survives Workflow and
pod retention:

```text
https://s3.ardenone.com/needle-ci-artifacts/failures/brand-kit-consumer-drift/v1/<workflow-uid>/report.json
```

The selected release tag and all check results remain in that JSON object. The
`needle-ci-artifacts` bucket has no lifecycle rule for this prefix, so the
consumer WorkflowTemplate runs the repository-owned
`tools/prune_consumer_drift_reports.py` policy before each audit. Reports that
are not referenced by committed `release-evidence/v1/*.json` records are kept
for 30 days; the exact cutoff is retained until the next pass. A report URL in
release evidence is a retention hold and is never deleted by this cleanup.
The pruner validates every evidence record before it lists objects and fails
closed on a missing, malformed, or unresolvable evidence directory, so an
operator can repair the checkout or storage access and rerun without losing a
held report. The workflow turns a cleanup failure into an indeterminate report
and owner alert. Unheld reports may return `404` after the bounded window;
copy one to an approved recovery location before then if a longer
investigation needs it. The report is therefore not lost when the failed
CronWorkflow disappears from the Argo UI, while the prefix still has bounded
storage growth.

## Consumer inventory (verified 2026-09-15)

| Consumer | File(s) | Relationship to this repo |
|---|---|---|
| `jedarden.com` checkout | `public/brand/logo.svg` | byte-copy of `logo/logo.svg` |
| | `public/brand/logo-512.png` | byte-copy of `logo/logo-512.png` |
| | `public/brand/og.jpg` | recompressed JPEG from `source/hero.png` — same crop as `banners/open-graph-1200x630.png` (1200×630, `fy=0.45`), JPEG quality 88; OG card for the `/brand` page |
| | `src/assets/brand-hero.jpg` | recompressed JPEG from `source/hero.png` at native 1536×1024, quality 88; the `/brand` page hero, served through Astro's `/_astro` pipeline. (plan.md's "`hero.jpg`" refers to this file — no `public/brand/hero.jpg` exists; the URL 404s live.) |
| `github.com/jedarden` | profile avatar | manual upload of `avatars/github-460.png`. GitHub recompresses on serve, so equality is perceptual, never byte-exact |
| `jedarden.com` favicon set *(site-owned)* | `public/favicon.svg`, `public/apple-touch-icon.png`, `public/icon-192.png`, `public/icon-512.png` | generated from `public/brand/logo.svg` by `node scripts/make-favicons.mjs` in jedarden.com. `consumer_sync.py` copies the logo first but deliberately does not regenerate these files; the read-only drift detector verifies all four |

## Machine-readable registration and provenance

`consumer_registry.json` is the source of truth for copies that a checkout can
record. Each consumer entry declares:

- `checkout`: its default local checkout, overridden with `--site` when needed;
- `provenance`: a JSON file committed in that consumer checkout;
- `assets`: files managed by `consumer_sync.py`, each with a relative `path`,
  canonical brand-kit `source`, description, and a `transform` (`copy` or
  `jpeg-crop`); and
- `related_assets`: files produced by the consumer's own tooling. These are
  included in the manifest but are not overwritten by this repository.

`consumer_sync.py --apply` writes the provenance file only after the managed
copies pass and every registered file exists. The manifest records the full
brand-kit commit, the exact release tag when `HEAD` is tagged, each registered
file's SHA-256, and the declared transform. `--check` compares that manifest
with the current checkout and reports a missing, malformed, old-reference, or
changed-digest manifest as `STALE`; it never silently refreshes it.

### Transactional apply and reruns

The mutating `--apply` path is transactional for the files owned by
`consumer_sync.py`. It first builds changed byte copies and JPEG conversions in
a checkout-local staging directory, then builds the provenance manifest from
that staged result plus the untouched `related_assets`. No consumer file is
written while staging. A missing input, failed copy, failed conversion, missing
related asset, or manifest error discards the staging directory and leaves the
checkout at its previous release.

After every staged file is ready, the command installs only files whose bytes
would change. It keeps temporary backups while replacing the destinations; if
an installation replacement fails partway through, already-replaced files are
restored from those backups and the command exits non-zero. A rollback failure
is reported explicitly and the checkout must be inspected before retrying.
The transaction does not commit or push the consumer repository, and it does
not include site-owned favicon generation. A sync that was already committed
is rolled back with the consumer repository's normal `git revert`/review flow,
not by changing the brand-kit release tag.

Rerunning the exact same `--release-tag` is idempotent: current copies,
derivatives, and provenance are reused, no replacement is performed, and the
command reports that the checkout is already current. This makes a retry after
repair safe without leaving a mixed-version set of managed assets.

To register a future consumer, add a stable identifier and its checkout-relative
paths to `consumer_registry.json`, choose the appropriate transform, and commit
the registry change. Add a `related_assets` entry for generated files and a
`live_checks` entry only when the script should perform a read-only URL check.
Run `consumer_sync.py --apply --consumer <id> --site <checkout>` after the
consumer has all registered files, commit the generated provenance file in the
consumer repository, and use the matching `--check` command in its update or
scheduled check. Paths must remain relative to the consumer checkout; a
consumer can register copy assets without adding any live checks.

## The workflow

Prereqs: a published `vX.Y.Z` entry in Forgejo's **Releases** tab; a **clean**
brand-kit checkout checked out at that exact tag; the jedarden.com checkout
(default `~/jedarden.com`, else `--site <path>`) with its Node dependencies
installed when the logo changes; Pillow (the README's pinned `.venv/bin/python`
is fine — the compare runs on tolerance, so build flavor doesn't matter here);
and network access for the release-record and live checks. Before touching a
consumer, run the release publisher's read-only gate from
[`release-publication.md`](release-publication.md):

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<successful-run-id>" \
  --verify-only
```

Its `READY` result confirms the Argo run attestation, the published Forgejo
record, and the exact tag on both the canonical remote and the read-only mirror.
If it does not print `READY`, stop: do not synchronize from `main` or from a
tag that is still propagating. Provision and inject `ARGO_TOKEN` and
`FORGEJO_TOKEN` using the
[`release-token-provisioning.md`](release-token-provisioning.md) contract
before running the gate; use the separate `ARGO_SUBMIT_TOKEN` for the
release-triggered handoff. Never put tokens in this repository or pass them on
the command line.

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
   This stages the two logo files byte-for-byte and both hero JPEGs from
   `source/hero.png`, then installs the complete managed set and provenance in
   one rollback-capable transaction. Files already in sync are left untouched,
   so the resulting site diff contains only what actually changed. If a copy,
   conversion, or manifest step fails, the staging directory is discarded and
   the previous consumer checkout remains intact. If the site-owned favicon
   files are already present, this first run also records their current bytes;
   rerun the command after the favicon step below so the final manifest captures
   the regenerated outputs.

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

   Run the sync command once more after this generator completes. That refreshes
   `public/brand/brand-kit-provenance.json` with the final favicon digests without
   rewriting files that are already current.

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
   current directory. The generated
   `public/brand/brand-kit-provenance.json` is the machine-readable provenance
   record; the `@vX.Y.Z` commit message remains a convenient human-readable
   summary.

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

6. **Reconcile X and LinkedIn profile media (owner upload only).** The detector
   is deliberately read-only: it discovers the current public CDN URL and
   compares the bytes served there, but it never receives or uses X/LinkedIn
   cookies, passwords, API tokens, or browser sessions. Use the exact source
   path named by the stale check; do not upload a copy from a consumer
   checkout, and do not switch to `main` while repairing a fixed release tag.

   The monitored profile surfaces and their owner controls are:

   | Profile | Surface | Upload this release asset | Public page checked |
   |---|---|---|---|
   | X `@jedardencodes` | X profile avatar (profile photo) | `avatars/x-400.png` | `https://x.com/jedardencodes` |
   | X `@jedardencodes` | X profile header (banner) | `banners/x-header-1500x500.png` | `https://x.com/jedardencodes` |
   | LinkedIn personal `jed-arden` | LinkedIn personal profile picture | `avatars/linkedin-400.png` | `https://www.linkedin.com/in/jed-arden/` |
   | LinkedIn personal `jed-arden` | LinkedIn personal banner (background) | `banners/linkedin-personal-1584x396.png` | `https://www.linkedin.com/in/jed-arden/` |
   | LinkedIn Page `runsybil` | LinkedIn company profile picture (logo) | `avatars/linkedin-400.png` | `https://www.linkedin.com/company/runsybil/` |
   | LinkedIn Page `runsybil` | LinkedIn company banner (background) | `banners/linkedin-company-1128x191.png` | `https://www.linkedin.com/company/runsybil/` |

   Upload one profile at a time in an owner-controlled browser. The provider's
   labels can move, so use the current official help flow if a label differs:
   [X profile customization](https://help.x.com/articles/166743), [X upload
   troubleshooting](https://help.x.com/en/managing-your-account/common-issues-when-uploading-profile-photo),
   [LinkedIn personal photo](https://www.linkedin.com/help/linkedin/answer/a541850),
   [LinkedIn personal cover](https://www.linkedin.com/help/linkedin/answer/a568217),
   [LinkedIn Page logo](https://www.linkedin.com/help/linkedin/answer/a1399545/change-the-logo-image-for-your-linkedin-page-or-showcase-page?lang=en),
   and [LinkedIn Page cover](https://www.linkedin.com/help/linkedin/answer/a7433061).

   - **X:** Sign in at `x.com`, open the `@jedardencodes` profile, choose
     *Edit profile*, and use the camera controls for the profile photo and
     header. Select the two files from the table, apply the crop only as
     needed, and click *Save*. X's documented recommendations are 400×400 for
     the profile photo, 1500×500 for the header, and 2 MiB maximum for a
     profile photo; this repository's files are already preflighted, so do not
     re-export them during remediation.
   - **LinkedIn personal:** Sign in to the personal account, choose *Me → View
     profile*, then edit the profile photo from the introduction section and
     the background photo from the camera/edit control at the top-right of the
     introduction section. Upload the two personal files from the table and
     save each change. Do not accidentally edit the Page or change the photo's
     visibility while repairing media drift.
   - **LinkedIn Page:** Switch to the `runsybil` Page's super-admin view, not
     the personal profile. Use *Edit Page → Page info* to replace the logo,
     then use the Page cover/background edit control to replace the banner.
     The Page account must have the required admin role; if the control is not
     available, stop and hand off to a Page super admin rather than sharing a
     credential or trying an API token.

   **Credential boundary:** only the owner performs these browser uploads with
   the provider's normal sign-in, password manager, and MFA. The scheduled
   detector has no X or LinkedIn secret and must remain public-GET-only. The
   `FORGEJO_TOKEN` used by the release audit is read-only release-record access;
   it cannot upload profile media and must never be copied into a browser,
   shell command, report, screenshot, or handoff. Never put a provider cookie,
   password, recovery code, or API token in this repository or an Argo Secret.

   **Post-upload CDN verification:** keep the tag and the source file fixed,
   wait at least five minutes, then rerun the detector with a new report path:

   ```bash
   AFTER="/tmp/brand-kit-consumer-drift-${VERSION}-after-profile-upload.json"
   .venv/bin/python tools/consumer_drift.py \
     --release-tag "$VERSION" --site "$SITE" --json --report "$AFTER"
   ```

   Exit `0` is the required completion signal. For every repaired profile
   check, the JSON must be `status: "current"`; the detector has fetched the
   public profile page, discovered the new rotating `media_url` on the
   provider CDN (`pbs.twimg.com` for X or `media.licdn.com` for LinkedIn),
   fetched that URL with no-cache headers, and compared the served image to the
   release source. The human output also prints an
   `ACTION` line with the source, profile, and observed CDN URL when a stale
   media check remains. Save the before/after report paths in the handoff, but
   do not attach credentials or private screenshots.

   **Failure handling and stale-media handoff:** an upload rejection is a
   provider-side failure, not permission to edit the brand asset. Confirm the
   file path, dimensions, supported format, and provider size limit; retry the
   same upload once from the official web/app flow. If the save succeeds but
   the public page still exposes the old CDN URL, wait and rerun the same
   command up to three times at 5, 15, and 30 minutes; the detector's
   no-cache request is the verification authority. If the page exposes no CDN
   URL, the fetch is unavailable, the account is wrong, or the admin control is
   missing, stop: that is `indeterminate`, not current, and requires the owner
   or Page super admin to resolve access/provider availability. Do not perform
   blind repeated uploads, change the release tag, or treat a logged-in view as
   proof of public CDN freshness.

   For an unresolved `stale` or `indeterminate` result, hand off this exact
   information to the owner: release tag; profile URL; stale surface and
   release source path; report path; exit code; `media_url` (if present);
   `observed_sha256`, `observed_size`, `mean_luma`, and the detector `reason`.
   The report is actionable without secrets: it identifies the upload target,
   the source file, and the public CDN object that still needs verification.
   Resume only after the owner resolves the provider/access issue, then rerun
   the same fixed-tag audit and require all configured checks to be PASS.

7. **Record the verification** in this repo's `CHANGELOG.md` under the release,
   date and result, mirroring v1.0.0's "Verified" section. The last-verified
   date for the avatar is otherwise folklore (its previous entry was
   2026-07-20 in plan.md, with nothing since).

## Recovery after a consumer failure

Keep the release tag fixed throughout remediation. A failed release-triggered
submission, a mirror timeout, a missing consumer file, a failed site deploy, or
an indeterminate live check does not authorize switching to `main` or a newer
tag. Rerun the release publisher with `--verify-only` first; it must print
`READY` for the same `$VERSION` before any consumer write or retry.

`consumer_sync.py --apply` is repeatable and idempotent. A failed copy or
conversion is discarded before installation; a replacement failure is rolled
back to the previous checkout. Inspect the reported local problem, repair only
that problem, and rerun the exact command:

```bash
.venv/bin/python tools/consumer_sync.py --apply \
  --release-tag "$VERSION" --site "$SITE"
```

Review the resulting consumer diff, run the site-owned favicon command when the
logo changed, and commit/push with the exact `@${VERSION}` provenance. A normal
push retry is safe; a force-push is not. If the release-triggered Argo
submission fails before a workflow name is returned, repair the API or mirror
condition and submit the same tag again. If a workflow was accepted, use its
record and inspect that run before submitting another one so a transient client
timeout does not create duplicate audits.

### Submission reconciliation

The release-triggered and daily fallback paths can select the same release.
The release handoff uses a deterministic Argo Workflow name derived from the
full target commit, and carries the release tag and commit as identity labels.
Before a submission, `ARGO_TOKEN` performs a read-only exact-name lookup and a
template-scoped list lookup. A matching existing run is reused, including an
older generated-name fallback run with the same release tag. The workflow
records the fallback's selected tag and commit as output parameters so a
completed daily run is matchable even though its CronWorkflow starts with an
empty tag; no `POST` is sent.
The list must be complete, and an inspection failure blocks submission.

Argo submission uses `ARGO_SUBMIT_TOKEN` for exactly one non-retryable `POST`.
If the response is lost or returns a conflict/error, inspect the deterministic
name and template-scoped list again with `ARGO_TOKEN`. Reuse a found workflow;
if none is found, stop and investigate the provider state before manually
running the same command again. A second blind `POST` can create a duplicate
audit. The submit token is never used for inspection, and the read token is
never used for the write.

If `--check` or `consumer_drift.py` reports drift after the consumer push, wait
for the deployment and rerun both read-only checks. Fix the consumer checkout,
site-owned outputs, or manually managed avatar identified by the report; do not
alter the brand-kit release tag. Only an all-PASS, exit-0 verification belongs
in the release's `CHANGELOG.md` record.

## What "in sync" means per asset

- **Provenance manifest** — `public/brand/brand-kit-provenance.json` must name
  the current brand-kit commit (and release tag when available), use the
  registry's transforms, and contain the current SHA-256 for all eight
  registered checkout files. A missing or mismatched manifest is stale even if
  the rendered pixels happen to match.
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
