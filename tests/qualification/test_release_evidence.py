"""Release evidence binds all gate reports and the staging runbook to one commit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import qualification

pytestmark = pytest.mark.qualification


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "first-release" / "ops-runbook.md"
MODEL_HASH = "d" * 64


def _deployment_config() -> dict:
    return {
        "schema_version": 1,
        "profile": "deployment",
        "allowed_origins": ["https://staging.example.invalid"],
        "models": {
            name: {"id": f"{name}-id", "revision": f"{name}-revision", "sha256": MODEL_HASH}
            for name in ("llm", "stt", "tts_en", "tts_hi")
        },
        "runner_api_endpoints": {
            "runner": "https://runner.staging.example.invalid",
            "api": "https://api.staging.example.invalid",
        },
        "turn": {
            "required": True,
            "endpoint": "turns:relay.staging.example.invalid:5349",
            "credential_reference": "secret-manager/voice-staging/turn",
        },
        "browser": {"engine": "chromium", "version": "1.64.1"},
        "database": {"server_version": "16.4", "image_digest": "sha256:" + "e" * 64},
        "concurrency": 4,
        "limits": {
            "turn_timeout_ms": 30_000,
            "latency_p95_ms": 2_000,
            "quality_threshold": 0.9,
        },
        "baseline_artifact_ids": ["latency-baseline-2026-10-30"],
    }


def test_staging_runbook_has_a_fail_closed_release_procedure() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    check = getattr(qualification, "validate_staging_runbook", None)
    assert callable(check), "staging runbook validation is not implemented"
    check(runbook)


def test_missing_required_report_fails_instead_of_being_ignored(tmp_path: Path) -> None:
    require = getattr(qualification, "require_report", None)
    assert callable(require), "required qualification report checks are not implemented"
    with pytest.raises(FileNotFoundError, match="browser report"):
        require(tmp_path / "playwright.json", "browser report")


def test_release_requires_backup_restore_acceptance_in_database_report(tmp_path: Path) -> None:
    require_test = getattr(qualification, "require_junit_testcase", None)
    assert callable(require_test), "required JUnit testcase validation is not implemented"
    report = tmp_path / "postgres.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="platform_api.test_ops_backup_restore" '
        'name="test_unrelated_database_test"/></testsuite>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="backup restore acceptance testcase missing"):
        require_test(report, "test_backup_restores_platform_data_into_fresh_owned_database")


def test_required_release_artifacts_are_checked_in_the_owned_run_directory(tmp_path: Path) -> None:
    check = getattr(qualification, "validate_release_artifacts", None)
    assert callable(check), "release artifact validation is not implemented"
    with pytest.raises((FileNotFoundError, ValueError), match="report|manifest|command"):
        check(tmp_path, require_final_report=False)


def test_deployment_config_pins_models_turn_browser_database_and_budgets() -> None:
    validate = getattr(qualification, "validate_deployment_config", None)
    assert callable(validate), "deployment qualification configuration validation is not implemented"
    validate(_deployment_config())


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda data: data["models"].pop("stt"), "model"),
        (lambda data: data["turn"].pop("endpoint"), "TURN"),
        (lambda data: data["browser"].pop("version"), "browser"),
        (lambda data: data["database"].pop("image_digest"), "database"),
        (lambda data: data["baseline_artifact_ids"].clear(), "baseline"),
    ],
)
def test_deployment_config_rejects_missing_required_qualification_inputs(mutation, message: str) -> None:
    validate = getattr(qualification, "validate_deployment_config", None)
    assert callable(validate), "deployment qualification configuration validation is not implemented"
    config = _deployment_config()
    mutation(config)
    with pytest.raises(ValueError, match=message):
        validate(config)


def test_missing_live_run_directory_or_config_is_not_silent_in_required_mode(tmp_path: Path) -> None:
    require = getattr(qualification, "require_prerequisite", None)
    check = getattr(qualification, "validate_release_artifacts", None)
    validate_config = getattr(qualification, "validate_deployment_config", None)
    assert callable(require) and callable(check) and callable(validate_config)

    if os.environ.get("VERIFY_REQUIRED") == "1":
        artifact_dir = os.environ.get("VERIFY_ARTIFACT_DIR")
        config_path = os.environ.get("VERIFY_QUALIFICATION_CONFIG")
        require("release report directory", available=bool(artifact_dir))
        check(Path(artifact_dir), require_final_report=False)
        require("reviewed model/TURN config", available=bool(config_path and Path(config_path).is_file()))
        validate_config(json.loads(Path(config_path).read_text(encoding="utf-8")))
    else:
        with pytest.raises(ValueError, match="release report directory"):
            require("release report directory", available=False)
        with pytest.raises(ValueError, match="reviewed model/TURN config"):
            require("reviewed model/TURN config", available=False)
