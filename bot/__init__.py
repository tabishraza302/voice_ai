"""A fully local, air-gapped English + Hindi voice agent.

Layering, from the bottom up:

- ``bot.config``     — settings, one selector per swappable layer, and the
  system prompt
- ``bot.stt_service`` — STT against the local mlx-audio server
- ``bot.tts_service`` — TTS against the local mlx-audio server
- ``bot.language``   — script detection and the per-backend routers
- ``bot.services``   — the three registries
- ``bot.pipeline``   — the language-routing processor
- ``bot.bot``        — the runner entrypoint that assembles the pipeline
"""

__all__ = ["config", "language", "pipeline", "services", "stt_service", "tts_service"]
