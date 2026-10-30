"""Trusted deployment adapter qualification report source tests."""

from __future__ import annotations

import base64
import json

from dataclasses import replace

import pytest
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request

from bot.flow.qualification import (
    REQUIRED_SCENARIO_CLASSES,
    CapabilityReport,
    ScenarioResult,
)
from bot.flow.schemas import AdapterCapabilities
from platform_api.agents.routes import _publication_context
from platform_api.agents.validation import validate_voice_capability
from platform_api.app import create_app
from platform_api.config import PlatformSettings
from platform_api.qualification import (
    DeploymentIdentity,
    load_deployment_capability_report,
)
from tests.support.postgres import FreshPostgres


def _unqualified_report() -> CapabilityReport:
    report = CapabilityReport(
        protocol_version=2,
        adapter_id="test-adapter",
        adapter_version="test-version",
        model_revision="test-model-revision",
        agent_settings={
            "llm_model": "qwen3.5:4b",
            "stt_provider": "mlx-audio",
            "tts_voice_en": "af_heart",
            "tts_voice_hi": "hf_alpha",
        },
        metadata="none",
        speech_gating=False,
        cancel_generation=False,
        playback="unqualified",
        locales=["en"],
        profile_hash="test-profile-hash",
        source_revision="test-source-revision",
        fixture_hashes={"transport_probe": "f" * 64},
        cases=[
            ScenarioResult(
                scenario_id=f"case_{scenario_class}",
                scenario_class=scenario_class,
                passed=True,
                measurements={"latency_ms": 1},
                raw_evidence={"evidence_id": f"evidence_{scenario_class}"},
                trace_id=f"trace_{scenario_class}",
            )
            for scenario_class in REQUIRED_SCENARIO_CLASSES
        ],
        measurements={"latency_ms": 1},
        raw_evidence_references=["sha256:" + "e" * 64],
        playback_boundary_description="Playback is explicitly unqualified.",
    )
    return report.model_copy(update={"report_hash": report.compute_hash()})


def _write_report(tmp_path, report: CapabilityReport):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json")),
        encoding="utf-8",
    )
    return path

def _test_settings() -> PlatformSettings:
    return PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_test",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=base64.urlsafe_b64encode(b"M" * 32).decode("ascii"),
    )


def test_missing_deployment_report_does_not_create_capabilities() -> None:
    assert load_deployment_capability_report(None, None) is None


def test_report_requires_a_pinned_hash(tmp_path) -> None:
    report = _unqualified_report()
    path = _write_report(tmp_path, report)

    with pytest.raises(ValueError):
        load_deployment_capability_report(str(path), None)


def test_report_hash_pins_the_exact_capability_and_agent_settings_tuple(tmp_path) -> None:
    report = _unqualified_report()
    path = _write_report(tmp_path, report)

    loaded = load_deployment_capability_report(str(path), report.report_hash)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == report.model_dump(mode="json")

    altered_tuples = (
        ("adapter_id", "other-adapter"),
        ("adapter_version", "other-version"),
        ("model_revision", "other-model-revision"),
        ("profile_hash", "other-profile-hash"),
        ("source_revision", "other-source-revision"),
        ("metadata", "complete_tool"),
        ("speech_gating", True),
        ("cancel_generation", True),
        ("playback", "qualified_fragment_completion"),
        ("locales", ["en", "hi"]),
        (
            "agent_settings",
            report.agent_settings.model_copy(update={"llm_model": "other-model"}),
        ),
        (
            "agent_settings",
            report.agent_settings.model_copy(update={"stt_provider": "other-stt"}),
        ),
        (
            "agent_settings",
            report.agent_settings.model_copy(update={"tts_voice_en": "other-en"}),
        ),
        (
            "agent_settings",
            report.agent_settings.model_copy(update={"tts_voice_hi": "other-hi"}),
        ),
    )
    for field, value in altered_tuples:
        altered = report.model_copy(update={field: value})
        altered = altered.model_copy(update={"report_hash": altered.compute_hash()})
        _write_report(tmp_path, altered)

        with pytest.raises(ValueError):
            load_deployment_capability_report(str(path), report.report_hash)


def test_voice_settings_must_match_the_pinned_report_tuple() -> None:
    report = _unqualified_report()
    capabilities = AdapterCapabilities(
        adapter_id=report.adapter_id,
        adapter_version=report.adapter_version,
        model_revision=report.model_revision,
        metadata=report.metadata,
        speech_gating=report.speech_gating,
        cancel_generation=report.cancel_generation,
        playback=report.playback,
        locales=list(report.locales),
        report_hash=report.report_hash,
    )
    config = {
        "schema_version": 1,
        "steps": [],
        "llm_model": "qwen3.5:4b",
        "stt_provider": "mlx-audio",
        "tts_voice_en": "af_heart",
        "tts_voice_hi": "hf_alpha",
    }

    # Matching settings still fail closed because this report is unqualified.
    matching_errors = validate_voice_capability(
        config,
        capabilities,
        report.agent_settings,
    )
    assert [diagnostic.path for diagnostic in matching_errors] == ["/capabilities"]

    for key in ("llm_model", "stt_provider", "tts_voice_en", "tts_voice_hi"):
        mismatched = {**config, key: "not-pinned"}
        diagnostics = validate_voice_capability(
            mismatched,
            capabilities,
            report.agent_settings,
        )
        assert any(
            diagnostic.path == f"/{key}"
            and diagnostic.code == "PLAYBACK_UNQUALIFIED"
            for diagnostic in diagnostics
        )



def test_publication_context_requires_exact_trusted_deployment_identity() -> None:
    test_platform_settings = _test_settings()
    report = _unqualified_report()
    settings = test_platform_settings.model_copy(
        update={
            "adapter_capability_report_hash": report.report_hash,
            "deployment_adapter_id": report.adapter_id,
            "deployment_adapter_version": report.adapter_version,
            "deployment_model_revision": report.model_revision,
            "deployment_profile_hash": report.profile_hash,
            "deployment_source_revision": report.source_revision,
        }
    )
    app = create_app(settings)
    app.state.adapter_capability_report = report
    request = Request({"type": "http", "app": app})

    unconfigured_settings = test_platform_settings.model_copy(
        update={
            "deployment_adapter_id": None,
            "deployment_adapter_version": None,
            "deployment_model_revision": None,
            "deployment_profile_hash": None,
            "deployment_source_revision": None,
        }
    )
    assert DeploymentIdentity.from_settings(unconfigured_settings) is None
    app.state.deployment_identity = None
    assert _publication_context(request) == (None, None)

    identity = DeploymentIdentity.from_settings(settings)
    assert identity is not None
    assert identity.matches_report(report)
    app.state.deployment_identity = identity
    capabilities, qualification_settings = _publication_context(request)
    assert capabilities is not None
    assert capabilities.adapter_id == report.adapter_id
    assert qualification_settings == report.agent_settings

    for field in (
        "adapter_id",
        "adapter_version",
        "model_revision",
        "profile_hash",
        "source_revision",
    ):
        app.state.deployment_identity = replace(
            identity,
            **{field: "different-deployment-value"},
        )
        assert _publication_context(request) == (None, None)

    partial_identity = unconfigured_settings.model_dump()
    partial_identity["deployment_adapter_id"] = "test-adapter"
    with pytest.raises(PydanticValidationError):
        PlatformSettings.model_validate(partial_identity)


def test_report_requires_all_contract_scenarios(tmp_path) -> None:
    report = _unqualified_report()
    incomplete = report.model_copy(update={"cases": report.cases[:-1]})
    incomplete = incomplete.model_copy(
        update={"report_hash": incomplete.compute_hash()}
    )
    path = _write_report(tmp_path, incomplete)

    with pytest.raises(ValueError):
        load_deployment_capability_report(str(path), incomplete.report_hash)


def test_qualified_report_without_playback_evidence_is_rejected(tmp_path) -> None:
    report = _unqualified_report().model_copy(
        update={
            "playback": "qualified_fragment_completion",
            "raw_evidence_references": [],
        }
    )
    report = report.model_copy(update={"report_hash": report.compute_hash()})
    path = _write_report(tmp_path, report)

    with pytest.raises(ValueError):
        load_deployment_capability_report(str(path), report.report_hash)


def test_report_with_boolean_protocol_version_is_rejected(tmp_path) -> None:
    report = _unqualified_report()
    invalid = report.model_dump(mode="json")
    invalid["protocol_version"] = True
    path = tmp_path / "report.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ValueError):
        load_deployment_capability_report(str(path), report.report_hash)


def test_report_path_and_hash_must_be_configured_together() -> None:
    test_platform_settings = _test_settings()
    settings = test_platform_settings.model_dump()
    settings["adapter_capability_report_path"] = "/qualification/report.json"

    with pytest.raises(PydanticValidationError):
        PlatformSettings.model_validate(settings)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_app_lifespan_loads_and_releases_deployment_report(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    tmp_path,
) -> None:
    report = _unqualified_report()
    path = _write_report(tmp_path, report)
    settings = test_platform_settings.model_copy(
        update={
            "adapter_capability_report_path": str(path),
            "adapter_capability_report_hash": report.report_hash,
            "deployment_adapter_id": report.adapter_id,
            "deployment_adapter_version": report.adapter_version,
            "deployment_model_revision": report.model_revision,
            "deployment_profile_hash": report.profile_hash,
            "deployment_source_revision": report.source_revision,
        }
    )
    app = create_app(settings)

    assert app.state.adapter_capability_report is None
    async with app.router.lifespan_context(app):
        loaded = app.state.adapter_capability_report
        assert loaded.report_hash == report.report_hash
        assert app.state.deployment_identity.matches_report(loaded)
        assert loaded.adapter_version == report.adapter_version
    assert app.state.adapter_capability_report is None
    assert app.state.deployment_identity is None
