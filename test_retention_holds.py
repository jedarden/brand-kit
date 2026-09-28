import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tools import retention_holds
from tools import release_evidence


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
ENDPOINT = "https://s3.example.test"
BUCKET = "needle-ci-artifacts"
PREFIX = "failures/brand-kit-ci-failure-watch/v1/"


def _hold(key, *, expires_at=None, reason="Keep this artifact for investigation"):
    return {
        "url": f"{ENDPOINT}/{BUCKET}/{key}",
        "reason": reason,
        "expires_at": expires_at,
    }


def _write_manifest(path, holds):
    path.write_text(
        json.dumps({"schema": retention_holds.SCHEMA, "holds": holds}),
        encoding="utf-8",
    )
    return path


def test_active_and_indefinite_holds_are_returned_but_expired_holds_are_not(tmp_path):
    active = f"{PREFIX}active/report.json"
    indefinite = f"{PREFIX}indefinite/report.json"
    expired = f"{PREFIX}expired/report.json"
    path = _write_manifest(
        tmp_path / "retention-holds.json",
        [
            _hold(active, expires_at="2026-09-27T13:00:01Z"),
            _hold(indefinite),
            _hold(expired, expires_at="2026-09-27T13:00:00Z"),
        ],
    )

    assert retention_holds.active_hold_keys(
        path,
        endpoint=ENDPOINT,
        bucket=BUCKET,
        now=NOW,
    ) == {active, indefinite}


@pytest.mark.parametrize(
    "hold, message",
    [
        (_hold(f"{PREFIX}run/report.json", reason="  "), "needs a reason"),
        (
            _hold(f"{PREFIX}run/report.json", expires_at="2026-09-27T13:00:00+00:00"),
            "UTC timestamp ending in Z",
        ),
        (
            _hold("failures/other/v1/run/report.json"),
            "not an exact",
        ),
        (
            {
                **_hold(f"{PREFIX}run/report.json"),
                "url": _hold(f"{PREFIX}run/report.json")["url"] + "?x=1",
            },
            "configured Garage endpoint",
        ),
    ],
)
def test_invalid_hold_entries_fail_closed(tmp_path, hold, message):
    path = _write_manifest(tmp_path / "retention-holds.json", [hold])

    with pytest.raises(retention_holds.RetentionError, match=message):
        retention_holds.active_hold_keys(
            path,
            endpoint=ENDPOINT,
            bucket=BUCKET,
            now=NOW,
        )


def test_missing_hold_manifest_fails_closed(tmp_path):
    with pytest.raises(retention_holds.RetentionError, match="unavailable"):
        retention_holds.active_hold_keys(
            tmp_path / "missing.json",
            endpoint=ENDPOINT,
            bucket=BUCKET,
            now=NOW,
        )


def test_shared_candidate_check_excludes_held_objects_and_keeps_cutoff_strict():
    held_key = f"{PREFIX}held/report.json"
    old_key = f"{PREFIX}old/report.json"
    boundary_key = f"{PREFIX}boundary/report.json"
    cutoff = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    objects = [
        SimpleNamespace(key=held_key, last_modified=cutoff.replace(day=1)),
        SimpleNamespace(key=old_key, last_modified=cutoff.replace(day=1)),
        SimpleNamespace(key=boundary_key, last_modified=cutoff),
    ]

    assert retention_holds.expired_candidate_keys(
        objects,
        cutoff=cutoff,
        filename="report.json",
        held_keys={held_key},
    ) == [old_key]


@pytest.mark.parametrize(
    ("artifact", "endpoint", "prefix", "filename"),
    (
        (
            "attestation",
            "https://s3.ardenone.com",
            retention_holds.CI_ATTESTATION_PREFIX,
            "attestations.json",
        ),
        (
            "consumer-report",
            ENDPOINT,
            retention_holds.CONSUMER_DRIFT_PREFIX,
            "report.json",
        ),
    ),
)
def test_ambiguous_delete_recovery_keeps_release_evidence_holds(
    tmp_path, artifact, endpoint, prefix, filename
):
    key = f"{prefix}held/{filename}"
    evidence_root = tmp_path / "release-evidence" / "v1"
    evidence_root.mkdir(parents=True)
    attestation_url = (
        f"https://s3.ardenone.com/needle-ci-artifacts/"
        f"{retention_holds.CI_ATTESTATION_PREFIX}watcher/attestations.json"
    )
    report_url = (
        f"{endpoint}/needle-ci-artifacts/{key}"
        if artifact == "consumer-report"
        else None
    )
    record = release_evidence.build_record(
        tag="v1.2.0",
        commit="a" * 40,
        ci_run="brand-kit-ci/run-123",
        ci_attestation_url=key_url(key, endpoint)
        if artifact == "attestation"
        else attestation_url,
        ci_workflow_uid="ci-uid",
        ci_finished_at="2026-09-27T17:00:00Z",
        forgejo_release_url="https://git.ardenone.com/jedarden/brand-kit/releases/tag/v1.2.0",
        consumer_workflow="brand-kit-consumer-drift-release-123",
        mirror_commit="a" * 40,
        consumer_report_url=report_url,
    )
    (evidence_root / "v1.2.0.json").write_text(json.dumps(record), encoding="utf-8")
    holds_path = _write_manifest(tmp_path / "retention-holds.json", [])
    old = SimpleNamespace(
        key=key,
        last_modified=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    class Reader:
        def list_objects(self, requested_prefix):
            assert requested_prefix == prefix
            return [old]

    survivors, eligible = retention_holds.reconcile_ambiguous_delete(
        Reader(),
        prefix=prefix,
        attempted_keys=[key],
        cutoff=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        filename=filename,
        holds_path=holds_path,
        evidence_root=evidence_root,
        endpoint=endpoint,
        bucket=BUCKET,
        now=NOW,
    )

    assert [item.key for item in survivors] == [key]
    assert eligible == []


def key_url(key, endpoint):
    return f"{endpoint}/needle-ci-artifacts/{key}"
