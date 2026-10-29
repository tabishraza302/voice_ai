"""Qualification harness contracts and report validation per flow contract section 9.

Validates immutable qualification reports:
- protocol_version: 2
- Exact deployment and agent settings tuple
- Required scenarios executed
- Verified actual latency/resource measurements
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.flow.canonical import canonical_json_v1
from bot.flow.schemas import FlowDiagnostic

ScenarioClass = Literal[
    "normal_en_hi",
    "barge_in",
    "correction",
    "refusal",
    "packet_loss",
    "model_failure",
    "full_path_matrix",
]

REQUIRED_SCENARIO_CLASSES: tuple[ScenarioClass, ...] = (
    "normal_en_hi",
    "barge_in",
    "correction",
    "refusal",
    "packet_loss",
    "model_failure",
    "full_path_matrix",
)


class ScenarioResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    scenario_id: str
    scenario_class: ScenarioClass
    passed: bool
    measurements: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    error_message: str | None = None


class AgentQualificationSettings(BaseModel):
    """Exact model, STT, and voice settings covered by a deployment report."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    llm_model: str = Field(min_length=1, max_length=64)
    stt_provider: str = Field(min_length=1, max_length=32)
    tts_voice_en: str = Field(min_length=1, max_length=32)
    tts_voice_hi: str = Field(min_length=1, max_length=32)


class CapabilityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    protocol_version: int = 2
    adapter_id: str
    adapter_version: str
    model_revision: str
    agent_settings: AgentQualificationSettings
    metadata: Literal["separate_stream", "complete_tool", "none"]
    speech_gating: bool
    cancel_generation: bool
    playback: Literal["qualified_fragment_completion", "unqualified"]
    locales: list[Literal["en", "hi"]]
    profile_hash: str
    source_revision: str
    fixture_hashes: dict[str, str] = Field(default_factory=dict)
    cases: list[ScenarioResult] = Field(default_factory=list)
    measurements: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    raw_evidence_references: list[str] = Field(default_factory=list)
    playback_boundary_description: str
    limitations: list[str] = Field(default_factory=list)
    report_hash: str = ""

    def compute_hash(self) -> str:
        dump = self.model_dump()
        dump.pop("report_hash", None)
        return hashlib.sha256(
            b"voice-ai/capability-report/v2\0" + canonical_json_v1(dump)
        ).hexdigest()


def validate_capability_report(report_data: dict[str, Any]) -> list[FlowDiagnostic]:
    """Validate qualification report format, required scenarios, and report hash."""
    diagnostics: list[FlowDiagnostic] = []

    if (
        type(report_data.get("protocol_version")) is not int
        or report_data.get("protocol_version") != 2
    ):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/protocol_version",
                message="Qualification report must have protocol_version: 2",
            )
        )

    for req_field in (
        "adapter_id",
        "adapter_version",
        "model_revision",
        "agent_settings",
        "playback",
        "profile_hash",
        "source_revision",
    ):
        if not report_data.get(req_field):
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_INVALID",
                    path=f"/{req_field}",
                    message=f"Missing required qualification report field '{req_field}'",
                )
            )

    # Check scenario cases
    cases = report_data.get("cases", [])
    if not cases:
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INCOMPLETE",
                path="/cases",
                message="Qualification report contains no executed scenario cases",
            )
        )
    else:
        classes_present = set(c.get("scenario_class") for c in cases)
        for req_class in REQUIRED_SCENARIO_CLASSES:
            if req_class not in classes_present:
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INCOMPLETE",
                        path="/cases",
                        message=f"Required qualification scenario class '{req_class}' is missing",
                    )
                )
        # All cases must pass
        for c_idx, case in enumerate(cases):
            if not case.get("passed"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_FAILED",
                        path=f"/cases/{c_idx}",
                        message=f"Scenario '{case.get('scenario_id')}' did not pass",
                    )
                )

        if report_data.get("playback") == "qualified_fragment_completion":
            required_evidence = (
                ("fixture_hashes", report_data.get("fixture_hashes")),
                ("measurements", report_data.get("measurements")),
                ("budgets", report_data.get("budgets")),
                ("raw_evidence_references", report_data.get("raw_evidence_references")),
                (
                    "playback_boundary_description",
                    report_data.get("playback_boundary_description"),
                ),
            )
            for field, value in required_evidence:
                if not value:
                    diagnostics.append(
                        FlowDiagnostic(
                            code="QUALIFICATION_INCOMPLETE",
                            path=f"/{field}",
                            message=f"Qualified playback report is missing '{field}' evidence",
                        )
                    )
            for c_idx, case in enumerate(cases):
                if not case.get("trace_id"):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="QUALIFICATION_INCOMPLETE",
                            path=f"/cases/{c_idx}/trace_id",
                            message="Qualified playback case is missing its trace ID",
                        )
                    )
                if not case.get("measurements"):
                    diagnostics.append(
                        FlowDiagnostic(
                            code="QUALIFICATION_INCOMPLETE",
                            path=f"/cases/{c_idx}/measurements",
                            message="Qualified playback case is missing measurements",
                        )
                    )

    # Check report hash against canonical bytes
    given_hash = report_data.get("report_hash")
    if given_hash:
        clean_data = dict(report_data)
        clean_data.pop("report_hash", None)
        expected_hash = hashlib.sha256(
            b"voice-ai/capability-report/v2\0" + canonical_json_v1(clean_data)
        ).hexdigest()
        if given_hash != expected_hash:
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_HASH_MISMATCH",
                    path="/report_hash",
                    message=f"Report hash {given_hash} does not match canonical preimage {expected_hash}",
                )
            )

    diagnostics.sort(key=lambda d: d.sort_key())
    return diagnostics

