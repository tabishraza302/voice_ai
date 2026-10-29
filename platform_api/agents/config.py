"""Agent configuration envelope and deterministic pin hashes.

A draft's ``config`` may be either a bare flow document (the form used by the
flow editor) or an envelope::

    {"flow": {...}, "llm_model": "...", "stt_provider": "...",
     "tts_voice_en": "...", "tts_voice_hi": "..."}

Both forms are accepted so existing clients keep working, while provider and
voice selections remain outside the flow schema (which forbids unknown root
properties). An explicitly empty pair of TTS voices selects text-only mode.
The original submitted config is what the authoring hash covers; the flow alone
is what the flow compiler normalizes and hashes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bot.flow.canonical import canonical_json_v1

DEFAULT_AGENT_SETTINGS: dict[str, str] = {
    "llm_model": "qwen3.5:4b",
    "stt_provider": "mlx-audio",
    "tts_voice_en": "af_heart",
    "tts_voice_hi": "hf_alpha",
}
AGENT_SETTING_KEYS = frozenset(DEFAULT_AGENT_SETTINGS)
TTS_VOICE_SETTING_KEYS = frozenset(("tts_voice_en", "tts_voice_hi"))
AGENT_SETTING_MAX_LENGTHS = {
    "llm_model": 64,
    "stt_provider": 32,
    "tts_voice_en": 32,
    "tts_voice_hi": 32,
}
DEFAULT_AGENT_CONFIG_MAX_BYTES = 262_144

MODEL_PIN_PREFIX = b"voice-ai/model-pins/v1\0"
VOICE_PIN_PREFIX = b"voice-ai/voice-pins/v1\0"
SETTINGS_PIN_PREFIX = b"voice-ai/agent-settings/v1\0"


class AgentConfigError(ValueError):
    """Raised when a draft config envelope is malformed."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.path = path


def is_envelope(config: dict[str, Any]) -> bool:
    """Return True when ``config`` uses the explicit ``{"flow": ...}`` envelope."""
    return isinstance(config.get("flow"), dict)


def extract_flow(config: dict[str, Any]) -> dict[str, Any]:
    """Return the flow document from a bare or enveloped agent config."""
    if not isinstance(config, dict):
        raise AgentConfigError("Agent config must be a JSON object")
    if "flow" in config:
        flow = config["flow"]
        if not isinstance(flow, dict):
            raise AgentConfigError("Agent config 'flow' must be an object", "/flow")
        return flow
    return {
        key: value
        for key, value in config.items()
        if key not in AGENT_SETTING_KEYS
    }


def extract_settings(config: dict[str, Any]) -> dict[str, str]:
    """Return validated provider/voice settings with platform defaults applied.

    Set both TTS voice values to empty strings to explicitly select text-only
    publishing. Configuring only one voice is invalid.
    """
    if not isinstance(config, dict):
        raise AgentConfigError("Agent config must be a JSON object")

    if "flow" in config:
        raw = {key: value for key, value in config.items() if key != "flow"}
        unknown = sorted(set(raw) - AGENT_SETTING_KEYS)
        if unknown:
            raise AgentConfigError(
                f"Unknown agent config setting(s): {', '.join(unknown)}",
                f"/{unknown[0]}",
            )
    else:
        raw = {
            key: value
            for key, value in config.items()
            if key in AGENT_SETTING_KEYS
        }

    settings = dict(DEFAULT_AGENT_SETTINGS)
    for key, value in raw.items():
        if not isinstance(value, str) or (
            not value.strip() and key not in TTS_VOICE_SETTING_KEYS
        ):
            raise AgentConfigError(
                f"Agent config setting '{key}' must be a non-empty string",
                f"/{key}",
            )
        if len(value) > AGENT_SETTING_MAX_LENGTHS[key]:
            raise AgentConfigError(
                f"Agent config setting '{key}' exceeds its maximum length",
                f"/{key}",
            )
        settings[key] = value.strip()

    voices = tuple(settings[key] for key in TTS_VOICE_SETTING_KEYS)
    if any(voices) and not all(voices):
        raise AgentConfigError(
            "Both TTS voices must be configured for voice publishing or "
            "both left empty for text-only",
            "/tts_voice_en",
        )
    return settings


def validate_config_bounds(
    config: dict[str, Any],
    max_bytes: int = DEFAULT_AGENT_CONFIG_MAX_BYTES,
) -> None:
    """Reject settings or full submitted configs that exceed authoring limits."""
    extract_settings(config)
    try:
        config_size = len(json.dumps(config).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise AgentConfigError("Agent config must contain valid JSON values") from exc
    if config_size > max_bytes:
        raise AgentConfigError(
            f"Agent config exceeds maximum wire size of {max_bytes} bytes"
        )


def _pin_hash(prefix: bytes, value: Any) -> str:
    return hashlib.sha256(prefix + canonical_json_v1(value)).hexdigest()


def compute_settings_hash(settings: dict[str, str]) -> str:
    return _pin_hash(SETTINGS_PIN_PREFIX, settings)


def compute_model_hash(settings: dict[str, str]) -> str:
    return _pin_hash(
        MODEL_PIN_PREFIX,
        {
            "llm_model": settings["llm_model"],
            "stt_provider": settings["stt_provider"],
        },
    )


def compute_voice_hash(settings: dict[str, str]) -> str:
    return _pin_hash(
        VOICE_PIN_PREFIX,
        {
            "tts_voice_en": settings["tts_voice_en"],
            "tts_voice_hi": settings["tts_voice_hi"],
        },
    )
