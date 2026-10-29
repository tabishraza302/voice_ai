"""Authorized-runtime glue.

The first slice is the pipeline seam: ``build_voice_pipeline`` is the one
place the voice pipeline is composed, shared by the local entrypoint and
the later authorized runtime. It exports no platform session, admission,
billing or transport type.
"""

from bot.runtime.pipeline_builder import build_voice_pipeline

__all__ = ["build_voice_pipeline"]
