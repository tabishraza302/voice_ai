"""Runtime tests for conversation flow controller, state reducer, and edge cases.

Per docs/first-release/flow-contract.md and flow-fixtures/controller-and-adapter-cases.json:
- early_multi_capture
- correction_invalidation
- candidate_not_confirmed
- refusal
- faq_detour
- hindi_same_logic
- else_unknown
- ordered_unknown_before_true
- selected_target_blocked
- cancel_before_commit
- cancel_after_commit
- tts_text_is_not_played
- partial_summary_cancel
- stale_and_forged_playback
- chat_voice_parity
- action_unregistered
- stop_preempts_disclosure
- unsupported_playback
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from bot.flow.compiler import compile_flow
from bot.flow.tools import ToolContract, ToolRegistry
from bot.flow.controller import (
    build_manifest_for_step,
    evaluate_predicate,
    evaluate_transitions,
    initial_session_state,
    reduce_event,
    resolve_target_step,
)
from bot.flow.schemas import (
    AdapterCapabilities,
    CancelGenerationCommand,
    CandidateCaptureUpdate,
    CandidateFieldState,
    ChatCommittedOutputEvent,
    ConfirmCaptureUpdate,
    ConfirmedFieldState,
    EmitSpeechManifestCommand,
    Evidence,
    FlowDiagnostic,
    FlowLimits,
    FragmentPlayedOutputEvent,
    LocaleChangedEvent,
    Origin,
    ProposeCaptureUpdate,
    RefuseCaptureUpdate,
    RequestClarificationCommand,
    SessionState,
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
        flow = json.load(f)["flow"]
    return compile_flow(flow)


QUALIFIED_REPORT_HASH = hashlib.sha256(b"voice-ai/qualification/report/1").hexdigest()


@pytest.fixture
def credit_card_qualified_compiled():
    with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
        flow = json.load(f)["flow"]
    caps = AdapterCapabilities(
        adapter_id="webrtc_browser_transport",
        adapter_version="1.0.0",
        model_revision="pinned/1",
        metadata="separate_stream",
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        report_hash=QUALIFIED_REPORT_HASH,
    )
    return compile_flow(flow, capabilities=caps)

class TestControllerRuntimeCases:
    """Implement the permanent behavioral cases from controller-and-adapter-cases.json."""

    def test_early_multi_capture(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:test")

        # Turn 1: user provides employment, salary, and card preference together
        t1_state = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        proposal = StateProposal(
            proposal_id="prop:1",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1_state.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="turn:1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="turn:1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="turn:1", quote="travel")),
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1_state,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal,
            ),
        )
        final_state = red.state

        # Assert: all three fields confirmed
        assert final_state.fields["employment_type"].status == "confirmed"
        assert final_state.fields["monthly_income_inr"].status == "confirmed"
        assert final_state.fields["card_preference"].status == "confirmed"

        # Assert: employment, salary, preferences steps completed
        assert final_state.steps["employment"] == "complete"
        assert final_state.steps["salary"] == "complete"
        assert final_state.steps["preferences"] == "complete"

        # Assert: advances directly to summary
        assert final_state.active_step_id == "summary"
        assert final_state.outcome == "in_progress"

    def test_correction_invalidation(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:test")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        p1 = StateProposal(
            proposal_id="p1",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="turn:1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="turn:1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="turn:1", quote="travel")),
            ],
        )
        s_after_early = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:test", transport_epoch="epoch:1", turn_id="turn:1", generation_id="gen:1", proposal=p1
            ),
        ).state

        # Correction turn: "Actually I am self-employed."
        t2 = reduce_event(
            credit_card_compiled,
            s_after_early,
            UserTurnAcceptedEvent(
                event_id="e3",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:2",
                generation_id="gen:2",
                text="Actually I am self-employed.",
            ),
        ).state

        p2 = StateProposal(
            proposal_id="p2",
            session_id="session:test",
            turn_id="turn:2",
            generation_id="gen:2",
            base_state_version=t2.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="self_employed", evidence=Evidence(source_turn_id="turn:2", quote="self-employed")),
            ],
        )
        s_after_corr = reduce_event(
            credit_card_compiled,
            t2,
            StateProposalModelEvent(
                event_id="e4", session_id="session:test", transport_epoch="epoch:1", turn_id="turn:2", generation_id="gen:2", proposal=p2
            ),
        ).state

        # monthly_income_inr becomes unknown and inapplicable
        assert s_after_corr.fields["monthly_income_inr"].status == "unknown"
        assert s_after_corr.fields["monthly_income_inr"].applicability == "inapplicable"

        # annual_income_inr becomes applicable
        assert s_after_corr.fields["annual_income_inr"].status == "unknown"
        assert s_after_corr.fields["annual_income_inr"].applicability == "applicable"

        # card_preference was unaffected and remains confirmed
        assert s_after_corr.fields["card_preference"].status == "confirmed"
        assert s_after_corr.fields["card_preference"].value == "travel"

        # salary and summary steps are invalidated back to pending
        assert s_after_corr.steps["salary"] == "pending"
        assert s_after_corr.steps["summary"] == "pending"

        # next step is business_income
        assert s_after_corr.active_step_id == "business_income"

    def test_candidate_not_confirmed(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:test")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="Maybe eighty, I am unsure about the period.",
            ),
        ).state

        proposal = StateProposal(
            proposal_id="p_cand",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                CandidateCaptureUpdate(
                    field="monthly_income_inr",
                    value=80,
                    reason="ambiguous",
                    evidence=Evidence(source_turn_id="turn:1", quote="eighty"),
                )
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal,
            ),
        )

        assert red.state.fields["monthly_income_inr"].status == "candidate"
        # No confirmed capture
        assert all(v.status != "confirmed" for v in red.state.fields.values())
        # Summary step is not complete
        assert red.state.steps.get("summary") != "complete"
        # Requests clarification
        assert any(isinstance(c, RequestClarificationCommand) for c in red.commands)

    def test_refusal(self, credit_card_compiled) -> None:
        state = initial_session_state(credit_card_compiled, "session:test")
        state.fields["employment_type"] = ConfirmedFieldState(
            status="confirmed",
            applicability="applicable",
            value="salaried",
            origin=Origin(source_turn_id="t0", generation_id="g0", committed_state_version=1, source="user_capture"),
        )
        state.steps["employment"] = "complete"
        state.steps["salary"] = "active"
        state.active_step_id = "salary"

        t1 = reduce_event(
            credit_card_compiled,
            state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I do not want to share my income.",
            ),
        ).state

        proposal = StateProposal(
            proposal_id="p_ref",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                RefuseCaptureUpdate(
                    field="monthly_income_inr",
                    evidence=Evidence(source_turn_id="turn:1", quote="do not want to share"),
                )
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal,
            ),
        )

        assert red.state.fields["monthly_income_inr"].status == "refused"
        assert red.state.active_step_id == "end"
        assert red.state.outcome == "partial"

    def test_faq_detour(self, credit_card_compiled) -> None:
        state = initial_session_state(credit_card_compiled, "session:test")
        state.fields["employment_type"] = ConfirmedFieldState(
            status="confirmed",
            applicability="applicable",
            value="salaried",
            origin=Origin(source_turn_id="t0", generation_id="g0", committed_state_version=1, source="user_capture"),
        )
        state.steps["employment"] = "complete"
        state.steps["salary"] = "active"
        state.active_step_id = "salary"

        # FAQ turn: no capture proposals
        t1 = reduce_event(
            credit_card_compiled,
            state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="What is the annual fee?",
            ),
        ).state

        proposal = StateProposal(
            proposal_id="p_faq",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal,
            ),
        )

        # Captures unchanged, step remains salary, clarification attempts not incremented
        assert red.state.active_step_id == "salary"
        assert red.state.resume_step_id == "salary"
        assert red.state.clarification_attempts.get("salary", 0) == 0
        assert red.state.detour_counts.get("salary", 0) == 1

    def test_hindi_same_logic(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:test", locale="hi")

        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="मैं नौकरी करता हूँ, मेरी मासिक आय 80000 रुपये है और मुझे ट्रैवल रिवॉर्ड चाहिए।",
                locale="hi",
            ),
        ).state

        proposal = StateProposal(
            proposal_id="prop:hi",
            session_id="session:test",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="turn:1", quote="नौकरी")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="turn:1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="turn:1", quote="ट्रैवल")),
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:test",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal,
            ),
        )

        assert red.state.active_step_id == "summary"
        assert red.state.fields["employment_type"].status == "confirmed"
        assert red.state.fields["monthly_income_inr"].status == "confirmed"
        assert red.state.fields["card_preference"].status == "confirmed"
        assert red.state.steps["employment"] == "complete"
        assert red.state.steps["salary"] == "complete"
        assert red.state.steps["preferences"] == "complete"

    def test_else_unknown_blocks_else(self, credit_card_compiled) -> None:
        step = {
            "id": "s1",
            "type": "collect",
            "transitions": [
                {"when": {"operator": "eq", "field": "employment_type", "value": "salaried"}, "to": "salary"},
                {"when": {"operator": "else"}, "to": "other"},
            ],
        }
        fields = {"employment_type": UnknownFieldState(status="unknown", applicability="applicable")}
        edge_idx, target, action = evaluate_transitions(step, fields, {}, credit_card_compiled)
        assert edge_idx is None
        assert action == "clarify"

    def test_ordered_unknown_before_true(self, credit_card_compiled) -> None:
        step = {
            "id": "s1",
            "type": "collect",
            "transitions": [
                {"when": {"operator": "eq", "field": "employment_type", "value": "salaried"}, "to": "salary"},
                {"when": {"operator": "is_present", "field": "card_preference"}, "to": "summary"},
                {"when": {"operator": "else"}, "to": "other"},
            ],
        }
        fields = {
            "employment_type": UnknownFieldState(status="unknown", applicability="applicable"),
            "card_preference": ConfirmedFieldState(
                status="confirmed",
                applicability="applicable",
                value="travel",
                origin=Origin(source_turn_id="t", generation_id="g", committed_state_version=1, source="user_capture"),
            ),
        }
        edge_idx, target, action = evaluate_transitions(step, fields, {}, credit_card_compiled)
        assert edge_idx == 1
        assert target == "summary"
        assert action == "route"

    def test_selected_target_blocked(self, credit_card_compiled) -> None:
        fields = {"employment_type": UnknownFieldState(status="unknown", applicability="applicable")}
        target_res, status = resolve_target_step("salary", fields, {"employment": "pending"}, credit_card_compiled)
        assert target_res == "employment"
        assert status == "ok"

    def test_cancel_before_commit(self, credit_card_compiled) -> None:
        s0 = initial_session_state(credit_card_compiled, "session:1")
        s1 = reduce_event(
            credit_card_compiled,
            s0,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried."
            ),
        ).state

        # Interrupted by turn 2
        s2 = reduce_event(
            credit_card_compiled,
            s1,
            UserTurnAcceptedEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Wait, second turn."
            ),
        ).state

        # Proposal from cancelled generation g1
        p_late = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=s1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))
            ],
        )
        s3 = reduce_event(
            credit_card_compiled,
            s2,
            StateProposalModelEvent(
                event_id="e3", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p_late
            ),
        ).state

        assert s3.fields["employment_type"].status == "unknown"
        assert any(t["turn_id"] == "t1" for t in s3.turn_history)

    def test_cancel_after_commit(self, credit_card_qualified_compiled) -> None:
        s0 = initial_session_state(credit_card_qualified_compiled, "session:1")
        s1 = reduce_event(
            credit_card_qualified_compiled,
            s0,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        p_commit = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=s1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red2 = reduce_event(
            credit_card_qualified_compiled,
            s1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p_commit
            ),
        )
        s2 = red2.state
        manifest_cmd = next(c for c in red2.commands if isinstance(c, EmitSpeechManifestCommand))
        manifest_id = manifest_cmd.manifest_id
        f1 = manifest_cmd.fragments[0]
        f2 = manifest_cmd.fragments[1]

        # f1 played with legitimate report hash and content hash
        s3 = reduce_event(
            credit_card_qualified_compiled,
            s2,
            FragmentPlayedOutputEvent(
                event_id="e3", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_id, fragment_id=f1.fragment_id, content_hash=f1.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            ),
        ).state

        # Turn 2 interrupts (switches active generation to g2)
        s4 = reduce_event(
            credit_card_qualified_compiled,
            s3,
            UserTurnAcceptedEvent(
                event_id="e4", session_id="session:1", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Hold on."
            ),
        ).state

        # Late f2 for g1
        s5 = reduce_event(
            credit_card_qualified_compiled,
            s4,
            FragmentPlayedOutputEvent(
                event_id="e5", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_id, fragment_id=f2.fragment_id, content_hash=f2.content_hash,
                evidence_id="ev2", capability_report_hash=QUALIFIED_REPORT_HASH,
            ),
        ).state

        assert s5.fields["employment_type"].status == "confirmed"
        assert s5.history_spoken_fragments == [f1.fragment_id]
        assert f2.fragment_id not in s5.history_spoken_fragments
        assert s5.steps["summary"] != "complete"
    def test_stale_and_forged_playback(self, credit_card_compiled) -> None:
        state = initial_session_state(credit_card_compiled, "session:1")
        # Attempt playback with wrong epoch and old generation
        red = reduce_event(
            credit_card_compiled,
            state,
            FragmentPlayedOutputEvent(
                event_id="eforge",
                session_id="session:1",
                transport_epoch="epoch:wrong",
                turn_id="t0",
                generation_id="old_gen",
                manifest_id="m1",
                fragment_id="f_forged",
                content_hash="h",
                evidence_id="e",
                capability_report_hash="r",
            ),
        )
        assert red.state.version == state.version
        assert red.state.history_spoken_fragments == []

    def test_chat_voice_parity(self, credit_card_qualified_compiled) -> None:
        init_state = initial_session_state(credit_card_qualified_compiled, "session:1")
        t1 = reduce_event(
            credit_card_qualified_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards."
            ),
        ).state
        prop = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red_summary = reduce_event(
            credit_card_qualified_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=prop
            ),
        )
        s_summary = red_summary.state
        manifest_cmd = next(c for c in red_summary.commands if isinstance(c, EmitSpeechManifestCommand))
        manifest_id = manifest_cmd.manifest_id
        fragments = manifest_cmd.fragments
        assert len(fragments) >= 2

        # Chat delivery path: durably commit each fragment of the emitted manifest
        s_chat = s_summary
        for f_idx, frag in enumerate(fragments):
            s_chat = reduce_event(
                credit_card_qualified_compiled,
                s_chat,
                ChatCommittedOutputEvent(
                    event_id=f"ec_{f_idx}",
                    session_id="session:1",
                    transport_epoch="epoch:1",
                    turn_id="t1",
                    generation_id="g1",
                    manifest_id=manifest_id,
                    fragment_id=frag.fragment_id,
                    message_id=frag.step_id,
                    durable_delivery_id=f"deliv_{f_idx}",
                ),
            ).state

        # Voice delivery path: play each fragment with genuine content hash and report hash
        s_voice = s_summary
        for f_idx, frag in enumerate(fragments):
            s_voice = reduce_event(
                credit_card_qualified_compiled,
                s_voice,
                FragmentPlayedOutputEvent(
                    event_id=f"ev_{f_idx}",
                    session_id="session:1",
                    transport_epoch="epoch:1",
                    turn_id="t1",
                    generation_id="g1",
                    manifest_id=manifest_id,
                    fragment_id=frag.fragment_id,
                    content_hash=frag.content_hash,
                    evidence_id=f"evid_{f_idx}",
                    capability_report_hash=QUALIFIED_REPORT_HASH,
                ),
            ).state

        assert s_chat.active_step_id == s_voice.active_step_id == "end"
        assert s_chat.fields == s_voice.fields
        assert s_chat.steps == s_voice.steps

    def test_stop_preempts_disclosure(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        red = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e_stop",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="t_stop",
                generation_id="g_stop",
                text="Stop now",
            ),
        )
        assert red.state.outcome == "aborted"
        assert any(isinstance(c, TeardownCommand) and c.outcome == "aborted" for c in red.commands)
        assert any(isinstance(c, CancelGenerationCommand) for c in red.commands)

    def test_unsupported_playback_gate(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]

        unqualified_caps = AdapterCapabilities(
            adapter_id="unqualified_voice",
            adapter_version="1.0.0",
            model_revision="pinned/1",
            metadata="none",
            speech_gating=False,
            cancel_generation=False,
            playback="unqualified",
            locales=["en"],
            report_hash="0" * 64,
        )

        diags = validate_executable(flow, capabilities=unqualified_caps)
        codes = [d.code for d in diags]
        assert "PLAYBACK_UNQUALIFIED" in codes


class TestContractReviewerInvariants:
    """Verify the exact validation, atomic rejection, evidence, duplicate, and teardown invariants."""

    def test_forged_or_mismatched_quote_rejected_atomically(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I work as a software engineer at an IT firm.",
            ),
        ).state

        # Model hallucinates quote "I am salaried" which does NOT appear in user text
        forged_proposal = StateProposal(
            proposal_id="p_forged",
            session_id="session:1",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(
                    field="employment_type",
                    value="salaried",
                    evidence=Evidence(source_turn_id="turn:1", quote="I am salaried"),
                )
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=forged_proposal,
            ),
        )

        # Must be rejected with INVALID_EVIDENCE diagnostic
        assert any(d.code == "INVALID_EVIDENCE" for d in red.diagnostics)
        # Field remains uncommitted
        assert red.state.fields["employment_type"].status == "unknown"
        # Clarification command emitted
        assert any(isinstance(c, RequestClarificationCommand) for c in red.commands)

    def test_wrong_session_rejected_atomically(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I am salaried.",
            ),
        ).state

        proposal_wrong_session = StateProposal(
            proposal_id="p_wrong_session",
            session_id="session:DIFFERENT",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(
                    field="employment_type",
                    value="salaried",
                    evidence=Evidence(source_turn_id="turn:1", quote="salaried"),
                )
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=proposal_wrong_session,
            ),
        )

        assert any(d.code == "SESSION_MISMATCH" for d in red.diagnostics)
        assert red.state.fields["employment_type"].status == "unknown"

    def test_cross_session_forged_matching_ids_rejected(self, credit_card_compiled) -> None:
        # State is bound to legitimate session
        init_state = initial_session_state(credit_card_compiled, "session:legitimate")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:legitimate",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I am salaried.",
            ),
        ).state

        # Attacker crafts BOTH event and proposal with matching forged session_id "session:ATTACKER"
        forged_proposal = StateProposal(
            proposal_id="p_forged_session",
            session_id="session:ATTACKER",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(
                    field="employment_type",
                    value="salaried",
                    evidence=Evidence(source_turn_id="turn:1", quote="salaried"),
                )
            ],
        )

        forged_event = StateProposalModelEvent(
            event_id="e2",
            session_id="session:ATTACKER",
            transport_epoch="epoch:1",
            turn_id="turn:1",
            generation_id="gen:1",
            proposal=forged_proposal,
        )

        red = reduce_event(credit_card_compiled, t1, forged_event)

        # Event and proposal match each other, but must fail against bound state.session_id
        assert any(d.code == "SESSION_MISMATCH" for d in red.diagnostics)
        assert red.state.fields["employment_type"].status == "unknown"
        assert red.state.version == t1.version

    def test_unbound_session_state_rejected(self, credit_card_compiled) -> None:
        # initial_session_state rejects empty or whitespace session_id
        with pytest.raises(ValueError, match="non-empty session_id"):
            initial_session_state(credit_card_compiled, "")
        with pytest.raises(ValueError, match="non-empty session_id"):
            initial_session_state(credit_card_compiled, "   ")

        # SessionState requires non-empty session_id
        with pytest.raises(ValidationError):
            SessionState(
                version=0,
                flow_hash="0" * 64,
                artifact_hash="0" * 64,
            )

        with pytest.raises(ValidationError):
            SessionState(
                session_id="",
                version=0,
                flow_hash="0" * 64,
                artifact_hash="0" * 64,
            )


    def test_mixed_valid_invalid_atomic_rejection_no_prefix_commit(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="I am salaried and my income is 80000.",
            ),
        ).state

        # Update 0 is completely valid; Update 1 targets an undeclared field "bitcoin_wallet"
        mixed_proposal = StateProposal(
            proposal_id="p_mixed",
            session_id="session:1",
            turn_id="turn:1",
            generation_id="gen:1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(
                    field="employment_type",
                    value="salaried",
                    evidence=Evidence(source_turn_id="turn:1", quote="salaried"),
                ),
                ProposeCaptureUpdate(
                    field="bitcoin_wallet",
                    value="0x123",
                    evidence=Evidence(source_turn_id="turn:1", quote="80000"),
                ),
            ],
        )

        red = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                proposal=mixed_proposal,
            ),
        )

        assert any(d.code == "UNKNOWN_FIELD" for d in red.diagnostics)
        # CRITICAL INVARIANT: Update 0 must NOT be prefix-committed!
        assert red.state.fields["employment_type"].status == "unknown"
        assert red.state.version == t1.version

    def test_replay_after_newer_turn(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="First turn text",
            ),
        ).state

        t2 = reduce_event(
            credit_card_compiled,
            t1,
            UserTurnAcceptedEvent(
                event_id="e2",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:2",
                generation_id="gen:2",
                text="Second turn text",
            ),
        ).state

        # 1. Replaying older turn:1 with identical text must be an idempotent no-op
        replay_same = reduce_event(
            credit_card_compiled,
            t2,
            UserTurnAcceptedEvent(
                event_id="e3",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="First turn text",
            ),
        )
        assert replay_same.commands == []
        assert replay_same.diagnostics == []
        assert replay_same.state.version == t2.version

        # 2. Replaying older turn:1 with conflicting text must fail with TURN_CONFLICT
        replay_conflict = reduce_event(
            credit_card_compiled,
            t2,
            UserTurnAcceptedEvent(
                event_id="e4",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:1",
                generation_id="gen:1",
                text="Conflicting altered text for turn 1",
            ),
        )
        assert any(d.code == "TURN_CONFLICT" for d in replay_conflict.diagnostics)
        assert replay_conflict.state.version == t2.version

    def test_repeated_stop_emits_single_idempotent_teardown(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")

        # First stop
        red1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e_stop1",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:stop1",
                generation_id="gen:stop1",
                text="Stop now",
            ),
        )
        assert red1.state.outcome == "aborted"
        teardown_cmds1 = [c for c in red1.commands if isinstance(c, TeardownCommand)]
        assert len(teardown_cmds1) == 1

        # Second stop (repeated user turn stop)
        red2 = reduce_event(
            credit_card_compiled,
            red1.state,
            UserTurnAcceptedEvent(
                event_id="e_stop2",
                session_id="session:1",
                transport_epoch="epoch:1",
                turn_id="turn:stop2",
                generation_id="gen:stop2",
                text="Stop now",
            ),
        )
        teardown_cmds2 = [c for c in red2.commands if isinstance(c, TeardownCommand)]
        # Must NOT emit duplicate teardown command
        assert len(teardown_cmds2) == 0
        assert red2.state.outcome == "aborted"

    def test_forged_manifest_and_hash_fail_closed(self, credit_card_qualified_compiled) -> None:
        init_state = initial_session_state(credit_card_qualified_compiled, "session:1")
        t1 = reduce_event(
            credit_card_qualified_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        prop = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red_summary = reduce_event(
            credit_card_qualified_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=prop
            ),
        )
        s_summary = red_summary.state
        manifest_cmd = next(c for c in red_summary.commands if isinstance(c, EmitSpeechManifestCommand))
        f1 = manifest_cmd.fragments[0]

        # 1. Forged manifest ID must be rejected
        red_forged_manifest = reduce_event(
            credit_card_qualified_compiled,
            s_summary,
            FragmentPlayedOutputEvent(
                event_id="ef1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id="forged_manifest", fragment_id=f1.fragment_id, content_hash=f1.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            ),
        )
        assert f1.fragment_id not in red_forged_manifest.state.history_spoken_fragments
        assert red_forged_manifest.state.steps["summary"] != "complete"

        # 2. Forged content hash must be rejected
        red_forged_hash = reduce_event(
            credit_card_qualified_compiled,
            s_summary,
            FragmentPlayedOutputEvent(
                event_id="ef2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_cmd.manifest_id, fragment_id=f1.fragment_id, content_hash="forged_content_hash",
                evidence_id="ev2", capability_report_hash=QUALIFIED_REPORT_HASH,
            ),
        )
        assert f1.fragment_id not in red_forged_hash.state.history_spoken_fragments
        assert red_forged_hash.state.steps["summary"] != "complete"

        # 3. Forged capability report hash must be rejected
        red_forged_rep = reduce_event(
            credit_card_qualified_compiled,
            s_summary,
            FragmentPlayedOutputEvent(
                event_id="ef3", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_cmd.manifest_id, fragment_id=f1.fragment_id, content_hash=f1.content_hash,
                evidence_id="ev3", capability_report_hash="forged_report_hash",
            ),
        )
        assert f1.fragment_id not in red_forged_rep.state.history_spoken_fragments
        assert red_forged_rep.state.steps["summary"] != "complete"

        # 4. Unknown fragment ID must be rejected
        red_unknown_frag = reduce_event(
            credit_card_qualified_compiled,
            s_summary,
            FragmentPlayedOutputEvent(
                event_id="ef4", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_cmd.manifest_id, fragment_id="unknown_fragment_id", content_hash=f1.content_hash,
                evidence_id="ev4", capability_report_hash=QUALIFIED_REPORT_HASH,
            ),
        )
        assert red_unknown_frag.state.steps["summary"] != "complete"

    def test_unqualified_voice_playback_fails_closed(self, credit_card_compiled) -> None:
        # credit_card_compiled is compiled without capabilities (unqualified voice)
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        prop = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red_summary = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=prop
            ),
        )
        s_summary = red_summary.state
        manifest_cmd = next(c for c in red_summary.commands if isinstance(c, EmitSpeechManifestCommand))

        # 1. Voice playback events arrive: MUST fail closed because flow lacks qualified playback capabilities
        s_voice = s_summary
        for f_idx, frag in enumerate(manifest_cmd.fragments):
            s_voice = reduce_event(
                credit_card_compiled,
                s_voice,
                FragmentPlayedOutputEvent(
                    event_id=f"ev_{f_idx}",
                    session_id="session:1",
                    transport_epoch="epoch:1",
                    turn_id="t1",
                    generation_id="g1",
                    manifest_id=manifest_cmd.manifest_id,
                    fragment_id=frag.fragment_id,
                    content_hash=frag.content_hash,
                    evidence_id=f"evid_{f_idx}",
                    capability_report_hash="rep:some_hash",
                ),
            ).state

        # Voice cannot advance unqualified flow
        assert s_voice.steps["summary"] != "complete"
        assert s_voice.active_step_id == "summary"

        # 2. Chat delivery on the same flow DOES advance
        s_chat = s_summary
        for f_idx, frag in enumerate(manifest_cmd.fragments):
            s_chat = reduce_event(
                credit_card_compiled,
                s_chat,
                ChatCommittedOutputEvent(
                    event_id=f"ec_{f_idx}",
                    session_id="session:1",
                    transport_epoch="epoch:1",
                    turn_id="t1",
                    generation_id="g1",
                    manifest_id=manifest_cmd.manifest_id,
                    fragment_id=frag.fragment_id,
                    message_id=frag.step_id,
                    durable_delivery_id=f"durable_{f_idx}",
                ),
            ).state

        assert s_chat.steps["summary"] == "complete"
        assert s_chat.active_step_id == "end"

    def test_chat_empty_durable_delivery_id_fails_closed(self, credit_card_compiled) -> None:
        init_state = initial_session_state(credit_card_compiled, "session:1")
        t1 = reduce_event(
            credit_card_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        prop = StateProposal(
            proposal_id="p1",
            session_id="session:1",
            turn_id="t1",
            generation_id="g1",
            base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red_summary = reduce_event(
            credit_card_compiled,
            t1,
            StateProposalModelEvent(
                event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=prop
            ),
        )
        s_summary = red_summary.state
        manifest_cmd = next(c for c in red_summary.commands if isinstance(c, EmitSpeechManifestCommand))

        # chat_committed with empty durable_delivery_id must be rejected
        s_chat = s_summary
        for f_idx, frag in enumerate(manifest_cmd.fragments):
            s_chat = reduce_event(
                credit_card_compiled,
                s_chat,
                ChatCommittedOutputEvent(
                    event_id=f"ec_{f_idx}",
                    session_id="session:1",
                    transport_epoch="epoch:1",
                    turn_id="t1",
                    generation_id="g1",
                    manifest_id=manifest_cmd.manifest_id,
                    fragment_id=frag.fragment_id,
                    message_id=frag.step_id,
                    durable_delivery_id="",
                ),
            ).state

        assert s_chat.steps["summary"] != "complete"
        assert s_chat.active_step_id == "summary"

    def test_forged_capability_hash_fails_closed(self, credit_card_qualified_compiled) -> None:
        init_state = initial_session_state(credit_card_qualified_compiled, "session:1")
        t1 = reduce_event(
            credit_card_qualified_compiled,
            init_state,
            UserTurnAcceptedEvent(
                event_id="e1", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                text="I am salaried, earn INR 80000 monthly and want travel rewards.",
            ),
        ).state

        p1 = StateProposal(
            proposal_id="p1", session_id="session:1", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ],
        )
        red_sum = reduce_event(credit_card_qualified_compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        m_cmd = next(c for c in red_sum.commands if isinstance(c, EmitSpeechManifestCommand))

        s_v = red_sum.state
        for f_idx, frag in enumerate(m_cmd.fragments):
            s_v = reduce_event(
                credit_card_qualified_compiled,
                s_v,
                FragmentPlayedOutputEvent(
                    event_id=f"ev_{f_idx}", session_id="session:1", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                    manifest_id=m_cmd.manifest_id, fragment_id=frag.fragment_id, content_hash=frag.content_hash,
                    evidence_id=f"evid_{f_idx}", capability_report_hash="forged_report_hash",
                ),
            ).state
        # Forged capability report hash must fail closed: summary does not complete
        assert s_v.steps["summary"] != "complete"
        assert s_v.active_step_id == "summary"

    def test_capability_report_validation_pure_rules(self) -> None:
        from bot.flow.qualification import (
            CapabilityReport,
            ScenarioResult,
            validate_capability_report,
            REQUIRED_SCENARIO_CLASSES,
        )

        all_7_cases = [
            ScenarioResult(
                scenario_id=f"sc_{sc_class}",
                scenario_class=sc_class,
                passed=True,
                measurements={"latency_ms": 12, "buffer_drain_ms": 4},
                raw_evidence={"trace_id": f"tr_{sc_class}", "audio_sha256": "abc123"},
            )
            for sc_class in REQUIRED_SCENARIO_CLASSES
        ]

        valid_report = CapabilityReport(
            protocol_version=1,
            adapter_id="webrtc_browser_transport",
            adapter_version="1.0.0",
            model_revision="pinned/qwen-3.5",
            metadata="separate_stream",
            speech_gating=True,
            cancel_generation=True,
            playback="qualified_fragment_completion",
            locales=["en", "hi"],
            profile_hash="a" * 64,
            source_revision="git_rev_12345",
            fixture_hashes={"valid_card": "f" * 64},
            cases=all_7_cases,
            measurements={"p95_ttft_ms": 120, "p99_playback_ms": 300},
            budgets={"max_ttft_ms": 500, "max_playback_ms": 1000},
            raw_evidence={"run_id": "qual_run_01", "pcap_evidence": "packet_capture_ref"},
            playback_boundary_description="WebRTC data-channel correlated render ack",
            limitations=["Requires Chrome 120+"],
        )
        valid_dump = valid_report.model_dump()
        valid_dump["report_hash"] = valid_report.compute_hash()

        # 1. Fully compliant report with all 7 groups passes with 0 diagnostics
        diags_valid = validate_capability_report(valid_dump)
        assert len(diags_valid) == 0

        # 1b. Qualified playback without recorded configured budgets is incomplete
        no_budget_report = valid_report.model_copy(update={"budgets": {}})
        no_budget_dump = no_budget_report.model_dump()
        no_budget_dump["report_hash"] = no_budget_report.compute_hash()
        diags_no_budget = validate_capability_report(no_budget_dump)
        assert any(
            d.code == "QUALIFICATION_INCOMPLETE" and d.path == "/budgets"
            for d in diags_no_budget
        )

        # 1c. protocol_version is an integer discriminator, never a boolean
        bool_version_dump = dict(valid_dump)
        bool_version_dump["protocol_version"] = True
        diags_bool_version = validate_capability_report(bool_version_dump)
        assert any(
            d.code == "QUALIFICATION_INVALID" and d.path == "/protocol_version"
            for d in diags_bool_version
        )

        # 1d. report_hash must be lowercase 64-hex
        upper_hash_dump = dict(valid_dump)
        upper_hash_dump["report_hash"] = valid_dump["report_hash"].upper()
        diags_upper_hash = validate_capability_report(upper_hash_dump)
        assert any(
            d.code == "QUALIFICATION_INVALID" and d.path == "/report_hash"
            for d in diags_upper_hash
        )

        # 2. Tampered report produces QUALIFICATION_HASH_MISMATCH
        tampered_dump = dict(valid_dump)
        tampered_dump["model_revision"] = "forged/tampered_revision"
        diags_tampered = validate_capability_report(tampered_dump)
        assert any(d.code == "QUALIFICATION_HASH_MISMATCH" for d in diags_tampered)

        # 3. Missing or empty report_hash produces QUALIFICATION_INVALID
        no_hash_dump = dict(valid_dump)
        no_hash_dump.pop("report_hash", None)
        diags_no_hash = validate_capability_report(no_hash_dump)
        assert any(d.code == "QUALIFICATION_INVALID" and d.path == "/report_hash" for d in diags_no_hash)

        # 4. Missing required measurements or raw evidence produces QUALIFICATION_INVALID
        no_evidence_dump = dict(valid_dump)
        no_evidence_dump["raw_evidence"] = {}
        no_evidence_dump["report_hash"] = CapabilityReport(**no_evidence_dump).compute_hash()
        diags_no_evidence = validate_capability_report(no_evidence_dump)
        assert any(d.code == "QUALIFICATION_INVALID" and d.path == "/raw_evidence" for d in diags_no_evidence)

        # 5. Omitting any of the 7 mandatory scenario groups produces QUALIFICATION_INCOMPLETE
        for omitted_class in REQUIRED_SCENARIO_CLASSES:
            subset_cases = [c for c in all_7_cases if c.scenario_class != omitted_class]
            inc_report = valid_report.model_copy(update={"cases": subset_cases})
            inc_dump = inc_report.model_dump()
            inc_dump["report_hash"] = inc_report.compute_hash()
            diags_inc = validate_capability_report(inc_dump)
            assert any(d.code == "QUALIFICATION_INCOMPLETE" and omitted_class in d.message for d in diags_inc)

        # 6. Failing scenario produces QUALIFICATION_FAILED
        failing_cases = [
            ScenarioResult(
                scenario_id="sc_fail",
                scenario_class="normal_en_hi",
                passed=False,
                error_message="Transport audio stall detected",
                measurements={"packet_loss_permille": 500},
            ),
            *[c for c in all_7_cases if c.scenario_class != "normal_en_hi"],
        ]
        fail_report = valid_report.model_copy(update={"cases": failing_cases})
        fail_dump = fail_report.model_dump()
        fail_dump["report_hash"] = fail_report.compute_hash()
        diags_fail = validate_capability_report(fail_dump)
        assert any(d.code == "QUALIFICATION_FAILED" for d in diags_fail)

        # 7. Float measurements are strictly rejected before compute_hash (prevents float serialization)
        from bot.flow.qualification import canonicalize_measurement
        assert canonicalize_measurement(12.5) == "12.5"
        assert canonicalize_measurement(100) == 100
        assert canonicalize_measurement("45.125") == "45.125"
        # Exact expansion, not a fixed-precision round trip.
        assert canonicalize_measurement(1e-10) == "0.0000000001"
        assert canonicalize_measurement(1.2345678901234567) == "1.2345678901234567"
        assert canonicalize_measurement(-0.0) == "0"
        with pytest.raises(ValueError, match="Boolean"):
            canonicalize_measurement(True)
        with pytest.raises(ValueError, match="NaN"):
            canonicalize_measurement(float("nan"))

        with pytest.raises(ValidationError):
            CapabilityReport(**{**valid_dump, "measurements": {"latency": 12.5}})

        with pytest.raises(ValidationError):
            ScenarioResult(scenario_id="s1", scenario_class="normal_en_hi", passed=True, measurements={"latency": 12.5})

        # Raw dictionary with float measurement fails report validation
        float_dump = dict(valid_dump)
        float_dump["measurements"] = {"latency_ms": 12.5}
        diags_float = validate_capability_report(float_dump)
        assert any(d.code == "QUALIFICATION_INVALID" and d.path == "/measurements/latency_ms" for d in diags_float)

        # Decimal string measurements (e.g. from canonicalize_measurement) compute hash and validate cleanly
        dec_report = valid_report.model_copy(update={"measurements": {"p95_ttft_ms": canonicalize_measurement(120.5), "drain_ms": "4.25"}})
        dec_dump = dec_report.model_dump()
        dec_dump["report_hash"] = dec_report.compute_hash()
        assert len(validate_capability_report(dec_dump)) == 0

    def test_version_bound_candidate_confirmation_with_stale_yes(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        # Set card_preference capture to confirmation: explicit
        flow["steps"][3]["captures"][0]["confirmation"] = "explicit"
        compiled = compile_flow(flow)
        init_st = initial_session_state(compiled, "session:explicit")

        # Turn 1: user says "I am salaried."
        t1 = reduce_event(
            compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:explicit", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))]
        )
        s1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1)).state

        # Turn 2: user proposes card preference "I prefer travel rewards."
        t2 = reduce_event(
            compiled, s1,
            UserTurnAcceptedEvent(event_id="e3", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="I prefer travel rewards.")
        ).state
        # Because card_preference requires explicit confirmation, propose op leaves it as candidate
        p2 = StateProposal(
            proposal_id="p2", session_id="session:explicit", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t2", quote="travel"))]
        )
        s2 = reduce_event(compiled, t2, StateProposalModelEvent(event_id="e4", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2)).state

        assert s2.fields["card_preference"].status == "candidate"
        recorded_version = s2.fields["card_preference"].candidate_state_version
        assert recorded_version == t2.version

        # Turn 3: User turn occurs (e.g. asking clarifying question) advancing state version
        t3 = reduce_event(
            compiled, s2,
            UserTurnAcceptedEvent(event_id="e5", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="Yes, travel.")
        ).state

        # 1. Stale confirmation with old version fails
        stale_confirm = StateProposal(
            proposal_id="p_stale", session_id="session:explicit", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[
                ConfirmCaptureUpdate(
                    field="card_preference",
                    candidate_state_version=recorded_version - 1, # stale version!
                    evidence=Evidence(source_turn_id="t3", quote="Yes"),
                )
            ]
        )
        red_stale = reduce_event(compiled, t3, StateProposalModelEvent(event_id="e6", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=stale_confirm))
        assert any(d.code == "STALE_CONFIRMATION" for d in red_stale.diagnostics)
        assert red_stale.state.fields["card_preference"].status == "candidate"

        # 2. Fresh confirmation with matching candidate_state_version succeeds
        fresh_confirm = StateProposal(
            proposal_id="p_fresh", session_id="session:explicit", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[
                ConfirmCaptureUpdate(
                    field="card_preference",
                    candidate_state_version=recorded_version, # exact matching version!
                    evidence=Evidence(source_turn_id="t3", quote="Yes"),
                )
            ]
        )
        red_fresh = reduce_event(compiled, t3, StateProposalModelEvent(event_id="e7", session_id="session:explicit", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=fresh_confirm))
        assert red_fresh.diagnostics == []
        assert red_fresh.state.fields["card_preference"].status == "confirmed"
        assert red_fresh.state.fields["card_preference"].origin.source == "user_confirmation"

    def test_allow_corrections_false_preserves_confirmed_facts(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        flow["behavior"]["allow_corrections"] = False
        compiled = compile_flow(flow)
        init_st = initial_session_state(compiled, "session:no_corr")

        # Turn 1: confirm employment_type as salaried
        t1 = reduce_event(
            compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:no_corr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:no_corr", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))]
        )
        s1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:no_corr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1)).state
        assert s1.fields["employment_type"].status == "confirmed"
        assert s1.fields["employment_type"].value == "salaried"

        # Turn 2: user attempts to correct to self-employed
        t2 = reduce_event(
            compiled, s1,
            UserTurnAcceptedEvent(event_id="e3", session_id="session:no_corr", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Actually I am self-employed.")
        ).state
        p2 = StateProposal(
            proposal_id="p2", session_id="session:no_corr", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ProposeCaptureUpdate(field="employment_type", value="self_employed", evidence=Evidence(source_turn_id="t2", quote="self-employed"))]
        )
        red2 = reduce_event(compiled, t2, StateProposalModelEvent(event_id="e4", session_id="session:no_corr", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2))

        # Disallowed: diagnostic emitted, existing confirmed value preserved
        assert any(d.code == "CORRECTION_DISALLOWED" for d in red2.diagnostics)
        assert red2.state.fields["employment_type"].status == "confirmed"
        assert red2.state.fields["employment_type"].value == "salaried"

    def test_bounded_max_attempts_and_offer_exit(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        flow["steps"][0]["fallback"] = {"action": "clarify", "max_attempts": 2, "on_exhausted": "offer_exit"}
        compiled = compile_flow(flow)
        init_st = initial_session_state(compiled, "session:budget")

        # Turn 1: ambiguous answer 1
        t1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:budget", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Unclear 1")).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:budget", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[CandidateCaptureUpdate(field="employment_type", value=None, reason="ambiguous", evidence=Evidence(source_turn_id="t1", quote="Unclear"))]
        )
        red1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:budget", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        assert red1.state.clarification_attempts["employment"] == 1
        assert not red1.state.offer_exit_pending.get("employment", False)

        # Turn 2: ambiguous answer 2 (hits max_attempts 2 -> emits offer_exit)
        t2 = reduce_event(compiled, red1.state, UserTurnAcceptedEvent(event_id="e3", session_id="session:budget", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Unclear 2")).state
        p2 = StateProposal(
            proposal_id="p2", session_id="session:budget", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[CandidateCaptureUpdate(field="employment_type", value=None, reason="ambiguous", evidence=Evidence(source_turn_id="t2", quote="Unclear"))]
        )
        red2 = reduce_event(compiled, t2, StateProposalModelEvent(event_id="e4", session_id="session:budget", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2))
        assert red2.state.clarification_attempts["employment"] == 2
        assert red2.state.offer_exit_pending.get("employment", False) is True
        assert any(isinstance(c, RequestClarificationCommand) and c.reason == "offer_exit" for c in red2.commands)

        # Turn 3: third unsuccessful attempt without reset -> finite partial exit
        t3 = reduce_event(compiled, red2.state, UserTurnAcceptedEvent(event_id="e5", session_id="session:budget", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="Unclear 3")).state
        p3 = StateProposal(
            proposal_id="p3", session_id="session:budget", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[CandidateCaptureUpdate(field="employment_type", value=None, reason="ambiguous", evidence=Evidence(source_turn_id="t3", quote="Unclear"))]
        )
        red3 = reduce_event(compiled, t3, StateProposalModelEvent(event_id="e6", session_id="session:budget", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p3))
        assert red3.state.outcome == "partial"
        assert red3.state.active_step_id == "end"

    def test_action_unregistered_validation_and_no_invocation(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        action_step = {
            "id": "action_step",
            "title": "Execute transfer",
            "type": "action",
            "instruction": "Transfer money via tool",
            "required": True,
            "priority": 95,
            "prerequisites": [],
            "captures": [],
            "transitions": [{"when": {"operator": "completed"}, "to": "end"}],
            "action": {
                "tool_id": "transfer_money",
                "input_fields": {},
                "result_fields": {},
            },
        }
        flow["steps"].append(action_step)
        # Publish/executable validation must fail with TOOL_UNQUALIFIED
        diags = validate_executable(flow)
        codes = [d.code for d in diags]
        assert "TOOL_UNQUALIFIED" in codes

    def test_partial_summary_cancel_resumes_only_unplayed_fragments(self, credit_card_qualified_compiled) -> None:
        init_st = initial_session_state(credit_card_qualified_compiled, "session:cancel_part")
        t1 = reduce_event(
            credit_card_qualified_compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:cancel_part", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:cancel_part", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ]
        )
        red1 = reduce_event(credit_card_qualified_compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:cancel_part", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        s_summary = red1.state
        manifest_cmd1 = next(c for c in red1.commands if isinstance(c, EmitSpeechManifestCommand))
        f_facts = next(f for f in manifest_cmd1.fragments if ":facts:" in f.fragment_id)
        f_no_guarantee = next(f for f in manifest_cmd1.fragments if ":no_guarantee:" in f.fragment_id)

        # 1. Play facts fragment
        s_played = reduce_event(
            credit_card_qualified_compiled, s_summary,
            FragmentPlayedOutputEvent(
                event_id="e3", session_id="session:cancel_part", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_cmd1.manifest_id, fragment_id=f_facts.fragment_id, content_hash=f_facts.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            )
        ).state
        assert s_played.steps["summary"] != "complete"

        # 2. Barge-in / cancel generation g1
        s_cancelled = reduce_event(
            credit_card_qualified_compiled, s_played,
            UserTurnAcceptedEvent(event_id="e4", session_id="session:cancel_part", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="What?")
        ).state

        # 3. Resume summary: build manifest command for g2
        cmd_resume = build_manifest_for_step("summary", credit_card_qualified_compiled, s_cancelled)
        assert cmd_resume is not None
        resume_frag_ids = [f.fragment_id for f in cmd_resume.fragments]
        # Contract assertion: replay_facts: false, resume_fragments: ["no_guarantee"]
        assert any(":no_guarantee:" in fid for fid in resume_frag_ids)
        assert not any(":facts:" in fid for fid in resume_frag_ids)

    def test_correction_changes_rendered_facts_even_when_same_fragment_name(self, credit_card_qualified_compiled) -> None:
        # 1. Reach summary with salaried facts and play facts fragment
        init_st = initial_session_state(credit_card_qualified_compiled, "session:corr_facts")
        t1 = reduce_event(
            credit_card_qualified_compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:corr_facts", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ]
        )
        red1 = reduce_event(credit_card_qualified_compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        s_summary1 = red1.state
        cmd1 = next(c for c in red1.commands if isinstance(c, EmitSpeechManifestCommand))
        f_facts1 = next(f for f in cmd1.fragments if ":facts:" in f.fragment_id)
        assert "Employment: salaried." in f_facts1.text

        # Play facts fragment 1
        s_played1 = reduce_event(
            credit_card_qualified_compiled, s_summary1,
            FragmentPlayedOutputEvent(
                event_id="e3", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=cmd1.manifest_id, fragment_id=f_facts1.fragment_id, content_hash=f_facts1.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            )
        ).state

        # 2. User corrects employment_type to self_employed
        t2 = reduce_event(
            credit_card_qualified_compiled, s_played1,
            UserTurnAcceptedEvent(event_id="e4", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Actually I am self-employed.")
        ).state
        p2 = StateProposal(
            proposal_id="p2", session_id="session:corr_facts", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ProposeCaptureUpdate(field="employment_type", value="self_employed", evidence=Evidence(source_turn_id="t2", quote="self-employed"))]
        )
        s_business = reduce_event(credit_card_qualified_compiled, t2, StateProposalModelEvent(event_id="e5", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2)).state

        # Complete annual_income_inr for self_employed
        t3 = reduce_event(
            credit_card_qualified_compiled, s_business,
            UserTurnAcceptedEvent(event_id="e6", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="My annual business income is 500000.")
        ).state
        p3 = StateProposal(
            proposal_id="p3", session_id="session:corr_facts", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[ProposeCaptureUpdate(field="annual_income_inr", value=500000, evidence=Evidence(source_turn_id="t3", quote="500000"))]
        )
        red3 = reduce_event(credit_card_qualified_compiled, t3, StateProposalModelEvent(event_id="e7", session_id="session:corr_facts", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p3))
        s_summary2 = red3.state
        assert s_summary2.active_step_id == "summary"
        cmd2 = next(c for c in red3.commands if isinstance(c, EmitSpeechManifestCommand))
        f_facts2 = next((f for f in cmd2.fragments if ":facts:" in f.fragment_id), None)
        assert f_facts2 is not None, "Corrected facts fragment must NOT be treated as already played"
        assert "Employment: self_employed." in f_facts2.text
        assert "Annual INR: 500000." in f_facts2.text
        assert f_facts2.content_hash != f_facts1.content_hash

    def test_locale_transition_retains_played_and_localizes_remaining(self, credit_card_qualified_compiled) -> None:
        init_st = initial_session_state(credit_card_qualified_compiled, "session:locale_tr", locale="en")
        t1 = reduce_event(
            credit_card_qualified_compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:locale_tr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:locale_tr", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ]
        )
        red_loc = reduce_event(credit_card_qualified_compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:locale_tr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        s_summary = red_loc.state
        cmd_en = next(c for c in red_loc.commands if isinstance(c, EmitSpeechManifestCommand))
        f_facts_en = next(f for f in cmd_en.fragments if ":facts:" in f.fragment_id)

        # Play English facts fragment
        s_played = reduce_event(
            credit_card_qualified_compiled, s_summary,
            FragmentPlayedOutputEvent(
                event_id="e3", session_id="session:locale_tr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=cmd_en.manifest_id, fragment_id=f_facts_en.fragment_id, content_hash=f_facts_en.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            )
        ).state

        # User switches language to Hindi
        red_hi = reduce_event(
            credit_card_qualified_compiled, s_played,
            LocaleChangedEvent(event_id="e4", session_id="session:locale_tr", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", locale="hi")
        )
        s_hi = red_hi.state
        assert s_hi.locale == "hi"
        # Historical English speech remains in history
        assert f_facts_en.fragment_id in s_hi.history_spoken_fragments

        # Manifest for newly selected locale is built in Hindi and emitted on locale change
        cmd_hi = next(c for c in red_hi.commands if isinstance(c, EmitSpeechManifestCommand))
        assert cmd_hi is not None
        # Previously played facts must NOT be re-emitted in Hindi after language-only switch per contract line 153
        cmd_hi_frag_ids = [f.fragment_id for f in cmd_hi.fragments]
        assert not any(":facts:" in fid for fid in cmd_hi_frag_ids), "Already played facts must not be replayed on locale switch"
        # Remaining unplayed message parts in Hindi use the newly selected locale
        f_no_guar_hi = next(f for f in cmd_hi.fragments if ":no_guarantee:" in f.fragment_id)
        assert "गारंटी नहीं है" in f_no_guar_hi.text
        assert f_no_guar_hi.locale == "hi"

    def test_manifest_construction_fails_closed_without_accepted_transport_epoch(self, credit_card_qualified_compiled) -> None:
        state_no_epoch = initial_session_state(credit_card_qualified_compiled, "session:no_epoch")
        assert state_no_epoch.transport_epoch is None

        # Attempting manifest construction without an accepted transport epoch fails closed (returns None)
        cmd = build_manifest_for_step("summary", credit_card_qualified_compiled, state_no_epoch)
        assert cmd is None, "Manifest construction must fail closed when transport_epoch is missing"

    def test_multipart_interrupted_message_across_locale_switch_and_correction(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]

        # Configure summary step with a long message that splits into multiple transport fragments (>1024 chars)
        long_en_sentence_0 = "Employment confirmed as {{employment_type}} and monthly income is {{monthly_income_inr}}. " + ("detail zero " * 50) + ". "
        long_en_sentence_1 = "Detailed terms and fee structure for card preference {{card_preference}}. " + ("detail one " * 50) + ". "
        long_en_sentence_2 = "Regulatory disclosures and mandatory compliance notifications. " + ("detail two " * 50) + ". "
        full_en = long_en_sentence_0 + long_en_sentence_1 + long_en_sentence_2

        long_hi_sentence_0 = "रोज़गार {{employment_type}} और मासिक आय {{monthly_income_inr}} है। " + ("विवरण शून्य " * 40) + "। "
        long_hi_sentence_1 = "क्रेडिट कार्ड पसंद {{card_preference}} की शर्तें और शुल्क। " + ("विवरण एक " * 40) + "। "
        long_hi_sentence_2 = "नियामक प्रकटीकरण और अनुपालन सूचनाएं। " + ("विवरण दो " * 40) + "। "
        full_hi = long_hi_sentence_0 + long_hi_sentence_1 + long_hi_sentence_2

        flow["steps"][4]["messages"] = [
            {"id": "long_notice", "en": full_en, "hi": full_hi}
        ]

        caps = AdapterCapabilities(
            adapter_id="webrtc_browser_transport",
            adapter_version="1.0.0",
            model_revision="pinned/1",
            metadata="separate_stream",
            speech_gating=True,
            cancel_generation=True,
            playback="qualified_fragment_completion",
            locales=["en", "hi"],
            report_hash=QUALIFIED_REPORT_HASH,
        )
        compiled = compile_flow(flow, capabilities=caps)

        # 1. Reach summary with initial facts (salaried, 80000, travel)
        init_st = initial_session_state(compiled, "session:multi", locale="en")
        t1 = reduce_event(
            compiled, init_st,
            UserTurnAcceptedEvent(event_id="e1", session_id="session:multi", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards.")
        ).state
        p1 = StateProposal(
            proposal_id="p1", session_id="session:multi", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
                ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
            ]
        )
        red1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:multi", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        s_summary = red1.state
        cmd1 = next(c for c in red1.commands if isinstance(c, EmitSpeechManifestCommand))

        # Assert initial manifest split into multiple parts
        parts_en = cmd1.fragments
        assert len(parts_en) >= 2, f"Expected long message to split into multiple parts, got {len(parts_en)}"
        part_0_en = parts_en[0]
        part_1_en = parts_en[1]

        # 2. Only Part 0 is played/delivered (delivery pauses or user interrupts after part 0)
        s_part0_played = reduce_event(
            compiled, s_summary,
            FragmentPlayedOutputEvent(
                event_id="e3", session_id="session:multi", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
                manifest_id=cmd1.manifest_id, fragment_id=part_0_en.fragment_id, content_hash=part_0_en.content_hash,
                evidence_id="ev1", capability_report_hash=QUALIFIED_REPORT_HASH,
            )
        ).state
        # Summary is NOT complete because remaining parts are undelivered
        assert s_part0_played.steps["summary"] != "complete"
        assert part_0_en.fragment_id in s_part0_played.history_spoken_fragments
        assert part_1_en.fragment_id not in s_part0_played.history_spoken_fragments

        # 3. User switches language to Hindi while remaining parts are undelivered
        red_hi = reduce_event(
            compiled, s_part0_played,
            LocaleChangedEvent(event_id="e4", session_id="session:multi", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", locale="hi")
        )
        s_hi = red_hi.state
        cmd_hi = next(c for c in red_hi.commands if isinstance(c, EmitSpeechManifestCommand))

        # Part 0 was ALREADY played in English with identical fingerprint -> must be omitted from new Hindi manifest
        hi_frag_ids = [f.fragment_id for f in cmd_hi.fragments]
        assert not any(fid.endswith(":long_notice:0") for fid in hi_frag_ids), "Already delivered part 0 must not be repeated"
        # BUT Part 1 was NOT delivered -> MUST be emitted in the new Hindi manifest without being lost
        assert any(fid.endswith(":long_notice:1") for fid in hi_frag_ids), "Undelivered part 1 must be emitted in new locale"
        part_1_hi = next(f for f in cmd_hi.fragments if f.fragment_id.endswith(":long_notice:1"))
        assert part_1_hi.locale == "hi"

        # 4. User interrupts and corrects fact (salaried -> self_employed)
        t2 = reduce_event(
            compiled, s_hi,
            UserTurnAcceptedEvent(event_id="e5", session_id="session:multi", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Actually I am self-employed.")
        ).state
        p2 = StateProposal(
            proposal_id="p2", session_id="session:multi", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ProposeCaptureUpdate(field="employment_type", value="self_employed", evidence=Evidence(source_turn_id="t2", quote="self-employed"))]
        )
        s_biz = reduce_event(compiled, t2, StateProposalModelEvent(event_id="e6", session_id="session:multi", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2)).state
        # Complete annual income
        t3 = reduce_event(
            compiled, s_biz,
            UserTurnAcceptedEvent(event_id="e7", session_id="session:multi", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="My annual business income is 500000.")
        ).state
        p3 = StateProposal(
            proposal_id="p3", session_id="session:multi", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[ProposeCaptureUpdate(field="annual_income_inr", value=500000, evidence=Evidence(source_turn_id="t3", quote="500000"))]
        )
        red_corr = reduce_event(compiled, t3, StateProposalModelEvent(event_id="e8", session_id="session:multi", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p3))
        s_summary_corr = red_corr.state
        assert s_summary_corr.active_step_id == "summary"

        cmd_corr = next(c for c in red_corr.commands if isinstance(c, EmitSpeechManifestCommand))
        corr_frag_ids = [f.fragment_id for f in cmd_corr.fragments]
        # On changed dependency fingerprint, ALL parts are re-issued with the corrected facts
        assert any(fid.endswith(":long_notice:0") for fid in corr_frag_ids), "Part 0 must be re-issued with corrected facts"
        assert any(fid.endswith(":long_notice:1") for fid in corr_frag_ids), "Part 1 must be re-issued with corrected facts"
        part_0_corr = next(f for f in cmd_corr.fragments if f.fragment_id.endswith(":long_notice:0"))
        assert "self_employed" in part_0_corr.text

    def test_continue_reset_used_resets_once_and_second_exhaustion_exits_partial(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        flow["steps"][0]["fallback"] = {"action": "clarify", "max_attempts": 1, "on_exhausted": "offer_exit"}
        compiled = compile_flow(flow)
        init_st = initial_session_state(compiled, "session:reset")

        # Turn 1: unclear answer -> reaches max_attempts 1 -> triggers offer_exit
        t1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:reset", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Unclear 1")).state
        p1 = StateProposal(proposal_id="p1", session_id="session:reset", turn_id="t1", generation_id="g1", base_state_version=t1.version, updates=[CandidateCaptureUpdate(field="employment_type", value=None, reason="ambiguous", evidence=Evidence(source_turn_id="t1", quote="Unclear"))])
        red1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:reset", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))
        assert red1.state.offer_exit_pending["employment"] is True

        # Turn 2: User explicitly chooses to continue ("Yes, continue")
        t2 = reduce_event(compiled, red1.state, UserTurnAcceptedEvent(event_id="e3", session_id="session:reset", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Yes, continue")).state
        # Reset used once, pending cleared, clarification attempts reset to 0
        assert t2.continue_reset_used["employment"] is True
        assert t2.offer_exit_pending.get("employment", False) is False
        assert t2.clarification_attempts["employment"] == 0

        # Turn 3: Second failure on same objective -> triggers offer_exit again
        t3 = reduce_event(compiled, t2, UserTurnAcceptedEvent(event_id="e4", session_id="session:reset", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="Unclear 2")).state
        p3 = StateProposal(proposal_id="p3", session_id="session:reset", turn_id="t3", generation_id="g3", base_state_version=t3.version, updates=[CandidateCaptureUpdate(field="employment_type", value=None, reason="ambiguous", evidence=Evidence(source_turn_id="t3", quote="Unclear"))])
        red3 = reduce_event(compiled, t3, StateProposalModelEvent(event_id="e5", session_id="session:reset", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p3))
        assert red3.state.offer_exit_pending["employment"] is True

        # Turn 4: User says continue again, but continue_reset_used is ALREADY True -> second budget exhausted exits partial into end
        red4 = reduce_event(compiled, red3.state, UserTurnAcceptedEvent(event_id="e6", session_id="session:reset", transport_epoch="epoch:1", turn_id="t4", generation_id="g4", text="Yes, continue"))
        assert red4.state.outcome == "partial"
        assert red4.state.active_step_id == "end"
        assert red4.state.teardown_emitted is False
        assert any(isinstance(c, EmitSpeechManifestCommand) and c.step_id == "end" for c in red4.commands)
    def test_max_turns_exceeded_terminates_partial(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        limits = FlowLimits(max_turns=2)
        compiled = compile_flow(flow, limits=limits)
        init_st = initial_session_state(compiled, "session:max_turns")

        # Turn 1: accepted, turn_history length 1 (< max_turns 2)
        t1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:max_turns", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Turn 1")).state
        assert t1.outcome == "in_progress"
        assert len(t1.turn_history) == 1

        # Turn 2: recorded in turn_history (length 2 >= max_turns 2) -> terminates partial with TeardownCommand
        red2 = reduce_event(compiled, t1, UserTurnAcceptedEvent(event_id="e2", session_id="session:max_turns", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Turn 2"))
        assert red2.state.outcome == "partial"
        assert red2.state.active_step_id == "end"
        assert len(red2.state.turn_history) == 2
        assert red2.state.last_accepted_turn_id == "t2"
        assert any(isinstance(c, TeardownCommand) and c.outcome == "partial" and c.reason == "max_turns_exceeded" for c in red2.commands)
        assert any(isinstance(c, CancelGenerationCommand) for c in red2.commands)
    def test_detour_count_bounded_per_objective(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        compiled = compile_flow(flow)
        state = initial_session_state(compiled, "session:detour")

        # User asks 3 FAQ questions on employment step (max_detours = 3)
        for i in range(1, 4):
            t = reduce_event(compiled, state, UserTurnAcceptedEvent(event_id=f"et_{i}", session_id="session:detour", transport_epoch="epoch:1", turn_id=f"t_{i}", generation_id=f"g_{i}", text=f"What is fee {i}?")).state
            p = StateProposal(proposal_id=f"p_{i}", session_id="session:detour", turn_id=f"t_{i}", generation_id=f"g_{i}", base_state_version=t.version, updates=[])
            state = reduce_event(compiled, t, StateProposalModelEvent(event_id=f"ep_{i}", session_id="session:detour", transport_epoch="epoch:1", turn_id=f"t_{i}", generation_id=f"g_{i}", proposal=p)).state
            assert state.detour_counts["employment"] == i

        # Fourth FAQ question arrives: detour count does NOT keep incrementing unboundedly
        t4 = reduce_event(compiled, state, UserTurnAcceptedEvent(event_id="et_4", session_id="session:detour", transport_epoch="epoch:1", turn_id="t_4", generation_id="g_4", text="What is fee 4?")).state
        p4 = StateProposal(proposal_id="p_4", session_id="session:detour", turn_id="t_4", generation_id="g_4", base_state_version=t4.version, updates=[])
        red4 = reduce_event(compiled, t4, StateProposalModelEvent(event_id="ep_4", session_id="session:detour", transport_epoch="epoch:1", turn_id="t_4", generation_id="g_4", proposal=p4))
        # Bounded at 3
        assert red4.state.detour_counts["employment"] == 3
        # Redirects to current objective with detour_limit_reached reason
        assert any(isinstance(c, RequestClarificationCommand) and c.reason == "detour_limit_reached" for c in red4.commands)

    def test_unclear_non_faq_answer_increments_clarification_attempts(self) -> None:
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        compiled = compile_flow(flow)
        state = initial_session_state(compiled, "session:unclear")

        # User mumbles non-FAQ text with no capture updates ("uhhh I guess so")
        t1 = reduce_event(compiled, state, UserTurnAcceptedEvent(event_id="e1", session_id="session:unclear", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="uhhh I guess so")).state
        p1 = StateProposal(proposal_id="p1", session_id="session:unclear", turn_id="t1", generation_id="g1", base_state_version=t1.version, updates=[])
        red1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="e2", session_id="session:unclear", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1))

        # Must NOT be treated as an FAQ detour; must increment clarification_attempts
        assert red1.state.clarification_attempts.get("employment", 0) == 1
        assert red1.state.detour_counts.get("employment", 0) == 0
        assert any(isinstance(c, RequestClarificationCommand) and c.reason == "clarification_needed" for c in red1.commands)

    def test_compile_flow_custom_limits_take_effect(self) -> None:
        """Custom FlowLimits profile stored in CompiledFlow is consumed by all runtime bounds."""
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        custom_limits = FlowLimits(
            max_turns=10,
            max_detours_per_objective=1,
            max_field_updates_per_proposal=1,
            max_speech_fragment_chars=30,
        )
        compiled = compile_flow(flow, limits=custom_limits)
        assert compiled.limits.max_turns == 10
        assert compiled.limits.max_detours_per_objective == 1
        assert compiled.limits.max_field_updates_per_proposal == 1
        assert compiled.limits.max_speech_fragment_chars == 30
        init_st = initial_session_state(compiled, "session:custom_lim", transport_epoch="epoch:1")

        # 1. Proposal update limit enforcement: 2 updates exceeds max_field_updates_per_proposal of 1
        t1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried and earn 80000")).state
        p_overlimit = StateProposal(
            proposal_id="p1", session_id="session:custom_lim", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[
                ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
                ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ]
        )
        red_prop = reduce_event(compiled, t1, StateProposalModelEvent(event_id="ep1", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p_overlimit))
        assert any(d.code == "INVALID_PROPOSAL" for d in red_prop.diagnostics)
        assert red_prop.state.fields["employment_type"].status == "unknown"

        # 2. Detour limit enforcement: max_detours_per_objective = 1
        # Detour 1
        t_faq1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="ef1", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="tf1", generation_id="gf1", text="What is the fee?")).state
        p_faq1 = StateProposal(proposal_id="pf1", session_id="session:custom_lim", turn_id="tf1", generation_id="gf1", base_state_version=t_faq1.version, updates=[])
        s_faq1 = reduce_event(compiled, t_faq1, StateProposalModelEvent(event_id="epf1", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="tf1", generation_id="gf1", proposal=p_faq1)).state
        assert s_faq1.detour_counts["employment"] == 1

        # Detour 2 hits custom limit 1 -> detour count bounded at 1, emits detour_limit_reached
        t_faq2 = reduce_event(compiled, s_faq1, UserTurnAcceptedEvent(event_id="ef2", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="tf2", generation_id="gf2", text="What is the interest?")).state
        p_faq2 = StateProposal(proposal_id="pf2", session_id="session:custom_lim", turn_id="tf2", generation_id="gf2", base_state_version=t_faq2.version, updates=[])
        red_faq2 = reduce_event(compiled, t_faq2, StateProposalModelEvent(event_id="epf2", session_id="session:custom_lim", transport_epoch="epoch:1", turn_id="tf2", generation_id="gf2", proposal=p_faq2))
        assert red_faq2.state.detour_counts["employment"] == 1
        assert any(isinstance(c, RequestClarificationCommand) and c.reason == "detour_limit_reached" for c in red_faq2.commands)

        # 3. Speech fragment chunking limit enforcement: max_speech_fragment_chars = 30
        # Step 'summary' message 'no_guarantee' has 66 characters and must be split into multiple <= 30 char parts
        summary_manifest = build_manifest_for_step("summary", compiled, init_st)
        assert summary_manifest is not None
        no_guar_frags = [f for f in summary_manifest.fragments if ":no_guarantee:" in f.fragment_id]
        assert len(no_guar_frags) > 1, "Must split 66-character message into multiple parts"
        assert all(len(f.text) <= 30 for f in no_guar_frags)
        assert all(len(f.text) <= 30 for f in summary_manifest.fragments)
    def test_replay_and_events_after_partial_preempts_all_future_events(self) -> None:
        """Terminal partial state preempts all future turns, proposals, stops, and playbacks without duplicate teardown."""
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        limits = FlowLimits(max_turns=1)
        compiled = compile_flow(flow, limits=limits)
        init_st = initial_session_state(compiled, "session:term_test")

        # Turn 1 hits max_turns 1 -> terminates with outcome="partial" and exactly 1 TeardownCommand
        red1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Hello"))
        assert red1.state.outcome == "partial"
        assert len(red1.state.turn_history) == 1
        assert len([c for c in red1.commands if isinstance(c, TeardownCommand)]) == 1
        term_state = red1.state

        # 1. Replaying exact turn 1 -> idempotent return, NO state change, NO duplicate commands
        replay_turn = reduce_event(compiled, term_state, UserTurnAcceptedEvent(event_id="e1", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Hello"))
        assert replay_turn.state.version == term_state.version
        assert len(replay_turn.commands) == 0

        # 2. Subsequent new turn 2 arriving after termination -> strictly preempted, NO duplicate teardown
        new_turn = reduce_event(compiled, term_state, UserTurnAcceptedEvent(event_id="e2", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Are you there?"))
        assert new_turn.state.version == term_state.version
        assert len(new_turn.commands) == 0

        # 3. Model proposal arriving after termination -> ignored
        prop_ev = StateProposalModelEvent(
            event_id="ep1", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
            proposal=StateProposal(proposal_id="p1", session_id="session:term_test", turn_id="t1", generation_id="g1", base_state_version=1, updates=[])
        )
        prop_red = reduce_event(compiled, term_state, prop_ev)
        assert prop_red.state.version == term_state.version
        assert len(prop_red.commands) == 0

        # 4. User stopped arriving after termination -> idempotent no-op, NO duplicate teardown
        stop_ev = UserStoppedEvent(event_id="es1", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t1", generation_id="g1")
        stop_red = reduce_event(compiled, term_state, stop_ev)
        assert stop_red.state.version == term_state.version
        assert len(stop_red.commands) == 0

        # 5. Playback event arriving after termination -> ignored
        play_ev = FragmentPlayedOutputEvent(
            event_id="epl1", session_id="session:term_test", transport_epoch="epoch:1", turn_id="t1", generation_id="g1",
            manifest_id="m1", fragment_id="f1", content_hash="h1", evidence_id="ev1", capability_report_hash="rep"
        )
        play_red = reduce_event(compiled, term_state, play_ev)
        assert play_red.state.version == term_state.version
        assert len(play_red.commands) == 0

    def test_partial_exit_closing_manifest_delivery_then_teardown_and_repeated_stop(self) -> None:
        """Partial exit enters end step, delivers required closing manifest, emits single teardown, and suppresses repeated stop."""
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        compiled = compile_flow(flow)
        init_st = initial_session_state(compiled, "session:part_close")

        # Turn 1: Confirm employment type to satisfy salary prerequisite
        t1 = reduce_event(compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:part_close", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I am salaried")).state
        p1 = StateProposal(proposal_id="p1", session_id="session:part_close", turn_id="t1", generation_id="g1", base_state_version=t1.version, updates=[ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))])
        s1 = reduce_event(compiled, t1, StateProposalModelEvent(event_id="ep1", session_id="session:part_close", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1)).state
        assert s1.active_step_id == "salary"

        # Turn 2: User refuses income -> takes partial exit to end step
        t2 = reduce_event(compiled, s1, UserTurnAcceptedEvent(event_id="e2", session_id="session:part_close", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="I do not want to share my income.")).state
        p2 = StateProposal(proposal_id="p2", session_id="session:part_close", turn_id="t2", generation_id="g2", base_state_version=t2.version, updates=[RefuseCaptureUpdate(field="monthly_income_inr", evidence=Evidence(source_turn_id="t2", quote="do not want to share"))])
        red2 = reduce_event(compiled, t2, StateProposalModelEvent(event_id="ep2", session_id="session:part_close", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2))

        # 1. Partial exit enters end step with outcome="partial", but teardown is NOT emitted yet
        assert red2.state.active_step_id == "end"
        assert red2.state.outcome == "partial"
        assert red2.state.teardown_emitted is False
        assert not any(isinstance(c, TeardownCommand) for c in red2.commands)

        # 2. End step emits closing manifest command
        manifest_cmd = next((c for c in red2.commands if isinstance(c, EmitSpeechManifestCommand)), None)
        assert manifest_cmd is not None
        assert manifest_cmd.step_id == "end"
        closing_frags = manifest_cmd.fragments
        assert len(closing_frags) > 0

        # 3. Deliver closing fragments via chat_committed
        curr_st = red2.state
        for frag in closing_frags:
            del_ev = ChatCommittedOutputEvent(
                event_id=f"del_{frag.fragment_id}",
                session_id="session:part_close",
                transport_epoch="epoch:1",
                turn_id="t2",
                generation_id="g2",
                manifest_id=manifest_cmd.manifest_id,
                fragment_id=frag.fragment_id,
                message_id=frag.semantic_fragment_id.split(":")[1],
                durable_delivery_id=f"dur_{frag.fragment_id}",
            )
            red_del = reduce_event(compiled, curr_st, del_ev)
            curr_st = red_del.state

        # 4. Closing delivery complete: teardown_emitted is now True, exactly one TeardownCommand emitted
        assert curr_st.teardown_emitted is True
        assert curr_st.outcome == "partial"
        assert any(isinstance(c, TeardownCommand) and c.outcome == "partial" and c.reason == "end_delivery_complete" for c in red_del.commands)

        # 5. Repeated stop event after actual termination is an idempotent no-op without duplicate teardown
        stop_ev = UserStoppedEvent(event_id="es_post", session_id="session:part_close", transport_epoch="epoch:1", turn_id="t2", generation_id="g2")
        stop_red = reduce_event(compiled, curr_st, stop_ev)
        assert stop_red.state.version == curr_st.version
        assert len(stop_red.commands) == 0

    def test_invalid_candidate_rejected_and_cannot_be_confirmed(self, credit_card_compiled) -> None:
        """Invalid candidate value (e.g. non-positive integer) is rejected and cannot be confirmed."""
        init_st = initial_session_state(credit_card_compiled, "session:inv_cand", transport_epoch="epoch:1")
        # Turn 1: user text with negative income
        t1 = reduce_event(credit_card_compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:inv_cand", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="I earn -500")).state
        p_invalid_cand = StateProposal(
            proposal_id="p1", session_id="session:inv_cand", turn_id="t1", generation_id="g1", base_state_version=t1.version,
            updates=[CandidateCaptureUpdate(field="monthly_income_inr", value=-500, reason="ambiguous", evidence=Evidence(source_turn_id="t1", quote="-500"))]
        )
        red1 = reduce_event(credit_card_compiled, t1, StateProposalModelEvent(event_id="ep1", session_id="session:inv_cand", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p_invalid_cand))
        assert any(d.code == "INVALID_PROPOSAL_VALUE" for d in red1.diagnostics)
        assert red1.state.fields["monthly_income_inr"].status == "unknown"

        # Attempt to confirm the unconfirmed candidate fails
        t2 = reduce_event(credit_card_compiled, red1.state, UserTurnAcceptedEvent(event_id="e2", session_id="session:inv_cand", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Yes, correct")).state
        p_confirm = StateProposal(
            proposal_id="p2", session_id="session:inv_cand", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ConfirmCaptureUpdate(field="monthly_income_inr", candidate_state_version=1, evidence=Evidence(source_turn_id="t2", quote="Yes"))]
        )
        red2 = reduce_event(credit_card_compiled, t2, StateProposalModelEvent(event_id="ep2", session_id="session:inv_cand", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p_confirm))
        assert any(d.code == "INVALID_CONFIRMATION" for d in red2.diagnostics)
        assert red2.state.fields["monthly_income_inr"].status == "unknown"

    def test_forged_old_yes_confirmation_rejected(self, credit_card_compiled) -> None:
        """Confirmation evidence citing an old historical turn rather than current turn is rejected."""
        init_st = initial_session_state(credit_card_compiled, "session:forged_conf", transport_epoch="epoch:1")
        # Turn 1: User says "Yes I am salaried" -> employment confirmed
        t1 = reduce_event(credit_card_compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Yes I am salaried")).state
        p1 = StateProposal(proposal_id="p1", session_id="session:forged_conf", turn_id="t1", generation_id="g1", base_state_version=t1.version, updates=[ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))])
        s1 = reduce_event(credit_card_compiled, t1, StateProposalModelEvent(event_id="ep1", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1)).state

        # Turn 2: User mentions preference ambiguously -> candidate status
        t2 = reduce_event(credit_card_compiled, s1, UserTurnAcceptedEvent(event_id="e2", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="Maybe travel cards")).state
        p2 = StateProposal(proposal_id="p2", session_id="session:forged_conf", turn_id="t2", generation_id="g2", base_state_version=t2.version, updates=[CandidateCaptureUpdate(field="card_preference", value="travel", reason="needs_confirmation", evidence=Evidence(source_turn_id="t2", quote="travel"))])
        s2 = reduce_event(credit_card_compiled, t2, StateProposalModelEvent(event_id="ep2", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p2)).state
        assert s2.fields["card_preference"].status == "candidate"
        cand_ver = getattr(s2.fields["card_preference"], "candidate_state_version", 1)

        # Turn 3: User says something non-confirming: "Wait let me check"
        t3 = reduce_event(credit_card_compiled, s2, UserTurnAcceptedEvent(event_id="e3", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="Wait let me check")).state
        # Attacker tries to confirm using old turn 1's "Yes"
        p_forged = StateProposal(
            proposal_id="p3", session_id="session:forged_conf", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[ConfirmCaptureUpdate(field="card_preference", candidate_state_version=cand_ver, evidence=Evidence(source_turn_id="t1", quote="Yes"))]
        )
        red3 = reduce_event(credit_card_compiled, t3, StateProposalModelEvent(event_id="ep3", session_id="session:forged_conf", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p_forged))
        assert any(d.code == "INVALID_CONFIRMATION" for d in red3.diagnostics)
        assert red3.state.fields["card_preference"].status == "candidate"

    def test_unrelated_or_no_answer_cannot_confirm_candidate(self, credit_card_compiled) -> None:
        """Negative ('No') or unrelated user responses cannot confirm a candidate field."""
        init_st = initial_session_state(credit_card_compiled, "session:no_ans", transport_epoch="epoch:1")
        t1 = reduce_event(credit_card_compiled, init_st, UserTurnAcceptedEvent(event_id="e1", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", text="Maybe travel")).state
        p1 = StateProposal(proposal_id="p1", session_id="session:no_ans", turn_id="t1", generation_id="g1", base_state_version=t1.version, updates=[CandidateCaptureUpdate(field="card_preference", value="travel", reason="needs_confirmation", evidence=Evidence(source_turn_id="t1", quote="travel"))])
        s1 = reduce_event(credit_card_compiled, t1, StateProposalModelEvent(event_id="ep1", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t1", generation_id="g1", proposal=p1)).state
        assert s1.fields["card_preference"].status == "candidate"
        cand_ver = getattr(s1.fields["card_preference"], "candidate_state_version", 1)

        # User says "No that is incorrect"
        t2 = reduce_event(credit_card_compiled, s1, UserTurnAcceptedEvent(event_id="e2", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", text="No that is incorrect")).state
        p_no = StateProposal(
            proposal_id="p2", session_id="session:no_ans", turn_id="t2", generation_id="g2", base_state_version=t2.version,
            updates=[ConfirmCaptureUpdate(field="card_preference", candidate_state_version=cand_ver, evidence=Evidence(source_turn_id="t2", quote="No"))]
        )
        red2 = reduce_event(credit_card_compiled, t2, StateProposalModelEvent(event_id="ep2", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t2", generation_id="g2", proposal=p_no))
        assert any(d.code == "INVALID_CONFIRMATION" for d in red2.diagnostics)
        assert red2.state.fields["card_preference"].status == "candidate"

        # User says unrelated "I like pizza"
        t3 = reduce_event(credit_card_compiled, s1, UserTurnAcceptedEvent(event_id="e3", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", text="I like pizza")).state
        p_unrelated = StateProposal(
            proposal_id="p3", session_id="session:no_ans", turn_id="t3", generation_id="g3", base_state_version=t3.version,
            updates=[ConfirmCaptureUpdate(field="card_preference", candidate_state_version=cand_ver, evidence=Evidence(source_turn_id="t3", quote="pizza"))]
        )
        red3 = reduce_event(credit_card_compiled, t3, StateProposalModelEvent(event_id="ep3", session_id="session:no_ans", transport_epoch="epoch:1", turn_id="t3", generation_id="g3", proposal=p_unrelated))
        assert any(d.code == "INVALID_CONFIRMATION" for d in red3.diagnostics)
        assert red3.state.fields["card_preference"].status == "candidate"

    def test_tool_registry_deterministic_explicit_and_no_global_effects(self) -> None:
        """Action validation is fail-closed by default, succeeds only with explicit registry, and has zero global side effects."""
        with open(FIXTURES_DIR / "valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
            flow = json.load(f)["flow"]
        action_step = {
            "id": "kyc_verify",
            "title": "KYC Verification",
            "type": "action",
            "instruction": "Verify customer identity",
            "required": True,
            "priority": 50,
            "prerequisites": [],
            "captures": [],
            "transitions": [{"when": {"operator": "completed"}, "to": "end"}],
            "action": {
                "tool_id": "kyc_service",
                "input_fields": {},
                "result_fields": {},
            },
        }
        flow["steps"].append(action_step)
        # Wire summary -> kyc_verify -> end to ensure all steps are structurally and semantically reachable
        for s in flow["steps"]:
            if s["id"] == "summary":
                s["transitions"] = [{"when": {"operator": "completed"}, "to": "kyc_verify"}]

        # 1. Default validation without tools parameter fails closed with TOOL_UNQUALIFIED
        diags_default = validate_executable(flow)
        assert any(d.code == "TOOL_UNQUALIFIED" for d in diags_default)

        # Default compilation fails
        with pytest.raises(ValueError, match="TOOL_UNQUALIFIED"):
            compile_flow(flow)

        # 2. Fully pinned ToolContract per flow contract section 10
        pinned_contract = ToolContract(
            tool_id="kyc_service",
            version="1.0.0",
            input_schema={"type": "object", "properties": {"pan": {"type": "string"}}},
            result_schema={"type": "object", "properties": {"verified": {"type": "boolean"}}},
            required_roles=["compliance_agent"],
            required_scopes=["kyc:read", "kyc:verify"],
            field_bindings={"pan": "pan_number"},
            confirmation_policy="explicit_user",
            idempotency_scope="session",
            max_retries=2,
            timeout_config_key="kyc_verification_timeout_ms",
            resumable_status_lookup=True,
            expected_failure_types=["PAN_NOT_FOUND", "SERVICE_UNAVAILABLE"],
            compensation_procedure="manual_document_review",
        )
        assert len(pinned_contract.contract_hash) == 64

        # 3. Explicit immutable registry
        explicit_registry = ToolRegistry.from_contracts([pinned_contract])
        assert explicit_registry.is_registered("kyc_service")
        assert not explicit_registry.is_registered("unregistered_tool")

        # 4. Validating with explicit registry resolves TOOL_UNQUALIFIED, but fails closed with ACTION_UNSUPPORTED
        diags_with_tools = validate_executable(flow, tools=explicit_registry)
        assert not any(d.code == "TOOL_UNQUALIFIED" for d in diags_with_tools)
        assert any(d.code == "ACTION_UNSUPPORTED" for d in diags_with_tools)

        # Compiling with registered action fails closed with ACTION_UNSUPPORTED until complete runtime lifecycle exists
        with pytest.raises(ValueError, match="ACTION_UNSUPPORTED"):
            compile_flow(flow, tools=explicit_registry)
        # 5. Zero global side effects: validating again with default tools remains fail-closed
        diags_post = validate_executable(flow)
        assert any(d.code == "TOOL_UNQUALIFIED" for d in diags_post)
        with pytest.raises(ValueError, match="TOOL_UNQUALIFIED"):
            compile_flow(flow)

    def test_forged_action_completed_against_collect_step_rejected(self, credit_card_compiled) -> None:
        """Forged action_completed event while active step is collect (or any non-action step) is rejected with zero mutation."""
        init_st = initial_session_state(credit_card_compiled, "session:forged_act", transport_epoch="epoch:1")
        assert init_st.active_step_id == "employment"
        assert init_st.steps["employment"] == "active"
        init_ver = init_st.version

        # Attacker injects action_completed event
        forged_event = {
            "type": "action_completed",
            "session_id": "session:forged_act",
            "execution_id": "exec_fake",
            "tool_id": "transfer_money",
        }
        red = reduce_event(credit_card_compiled, init_st, forged_event)

        # Must fail closed with FORGED_ACTION diagnostic
        assert any(d.code == "FORGED_ACTION" for d in red.diagnostics)
        # Must NOT advance state or complete step
        assert red.state.steps["employment"] == "active"
        assert red.state.active_step_id == "employment"
        assert red.state.version == init_ver
        assert len(red.commands) == 0

    def test_forged_action_completed_against_action_step_rejected(self, credit_card_compiled) -> None:
        """Action_completed event without prior controller-authorized request is rejected with zero mutation."""
        init_st = initial_session_state(credit_card_compiled, "session:forged_act2", transport_epoch="epoch:1")
        # Synthesize state pointing to an action step
        action_state = init_st.model_copy(update={
            "active_step_id": "action_step",
            "steps": {**init_st.steps, "action_step": "active"},
        })
        # Mock compiled flow with action step definition
        compiled_with_action = credit_card_compiled.model_copy(update={
            "normalized": {
                **credit_card_compiled.normalized,
                "steps": [
                    *credit_card_compiled.normalized.get("steps", []),
                    {"id": "action_step", "type": "action", "action": {"tool_id": "transfer_money"}},
                ],
            }
        })
        action_ver = action_state.version
        forged_event = {
            "type": "action_completed",
            "session_id": "session:forged_act2",
            "execution_id": "exec_unauthorized",
            "tool_id": "transfer_money",
        }
        red = reduce_event(compiled_with_action, action_state, forged_event)

        # Must fail closed with UNAUTHORIZED_ACTION diagnostic
        assert any(d.code == "UNAUTHORIZED_ACTION" for d in red.diagnostics)
        # Must NOT mark action complete or advance active step
        assert red.state.steps["action_step"] == "active"
        assert red.state.active_step_id == "action_step"
        assert red.state.version == action_ver
        assert len(red.commands) == 0
