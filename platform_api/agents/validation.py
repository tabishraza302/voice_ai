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

from bot.flow.qualification import (
    AgentQualificationSettings,
    LegacyAgentQualificationSettings,
    ProviderlessAgentQualificationSettings,
)
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
    qualification_settings: (
        AgentQualificationSettings
        | ProviderlessAgentQualificationSettings
        | LegacyAgentQualificationSettings
        | None
    ) = None,
    require_voice_capability: bool = True,
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
    if require_voice_capability:
        errors.extend(
            validate_voice_capability(config, capabilities, qualification_settings)
        )
    return (errors, warnings)


def validate_catalog_eligibility(
    config: dict[str, Any],
    catalog: ModelCatalog | None = None,
) -> list[FlowDiagnostic]:
    """Ensure configured models and voices are real, catalogued, and selectable."""
    cat = catalog if catalog is not None else load_catalog()
    diagnostics: list[FlowDiagnostic] = []

    try:
        settings = extract_settings(config)
    except AgentConfigError as exc:
        return [_config_error(exc)]
    if "llm_provider" not in config:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_PROVIDER_REQUIRED",
                path="/llm_provider",
                message="Select the serving provider for this model before using the agent.",
                severity="error",
            )
        )

    valid_llms = {
        (model.provider_id, model.id)
        for model in cat.llm_models
        if model.selectable
    }
    llm_identity = (settings["llm_provider"], settings["llm_model"])
    if llm_identity not in valid_llms:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/llm_model",
                message=(
                    f"Model '{settings['llm_model']}' for provider "
                    f"'{settings['llm_provider']}' is not selectable in the "
                    "platform catalog"
                ),
                severity="error",
            )
        )

    stt_provider = next(
        (provider for provider in cat.stt_providers if provider.id == settings["stt_provider"]),
        None,
    )
    if stt_provider is None:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/stt_provider",
                message="Selected STT provider is not qualified in the platform catalog",
                severity="error",
            )
        )
    else:
        stt_model = next(
            (model for model in stt_provider.models if model.id == settings["stt_model"]),
            None,
        )
        if stt_model is None or not stt_model.selectable:
            diagnostics.append(
                FlowDiagnostic(
                    code="MODEL_UNQUALIFIED",
                    path="/stt_model",
                    message="Selected STT model is not qualified for this deployment",
                    severity="error",
                )
            )

    tts_provider = next(
        (provider for provider in cat.tts_providers if provider.id == settings["tts_provider"]),
        None,
    )
    tts_model = (
        next(
            (model for model in tts_provider.models if model.id == settings["tts_model"]),
            None,
        )
        if tts_provider is not None
        else None
    )
    uses_voice = bool(settings["tts_voice_en"] or settings["tts_voice_hi"])
    if tts_provider is None:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/tts_provider",
                message="Selected TTS provider is not registered in the platform catalog",
                severity="error",
            )
        )
    elif tts_model is None:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/tts_model",
                message="Selected TTS model is not registered for the provider",
                severity="error",
            )
        )
    elif uses_voice and not tts_model.selectable:
        diagnostics.append(
            FlowDiagnostic(
                code="MODEL_UNQUALIFIED",
                path="/tts_model",
                message="Selected TTS model is not qualified for this deployment",
                severity="error",
            )
        )

    if uses_voice:
        for key, label in (
            ("tts_voice_en", "English TTS voice"),
            ("tts_voice_hi", "Hindi TTS voice"),
        ):
            voice = (
                next(
                    (item for item in tts_model.voices if item.id == settings[key]),
                    None,
                )
                if settings[key] and tts_model is not None
                else None
            )
            if settings[key] and (voice is None or not voice.selectable):
                diagnostics.append(
                    FlowDiagnostic(
                        code="MODEL_UNQUALIFIED",
                        path=f"/{key}",
                        message=f"{label} is not qualified for the selected provider and model",
                        severity="error",
                    )
                )

    return diagnostics


def validate_chat_test_eligibility(
    config: dict[str, Any],
    catalog: ModelCatalog | None = None,
    limits: FlowLimits | None = None,
) -> tuple[list[FlowDiagnostic], list[FlowDiagnostic]]:
    """Validate only the inputs needed by text-only Chat Test.

    Voice, speech-provider, and playback-capability qualification remains
    required for publication, but Chat Test uses only the runtime LLM.
    """
    secret_errors = secret_diagnostics(config)
    structural = validate_draft_structure(config, limits)
    structural_errors = [d for d in structural if d.severity == "error"]
    if structural_errors:
        return (secret_errors + structural_errors, [])

    catalog_errors = [
        diagnostic
        for diagnostic in validate_catalog_eligibility(config, catalog)
        if diagnostic.path in {"/llm_model", "/llm_provider"}
    ]
    semantic_errors, warnings = validate_draft_semantics(
        config,
        limits,
        require_voice_capability=False,
    )
    return (secret_errors + catalog_errors + semantic_errors, warnings)


def validate_voice_capability(
    config: dict[str, Any],
    capabilities: AdapterCapabilities | None,
    qualification_settings: (
        AgentQualificationSettings
        | ProviderlessAgentQualificationSettings
        | LegacyAgentQualificationSettings
        | None
    ) = None,
) -> list[FlowDiagnostic]:
    """Require voice settings to match a trusted, pinned deployment report."""
    try:
        settings = extract_settings(config)
    except AgentConfigError:
        return []
    if not (settings["tts_voice_en"] or settings["tts_voice_hi"]):
        return []

    if isinstance(
        qualification_settings,
        (ProviderlessAgentQualificationSettings, LegacyAgentQualificationSettings),
    ):
        return [
            FlowDiagnostic(
                code="PLAYBACK_UNQUALIFIED",
                path="/qualification",
                message=(
                    "This legacy qualification report does not bind the serving "
                    "llm_provider; rerun qualification with protocol version 4"
                ),
                severity="error",
            )
        ]
    if isinstance(qualification_settings, AgentQualificationSettings):
        report_setting_keys = (
            "llm_provider",
            "llm_model",
            "stt_provider",
            "stt_model",
            "tts_provider",
            "tts_model",
            "tts_voice_en",
            "tts_voice_hi",
        )
    else:
        return [
            FlowDiagnostic(
                code="PLAYBACK_UNQUALIFIED",
                path="/qualification",
                message=(
                    "Voice settings require a provider-bound protocol-version-4 report "
                    "for the exact LLM, STT provider/model, TTS provider/model, voices, "
                    "and deployment identity"
                ),
                severity="error",
            )
        ]

    mismatched_settings = [
        FlowDiagnostic(
            code="PLAYBACK_UNQUALIFIED",
            path=f"/{key}",
            message="Selected setting does not match the pinned qualification report",
            severity="error",
        )
        for key in report_setting_keys
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
    qualification_settings: (
        AgentQualificationSettings
        | ProviderlessAgentQualificationSettings
        | LegacyAgentQualificationSettings
        | None
    ) = None,
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
