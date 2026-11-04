"""Behavioral tests for the factual qualification runner per flow-contract §8-9."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from bot.flow.model_probe import ModelTiming, ProbeObservation
from bot.flow.qualification import CapabilityReport, ScenarioResult
from bot.flow.qualification_runner import (
    REQUIRED_CONTRACT_SCENARIOS,
    REQUIRED_SCENARIO_CLASSES,
    QualificationReport,
    build_contract_scenario_results,
    build_unexecuted_scenarios,
    execute_contract_scenarios,
    main_async,
    parse_args,
    run_qualification,
)
from bot.flow.schemas import AdapterCapabilities
from bot.flow.transport_probe import TransportProbeEvidence, TransportProbeResult


def test_build_unexecuted_scenarios_contains_all_seven_required_classes():
    """All 7 scenario classes from flow-contract §9 are enumerated and marked passed=False (NOT RUN)."""
    scenarios = build_unexecuted_scenarios()
    assert len(scenarios) == 7
    classes = [s.scenario_class for s in scenarios]
    assert classes == [
        "normal_en_hi",
        "adversarial_gating",
        "barge_in",
        "playback_faults",
        "stop_cancellation",
        "chat_durable_commit",
        "full_path_matrix",
    ]
    for s in scenarios:
        assert s.passed is False
        assert s.measurements == {}
        assert s.error_message is not None
        assert len(s.error_message) > 0


def test_build_contract_scenario_results_enumerates_all_seven_numbered_contract_scenarios():
    """Contract scenarios 1–7 faithfully represent flow-contract.md §9 requirements as NOT_RUN."""
    contract_results = build_contract_scenario_results()
    assert len(contract_results) == 7
    numbers = [c.scenario_number for c in contract_results]
    assert numbers == [1, 2, 3, 4, 5, 6, 7]
    for res in contract_results:
        assert res.passed is False
        assert res.status == "NOT_RUN"
        assert res.measurements == {}
        assert len(res.contract_description) > 0
        assert len(res.error_message) > 0
@pytest.mark.asyncio
async def test_run_qualification_missing_explicit_endpoints_fails_closed():
    """Missing explicit endpoints never defaults to dev ports and marks unsupported."""
    report = await run_qualification(
        model_endpoint=None,
        model_name=None,
        transport_endpoint=None,
        source_revision=None,
        profile_hash=None,
    )
    assert report.overall_status == "unsupported"
    assert report.task_2_complete is False
    assert len(report.unsupported_reasons) > 0
    assert len(report.missing_evidence) > 0
    assert len(report.scenarios) == 7
    assert len(report.contract_scenarios) == 7
    assert all(not s.passed for s in report.scenarios)
    assert all(not c.passed for c in report.contract_scenarios)
    assert report.model_observation is None
    assert report.transport_observation is None
    assert report.advertised_configurations == []
    assert report.unsupported_configurations == []
    assert report.qualification_scope == "reducer_orchestration_only"


@pytest.mark.asyncio
async def test_run_qualification_composes_real_model_and_transport_probe_observations():
    """Real observations from model and transport probes are accurately composed."""
    mock_model_obs = ProbeObservation(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        configured_model="qwen3.5:4b",
        pinned_model_revision="qwen3.5:4b@sha256:abc123def456",
        connected=True,
        supports_separate_stream=False,
        supports_complete_tool=False,
        metadata_strategy="none",
        client_abort_observed=True,
        server_cancellation_verified=False,
        error_handling_verified=True,
        timings=ModelTiming(time_to_first_token_ms=120.0, total_duration_ms=450.0),
        unsupported_reasons=["separate stream not supported"],
    )

    cap_report = CapabilityReport(
        adapter_id="webrtc_daily",
        adapter_version="1.0.0",
        model_revision="qwen3.5:4b@sha256:abc123def456",
        agent_settings={
            "llm_provider": "ollama",
            "llm_model": "qwen3.5:4b",
            "stt_provider": "mlx-audio",
            "stt_model": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
            "tts_provider": "mlx-audio",
            "tts_model": "mlx-community/Kokoro-82M-4bit",
            "tts_voice_en": "af_heart",
            "tts_voice_hi": "hf_alpha",
        },
        metadata="none",
        speech_gating=False,
        cancel_generation=False,
        playback="unqualified",
        locales=["en"],
        profile_hash="profile_hash_123",
        source_revision="rev_git_456",
        cases=[],
        playback_boundary_description="Live loopback transport probe without audio render sink",
        limitations=["missing render marker binding"],
    )

    mock_transport_res = TransportProbeResult(
        supported=False,
        playback_capability="unqualified",
        capabilities=AdapterCapabilities(
            adapter_id="webrtc_daily",
            adapter_version="1.0.0",
            model_revision="qwen3.5:4b@sha256:abc123def456",
            metadata="none",
            speech_gating=False,
            cancel_generation=False,
            playback="unqualified",
            locales=["en"],
            report_hash="rep_hash",
        ),
        evidence=TransportProbeEvidence(
            adapter_id="webrtc_daily",
            transport_name="webrtc",
            has_audio_out_track=True,
            has_audio_element_render_ack=False,
            has_exact_fragment_binding=False,
            playback_boundary_description="unqualified",
            unsupported_reasons=["Render marker missing"],
        ),
        report=cap_report,
    )

    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
        return_value=mock_model_obs,
    ), patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
        return_value=mock_transport_res,
    ):
        report = await run_qualification(
            model_endpoint="http://127.0.0.1:11434/v1",
            model_name="qwen3.5:4b",
            transport_endpoint="http://127.0.0.1:7860",
            source_revision="rev_git_456",
            profile_hash="profile_hash_123",
        )

        assert report.overall_status == "unsupported"
        assert report.task_2_complete is False
        assert report.model_observation is not None
        assert report.model_observation.pinned_model_revision == "qwen3.5:4b@sha256:abc123def456"
        assert report.transport_observation is not None
        assert report.transport_observation["playback_capability"] == "unqualified"
        assert report.capability_report == cap_report
        assert len(report.scenarios) == 7
        assert len(report.contract_scenarios) == 7
        assert all(not s.passed for s in report.scenarios)
        assert all(not c.passed for c in report.contract_scenarios)
        assert len(report.unsupported_reasons) > 0
        assert len(report.missing_evidence) > 0
        assert report.advertised_configurations == []
        assert {entry["kind"] for entry in report.unsupported_configurations} == {
            "model",
            "transport",
        }
        assert all(
            entry["advertised"] is False
            for entry in report.unsupported_configurations
        )
        assert all(
            entry["reasons"] for entry in report.unsupported_configurations
        )


@pytest.mark.asyncio
async def test_fake_real_flags_cannot_promote_reducer_scenarios_to_passed():
    """Even fake 'real' probe flags never advertise voice; only reducer orchestration is verified."""
    fake_model = ProbeObservation(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        configured_model="qwen3.5:4b",
        pinned_model_revision="qwen3.5:4b@sha256:" + "a" * 64,
        revision_pinned=True,
        connected=True,
        supports_separate_stream=True,
        supports_complete_tool=True,
        metadata_strategy="separate_stream",
        client_abort_observed=True,
        server_cancellation_verified=True,
        error_handling_verified=True,
        timings=ModelTiming(time_to_first_token_ms=1.0, total_duration_ms=2.0),
        unsupported_reasons=[],
    )
    fake_transport = TransportProbeResult(
        supported=True,
        playback_capability="qualified_fragment_completion",
        capabilities=None,
        evidence=TransportProbeEvidence(
            adapter_id="fake_browser",
            transport_name="fake",
            has_audio_out_track=True,
            has_audio_element_render_ack=True,
            data_channel_receipt_observed=True,
            has_exact_fragment_binding=True,
            has_exact_epoch_binding=True,
            has_exact_hash_binding=True,
            playback_boundary_description="fake flags",
            unsupported_reasons=[],
        ),
        report=None,
    )
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
        return_value=fake_model,
    ), patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
        return_value=fake_transport,
    ):
        report = await run_qualification(
            model_endpoint="http://127.0.0.1:11434/v1",
            model_name="qwen3.5:4b",
            transport_endpoint="http://127.0.0.1:7860",
            source_revision="rev_fake",
            profile_hash="profile_fake",
        )

    assert report.overall_status == "unsupported"
    assert report.task_2_complete is False
    assert report.advertised_configurations == []
    assert all(not scenario.passed for scenario in report.scenarios)
    assert all(
        scenario.status == "UNSUPPORTED" for scenario in report.contract_scenarios
    )
    assert all(
        entry["advertised"] is False
        for entry in report.unsupported_configurations
    )


@pytest.mark.asyncio
async def test_run_qualification_rejects_non_loopback_endpoints_without_network():
    """Air-gapped policy rejects external or DNS hosts before any probe execution."""
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
    ) as mock_model_probe, patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
    ) as mock_transport_probe:
        report = await run_qualification(
            model_endpoint="http://remote-ollama.internal:11434/v1",
            model_name="qwen3.5:4b",
            transport_endpoint="https://api.external.com/webrtc",
            source_revision="rev_git_456",
            profile_hash="profile_hash_123",
        )
        # Probes must NEVER have been called over network
        mock_model_probe.assert_not_called()
        mock_transport_probe.assert_not_called()

        assert report.overall_status == "unsupported"
        assert report.task_2_complete is False
        assert report.model_observation is None
        assert report.transport_observation is None
        assert any("air-gapped policy" in r for r in report.unsupported_reasons)
        assert len(report.scenarios) == 7
        assert len(report.contract_scenarios) == 7
        assert all(not s.passed for s in report.scenarios)
        assert all(not c.passed for c in report.contract_scenarios)

@pytest.mark.asyncio
async def test_main_async_returns_nonzero_when_unsupported():
    """Entrypoint returns 1 when qualification is unsupported or Task 2 incomplete."""
    mock_obs = ProbeObservation(
        provider="ollama",
        base_url="http://10.0.0.1:11434/v1",
        configured_model="qwen3.5:4b",
        pinned_model_revision="qwen3.5:4b@unpinned",
        connected=True,
        supports_separate_stream=False,
        supports_complete_tool=False,
        metadata_strategy="none",
        client_abort_observed=False,
        server_cancellation_verified=False,
        error_handling_verified=False,
        timings=ModelTiming(),
    )
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
        return_value=mock_obs,
    ) as mock_probe:
        code = await main_async([
            "--model-endpoint", "http://127.0.0.1:11434/v1",
            "--model-name", "qwen3.5:4b",
        ])
        assert code == 1
        mock_probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_async_writes_output_file(tmp_path):
    """Entrypoint writes structured JSON to --output path without network calls."""
    out_file = tmp_path / "diag.json"
    code = await main_async([
        "--output", str(out_file),
    ])
    assert code == 1
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["overall_status"] == "unsupported"
    assert data["task_2_complete"] is False
    assert len(data["scenarios"]) == 7
    assert len(data["contract_scenarios"]) == 7
    assert all(not s["passed"] for s in data["scenarios"])
    assert all(not c["passed"] for c in data["contract_scenarios"])
    assert len(data["unsupported_reasons"]) > 0
    assert len(data["missing_evidence"]) > 0


@pytest.mark.asyncio
async def test_run_qualification_verifies_pure_flow_runtime():
    """Runner exercises pure flow runtime and marks flow_runtime_verified."""
    report = await run_qualification(
        model_endpoint=None,
        model_name=None,
        transport_endpoint=None,
        source_revision=None,
        profile_hash=None,
    )
    assert report.flow_runtime_verified is True
    assert report.overall_status == "unsupported"


@pytest.mark.asyncio
async def test_main_async_with_profile_nonexistent_fails_closed(tmp_path):
    """Entrypoint fails closed when specified profile configuration does not exist."""
    missing_profile = tmp_path / "nonexistent_profile.json"
    code = await main_async([
        "--profile", str(missing_profile),
    ])
    assert code == 1


@pytest.mark.asyncio
async def test_main_async_with_profile_populates_endpoints(tmp_path):
    """Entrypoint reads configuration from profile file without network calls."""
    profile_file = tmp_path / "test_profile.json"
    profile_file.write_text(
        json.dumps({
            "model_endpoint": "http://127.0.0.1:11434/v1",
            "model_name": "qwen3.5:4b",
            "transport_endpoint": "http://127.0.0.1:7860",
            "source_revision": "rev_profile_123",
        }),
        encoding="utf-8",
    )
    mock_obs = ProbeObservation(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        configured_model="qwen3.5:4b",
        pinned_model_revision="qwen3.5:4b@sha256:12345",
        connected=True,
        supports_separate_stream=False,
        supports_complete_tool=False,
        metadata_strategy="none",
        client_abort_observed=False,
        server_cancellation_verified=False,
        error_handling_verified=True,
        timings=ModelTiming(),
    )
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
        return_value=mock_obs,
    ) as mock_model_probe, patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
        return_value=TransportProbeResult(
            supported=False,
            playback_capability="unqualified",
            capabilities=None,
            evidence=TransportProbeEvidence(adapter_id="test", transport_name="webrtc"),
        ),
    ) as mock_trans_probe:
        code = await main_async([
            "--profile", str(profile_file),
        ])
        assert code == 1
        mock_model_probe.assert_awaited_once()
        mock_trans_probe.assert_awaited_once()


def test_execute_contract_scenarios_runs_all_seven_and_reports_unsupported_without_playback():
    """Executable scenario verification runs all 7 scenarios across reducer boundary and reports unsupported."""
    c_res, s_res, unsupported, missing = execute_contract_scenarios()
    assert len(c_res) == 7
    assert len(s_res) == 7
    assert all(not c.passed for c in c_res)
    assert all(not s.passed for s in s_res)
    assert all(c.status == "UNSUPPORTED" for c in c_res)
    # All 7 scenarios have empirical execution duration in canonical integers
    for c in c_res:
        assert "reducer_duration_us" in c.measurements
        assert isinstance(c.measurements["reducer_duration_us"], int)
        assert c.measurements["reducer_duration_us"] >= 0
        assert "real voice playback qualification is unsupported" in c.error_message
    for s in s_res:
        assert "reducer_duration_us" in s.measurements
        assert s.raw_evidence.get("reducer_verified") is True
        assert s.raw_evidence.get("has_real_playback") is False
    assert len(missing) > 0


