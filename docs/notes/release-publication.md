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
2. Confirm the Argo `brand-kit-ci` workflow for that exact commit reached
   `Succeeded`, including the asset verifier, full pytest suite, regeneration,
   and no-drift checks. Copy its run identifier; the publisher records that
   identifier but cannot query Argo on your behalf.
3. Make sure `origin` is the canonical Forgejo remote and `github` is the
   read-only tag mirror. The publisher reads both remotes and never pushes to
   either remote.
4. Export a narrowly scoped Forgejo API token with permission to create and
   publish releases. Do not put the token in the repository or pass it as a
   command-line argument:

   ```bash
   export FORGEJO_TOKEN='...'
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

The default notes are extracted from the exact `## [v1.1.0]` section in
`CHANGELOG.md`; add that section before running the command. Use `--notes` only
when the release notes intentionally come from another reviewed file.

```bash
.venv/bin/python tools/release_publish.py \
  --tag "$VERSION" \
  --ci-run "brand-kit-ci/<successful-run-id>"
```

The publisher performs these checks in order:

1. `v1.1.0` is an annotated tag at the current `HEAD`.
2. The canonical `origin` and read-only `github` remotes advertise the same
   peeled commit for that exact tag.
3. The successful CI run identifier is supplied by the operator.
4. Forgejo's release-by-tag endpoint either finds the matching published
   record, publishes an existing matching draft, or creates a non-draft stable
   release with `tag_name` and `target_commitish` set to the exact tag and
   commit.
5. Forgejo is queried again after the write, and both remotes are checked again
   for the tag.

A missing or mismatched tag, an inaccessible API, a wrong release tag, a
prerelease, or a draft record fails closed. A `409` response is treated as a
race and is accepted only when a subsequent read returns the exact published
record. A failed run does not authorize consumer synchronization.

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

A successful verification prints `READY` only after the Forgejo record is
published and the canonical and mirror refs still point to the same commit.
The command prints the next consumer check, but does not run it:

```bash
.venv/bin/python tools/consumer_sync.py --release-tag "$VERSION" --check
```

The consumer workflow in
[`docs/notes/post-tag-consumer-update.md`](post-tag-consumer-update.md) starts
only after that `READY` result. It must retain the exact `$VERSION` in its
consumer commits and verification records. If the mirror is not ready, rerun
the command after propagation; do not synchronize from `main` or from a
lightweight tag.

## Recovery and release records

- If the API call fails after the tag push, leave the tag in place and rerun
  the same command. The script is idempotent and will verify or complete the
  matching Forgejo record.
- If a draft exists for the same tag, the script publishes that record rather
  than creating a duplicate. Review the draft body and target before rerunning.
- If a release record names another tag, reports a prerelease, or is malformed,
  stop and repair the Forgejo record manually. Do not create a replacement tag
  with a different name to bypass the mismatch.
- If the mirror is missing or points elsewhere, stop consumer work. The GitHub
  remote is a read-only distribution mirror; it is not a release authority and
  is not a reason to create a GitHub Release.
- Record the publication and consumer verification in the corresponding
  `CHANGELOG.md` section. The exact tag, commit, CI run, and publication result
  are the provenance record.
