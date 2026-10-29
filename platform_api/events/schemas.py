"""Strict durable-event envelope, payload schemas and canonical fingerprints.

The transport envelope is exactly:

    {event_id, session_id, sequence, schema_version, event_type, emitted_at,
     payload_hash, payload}

Rules owned here (implementation plan, shared interface decisions):

* every object rejects unknown keys;
* ``sequence`` is a positive session-wide integer allocated by the producer
  spool, not by the consumer;
* ``emitted_at`` is timezone-aware UTC and normalised to microseconds;
* the wire body is canonical JSON (sorted keys, compact separators, UTF-8);
* ``payload`` is validated per ``event_type``;
* the event fingerprint covers the whole canonical envelope, and the payload
  hash covers the canonical payload.  Retrying an event must reproduce both.

This module deliberately imports no database, FastAPI, GPU or bot modules so
the voice runtime can use it without loading the control plane.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Literal, Mapping
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})

# Storage ceilings.  These are deliberately enforced before a body is accepted
# by the spool or the consumer, so a malformed producer cannot grow a spool or
# quarantine row without bound.
MAX_BODY_BYTES = 128 * 1024
MAX_PAYLOAD_BYTES = 96 * 1024
MAX_TEXT_CHARS = 8192
MAX_IDENTIFIER_CHARS = 128
MAX_REASON_CHARS = 256

MIN_SAFE_INTEGER = -9007199254740991
MAX_SAFE_INTEGER = 9007199254740991

EventType = Literal[
    "transcript.segment",
    "usage.provider",
    "session.lifecycle",
    "transcript.gap",
]

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "transcript.segment",
        "usage.provider",
        "session.lifecycle",
        "transcript.gap",
    }
)


class EventValidationError(ValueError):
    """The event body is not a valid, canonical envelope."""


class EventConflict(ValueError):
    """The same event identity was reused with different content."""


def _reject_non_finite(value: str) -> Any:
    raise EventValidationError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EventValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _canonical_value(value: Any, *, path: str = "$") -> Any:
    """Return a JSON-safe canonical copy, rejecting non-string keys and NaN."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise EventValidationError(
            f"{path}: floating-point JSON numbers are forbidden in canonical JSON: {value}"
        )
    if isinstance(value, int):
        if value < MIN_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
            raise EventValidationError(
                f"{path}: integer outside safe range [{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}]: {value}"
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path}: JSON object keys must be strings")
            result[key] = _canonical_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _canonical_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise EventValidationError(
        f"{path}: unsupported JSON value type {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Serialise a value with the single canonical JSON rule used everywhere."""
    canonical = _canonical_value(value)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EventValidationError("payload must be a JSON object")
    result = _canonical_value(dict(payload))
    if not isinstance(result, dict):  # pragma: no cover - defensive
        raise EventValidationError("payload must be a JSON object")
    encoded = canonical_bytes(result)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise EventValidationError(
            f"payload exceeds maximum of {MAX_PAYLOAD_BYTES} bytes"
        )
    return result


def payload_hash_for(payload: Mapping[str, Any]) -> str:
    """Hash the canonical payload exactly as it is carried on the wire."""
    return hashlib.sha256(canonical_bytes(canonical_payload(payload))).hexdigest()


def normalize_emitted_at(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise EventValidationError("emitted_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventValidationError("emitted_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def emitted_at_text(value: datetime) -> str:
    """Canonical UTC timestamp with a stable microsecond representation."""
    return normalize_emitted_at(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TranscriptSegmentPayload(_StrictPayload):
    """A finalized transcript segment.  Hypotheses are never durable history."""

    speaker: Literal["user", "assistant", "system"]
    final_text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    kind: Literal["utterance", "welcome", "correction"] = "utterance"

    @model_validator(mode="after")
    def _check_span(self) -> "TranscriptSegmentPayload":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class ProviderUsagePayload(_StrictPayload):
    provider: str = Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    model: str = Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    kind: str = Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS)
    quantity: int = Field(ge=0)
    unit: str = Field(min_length=1, max_length=32)


class SessionLifecyclePayload(_StrictPayload):
    state: Literal[
        "started",
        "active",
        "reconnecting",
        "completed",
        "failed",
        "ended",
    ]
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class TranscriptGapPayload(_StrictPayload):
    """An explicit, operator-visible gap.  Text is never fabricated for it."""

    after_sequence: int = Field(ge=0)
    before_sequence: int = Field(ge=1)
    reason: Literal["timeout", "producer_loss", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _check_order(self) -> "TranscriptGapPayload":
        if self.before_sequence <= self.after_sequence:
            raise ValueError("before_sequence must be greater than after_sequence")
        return self


PAYLOAD_MODELS: dict[str, type[_StrictPayload]] = {
    "transcript.segment": TranscriptSegmentPayload,
    "usage.provider": ProviderUsagePayload,
    "session.lifecycle": SessionLifecyclePayload,
    "transcript.gap": TranscriptGapPayload,
}


def validate_payload(event_type: str, payload: Mapping[str, Any]) -> _StrictPayload:
    model = PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise EventValidationError(f"unsupported event_type: {event_type!r}")
    canonical = canonical_payload(payload)
    try:
        return model.model_validate(canonical)
    except Exception as exc:
        raise EventValidationError(
            f"invalid payload for {event_type}: {exc}"
        ) from exc


class EventEnvelope(BaseModel):
    """The strict durable event envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    session_id: UUID
    sequence: int = Field(ge=1)
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    event_type: EventType
    emitted_at: datetime
    payload_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the canonical payload",
    )
    payload: dict[str, Any]

    @field_validator("event_id", "session_id", mode="before")
    @classmethod
    def _coerce_uuid(cls, value: Any) -> UUID:
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value)
            except ValueError as exc:
                raise ValueError("must be a UUID") from exc
        raise ValueError("must be a UUID")

    @field_validator("sequence", "schema_version", mode="before")
    @classmethod
    def _strict_int(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer")
        return value

    @field_validator("emitted_at", mode="before")
    @classmethod
    def _parse_emitted_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            raw = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                value = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("emitted_at must be an ISO-8601 timestamp") from exc
        return normalize_emitted_at(value)

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload_object(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("payload must be a JSON object")
        return canonical_payload(value)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "EventEnvelope":
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {self.schema_version}; "
                f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if self.event_type not in SUPPORTED_EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {self.event_type!r}")
        validate_payload(self.event_type, self.payload)
        expected_hash = payload_hash_for(self.payload)
        if self.payload_hash != expected_hash:
            raise ValueError(
                "payload_hash does not match the canonical payload "
                f"(expected {expected_hash})"
            )
        if len(self.to_json_bytes()) > MAX_BODY_BYTES:
            raise ValueError(
                f"event body exceeds maximum of {MAX_BODY_BYTES} bytes"
            )
        return self

    @property
    def typed_payload(self) -> _StrictPayload:
        return validate_payload(self.event_type, self.payload)

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "session_id": str(self.session_id),
            "sequence": self.sequence,
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "emitted_at": emitted_at_text(self.emitted_at),
            "payload_hash": self.payload_hash,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_canonical_dict())

    def to_json_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()


def build_event(
    *,
    session_id: UUID | str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    event_id: UUID | str | None = None,
    emitted_at: datetime | None = None,
    schema_version: int = SCHEMA_VERSION,
    payload_hash: str | None = None,
) -> EventEnvelope:
    """Build a validated envelope.  The caller owns the sequence decision."""
    try:
        return EventEnvelope(
            event_id=event_id or uuid4(),
            session_id=session_id,
            sequence=sequence,
            schema_version=schema_version,
            event_type=event_type,
            emitted_at=emitted_at or datetime.now(timezone.utc),
            payload_hash=payload_hash or payload_hash_for(payload),
            payload=payload,
        )
    except EventValidationError:
        raise
    except Exception as exc:
        raise EventValidationError(f"invalid event envelope: {exc}") from exc


def parse_event_body(body: bytes | str) -> EventEnvelope:
    """Parse and validate a canonical wire body.

    Non-canonical JSON, duplicate keys, unknown envelope fields, unsupported
    schema versions, NaN and oversized bodies all fail closed.
    """
    if isinstance(body, str):
        raw = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    else:
        raise EventValidationError("event body must be bytes or str")

    if len(raw) > MAX_BODY_BYTES:
        raise EventValidationError(
            f"event body exceeds maximum of {MAX_BODY_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EventValidationError("event body is not valid UTF-8") from exc

    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except EventValidationError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise EventValidationError(f"event body is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise EventValidationError("event body must be a JSON object")

    try:
        envelope = EventEnvelope.model_validate(data)
    except Exception as exc:
        raise EventValidationError(f"invalid event envelope: {exc}") from exc

    # The fingerprint contract only has one spelling.  A body with different
    # whitespace, key order or escaping must not produce a second identity.
    if envelope.to_json() != text:
        raise EventValidationError("event body is not canonical JSON")
    return envelope


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SUPPORTED_EVENT_TYPES",
    "MAX_BODY_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_TEXT_CHARS",
    "MIN_SAFE_INTEGER",
    "MAX_SAFE_INTEGER",
    "EventType",
    "EventValidationError",
    "EventConflict",
    "EventEnvelope",
    "TranscriptSegmentPayload",
    "ProviderUsagePayload",
    "SessionLifecyclePayload",
    "TranscriptGapPayload",
    "canonical_json",
    "canonical_bytes",
    "canonical_payload",
    "payload_hash_for",
    "normalize_emitted_at",
    "emitted_at_text",
    "validate_payload",
    "build_event",
    "parse_event_body",
]
