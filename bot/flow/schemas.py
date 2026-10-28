"""Strict authoring and executable schemas, condition models, events, diagnostics, and limits.

Per docs/first-release/flow-contract.md:
- Provider-neutral, pure representations.
- Rejects unknown keys at trust boundaries.
- No database, network, or provider imports.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

Scalar = Union[str, int, bool]
Applicability = Literal["applicable", "inapplicable", "unknown"]
StepStatus = Literal["pending", "active", "complete", "waived", "blocked"]
Outcome = Literal["in_progress", "completed", "partial", "aborted", "failed"]
CandidateReason = Literal[
    "ambiguous",
    "needs_confirmation",
    "uncertain_evidence",
    "dependency_unknown",
]
OriginSource = Literal["user_capture", "user_confirmation", "tool_result"]
DiagnosticSeverity = Literal["error", "warning"]

MIN_SAFE_INTEGER = -9007199254740991
MAX_SAFE_INTEGER = 9007199254740991


class FlowLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = 128
    max_total_captured_fields: int = 128
    max_captures_per_node: int = 32
    max_transitions_per_node: int = 32
    max_prerequisites_per_node: int = 16
    max_messages_per_node: int = 32
    max_enum_values: int = 64
    max_id_chars: int = 64
    max_title_chars: int = 128
    max_scalar_chars: int = 8192
    max_capture_value_chars: int = 2048
    max_tool_mappings: int = 32
    max_clarification_attempts: int = 5
    max_flow_bytes: int = 262144
    min_safe_integer: int = MIN_SAFE_INTEGER
    max_safe_integer: int = MAX_SAFE_INTEGER

    # Runtime limits
    max_turns: int = 100
    max_detours_per_objective: int = 3
    max_context_turns: int = 20
    max_context_utf8_bytes: int = 65536
    max_proposal_bytes: int = 16384
    max_field_updates_per_proposal: int = 32
    max_speech_fragment_chars: int = 1024
    max_generation_fragments: int = 128
    max_pending_speech_bytes: int = 65536
    max_adapter_events_per_generation: int = 1024


class FlowDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    path: str
    message: str
    severity: DiagnosticSeverity = "error"

    def sort_key(self) -> tuple[str, str, str]:
        return (self.path, self.code, self.message)

    def __lt__(self, other: FlowDiagnostic) -> bool:
        return self.sort_key() < other.sort_key()


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_turn_id: str
    quote: str


class Origin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_turn_id: str
    generation_id: str
    committed_state_version: int
    source: OriginSource
    execution_id: str | None = None


# Field states per flow-contract.md line 91-97
class UnknownFieldState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["unknown"] = "unknown"
    applicability: Applicability
    invalidated_by: str | None = None


class CandidateFieldState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["candidate"] = "candidate"
    applicability: Applicability
    value: Scalar | None
    reason: CandidateReason
    evidence: Evidence
    candidate_state_version: int = 0


class ConfirmedFieldState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["confirmed"] = "confirmed"
    applicability: Literal["applicable"] = "applicable"
    value: Scalar
    origin: Origin


class RefusedFieldState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["refused"] = "refused"
    applicability: Applicability
    origin: Origin


FieldState = Annotated[
    Union[
        UnknownFieldState,
        CandidateFieldState,
        ConfirmedFieldState,
        RefusedFieldState,
    ],
    Field(discriminator="status"),
]


# Capture updates per flow-contract.md line 98-103
class ProposeCaptureUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["propose"] = "propose"
    field: str
    value: Scalar
    evidence: Evidence


class CandidateCaptureUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["candidate"] = "candidate"
    field: str
    value: Scalar | None = None
    reason: CandidateReason
    evidence: Evidence


class RefuseCaptureUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["refuse"] = "refuse"
    field: str
    evidence: Evidence


class ConfirmCaptureUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["confirm"] = "confirm"
    field: str
    candidate_state_version: int
    evidence: Evidence


CaptureUpdate = Annotated[
    Union[
        ProposeCaptureUpdate,
        CandidateCaptureUpdate,
        RefuseCaptureUpdate,
        ConfirmCaptureUpdate,
    ],
    Field(discriminator="op"),
]

class StateProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    proposal_id: str
    session_id: str
    turn_id: str
    generation_id: str
    base_state_version: int
    updates: list[CaptureUpdate]


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., min_length=1)
    transport_epoch: str | None = None
    version: int
    flow_hash: str
    artifact_hash: str
    last_accepted_turn_id: str | None = None
    generation_id: str | None = None
    fields: dict[str, FieldState] = Field(default_factory=dict)
    steps: dict[str, StepStatus] = Field(default_factory=dict)
    active_step_id: str | None = None
    resume_step_id: str | None = None
    locale: Literal["en", "hi"] = "en"
    outcome: Outcome = "in_progress"
    teardown_emitted: bool = False
    clarification_attempts: dict[str, int] = Field(default_factory=dict)
    detour_counts: dict[str, int] = Field(default_factory=dict)
    offer_exit_pending: dict[str, bool] = Field(default_factory=dict)
    continue_reset_used: dict[str, bool] = Field(default_factory=dict)
    history_spoken_fragments: list[str] = Field(default_factory=list)
    turn_history: list[dict[str, Any]] = Field(default_factory=list)
    committed_proposal_ids: list[str] = Field(default_factory=list)
    active_manifest_id: str | None = None
    active_manifest_fragments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    delivered_fragment_ids: list[str] = Field(default_factory=list)
    delivered_semantic_fragment_ids: list[str] = Field(default_factory=list)
    delivered_part_fingerprints: dict[str, str] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("session_id must be a non-empty string")
        return v.strip()


# Events
class EventKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    session_id: str
    transport_epoch: str
    turn_id: str
    generation_id: str


class SpeechDeltaModelEvent(EventKey):
    type: Literal["speech_delta"] = "speech_delta"
    seq: int
    text: str


class StateProposalModelEvent(EventKey):
    type: Literal["state_proposal"] = "state_proposal"
    proposal: StateProposal


class GenerationSealedModelEvent(EventKey):
    type: Literal["generation_sealed"] = "generation_sealed"
    last_seq: int


class GenerationFailedModelEvent(EventKey):
    type: Literal["generation_failed"] = "generation_failed"
    code: str


ModelEvent = Annotated[
    Union[
        SpeechDeltaModelEvent,
        StateProposalModelEvent,
        GenerationSealedModelEvent,
        GenerationFailedModelEvent,
    ],
    Field(discriminator="type"),
]


class SpeechFragment(EventKey):
    fragment_id: str
    semantic_fragment_id: str
    step_id: str
    state_version: int
    manifest_id: str
    content_hash: str
    locale: Literal["en", "hi"]
    text: str
    required: bool = True


class FragmentGeneratedOutputEvent(EventKey):
    type: Literal["fragment_generated"] = "fragment_generated"
    fragment: SpeechFragment


class FragmentQueuedOutputEvent(EventKey):
    type: Literal["fragment_queued"] = "fragment_queued"
    manifest_id: str
    fragment_id: str


class FragmentPlayedOutputEvent(EventKey):
    type: Literal["fragment_played"] = "fragment_played"
    manifest_id: str
    fragment_id: str
    content_hash: str
    evidence_id: str
    capability_report_hash: str


class ChatCommittedOutputEvent(EventKey):
    type: Literal["chat_committed"] = "chat_committed"
    manifest_id: str
    fragment_id: str
    message_id: str
    durable_delivery_id: str


class OutputCancelledOutputEvent(EventKey):
    type: Literal["output_cancelled"] = "output_cancelled"
    cancellation_id: str
    discarded_fragment_ids: list[str] = Field(default_factory=list)


OutputEvent = Annotated[
    Union[
        FragmentGeneratedOutputEvent,
        FragmentQueuedOutputEvent,
        FragmentPlayedOutputEvent,
        ChatCommittedOutputEvent,
        OutputCancelledOutputEvent,
    ],
    Field(discriminator="type"),
]


class CancelCommand(EventKey):
    type: Literal["cancel_generation"] = "cancel_generation"
    cancellation_id: str
    reason: Literal["barge_in", "user_stop", "credit", "safety", "timeout", "disconnect"]


class UserTurnAcceptedEvent(EventKey):
    type: Literal["user_turn_accepted"] = "user_turn_accepted"
    text: str
    locale: Literal["en", "hi"] | None = None


class LocaleChangedEvent(EventKey):
    type: Literal["locale_changed"] = "locale_changed"
    locale: Literal["en", "hi"]


class UserStoppedEvent(EventKey):
    type: Literal["user_stopped"] = "user_stopped"
    reason: str = "user_stop"


class ActionCompletedEvent(EventKey):
    type: Literal["action_completed"] = "action_completed"
    execution_id: str
    tool_id: str
    result: dict[str, Any]
    result_hash: str


class ActionFailedEvent(EventKey):
    type: Literal["action_failed"] = "action_failed"
    execution_id: str
    tool_id: str
    error_code: str
    message: str


class SessionTimeoutEvent(EventKey):
    type: Literal["session_timeout"] = "session_timeout"
    reason: str = "timeout"


class SessionTerminatedEvent(EventKey):
    type: Literal["session_terminated"] = "session_terminated"
    reason: str = "shutdown"


FlowEvent = Annotated[
    Union[
        UserTurnAcceptedEvent,
        LocaleChangedEvent,
        UserStoppedEvent,
        SpeechDeltaModelEvent,
        StateProposalModelEvent,
        GenerationSealedModelEvent,
        GenerationFailedModelEvent,
        FragmentGeneratedOutputEvent,
        FragmentQueuedOutputEvent,
        FragmentPlayedOutputEvent,
        ChatCommittedOutputEvent,
        OutputCancelledOutputEvent,
        ActionCompletedEvent,
        ActionFailedEvent,
        CancelCommand,
        SessionTimeoutEvent,
        SessionTerminatedEvent,
    ],
    Field(discriminator="type"),
]


class AdapterCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter_id: str
    adapter_version: str
    model_revision: str
    metadata: Literal["separate_stream", "complete_tool", "none"]
    speech_gating: bool
    cancel_generation: bool
    playback: Literal["qualified_fragment_completion", "unqualified"]
    locales: list[Literal["en", "hi"]]
    report_hash: str

# Reducer commands
class CancelGenerationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["cancel_generation"] = "cancel_generation"
    cancellation_id: str
    reason: str


class EmitSpeechManifestCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["emit_speech_manifest"] = "emit_speech_manifest"
    manifest_id: str
    step_id: str
    fragments: list[SpeechFragment]


class RequestActionCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["request_action"] = "request_action"
    execution_id: str
    tool_contract_hash: str
    input_hash: str
    validated_arguments: dict[str, Any]
    confirmation_record_id: str | None = None


class TeardownCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["teardown"] = "teardown"
    outcome: Outcome
    reason: str


class RequestClarificationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["request_clarification"] = "request_clarification"
    field_id: str
    step_id: str
    reason: str


FlowCommand = Annotated[
    Union[
        CancelGenerationCommand,
        EmitSpeechManifestCommand,
        RequestActionCommand,
        TeardownCommand,
        RequestClarificationCommand,
    ],
    Field(discriminator="type"),
]


class Reduction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: SessionState
    commands: list[FlowCommand] = Field(default_factory=list)
    diagnostics: list[FlowDiagnostic] = Field(default_factory=list)


class CompiledFlow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized: dict[str, Any]
    manifest: dict[str, Any]
    flow_hash: str
    artifact_hash: str
    limits: FlowLimits = Field(default_factory=FlowLimits)
