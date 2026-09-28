from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools import release_evidence


COMMIT = "a" * 40
ATTESTATION_URL = (
    "https://s3.ardenone.com/needle-ci-artifacts/attestations/"
    "brand-kit-ci/v1/watcher-uid/attestations.json"
)
REPORT_URL = (
    "https://s3.ardenone.com/needle-ci-artifacts/failures/"
    "brand-kit-consumer-drift/v1/consumer-uid/report.json"
)


def record(**overrides):
    value = {
        "tag": "v1.2.0",
        "commit": COMMIT,
        "ci_run": "brand-kit-ci/run-123",
        "ci_attestation_url": ATTESTATION_URL,
        "ci_workflow_uid": "watcher-uid",
        "ci_finished_at": "2026-09-27T17:00:00Z",
        "forgejo_release_url": (
            "https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0"
        ),
        "consumer_workflow": "brand-kit-consumer-drift-release-123",
        "mirror_commit": COMMIT,
        "consumer_report_url": REPORT_URL,
    }
    value.update(overrides)
    return release_evidence.build_record(**value)


def test_record_contains_each_required_release_evidence_section():
    evidence = record()

    assert evidence["schema_version"] == 1
    assert evidence["release"] == {"tag": "v1.2.0", "commit": COMMIT}
    assert evidence["ci"]["run_name"] == "brand-kit-ci/run-123"
    assert evidence["ci"]["attestation_url"] == ATTESTATION_URL
    assert evidence["ci"]["commit"] == COMMIT
    assert evidence["forgejo"]["tag"] == "v1.2.0"
    assert evidence["forgejo"]["published"] is True
    assert evidence["mirror"]["tag"] == "v1.2.0"
    assert evidence["mirror"]["agrees"] is True
    assert evidence["mirror"]["canonical_commit"] == COMMIT
    assert evidence["mirror"]["mirror_commit"] == COMMIT
    assert evidence["consumer_drift"]["release_tag"] == "v1.2.0"
    assert evidence["consumer_drift"]["submitted"] is True
    assert evidence["consumer_drift"]["workflow_name"] == (
        "brand-kit-consumer-drift-release-123"
    )


def test_record_links_every_completion_fact_to_the_exact_release_identity():
    evidence = record(consumer_status="passed")

    assert evidence["release"] == {"tag": "v1.2.0", "commit": COMMIT}
    assert evidence["ci"] == {
        "run_name": "brand-kit-ci/run-123",
        "attestation_url": ATTESTATION_URL,
        "workflow_uid": "watcher-uid",
        "phase": "Succeeded",
        "finished_at": "2026-09-27T17:00:00Z",
        "commit": COMMIT,
    }
    assert evidence["forgejo"] == {
        "repository": "jedarden/brand-kit",
        "tag": "v1.2.0",
        "release_url": (
            "https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0"
        ),
        "target_commit": COMMIT,
        "published": True,
    }
    assert evidence["mirror"] == {
        "tag": "v1.2.0",
        "canonical_remote": "origin",
        "canonical_commit": COMMIT,
        "mirror_remote": "github",
        "mirror_commit": COMMIT,
        "agrees": True,
    }
    assert evidence["consumer_drift"] == {
        "release_tag": "v1.2.0",
        "workflow_name": "brand-kit-consumer-drift-release-123",
        "submitted": True,
        "status": "passed",
        "report_url": REPORT_URL,
    }


def test_record_contract_is_strict_and_credential_free():
    evidence = record()
    assert not any(
        forbidden in json.dumps(evidence).lower()
        for forbidden in ("token", "secret", "password", "authorization", "bearer")
    )

    with pytest.raises(release_evidence.EvidenceError, match="credential-shaped"):
        release_evidence.validate_record({**evidence, "credential": "not allowed"})

    with pytest.raises(release_evidence.EvidenceError, match="credential material"):
        release_evidence.validate_record(
            {
                **evidence,
                "consumer_drift": {
                    **evidence["consumer_drift"],
                    "report_url": "https://example.test/report?auth=ARGO_TOKEN",
                },
            }
        )


@pytest.mark.parametrize(
    ("section", "replacement"),
    [
        ("release", {"tag": "v1.2.0"}),
        ("ci", {"run_name": "brand-kit-ci/run-123"}),
        ("forgejo", {"tag": "v1.2.0"}),
        ("mirror", {"agrees": True}),
        ("consumer_drift", {"release_tag": "v1.2.0"}),
    ],
)
def test_record_requires_complete_sections(section, replacement):
    evidence = record()
    evidence[section] = replacement

    with pytest.raises(release_evidence.EvidenceError, match="missing required fields"):
        release_evidence.validate_record(evidence)


def _with_evidence_value(section, field, value):
    evidence = deepcopy(record())
    evidence[section][field] = value
    return evidence


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("release", "tag", "v1.2.1", "forgejo.tag must match"),
        ("release", "commit", "b" * 40, "ci.commit must match"),
        ("ci", "commit", "b" * 40, "ci.commit must match"),
        ("ci", "phase", "Failed", "ci.phase must be Succeeded"),
        ("ci", "finished_at", "2026-09-27T17:00:00", "timezone"),
        ("forgejo", "repository", "someone/else", "release_url must identify"),
        ("forgejo", "tag", "v1.2.1", "forgejo.tag must match"),
        (
            "forgejo",
            "release_url",
            "https://git.ardenone.com/jedarden/other/releases/tag/v1.2.0",
            "release_url must identify",
        ),
        ("forgejo", "target_commit", "b" * 40, "target_commit must match"),
        ("forgejo", "published", False, "published must be true"),
        ("mirror", "tag", "v1.2.1", "mirror.tag must match"),
        ("mirror", "canonical_remote", "github", "canonical_remote must be origin"),
        ("mirror", "canonical_commit", "b" * 40, "canonical_commit must match"),
        ("mirror", "mirror_remote", "origin", "mirror_remote must be github"),
        ("mirror", "mirror_commit", "b" * 40, "mirror_commit must match"),
        ("mirror", "agrees", False, "agrees must be true"),
        ("consumer_drift", "release_tag", "v1.2.1", "release_tag must match"),
        ("consumer_drift", "workflow_name", "wrong-workflow", "workflow_name is malformed"),
        ("consumer_drift", "submitted", False, "submitted must be true"),
        ("consumer_drift", "status", "unknown", "status is not a supported"),
        (
            "consumer_drift",
            "report_url",
            "https://example.test/report?token=redacted",
            "credential material",
        ),
    ],
)
def test_record_rejects_partial_or_mismatched_identity(
    section, field, value, message
):
    with pytest.raises(release_evidence.EvidenceError, match=message):
        release_evidence.validate_record(_with_evidence_value(section, field, value))


def test_publication_is_not_complete_until_a_complete_versioned_record_exists(tmp_path):
    path = release_evidence.evidence_path("v1.2.0", tmp_path)

    with pytest.raises(release_evidence.EvidenceError, match="cannot read release evidence"):
        release_evidence.load_record(path)

    path.write_text(
        json.dumps({"schema_version": 1, "release": {"tag": "v1.2.0"}}),
        encoding="utf-8",
    )
    with pytest.raises(release_evidence.EvidenceError, match="missing required fields"):
        release_evidence.load_record(path)

    path.unlink()
    evidence = record()
    release_evidence.write_record(evidence, path)
    assert release_evidence.load_record(path) == evidence


def test_write_record_persists_a_versioned_json_record_and_is_idempotent(tmp_path):
    evidence = record()
    path = release_evidence.write_record(
        evidence,
        release_evidence.evidence_path("v1.2.0", tmp_path),
    )

    assert path == tmp_path / "v1.2.0.json"
    assert json.loads(path.read_text(encoding="utf-8")) == evidence
    assert release_evidence.write_record(evidence, path) == path

    changed = record(consumer_status="passed")
    with pytest.raises(release_evidence.EvidenceError, match="already exists"):
        release_evidence.write_record(changed, path)


def test_cli_does_not_offer_credential_arguments():
    options = {action.dest for action in release_evidence.build_parser()._actions}

    assert not options & {
        "token",
        "forgejo_token",
        "argo_token",
        "argo_submit_token",
        "password",
        "secret",
    }


def test_schema_declares_the_same_required_top_level_sections():
    schema = json.loads(
        (Path(__file__).resolve().parent / "release-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["required"] == [
        "schema_version",
        "release",
        "ci",
        "forgejo",
        "mirror",
        "consumer_drift",
    ]
    for definition in ("release", "ci", "forgejo", "mirror", "consumer_drift"):
        assert schema["$defs"][definition]["additionalProperties"] is False
