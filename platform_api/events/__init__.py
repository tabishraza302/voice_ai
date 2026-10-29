"""Durable session events: strict envelope, projections and consumer.

Importing this package is cheap and safe: it does not import SQLAlchemy,
FastAPI, the control-plane models or any GPU service.  Consumers that need the
database import :mod:`platform_api.events.repository` explicitly.
"""

from platform_api.events.schemas import (
    SCHEMA_VERSION,
    SUPPORTED_EVENT_TYPES,
    EventConflict,
    EventEnvelope,
    EventType,
    EventValidationError,
    build_event,
    canonical_json,
    parse_event_body,
)

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_EVENT_TYPES",
    "EventConflict",
    "EventEnvelope",
    "EventType",
    "EventValidationError",
    "build_event",
    "canonical_json",
    "parse_event_body",
]
