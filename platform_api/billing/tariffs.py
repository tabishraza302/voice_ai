"""Transactional current-tariff reads and immutable version changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from platform_api.auth.errors import DependencyUnavailableError, RevisionConflictError, ValidationError
from platform_api.models.tariff import ActiveSessionTariff, SessionTariffRecord

_MAX_BIGINT = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class SessionTariff:
    tariff_id: uuid.UUID
    tariff_version: str
    rate_minor_per_minute: int
    billing_slice_ms: int
    permit_ttl_ms: int


@dataclass(frozen=True, slots=True)
class InitialSessionTariff:
    """Explicit one-time inputs required to seed a fresh platform schema."""

    rate_minor_per_minute: int
    billing_slice_ms: int
    permit_ttl_ms: int


async def seed_initial_active_tariff(
    connection: AsyncConnection,
    initial: InitialSessionTariff,
) -> None:
    """Insert the configured initial tariff and its pointer during fresh schema creation."""
    _validate_tariff_values(
        initial.rate_minor_per_minute,
        initial.billing_slice_ms,
        initial.permit_ttl_ms,
    )
    now = datetime.now(timezone.utc)
    tariff_id = uuid.uuid4()
    await connection.execute(
        SessionTariffRecord.__table__.insert().values(
            id=tariff_id,
            revision=1,
            rate_minor_per_minute=initial.rate_minor_per_minute,
            billing_slice_ms=initial.billing_slice_ms,
            permit_ttl_ms=initial.permit_ttl_ms,
            created_at=now,
        )
    )
    await connection.execute(
        ActiveSessionTariff.__table__.insert().values(
            singleton_id=1,
            current_tariff_id=tariff_id,
            updated_at=now,
        )
    )


def _validate_tariff_values(
    rate_minor_per_minute: int,
    billing_slice_ms: int,
    permit_ttl_ms: int,
) -> None:
    if not 0 <= rate_minor_per_minute <= _MAX_BIGINT:
        raise ValidationError("Tariff rate must be between 0 and the PostgreSQL integer limit.")
    if not 0 < billing_slice_ms <= _MAX_BIGINT:
        raise ValidationError("Billing slice must be a positive PostgreSQL integer.")
    if not 0 < permit_ttl_ms <= _MAX_BIGINT:
        raise ValidationError("Permit TTL must be a positive PostgreSQL integer.")


async def _load_pointer(
    db: AsyncSession,
    *,
    for_update: bool,
) -> tuple[ActiveSessionTariff, SessionTariffRecord]:
    statement = (
        select(ActiveSessionTariff)
        .where(ActiveSessionTariff.singleton_id == 1)
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update(of=ActiveSessionTariff)
    else:
        statement = statement.with_for_update(read=True, of=ActiveSessionTariff)
    pointer = (await db.execute(statement)).scalar_one_or_none()
    if pointer is None:
        raise DependencyUnavailableError("No active session tariff is configured.")
    tariff = await db.get(SessionTariffRecord, pointer.current_tariff_id)
    if tariff is None:
        raise DependencyUnavailableError("The active session tariff record is missing.")
    return pointer, tariff


async def lock_active_tariff(
    db: AsyncSession,
) -> tuple[ActiveSessionTariff, SessionTariffRecord]:
    """Lock the singleton pointer for an audited tariff mutation."""
    return await _load_pointer(db, for_update=True)


def _to_session_tariff(record: SessionTariffRecord) -> SessionTariff:
    return SessionTariff(
        tariff_id=record.id,
        tariff_version=f"v{record.revision}",
        rate_minor_per_minute=record.rate_minor_per_minute,
        billing_slice_ms=record.billing_slice_ms,
        permit_ttl_ms=record.permit_ttl_ms,
    )


async def resolve_active_tariff(db: AsyncSession) -> SessionTariff:
    """Read and hold the active pointer shared until the caller's transaction ends."""
    _, record = await _load_pointer(db, for_update=False)
    return _to_session_tariff(record)


async def append_tariff_version(
    db: AsyncSession,
    *,
    expected_revision: int,
    rate_minor_per_minute: int,
    now: datetime | None = None,
) -> SessionTariffRecord:
    """Append a new immutable rate version and switch the pointer without committing."""
    if expected_revision <= 0:
        raise ValidationError("Expected tariff revision must be positive.")
    _validate_tariff_values(rate_minor_per_minute, 1, 1)
    pointer, current = await lock_active_tariff(db)
    if current.revision != expected_revision:
        raise RevisionConflictError("The active tariff changed; reload before applying a new rate.")
    if current.revision >= _MAX_BIGINT:
        raise ValidationError("The tariff revision limit has been reached.")

    created_at = now or datetime.now(timezone.utc)
    record = SessionTariffRecord(
        id=uuid.uuid4(),
        revision=current.revision + 1,
        rate_minor_per_minute=rate_minor_per_minute,
        billing_slice_ms=current.billing_slice_ms,
        permit_ttl_ms=current.permit_ttl_ms,
        created_at=created_at,
    )
    db.add(record)
    await db.flush()

    pointer.current_tariff_id = record.id
    pointer.updated_at = created_at
    await db.flush()
    return record
