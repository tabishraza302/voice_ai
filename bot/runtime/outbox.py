"""Durable JSONL journal for critical runtime events.

Capture, sequence allocation and publisher acknowledgements are fsynced
records. Confirmed events are reclaimed by crash-safe compaction; a partial
final record is truncated during recovery, while a complete invalid record
fails closed. Acknowledgements are recorded only after mandatory routing and
broker confirmation.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Protocol
import uuid

from platform_api.events.schemas import (
    MAX_BODY_BYTES, EventConflict, EventEnvelope,
    build_event, canonical_json, normalize_emitted_at, parse_event_body,
)


class SpoolError(RuntimeError):
    """Base class for spool failures."""


class SpoolCapacityError(SpoolError):
    """The journal has reached an admission ceiling."""


class SpoolCorruptionError(SpoolError):
    """A complete journal record is corrupt or inconsistent."""


class SpoolClosedError(SpoolError):
    """The journal was used after close."""


class PublishNotConfirmed(SpoolError):
    """The publisher did not return a durable confirmation."""


@dataclass(frozen=True)
class SpoolRecord:
    durable_position: int
    event_id: uuid.UUID
    session_id: uuid.UUID
    sequence: int
    body: bytes
    envelope: EventEnvelope
    created_at: datetime

    @property
    def payload_hash(self) -> str:
        return self.envelope.payload_hash


@dataclass(frozen=True)
class SpoolCapacity:
    events: int
    bytes: int
    max_events: int
    max_bytes: int
    free_bytes: int
    min_free_bytes: int
    saturated: bool
    drain_threshold_events: int = 0
    drain_threshold_bytes: int = 0
    needs_drain: bool = False


@dataclass(frozen=True)
class PublishBatchResult:
    published: int
    remaining: int
    blocked: bool
    error: str | None = None
    retry_after_seconds: float = 0.0


class EventPublisher(Protocol):
    async def publish(self, body: bytes, *, routing_key: str,
                      exchange: str | None = None,
                      mandatory: bool = True) -> Any: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DEFAULT_SPOOL_DIR = Path("data/runtime/spool")


def spool_path_for_runner(runner_id: str | uuid.UUID,
                          base_dir: Path | str = DEFAULT_SPOOL_DIR) -> Path:
    """Return the runner's append-only journal beneath its private spool dir."""
    return Path(base_dir) / str(runner_id) / "journal.jsonl"


class EventSpool:
    """Bounded durable journal with append-only records between compactions."""

    def __init__(self, path: str | Path | None = None, *,
                 runner_id: str | uuid.UUID | None = None,
                 max_events: int = 100_000,
                 max_bytes: int = 512 * 1024 * 1024,
                 min_free_bytes: int = 64 * 1024 * 1024,
                 drain_threshold_events: int | None = None,
                 drain_threshold_bytes: int | None = None,
                 drain_threshold_ratio: float = 0.8,
                 stop_admission_on_drain: bool = False,
                 publisher: EventPublisher | None = None,
                 routing_key_prefix: str = "session.") -> None:
        if path is None:
            if runner_id is None:
                raise ValueError("either path or runner_id must be provided")
            path = spool_path_for_runner(runner_id)
        if max_events <= 0 or max_bytes <= 0 or min_free_bytes < 0:
            raise ValueError("spool ceilings must be positive")
        self.path = Path(path)
        self._compaction_temp = self.path.with_name(
            f".{self.path.name}.compact.tmp"
        )
        self.runner_id = str(runner_id) if runner_id is not None else None
        self.max_events, self.max_bytes = max_events, max_bytes
        self.min_free_bytes = min_free_bytes
        self.drain_threshold_ratio = drain_threshold_ratio
        self.drain_threshold_events = drain_threshold_events or max(1, int(max_events * drain_threshold_ratio))
        self.drain_threshold_bytes = drain_threshold_bytes or max(1, int(max_bytes * drain_threshold_ratio))
        self.stop_admission_on_drain = stop_admission_on_drain
        self._publisher = publisher
        self.routing_key_prefix = routing_key_prefix
        self._lock = asyncio.Lock()
        self._lock_fd: int | None = None
        self._open = False
        self._corrupt_count = 0
        self._records: dict[int, SpoolRecord] = {}
        self._event_positions: dict[uuid.UUID, int] = {}
        self._session_sequences: dict[uuid.UUID, set[int]] = {}
        self._acked: set[int] = set()
        self._attempts: dict[int, int] = {}
        self._session_sequence_watermarks: dict[uuid.UUID, int] = {}
        self._pending_events = 0
        self._pending_body_bytes = 0
        self._next_position = 1
        self._journal_has_checkpoint = False
        self._checkpoint_next_position = 1
        self._last_event_position = 0
        self._journal_identity: tuple[int, int] | None = None
        self._journal_size: int | None = None
        self._journal_mtime_ns: int | None = None
        self._journal_line_count = 0

    @classmethod
    def _ensure_directory(cls, directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for child in reversed(missing):
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            cls._fsync_directory(child.parent)

    async def open(self) -> "EventSpool":
        if self._open:
            return self
        self._ensure_directory(self.path.parent)
        os.chmod(self.path.parent, 0o700)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        self._fsync_directory(self.path.parent)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._fsync_directory(self.path.parent)
        os.chmod(self.path, 0o600)
        self._open = True
        try:
            with self._file_lock():
                self._discard_compaction_temp()
                self._recover()
        except Exception:
            os.close(self._lock_fd)
            self._lock_fd = None
            self._open = False
            raise
        return self

    async def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
        self._lock_fd = None
        self._open = False

    async def __aenter__(self) -> "EventSpool":
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _require_open(self) -> None:
        if not self._open or self._lock_fd is None:
            raise SpoolClosedError("spool is not open")

    class _FileLock:
        def __init__(self, fd: int): self.fd = fd
        def __enter__(self): fcntl.flock(self.fd, fcntl.LOCK_EX)
        def __exit__(self, *_: object): fcntl.flock(self.fd, fcntl.LOCK_UN)

    def _file_lock(self) -> "EventSpool._FileLock":
        self._require_open()
        return self._FileLock(self._lock_fd)  # type: ignore[arg-type]

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def _discard_compaction_temp(self) -> None:
        try:
            self._compaction_temp.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self.path.parent)

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("journal write made no forward progress")
            view = view[written:]

    @staticmethod
    def _journal_line(item: dict[str, Any]) -> bytes:
        return (canonical_json(item) + "\n").encode("utf-8")

    def _append_line(self, item: dict[str, Any]) -> int:
        raw = self._journal_line(item)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        try:
            position = os.lseek(fd, 0, os.SEEK_END)
            self._write_all(fd, raw)
            os.fsync(fd)
            info = os.fstat(fd)
            self._journal_identity = (info.st_dev, info.st_ino)
            self._journal_size = info.st_size
            self._journal_mtime_ns = info.st_mtime_ns
            self._journal_line_count += 1
            return position
        finally:
            os.close(fd)


    def _recover(self) -> None:
        current = self.path.stat()
        identity = (current.st_dev, current.st_ino)
        same_file = identity == self._journal_identity
        if (
            same_file
            and self._journal_size == current.st_size
            and self._journal_mtime_ns == current.st_mtime_ns
        ):
            return

        incremental = (
            same_file
            and self._journal_size is not None
            and current.st_size > self._journal_size
        )
        if incremental:
            offset = self._journal_size
            line_number = self._journal_line_count
        else:
            self._records.clear()
            self._event_positions.clear()
            self._session_sequences.clear()
            self._session_sequence_watermarks.clear()
            self._acked.clear()
            self._attempts.clear()
            self._pending_events = 0
            self._pending_body_bytes = 0
            self._next_position = 1
            self._journal_has_checkpoint = False
            self._checkpoint_next_position = 1
            self._last_event_position = 0
            self._journal_line_count = 0
            offset = 0
            line_number = 0

        try:
            with self.path.open("r+b") as journal:
                journal.seek(offset)
                while True:
                    line_start = journal.tell()
                    raw_line = journal.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n"):
                        journal.truncate(line_start)
                        journal.flush()
                        os.fsync(journal.fileno())
                        self._fsync_directory(self.path.parent)
                        break

                    line_number += 1
                    line = raw_line[:-1]
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            raise ValueError("journal record must be an object")
                        if canonical_json(item).encode("utf-8") != line:
                            raise ValueError("journal record is not canonical JSON")
                        kind = item["kind"]
                        if kind == "checkpoint":
                            expected_fields = {
                                "kind",
                                "version",
                                "next_position",
                                "session_sequence_watermarks",
                            }
                            if line_number != 1 or self._journal_has_checkpoint:
                                raise ValueError("checkpoint must be the first record")
                            if (
                                set(item) != expected_fields
                                or type(item["version"]) is not int
                                or item["version"] != 1
                            ):
                                raise ValueError("invalid journal checkpoint")
                            next_position = item["next_position"]
                            if (
                                isinstance(next_position, bool)
                                or not isinstance(next_position, int)
                                or next_position < 1
                            ):
                                raise ValueError("invalid checkpoint position")
                            watermarks = item["session_sequence_watermarks"]
                            if not isinstance(watermarks, dict):
                                raise ValueError("invalid checkpoint watermarks")
                            for session_text, sequence in watermarks.items():
                                session_id = uuid.UUID(session_text)
                                if (
                                    str(session_id) != session_text
                                    or isinstance(sequence, bool)
                                    or not isinstance(sequence, int)
                                    or sequence < 1
                                ):
                                    raise ValueError("invalid checkpoint watermark")
                                self._session_sequence_watermarks[session_id] = sequence
                            self._journal_has_checkpoint = True
                            self._checkpoint_next_position = next_position
                            self._next_position = next_position
                            continue

                        position = int(item["position"])
                        if kind == "event":
                            if self._journal_has_checkpoint:
                                if position <= self._last_event_position:
                                    raise ValueError(
                                        "event journal positions are not ordered"
                                    )
                                if position < self._checkpoint_next_position:
                                    pass
                                elif position == self._next_position:
                                    self._next_position += 1
                                else:
                                    raise ValueError(
                                        "event journal positions are not ordered"
                                    )
                            else:
                                if position != self._next_position:
                                    raise ValueError(
                                        "event journal positions are not ordered"
                                    )
                                self._next_position += 1
                            body = base64.b64decode(item["body"], validate=True)
                            if _sha256(body) != item["sha256"]:
                                raise ValueError("body digest mismatch")
                            envelope = parse_event_body(body)
                            if (
                                str(envelope.event_id) != item["event_id"]
                                or str(envelope.session_id) != item["session_id"]
                                or envelope.sequence != int(item["sequence"])
                                or envelope.event_type != item["event_type"]
                                or envelope.payload_hash != item["payload_hash"]
                            ):
                                raise ValueError("event identity mismatch")
                            if (
                                self._journal_has_checkpoint
                                and position < self._checkpoint_next_position
                                and envelope.sequence
                                > self._session_sequence_watermarks.get(
                                    envelope.session_id, 0
                                )
                            ):
                                raise ValueError("checkpoint sequence watermark is stale")
                            if (
                                self._journal_has_checkpoint
                                and position >= self._checkpoint_next_position
                                and envelope.sequence
                                <= self._session_sequence_watermarks.get(
                                    envelope.session_id, 0
                                )
                            ):
                                raise ValueError(
                                    "session event sequence did not advance checkpoint"
                                )
                            if (
                                position in self._records
                                or envelope.event_id in self._event_positions
                            ):
                                raise ValueError("duplicate event journal position or id")
                            sequences = self._session_sequences.setdefault(
                                envelope.session_id, set()
                            )
                            if envelope.sequence in sequences:
                                raise ValueError("duplicate session event sequence")
                            sequences.add(envelope.sequence)
                            self._session_sequence_watermarks[envelope.session_id] = max(
                                envelope.sequence,
                                self._session_sequence_watermarks.get(
                                    envelope.session_id, 0
                                ),
                            )
                            record = SpoolRecord(
                                position,
                                envelope.event_id,
                                envelope.session_id,
                                envelope.sequence,
                                body,
                                envelope,
                                datetime.fromisoformat(item["created_at"]),
                            )
                            self._records[position] = record
                            self._event_positions[envelope.event_id] = position
                            self._pending_events += 1
                            self._pending_body_bytes += len(body)
                            self._last_event_position = position
                        elif kind == "ack":
                            if position not in self._records:
                                raise ValueError("ack before event")
                            if position not in self._acked:
                                self._acked.add(position)
                                self._pending_events -= 1
                                self._pending_body_bytes -= len(
                                    self._records[position].body
                                )
                        elif kind == "attempt":
                            if position not in self._records:
                                raise ValueError("attempt before event")
                            self._attempts[position] = (
                                self._attempts.get(position, 0) + 1
                            )
                        else:
                            raise ValueError("unknown journal record kind")
                    except Exception as exc:
                        raise SpoolCorruptionError(
                            f"invalid complete journal record {line_number}: {exc}"
                        ) from exc
        except SpoolCorruptionError:
            self._journal_identity = None
            self._journal_size = None
            self._journal_mtime_ns = None
            raise

        recovered = self.path.stat()
        self._journal_identity = (recovered.st_dev, recovered.st_ino)
        self._journal_size = recovered.st_size
        self._journal_mtime_ns = recovered.st_mtime_ns
        self._journal_line_count = line_number

    def _pending(self) -> list[SpoolRecord]:
        # Recovery, append and compaction preserve durable-position order.
        return [
            record
            for position, record in self._records.items()
            if position not in self._acked
        ]

    @staticmethod
    def _event_journal_item(
        position: int,
        envelope: EventEnvelope,
        body: bytes,
        created_at: datetime,
    ) -> dict[str, Any]:
        return {
            "kind": "event",
            "position": position,
            "event_id": str(envelope.event_id),
            "session_id": str(envelope.session_id),
            "sequence": envelope.sequence,
            "event_type": envelope.event_type,
            "payload_hash": envelope.payload_hash,
            "body": base64.b64encode(body).decode("ascii"),
            "sha256": _sha256(body),
            "created_at": created_at.isoformat(timespec="microseconds"),
        }

    def _compact_locked(self) -> None:
        if not self._acked:
            return
        # The stable .lock file remains held through the atomic journal swap.
        self._discard_compaction_temp()
        checkpoint = {
            "kind": "checkpoint",
            "version": 1,
            "next_position": self._next_position,
            "session_sequence_watermarks": {
                str(session_id): sequence
                for session_id, sequence in self._session_sequence_watermarks.items()
            },
        }
        pending = self._pending()
        original_size = self.path.stat().st_size
        replaced = False
        fd = os.open(
            self._compaction_temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            try:
                self._write_all(fd, self._journal_line(checkpoint))
                for record in pending:
                    self._write_all(
                        fd,
                        self._journal_line(
                            self._event_journal_item(
                                record.durable_position,
                                record.envelope,
                                record.body,
                                record.created_at,
                            )
                        ),
                    )
                os.fsync(fd)
                compacted_size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if compacted_size >= original_size:
                return
            os.replace(self._compaction_temp, self.path)
            replaced = True
            self._fsync_directory(self.path.parent)
        finally:
            if not replaced:
                try:
                    self._compaction_temp.unlink()
                except FileNotFoundError:
                    pass
        self._recover()

    def _capacity_locked(self) -> SpoolCapacity:
        free = shutil.disk_usage(self.path.parent).free
        journal_size = self.path.stat().st_size
        saturated = (
            self._pending_events >= self.max_events
            or journal_size >= self.max_bytes
            or free <= self.min_free_bytes
        )
        needs_drain = (
            self._pending_events >= self.drain_threshold_events
            or self._pending_body_bytes >= self.drain_threshold_bytes
            or free <= self.min_free_bytes * 2
        )
        return SpoolCapacity(
            self._pending_events,
            self._pending_body_bytes,
            self.max_events,
            self.max_bytes,
            free,
            self.min_free_bytes,
            saturated,
            self.drain_threshold_events,
            self.drain_threshold_bytes,
            needs_drain,
        )

    async def capacity(self) -> SpoolCapacity:
        self._require_open()
        async with self._lock:
            with self._file_lock():
                self._recover()
                capacity = self._capacity_locked()
                if self._acked and (
                    capacity.free_bytes <= self.min_free_bytes
                    or self.path.stat().st_size >= self.max_bytes
                ):
                    self._compact_locked()
                    return self._capacity_locked()
                return capacity

    async def pending_count(self) -> int:
        self._require_open()
        async with self._lock:
            with self._file_lock():
                self._recover()
                return self._pending_events

    @staticmethod
    def _reserved_journal_bytes(body: bytes) -> int:
        # Reserve room for the base64 event record, its metadata and the ACK
        # tombstone that must fit after broker confirmation.
        return ((len(body) + 2) // 3) * 4 + 512 + 64

    async def _check_admission(self, body: bytes) -> None:
        capacity = self._capacity_locked()
        if capacity.events + 1 > self.max_events:
            raise SpoolCapacityError(
                f"spool event ceiling {self.max_events} reached"
            )
        reserved_bytes = self._reserved_journal_bytes(body)
        if self._acked and (
            capacity.free_bytes <= self.min_free_bytes
            or self.path.stat().st_size + reserved_bytes > self.max_bytes
        ):
            self._compact_locked()
            capacity = self._capacity_locked()
        if capacity.free_bytes <= self.min_free_bytes:
            raise SpoolCapacityError(
                f"spool disk headroom is below {self.min_free_bytes} bytes"
            )
        if self.path.stat().st_size + reserved_bytes > self.max_bytes:
            raise SpoolCapacityError(
                f"spool byte ceiling {self.max_bytes} reached"
            )
        if self.stop_admission_on_drain and (
            capacity.events + 1 >= self.drain_threshold_events
            or capacity.bytes + len(body) >= self.drain_threshold_bytes
        ):
            raise SpoolCapacityError(
                "admission-drain threshold reached; stop admission and drain spool "
                "before capture fails"
            )

    def _append_event_record(
        self, envelope: EventEnvelope, body: bytes
    ) -> SpoolRecord:
        position = self._next_position
        created = _utcnow()
        self._append_line(
            self._event_journal_item(position, envelope, body, created)
        )
        record = SpoolRecord(
            position,
            envelope.event_id,
            envelope.session_id,
            envelope.sequence,
            body,
            envelope,
            created,
        )
        self._records[position] = record
        self._event_positions[envelope.event_id] = position
        self._session_sequences.setdefault(envelope.session_id, set()).add(
            envelope.sequence
        )
        self._session_sequence_watermarks[envelope.session_id] = max(
            envelope.sequence,
            self._session_sequence_watermarks.get(envelope.session_id, 0),
        )
        self._pending_events += 1
        self._pending_body_bytes += len(body)
        self._next_position += 1
        self._last_event_position = position
        return record


    async def append_event(
        self,
        *,
        session_id: uuid.UUID | str,
        event_type: str,
        payload: dict[str, Any],
        event_id: uuid.UUID | str | None = None,
        emitted_at: datetime | None = None,
    ) -> SpoolRecord:
        self._require_open()
        event_uuid = uuid.UUID(str(event_id or uuid.uuid4()))
        session_uuid = uuid.UUID(str(session_id))
        emitted = normalize_emitted_at(emitted_at or _utcnow())
        async with self._lock:
            with self._file_lock():
                self._recover()
                existing_pos = self._event_positions.get(event_uuid)
                if existing_pos is not None:
                    existing = self._records[existing_pos]
                    stored = existing.envelope
                    if (
                        stored.session_id != session_uuid
                        or stored.event_type != event_type
                        or canonical_json(stored.payload) != canonical_json(payload)
                    ):
                        raise EventConflict(
                            "event_id already exists with a different payload"
                        )
                    return existing
                sequence = self._session_sequence_watermarks.get(session_uuid, 0) + 1
                envelope = build_event(
                    session_id=session_uuid,
                    sequence=sequence,
                    event_type=event_type,
                    payload=payload,
                    event_id=event_uuid,
                    emitted_at=emitted,
                )
                body = envelope.to_json_bytes()
                if len(body) > MAX_BODY_BYTES:
                    raise SpoolCapacityError(
                        f"event body exceeds {MAX_BODY_BYTES} bytes"
                    )
                await self._check_admission(body)
                return self._append_event_record(envelope, body)

    async def append_prebuilt(self, envelope: EventEnvelope) -> SpoolRecord:
        self._require_open()
        body = envelope.to_json_bytes()
        if len(body) > MAX_BODY_BYTES:
            raise SpoolCapacityError(
                f"event body exceeds {MAX_BODY_BYTES} bytes"
            )
        validated = parse_event_body(body)
        async with self._lock:
            with self._file_lock():
                self._recover()
                old_pos = self._event_positions.get(validated.event_id)
                if old_pos is not None:
                    old = self._records[old_pos]
                    if old.envelope.fingerprint != validated.fingerprint:
                        raise EventConflict(
                            "event_id already exists with a different body"
                        )
                    return old
                watermark = self._session_sequence_watermarks.get(
                    validated.session_id, 0
                )
                if validated.sequence <= watermark:
                    raise EventConflict(
                        "session sequence does not advance the durable watermark"
                    )
                if validated.sequence in self._session_sequences.get(
                    validated.session_id, set()
                ):
                    raise EventConflict(
                        "session sequence already allocated to another event"
                    )
                await self._check_admission(body)
                return self._append_event_record(validated, body)

    async def verify_integrity(self) -> list[str]:
        self._require_open()
        async with self._lock:
            with self._file_lock():
                try: self._recover(); self._corrupt_count = 0; return []
                except SpoolCorruptionError as exc: self._corrupt_count = 1; return [str(exc)]


    async def corrupt_count(self) -> int:
        return self._corrupt_count

    async def publish_pending(self, *, limit: int = 100,
                              publisher: EventPublisher | None = None) -> PublishBatchResult:
        self._require_open()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        chosen = publisher or self._publisher
        if chosen is None: raise SpoolError("no publisher configured")
        async with self._lock:
            with self._file_lock(): self._recover(); rows = self._pending()[:limit]
        published, blocked, error, retry_after = 0, False, None, 0.0
        for record in rows:
            try:
                confirmation = await chosen.publish(
                    record.body,
                    routing_key=f"{self.routing_key_prefix}{record.session_id}",
                    mandatory=True,
                )
                if not confirmation:
                    raise PublishNotConfirmed("publisher returned no durable confirmation")
            except Exception as exc:
                blocked, error = True, f"{type(exc).__name__}: {exc}"
                retry_after = 1.0
                break
            async with self._lock:
                with self._file_lock():
                    self._recover()
                    if (
                        record.durable_position in self._records
                        and record.durable_position not in self._acked
                    ):
                        self._append_line(
                            {"kind": "ack", "position": record.durable_position}
                        )
                        self._acked.add(record.durable_position)
                        self._pending_events -= 1
                        self._pending_body_bytes -= len(record.body)
            published += 1
        remaining = await self.pending_count()
        return PublishBatchResult(published, remaining, blocked, error, retry_after)

    async def drain(self, *, limit: int = 100,
                    publisher: EventPublisher | None = None) -> PublishBatchResult:
        return await self.publish_pending(limit=limit, publisher=publisher)


__all__ = ["DEFAULT_SPOOL_DIR", "spool_path_for_runner", "EventSpool", "SpoolRecord",
           "SpoolCapacity", "PublishBatchResult", "EventPublisher", "SpoolError",
           "SpoolCapacityError", "SpoolCorruptionError", "SpoolClosedError",
           "PublishNotConfirmed", "EventConflict"]
