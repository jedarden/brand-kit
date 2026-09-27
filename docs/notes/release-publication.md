# Forgejo release-publication workflow

ADR-1 defines a release as two objects: an annotated `vX.Y.Z` tag on the
release commit and a published Forgejo Release attached to that exact tag. A
pushed tag alone is not a release. Forgejo is the only release authority;
GitHub receives the tag through the server-side mirror and must not receive a
second Release object. Forgejo is the sole release authority.

The architecture and rationale for the publication gates, CI attestation,
mirror agreement, and consumer handoff are recorded in
[`ADR-6`](../plan/plan.md#adr-6-2026-09-27--release-publication-and-consumer-drift-handoff-architecture).

Choose the version using [ADR-5's SemVer policy](../plan/plan.md#adr-5-2026-09-27--semver-policy-and-canonical-release-version).
The root `VERSION` file is the one authoritative version source. Do not
invent a value by hand in this checklist or infer it from the previous
changelog heading.

Run this procedure for every release selected under ADR-5. Changes to
`source/` or derived assets require a release; docs-only and metadata-only
changes may stay under `Unreleased`. The command is safe to run again: an
already-published matching release is verified and left unchanged.

## Prepare the release version

Before the CI run and before creating the tag, classify the complete diff using
ADR-5 and select the next SemVer value from the committed version in
`VERSION`. Write the selected value (including the `v` prefix) as the only
line of `VERSION`. In the same release commit, move the completed entries from
`CHANGELOG.md`'s `Unreleased` section into a unique heading of the exact form
`## [$VERSION] - YYYY-MM-DD`, then create a new `Unreleased` section. The
changelog heading, `VERSION`, and eventual tag must be identical. A docs-only
or metadata-only diff may remain unreleased; if it is intentionally published,
use the patch rule and this same ordering.

## Preconditions

1. Start from a clean brand-kit checkout at the commit that will be released.
2. Confirm that `VERSION` is committed at the selected value and that the
   changelog section named by that value is already in the release commit.
3. Submit `brand-kit-ci` for that exact commit before creating the tag. The
   supported trigger is the repository-owned manual Argo submitter:

   ```bash
   COMMIT="$(git rev-parse --verify HEAD^{commit})"
   .venv/bin/python tools/brand_kit_ci_submit.py \
     --commit "$COMMIT" \
     --branch main
   ```

   This passes the full SHA as the WorkflowTemplate's `revision` parameter;
   the template must check out that revision detached, not resolve `main`
   again when the pod starts. Record the returned workflow name and wait for
   its `Succeeded` phase, including the asset verifier, full pytest suite,
   regeneration, and no-drift checks. Confirm that the structured `commit`
   output equals `$COMMIT`. The publisher performs a read-only `GET` against
   Argo and accepts the run only when its status is `Succeeded` and a
   structured `commit`, `revision`, or equivalent full SHA output matches the
   release commit. A log line or a branch name is not an attestation.
   The application-owned `brand-kit-ci-failure-watch` also copies recent
   successful runs into a non-GC'd Garage artifact at
   `attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json`.
   Keep that URL with the release evidence: it is the recovery input if Argo
   reaps the named Workflow before publication.
4. Make sure `origin` is the canonical Forgejo remote and `github` is the
   read-only tag mirror. The publisher reads both remotes and never pushes to
   either remote.
5. Provision the three release credentials according to
   [`release-token-provisioning.md`](release-token-provisioning.md). The
   publisher requires `FORGEJO_TOKEN` and `ARGO_TOKEN`; the consumer handoff
   requires `FORGEJO_TOKEN` and `ARGO_SUBMIT_TOKEN`. Load them from OpenBao
   into the short-lived process environment only. Never put a token in the
   repository, a workflow parameter, a URL, or a command-line argument.

## Create the exact tag

After CI is green, create the annotated tag locally and push it to Forgejo.
Never create a second tag, retarget an existing tag, or use a branch name as
the release reference.

```bash
VERSION="$(tr -d '\r\n' < VERSION)"
test "$(git show HEAD:VERSION)" = "$VERSION"
git tag -a "$VERSION" -m "brand-kit $VERSION"
git push origin "refs/tags/$VERSION"
```

The publisher requires the current `HEAD` to be the tagged commit and checks
that both remotes expose the same peeled commit. It waits for the mirror rather
than allowing a consumer workflow to start during propagation.

## Publish the Forgejo Release

The release notes are extracted from the exact `## [$VERSION]` section in
`CHANGELOG.md`; prepare that section in the release commit as described above,
before creating the tag. The heading must be a valid stable-release heading,
the section must be unique and non-empty, and any `--notes` value is accepted
only when it is byte-for-byte equal to the extracted section.

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<workflow-run-name>"
```

If the named Workflow has already been reaped, add the durable attestation
object captured by the 15-minute watcher:

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<workflow-run-name>" \
  --ci-attestation-url \
  "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json"
```

The publisher always tries the read-only Argo `GET` first. It reads the Garage
object only after that exact run returns `404`; a live failed run, a different
commit, an invalid timestamp, or an untrusted URL still fails closed. The
attestation artifact contains one or more records with only `commit`,
`workflow_uid`, `phase: Succeeded`, and `finished_at`; the publisher selects
exactly one record for the release commit. If more than one matching record is
present, use the watcher artifact for the run being published or rerun the
watcher/archive path so the evidence is unambiguous.

The publisher performs these checks in order:

1. `$VERSION` is an annotated tag at the current `HEAD`.
2. The canonical `origin` and read-only `github` remotes advertise the same
   peeled commit for that exact tag.
3. The read-only Argo API confirms that the named run is `Succeeded` and
   carries the exact release commit in a structured output. If Argo returns
   `404` because the run was reaped, the optional `--ci-attestation-url`
   fallback must instead return a validated durable `Succeeded` record for the
   exact commit. A failed run, missing output, mismatched SHA, malformed
   response, invalid artifact URL, or API error fails closed.
4. The exact changelog section is retained as the release body. Forgejo's
   release-by-tag endpoint either finds the matching published record, publishes
   an existing matching draft, or creates a non-draft stable release with
   `tag_name`, full `target_commitish`, and `body` equal to the exact tag,
   commit, and changelog notes. Existing records with a different tag, target,
   or body fail closed before any write.
5. Forgejo is queried again after the write, and both remotes are checked again
   for the tag.

A missing or mismatched tag, an inaccessible Argo or Forgejo API, a
non-`Succeeded` run, missing or mismatched run commit, wrong release tag, a
prerelease, or a draft record fails closed. A `409` response is treated as a
race and is accepted only when a subsequent read returns the exact published
record. A failed or unverifiable run does not authorize publication or
consumer synchronization.

The command never calls the GitHub Releases API, creates a GitHub Release, or
pushes the tag to the mirror. The API base defaults to
`https://git.ardenone.com/api/v1` and the repository to `jedarden/brand-kit`;
`--api-url` and `--repository` exist only for an explicitly reviewed Forgejo
instance.

For a standalone, credential-free exact-tag audit of the release handoff, run
the read-only mirror checker after the publisher (or when investigating a
mirror alert):

```bash
.venv/bin/python tools/check_release_mirror.py \
  --tag "$VERSION" \
  --commit "$COMMIT" \
  --report /tmp/brand-kit-release-mirror.json
```

The audit reads only the peeled tag from Forgejo and GitHub with
`git ls-remote`. It records Forgejo as the release authority and deliberately
does not query, accept, update, or create a GitHub Release. A missing GitHub
tag is transient mirror lag: it is `INDETERMINATE` while the server-side
mirror catches up; retry the audit and keep the release tag fixed. A matching
tag is `HEALTHY`, while a tag that points at a different commit is
`UNHEALTHY`. A canonical Forgejo tag that is absent or points at a different
commit is also `UNHEALTHY`.

## Verify the handoff

Use the read-only form after publication, or as a separate audit before a
consumer run:

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<successful-run-id>" \
  --verify-only
```

A successful verification prints `READY` only after the Argo attestation, the
Forgejo record is published, and the canonical and mirror refs still point to
the same commit. `--verify-only` still performs the Argo check, but does not
write to Forgejo.

After `READY`, submit the release-triggered read-only consumer audit. The
submitter reads the exact published Forgejo record, requires its full target
commit, waits for the canonical and GitHub mirror tags to agree, and passes the
exact tag to the Argo `WorkflowTemplate`:

```bash
.venv/bin/python tools/consumer_drift_submit.py \
  --release-tag "$VERSION"
```

The Argo workflow retries mirror visibility for up to ten minutes before its
audit starts as an additional propagation guard. A failed, draft, prerelease,
malformed, or otherwise unverifiable Forgejo record never submits a workflow.
The daily `CronWorkflow` remains enabled as a fallback; it discovers the
newest stable release after the configured 24-hour propagation age.

The remediation checklist in
[`docs/notes/post-tag-consumer-update.md`](post-tag-consumer-update.md) starts
after the release-triggered audit has been submitted. It must retain the exact
`$VERSION` in its consumer commits and verification records. If the handoff
fails, repair the release or mirror state and rerun the submitter; do not
synchronize from `main` or from a lightweight tag.
The remediation command remains `tools/consumer_sync.py` and is separate from
the read-only drift submission.

## Persist release evidence

The release evidence is a versioned, credential-free JSON record committed in
this repository at
release-evidence/v1/<tag>.json. It is the durable handoff record for the exact
release identity, not a replacement for the Argo or Forgejo records. The
record contains:

- the full release commit;
- the successful brand-kit-ci/<run-name> and its durable attestation URL,
  workflow UID, phase, and completion time;
- the published Forgejo Release URL, tag, and target commit;
- the origin and github peeled tag commits and their agrees result; and
- the exact consumer-drift tag, submitted workflow name, status, and durable
  report URL when one has been retained.

After publication and the consumer handoff, create it with the non-secret
outputs from those commands:

    .venv/bin/python tools/release_evidence.py \
      --tag "$VERSION" \
      --commit "$COMMIT" \
      --ci-run "brand-kit-ci/<workflow-name>" \
      --ci-attestation-url \
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/<watcher-uid>/attestations.json" \
      --ci-workflow-uid "<ci-workflow-uid>" \
      --ci-finished-at "<timezone-qualified-finished-at>" \
      --forgejo-release-url \
        "https://git.ardenone.com/jedarden/brand-kit/releases/tag/$VERSION" \
      --consumer-workflow "<brand-kit-consumer-drift-workflow-name>" \
      --mirror-commit "$COMMIT" \
      --consumer-status submitted

When the consumer run has a durable report, rerun the command with its final
consumer status and report URL before committing the record. The writer
validates the v1 schema, requires all identity fields to agree, rejects
credential-shaped fields and values, and refuses to overwrite an existing tag
record with different contents. Never add FORGEJO_TOKEN, ARGO_TOKEN,
ARGO_SUBMIT_TOKEN, bearer values, or any other credential to the record;
credentials remain environment-only inputs to the operational commands.

## Recovery and release records

Release recovery always keeps the original annotated tag and commit as the
identity of the attempt. A retry is safe only when it repeats the same
`$VERSION`, release commit, changelog section, and CI run. Never retag an
existing name, amend a tag, force-push, delete a tag to make a retry fit, or
create a second Forgejo/GitHub release to work around a bad state.

| Failure state | Safe recovery | Do not do this |
|---|---|---|
| Mirror timeout or disagreement | Leave the canonical tag in place. Confirm that `origin` still advertises the expected peeled commit, then rerun the same publisher command (or `--verify-only`) after the server-side mirror catches up. Increase `--mirror-timeout` only for the wait; the command must still end in `READY`. | Do not push the tag again, push to `github`, start consumer synchronization, or create a GitHub Release while the mirror is absent or points at another commit. |
| Failed, missing, or unverifiable Argo attestation | Do not publish. If the named Workflow returns `404`, locate the non-GC'd `attestations/brand-kit-ci/v1/<watcher-workflow-uid>/attestations.json` object from the scheduled watcher and rerun the same command with `--ci-attestation-url`; the artifact must contain exactly one matching commit record. If Argo is transiently unavailable, rerun after it recovers. If CI genuinely failed or no durable record exists, fix the source on a new commit, run CI for that commit, and publish a new version with a new annotated tag. | Do not substitute a branch name, a log line, a different run, or a successful run for another commit. Do not use the fallback to override a live non-`Succeeded` run, and do not retag the failed release commit. |
| Partial Forgejo publication or an ambiguous create response | Rerun the same command. The publisher reads `/releases/tags/$VERSION` first, reuses an exact published record, publishes an exact matching draft, and accepts `409` only after a matching read. A timed-out POST is therefore resolved by a GET, not another manual POST. | Do not manually create a second release, guess whether the first POST committed, or alter the tag/target/body to make the request succeed. |
| Post-write verification failure | Leave both the tag and any Forgejo record in place. Retry the same command after a transient API/read problem; it will verify an existing exact record without writing again. If Forgejo returns a record with the wrong tag, full target commit, body, draft, or prerelease state, stop and have an operator correct that record in place to the exact tag, commit, and changelog body, then rerun. | Do not delete and recreate the Git tag, force-push, publish a replacement tag, or proceed to consumers on an unverified record. |
| Consumer handoff or `consumer_sync.py` failure | Keep `$VERSION` fixed. First rerun the publisher's `--verify-only` gate and repair mirror/API state if needed. For a local consumer failure, inspect the consumer checkout, rerun `consumer_sync.py --apply --release-tag "$VERSION"` after correcting the reported issue, review only the expected files, then commit and push normally. Rerun `--check` and the drift audit after deployment. | Do not sync from `main`, a newer tag, or a lightweight tag; do not write consumer files when the Forgejo release gate fails; do not force-push a consumer checkout or mark a failed/indeterminate audit as verified. |

The corrective-release path for a real content problem is a new normal release:
fix the source, regenerate and pass CI, update the changelog, choose the next
version, create a new annotated tag on the new commit, and push that tag once to
`origin`. It never retargets or force-pushes the old tag. A service outage or
partial publication is not a content problem and uses the same-tag retry path
above.

After a successful recovery, record the original tag, full commit, Argo run,
publication result, and consumer verification in the corresponding
`CHANGELOG.md` section. The exact tag remains the provenance record even when
the first attempt timed out or a consumer needed a second sync.
