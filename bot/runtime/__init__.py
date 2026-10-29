"""Authorized runtime glue for the shared pipeline and durable event spool.

The pipeline builder is the one composition point shared by the local runner
and authorized runtime. Model services remain behind the existing registries.
"""

from bot.runtime.outbox import (
    EventSpool,
    PublishBatchResult,
    SpoolCapacity,
    SpoolCapacityError,
    SpoolCorruptionError,
    SpoolRecord,
)
from bot.runtime.pipeline_builder import build_voice_pipeline

__all__ = [
    "build_voice_pipeline",
    "EventSpool",
    "PublishBatchResult",
    "SpoolCapacity",
    "SpoolCapacityError",
    "SpoolCorruptionError",
    "SpoolRecord",
]
