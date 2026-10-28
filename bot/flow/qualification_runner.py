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
    model_observation: ProbeObservation | None = None
    transport_observation: dict[str, Any] | None = None
    capability_report: CapabilityReport | None = None
    contract_scenarios: list[ContractScenarioResult] = Field(default_factory=list)
    scenarios: list[ScenarioResult] = Field(default_factory=list)
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

def build_contract_scenario_results(
    reasons: list[str] | None = None,
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
            status="NOT_RUN",
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
    unsupported_reasons.append(
        "Missing real scenario runner: real end-to-end scenario runner is not implemented; "
        "cannot execute required voice qualification scenarios."
    )
    missing_evidence.append(
        "All seven required scenario classes lack independent empirical execution."
    )

    # Contract scenarios and core scenario results: enumerate all seven as NOT RUN / unsupported
    contract_scenarios = build_contract_scenario_results(
        reasons=["Missing real scenario runner; reducer-only simulations are not voice qualification."]
    )
    scenarios = build_unexecuted_scenarios(
        reasons=["Missing real scenario runner; reducer-only simulations are not voice qualification."]
    )

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

    # Always fail closed: overall_status is unsupported unless all required evidence exists
    # And Task 2 is explicitly incomplete.
    summary = (
        "UNSUPPORTED: Task 2 is incomplete. Real qualification requires independent "
        "empirical evidence for model protocol separation, bound physical/browser transport "
        "rendering acknowledgements, and all 7 executed scenario classes defined in flow-contract.md §9. "
        "Reducer-only simulations or mock passes do not satisfy voice qualification."
    )

    return QualificationReport(
        overall_status="unsupported",
        task_2_complete=False,
        status_summary=summary,
        model_observation=model_obs,
        transport_observation=transport_obs_dict,
        capability_report=capability_rep,
        contract_scenarios=contract_scenarios,
        scenarios=scenarios,
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
