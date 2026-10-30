"""Release manifest acceptance rules for the complete first-release gate."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.support import qualification

pytestmark = pytest.mark.qualification


REQUIRED_GATES = (
    "python_unit",
    "bun_test",
    "bun_typecheck",
    "bun_build",
    "postgres_gate",
    "browser_gate",
    "voice_local_offline",
    "voice_deployment",
    "release_evidence",
)


def _passing_manifest() -> dict:
    return {
        "manifest_version": 2,
        "run_id": "run",
        "run_dir": "/tmp/qualification/run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": "a" * 40,
        "python_version": "3.12.0",
        "uv_lock_sha256": "b" * 64,
        "bun_lock_sha256": "c" * 64,
        "pyproject_toml_sha256": "d" * 64,
        "overall_success": True,
        "required_gates": list(REQUIRED_GATES),
        "gates": {
            name: {
                "status": "passed",
                "reason": "gate completed successfully",
                "counts": {
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
            }
            for name in REQUIRED_GATES
        },
    }


def _validate(manifest: dict) -> bool:
    validator = getattr(qualification, "validate_release_manifest", None)
    assert callable(validator), "release manifest validation is not implemented"
    return validator(manifest)


def test_complete_manifest_records_every_required_gate_and_reason() -> None:
    assert _validate(_passing_manifest()) is True


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda data: data["gates"].pop("browser_gate"), "missing required gate"),
        (lambda data: data["gates"]["voice_deployment"].update(status="unknown"), "invalid status"),
        (lambda data: data["gates"]["postgres_gate"].pop("reason"), "reason"),
        (lambda data: data.update(commit="not-a-commit"), "commit"),
        (lambda data: data.update(unreviewed_field=True), "unknown fields"),
        (lambda data: data["gates"]["browser_gate"].pop("counts"), "test counts"),
        (lambda data: data["gates"]["python_unit"]["counts"].update(collected=1, passed=0, skipped=1), "non-pass"),
    ],
)
def test_malformed_manifest_fails_closed(mutation, message: str) -> None:
    manifest = _passing_manifest()
    mutation(manifest)
    with pytest.raises(ValueError, match=message):
        _validate(manifest)


def test_failed_or_not_run_gate_never_qualifies_manifest() -> None:
    manifest = _passing_manifest()
    manifest["overall_success"] = False
    manifest["gates"]["browser_gate"] = {
        "status": "failed",
        "reason": "Chromium binary unavailable",
    }

    assert _validate(manifest) is False


@pytest.mark.parametrize("resource", ["fixtures", "model", "browser", "database", "TURN", "report"])
def test_missing_required_release_prerequisite_is_an_explicit_failure(resource: str) -> None:
    require = getattr(qualification, "require_prerequisite", None)
    assert callable(require), "required release prerequisite checks are not implemented"
    with pytest.raises(ValueError, match=resource):
        require(resource, available=False)


def test_missing_report_file_cannot_be_accepted(tmp_path: Path) -> None:
    load = getattr(qualification, "load_release_manifest", None)
    assert callable(load), "release manifest loading is not implemented"
    with pytest.raises(FileNotFoundError, match="manifest"):
        load(tmp_path / "missing-manifest.json")
