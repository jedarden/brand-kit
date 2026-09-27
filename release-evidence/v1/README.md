# Release evidence records

Each published release has one immutable JSON record at:

    release-evidence/v1/<tag>.json

The v1 directory is the storage version. The record's schema_version is also
1; both must change together if the shape changes. Records contain only
non-secret release facts:

- the release tag and full commit;
- the successful brand-kit-ci run name, workflow UID, completion time, and
  durable Garage attestation URL;
- the published Forgejo Release URL and target commit;
- the canonical origin and read-only github peeled-tag commits, with agrees:
  true;
- the exact consumer-drift release tag, submitted workflow name, handoff
  status, and optional durable report URL.

Create and persist a record after the Forgejo publication and consumer-drift
handoff have been accepted:

    .venv/bin/python tools/release_evidence.py \
      --tag "$VERSION" \
      --commit "$COMMIT" \
      --ci-run "brand-kit-ci/<workflow-name>" \
      --ci-attestation-url \
        "https://s3.ardenone.com/needle-ci-artifacts/attestations/brand-kit-ci/v1/<watcher-uid>/attestations.json" \
      --ci-workflow-uid "<ci-workflow-uid>" \
      --ci-finished-at "<timezone-qualified-finished-at>" \
      --forgejo-release-url "https://git.ardenone.com/jedarden/brand-kit/releases/tag/$VERSION" \
      --consumer-workflow "<brand-kit-consumer-drift-workflow-name>" \
      --mirror-commit "$COMMIT" \
      --consumer-status submitted

If the consumer workflow has completed, add
--consumer-status passed|drift|indeterminate|failed and its durable
--consumer-report-url. Commit the resulting file to main with the release
follow-up. The writer refuses credential-shaped fields and values, rejects
URLs containing credentials or query data, and will not overwrite a record
with different contents.

The contract is declared in release-evidence.schema.json and enforced by
tools/release_evidence.py and test_release_evidence.py. Release credentials
are loaded only by the publication or submission commands; they are never
fields in this record. A non-null `consumer_drift.report_url` is also a
retention hold: the consumer-drift pruner validates this record before
deletion and never removes the referenced report. Unheld consumer-drift
reports are retained for 30 days; copy a report to an approved recovery
location before that window expires if an investigation needs it longer.
