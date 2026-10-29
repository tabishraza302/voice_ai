from __future__ import annotations

import json
import pathlib

import pytest

from bot.flow import compile_flow
from bot.flow.schemas import FragmentPlayedOutputEvent
from bot.flow_runtime import FlowRuntime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _compiled_flow():
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/flow/valid-credit-card-en-hi.json").read_text(
            encoding="utf-8"
        )
    )
    return compile_flow(fixture["flow"])


@pytest.mark.asyncio
async def test_runtime_binds_state_to_compiled_hashes_and_transport_epoch():
    compiled = _compiled_flow()

    runtime = FlowRuntime(compiled, "session-1", "epoch-1", locale="hi")

    assert runtime.state.session_id == "session-1"
    assert runtime.state.transport_epoch == "epoch-1"
    assert runtime.state.flow_hash == compiled.flow_hash
    assert runtime.state.artifact_hash == compiled.artifact_hash
    assert runtime.state.locale == "hi"


@pytest.mark.asyncio
async def test_unverified_playback_receipt_cannot_complete_a_fragment():
    compiled = _compiled_flow()
    runtime = FlowRuntime(compiled, "session-1", "epoch-1")
    event = FragmentPlayedOutputEvent(
        event_id="event-1",
        session_id="session-1",
        transport_epoch="epoch-1",
        turn_id="turn-1",
        generation_id="generation-1",
        type="fragment_played",
        manifest_id="manifest-1",
        fragment_id="fragment-1",
        content_hash="a" * 64,
        evidence_id="unverified-receipt",
        capability_report_hash="0" * 64,
    )
    before = runtime.state.model_dump(mode="json")

    reduction = await runtime.accept_output_event(event)

    assert reduction.state.model_dump(mode="json") == before
    assert reduction.diagnostics
    assert reduction.diagnostics[0].code == "PLAYBACK_UNQUALIFIED"
    assert runtime.state.delivered_fragment_ids == []
    assert runtime.state.outcome == "in_progress"


@pytest.mark.asyncio
async def test_pipeline_close_aborts_once_and_never_reports_success():
    runtime = FlowRuntime(_compiled_flow(), "session-1", "epoch-1")
    await runtime.accept_transcription("I would like to continue.")

    first = await runtime.close("transport_disconnected")
    state_after_close = runtime.state.model_dump(mode="json")
    second = await runtime.close("duplicate_close")

    assert first.state.outcome == "aborted"
    assert first.state.teardown_emitted is True
    assert runtime.state.model_dump(mode="json") == state_after_close
    assert second.commands == []
    assert runtime.state.outcome != "completed"
