"""Validate and detach the immutable platform configuration before join consumption."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from uuid import UUID
from bot.provider_registry import model_for
from bot.agents.compiler import compile_system_prompt
from bot.config import Settings
from bot.flow.canonical import compute_authoring_hash, compute_limits_hash
from bot.flow.compiler import compile_flow
from bot.flow.schemas import AdapterCapabilities, CompiledFlow, FlowLimits
from platform_api.agents.config import (
    LEGACY_AGENT_SETTING_KEYS,
    V2_AGENT_SETTING_KEYS,
    SUPPORTED_AGENT_LLM_PROVIDERS,
    compute_legacy_model_hash,
    compute_legacy_settings_hash,
    compute_legacy_voice_hash,
    compute_model_hash,
    compute_model_hash_v3,
    compute_settings_hash,
    compute_settings_hash_v3,
    compute_voice_hash,
    compute_voice_hash_v3,
    extract_flow,
    extract_settings,
)
from platform_api.agents.defaults import compute_base_prompt_hash, compute_policy_hash
from bot.runtime.budget_client import BudgetPrerequisiteError, validate_initial_permit
from platform_api.sessions.schemas import RuntimeBudgetSnapshot, RuntimeConfigView


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_HASH = "0" * 64
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "compiler_version",
        "authoring_config",
        "authoring_flow",
        "normalized_flow",
        "manifest",
        "policy",
        "persona_prompt",
        "model_pins",
        "hashes",
        "capabilities",
    }
)
_POLICY_KEYS = frozenset(
    {"version_id", "version", "slug", "policy_hash", "prompt_text"}
)
_SETTING_KEYS = frozenset(
    {
        "llm_model",
        "stt_provider",
        "stt_model",
        "tts_provider",
        "tts_model",
        "tts_voice_en",
        "tts_voice_hi",
    }
)
_V3_SETTING_KEYS = _SETTING_KEYS | {"llm_provider"}
_V3_PIN_KEYS = _V3_SETTING_KEYS | {"model_hash", "voice_hash", "settings_hash"}
_PIN_KEYS = _SETTING_KEYS | {"model_hash", "voice_hash", "settings_hash"}
_LEGACY_SETTING_KEYS = LEGACY_AGENT_SETTING_KEYS
_LEGACY_PIN_KEYS = _LEGACY_SETTING_KEYS | {
    "model_hash",
    "voice_hash",
    "settings_hash",
}
_HASH_KEYS = frozenset(
    {
        "authoring_hash",
        "flow_hash",
        "artifact_hash",
        "policy_hash",
        "base_prompt_hash",
        "model_hash",
        "voice_hash",
        "settings_hash",
    }
)


class RuntimeSnapshotError(ValueError):
    """The registered session snapshot is stale, incomplete, or unsafe to run."""


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Detached execution inputs derived only from one registered version."""

    session_id: UUID
    owner_user_id: UUID
    participant_id: UUID
    auth_session_id: UUID | None
    participant_kind: str
    origin: str
    runner_id: str
    registration_id: UUID
    config_version_id: UUID
    session_version: int
    state: str
    connect_deadline: datetime
    hard_end_deadline: datetime
    budget: RuntimeBudgetSnapshot
    hashes: tuple[tuple[str, str], ...]
    capabilities: AdapterCapabilities
    compiled: CompiledFlow
    model_pins: Mapping[str, str]
    policy_prompt: str
    system_instruction: str

    def settings_for(self, base: Settings) -> Settings:
        """Return a per-session copy with every published pin applied."""
        provider_id = self.model_pins.get("llm_provider")
        if (
            provider_id not in SUPPORTED_AGENT_LLM_PROVIDERS
            or base.llm_provider != provider_id
        ):
            raise RuntimeSnapshotError(
                "Published LLM provider does not match the configured local provider"
            )
        from bot.services import local_llm_tags_url

        if local_llm_tags_url(base) is None:
            raise RuntimeSnapshotError(
                "Published Ollama provider does not match a local Ollama endpoint"
            )
        return base.model_copy(
            deep=True,
            update={
                **dict(self.model_pins),
                "stt_language": "auto",
                "system_instruction": self.system_instruction,
            },
        )

    def hash_value(self, name: str) -> str:
        for key, value in self.hashes:
            if key == name:
                return value
        raise KeyError(name)


def validate_runtime_snapshot(
    view: RuntimeConfigView,
    *,
    expected_session_id: UUID,
    expected_runner_id: str,
    now: datetime | None = None,
) -> RuntimeSnapshot:
    """Verify all hashes and runtime gates, then return only detached inputs."""
    view = view.model_copy(deep=True)
    if view.session_id != expected_session_id:
        raise RuntimeSnapshotError("Runtime config belongs to a different session")
    if view.runner_id != expected_runner_id:
        raise RuntimeSnapshotError("Runtime config belongs to a different runner")
    if view.state not in {"REGISTERED", "RECONNECTING"}:
        raise RuntimeSnapshotError("Session is not eligible for a browser join")

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise RuntimeSnapshotError("Runtime clock must be timezone-aware")
    if view.hard_end_deadline <= instant:
        raise RuntimeSnapshotError("Session hard-end deadline has passed")
    if view.state == "REGISTERED" and view.connect_deadline <= instant:
        raise RuntimeSnapshotError("Session connect deadline has passed")
    try:
        validate_initial_permit(
            view.budget,
            session_id=expected_session_id,
            hard_end_deadline=view.hard_end_deadline,
            now=instant,
        )
    except BudgetPrerequisiteError as exc:
        raise RuntimeSnapshotError(str(exc)) from exc

    config = view.immutable_config
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
        raise RuntimeSnapshotError("Immutable session config has an unsupported shape")
    schema_version = config.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in {1, 2, 3}
        or config.get("compiler_version") != "flow-compiler/1"
    ):
        raise RuntimeSnapshotError("Immutable session config version is unsupported")

    authoring_config = config.get("authoring_config")
    authoring_flow = config.get("authoring_flow")
    normalized_flow = config.get("normalized_flow")
    manifest = config.get("manifest")
    policy = config.get("policy")
    persona_prompt = config.get("persona_prompt")
    model_pins = config.get("model_pins")
    hashes = config.get("hashes")
    if (
        not isinstance(authoring_config, dict)
        or not isinstance(authoring_flow, dict)
        or not isinstance(normalized_flow, dict)
        or not isinstance(manifest, dict)
        or not isinstance(policy, dict)
        or not isinstance(model_pins, dict)
        or not isinstance(hashes, dict)
        or not isinstance(persona_prompt, str)
    ):
        raise RuntimeSnapshotError("Immutable session config is incomplete")
    expected_pin_keys = (
        _LEGACY_PIN_KEYS
        if schema_version == 1
        else _PIN_KEYS
        if schema_version == 2
        else _V3_PIN_KEYS
    )
    if (
        set(policy) != _POLICY_KEYS
        or set(model_pins) != expected_pin_keys
        or set(hashes) != _HASH_KEYS
    ):
        raise RuntimeSnapshotError("Immutable session config pins have an unsupported shape")

    for value in hashes.values():
        if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
            raise RuntimeSnapshotError("Immutable session config contains an invalid hash")
    for name in ("authoring_hash", "flow_hash", "artifact_hash", "model_hash", "voice_hash", "settings_hash"):
        if getattr(view, name) != hashes[name]:
            raise RuntimeSnapshotError(f"Registered {name} does not match its immutable config")

    try:
        extracted_flow = extract_flow(authoring_config)
        extracted_settings = extract_settings(authoring_config)
    except (TypeError, ValueError) as exc:
        raise RuntimeSnapshotError("Immutable authoring config is invalid") from exc
    if extracted_flow != authoring_flow:
        raise RuntimeSnapshotError("Published authoring flow does not match its source config")
    try:
        source_hash = compute_authoring_hash(authoring_config)
    except (TypeError, ValueError) as exc:
        raise RuntimeSnapshotError("Immutable authoring source cannot be hashed") from exc
    if source_hash != view.authoring_hash:
        raise RuntimeSnapshotError("Published authoring hash does not match its source config")
    if schema_version == 1:
        extracted_settings = {
            key: extracted_settings[key] for key in _LEGACY_SETTING_KEYS
        }
        expected_model_hash = compute_legacy_model_hash(extracted_settings)
        expected_voice_hash = compute_legacy_voice_hash(extracted_settings)
        expected_settings_hash = compute_legacy_settings_hash(extracted_settings)
        setting_keys = _LEGACY_SETTING_KEYS
    elif schema_version == 2:
        extracted_settings = {
            key: extracted_settings[key] for key in V2_AGENT_SETTING_KEYS
        }
        expected_model_hash = compute_model_hash(extracted_settings)
        expected_voice_hash = compute_voice_hash(extracted_settings)
        expected_settings_hash = compute_settings_hash(extracted_settings)
        setting_keys = V2_AGENT_SETTING_KEYS
    else:
        expected_model_hash = compute_model_hash_v3(extracted_settings)
        expected_voice_hash = compute_voice_hash_v3(extracted_settings)
        expected_settings_hash = compute_settings_hash_v3(extracted_settings)
        setting_keys = _V3_SETTING_KEYS
    if set(extracted_settings) != setting_keys:
        raise RuntimeSnapshotError("Published model settings are incomplete")
    if any(model_pins.get(name) != value for name, value in extracted_settings.items()):
        raise RuntimeSnapshotError("Published model pins do not match their authoring source")

    for name in ("model_hash", "voice_hash", "settings_hash"):
        if not isinstance(model_pins.get(name), str) or model_pins[name] != hashes[name]:
            raise RuntimeSnapshotError(f"Published {name} does not match its immutable config")
    if (
        expected_model_hash != hashes["model_hash"]
        or expected_voice_hash != hashes["voice_hash"]
        or expected_settings_hash != hashes["settings_hash"]
    ):
        raise RuntimeSnapshotError("Published provider and voice hashes do not match their pins")
    if schema_version < 3:
        raise RuntimeSnapshotError(
            "Published runtime version does not pin an LLM provider"
        )
    if extracted_settings.get("llm_provider") not in SUPPORTED_AGENT_LLM_PROVIDERS:
        raise RuntimeSnapshotError("Published LLM provider is not supported")
    if schema_version in {2, 3}:
        if model_for(
            "stt", extracted_settings["stt_provider"], extracted_settings["stt_model"]
        ) is None:
            raise RuntimeSnapshotError("Published STT provider and model are not registered")
        tts_model = model_for(
            "tts", extracted_settings["tts_provider"], extracted_settings["tts_model"]
        )
        if tts_model is None:
            raise RuntimeSnapshotError("Published TTS provider and model are not registered")
        supported_voices = {voice.id for voice in tts_model.voices}
        if any(
            voice and voice not in supported_voices
            for voice in (
                extracted_settings["tts_voice_en"],
                extracted_settings["tts_voice_hi"],
            )
        ):
            raise RuntimeSnapshotError(
                "Published TTS voice is not supported by its pinned model"
            )

    prompt_text = policy.get("prompt_text")
    if (
        not isinstance(prompt_text, str)
        or not prompt_text
        or not isinstance(policy.get("version_id"), str)
        or not isinstance(policy.get("version"), int)
        or not isinstance(policy.get("slug"), str)
        or policy.get("policy_hash") != hashes["policy_hash"]
        or compute_policy_hash(prompt_text) != hashes["policy_hash"]
    ):
        raise RuntimeSnapshotError("Published policy text does not match its pinned hash")
    system_instruction = compile_system_prompt(prompt_text, persona_prompt)
    if compute_base_prompt_hash(system_instruction) != hashes["base_prompt_hash"]:
        raise RuntimeSnapshotError("Published base prompt does not match its pinned hash")

    capabilities_raw = config.get("capabilities")
    if not isinstance(capabilities_raw, dict):
        raise RuntimeSnapshotError("A qualified voice adapter is required before speech")
    try:
        capabilities = AdapterCapabilities.model_validate(capabilities_raw)
    except Exception as exc:  # Pydantic reports a public schema error without exposing secrets.
        raise RuntimeSnapshotError("Published adapter capabilities are invalid") from exc
    report_hash = capabilities.report_hash
    if (
        not _HASH_RE.fullmatch(report_hash)
        or report_hash == _ZERO_HASH
        or capabilities.metadata == "none"
        or not capabilities.speech_gating
        or not capabilities.cancel_generation
        or capabilities.playback != "qualified_fragment_completion"
        or not {"en", "hi"}.issubset(set(capabilities.locales))
        or manifest.get("adapter_qualification_references") != report_hash
    ):
        raise RuntimeSnapshotError("A qualified, speech-gated EN/HI playback adapter is required")

    default_limits = FlowLimits()
    if manifest.get("limits_profile_hash") != compute_limits_hash(default_limits.model_dump()):
        raise RuntimeSnapshotError("Published flow limits do not match the installed runtime")
    try:
        compiled = compile_flow(
            flow=authoring_flow,
            policy=prompt_text,
            limits=default_limits,
            capabilities=capabilities,
            base_prompt_hash=hashes["base_prompt_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeSnapshotError("Published flow cannot be compiled by this runtime") from exc
    if (
        compiled.normalized != normalized_flow
        or compiled.manifest != manifest
        or compiled.flow_hash != view.flow_hash
        or compiled.artifact_hash != view.artifact_hash
        or compiled.flow_hash != hashes["flow_hash"]
        or compiled.artifact_hash != hashes["artifact_hash"]
    ):
        raise RuntimeSnapshotError("Published flow artifact does not match its registered hashes")

    detached = view.model_copy(deep=True)
    return RuntimeSnapshot(
        session_id=detached.session_id,
        owner_user_id=detached.owner_user_id,
        participant_id=detached.participant_id,
        auth_session_id=detached.auth_session_id,
        participant_kind=detached.participant_kind,
        origin=detached.origin,
        runner_id=detached.runner_id,
        registration_id=detached.registration_id,
        config_version_id=detached.config_version_id,
        session_version=detached.session_version,
        state=detached.state,
        connect_deadline=detached.connect_deadline,
        hard_end_deadline=detached.hard_end_deadline,
        budget=detached.budget.model_copy(deep=True),
        hashes=tuple(sorted(hashes.items())),
        capabilities=capabilities.model_copy(deep=True),
        compiled=compiled.model_copy(deep=True),
        model_pins=MappingProxyType(dict(extracted_settings)),
        policy_prompt=prompt_text,
        system_instruction=system_instruction,
    )



__all__ = ["RuntimeSnapshot", "RuntimeSnapshotError", "validate_runtime_snapshot"]
