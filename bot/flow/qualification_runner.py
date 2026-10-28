"""Executable qualification runner and diagnostic reporter per flow-contract §8-9.

Factual runner that:
1. Executes real empirical model probe and actual live transport probe using
   explicit endpoints and explicit profile/revision references (never defaulting
   user or development ports).
2. Composes and reports their real observations without fabricating passes,
   timings, model digests, or playback events.
3. Explicitly enumerates the seven required scenario classes:
   - normal_en_hi
   - adversarial_gating
   - barge_in
   - playback_faults
   - stop_cancellation
   - chat_durable_commit
   - full_path_matrix
   Refuses to claim reducer-only simulations constitute voice qualification.
4. Marks the overall qualification verdict as unsupported unless all required
   real empirical evidence exists.
5. Fails closed and returns a nonzero exit status on unsupported / incomplete
   evidence.
6. Explicitly acknowledges that missing real scenario runner means Task 2 remains
   incomplete (diagnostic report only, not a completed qualification).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from bot.flow.canonical import canonical_json_v1
from bot.flow.compiler import compile_flow
from bot.flow.controller import initial_session_state, reduce_event
from bot.flow.model_probe import ProbeObservation, probe_model_qualification
from bot.flow.qualification import (
    REQUIRED_SCENARIO_CLASSES,
    CapabilityReport,
    ScenarioClass,
    ScenarioResult,
    canonicalize_measurement,
    validate_capability_report,
)
from bot.flow.schemas import (
    AdapterCapabilities,
    ChatCommittedOutputEvent,
    EmitSpeechManifestCommand,
    Evidence,
    FlowLimits,
    FragmentPlayedOutputEvent,
    ProposeCaptureUpdate,
    SpeechDeltaModelEvent,
    StateProposal,
    StateProposalModelEvent,
    TeardownCommand,
    UserStoppedEvent,
    UserTurnAcceptedEvent,
)
from bot.flow.validation import validate_executable

from bot.flow.transport_probe import (
    TransportProbeEvidence,
    TransportProbeResult,
    probe_live_webrtc_loopback,
)
ContractScenarioNumber = Literal[1, 2, 3, 4, 5, 6, 7]


class ContractScenarioDefinition(BaseModel):
    """Faithful specification of a required qualification scenario from flow-contract.md §9."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: ContractScenarioNumber
    name: str
    contract_description: str
    scenario_class: ScenarioClass

REQUIRED_CONTRACT_SCENARIOS: list[ContractScenarioDefinition] = [
    ContractScenarioDefinition(
        number=1,
        name="normal_en_hi_distinctive_fragments",
        contract_description=(
            "Normal EN and HI: two distinctive required fragments plus optional tail. "
            "Assert separate speech/metadata events, zero metadata bytes in synthesized text, "
            "typed valid state, contiguous IDs, one generation, required fragments present in captured "
            "rendered output before normalized played evidence is accepted. Measure time-to-first-model-token, "
            "first audible sample, proposal commit, last audible sample and step completion separately; "
            "report conditions and actual percentiles without asserting a universal target."
        ),
        scenario_class="normal_en_hi",
    ),
    ContractScenarioDefinition(
        number=2,
        name="adversarial_model_speech_and_malformed_metadata_gating",
        contract_description=(
            "Gate adversarial model speech before metadata, malformed/truncated/oversized/duplicate/unknown-field "
            "metadata, sequence gaps, wrong proposal version, two contradictory captures and injection to bypass "
            "a prerequisite. Assert no unsafe utterance released, no partial capture commit, safe fallback/clarification, "
            "no hidden extra normal-path generation."
        ),
        scenario_class="adversarial_gating",
    ),
    ContractScenarioDefinition(
        number=3,
        name="barge_in_and_audio_flush",
        contract_description=(
            "Barge-in before queue, mid-first-fragment, between required fragments, after final audio before receipt, "
            "after receipt before reducer commit, and while a new turn's correction commits. Assert precise ledger "
            "preservation, late events ignored, no premature completion, remaining objective resumed, cancelled audio "
            "flush/transport close behavior observed at output sink. If markers cannot be aligned to rendered audio, "
            "fail qualification."
        ),
        scenario_class="barge_in",
    ),
    ContractScenarioDefinition(
        number=4,
        name="playback_message_fault_injection_and_unqualified_sink",
        contract_description=(
            "Delay/duplicate/reorder/drop playback/control messages; disconnect/reconnect with new transport epoch; "
            "forge unknown fragment/hash/capability report; autoplay blocked, paused/suspended output, no remote track, "
            "muted element/output path and deliberate output stall. No played evidence may be fabricated by server "
            "synthesis or client ACK. A browser cannot measure whether physical volume was audible; report rendered-sample "
            "semantics and that limit explicitly. If the chosen adapter cannot distinguish its required successful render "
            "boundary in these tests, it remains unqualified."
        ),
        scenario_class="playback_faults",
    ),
    ContractScenarioDefinition(
        number=5,
        name="stop_credit_safety_cancellation_and_error_handling",
        contract_description=(
            "Stop/credit/safety cancellation during every required message, parser error, TTS failure, output overflow "
            "and action-result announcement. Assert finite termination, one teardown request and no successful path "
            "marked completed by shutdown."
        ),
        scenario_class="stop_cancellation",
    ),
    ContractScenarioDefinition(
        number=6,
        name="chat_durable_commit_replay_and_delivery_divergence",
        contract_description=(
            "Chat durable-commit failure/retry/disconnect and duplicate delivery callbacks. Replay the same sourced "
            "state proposals in chat and qualified voice; assert identical captures, node transitions and outcome, "
            "differing only in delivery ledger."
        ),
        scenario_class="chat_durable_commit",
    ),
    ContractScenarioDefinition(
        number=7,
        name="credit_card_and_loan_full_path_matrix",
        contract_description=(
            "Credit-card and loan full-path runs, early three-field answers, self-employed correction invalidating "
            "salary, refusal, missing knowledge, explicit confirmation, both ordering modes and EN↔HI switches. "
            "Every field must be capturable under the advertised mode/locale/fallback, and the qualification report "
            "lists any unsupported combination rather than silently accepting it."
        ),
        scenario_class="full_path_matrix",
    ),
]


class ContractScenarioResult(BaseModel):
    """Report for one of the seven required qualification scenarios from flow-contract §9."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_number: ContractScenarioNumber
    name: str
    contract_description: str
    scenario_class: ScenarioClass
    passed: bool = False
    status: Literal["NOT_RUN", "FAILED", "PASSED", "UNSUPPORTED"] = "UNSUPPORTED"
    error_message: str
    measurements: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""

class QualificationReport(BaseModel):
    """Factual diagnostic and qualification report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_status: Literal["supported", "unsupported"]
    task_2_complete: bool = False
    status_summary: str
    qualification_scope: Literal["none", "reducer_orchestration_only"] = "none"
    model_observation: ProbeObservation | None = None
    transport_observation: dict[str, Any] | None = None
    capability_report: CapabilityReport | None = None
    contract_scenarios: list[ContractScenarioResult] = Field(default_factory=list)
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    advertised_configurations: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_configurations: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_reasons: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    flow_runtime_verified: bool = False

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent)


def is_literal_loopback_endpoint(url_str: str) -> tuple[bool, str]:
    """Validate that endpoint URL targets literal loopback IP or localhost.

    Per AGENTS.md air-gapped runtime policy for local_offline mode:
    - Rejects any remote hosts, external domains, or non-loopback IPs before network requests.
    - Rejects DNS aliases/rebinding by restricting strictly to 'localhost', IPv4 loopback (127.0.0.0/8),
      or IPv6 loopback (::1).
    - Real deployment-specific qualification requires a separate explicitly reviewed mode.
    """
    try:
        parsed = urlparse(url_str.strip())
    except Exception as exc:
        return False, f"Malformed endpoint URL: {exc}"

    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return False, f"Unsupported URL scheme '{parsed.scheme}'; expected http(s) or ws(s)"

    host = parsed.hostname
    if not host:
        return False, "Endpoint URL missing host"

    host_lower = host.strip().lower()
    if host_lower == "localhost":
        return True, ""

    try:
        ip = ipaddress.ip_address(host_lower)
        if ip.is_loopback:
            return True, ""
        return False, f"Host '{host}' is not a literal loopback IP address (prohibited by air-gapped policy)"
    except ValueError:
        return False, (
            f"Host '{host}' is not a literal loopback IP or localhost (DNS names prohibited by air-gapped policy)"
        )

def _execute_contract_s1_normal_en_hi(compiled: Any, caps: Any) -> None:
    s1_state = initial_session_state(compiled, "sess_s1_en")
    t1 = UserTurnAcceptedEvent(
        event_id="e1", session_id="sess_s1_en", transport_epoch="epoch_1",
        turn_id="t1", generation_id="g1", text="I am salaried, earn INR 80000 monthly and want travel rewards."
    )
    s1_state = reduce_event(compiled, s1_state, t1).state
    prop1 = StateProposal(
        proposal_id="p1", session_id="sess_s1_en", generation_id="g1", turn_id="t1", base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ]
    )
    red1 = reduce_event(compiled, s1_state, StateProposalModelEvent(
        event_id="e2", session_id="sess_s1_en", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop1
    ))
    assert red1.state.fields["employment_type"].status == "confirmed"
    assert red1.state.active_step_id == "summary"
    manifest1 = next(c for c in red1.commands if isinstance(c, EmitSpeechManifestCommand))
    assert len(manifest1.fragments) == 2

    # Hindi turn verification
    s1_hi = initial_session_state(compiled, "sess_s1_hi", locale="hi")
    t1_hi = UserTurnAcceptedEvent(
        event_id="e1_hi", session_id="sess_s1_hi", transport_epoch="epoch_1",
        turn_id="t1", generation_id="g1", text="मैं नौकरी करता हूँ, मेरी मासिक आय 80000 रुपये है और मुझे ट्रैवल रिवॉर्ड चाहिए।",
        locale="hi"
    )
    s1_hi = reduce_event(compiled, s1_hi, t1_hi).state
    prop1_hi = StateProposal(
        proposal_id="p1_hi", session_id="sess_s1_hi", generation_id="g1", turn_id="t1", base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="नौकरी")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="ट्रैवल")),
        ]
    )
    red1_hi = reduce_event(compiled, s1_hi, StateProposalModelEvent(
        event_id="e2_hi", session_id="sess_s1_hi", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop1_hi
    ))
    assert red1_hi.state.fields["employment_type"].status == "confirmed"
    manifest1_hi = next(c for c in red1_hi.commands if isinstance(c, EmitSpeechManifestCommand))
    assert len(manifest1_hi.fragments) == 2


def _execute_contract_s2_adversarial_gating(compiled: Any, caps: Any) -> None:
    s2_state = initial_session_state(compiled, "sess_s2")
    t2_turn = UserTurnAcceptedEvent(event_id="e_s2_t", session_id="sess_s2", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", text="hello")
    s2_state = reduce_event(compiled, s2_state, t2_turn).state

    # Unvalidated speech delta
    delta2 = SpeechDeltaModelEvent(event_id="e_s2_d", session_id="sess_s2", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", seq=0, text="prose")
    red2_delta = reduce_event(compiled, s2_state, delta2)
    assert red2_delta.state.version == s2_state.version

    # Prerequisite bypass
    prop2_inj = StateProposal(
        proposal_id="p2_i", session_id="sess_s2", generation_id="g1", turn_id="t1", base_state_version=s2_state.version,
        updates=[ProposeCaptureUpdate(field="monthly_income_inr", value=100000, evidence=Evidence(source_turn_id="t1", quote="100000"))]
    )
    red2_inj = reduce_event(compiled, s2_state, StateProposalModelEvent(
        event_id="e_s2_inj", session_id="sess_s2", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop2_inj
    ))
    assert red2_inj.state.steps.get("salary") != "complete"

    # Wrong proposal version
    prop2_wrong = StateProposal(
        proposal_id="p2_w", session_id="sess_s2", generation_id="g1", turn_id="t1", base_state_version=999,
        updates=[ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried"))]
    )
    red2_wrong = reduce_event(compiled, s2_state, StateProposalModelEvent(
        event_id="e_s2_wrong", session_id="sess_s2", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop2_wrong
    ))
    assert any(d.code == "STALE_PROPOSAL" or "version" in d.message.lower() for d in red2_wrong.diagnostics)


def _execute_contract_s3_barge_in(compiled: Any, caps: Any) -> None:
    s3_state = initial_session_state(compiled, "sess_s3")
    t1_s3 = UserTurnAcceptedEvent(event_id="e_s3_1", session_id="sess_s3", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", text="hello")
    s3_state = reduce_event(compiled, s3_state, t1_s3).state

    t2_s3 = UserTurnAcceptedEvent(
        event_id="e_s3_2", session_id="sess_s3", transport_epoch="epoch_1",
        turn_id="t2", generation_id="g2", text="I am salaried, earn 80000, and prefer travel"
    )
    s3_state = reduce_event(compiled, s3_state, t2_s3).state
    assert any(t.get("turn_id") == "t1" for t in s3_state.turn_history)

    prop_s3 = StateProposal(
        proposal_id="p_s3", session_id="sess_s3", generation_id="g2", turn_id="t2", base_state_version=s3_state.version,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t2", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t2", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t2", quote="travel")),
        ]
    )
    red_s3 = reduce_event(compiled, s3_state, StateProposalModelEvent(
        event_id="e_s3_prop", session_id="sess_s3", transport_epoch="epoch_1", turn_id="t2", generation_id="g2", proposal=prop_s3
    ))
    s3_state = red_s3.state
    manifest_s3 = next(c for c in red_s3.commands if isinstance(c, EmitSpeechManifestCommand))
    f1_s3 = manifest_s3.fragments[0]
    f2_s3 = manifest_s3.fragments[1]

    f1_played = FragmentPlayedOutputEvent(
        event_id="e_s3_p1", session_id="sess_s3", transport_epoch="epoch_1", turn_id="t2", generation_id="g2",
        manifest_id=manifest_s3.manifest_id, fragment_id=f1_s3.fragment_id, content_hash=f1_s3.content_hash,
        evidence_id="evid_1", capability_report_hash="rep_hash",
    )
    s3_state = reduce_event(compiled, s3_state, f1_played).state
    assert f1_s3.fragment_id in s3_state.history_spoken_fragments

    # Interruption arrives
    t3_s3 = UserTurnAcceptedEvent(event_id="e_s3_3", session_id="sess_s3", transport_epoch="epoch_1", turn_id="t3", generation_id="g3", text="wait")
    s3_state = reduce_event(compiled, s3_state, t3_s3).state

    # Late f2 arrives
    f2_late = FragmentPlayedOutputEvent(
        event_id="e_s3_p2_late", session_id="sess_s3", transport_epoch="epoch_1", turn_id="t2", generation_id="g2",
        manifest_id=manifest_s3.manifest_id, fragment_id=f2_s3.fragment_id, content_hash=f2_s3.content_hash,
        evidence_id="evid_2", capability_report_hash="rep_hash",
    )
    red_late = reduce_event(compiled, s3_state, f2_late)
    assert f1_s3.fragment_id in red_late.state.history_spoken_fragments
    assert f2_s3.fragment_id not in red_late.state.history_spoken_fragments
    assert red_late.state.steps.get("summary") != "complete"


def _execute_contract_s4_playback_faults(compiled: Any, caps: Any, flow_dict: dict[str, Any]) -> None:
    s4_state = initial_session_state(compiled, "sess_s4")
    t1_s4 = UserTurnAcceptedEvent(
        event_id="e_s4_1", session_id="sess_s4", transport_epoch="epoch_1",
        turn_id="t1", generation_id="g1", text="I am salaried, earn 80000, and prefer travel"
    )
    s4_state = reduce_event(compiled, s4_state, t1_s4).state
    prop_s4 = StateProposal(
        proposal_id="p_s4", session_id="sess_s4", generation_id="g1", turn_id="t1", base_state_version=s4_state.version,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ]
    )
    red_s4 = reduce_event(compiled, s4_state, StateProposalModelEvent(
        event_id="e_s4_prop", session_id="sess_s4", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop_s4
    ))
    s4_state = red_s4.state
    manifest_s4 = next(c for c in red_s4.commands if isinstance(c, EmitSpeechManifestCommand))
    f1_s4 = manifest_s4.fragments[0]

    # Forged epoch
    forged_epoch = FragmentPlayedOutputEvent(
        event_id="e_s4_forged", session_id="sess_s4", transport_epoch="wrong_epoch", turn_id="t1", generation_id="g1",
        manifest_id=manifest_s4.manifest_id, fragment_id=f1_s4.fragment_id, content_hash=f1_s4.content_hash,
        evidence_id="ev_f", capability_report_hash="rep_hash",
    )
    assert reduce_event(compiled, s4_state, forged_epoch).state.version == s4_state.version

    # Unqualified playback capability diagnostic check
    unqual_caps = AdapterCapabilities(
        adapter_id="unqual", adapter_version="1.0.0", model_revision="m1", metadata="separate_stream",
        speech_gating=True, cancel_generation=True, playback="unqualified", locales=["en"], report_hash="rh",
    )
    try:
        compile_flow(flow_dict, policy="p", capabilities=unqual_caps)
        assert False, "Should have failed compilation"
    except ValueError as exc:
        assert "PLAYBACK_UNQUALIFIED" in str(exc)


def _execute_contract_s5_stop_cancellation(compiled: Any, caps: Any) -> None:
    s5_state = initial_session_state(compiled, "sess_s5")
    stop_ev = UserStoppedEvent(
        event_id="ev_stop", session_id="sess_s5", transport_epoch="epoch_1",
        turn_id="t1", generation_id="g1", reason="user_stop",
    )
    red5 = reduce_event(compiled, s5_state, stop_ev)
    teardowns = [c for c in red5.commands if isinstance(c, TeardownCommand)]
    assert len(teardowns) == 1
    assert teardowns[0].outcome == "aborted"
    assert red5.state.outcome == "aborted"


def _execute_contract_s6_chat_durable_commit(compiled: Any, caps: Any) -> None:
    state_chat = initial_session_state(compiled, "sess_chat")
    state_voice = initial_session_state(compiled, "sess_voice")
    turn_chat = UserTurnAcceptedEvent(
        event_id="ev_tc", session_id="sess_chat", transport_epoch="epoch_1", turn_id="t1", generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    turn_voice = UserTurnAcceptedEvent(
        event_id="ev_tv", session_id="sess_voice", transport_epoch="epoch_1", turn_id="t1", generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    state_chat = reduce_event(compiled, state_chat, turn_chat).state
    state_voice = reduce_event(compiled, state_voice, turn_voice).state

    prop_c = StateProposal(
        proposal_id="prop_p", session_id="sess_chat", turn_id="t1", generation_id="g1", base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    prop_v = StateProposal(
        proposal_id="prop_p", session_id="sess_voice", turn_id="t1", generation_id="g1", base_state_version=1,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    red_chat = reduce_event(compiled, state_chat, StateProposalModelEvent(
        event_id="ev_pr_c", session_id="sess_chat", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop_c
    ))
    red_voice = reduce_event(compiled, state_voice, StateProposalModelEvent(
        event_id="ev_pr_v", session_id="sess_voice", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop_v
    ))
    manifest_c = next(c for c in red_chat.commands if isinstance(c, EmitSpeechManifestCommand))
    manifest_v = next(c for c in red_voice.commands if isinstance(c, EmitSpeechManifestCommand))
    state_chat = red_chat.state
    state_voice = red_voice.state
    for f_idx, frag in enumerate(manifest_c.fragments):
        state_chat = reduce_event(
            compiled, state_chat,
            ChatCommittedOutputEvent(
                event_id=f"ev_cc_{f_idx}", session_id="sess_chat", transport_epoch="epoch_1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_c.manifest_id, fragment_id=frag.fragment_id, message_id=frag.step_id,
                durable_delivery_id=f"durable_{f_idx}",
            )
        ).state
        state_voice = reduce_event(
            compiled, state_voice,
            FragmentPlayedOutputEvent(
                event_id=f"ev_vp_{f_idx}", session_id="sess_voice", transport_epoch="epoch_1", turn_id="t1", generation_id="g1",
                manifest_id=manifest_v.manifest_id, fragment_id=frag.fragment_id, content_hash=frag.content_hash,
                evidence_id=f"evid_{f_idx}", capability_report_hash="rep_hash",
            )
        ).state
    assert state_chat.fields["employment_type"].status == state_voice.fields["employment_type"].status == "confirmed"
    assert state_chat.fields["employment_type"].value == state_voice.fields["employment_type"].value == "salaried"
    assert state_chat.steps == state_voice.steps
    assert state_chat.active_step_id == state_voice.active_step_id == "end"


def _execute_contract_s7_full_path_matrix(compiled: Any, caps: Any, fixtures_dir: Path) -> None:
    s7_state = initial_session_state(compiled, "sess_s7")
    t1_s7 = UserTurnAcceptedEvent(
        event_id="e_s7_1", session_id="sess_s7", transport_epoch="epoch_1", turn_id="t1", generation_id="g1",
        text="I am salaried, earn INR 80000 monthly and want travel rewards.",
    )
    s7_state = reduce_event(compiled, s7_state, t1_s7).state
    prop_s7_1 = StateProposal(
        proposal_id="p_s7_1", session_id="sess_s7", turn_id="t1", generation_id="g1", base_state_version=s7_state.version,
        updates=[
            ProposeCaptureUpdate(field="employment_type", value="salaried", evidence=Evidence(source_turn_id="t1", quote="salaried")),
            ProposeCaptureUpdate(field="monthly_income_inr", value=80000, evidence=Evidence(source_turn_id="t1", quote="80000")),
            ProposeCaptureUpdate(field="card_preference", value="travel", evidence=Evidence(source_turn_id="t1", quote="travel")),
        ],
    )
    s7_state = reduce_event(compiled, s7_state, StateProposalModelEvent(
        event_id="e_s7_p1", session_id="sess_s7", transport_epoch="epoch_1", turn_id="t1", generation_id="g1", proposal=prop_s7_1
    )).state
    assert s7_state.active_step_id == "summary"

    # Correction invalidation
    t2_s7 = UserTurnAcceptedEvent(
        event_id="e_s7_2", session_id="sess_s7", transport_epoch="epoch_1", turn_id="t2", generation_id="g2",
        text="Actually I am self_employed.",
    )
    s7_state = reduce_event(compiled, s7_state, t2_s7).state
    prop_s7_2 = StateProposal(
        proposal_id="p_s7_2", session_id="sess_s7", turn_id="t2", generation_id="g2", base_state_version=s7_state.version,
        updates=[ProposeCaptureUpdate(field="employment_type", value="self_employed", evidence=Evidence(source_turn_id="t2", quote="self_employed"))],
    )
    s7_state = reduce_event(compiled, s7_state, StateProposalModelEvent(
        event_id="e_s7_p2", session_id="sess_s7", transport_epoch="epoch_1", turn_id="t2", generation_id="g2", proposal=prop_s7_2
    )).state
    assert s7_state.fields["employment_type"].value == "self_employed"
    assert s7_state.fields["monthly_income_inr"].applicability == "inapplicable"
    assert s7_state.fields["annual_income_inr"].applicability == "applicable"
    assert s7_state.active_step_id == "business_income"

    # Loan flow validation
    loan_file = fixtures_dir / "valid-loan-en-hi.json"
    if loan_file.exists():
        with open(loan_file, "r", encoding="utf-8") as f:
            loan_flow = json.load(f)["flow"]
        loan_compiled = compile_flow(loan_flow, policy="loan policy", capabilities=caps)
        loan_state = initial_session_state(loan_compiled, "sess_loan")
        assert loan_state.active_step_id == loan_flow["entry_step_id"]


def execute_contract_scenarios(
    flow: dict[str, Any] | None = None,
    model_obs: ProbeObservation | None = None,
    transport_res: TransportProbeResult | None = None,
) -> tuple[list[ContractScenarioResult], list[ScenarioResult], list[str], list[str]]:
    """Execute all seven required contract qualification scenarios per flow-contract §9.

    Replaces static NOT_RUN scaffolding with genuine executable scenario verification
    across the pure reducer boundary, while failing closed when real voice playback
    evidence (browser/physical audio render sink acknowledgments) or real model stream
    separation is missing. Reducer-only simulation does not qualify voice playback.
    """
    unsupported_reasons: list[str] = []
    missing_evidence: list[str] = []

    has_real_playback = bool(
        transport_res is not None
        and transport_res.supported
        and transport_res.playback_capability == "qualified_fragment_completion"
        and transport_res.evidence.has_audio_element_render_ack
        and transport_res.evidence.has_exact_fragment_binding
        and transport_res.evidence.has_exact_epoch_binding
        and transport_res.evidence.has_exact_hash_binding
    )
    if not has_real_playback:
        missing_evidence.append(
            "Real voice playback evidence missing: transport lacks verified bound audio-element render acknowledgments "
            "or physical audio sink capture; reducer simulation does not qualify voice playback."
        )

    has_real_model = bool(
        model_obs is not None
        and model_obs.connected
        and model_obs.metadata_strategy in ("separate_stream", "complete_tool")
        and model_obs.error_handling_verified
        and model_obs.server_cancellation_verified
        and not model_obs.unsupported_reasons
    )
    if not has_real_model:
        missing_evidence.append(
            "Real model qualification proof missing: streaming state proposal separation or server cancellation not proven."
        )

    fixtures_dir = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "flow"
    credit_card_file = fixtures_dir / "valid-credit-card-en-hi.json"

    card_flow: dict[str, Any]
    if flow is not None:
        card_flow = flow
    elif credit_card_file.exists():
        with open(credit_card_file, "r", encoding="utf-8") as f:
            card_flow = json.load(f)["flow"]
    else:
        card_flow = {
            "schema_version": 1,
            "entry_step_id": "start",
            "exit_step_id": "end",
            "steps": [
                {
                    "id": "start",
                    "title": "Start",
                    "type": "ask",
                    "required": True,
                    "priority": 1,
                    "prerequisites": [],
                    "instruction": "Confirm user",
                    "captures": [{"field": "confirmed", "kind": "boolean"}],
                    "transitions": [{"to": "end", "when": {"operator": "eq", "field": "confirmed", "value": True}}],
                },
                {
                    "id": "end",
                    "title": "End",
                    "type": "end",
                    "required": False,
                    "priority": 0,
                    "prerequisites": [],
                    "instruction": "End",
                    "captures": [],
                    "transitions": [],
                    "messages": [{"id": "closing", "en": "Goodbye.", "hi": "नमस्ते।"}],
                },
            ],
        }

    caps = AdapterCapabilities(
        adapter_id="qualification_harness",
        adapter_version="1.0.0",
        model_revision=model_obs.pinned_model_revision if model_obs and model_obs.pinned_model_revision else "pinned-test-v1",
        metadata="separate_stream" if (model_obs and model_obs.metadata_strategy == "separate_stream") else "separate_stream",
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        report_hash="rep_hash",
    )
    compiled = compile_flow(card_flow, policy="qualification policy", capabilities=caps)

    contract_results: list[ContractScenarioResult] = []
    scenario_results: list[ScenarioResult] = []

    for defn in REQUIRED_CONTRACT_SCENARIOS:
        t0 = time.perf_counter_ns()
        reducer_ok = False
        scenario_error: str | None = None
        try:
            if defn.number == 1:
                _execute_contract_s1_normal_en_hi(compiled, caps)
            elif defn.number == 2:
                _execute_contract_s2_adversarial_gating(compiled, caps)
            elif defn.number == 3:
                _execute_contract_s3_barge_in(compiled, caps)
            elif defn.number == 4:
                _execute_contract_s4_playback_faults(compiled, caps, card_flow)
            elif defn.number == 5:
                _execute_contract_s5_stop_cancellation(compiled, caps)
            elif defn.number == 6:
                _execute_contract_s6_chat_durable_commit(compiled, caps)
            elif defn.number == 7:
                _execute_contract_s7_full_path_matrix(compiled, caps, fixtures_dir)
            reducer_ok = True
        except Exception as exc:
            scenario_error = f"Reducer verification failed: {exc}"

        elapsed_us = (time.perf_counter_ns() - t0) // 1000
        canonical_meas = {"reducer_duration_us": canonicalize_measurement(elapsed_us)}

        if reducer_ok:
            # Reducer orchestration is not voice qualification. Even when the
            # model/transport observations look clean, no scenario has produced
            # per-scenario rendered-audio evidence, so the scenario stays
            # unsupported rather than being promoted to PASSED by a flag.
            scenario_passed = False
            scenario_status = "UNSUPPORTED"
            gap_reasons = []
            if not has_real_playback:
                gap_reasons.append("audio render sink evidence (element render ack) missing")
            if not has_real_model:
                gap_reasons.append("real model stream separation / cancellation proof missing")
            gap_reasons.append(
                "no per-scenario real audio/render evidence was executed"
            )
            scenario_msg = (
                f"Scenario {defn.number} ({defn.name}): Executable reducer scenario verified, "
                f"but real voice playback qualification is unsupported: {'; '.join(gap_reasons)}; "
                "reducer-only simulation does not qualify voice playback."
            )
        else:
            scenario_passed = False
            scenario_status = "FAILED"
            scenario_msg = scenario_error or "Reducer verification failed"

        contract_results.append(
            ContractScenarioResult(
                scenario_number=defn.number,
                name=defn.name,
                contract_description=defn.contract_description,
                scenario_class=defn.scenario_class,
                passed=scenario_passed,
                status=scenario_status,
                error_message=scenario_msg,
                measurements=canonical_meas,
                trace_id=f"trace_sc_{defn.number}",
            )
        )
        scenario_results.append(
            ScenarioResult(
                scenario_id=f"required_{defn.scenario_class}",
                scenario_class=defn.scenario_class,
                passed=scenario_passed,
                measurements=canonical_meas,
                raw_evidence={
                    "reducer_verified": reducer_ok,
                    "has_real_playback": has_real_playback,
                    "has_real_model": has_real_model,
                },
                trace_id=f"trace_sc_{defn.number}",
                error_message=scenario_msg if not scenario_passed else None,
            )
        )

    return contract_results, scenario_results, unsupported_reasons, missing_evidence


def build_contract_scenario_results(
    reasons: list[str] | None = None,
    status: Literal["NOT_RUN", "FAILED", "PASSED", "UNSUPPORTED"] = "NOT_RUN",
) -> list[ContractScenarioResult]:
    """Enumerate the exact seven contract qualification scenarios as NOT_RUN."""
    default_msg = (
        "NOT RUN / UNSUPPORTED: Independent real end-to-end voice scenario runner "
        "and physical/browser audio render sink evidence not available; reducer-only "
        "simulations do not qualify voice playback."
    )
    msg = "; ".join(reasons) if reasons else default_msg
    return [
        ContractScenarioResult(
            scenario_number=defn.number,
            name=defn.name,
            contract_description=defn.contract_description,
            scenario_class=defn.scenario_class,
            passed=False,
            status=status,
            error_message=f"Scenario {defn.number} ({defn.name}): {msg}",
            measurements={},
            trace_id="",
        )
        for defn in REQUIRED_CONTRACT_SCENARIOS
    ]


def build_unexecuted_scenarios(
    reasons: list[str] | None = None,
) -> list[ScenarioResult]:
    """Enumerate all seven required scenario classes as unexecuted / unsupported.

    Reducer-only simulations or mock passes do not constitute voice qualification.
    Without an independent real scenario runner and audio capture/render evidence,
    each required scenario class must be recorded as passed=False with an explicit
    error message.
    """
    default_msg = (
        "NOT RUN / UNSUPPORTED: Independent real end-to-end voice scenario runner "
        "and physical/browser audio render sink evidence not available; reducer-only "
        "simulations do not qualify voice playback."
    )
    msg = "; ".join(reasons) if reasons else default_msg
    return [
        ScenarioResult(
            scenario_id=f"required_{sc_class}",
            scenario_class=sc_class,
            passed=False,
            measurements={},
            trace_id="",
            error_message=f"{sc_class}: {msg}",
        )
        for sc_class in REQUIRED_SCENARIO_CLASSES
    ]

async def run_qualification(
    model_endpoint: str | None = None,
    model_name: str | None = None,
    transport_endpoint: str | None = None,
    source_revision: str | None = None,
    profile_hash: str | None = None,
    profile_path: str | None = None,
    flow: dict[str, Any] | None = None,
    extra_unsupported: list[str] | None = None,
    timeout_seconds: float = 10.0,
) -> QualificationReport:
    """Execute real model and live transport probes with explicit parameters.

    Never defaults user or development ports (e.g. 7860 or 11434) implicitly.
    If endpoints or revisions are missing, fails closed with explicit unsupported
    reasons.
    """
    unsupported_reasons: list[str] = []
    missing_evidence: list[str] = []

    if extra_unsupported:
        unsupported_reasons.extend(extra_unsupported)
        missing_evidence.extend(extra_unsupported)

    # 1. Flow runtime verification: exercise pure schemas, validation, compilation, and reducer
    flow_runtime_verified = False
    try:
        limits = FlowLimits()
        sanity_flow = flow or {
            "schema_version": 1,
            "entry_step_id": "start",
            "exit_step_id": "end",
            "steps": [
                {
                    "id": "start",
                    "title": "Start",
                    "type": "ask",
                    "required": True,
                    "priority": 1,
                    "prerequisites": [],
                    "instruction": "Ask user for confirmation",
                    "captures": [
                        {
                            "field": "confirmed",
                            "kind": "boolean",
                        }
                    ],
                    "transitions": [
                        {
                            "to": "end",
                            "when": {
                                "operator": "eq",
                                "field": "confirmed",
                                "value": True,
                            },
                        }
                    ],
                },
                {
                    "id": "end",
                    "title": "End",
                    "type": "end",
                    "required": False,
                    "priority": 0,
                    "prerequisites": [],
                    "instruction": "End of conversation",
                    "captures": [],
                    "transitions": [],
                    "messages": [
                        {
                            "id": "closing",
                            "en": "Goodbye.",
                            "hi": "नमस्ते।",
                        }
                    ],
                },
            ],
        }
        val_diags = validate_executable(sanity_flow, limits=limits, capabilities=None)
        val_errors = [d for d in val_diags if d.severity == "error"]
        if val_errors:
            unsupported_reasons.append(
                f"Flow runtime validation error: {'; '.join(d.message for d in val_errors)}"
            )
            missing_evidence.append("Flow runtime validation failed on executable flow schema.")
        else:
            compiled = compile_flow(
                sanity_flow,
                policy="diagnostic test policy",
                limits=limits,
                capabilities=None,
            )
            state = initial_session_state(compiled, "diag_session")
            turn_ev = UserTurnAcceptedEvent(
                event_id="ev_diag_turn",
                session_id="diag_session",
                transport_epoch="epoch_1",
                turn_id="t1",
                generation_id="g1",
                text="yes",
            )
            reduction = reduce_event(compiled, state, turn_ev)
            if reduction.state.version >= 1:
                flow_runtime_verified = True
    except Exception as exc:
        unsupported_reasons.append(f"Flow runtime dependency or execution error: {exc}")
        missing_evidence.append(f"Flow runtime execution failed: {exc}")
    # Check for explicit parameters
    if not model_endpoint or not model_endpoint.strip():
        unsupported_reasons.append(
            "Model endpoint was not explicitly specified; caller must provide an explicit "
            "model endpoint URL (e.g. http://127.0.0.1:11434/v1). Refusing to connect to any unconfigured default."
        )
    if not model_name or not model_name.strip():
        unsupported_reasons.append("Model name was not explicitly specified.")
    if not transport_endpoint or not transport_endpoint.strip():
        unsupported_reasons.append(
            "Transport endpoint was not explicitly specified; caller must provide an explicit "
            "WebRTC loopback endpoint URL. Refusing to connect to port 7860 or any unconfigured default."
        )
    if not source_revision or not source_revision.strip():
        unsupported_reasons.append("Source revision was not explicitly specified.")
    if not profile_hash or not profile_hash.strip():
        unsupported_reasons.append("Profile hash was not explicitly specified.")

    model_obs: ProbeObservation | None = None
    if model_endpoint and model_name:
        is_loopback, loopback_err = is_literal_loopback_endpoint(model_endpoint)
        if not is_loopback:
            unsupported_reasons.append(
                f"Model endpoint rejected by air-gapped policy: {loopback_err}"
            )
            missing_evidence.append(
                f"Model probe skipped due to non-loopback endpoint: {model_endpoint}"
            )
        else:
            try:
                model_obs = await probe_model_qualification(
                    base_url=model_endpoint.strip(),
                    model=model_name.strip(),
                    timeout=timeout_seconds,
                )
                if model_obs.unsupported_reasons:
                    unsupported_reasons.extend(
                        f"Model probe: {r}" for r in model_obs.unsupported_reasons
                    )
                if not model_obs.connected:
                    missing_evidence.append("Model connection could not be established.")
                if not model_obs.revision_pinned:
                    unsupported_reasons.append(
                        "Model probe did not verify a full sha256 model revision; "
                        "the tuple cannot be advertised as pinned."
                    )
                if model_obs.metadata_strategy == "none":
                    missing_evidence.append("Model endpoint does not support typed state proposal separation.")
                if not model_obs.server_cancellation_verified:
                    missing_evidence.append("Model server-side stream cancellation could not be verified.")
            except Exception as exc:
                unsupported_reasons.append(f"Model probe execution failed: {exc}")
                missing_evidence.append(f"Model probe failure: {exc}")
    else:
        missing_evidence.append("Real model probe not executed due to missing endpoint/name configuration.")

    transport_res: TransportProbeResult | None = None
    if transport_endpoint and source_revision and profile_hash:
        is_loopback, loopback_err = is_literal_loopback_endpoint(transport_endpoint)
        if not is_loopback:
            unsupported_reasons.append(
                f"Transport endpoint rejected by air-gapped policy: {loopback_err}"
            )
            missing_evidence.append(
                f"Transport probe skipped due to non-loopback endpoint: {transport_endpoint}"
            )
        else:
            try:
                transport_res = await probe_live_webrtc_loopback(
                    base_url=transport_endpoint.strip(),
                    source_revision=source_revision.strip(),
                    profile_hash=profile_hash.strip(),
                    metadata_strategy=(
                        model_obs.metadata_strategy if model_obs is not None else "none"
                    ),
                    speech_gating=(
                        (model_obs.supports_separate_stream or model_obs.supports_complete_tool)
                        if model_obs is not None
                        else False
                    ),
                    cancel_generation=(
                        model_obs.server_cancellation_verified
                        if model_obs is not None
                        else False
                    ),
                    timeout_seconds=timeout_seconds,
                )
                if transport_res.evidence.unsupported_reasons:
                    unsupported_reasons.extend(
                        f"Transport probe: {r}" for r in transport_res.evidence.unsupported_reasons
                    )
                if not transport_res.supported:
                    missing_evidence.append("Transport probe indicated unsupported transport capabilities.")
                if transport_res.playback_capability != "qualified_fragment_completion":
                    missing_evidence.append(
                        f"Playback capability is '{transport_res.playback_capability}'; "
                        "requires 'qualified_fragment_completion'."
                    )
            except Exception as exc:
                unsupported_reasons.append(f"Transport probe execution failed: {exc}")
                missing_evidence.append(f"Transport probe failure: {exc}")
    else:
        missing_evidence.append("Real transport probe not executed due to missing endpoint/revision configuration.")
    # Execute all 7 required qualification scenarios across the pure reducer boundary
    contract_scenarios, scenarios, sc_unsupported, sc_missing = execute_contract_scenarios(
        flow=flow,
        model_obs=model_obs,
        transport_res=transport_res,
    )
    unsupported_reasons.extend(sc_unsupported)
    missing_evidence.extend(sc_missing)

    transport_obs_dict: dict[str, Any] | None = None
    capability_rep: CapabilityReport | None = None
    if transport_res is not None:
        transport_obs_dict = {
            "supported": transport_res.supported,
            "playback_capability": transport_res.playback_capability,
            "has_audio_out_track": transport_res.evidence.has_audio_out_track,
            "has_audio_element_render_ack": transport_res.evidence.has_audio_element_render_ack,
            "has_exact_fragment_binding": transport_res.evidence.has_exact_fragment_binding,
            "has_exact_epoch_binding": transport_res.evidence.has_exact_epoch_binding,
            "has_exact_hash_binding": transport_res.evidence.has_exact_hash_binding,
            "observed_latency_ms": (
                canonicalize_measurement(transport_res.evidence.observed_latency_ms)
                if transport_res.evidence.observed_latency_ms is not None
                else None
            ),
            "unsupported_reasons": transport_res.evidence.unsupported_reasons,
        }
        capability_rep = transport_res.report
        if capability_rep is not None:
            try:
                rep_diags = validate_capability_report(capability_rep.model_dump(mode="json"))
                for diag in rep_diags:
                    if diag.severity == "error":
                        unsupported_reasons.append(f"Capability report diagnostic: {diag.message}")
            except Exception as exc:
                unsupported_reasons.append(f"Capability report validation error: {exc}")

    has_real_playback = bool(
        transport_res is not None
        and transport_res.supported
        and transport_res.playback_capability == "qualified_fragment_completion"
        and transport_res.evidence.has_audio_element_render_ack
        and transport_res.evidence.has_exact_fragment_binding
        and transport_res.evidence.has_exact_epoch_binding
        and transport_res.evidence.has_exact_hash_binding
    )
    has_real_model = bool(
        model_obs is not None
        and model_obs.connected
        and model_obs.metadata_strategy in ("separate_stream", "complete_tool")
        and model_obs.error_handling_verified
        and model_obs.server_cancellation_verified
        and not model_obs.unsupported_reasons
    )

    all_passed = bool(
        contract_scenarios
        and all(c.passed for c in contract_scenarios)
        and flow_runtime_verified
        and has_real_playback
        and has_real_model
    )
    overall_status: Literal["supported", "unsupported"] = "supported" if all_passed else "unsupported"
    task_2_complete = all_passed

    # Record every probed tuple explicitly. A tuple is advertised only when all
    # seven required scenario classes passed for it; a diagnostic probe alone
    # never populates this list.
    unsupported_configurations: list[dict[str, Any]] = []
    scenario_gap = "All seven required qualification scenarios have not been executed."

    if model_obs is not None:
        model_reasons = list(model_obs.unsupported_reasons)
        if not model_obs.connected:
            model_reasons.append("Model connection could not be established.")
        if not model_obs.revision_pinned:
            model_reasons.append("Model revision is not pinned to a full sha256 digest.")
        if model_obs.metadata_strategy == "none":
            model_reasons.append("No verified typed state-proposal separation.")
        if not model_obs.server_cancellation_verified:
            model_reasons.append("Server-side generation cancellation was not verified.")
        if not has_real_playback or not has_real_model:
            model_reasons.append(scenario_gap)
        if not all_passed:
            unsupported_configurations.append(
                {
                    "kind": "model",
                    "endpoint": model_obs.base_url,
                    "model": model_obs.configured_model,
                    "model_revision": model_obs.pinned_model_revision,
                    "metadata": model_obs.metadata_strategy,
                    "speech_gating": (
                        model_obs.supports_separate_stream
                        or model_obs.supports_complete_tool
                    ),
                    "cancel_generation": model_obs.server_cancellation_verified,
                    "advertised": False,
                    "reasons": model_reasons,
                }
            )

    if transport_res is not None and not all_passed:
        transport_reasons = list(transport_res.evidence.unsupported_reasons)
        transport_reasons.append(scenario_gap)
        unsupported_configurations.append(
            {
                "kind": "transport",
                "endpoint": transport_endpoint,
                "adapter_id": transport_res.evidence.adapter_id,
                "playback": transport_res.playback_capability,
                "advertised": False,
                "reasons": transport_reasons,
            }
        )

    advertised_configurations: list[dict[str, Any]] = []
    if all_passed:
        # This branch is unreachable while the transport probe reports
        # unqualified playback; keep the shape explicit for a future real
        # scenario runner that can actually satisfy the gate.
        advertised_configurations.append(
            {
                "kind": "model+transport",
                "model": model_obs.configured_model if model_obs else None,
                "model_revision": (
                    model_obs.pinned_model_revision if model_obs else None
                ),
                "adapter_id": (
                    transport_res.evidence.adapter_id if transport_res else None
                ),
                "playback": (
                    transport_res.playback_capability if transport_res else None
                ),
            }
        )

    if all_passed:
        summary = "All 7 contract qualification scenarios passed with verified real model and playback evidence."
    else:
        summary = (
            "UNSUPPORTED: Task 2 is incomplete. Real qualification requires independent "
            "empirical evidence for model protocol separation, bound physical/browser transport "
            "rendering acknowledgements, and all 7 executed scenario classes defined in flow-contract.md §9. "
            "Reducer-only simulations or mock passes do not satisfy voice qualification. "
            "No configuration is advertised."
        )

    return QualificationReport(
        overall_status=overall_status,
        task_2_complete=task_2_complete,
        status_summary=summary,
        qualification_scope=(
            "reducer_orchestration_only" if flow_runtime_verified else "none"
        ),
        model_observation=model_obs,
        transport_observation=transport_obs_dict,
        capability_report=capability_rep,
        contract_scenarios=contract_scenarios,
        scenarios=scenarios,
        advertised_configurations=advertised_configurations,
        unsupported_configurations=unsupported_configurations,
        unsupported_reasons=unsupported_reasons,
        missing_evidence=missing_evidence,
        flow_runtime_verified=flow_runtime_verified,
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Factual qualification runner diagnostic entrypoint."
    )
    parser.add_argument(
        "--model-endpoint",
        type=str,
        default=None,
        help="Explicit base URL for the model probe (e.g. http://127.0.0.1:11434/v1). Never defaulted.",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default=None,
        help="Explicit model name to inspect and probe. Never defaulted.",
    )
    parser.add_argument(
        "--transport-endpoint",
        type=str,
        default=None,
        help="Explicit WebRTC loopback endpoint URL. Never defaulted.",
    )
    parser.add_argument(
        "--source-revision",
        type=str,
        default=None,
        help="Explicit source code revision identifier.",
    )
    parser.add_argument(
        "--profile-hash",
        type=str,
        default=None,
        help="Explicit qualification profile hash.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Optional path to versioned qualification profile JSON configuration.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds for probes.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path to write JSON diagnostic report.",
    )
    return parser.parse_args(args)


async def main_async(args: list[str] | None = None) -> int:
    parsed = parse_args(args)

    model_endpoint = parsed.model_endpoint
    model_name = parsed.model_name
    transport_endpoint = parsed.transport_endpoint
    source_revision = parsed.source_revision
    profile_hash = parsed.profile_hash
    profile_path = parsed.profile

    extra_unsupported: list[str] = []
    if profile_path:
        if not os.path.exists(profile_path):
            extra_unsupported.append(f"Qualification profile file not found: {profile_path}")
        else:
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    profile_data = json.load(f)
                if not profile_hash:
                    profile_hash = hashlib.sha256(
                        b"voice-ai/qualification-profile/v1\0" + canonical_json_v1(profile_data)
                    ).hexdigest()
                if not model_endpoint and "model_endpoint" in profile_data:
                    model_endpoint = profile_data["model_endpoint"]
                if not model_name and "model_name" in profile_data:
                    model_name = profile_data["model_name"]
                if not transport_endpoint and "transport_endpoint" in profile_data:
                    transport_endpoint = profile_data["transport_endpoint"]
                if not source_revision and "source_revision" in profile_data:
                    source_revision = profile_data["source_revision"]
            except Exception as exc:
                extra_unsupported.append(f"Failed to read qualification profile file: {exc}")

    report = await run_qualification(
        model_endpoint=model_endpoint,
        model_name=model_name,
        transport_endpoint=transport_endpoint,
        source_revision=source_revision,
        profile_hash=profile_hash,
        profile_path=profile_path,
        extra_unsupported=extra_unsupported if extra_unsupported else None,
        timeout_seconds=parsed.timeout,
    )

    report_json = report.to_json()
    if parsed.output:
        os.makedirs(os.path.dirname(os.path.abspath(parsed.output)), exist_ok=True)
        with open(parsed.output, "w", encoding="utf-8") as f:
            f.write(report_json)
    else:
        print(report_json)

    # Return nonzero (1) when unsupported / incomplete
    if report.overall_status != "supported" or not report.task_2_complete:
        return 1
    return 0


def main() -> None:
    code = asyncio.run(main_async(sys.argv[1:]))
    sys.exit(code)


if __name__ == "__main__":
    main()
