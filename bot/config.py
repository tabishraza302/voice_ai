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
    # "mlx-audio" is the default: NVIDIA Nemotron 3.5 ASR Streaming 0.6B,
    # served by mlx_audio.server and consumed over its OpenAI-compatible
    # HTTP API. "mlx-whisper" and "funasr" are the registered alternates.
    stt_provider: str = "mlx-audio"
    stt_model: str = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    # "auto" | "en" | "hi". "auto" maps to Nemotron's language-ID prompt
    # conditioning, which is what survives code-switching.
    stt_language: str = "auto"
    stt_base_url: str = "http://localhost:8020/v1"
    # Whether the bot should start the STT server itself. Off by default so
    # the server can be restarted independently of the agent.
    stt_autostart: bool = False

    # --- LLM layer -------------------------------------------------------
    # "ollama" is the default. "openai-compatible" points at any local
    # OpenAI-compatible server (llama.cpp, vLLM, LM Studio).
    #
    # The model is Gemma 3, not Qwen 3, and that is a measured choice:
    # **qwen3 is a thinking model** ("thinking: default true" in
    # `ollama show`), and Ollama's OpenAI-compatible endpoint does not
    # forward `think: false` — nor `chat_template_kwargs` — so the
    # reasoning cannot be suppressed through Pipecat's Ollama service.
    # A trivial "say hello" spent 200 completion tokens on reasoning and
    # returned an *empty* answer, which for a voice agent is seconds of
    # dead air. Gemma 3 answers the same prompt in 13 tokens, in
    # Devanagari. See the README's "Why not Qwen 3" note.
    llm_provider: str = "ollama"
    llm_model: str = "gemma3:4b"
    llm_base_url: str = "http://localhost:11434/v1"
    # Minutes to keep the model resident after a request. 0 lets Ollama
    # unload it immediately: on a shared machine, permanently pinning 5.2 GB
    # is the wrong default. `make warm` pre-loads on demand instead.
    llm_keep_alive: str = "0"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 200

    # --- TTS layer -------------------------------------------------------
    # "kokoro" is the default. "mlx-audio" runs any mlx-audio TTS model
    # through the same local server that serves STT — including
    # mlx-community/chatterbox-multilingual-v3, the escape hatch if
    # Kokoro's grade-C Hindi voices prove unintelligible.
    tts_provider: str = "kokoro"
    tts_model: str = "mlx-community/chatterbox-multilingual-v3"
    tts_voice_en: str = "af_heart"
    tts_voice_hi: str = "hf_alpha"
    tts_speed: float = 1.0
    # The mlx-audio server's root URL (no /v1 suffix): the TTS endpoint is
    # POST /v1/audio/speech.
    tts_base_url: str = "http://localhost:8020"

    # --- Observability ---------------------------------------------------
    # The OTLP gRPC endpoint of the local collector. Not 4317: the
    # hear_help stack already holds 4317/4318 on this host.
    otlp_endpoint: str = "http://localhost:4319"
    phoenix_endpoint: str = "http://localhost:6006"
    enable_tracing: bool = True
    enable_metrics: bool = True

    # --- Server ----------------------------------------------------------
    host: str = "localhost"
    port: int = 7860

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
