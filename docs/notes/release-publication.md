# Forgejo release-publication workflow

ADR-1 defines a release as two objects: an annotated `vX.Y.Z` tag on the
release commit and a published Forgejo Release attached to that exact tag. A
pushed tag alone is not a release. Forgejo is the only release authority;
GitHub receives the tag through the server-side mirror and must not receive a
second Release object. Forgejo is the sole release authority.

Run this procedure for every release that changes `source/` or derived assets.
The command is safe to run again: an already-published matching release is
verified and left unchanged.

## Preconditions

1. Start from a clean brand-kit checkout at the commit that will be released.
2. Confirm that the Argo `brand-kit-ci` workflow for that exact commit reached
   `Succeeded`, including the asset verifier, full pytest suite, regeneration,
   and no-drift checks. Copy the workflow run name. The publisher performs a
   read-only `GET` against Argo and will accept the run only when its status is
   `Succeeded` and a structured `commit`, `revision`, or equivalent full SHA
   output matches the release commit. A log line or a branch name is not an
   attestation; the WorkflowTemplate must expose the checked-out SHA as a
   structured output before this release gate can pass.
3. Make sure `origin` is the canonical Forgejo remote and `github` is the
   read-only tag mirror. The publisher reads both remotes and never pushes to
   either remote.
4. Export a narrowly scoped Forgejo API token with permission to create and
   publish releases. Do not put the token in the repository or pass it as a
   command-line argument:

   ```bash
   export FORGEJO_TOKEN='...'
   ```

   If the Argo server requires authentication, export its read-only token
   separately. The publisher uses it only for the workflow `GET`:

   ```bash
   export ARGO_TOKEN='...'
   ```

   The release-triggered consumer handoff uses an Argo token with permission
   to submit workflows. Keep it separate from the read-only attestation token:

   ```bash
   export ARGO_SUBMIT_TOKEN='...'
   ```

## Create the exact tag

After CI is green, create the annotated tag locally and push it to Forgejo.
Never create a second tag, retarget an existing tag, or use a branch name as
the release reference.

```bash
VERSION=v1.1.0
git tag -a "$VERSION" -m "brand-kit $VERSION"
git push origin "refs/tags/$VERSION"
```

The publisher requires the current `HEAD` to be the tagged commit and checks
that both remotes expose the same peeled commit. It waits for the mirror rather
than allowing a consumer workflow to start during propagation.

## Publish the Forgejo Release

The release notes are extracted from the exact `## [v1.1.0]` section in
`CHANGELOG.md`; add that section before running the command. The heading must
be a valid stable-release heading, the section must be unique and non-empty,
and any `--notes` value is accepted only when it is byte-for-byte equal to the
extracted section.

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<workflow-run-name>"
```

The publisher performs these checks in order:

1. `v1.1.0` is an annotated tag at the current `HEAD`.
2. The canonical `origin` and read-only `github` remotes advertise the same
   peeled commit for that exact tag.
3. The read-only Argo API confirms that the named run is `Succeeded` and
   carries the exact release commit in a structured output. A failed run,
   missing output, mismatched SHA, malformed response, or API error fails
   closed.
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

## Recovery and release records

Release recovery always keeps the original annotated tag and commit as the
identity of the attempt. A retry is safe only when it repeats the same
`$VERSION`, release commit, changelog section, and CI run. Never retag an
existing name, amend a tag, force-push, delete a tag to make a retry fit, or
create a second Forgejo/GitHub release to work around a bad state.

| Failure state | Safe recovery | Do not do this |
|---|---|---|
| Mirror timeout or disagreement | Leave the canonical tag in place. Confirm that `origin` still advertises the expected peeled commit, then rerun the same publisher command (or `--verify-only`) after the server-side mirror catches up. Increase `--mirror-timeout` only for the wait; the command must still end in `READY`. | Do not push the tag again, push to `github`, start consumer synchronization, or create a GitHub Release while the mirror is absent or points at another commit. |
| Failed, missing, or unverifiable Argo attestation | Do not publish. Keep the tag untouched. If the run/API was transiently unavailable, rerun the same command after it recovers. If CI genuinely failed, fix the source on a new commit, run CI for that commit, and publish a new version with a new annotated tag. | Do not substitute a branch name, a log line, a different run, or a successful run for another commit. Do not retag the failed release commit. |
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
