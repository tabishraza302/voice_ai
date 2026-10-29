"""Strict envelope and canonical-fingerprint tests (no database required)."""

from datetime import datetime, timezone
import hashlib
import json
import uuid
import pytest

from platform_api.events.schemas import (
    EventEnvelope,
    EventValidationError,
    build_event,
    canonical_json,
    parse_event_body,
)


SESSION = uuid.uuid4()
EMITTED = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)


def _event(**overrides):
    values = dict(
        session_id=SESSION,
        sequence=1,
        event_type="transcript.segment",
        payload={
            "speaker": "user",
            "final_text": "hello",
            "start_ms": 0,
            "end_ms": 100,
        },
        emitted_at=EMITTED,
    )
    values.update(overrides)
    return build_event(**values)


def test_envelope_has_exact_transport_fields_and_canonical_body():
    envelope = _event()
    data = json.loads(envelope.to_json())
    assert set(data) == {
        "event_id",
        "session_id",
        "sequence",
        "schema_version",
        "event_type",
        "emitted_at",
        "payload_hash",
        "payload",
    }
    assert data["schema_version"] == 1
    assert data["emitted_at"] == "2026-01-02T03:04:05.123456Z"
    assert data["payload_hash"] == envelope.payload_hash
    assert envelope.to_json() == canonical_json(data)


def test_unknown_envelope_fields_are_rejected():
    data = json.loads(_event().to_json())
    data["unexpected"] = True
    with pytest.raises(EventValidationError):
        parse_event_body(canonical_json(data))


def test_unknown_payload_fields_are_rejected():
    data = json.loads(_event().to_json())
    data["payload"]["unexpected"] = True
    with pytest.raises(EventValidationError):
        parse_event_body(canonical_json(data))


def test_noncanonical_json_is_rejected():
    data = json.loads(_event().to_json())
    pretty = json.dumps(data, indent=2)
    with pytest.raises(EventValidationError):
        parse_event_body(pretty)


def test_duplicate_json_keys_are_rejected():
    body = _event().to_json()
    duplicate = body[:-1] + ',"sequence":1}'
    with pytest.raises(EventValidationError):
        parse_event_body(duplicate)


def test_unsupported_schema_version_is_rejected():
    data = json.loads(_event().to_json())
    data["schema_version"] = 2
    with pytest.raises(EventValidationError):
        parse_event_body(canonical_json(data))


def test_payload_type_validation_is_strict():
    with pytest.raises(EventValidationError):
        _event(payload={"speaker": "user", "final_text": "", "start_ms": 0, "end_ms": 1})
    with pytest.raises(EventValidationError):
        _event(
            payload={
                "speaker": "user",
                "final_text": "x",
                "start_ms": 5,
                "end_ms": 1,
            }
        )
    with pytest.raises(EventValidationError):
        _event(
            event_type="usage.provider",
            payload={
                "provider": "p",
                "model": "m",
                "kind": "k",
                "quantity": -1,
                "unit": "ms",
            },
        )


def test_fingerprint_is_stable_across_serialisation_round_trips():
    envelope = _event()
    parsed = parse_event_body(envelope.to_json_bytes())
    assert parsed.fingerprint == envelope.fingerprint
    assert parsed.payload_hash == envelope.payload_hash
    assert parsed.to_json() == envelope.to_json()


def test_payload_hash_must_match_payload():
    data = json.loads(_event().to_json())
    data["payload_hash"] = "0" * 64
    with pytest.raises(EventValidationError):
        parse_event_body(canonical_json(data))

    data = json.loads(_event().to_json())
    data["payload"]["final_text"] = "tampered"
    with pytest.raises(EventValidationError):
        parse_event_body(canonical_json(data))


def test_emitted_at_requires_timezone():
    with pytest.raises(EventValidationError):
        _event(emitted_at=datetime(2026, 1, 1))


def test_payload_hash_direct_mismatch_on_model_construction():
    with pytest.raises(EventValidationError, match="payload_hash does not match"):
        build_event(
            session_id=SESSION,
            sequence=1,
            event_type="transcript.segment",
            payload={
                "speaker": "user",
                "final_text": "hello",
                "start_ms": 0,
                "end_ms": 100,
            },
            payload_hash="f" * 64,
        )


def test_payload_hash_serialization_matches_canonical_payload():
    payload = {
        "speaker": "assistant",
        "final_text": "world",
        "start_ms": 100,
        "end_ms": 500,
    }
    envelope = build_event(
        session_id=SESSION,
        sequence=2,
        event_type="transcript.segment",
        payload=payload,
    )
    wire_json = envelope.to_json()
    data = json.loads(wire_json)
    assert "payload_hash" in data
    assert data["payload_hash"] == envelope.payload_hash
    expected_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert envelope.payload_hash == expected_hash

    parsed = parse_event_body(wire_json)
    assert parsed.payload_hash == expected_hash


def test_canonical_json_rejects_floating_point_numbers():
    with pytest.raises(EventValidationError, match="floating-point JSON numbers are forbidden"):
        canonical_json({"rate": 1.5})

    with pytest.raises(EventValidationError, match="floating-point JSON numbers are forbidden"):
        canonical_json({"whole_float": 1.0})


def test_canonical_json_rejects_integers_outside_safe_range():
    with pytest.raises(EventValidationError, match="integer outside safe range"):
        canonical_json({"too_large": 9007199254740992})

    with pytest.raises(EventValidationError, match="integer outside safe range"):
        canonical_json({"too_small": -9007199254740992})

    # Boundary values inside safe range must succeed
    valid = {"min": -9007199254740991, "max": 9007199254740991, "zero": 0}
    assert canonical_json(valid) == '{"max":9007199254740991,"min":-9007199254740991,"zero":0}'
