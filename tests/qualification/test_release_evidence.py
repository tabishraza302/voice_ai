"""Release evidence binds all gate reports and the staging runbook to one commit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bot.provider_registry import MLX_KOKORO_MODEL, NEMOTRON_MODEL
from tests.support import qualification

pytestmark = pytest.mark.qualification


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "first-release" / "ops-runbook.md"
MODEL_HASH = "d" * 64


def _qualification_settings() -> dict:
    return {
        "model_endpoint": "http://127.0.0.1:11434",
        "transport_endpoint": "http://127.0.0.1:8080",
        "adapter_version": "qualification-adapter-1",
        "agent_settings": {
            "llm_model": "llm-id",
            "stt_provider": "mlx-audio",
            "stt_model": NEMOTRON_MODEL,
            "tts_provider": "mlx-audio",
            "tts_model": MLX_KOKORO_MODEL,
            "tts_voice_en": "af_heart",
            "tts_voice_hi": "hf_alpha",
        },
        "locales": ["en", "hi"],
        "source_revision": "f" * 40,
    }


def _models() -> dict:
    return {
        "llm": {"id": "llm-id", "revision": "llm-revision", "sha256": MODEL_HASH},
        "stt": {"id": NEMOTRON_MODEL, "revision": "stt-revision", "sha256": MODEL_HASH},
        "tts_en": {
            "id": MLX_KOKORO_MODEL,
            "revision": "tts-en-revision",
            "sha256": MODEL_HASH,
        },
        "tts_hi": {
            "id": MLX_KOKORO_MODEL,
            "revision": "tts-hi-revision",
            "sha256": MODEL_HASH,
        },
    }


def _deployment_config() -> dict:
    return {
        "schema_version": 1,
        "profile": "deployment",
        "allowed_origins": ["https://staging.example.invalid"],
        "models": _models(),
        "runner_api_endpoints": {
            "runner": "https://runner.staging.example.invalid",
            "api": "https://api.staging.example.invalid",
            "flags": "https://flags.staging.example.invalid/health/flags",
        },
        "qualification": _qualification_settings(),
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


def _local_config() -> dict:
    return {
        "schema_version": 1,
        "profile": "local_offline",
        "models": _models(),
        "qualification": _qualification_settings(),
        "concurrency": 1,
        "limits": {
            "turn_timeout_ms": 30_000,
            "latency_p95_ms": 2_000,
            "quality_threshold": 0.9,
        },
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

def test_local_and_deployment_profiles_bind_the_exact_model_tuple() -> None:
    validate_local = getattr(qualification, "validate_local_qualification_config", None)
    validate_deployment = getattr(qualification, "validate_deployment_config", None)
    assert callable(validate_local) and callable(validate_deployment)
    validate_local(_local_config())
    validate_deployment(_deployment_config())

    bad_model = _local_config()
    bad_model["qualification"]["agent_settings"]["llm_model"] = "unpinned-model"
    with pytest.raises(ValueError, match="does not match the pinned LLM"):
        validate_local(bad_model)


def test_profile_hash_accepts_valid_float_limits_and_is_order_independent() -> None:
    config = _deployment_config()
    qualification.validate_deployment_config(config)

    profile_hash = qualification.profile_sha256(config)
    reordered = dict(reversed(tuple(config.items())))
    assert qualification.profile_sha256(reordered) == profile_hash

    changed = _deployment_config()
    changed["limits"]["quality_threshold"] = 0.91
    assert qualification.profile_sha256(changed) != profile_hash


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda data: data["runner_api_endpoints"].pop("flags"), "flags endpoint"),
        (
            lambda data: data["runner_api_endpoints"].update(
                flags="https://flags.staging.example.invalid/health/flags?editable=true"
            ),
            "credential-free",
        ),
        (
            lambda data: data["qualification"].update(model_endpoint="https://model.example.invalid"),
            "loopback address",
        ),
        (
            lambda data: data["qualification"]["agent_settings"].update(llm_model="other-model"),
            "does not match the pinned LLM",
        ),
    ],
)
def test_profiles_reject_unqualified_endpoints_and_model_drift(mutation, message: str) -> None:
    config = _deployment_config()
    mutation(config)
    with pytest.raises(ValueError, match=message):
        qualification.validate_deployment_config(config)


def test_hmac_binds_verifier_envelope_but_not_payload_origin() -> None:
    authenticate = getattr(qualification, "authenticate_live_evidence", None)
    validate = getattr(qualification, "validate_live_evidence", None)
    assert callable(authenticate) and callable(validate)
    payload = {"scenarios": ["actual-run-result"]}
    kwargs = {
        "kind": "adapter_qualification",
        "run_id": "run-20261106T000000Z-12345678",
        "profile": "deployment",
        "profile_hash": "a" * 64,
        "commit": "f" * 40,
        "key": "12" * 32,
    }
    envelope = authenticate(payload, **kwargs)
    assert validate(envelope, **kwargs) == payload

    with pytest.raises(ValueError, match="missing or unknown fields"):
        validate(payload, **kwargs)
    altered = dict(envelope)
    altered["payload"] = {"scenarios": ["hand-edited"]}
    with pytest.raises(ValueError, match="HMAC is invalid"):
        validate(altered, **kwargs)
    with pytest.raises(ValueError, match="different verification run"):
        validate(envelope, **{**kwargs, "run_id": "different-run"})



def test_missing_live_run_directory_or_config_is_not_silent_in_required_mode(tmp_path: Path) -> None:
    require = getattr(qualification, "require_prerequisite", None)
    check = getattr(qualification, "validate_release_artifacts", None)
    validate_config = getattr(qualification, "validate_deployment_config", None)
    assert callable(require) and callable(check) and callable(validate_config)

    if os.environ.get("VERIFY_REQUIRED") == "1":
        artifact_dir = os.environ.get("VERIFY_ARTIFACT_DIR")
        deployment_path = os.environ.get("VERIFY_QUALIFICATION_CONFIG")
        local_path = os.environ.get("VERIFY_LOCAL_QUALIFICATION_CONFIG")
        audio_root = os.environ.get("VERIFY_AUDIO_ROOT")
        require("release report directory", available=bool(artifact_dir))
        require(
            "reviewed deployment profile",
            available=bool(deployment_path and Path(deployment_path).is_file()),
        )
        require(
            "reviewed local profile",
            available=bool(local_path and Path(local_path).is_file()),
        )
        require("reviewed audio fixtures", available=bool(audio_root and Path(audio_root).is_dir()))
        profiles = {
            "deployment": json.loads(Path(deployment_path).read_text(encoding="utf-8")),
            "local_offline": json.loads(Path(local_path).read_text(encoding="utf-8")),
        }
        validate_config(profiles["deployment"])
        qualification.validate_local_qualification_config(profiles["local_offline"])
        fixtures = qualification.load_audio_fixture_manifest(
            Path(audio_root) / "audio_manifest.json",
            audio_root=Path(audio_root),
        )
        check(
            Path(artifact_dir),
            require_final_report=False,
            evidence_key=os.environ.get("VERIFY_EVIDENCE_KEY"),
            profile_hashes={
                "local_offline": os.environ.get("VERIFY_LOCAL_PROFILE_SHA256"),
                "deployment": os.environ.get("VERIFY_DEPLOYMENT_PROFILE_SHA256"),
            },
            profile_configs={
                "local_offline": profiles["local_offline"],
                "deployment": profiles["deployment"],
            },
            audio_fixtures=fixtures["fixtures"],
        )
    else:
        with pytest.raises(ValueError, match="release report directory"):
            require("release report directory", available=False)
        with pytest.raises(ValueError, match="reviewed model/TURN config"):
            require("reviewed model/TURN config", available=False)
