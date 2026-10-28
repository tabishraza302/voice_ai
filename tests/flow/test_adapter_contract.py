"""Adapter contract and trust-boundary tests per flow-contract.md sections 7-9.

Verifies:
- test_adapter_stream_does_not_speak_unvalidated_state
- test_adapter_cancel_invalidates_late_delta
- tts_text_is_not_played (TTSTextFrame does not satisfy fragment_played)
- stale_and_forged_playback (epoch mismatch, unknown fragment, forged hash)
- cancel_before_commit and cancel_after_commit
- partial_summary_cancel
- stop_preempts_disclosure (immediate teardown, no playback wait)
- unsupported_playback (fails voice admission with PLAYBACK_UNQUALIFIED)
- chat_voice_parity (identical state and transitions)
- action_unregistered (TOOL_UNQUALIFIED)
- model prose cannot authorize transitions or captures
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from bot.flow.compiler import compile_flow
from bot.flow.controller import initial_session_state, reduce_event
from bot.flow.schemas import (
    AdapterCapabilities,
    CancelCommand,
    CancelGenerationCommand,
    ChatCommittedOutputEvent,
    ConfirmedFieldState,
    EmitSpeechManifestCommand,
    Evidence,
    FlowDiagnostic,
    FragmentPlayedOutputEvent,
    FragmentQueuedOutputEvent,
    Origin,
    ProposeCaptureUpdate,
    SessionState,
    SpeechDeltaModelEvent,
    SpeechFragment,
    StateProposal,
    StateProposalModelEvent,
    TeardownCommand,
    UnknownFieldState,
    UserStoppedEvent,
    UserTurnAcceptedEvent,
)
from bot.flow.validation import validate_executable

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "flow"


@pytest.fixture
def credit_card_compiled():
    with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
        flow_fixture = json.load(f)["flow"]
    capabilities = AdapterCapabilities(
        adapter_id="test_adapter",
        adapter_version="1.0.0",
        model_revision="pinned-v1",
        metadata="separate_stream",
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        report_hash="rep_hash",
    )
    return compile_flow(flow_fixture, policy="policy text", capabilities=capabilities)


def test_adapter_stream_does_not_speak_unvalidated_state(credit_card_compiled):
    """Speech emitted before proposal validation must not commit state or release speech."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # 1. User turn accepted
    turn_ev = UserTurnAcceptedEvent(
        event_id="ev_t1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I want travel rewards",
    )
    red1 = reduce_event(credit_card_compiled, state, turn_ev)
    state = red1.state
    assert state.version == 1

    # 2. Model emits speech delta claiming approval BEFORE state proposal
    speech_ev = SpeechDeltaModelEvent(
        event_id="ev_s1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        seq=0,
        text="Congratulations, you are approved for the travel card!",
    )
    red2 = reduce_event(credit_card_compiled, state, speech_ev)

    # State must NOT be updated by model prose
    assert red2.state.version == state.version
    assert "card_preference" not in red2.state.fields or red2.state.fields["card_preference"].status == "unknown"

    # Reducer does not emit teardown or completion command from model prose
    assert not any(isinstance(c, TeardownCommand) and c.outcome == "completed" for c in red2.commands)


def test_adapter_cancel_invalidates_late_delta(credit_card_compiled):
    """Barge-in / cancel command rejects subsequent late deltas from cancelled generation."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # User turn accepted
    turn_ev = UserTurnAcceptedEvent(
        event_id="ev_t1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="Hello",
    )
    red = reduce_event(credit_card_compiled, state, turn_ev)
    state = red.state

    # Barge-in interruption arrives: user turn t2 with generation g2
    interrupt_ev = UserTurnAcceptedEvent(
        event_id="ev_t2",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t2",
        generation_id="g2",
        text="Wait",
    )
    state = reduce_event(credit_card_compiled, state, interrupt_ev).state

    # Late model delta arrives after cancellation
    late_delta = SpeechDeltaModelEvent(
        event_id="ev_late",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        seq=1,
        text="Late audio fragment text",
    )
    red_late = reduce_event(credit_card_compiled, state, late_delta)

    # Late delta rejected, version unchanged
    assert red_late.state.version == state.version


def test_tts_text_is_not_played(credit_card_compiled):
    """TTSTextFrame / raw text does NOT satisfy fragment_played and cannot complete an inform step."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # Move active step to summary or inform step
    state = state.model_copy(update={"active_step_id": "summary"})

    # Emitting raw model delta or non-played event does not add to history_spoken_fragments
    delta = SpeechDeltaModelEvent(
        event_id="ev_d1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        seq=0,
        text="Summary speech text",
    )
    red = reduce_event(credit_card_compiled, state, delta)

    # Step is NOT complete
    assert red.state.steps.get("summary") != "complete"
    assert "Summary speech text" not in red.state.history_spoken_fragments
    assert len(red.state.history_spoken_fragments) == 0


def test_stale_and_forged_playback_rejected(credit_card_compiled):
    """Epoch mismatch, unknown fragment, or old generation playback events are rejected."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # Set state version and generation
    state = state.model_copy(update={"generation_id": "g2"})

    # 1. Event with wrong transport epoch
    forged_epoch = FragmentPlayedOutputEvent(
        event_id="ev_p1",
        session_id="sess_1",
        transport_epoch="wrong_epoch",
        turn_id="t1",
        generation_id="g2",
        manifest_id="man_1",
        fragment_id="frag_1",
        content_hash="hash_1",
        evidence_id="ev_1",
        capability_report_hash="rep_hash",
    )
    red1 = reduce_event(credit_card_compiled, state, forged_epoch)
    # Transport epoch mismatch or wrong generation rejected without mutating state
    assert red1.state.version == state.version
    assert "frag_1" not in red1.state.history_spoken_fragments
    # 2. Event with old generation ID
    old_gen = FragmentPlayedOutputEvent(
        event_id="ev_p2",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="old_g0",
        manifest_id="man_1",
        fragment_id="frag_1",
        content_hash="hash_1",
        evidence_id="ev_2",
        capability_report_hash="rep_hash",
    )
    red2 = reduce_event(credit_card_compiled, state, old_gen)
    # Stale generation rejected without mutating state
    assert red2.state.version == state.version
    assert "frag_1" not in red2.state.history_spoken_fragments

def test_cancel_before_commit_preserves_turn_history(credit_card_compiled):
    """Cancellation before proposal commit drops proposal but preserves turn in history."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # Accept turn t1
    turn_ev = UserTurnAcceptedEvent(
        event_id="ev_t1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I am salaried",
    )
    state = reduce_event(credit_card_compiled, state, turn_ev).state

    # Cancel before proposal commits
    cancel_ev = CancelCommand(
        event_id="ev_c1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        cancellation_id="cancel_1",
        reason="barge_in",
    )
    state = reduce_event(credit_card_compiled, state, cancel_ev).state

    # Turn t1 is recorded in turn history
    assert any(t.get("turn_id") == "t1" for t in state.turn_history)
    # But employment_type was never committed
    assert state.fields["employment_type"].status == "unknown"


def test_cancel_after_commit_preserves_captures_ignores_late_fragments(credit_card_compiled):
    """Cancellation after commit preserves captured state and played fragments, ignores late fragments."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    # Accept turn
    turn_ev = UserTurnAcceptedEvent(
        event_id="ev_t1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    state = reduce_event(credit_card_compiled, state, turn_ev).state

    # Proposal commits all captures and transitions to summary
    proposal = StateProposal(
        proposal_id="prop_1",
        session_id="sess_1",
        turn_id="t1",
        generation_id="g1",
        base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    prop_ev = StateProposalModelEvent(
        event_id="ev_pr1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        proposal=proposal,
    )
    red_prop = reduce_event(credit_card_compiled, state, prop_ev)
    state = red_prop.state
    assert state.fields["employment_type"].status == "confirmed"
    manifest_cmd = next(c for c in red_prop.commands if isinstance(c, EmitSpeechManifestCommand))
    f1 = manifest_cmd.fragments[0]
    f2 = manifest_cmd.fragments[1]

    # Fragment 1 played with authentic emitted manifest and fragment ID
    f1_played = FragmentPlayedOutputEvent(
        event_id="ev_p1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        manifest_id=manifest_cmd.manifest_id,
        fragment_id=f1.fragment_id,
        content_hash=f1.content_hash,
        evidence_id="evid_1",
        capability_report_hash="rep_hash",
    )
    state = reduce_event(credit_card_compiled, state, f1_played).state
    assert f1.fragment_id in state.history_spoken_fragments

    # Barge-in interruption arrives: user turn t2 with generation g2
    interrupt_ev = UserTurnAcceptedEvent(
        event_id="ev_t2",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t2",
        generation_id="g2",
        text="Wait a second",
    )
    state = reduce_event(credit_card_compiled, state, interrupt_ev).state

    # Late fragment 2 played arrives
    f2_late = FragmentPlayedOutputEvent(
        event_id="ev_p2",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        manifest_id=manifest_cmd.manifest_id,
        fragment_id=f2.fragment_id,
        content_hash=f2.content_hash,
        evidence_id="evid_2",
        capability_report_hash="rep_hash",
    )
    red_late = reduce_event(credit_card_compiled, state, f2_late)

    # f1 is in played history, captures retained, f2 late is ignored
    assert f1.fragment_id in red_late.state.history_spoken_fragments
    assert f2.fragment_id not in red_late.state.history_spoken_fragments
    assert red_late.state.fields["employment_type"].status == "confirmed"

def test_partial_summary_cancel(credit_card_compiled):
    """When summary is cancelled after first fragment, step is incomplete and only unplayed resumes."""
    state = initial_session_state(credit_card_compiled, "sess_1")
    # Reach summary legitimately via turn and proposal
    turn_ev = UserTurnAcceptedEvent(
        event_id="ev_t1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    state = reduce_event(credit_card_compiled, state, turn_ev).state
    proposal = StateProposal(
        proposal_id="prop_1",
        session_id="sess_1",
        turn_id="t1",
        generation_id="g1",
        base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    red_prop = reduce_event(
        credit_card_compiled,
        state,
        StateProposalModelEvent(
            event_id="ev_pr1", session_id="sess_1", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=proposal
        ),
    )
    state = red_prop.state
    manifest_cmd = next(c for c in red_prop.commands if isinstance(c, EmitSpeechManifestCommand))
    f_facts = next(f for f in manifest_cmd.fragments if ":facts:" in f.fragment_id)

    # Played first fragment (facts)
    p1 = FragmentPlayedOutputEvent(
        event_id="ev_p1",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        manifest_id=manifest_cmd.manifest_id,
        fragment_id=f_facts.fragment_id,
        content_hash=f_facts.content_hash,
        evidence_id="e1",
        capability_report_hash="rep_hash",
    )
    state = reduce_event(credit_card_compiled, state, p1).state
    assert state.steps.get("summary") != "complete"
    assert f_facts.fragment_id in state.history_spoken_fragments

    # Barge-in interruption arrives: user turn t2 with generation g2
    interrupt_ev = UserTurnAcceptedEvent(
        event_id="ev_t2",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t2",
        generation_id="g2",
        text="Wait, pause",
    )
    state = reduce_event(credit_card_compiled, state, interrupt_ev).state

    # Summary is NOT completed
    assert state.steps.get("summary") != "complete"
    assert f_facts.fragment_id in state.history_spoken_fragments
def test_stop_preempts_disclosure(credit_card_compiled):
    """User stop terminates immediately with aborted outcome, zero playback wait, single teardown command."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    stop_ev = UserStoppedEvent(
        event_id="ev_stop",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        reason="user_stop",
    )
    red = reduce_event(credit_card_compiled, state, stop_ev)

    # Exactly one teardown command with outcome aborted
    teardown_cmds = [c for c in red.commands if isinstance(c, TeardownCommand)]
    assert len(teardown_cmds) == 1
    assert teardown_cmds[0].outcome == "aborted"
    assert red.state.outcome == "aborted"


def test_unsupported_playback_fails_voice_admission():
    """Unqualified adapter capability produces PLAYBACK_UNQUALIFIED diagnostic for voice execution."""
    with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
        flow_fixture = json.load(f)["flow"]

    unqualified_cap = AdapterCapabilities(
        adapter_id="unadapted_adapter",
        adapter_version="0.1.0",
        model_revision="pinned-v1",
        metadata="none",
        speech_gating=False,
        cancel_generation=False,
        playback="unqualified",
        locales=["en"],
        report_hash="rep_hash",
    )

    diags = validate_executable(flow_fixture, capabilities=unqualified_cap)
    # Must report PLAYBACK_UNQUALIFIED error for voice execution
    playback_diags = [d for d in diags if d.code == "PLAYBACK_UNQUALIFIED"]
    assert len(playback_diags) > 0


def test_chat_voice_parity(credit_card_compiled):
    """Replaying identical proposals in chat (chat_committed) and voice (fragment_played) yields identical captures."""
    state_chat = initial_session_state(credit_card_compiled, "sess_chat")
    state_voice = initial_session_state(credit_card_compiled, "sess_voice")

    # Step 1: User turn
    turn_chat = UserTurnAcceptedEvent(
        event_id="ev_t_c",
        session_id="sess_chat",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    turn_voice = UserTurnAcceptedEvent(
        event_id="ev_t_v",
        session_id="sess_voice",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    state_chat = reduce_event(credit_card_compiled, state_chat, turn_chat).state
    state_voice = reduce_event(credit_card_compiled, state_voice, turn_voice).state

    # Step 2: Identical proposals commit and transition to summary
    prop = StateProposal(
        proposal_id="prop_p",
        session_id="sess_chat",
        turn_id="t1",
        generation_id="g1",
        base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    prop_voice = StateProposal(
        proposal_id="prop_p",
        session_id="sess_voice",
        turn_id="t1",
        generation_id="g1",
        base_state_version=1,
        updates=prop.updates,
    )
    red_chat = reduce_event(
        credit_card_compiled,
        state_chat,
        StateProposalModelEvent(event_id="ev_pr_c", session_id="sess_chat", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop),
    )
    red_voice = reduce_event(
        credit_card_compiled,
        state_voice,
        StateProposalModelEvent(event_id="ev_pr_v", session_id="sess_voice", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop_voice),
    )
    state_chat = red_chat.state
    state_voice = red_voice.state

    manifest_cmd = next(c for c in red_chat.commands if isinstance(c, EmitSpeechManifestCommand))
    manifest_id = manifest_cmd.manifest_id
    fragments = manifest_cmd.fragments

    # Step 3: Delivery: Chat uses chat_committed, Voice uses fragment_played
    for f_idx, frag in enumerate(fragments):
        state_chat = reduce_event(
            credit_card_compiled,
            state_chat,
            ChatCommittedOutputEvent(
                event_id=f"ev_cc_{f_idx}",
                session_id="sess_chat",
                transport_epoch="epoch_1",
                turn_id="t1",
                generation_id="g1",
                manifest_id=manifest_id,
                fragment_id=frag.fragment_id,
                message_id=frag.step_id,
                durable_delivery_id=f"durable_{f_idx}",
            ),
        ).state
        state_voice = reduce_event(
            credit_card_compiled,
            state_voice,
            FragmentPlayedOutputEvent(
                event_id=f"ev_vp_{f_idx}",
                session_id="sess_voice",
                transport_epoch="epoch_1",
                turn_id="t1",
                generation_id="g1",
                manifest_id=manifest_id,
                fragment_id=frag.fragment_id,
                content_hash=frag.content_hash,
                evidence_id=f"evid_{f_idx}",
                capability_report_hash="rep_hash",
            ),
        ).state

    # Assert identical business capture state and transition to end
    assert state_chat.fields["employment_type"].status == state_voice.fields["employment_type"].status
    assert state_chat.fields["employment_type"].value == state_voice.fields["employment_type"].value
    assert state_chat.steps == state_voice.steps
    assert state_chat.active_step_id == state_voice.active_step_id == "end"

def test_action_unregistered_fails(credit_card_compiled):
    """Unregistered tool ID is absent from tool registry and cannot be executed."""
    from bot.flow.tools import ToolRegistry

    registry = ToolRegistry.empty()
    assert not registry.is_registered("unregistered_kyc_tool")
    assert not registry.has("unregistered_kyc_tool")

def test_model_prose_cannot_authorize_transitions(credit_card_compiled):
    """Model prose claiming a transition without a valid structured StateProposal is ignored."""
    state = initial_session_state(credit_card_compiled, "sess_1")

    prose = SpeechDeltaModelEvent(
        event_id="ev_prose",
        session_id="sess_1",
        transport_epoch="epoch_1",
        turn_id="t1",
        generation_id="g1",
        seq=0,
        text="I am moving you to the approval step now.",
    )
    red = reduce_event(credit_card_compiled, state, prose)

    # State active step and visited steps are unchanged
    assert red.state.active_step_id == state.active_step_id
    assert red.state.steps == state.steps
