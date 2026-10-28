"""Configuration for the voice agent.

One rule governs this file: **the pipeline never hardcodes a model.** Every
layer — STT, TTS, LLM — is selected by a provider name plus a model id, both
read from the environment. Changing a model is an edit to ``.env``, not a
code change.

That is only possible because the three layers are registry-driven (see
``bot/services.py``) and because the settings that flow between them are
built by per-backend routers (see ``bot/language.py``).
"""

from __future__ import annotations

import ipaddress
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The bot's behaviour, in one place. Shared by every LLM backend so that
# swapping the LLM cannot silently change what the bot says.
SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Reply in the same language the user "
    "speaks: English or Hindi. Keep replies under two sentences. Speak numbers "
    "and units naturally. Never use emoji, markdown, bullet points, or code "
    "formatting — your output is read aloud."
)


class Settings(BaseSettings):
    """Runtime configuration.

    Field names are the environment variable names (``env_prefix=""``), so
    ``stt_provider`` in ``.env`` lands in ``settings.stt_provider``.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- STT layer -------------------------------------------------------
    # "mlx-audio-realtime" is the default: NVIDIA Nemotron 3.5 ASR
    # Streaming 0.6B, served by mlx_audio.server over its streaming
    # WebSocket (/v1/realtime), so the turn is transcribed while the user
    # speaks. It has no language field, so it is auto-language only.
    # "mlx-audio" is the same checkpoint over the server's
    # OpenAI-compatible batch HTTP API, which does take a language.
    # "mlx-whisper" is the registered alternate.
    stt_provider: str = "mlx-audio-realtime"
    stt_model: str = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    # "auto" | "en" | "hi". The realtime default accepts only "auto"; on
    # the batch transport "auto" maps to Nemotron's language-ID prompt
    # conditioning, which is what survives code-switching.
    stt_language: str = "auto"
    stt_base_url: str = "http://localhost:8020/v1"
    # Whether the agent owns the mlx-audio server's lifetime: it starts
    # the server when it is not already up, and stops it when it exits.
    # Off by default so the server can be restarted independently of the
    # agent. A layer that needs the server and cannot reach it is fatal
    # either way — see needs_mlx_audio_server() in bot/bot.py.
    stt_autostart: bool = False

    # --- LLM layer -------------------------------------------------------
    # "ollama" is the default. "openai-compatible" points at any local
    # OpenAI-compatible server (llama.cpp, vLLM, LM Studio).
    #
    # Qwen3.5 4B is the default. It is a thinking model, and Ollama's
    # OpenAI-compatible endpoint does not forward `think: false`: without
    # the reasoning policy below, every turn spends its whole token budget
    # on hidden reasoning and returns no spoken answer. `reasoning_effort:
    # none` is the field Ollama does forward.
    llm_provider: str = "ollama"
    llm_model: str = "qwen3.5:4b"
    llm_base_url: str = "http://localhost:11434/v1"
    # How long Ollama keeps the model resident after a turn. "0" unloads
    # it immediately, which with the 12B tier is an 8 GB reload between
    # every turn. 5m is Ollama's own default; `scripts/voice-ai.sh warm`
    # pins the model for 30m on demand.
    llm_keep_alive: str = "5m"
    llm_temperature: float = 0.5
    llm_max_tokens: int = 200
    # The name the LLM server uses for the reply's length cap.
    # "max_tokens" is what llama.cpp, vLLM and LM Studio accept; the
    # gpt-5 family rejects it outright ("use max_completion_tokens
    # instead") and answers nothing.
    llm_max_tokens_parameter: str = "max_tokens"
    # Suppress hidden reasoning on the OpenAI-compatible endpoint. Ollama
    # forwards `reasoning_effort` even though it ignores `think`; "none"
    # is what a voice agent needs, because the reply is read aloud and
    # the token budget is small. An empty string omits the field, for a
    # server or model that rejects it.
    llm_reasoning_effort: str = "none"
    # The credential for an LLM provider that needs one. The
    # "openai-compatible" provider sends it as a bearer token; a local
    # server (llama.cpp, vLLM, LM Studio) needs no credential and
    # ignores it. `OPENAI_KEY` is accepted as an alias, so a key that
    # was put in the environment under that name keeps working.
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_KEY"),
    )
    # Compiled or default system instruction passed to the LLM backend
    system_instruction: str = SYSTEM_PROMPT

    # Local agent configuration storage. A configured PostgreSQL database
    # that cannot be reached is a startup failure by default: silently
    # writing agent edits to a different store makes them disappear when
    # PostgreSQL comes back. A deployment that deliberately accepts a
    # local-only SQLite store must opt in with ALLOW_SQLITE_FALLBACK=1;
    # /api/storage then reports the fallback, and sessions that request a
    # saved agent profile fail with 503 instead of reading the wrong store.
    database_url: str = "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai"
    sqlite_database_url: str = "sqlite+aiosqlite:///voice_ai.db"
    allow_sqlite_fallback: bool = False

    # --- TTS layer -------------------------------------------------------
    # "mlx-audio" is the default, and it is a measured choice: the median
    # time to first audio is 0.33 s through the local server against
    # 0.79 s for the in-process ONNX build (scripts/bench_tts.py, warm,
    # five sentences, this machine). It is the same Kokoro-82M with the
    # same voice packs, on the Metal GPU instead of the CPU. "kokoro" is
    # the alternate: in-process, needs no server, but slower to speak.
    tts_provider: str = "mlx-audio"
    # The model id reported in the metrics. It is a placeholder for
    # "whatever model this provider serves by default", not a checkpoint:
    # resolve_models maps it to the model actually loaded
    # (mlx-community/Kokoro-82M-4bit for mlx-audio).
    tts_model: str = "kokoro"
    # The Kokoro voice packs. None means "this model has no voice packs"
    # (the multilingual Chatterbox clones a voice from reference audio).
    tts_voice_en: str | None = "af_heart"
    tts_voice_hi: str | None = "hf_alpha"
    tts_speed: float = 1.0
    # The mlx-audio server's root URL (no /v1 suffix): the TTS endpoint is
    # POST /v1/audio/speech.
    tts_base_url: str = "http://localhost:8020"

    # How long the first sentence of a reply may take before the bot
    # speaks the first clause instead. The bot is silent until the
    # model finishes a sentence, and that wait is the largest single
    # part of the time to the first sound. Zero turns the cut off, which
    # is the behaviour before the deadline existed.
    first_chunk_deadline_seconds: float = 0.25

    # --- Observability ---------------------------------------------------
    # The OTLP gRPC endpoint of the local collector. Default 4319 avoids
    # standard 4317 port collisions on developer workstations. The collector
    # is what talks to Phoenix, so Phoenix's own address is configured in
    # observability/otel-collector.yaml.
    otlp_endpoint: str = "http://localhost:4319"
    enable_tracing: bool = True
    enable_metrics: bool = True

    # --- Server ----------------------------------------------------------
    host: str = "localhost"
    port: int = 7860

    @field_validator("host")
    @classmethod
    def validate_host_is_literal_loopback(cls, value: str) -> str:
        """Fail closed unless the host is a literal loopback address or localhost.

        Management routes (/api, /, /settings, /metrics) are unauthenticated in this
        local air-gapped system. Binding to wildcards, non-loopback addresses, or
        arbitrary domain names without DNS lookups is strictly forbidden.
        """
        if not isinstance(value, str):
            raise ValueError("host must be a string")
        candidate = value.strip()
        if candidate.lower() == "localhost":
            return candidate
        try:
            parsed = ipaddress.ip_address(candidate)
            if parsed.is_loopback:
                return candidate
        except ValueError:
            pass
        raise ValueError(
            f"host {value!r} is not allowed: unauthenticated local management routes "
            "must only bind to localhost or a literal loopback IP (127.0.0.1, ::1)"
        )

    # --- Measured behaviour ----------------------------------------------
    # P99 latency from end-of-speech to final transcript, in seconds.
    # Pipecat's built-in default (1.0) is a placeholder for unknown
    # hardware; scripts/bench_stt.py measures the real value and
    # scripts/apply_tier.py writes it here.
    ttfs_p99_latency: float = 1.0

    # Seconds of silence that closes a user turn. Pipecat's default is
    # 0.2 s, which split a real question at its first mid-sentence pause
    # ("...what languages do you understand and uh | what kind of ..."):
    # the STT segment ended, the bot replied to a fragment, and the tail
    # of the question became a second turn. 0.8 s costs up to 0.6 s more
    # end-of-turn latency in exchange for not cutting the user off. It is
    # the one latency knob to lower (0.6 is still better than 0.2) if the
    # README's published end-of-speech-to-bot number regresses.
    vad_stop_secs: float = 0.8

    # These match Pipecat 1.13.0 defaults. Tune per deployment through .env;
    # changing them can alter turn detection and requires a new measurement.
    vad_min_volume: float = Field(default=0.6, ge=0.0, le=1.0)
    vad_confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    # Local browser Studio port. The launch script exports this same setting
    # to both Next.js and the agent's CORS allowlist.
    web_port: int = Field(default=3001, ge=1, le=65535)
