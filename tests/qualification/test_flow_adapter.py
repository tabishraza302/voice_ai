"""Formal qualification gate tests for flow adapter qualification per flow-contract §8-9.

Registered under the pytest 'qualification' marker per verification-plan.md §4 and §6.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from bot.adapter_qualification import (
    REQUIRED_CONTRACT_SCENARIOS,
    REQUIRED_SCENARIO_CLASSES,
    QualificationReport,
    build_contract_scenario_results,
    build_unexecuted_scenarios,
    main_async,
    run_qualification,
)
from bot.flow.model_probe import ModelTiming, ProbeObservation
from bot.flow.schemas import AdapterCapabilities
from bot.flow.transport_probe import TransportProbeEvidence, TransportProbeResult

pytestmark = pytest.mark.qualification


@pytest.mark.asyncio
async def test_adapter_qualification_fails_closed_without_explicit_endpoints():
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
    assert report.advertised_configurations == []
    assert report.qualification_scope == "reducer_orchestration_only"


@pytest.mark.asyncio
async def test_adapter_qualification_records_all_seven_contract_scenarios_as_not_run():
    """All seven contract scenarios are enumerated with status NOT_RUN."""
    results = build_contract_scenario_results()
    assert len(results) == 7
    assert [r.scenario_number for r in results] == [1, 2, 3, 4, 5, 6, 7]
    for res in results:
        assert res.passed is False
        assert res.status == "NOT_RUN"
        assert len(res.contract_description) > 0
        assert len(res.error_message) > 0


@pytest.mark.asyncio
async def test_adapter_qualification_rejects_non_loopback_endpoints():
    """Air-gapped policy rejects external or DNS hosts before probe execution."""
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
    ) as mock_model_probe, patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
    ) as mock_transport_probe:
        report = await run_qualification(
            model_endpoint="http://remote.host.internal:11434/v1",
            model_name="qwen3.5:4b",
            transport_endpoint="https://remote.webrtc.io/live",
            source_revision="rev_test",
            profile_hash="hash_test",
        )
        mock_model_probe.assert_not_called()
        mock_transport_probe.assert_not_called()
        assert report.overall_status == "unsupported"
        assert report.task_2_complete is False
        assert any("air-gapped policy" in r for r in report.unsupported_reasons)


@pytest.mark.asyncio
async def test_adapter_qualification_cli_exits_nonzero_when_unsupported():
    """CLI entrypoint returns 1 when qualification is unsupported or Task 2 incomplete."""
    code = await main_async([])
    assert code == 1


@pytest.mark.asyncio
async def test_adapter_qualification_pure_flow_runtime_verified():
    """Pure flow runtime execution is verified while maintaining fail-closed voice qualification."""
    report = await run_qualification(
        model_endpoint=None,
        model_name=None,
        transport_endpoint=None,
        source_revision=None,
        profile_hash=None,
    )
    assert report.flow_runtime_verified is True
    assert report.overall_status == "unsupported"
    assert report.task_2_complete is False


@pytest.mark.asyncio
async def test_adapter_qualification_unadapted_transport_produces_unqualified_playback():
    """Unadapted transport lacking rendered fragment ACKs reports playback unqualified."""
    mock_model_obs = ProbeObservation(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        configured_model="qwen3.5:4b",
        pinned_model_revision="qwen3.5:4b@sha256:abc",
        connected=True,
        supports_separate_stream=False,
        supports_complete_tool=False,
        metadata_strategy="none",
        client_abort_observed=True,
        server_cancellation_verified=False,
        error_handling_verified=True,
        timings=ModelTiming(time_to_first_token_ms=100.0, total_duration_ms=400.0),
    )
    mock_trans_res = TransportProbeResult(
        supported=False,
        playback_capability="unqualified",
        capabilities=None,
        evidence=TransportProbeEvidence(
            adapter_id="webrtc_daily",
            transport_name="webrtc",
            has_audio_out_track=True,
            has_audio_element_render_ack=False,
            has_exact_fragment_binding=False,
            playback_boundary_description="unqualified",
            unsupported_reasons=["Render marker missing"],
        ),
    )
    with patch(
        "bot.flow.qualification_runner.probe_model_qualification",
        new_callable=AsyncMock,
        return_value=mock_model_obs,
    ), patch(
        "bot.flow.qualification_runner.probe_live_webrtc_loopback",
        new_callable=AsyncMock,
        return_value=mock_trans_res,
    ):
        report = await run_qualification(
            model_endpoint="http://127.0.0.1:11434/v1",
            model_name="qwen3.5:4b",
            transport_endpoint="http://127.0.0.1:7860",
            source_revision="rev_git_1",
            profile_hash="prof_hash_1",
        )
        assert report.overall_status == "unsupported"
        assert report.task_2_complete is False
        assert report.transport_observation is not None
        assert report.transport_observation["playback_capability"] == "unqualified"
        assert len(report.missing_evidence) > 0
