"""Deployment transport evidence must prove real browser media and TURN use."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import qualification

pytestmark = pytest.mark.qualification


def _config() -> dict:
    return {
        "allowed_origins": ["https://staging.example.invalid"],
        "browser": {"engine": "chromium", "version": "1.64.1"},
        "turn": {
            "required": True,
            "endpoint": "turns:relay.staging.example.invalid:5349",
            "credential_reference": "secret-manager/voice-staging/turn",
        },
    }


def _evidence() -> dict:
    return {
        "schema_version": 1,
        "profile": "deployment",
        "commit": "f" * 40,
        "origin": "https://staging.example.invalid",
        "browser": {"engine": "chromium", "version": "1.64.1", "real_browser": True},
        "tls_verified": True,
        "relay": {
            "endpoint": "turns:relay.staging.example.invalid:5349",
            "relay_used": True,
            "candidate_type": "relay",
        },
        "media": {
            "remote_track_received": True,
            "input_pcm_samples": 1_600,
            "output_pcm_samples": 1_600,
        },
        "turn_completed": True,
        "barge_in_cancelled_audio": True,
        "reconnect_restored_audio": True,
    }


def _validate(evidence: dict, config: dict | None = None) -> None:
    validator = getattr(qualification, "validate_public_transport_evidence", None)
    assert callable(validator), "public transport evidence validation is not implemented"
    validator(evidence, config=config or _config(), release_commit="f" * 40)


def test_transport_evidence_binds_tls_browser_relay_and_audio() -> None:
    _validate(_evidence())


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda data: data.update(commit="e" * 40), "release commit"),
        (lambda data: data["browser"].update(real_browser=False), "real Chromium"),
        (lambda data: data.update(tls_verified=False), "TLS"),
        (lambda data: data["relay"].update(relay_used=False), "TURN relay"),
        (lambda data: data["relay"].update(candidate_type="host"), "relay candidate"),
        (lambda data: data["media"].update(input_pcm_samples=0), "real media"),
        (lambda data: data.update(turn_completed=False), "completed turn"),
        (lambda data: data.update(barge_in_cancelled_audio=False), "barge-in"),
        (lambda data: data.update(reconnect_restored_audio=False), "reconnect"),
    ],
)
def test_fake_or_incomplete_transport_evidence_fails_closed(mutation, message: str) -> None:
    evidence = _evidence()
    mutation(evidence)
    with pytest.raises(ValueError, match=message):
        _validate(evidence)


def test_editable_runner_fields_and_passed_scenario_labels_are_not_live_proof() -> None:
    from bot.flow.qualification_runner import REQUIRED_CONTRACT_SCENARIOS

    run_id = "caller-chosen-run"
    profile_hash = "a" * 64
    commit = "f" * 40
    report = {
        "execution_provenance": {
            "runner": "bot.flow.qualification_runner",
            "run_id": run_id,
            "profile_hash": profile_hash,
            "source_revision": commit,
        },
        "contract_scenarios": [
            {
                "scenario_number": item.number,
                "name": item.name,
                "status": "PASSED",
                "passed": True,
            }
            for item in REQUIRED_CONTRACT_SCENARIOS
        ],
        "overall_status": "supported",
        "task_2_complete": True,
    }

    with pytest.raises(ValueError, match="authenticated live execution/ledger chain"):
        qualification.validate_adapter_qualification_report(
            report,
            run_id=run_id,
            profile_hash=profile_hash,
            source_revision=commit,
            require_supported=True,
            execution_key="b" * 64,
        )

def test_transport_report_requires_current_run_qualified_adapter_scenarios() -> None:
    if os.environ.get("RUN_TRANSPORT_QUALIFICATION") != "1":
        pytest.skip("deployment transport qualification is not enabled")

    config_path_value = os.environ.get("VERIFY_QUALIFICATION_CONFIG")
    artifact_dir_value = os.environ.get("VERIFY_ARTIFACT_DIR")
    run_id = os.environ.get("VERIFY_RUN_ID")
    profile_hash = os.environ.get("VERIFY_PROFILE_SHA256")
    signing_key = os.environ.get("VERIFY_EVIDENCE_KEY")
    if not config_path_value or not artifact_dir_value or not run_id or not profile_hash or not signing_key:
        pytest.fail("missing prerequisite: current-run deployment adapter execution context")
    config_path = Path(config_path_value)
    artifact_dir = Path(artifact_dir_value)
    assert config_path.is_absolute() and config_path.is_file(), (
        "deployment transport config is missing"
    )
    assert artifact_dir.is_absolute(), "deployment transport report directory must be absolute"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    qualification.validate_deployment_config(config)
    manifest = qualification.load_release_manifest(artifact_dir / "manifest.json")
    report_path = qualification.require_report(
        artifact_dir / "adapter-qualification-deployment.json",
        "current-run deployment adapter qualification",
    )
    envelope = json.loads(report_path.read_text(encoding="utf-8"))
    report = qualification.validate_live_evidence(
        envelope,
        kind="adapter_qualification",
        run_id=run_id,
        profile="deployment",
        profile_hash=profile_hash,
        commit=manifest["commit"],
        key=signing_key,
    )
    qualification.validate_adapter_qualification_report(
        report,
        run_id=run_id,
        profile_hash=profile_hash,
        source_revision=manifest["commit"],
        execution_key=signing_key,
        require_supported=True,
    )
