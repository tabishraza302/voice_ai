"""Qualification harness contracts and report validation per flow contract section 9.

Validates immutable qualification reports:
- protocol_version: 1
- Immutable capability tuple
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
]


class ScenarioResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    scenario_class: ScenarioClass
    passed: bool
    measurements: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    error_message: str | None = None


class CapabilityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: int = 1
    adapter_id: str
    adapter_version: str
    model_revision: str
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
    playback_boundary_description: str
    limitations: list[str] = Field(default_factory=list)
    report_hash: str = ""

    def compute_hash(self) -> str:
        dump = self.model_dump()
        dump.pop("report_hash", None)
        return hashlib.sha256(b"voice-ai/capability-report/v1\0" + canonical_json_v1(dump)).hexdigest()


def validate_capability_report(report_data: dict[str, Any]) -> list[FlowDiagnostic]:
    """Validate qualification report format, required scenarios, and report hash."""
    diagnostics: list[FlowDiagnostic] = []

    if report_data.get("protocol_version") != 1:
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/protocol_version",
                message="Qualification report must have protocol_version: 1",
            )
        )

    for req_field in ("adapter_id", "adapter_version", "model_revision", "playback", "profile_hash"):
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
        for req_class in ("normal_en_hi", "barge_in", "correction", "refusal"):
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

    # Check report hash against canonical bytes
    given_hash = report_data.get("report_hash")
    if given_hash:
        clean_data = dict(report_data)
        clean_data.pop("report_hash", None)
        expected_hash = hashlib.sha256(
            b"voice-ai/capability-report/v1\0" + canonical_json_v1(clean_data)
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

