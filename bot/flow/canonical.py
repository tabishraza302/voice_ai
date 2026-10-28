"""Canonical JSON v1 encoding and SHA-256 preimages per flow contract section 4.

Cross-language deterministic encoding with golden vectors:
- Ascii-byte key sorting
- Integer-only numeric values (safe integers -9007199254740991..9007199254740991)
- Rejects floats (including 1.0, 1e0, NaN, Infinity)
- Unescaped forward slash
- Control character escapes (\\", \\\\, \\b, \\f, \\n, \\r, \\t, \\u00xx)
- Literal UTF-8 for other Unicode scalar values
- No BOM, spaces, or trailing newline
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from bot.flow.schemas import MAX_SAFE_INTEGER, MIN_SAFE_INTEGER

AUTHORING_PREFIX = b"voice-ai/flow-authoring/v1\0"
NORMALIZED_PREFIX = b"voice-ai/flow-normalized/v1\0"
ARTIFACT_PREFIX = b"voice-ai/flow-artifact/v1\0"
LIMITS_PREFIX = b"voice-ai/flow-limits/v1\0"
POLICY_PREFIX = b"voice-ai/policy/v1\0"


def _encode_string(s: str) -> str:
    parts = ['"']
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            parts.append('\\"')
        elif ch == "\\":
            parts.append("\\\\")
        elif ch == "\b":
            parts.append("\\b")
        elif ch == "\f":
            parts.append("\\f")
        elif ch == "\n":
            parts.append("\\n")
        elif ch == "\r":
            parts.append("\\r")
        elif ch == "\t":
            parts.append("\\t")
        elif cp < 0x20:
            parts.append(f"\\u{cp:04x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _encode_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # Floats, including NaN, Infinity, 1.0, fail
        if isinstance(value, float):
            raise ValueError(f"Floating-point JSON numbers are forbidden in canonical_json_v1: {value}")
        # Negative zero check (in case int has sign)
        if value == 0:
            return "0"
        if value < MIN_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
            raise ValueError(f"Integer outside safe range [{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}]: {value}")
        return str(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode_value(item) for item in value) + "]"
    if isinstance(value, dict):
        # Keys sorted in ascending ASCII byte order
        # All keys must be strings
        sorted_keys = sorted(value.keys(), key=lambda k: str(k).encode("utf-8"))
        items = []
        for k in sorted_keys:
            if not isinstance(k, str):
                raise TypeError(f"Dictionary keys must be strings, got {type(k)}")
            items.append(f"{_encode_string(k)}:{_encode_value(value[k])}")
        return "{" + ",".join(items) + "}"
    raise TypeError(f"Unsupported type for canonical_json_v1: {type(value)}")


def canonical_json_v1(value: Any) -> bytes:
    """Encode JSON-compatible structure into canonical UTF-8 bytes."""
    return _encode_value(value).encode("utf-8")


def sha256_canonical(prefix: bytes, value: Any) -> str:
    """Compute 64-hex SHA-256 over prefix + canonical_json_v1(value)."""
    encoded = canonical_json_v1(value)
    return hashlib.sha256(prefix + encoded).hexdigest()


def compute_authoring_hash(source_flow: dict[str, Any]) -> str:
    return sha256_canonical(AUTHORING_PREFIX, source_flow)


def compute_flow_hash(normalized_flow: dict[str, Any]) -> str:
    return sha256_canonical(NORMALIZED_PREFIX, normalized_flow)


def compute_artifact_hash(manifest: dict[str, Any]) -> str:
    return sha256_canonical(ARTIFACT_PREFIX, manifest)


def compute_limits_hash(limits_dict: dict[str, Any]) -> str:
    return sha256_canonical(LIMITS_PREFIX, limits_dict)
