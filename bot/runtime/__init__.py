"""Authorized voice-runtime glue.

This package is intentionally free of model imports at module import time;
the concrete STT/TTS/LLM services remain behind the existing registries.
"""

from bot.runtime.outbox import (
    EventSpool,
    PublishBatchResult,
    SpoolCapacity,
    SpoolCapacityError,
    SpoolCorruptionError,
    SpoolRecord,
)

__all__ = [
    "EventSpool",
    "PublishBatchResult",
    "SpoolCapacity",
    "SpoolCapacityError",
    "SpoolCorruptionError",
    "SpoolRecord",
]
