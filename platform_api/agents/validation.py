"""Flow validation and diagnostic adapters for the platform control plane.

Follows flow-contract.md and master spec III.5:
- validate_draft_structure: authoring schema & size limits (failure blocks save)
- validate_draft_semantics: normalization and executable checks (errors returned in draft view)
- validate_catalog_eligibility: ensures models, voices, and providers are qualified
- validate_agent_config: combines all three for draft/publish callers

A draft config may be a bare flow or the platform envelope documented in
``platform_api.agents.config``.  Only the flow member is validated against the
flow schemas; provider/voice settings are checked against the catalog.
"""

from __future__ import annotations

from typing import Any

from bot.flow.qualification import AgentQualificationSettings
from bot.flow.schemas import AdapterCapabilities, FlowDiagnostic, FlowLimits
from bot.flow.validation import (
    normalize_executable,
    validate_authoring,
    validate_executable,
)
from bot.flow.tools import ToolRegistry
from platform_api.agents.config import (
    AgentConfigError,
    extract_flow,
    extract_settings,
    validate_config_bounds,
)
from platform_api.agents.schemas import DiagnosticView
from platform_api.agents.security import find_secret_paths
from platform_api.catalog import ModelCatalog, load_catalog


def diagnostic_to_view(d: FlowDiagnostic) -> DiagnosticView:
    """Map pure FlowDiagnostic to wire DiagnosticView."""
    return DiagnosticView(
        code=d.code,
        path=d.path,
        message=d.message,
        severity=d.severity,
    )


def diagnostics_to_views(diagnostics: list[FlowDiagnostic]) -> list[DiagnosticView]:
    """Map list of FlowDiagnostics to DiagnosticViews."""
    return [diagnostic_to_view(d) for d in diagnostics]


def _config_error(exc: AgentConfigError) -> FlowDiagnostic:
    return FlowDiagnostic(
        code="SCHEMA_INVALID",
        path=exc.path,
        message=str(exc),
        severity="error",
    )


def secret_diagnostics(config: dict[str, Any]) -> list[FlowDiagnostic]:
    """Return blocking diagnostics for forbidden credential keys."""
    return [
        FlowDiagnostic(
            code="SECRET_NOT_ALLOWED",
            path=path,
            message="Operational credentials and secret fields are not allowed in agent configuration",
            severity="error",
        )
        for path in find_secret_paths(config)
    ]


def validate_draft_structure(
    config: dict[str, Any],
    limits: FlowLimits | None = None,
) -> list[FlowDiagnostic]:
    """Validate flow authoring structure and configured size bounds.

    Returns non-empty diagnostics if the flow violates wire schema or size bounds.
    """
    limits = limits or FlowLimits()
    try:
        validate_config_bounds(config, limits.max_flow_bytes)
        flow = extract_flow(config)
    except AgentConfigError as exc:
        return [_config_error(exc)]
    return validate_authoring(flow, limits)


def validate_draft_semantics(
    config: dict[str, Any],
    limits: FlowLimits | None = None,
    capabilities: AdapterCapabilities | None = None,
    tools: ToolRegistry | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
) -> tuple[list[FlowDiagnostic], list[FlowDiagnostic]]:
    """Normalize and validate executable semantics.

    Returns tuple (errors, warnings).
    Does not raise for incomplete graphs; returns errors in the errors list.
    """
    limits = limits or FlowLimits()
    try:
        flow = extract_flow(config)
        normalized = normalize_executable(flow, limits)
        diagnostics = validate_executable(
            normalized, limits, capabilities, tools=tools
        )
    except AgentConfigError as exc:
        return ([_config_error(exc)], [])
    except Exception as exc:
        # Schema error or conversion failure during normalization
        return (
            [
                FlowDiagnostic(
                    code="NORMALIZATION_FAILED",
                    path="",
                    message=str(exc),
                    severity="error",
                )
            ],
            [],
        )

    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    errors.extend(
        validate_voice_capability(config, capabilities, qualification_settings)
    )
    return (errors, warnings)


def validate_catalog_eligibility(
    config: dict[str, Any],
    catalog: ModelCatalog | None = None,
) -> list[FlowDiagnostic]:
    """Ensure configured model, STT, and voice items are qualified in the platform catalog."""
    cat = catalog if catalog is not None else load_catalog()
    diagnostics: list[FlowDiagnostic] = []

    try:
        settings = extract_settings(config)
    except AgentConfigError as exc:
        return [_config_error(exc)]

    valid_llms = {m.id for m in cat.llm_models}
    if settings["llm_model"] not in valid_llms:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/llm_model",
                message=(
                    f"Model '{settings['llm_model']}' is not qualified in the "
                    "platform catalog"
                ),
                severity="error",
            )
        )

    valid_stts = {s.id for s in cat.stt_providers}
    if settings["stt_provider"] not in valid_stts:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/stt_provider",
                message=(
                    f"STT provider '{settings['stt_provider']}' is not qualified "
                    "in the platform catalog"
                ),
                severity="error",
            )
        )
    valid_voices = {voice.id for voice in cat.tts_voices}

    for key, label in (
        ("tts_voice_en", "English TTS voice"),
        ("tts_voice_hi", "Hindi TTS voice"),
    ):
        if settings[key] and settings[key] not in valid_voices:
            diagnostics.append(
                FlowDiagnostic(
                    code="MODEL_UNQUALIFIED",
                    path=f"/{key}",
                    message=(
                        f"{label} '{settings[key]}' is not qualified in the "
                        "platform catalog"
                    ),
                    severity="error",
                )
            )

    return diagnostics


def validate_voice_capability(
    config: dict[str, Any],
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None = None,
) -> list[FlowDiagnostic]:
    """Require voice settings to match a trusted, pinned deployment report."""
    try:
        settings = extract_settings(config)
    except AgentConfigError:
        return []
    if not (settings["tts_voice_en"] or settings["tts_voice_hi"]):
        return []
    if not isinstance(qualification_settings, AgentQualificationSettings):
        return [
            FlowDiagnostic(
                code="PLAYBACK_UNQUALIFIED",
                path="/qualification",
                message=(
                    "Voice settings require a pinned report for the exact model, "
                    "STT provider, voices, and trusted deployment identity"
                ),
            )
        ]

    mismatched_settings = [
        FlowDiagnostic(
            code="PLAYBACK_UNQUALIFIED",
            path=f"/{key}",
            message="Selected setting does not match the pinned qualification report",
            severity="error",
        )
        for key in ("llm_model", "stt_provider", "tts_voice_en", "tts_voice_hi")
        if settings[key] != getattr(qualification_settings, key)
    ]
    if mismatched_settings:
        return mismatched_settings

    if (
        capabilities is not None
        and capabilities.playback == "qualified_fragment_completion"
        and capabilities.speech_gating
        and capabilities.cancel_generation
        and capabilities.metadata != "none"
        and {"en", "hi"}.issubset(capabilities.locales)
        and len(capabilities.report_hash) == 64
        and all(char in "0123456789abcdef" for char in capabilities.report_hash)
    ):
        return []
    return [
        FlowDiagnostic(
            code="PLAYBACK_UNQUALIFIED",
            path="/capabilities",
            message="Voice settings require qualified EN/HI fragment playback, metadata, gating, and cancellation",
            severity="error",
        )
    ]


def validate_agent_config(
    config: dict[str, Any],
    limits: FlowLimits | None = None,
    capabilities: AdapterCapabilities | None = None,
    tools: ToolRegistry | None = None,
    catalog: ModelCatalog | None = None,
    qualification_settings: AgentQualificationSettings | None = None,
) -> tuple[list[FlowDiagnostic], list[FlowDiagnostic]]:
    """Return (blocking errors, warnings) for a complete agent config.

    Structural failures short-circuit the other checks, matching save semantics.
    Secret keys are always blocking and are reported before schema diagnostics.
    """
    secret_errors = secret_diagnostics(config)
    structural = validate_draft_structure(config, limits)
    structural_errors = [d for d in structural if d.severity == "error"]
    if structural_errors:
        return (secret_errors + structural_errors, [])

    catalog_errors = validate_catalog_eligibility(config, catalog)
    semantic_errors, warnings = validate_draft_semantics(
        config,
        limits,
        capabilities,
        tools,
        qualification_settings,
    )
    return (secret_errors + catalog_errors + semantic_errors, warnings)
