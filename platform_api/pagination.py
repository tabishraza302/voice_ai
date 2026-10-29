"""Bounded keyset cursors for private, filter-bound API pages."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from typing import Any


MAX_CURSOR_LENGTH = 1024
MAX_QUERY_DATE_RANGE = timedelta(days=366)
_CURSOR_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")


class PaginationError(ValueError):
    """A cursor or date range is invalid for the requested page."""


@dataclass(frozen=True)
class KeysetPosition:
    created_at: datetime
    record_id: uuid.UUID


def normalize_date_range(
    from_at: datetime | None,
    to_at: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Require paired timezone-aware bounds no more than 366 days apart."""
    if (from_at is None) != (to_at is None):
        raise PaginationError("from_at and to_at must be supplied together")
    if from_at is None or to_at is None:
        return None, None
    if from_at.tzinfo is None or from_at.utcoffset() is None:
        raise PaginationError("from_at must include a timezone")
    if to_at.tzinfo is None or to_at.utcoffset() is None:
        raise PaginationError("to_at must include a timezone")
    start = from_at.astimezone(timezone.utc)
    end = to_at.astimezone(timezone.utc)
    if end <= start:
        raise PaginationError("to_at must be later than from_at")
    if end - start > MAX_QUERY_DATE_RANGE:
        raise PaginationError("date range cannot exceed 366 days")
    return start, end


def filter_fingerprint(filters: dict[str, Any]) -> str:
    """Hash normalized query filters so a cursor cannot switch query scope."""
    canonical = json.dumps(
        filters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def encode_keyset_cursor(
    *,
    scope: str,
    filters: dict[str, Any],
    created_at: datetime,
    record_id: uuid.UUID,
) -> str:
    """Encode the last returned row for a created_at/id descending page."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    payload = {
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "filter_hash": filter_fingerprint(filters),
        "id": str(record_id),
        "scope": scope,
        "v": 1,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_keyset_cursor(
    cursor: str,
    *,
    scope: str,
    filters: dict[str, Any],
) -> KeysetPosition:
    """Decode and bind a cursor to its route, filters, and stable keyset."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise PaginationError("cursor must be between 1 and 1024 characters")
    if _CURSOR_TOKEN.fullmatch(cursor) is None:
        raise PaginationError("cursor is malformed")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PaginationError("cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "created_at",
        "filter_hash",
        "id",
        "scope",
        "v",
    }:
        raise PaginationError("cursor has an invalid shape")
    if type(payload["v"]) is not int or payload["v"] != 1:
        raise PaginationError("cursor version is unsupported")
    if payload["scope"] != scope or payload["filter_hash"] != filter_fingerprint(filters):
        raise PaginationError("cursor does not match this query")
    if not isinstance(payload["created_at"], str) or not isinstance(payload["id"], str):
        raise PaginationError("cursor position is malformed")
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
        record_id = uuid.UUID(payload["id"])
    except (ValueError, TypeError) as exc:
        raise PaginationError("cursor position is malformed") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise PaginationError("cursor timestamp must include a timezone")
    return KeysetPosition(
        created_at=created_at.astimezone(timezone.utc),
        record_id=record_id,
    )
