"""Qualification harness contracts and report validation per flow contract section 9.

Validates immutable qualification reports:
- protocol_version: 2
- Immutable deployment capability and selected agent settings tuple
- Required seven scenario groups executed and passed
- Verified actual latency/resource measurements and raw evidence references
- Measurements strictly canonical: safe integers (e.g. integer microseconds) or decimal strings (e.g. '12.5'), never floats
"""

from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bot.flow.canonical import canonical_json_v1
from bot.flow.schemas import MAX_SAFE_INTEGER, MIN_SAFE_INTEGER, FlowDiagnostic

ScenarioClass = Literal[
    "normal_en_hi",
    "adversarial_gating",
    "barge_in",
    "playback_faults",
    "stop_cancellation",
    "chat_durable_commit",
    "full_path_matrix",
]

REQUIRED_SCENARIO_CLASSES: tuple[ScenarioClass, ...] = (
    "normal_en_hi",
    "adversarial_gating",
    "barge_in",
    "playback_faults",
    "stop_cancellation",
    "chat_durable_commit",
    "full_path_matrix",
)

CanonicalMeasurement = Union[int, str]


def canonicalize_measurement(val: float | int | str) -> int | str:
    """Convert measured latency/timing to canonical representation without silent rounding or fabrication.

    - Floats are expanded from their exact shortest round-trip representation
      (``repr``) into a plain decimal string, e.g. 12.5 -> '12.5' and
      1e-10 -> '0.0000000001'. No fixed-precision rounding is applied.
    - Safe integers (e.g. integer microseconds) are preserved.
    - Decimal strings are validated and preserved.
    - Rejects bool, NaN, Infinity and non-finite values.
    """
    if isinstance(val, bool):
        raise ValueError("Boolean values are not valid measurements")
    if isinstance(val, int):
        if val < MIN_SAFE_INTEGER or val > MAX_SAFE_INTEGER:
            raise ValueError(f"Integer measurement {val} out of safe integer range")
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("NaN and Infinity are not valid measurements")
        # Decimal(repr(float)) is exact for the float's shortest round-trip
        # representation. format(..., 'f') expands exponent notation without
        # changing the value, unlike a fixed-precision format string.
        expanded = format(Decimal(repr(val)), "f")
        if expanded in {"-0", "-0.0"} or Decimal(expanded).is_zero():
            return "0"
        return expanded
    if isinstance(val, str):
        s = val.strip()
        if not re.match(r"^-?\d+(\.\d+)?$", s):
            raise ValueError(f"String measurement must be a valid decimal string, got {val!r}")
        return s
    raise TypeError(f"Unsupported measurement type: {type(val).__name__}")


def _validate_measurements_dict(measurements: dict[str, Any]) -> dict[str, CanonicalMeasurement]:
    validated: dict[str, CanonicalMeasurement] = {}
    for k, v in measurements.items():
        if isinstance(v, bool):
            raise ValueError(f"Measurement '{k}' cannot be boolean")
        if isinstance(v, float):
            raise ValueError(
                f"Floating point measurement for '{k}' ({v!r}) is rejected in canonical capability report; "
                "use integer microseconds/milliseconds or decimal strings (e.g. '12.5')"
            )
        if isinstance(v, int):
            if v < MIN_SAFE_INTEGER or v > MAX_SAFE_INTEGER:
                raise ValueError(f"Measurement '{k}' integer {v} out of safe integer bounds")
            validated[k] = v
        elif isinstance(v, str):
            s = v.strip()
            if not re.match(r"^-?\d+(\.\d+)?$", s):
                raise ValueError(f"Measurement '{k}' string must be a valid decimal string, got {v!r}")
            validated[k] = s
        else:
            raise TypeError(f"Measurement '{k}' must be safe integer or decimal string, got {type(v).__name__}")
    return validated

class ScenarioResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    scenario_id: str
    scenario_class: ScenarioClass
    passed: bool
    measurements: dict[str, CanonicalMeasurement] = Field(default_factory=dict)
    raw_evidence: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    error_message: str | None = None

    @field_validator("measurements", mode="before")
    @classmethod
    def validate_measurements(cls, v: dict[str, Any]) -> dict[str, CanonicalMeasurement]:
        return _validate_measurements_dict(v)


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
    measurements: dict[str, CanonicalMeasurement] = Field(default_factory=dict)
    budgets: dict[str, CanonicalMeasurement] = Field(default_factory=dict)
    raw_evidence_references: list[str] = Field(default_factory=list)
    playback_boundary_description: str
    limitations: list[str] = Field(default_factory=list)
    report_hash: str = ""

    @field_validator("measurements", "budgets", mode="before")
    @classmethod
    def validate_measurements(cls, v: dict[str, Any]) -> dict[str, CanonicalMeasurement]:
        return _validate_measurements_dict(v)

    def compute_hash(self) -> str:
        _validate_measurements_dict(self.measurements)
        _validate_measurements_dict(self.budgets)
        for case in self.cases:
            _validate_measurements_dict(case.measurements)
        dump = self.model_dump()
        dump.pop("report_hash", None)
        return hashlib.sha256(
            b"voice-ai/capability-report/v2\0" + canonical_json_v1(dump)
        ).hexdigest()


def validate_capability_report(report_data: dict[str, Any]) -> list[FlowDiagnostic]:
    """Validate qualification report format, required seven scenario groups, and report hash."""
    diagnostics: list[FlowDiagnostic] = []

    if (
        type(report_data.get("protocol_version")) is not int
        or report_data.get("protocol_version") != 2
    ):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/protocol_version",
                message="Qualification report must have integer protocol_version: 2",
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
        "playback_boundary_description",
    ):
        val = report_data.get(req_field)
        if not val or (isinstance(val, str) and not val.strip()):
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_INVALID",
                    path=f"/{req_field}",
                    message=f"Missing required qualification report field '{req_field}'",
                )
            )

    locales = report_data.get("locales")
    if not locales or not isinstance(locales, list):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/locales",
                message="Missing or empty required locales list",
            )
        )
    else:
        unknown_locales = [locale for locale in locales if locale not in ("en", "hi")]
        if unknown_locales:
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_INVALID",
                    path="/locales",
                    message=f"Unsupported qualification locales: {unknown_locales}",
                )
            )

    for dict_field in ("fixture_hashes", "measurements"):
        val = report_data.get(dict_field)
        if not val or not isinstance(val, dict):
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_INVALID",
                    path=f"/{dict_field}",
                    message=f"Missing or empty required dictionary field '{dict_field}'",
                )
            )

    raw_evidence_references = report_data.get("raw_evidence_references")
    if (
        not isinstance(raw_evidence_references, list)
        or not raw_evidence_references
        or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in raw_evidence_references
        )
    ):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/raw_evidence_references",
                message="Missing or invalid non-empty raw evidence references",
            )
        )
    # Validate measurements canonical representation (safe integers or decimal strings, no floats)
    report_meas = report_data.get("measurements")
    if isinstance(report_meas, dict):
        for k, v in report_meas.items():
            if isinstance(v, float) or isinstance(v, bool) or not isinstance(v, (int, str)):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/measurements/{k}",
                        message=f"Measurement '{k}' must be an integer (e.g. integer microseconds) or decimal string, not float {v!r}",
                    )
                )
            elif isinstance(v, str) and not re.match(r"^-?\d+(\.\d+)?$", v.strip()):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/measurements/{k}",
                        message=f"Measurement '{k}' string must be a valid decimal string, got {v!r}",
                    )
                )

    report_budgets = report_data.get("budgets")
    if isinstance(report_budgets, dict):
        for k, v in report_budgets.items():
            if isinstance(v, float) or isinstance(v, bool) or not isinstance(v, (int, str)):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/budgets/{k}",
                        message=f"Budget '{k}' must be an integer or decimal string, not float {v!r}",
                    )
                )
            elif isinstance(v, str) and not re.match(r"^-?\d+(\.\d+)?$", v.strip()):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/budgets/{k}",
                        message=f"Budget '{k}' string must be a valid decimal string, got {v!r}",
                    )
                )

    if report_data.get("playback") == "qualified_fragment_completion" and not report_budgets:
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INCOMPLETE",
                path="/budgets",
                message=(
                    "A qualified playback report must record the configured "
                    "latency/resource budgets used for the run"
                ),
            )
        )

    # Check scenario cases
    cases = report_data.get("cases", [])
    if not cases or not isinstance(cases, list):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INCOMPLETE",
                path="/cases",
                message="Qualification report contains no executed scenario cases",
            )
        )
    else:
        classes_present: set[str] = set()
        for c_idx, case in enumerate(cases):
            if not isinstance(case, dict):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/cases/{c_idx}",
                        message="Scenario case must be a JSON object",
                    )
                )
                continue
            s_class = case.get("scenario_class")
            if s_class not in REQUIRED_SCENARIO_CLASSES:
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/cases/{c_idx}/scenario_class",
                        message=f"Unknown or unsupported scenario class '{s_class}'",
                    )
                )
            c_ev = case.get("raw_evidence")
            c_meas = case.get("measurements")
            if not c_ev and not c_meas:
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INVALID",
                        path=f"/cases/{c_idx}",
                        message=f"Scenario '{case.get('scenario_id')}' missing measurements and raw evidence",
                    )
                )
            # Check case measurement types
            if isinstance(c_meas, dict):
                for k, v in c_meas.items():
                    if isinstance(v, float) or isinstance(v, bool) or not isinstance(v, (int, str)):
                        diagnostics.append(
                            FlowDiagnostic(
                                code="QUALIFICATION_INVALID",
                                path=f"/cases/{c_idx}/measurements/{k}",
                                message=f"Scenario measurement '{k}' must be an integer or decimal string, not float {v!r}",
                            )
                        )
                    elif isinstance(v, str) and not re.match(r"^-?\d+(\.\d+)?$", v.strip()):
                        diagnostics.append(
                            FlowDiagnostic(
                                code="QUALIFICATION_INVALID",
                                path=f"/cases/{c_idx}/measurements/{k}",
                                message=f"Scenario measurement '{k}' string must be a valid decimal string, got {v!r}",
                            )
                        )
            if not case.get("passed"):
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_FAILED",
                        path=f"/cases/{c_idx}",
                        message=f"Scenario '{case.get('scenario_id')}' did not pass",
                    )
                )
            else:
                classes_present.add(s_class)

        for req_class in REQUIRED_SCENARIO_CLASSES:
            if req_class not in classes_present:
                diagnostics.append(
                    FlowDiagnostic(
                        code="QUALIFICATION_INCOMPLETE",
                        path="/cases",
                        message=f"Required qualification scenario class '{req_class}' is missing",
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
    if (
        not given_hash
        or not isinstance(given_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", given_hash)
    ):
        diagnostics.append(
            FlowDiagnostic(
                code="QUALIFICATION_INVALID",
                path="/report_hash",
                message="Missing or invalid lowercase 64-hex report_hash in qualification report",
            )
        )
    else:
        clean_data = dict(report_data)
        clean_data.pop("report_hash", None)
        try:
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
        except (ValueError, TypeError) as ex:
            diagnostics.append(
                FlowDiagnostic(
                    code="QUALIFICATION_INVALID",
                    path="/report_hash",
                    message=f"Cannot compute canonical report hash: {ex}",
                )
            )

    diagnostics.sort(key=lambda d: d.sort_key())
    return diagnostics
