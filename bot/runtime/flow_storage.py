"""Durable PostgreSQL storage for session flow state and rendered playback usage.

Only rows committed with the canonical platform session are used for restore or
billing. This module never creates or migrates a schema; callers must run the
platform's explicit schema initializer and pass a ready PostgreSQL engine.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.flow.canonical import canonical_json_v1
from bot.flow.playback_receipts import (
    PlaybackBinding,
    PlaybackReceiptVerifier,
    PcmSampleRange,
    RenderedFrameRange,
    VerifiedPlaybackReceipt,
)
from bot.flow.schemas import EventKey, FragmentPlayedOutputEvent, Reduction, SessionState
from bot.runtime.session_task import RuntimeStopRecord
from platform_api.billing.schemas import MAX_BIGINT, RuntimeStopAttestation
from platform_api.db.schema import check_schema_readiness
from platform_api.db.session import get_session_factory
from platform_api.models import Session
from platform_api.sessions.models import (
    CommittedPlaybackReceipt,
    FlowRuntimeEvent,
    FlowRuntimeState,
    RuntimeRegistration,
    RuntimeStopRecordRow,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MAX_SESSION_RECEIPTS = 4096


class FlowStoreConflict(RuntimeError):
    """Persisted state conflicts with a new or retried flow operation."""


class FlowStoreUnavailable(RuntimeError):
    """The required PostgreSQL schema cannot safely serve a managed session."""


def _canonical_session_id(value: object) -> tuple[UUID, str]:
    if isinstance(value, UUID):
        return value, str(value)
    if not isinstance(value, str):
        raise FlowStoreConflict("Flow session ID is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise FlowStoreConflict("Flow session ID is invalid") from exc
    canonical = str(parsed)
    if value != canonical:
        raise FlowStoreConflict("Flow session ID is not canonical")
    return parsed, canonical


def _require_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise FlowStoreConflict(f"{name} must be a lowercase SHA-256 digest")
    return value


def _digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json_v1(value)).hexdigest()


def _stop_record_payload(record: RuntimeStopRecord) -> dict[str, Any]:
    return {
        "attestation": record.attestation.model_dump(mode="json"),
        "expected_budget_version": record.expected_budget_version,
        "cumulative_billable_ms": record.cumulative_billable_ms,
    }


class DurablePostgresFlowStore:
    """Serialize a session's flow event, state, and optional receipt in PostgreSQL."""

    storage_backend = "postgresql"

    def __init__(self, engine: AsyncEngine, *, schema: str = "platform") -> None:
        if getattr(engine, "dialect", None) is None or engine.dialect.name != "postgresql":
            raise ValueError("Durable flow storage requires PostgreSQL")
        if not isinstance(schema, str) or _SCHEMA_RE.fullmatch(schema) is None:
            raise ValueError("Platform schema must be a lowercase PostgreSQL identifier")
        self.engine = engine
        self.schema = schema
        self.session_factory = get_session_factory(engine)
        self._receipt_verifiers: weakref.WeakSet[PlaybackReceiptVerifier] = weakref.WeakSet()

    async def ensure_ready(self) -> None:
        """Read-only schema check. Never initialize or migrate the platform database."""
        try:
            ready = await check_schema_readiness(self.engine, schema=self.schema)
        except Exception as exc:
            raise FlowStoreUnavailable(
                "The configured platform PostgreSQL store is unavailable"
            ) from exc
        if not ready:
            raise FlowStoreUnavailable(
                "The configured platform PostgreSQL schema is not at the required version"
            )

    def register_receipt_verifier(
        self,
        session_id: str,
        verifier: PlaybackReceiptVerifier,
    ) -> None:
        _, canonical_id = _canonical_session_id(session_id)
        if (
            type(verifier) is not PlaybackReceiptVerifier
            or verifier.session_id != canonical_id
        ):
            raise FlowStoreConflict("Playback verifier is not bound to this session")
        self._receipt_verifiers.add(verifier)

    async def commit(
        self,
        event: EventKey,
        reduction: Reduction,
        *,
        verified_receipts: tuple[VerifiedPlaybackReceipt, ...] = (),
    ) -> None:
        """Atomically persist an accepted event, its reducer result, and receipt proof."""
        if not isinstance(event, EventKey) or type(reduction) is not Reduction:
            raise FlowStoreConflict("Flow event or reduction has an invalid type")
        if not isinstance(verified_receipts, tuple):
            raise FlowStoreConflict("Verified playback receipts must be a tuple")
        session_uuid, session_id = _canonical_session_id(event.session_id)
        state = reduction.state
        if type(state) is not SessionState:
            raise FlowStoreConflict("Flow reduction has an invalid session state")
        if state.session_id != session_id:
            raise FlowStoreConflict("Flow reduction belongs to a different session")
        flow_hash = _require_digest(state.flow_hash, "flow_hash")
        event_id = event.event_id
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            raise FlowStoreConflict("Flow event ID is invalid")
        event_data = event.model_dump(mode="json")
        reduction_data = reduction.model_dump(mode="json")
        try:
            event_digest = _digest_json(event_data)
            reduction_digest = _digest_json(reduction_data)
        except (TypeError, ValueError) as exc:
            raise FlowStoreConflict("Flow event or reduction is not canonical JSON") from exc

        receipt: VerifiedPlaybackReceipt | None
        if isinstance(event, FragmentPlayedOutputEvent):
            if len(verified_receipts) != 1:
                raise FlowStoreConflict("A played event requires exactly one verified playback receipt")
            receipt = verified_receipts[0]
            receipt_values = self._receipt_values(receipt, event, session_id)
        elif verified_receipts:
            raise FlowStoreConflict("Playback receipt supplied for a non-played event")
        else:
            receipt = None
            receipt_values = None

        async with self.session_factory() as db:
            async with db.begin():
                canonical_session = await db.scalar(
                    select(Session)
                    .where(Session.id == session_uuid)
                    .with_for_update()
                )
                if canonical_session is None:
                    raise FlowStoreConflict("Flow event does not belong to a canonical platform session")

                prior = await db.scalar(
                    select(FlowRuntimeEvent).where(
                        FlowRuntimeEvent.session_id == session_uuid,
                        FlowRuntimeEvent.event_id == event_id,
                    )
                )
                if prior is not None:
                    if (
                        prior.event_digest != event_digest
                        or prior.reduction_digest != reduction_digest
                    ):
                        raise FlowStoreConflict("Flow event ID conflicts with a committed event")
                    if receipt is not None:
                        prior_receipt = await db.scalar(
                            select(CommittedPlaybackReceipt).where(
                                CommittedPlaybackReceipt.session_id == session_uuid,
                                CommittedPlaybackReceipt.event_id == event_id,
                            )
                        )
                        if (
                            prior_receipt is None
                            or prior_receipt.receipt_digest != receipt_values["receipt_digest"]
                            or not self._has_exact_verifier(
                                session_id, event, receipt, allow_committed=True
                            )
                        ):
                            raise FlowStoreConflict(
                                "Playback retry does not match its verifier-issued committed receipt"
                            )
                    return

                if receipt is not None and not self._has_exact_verifier(
                    session_id, event, receipt, allow_committed=False
                ):
                    raise FlowStoreConflict(
                        "Playback receipt is not the exact pending verifier-issued object"
                    )

                stopped = await db.scalar(
                    select(RuntimeStopRecordRow.session_id).where(
                        RuntimeStopRecordRow.session_id == session_uuid
                    )
                )
                if stopped is not None:
                    raise FlowStoreConflict("Flow session already has a committed stop record")

                state_row = await db.scalar(
                    select(FlowRuntimeState)
                    .where(FlowRuntimeState.session_id == session_uuid)
                    .with_for_update()
                )
                previous_count = state_row.event_count if state_row is not None else 0
                previous_max = await db.scalar(
                    select(func.max(FlowRuntimeEvent.sequence)).where(
                        FlowRuntimeEvent.session_id == session_uuid
                    )
                )
                if (previous_max or 0) != previous_count:
                    raise FlowStoreConflict("Flow event history and reducer state are inconsistent")

                if receipt_values is not None:
                    await self._validate_nonoverlapping_receipt(db, session_uuid, receipt_values)

                next_sequence = previous_count + 1
                db.add(
                    FlowRuntimeEvent(
                        session_id=session_uuid,
                        sequence=next_sequence,
                        event_id=event_id,
                        event_type=event.type,
                        event_digest=event_digest,
                        reduction_digest=reduction_digest,
                        event_json=event_data,
                        reduction_json=reduction_data,
                    )
                )
                if state_row is None:
                    state_row = FlowRuntimeState(
                        session_id=session_uuid,
                        flow_hash=flow_hash,
                        state_json=state.model_dump(mode="json"),
                        event_count=next_sequence,
                    )
                    db.add(state_row)
                else:
                    if state_row.flow_hash != flow_hash:
                        raise FlowStoreConflict("Flow hash changed within a canonical session")
                    state_row.state_json = state.model_dump(mode="json")
                    state_row.event_count = next_sequence
                    state_row.updated_at = datetime.now(timezone.utc)
                await db.flush()
                if receipt_values is not None:
                    db.add(
                        CommittedPlaybackReceipt(
                            session_id=session_uuid,
                            event_id=event_id,
                            **receipt_values,
                        )
                    )
                    await db.flush()

    async def load_state(self, session_id: str, flow_hash: str) -> SessionState | None:
        session_uuid, canonical_id = _canonical_session_id(session_id)
        expected_hash = _require_digest(flow_hash, "flow_hash")
        async with self.session_factory() as db:
            row = await db.get(FlowRuntimeState, session_uuid)
        if row is None:
            return None
        if row.flow_hash != expected_hash:
            raise FlowStoreConflict("Durable flow state belongs to a different flow hash")
        try:
            state = SessionState.model_validate(row.state_json)
        except Exception as exc:
            raise FlowStoreConflict("Durable flow state is invalid") from exc
        if state.session_id != canonical_id or state.flow_hash != expected_hash:
            raise FlowStoreConflict("Durable flow state has an invalid session binding")
        return state

    async def save_stop_record(
        self,
        session_id: str,
        session_version: int,
        record: RuntimeStopRecord,
    ) -> None:
        session_uuid, canonical_id = _canonical_session_id(session_id)
        if type(record) is not RuntimeStopRecord or type(record.attestation) is not RuntimeStopAttestation:
            raise FlowStoreConflict("Runtime stop record has an invalid type")
        attestation = record.attestation
        if (
            attestation.source != "voice-runtime"
            or attestation.session_id != session_uuid
            or type(session_version) is not int
            or session_version < 1
            or attestation.session_version != session_version
            or type(record.expected_budget_version) is not int
            or not 1 <= record.expected_budget_version <= MAX_BIGINT
            or type(record.cumulative_billable_ms) is not int
            or not 0 <= record.cumulative_billable_ms <= MAX_BIGINT
            or not attestation.stopped_at.tzinfo
            or attestation.stopped_at.utcoffset() is None
        ):
            raise FlowStoreConflict("Runtime stop record has an invalid session or billing binding")
        try:
            payload = _stop_record_payload(record)
            record_digest = _digest_json(payload)
        except (TypeError, ValueError) as exc:
            raise FlowStoreConflict("Runtime stop record is not canonical JSON") from exc

        async with self.session_factory() as db:
            async with db.begin():
                session = await db.scalar(
                    select(Session).where(Session.id == session_uuid).with_for_update()
                )
                if (
                    session is None
                    or session.version != session_version
                    or session.runner_id != attestation.runner_id
                ):
                    raise FlowStoreConflict("Runtime stop record does not match the canonical session")
                registration = await db.scalar(
                    select(RuntimeRegistration)
                    .where(RuntimeRegistration.session_id == session_uuid)
                    .with_for_update()
                )
                if (
                    registration is None
                    or registration.registration_id != attestation.registration_id
                    or registration.runner_id != attestation.runner_id
                    or registration.status not in {"REGISTERED", "RELEASE_PENDING", "RELEASED"}
                ):
                    raise FlowStoreConflict("Runtime stop record does not match the canonical runner registration")

                existing = await db.get(
                    RuntimeStopRecordRow,
                    (session_uuid, session_version),
                )
                if existing is not None:
                    if existing.record_digest != record_digest:
                        raise FlowStoreConflict("A different stop record is already committed")
                    return

                billable_ms = await self._cumulative_billable_ms_in_session(db, session_uuid)
                if billable_ms != record.cumulative_billable_ms:
                    raise FlowStoreConflict(
                        "Runtime stop record usage does not match committed playback receipts"
                    )
                db.add(
                    RuntimeStopRecordRow(
                        session_id=session_uuid,
                        session_version=session_version,
                        attestation_json=payload["attestation"],
                        expected_budget_version=record.expected_budget_version,
                        cumulative_billable_ms=record.cumulative_billable_ms,
                        record_digest=record_digest,
                    )
                )
                await db.flush()

    async def load_stop_record(
        self,
        session_id: str,
        session_version: int,
    ) -> RuntimeStopRecord | None:
        session_uuid, _ = _canonical_session_id(session_id)
        if type(session_version) is not int or session_version < 1:
            raise FlowStoreConflict("Session version is invalid")
        async with self.session_factory() as db:
            row = await db.get(RuntimeStopRecordRow, (session_uuid, session_version))
        if row is None:
            return None
        try:
            attestation = RuntimeStopAttestation.model_validate_json(
                json.dumps(row.attestation_json)
            )
            record = RuntimeStopRecord(
                attestation=attestation,
                expected_budget_version=row.expected_budget_version,
                cumulative_billable_ms=row.cumulative_billable_ms,
            )
            if _digest_json(_stop_record_payload(record)) != row.record_digest:
                raise ValueError("stored stop record digest mismatch")
        except Exception as exc:
            raise FlowStoreConflict("Durable runtime stop record is invalid") from exc
        if attestation.session_id != session_uuid or attestation.session_version != session_version:
            raise FlowStoreConflict("Durable runtime stop record has an invalid session binding")
        return record

    def _receipt_values(
        self,
        receipt: VerifiedPlaybackReceipt,
        event: FragmentPlayedOutputEvent,
        session_id: str,
    ) -> dict[str, Any]:
        if (
            type(receipt) is not VerifiedPlaybackReceipt
            or receipt.event is not event
            or type(receipt.binding) is not PlaybackBinding
            or type(receipt.media_samples) is not PcmSampleRange
            or type(receipt.rendered_frames) is not RenderedFrameRange
            or receipt.binding.session_id != session_id
            or not receipt.binding.matches_played_event(event)
            or receipt.evidence_id != event.evidence_id
            or not isinstance(receipt.peer_id, str)
            or not receipt.peer_id
            or not isinstance(receipt.verifier_identity, str)
            or not receipt.verifier_identity
        ):
            raise FlowStoreConflict("Playback receipt does not match its played event and session")
        media = receipt.media_samples
        rendered = receipt.rendered_frames
        if (
            type(media.sample_start) is not int
            or type(media.sample_end) is not int
            or type(media.sample_rate) is not int
            or type(rendered.frame_start) is not int
            or type(rendered.frame_end) is not int
            or type(rendered.sample_rate) is not int
            or media.sample_start < 0
            or media.sample_end <= media.sample_start
            or rendered.frame_start < 0
            or rendered.frame_end <= rendered.frame_start
        ):
            raise FlowStoreConflict("Playback receipt range is invalid")
        receipt_digest = _digest_json(
            {
                "binding": receipt.binding.to_message(),
                "event": event.model_dump(mode="json"),
                "media_samples": media.to_message(),
                "rendered_frames": rendered.to_message(),
                "evidence_id": receipt.evidence_id,
                "peer_id": receipt.peer_id,
                "verifier_identity": receipt.verifier_identity,
            }
        )
        return {
            "evidence_id": receipt.evidence_id,
            "transport_epoch": receipt.binding.transport_epoch,
            "turn_id": receipt.binding.turn_id,
            "generation_id": receipt.binding.generation_id,
            "manifest_id": receipt.binding.manifest_id,
            "fragment_id": receipt.binding.fragment_id,
            "content_hash": receipt.binding.content_hash,
            "capability_report_hash": receipt.binding.capability_report_hash,
            "peer_id": receipt.peer_id,
            "verifier_identity": receipt.verifier_identity,
            "sample_start": media.sample_start,
            "sample_end": media.sample_end,
            "sample_rate": media.sample_rate,
            "frame_start": rendered.frame_start,
            "frame_end": rendered.frame_end,
            "render_sample_rate": rendered.sample_rate,
            "receipt_digest": receipt_digest,
        }

    def _has_exact_verifier(
        self,
        session_id: str,
        event: FragmentPlayedOutputEvent,
        receipt: VerifiedPlaybackReceipt,
        *,
        allow_committed: bool,
    ) -> bool:
        for verifier in tuple(self._receipt_verifiers):
            if verifier.session_id != session_id or verifier.verifier_identity != receipt.verifier_identity:
                continue
            if verifier.verified_receipt_for_event(event) is receipt:
                return True
            if allow_committed and any(
                committed is receipt for committed in verifier.committed_receipts()
            ):
                return True
        return False

    async def _validate_nonoverlapping_receipt(
        self,
        db,
        session_uuid: UUID,
        values: dict[str, Any],
    ) -> None:
        receipt_count = await db.scalar(
            select(func.count())
            .select_from(CommittedPlaybackReceipt)
            .where(CommittedPlaybackReceipt.session_id == session_uuid)
        )
        if receipt_count >= _MAX_SESSION_RECEIPTS:
            raise FlowStoreConflict("Session exceeded the bounded committed playback receipt ledger")
        prior_rows = (
            await db.execute(
                select(
                    CommittedPlaybackReceipt.sample_start,
                    CommittedPlaybackReceipt.sample_end,
                    CommittedPlaybackReceipt.sample_rate,
                    CommittedPlaybackReceipt.frame_start,
                    CommittedPlaybackReceipt.frame_end,
                    CommittedPlaybackReceipt.render_sample_rate,
                ).where(
                    CommittedPlaybackReceipt.session_id == session_uuid,
                    CommittedPlaybackReceipt.transport_epoch == values["transport_epoch"],
                )
            )
        ).all()
        for (
            sample_start,
            sample_end,
            sample_rate,
            frame_start,
            frame_end,
            render_sample_rate,
        ) in prior_rows:
            if sample_rate != values["sample_rate"]:
                raise FlowStoreConflict("Playback sample rate changed within a transport epoch")
            if max(sample_start, values["sample_start"]) < min(sample_end, values["sample_end"]):
                raise FlowStoreConflict("Playback sample ranges overlap")
            if render_sample_rate != values["render_sample_rate"]:
                raise FlowStoreConflict("Rendered sample rate changed within a transport epoch")
            if max(frame_start, values["frame_start"]) < min(
                frame_end,
                values["frame_end"],
            ):
                raise FlowStoreConflict("Rendered frame ranges overlap")

    async def _cumulative_billable_ms_in_session(self, db, session_uuid: UUID) -> int:
        rows = (
            await db.execute(
                select(
                    CommittedPlaybackReceipt.sample_start,
                    CommittedPlaybackReceipt.sample_end,
                    CommittedPlaybackReceipt.sample_rate,
                ).where(CommittedPlaybackReceipt.session_id == session_uuid)
            )
        ).all()
        duration_ms = sum(
            (
                Fraction((sample_end - sample_start) * 1000, sample_rate)
                for sample_start, sample_end, sample_rate in rows
            ),
            Fraction(),
        )
        billable_ms = duration_ms.numerator // duration_ms.denominator
        if billable_ms > MAX_BIGINT:
            raise FlowStoreConflict("Committed playback usage exceeds the billing integer range")
        return billable_ms


class PostgresVerifiedPlaybackUsageMeter:
    """Read cumulative billable milliseconds only from durable committed receipts."""

    receipt_source = "committed_playback_receipts"

    def __init__(
        self,
        store: DurablePostgresFlowStore,
        session_id: str,
        receipt_verifier: PlaybackReceiptVerifier | None,
    ) -> None:
        _, canonical_id = _canonical_session_id(session_id)
        if not isinstance(store, DurablePostgresFlowStore):
            raise FlowStoreConflict("Usage meter requires the durable PostgreSQL flow store")
        if receipt_verifier is not None:
            store.register_receipt_verifier(canonical_id, receipt_verifier)
        self._store = store
        self.session_id = canonical_id
        self.receipt_verifier = receipt_verifier

    async def cumulative_billable_ms(self) -> int:
        session_uuid, _ = _canonical_session_id(self.session_id)
        async with self._store.session_factory() as db:
            return await self._store._cumulative_billable_ms_in_session(db, session_uuid)
