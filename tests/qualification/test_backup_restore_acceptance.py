"""Backup/restore evidence must prove an isolated, reconciled fresh restore."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.support import qualification

pytestmark = pytest.mark.qualification


RELEASE_COMMIT = "e" * 40


def _report(archive: Path) -> dict:
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    counts = {"users": 2, "wallets": 2, "ledger_entries": 4, "audit_logs": 7}
    return {
        "schema_version": 1,
        "source_commit": RELEASE_COMMIT,
        "archive_path": str(archive),
        "archive_sha256": archive_hash,
        "fresh_target": True,
        "target_is_production": False,
        "target_database_id": "restore-drill-2026-10-30-01",
        "source_counts": counts,
        "restored_counts": counts.copy(),
        "ledger_reconciled": True,
        "prior_audit_preserved": True,
        "restore_audit_entry": {"action": "ops.backup.restored", "present": True},
        "restore_status": "passed",
    }


def _validate(report: dict, release_commit: str = RELEASE_COMMIT) -> None:
    validator = getattr(qualification, "validate_backup_restore_evidence", None)
    assert callable(validator), "backup/restore evidence validation is not implemented"
    validator(report, release_commit=release_commit)


def test_fresh_database_restore_with_reconciled_ledger_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"reviewed backup bytes")
    _validate(_report(archive))


def test_missing_backup_artifact_fails_restore_acceptance(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"reviewed backup bytes")
    report = _report(archive)
    archive.unlink()
    with pytest.raises(FileNotFoundError, match="backup artifact"):
        _validate(report)


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("fresh_target", False, "fresh target"),
        ("target_is_production", True, "production"),
        ("ledger_reconciled", False, "ledger"),
        ("prior_audit_preserved", False, "audit"),
        ("restore_status", "failed", "restore"),
        ("restored_counts", {"users": 1}, "counts"),
    ],
)
def test_restore_acceptance_rejects_unsafe_or_incomplete_evidence(
    tmp_path: Path, field: str, value, message: str
) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"reviewed backup bytes")
    report = _report(archive)
    report[field] = value
    with pytest.raises(ValueError, match=message):
        _validate(report)


def test_restore_evidence_must_match_the_release_commit(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"reviewed backup bytes")
    with pytest.raises(ValueError, match="same release commit"):
        _validate(_report(archive), release_commit="f" * 40)
