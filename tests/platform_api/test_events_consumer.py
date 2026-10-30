"""Tests for history consumer ingestion, outcomes, and wallet immutability (Task 8).

Follows Task 8 specification:
- consume_event returns exactly inserted | duplicate | quarantine.
- Same identity with different payload_hash is quarantined and never applied.
- Consumer cannot mutate wallet tables (enforced at session boundary, raising
  WalletMutationForbidden / InsufficientPrivilege).
- Normal non-consumer sessions can mutate wallet tables without restriction.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import uuid
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from platform_api.config import PlatformSettings
from platform_api.db.session import create_engine
from platform_api.events import consumer as consumer_module
from platform_api.events.consumer import (
    HistoryConsumer,
    QueueTransfer,
    run_consumer,
)
from platform_api.events.permissions import EventConsumerRoleError
from platform_api.events.amqp import PublishNotConfirmed
from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    ProviderUsage,
    TranscriptGap,
    TranscriptSegment,
    TranscriptState,
)
from platform_api.events.repository import (
    HistoryRepository,
    TranscriptChanged,
    WalletMutationForbidden,
)
from platform_api.events.schemas import build_event, canonical_json
from platform_api.models.auth import User
from platform_api.models.wallet import WalletAccount
from tests.support.postgres import FreshPostgres


def _segment_event(
    session_id: uuid.UUID,
    sequence: int,
    text_content: str = "hello",
    event_id: uuid.UUID | None = None,
) -> str:
    envelope = build_event(
        session_id=session_id,
        sequence=sequence,
        event_type="transcript.segment",
        payload={
            "speaker": "user",
            "final_text": text_content,
            "start_ms": 0,
            "end_ms": 100,
        },
        event_id=event_id,
    )
    return envelope.to_json()


def test_consumer_cli_returns_success_for_once_delivery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = object()

    async def consume_once(
        _settings: PlatformSettings,
        *,
        once: bool,
        limit: int | None,
        queue: str | None,
    ) -> int:
        assert _settings is settings
        assert once is True
        assert limit is None
        assert queue is None
        return 1

    monkeypatch.setattr(consumer_module, "get_settings", lambda: settings)
    monkeypatch.setattr(consumer_module, "run_consumer", consume_once)
    caplog.set_level(logging.INFO, logger="platform_api.events.consumer")

    assert consumer_module.main(["--once"]) == 0
    assert "handled=1" in caplog.text


def test_consumer_cli_returns_nonzero_for_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_to_consume(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(consumer_module, "get_settings", lambda: object())
    monkeypatch.setattr(consumer_module, "run_consumer", fail_to_consume)
    caplog.set_level(logging.ERROR, logger="platform_api.events.consumer")

    assert consumer_module.main(["--once"]) == 1
    assert "history consumer failed" in caplog.text
    assert "broker unavailable" in caplog.text


_EVENT_ROLE_TABLE_GRANTS = (
    (ProcessedEvent.__table__, "SELECT, INSERT"),
    (TranscriptSegment.__table__, "SELECT, INSERT"),
    (ProviderUsage.__table__, "INSERT"),
    (TranscriptState.__table__, "SELECT, INSERT, UPDATE"),
    (TranscriptGap.__table__, "SELECT, INSERT"),
    (EventQuarantine.__table__, "SELECT, INSERT"),
)


def _qualified_table(engine, table, effective_schema: str | None = None) -> str:
    preparer = engine.sync_engine.dialect.identifier_preparer
    schema_map = engine.sync_engine.get_execution_options().get(
        "schema_translate_map", {}
    )
    schema = effective_schema
    if schema is None:
        schema = schema_map.get(table.schema, table.schema)
    if schema is None:
        raise AssertionError("test did not resolve the configured PostgreSQL schema")
    return f"{preparer.quote_schema(schema)}.{preparer.quote(table.name)}"


@asynccontextmanager
async def _event_consumer_role(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    *,
    wallet_update: bool = False,
    partial_event_select: bool = False,
    grant_case: Literal[
        "database",
        "schema",
        "table",
        "column",
        "public-column",
        "public-column-duplicate",
        "public-table-select",
        "public-table-insert",
    ]
    | None = None,
):
    role = f"event_consumer_{uuid.uuid4().hex[:16]}"
    password = uuid.uuid4().hex
    owner_engine = create_engine(settings)
    preparer = owner_engine.sync_engine.dialect.identifier_preparer
    quoted_role = preparer.quote(role)
    created = False
    public_column_grant_added = False
    public_table_grant_added = False
    public_table_privilege: str | None = None
    try:
        configured_url = make_url(settings.database_url)
        test_url = make_url(initialized_pg.url)
        assert (
            configured_url.host,
            configured_url.port,
            configured_url.database,
        ) == (test_url.host, test_url.port, test_url.database)
        schema_map = owner_engine.sync_engine.get_execution_options().get(
            "schema_translate_map", {}
        )
        effective_schema = schema_map.get(
            ProcessedEvent.__table__.schema, ProcessedEvent.__table__.schema
        )
        if effective_schema is None:
            async with owner_engine.connect() as connection:
                effective_schema = await connection.scalar(
                    text("SELECT current_schema()")
                )
        if not effective_schema:
            raise AssertionError("test database has no effective PostgreSQL schema")

        async with owner_engine.begin() as connection:
            # The isolated test DB must not inherit broad default schema creation.
            public_create_grantable = await connection.scalar(
                text(
                    "SELECT acl.is_grantable FROM pg_namespace AS n "
                    "CROSS JOIN LATERAL aclexplode(COALESCE("
                    "n.nspacl, acldefault('n', n.nspowner))) AS acl "
                    "WHERE n.nspname = 'public' AND acl.grantee = 0 "
                    "AND acl.privilege_type = 'CREATE'"
                )
            )
            if public_create_grantable is not None:
                await connection.execute(
                    text("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                )
            await connection.execute(
                text(
                    f"CREATE ROLE {quoted_role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                )
            )
            database = configured_url.database
            assert database is not None
            connect_grant_option = (
                " WITH GRANT OPTION" if grant_case == "database" else ""
            )
            await connection.execute(
                text(
                    f"GRANT CONNECT ON DATABASE {preparer.quote(database)} "
                    f"TO {quoted_role}{connect_grant_option}"
                )
            )
            schema_grant_option = (
                " WITH GRANT OPTION" if grant_case == "schema" else ""
            )
            await connection.execute(
                text(
                    f"GRANT USAGE ON SCHEMA "
                    f"{preparer.quote_schema(effective_schema)} "
                    f"TO {quoted_role}{schema_grant_option}"
                )
            )
            for table, privileges in _EVENT_ROLE_TABLE_GRANTS:
                qualified_table = _qualified_table(
                    owner_engine, table, effective_schema
                )
                if partial_event_select and table is ProcessedEvent.__table__:
                    await connection.execute(
                        text(
                            f"GRANT INSERT ON TABLE {qualified_table} "
                            f"TO {quoted_role}"
                        )
                    )
                    await connection.execute(
                        text(
                            f"GRANT SELECT ({preparer.quote('event_id')}) ON TABLE "
                            f"{qualified_table} TO {quoted_role}"
                        )
                    )
                elif grant_case == "table" and table is TranscriptSegment.__table__:
                    await connection.execute(
                        text(
                            f"GRANT SELECT ON TABLE {qualified_table} "
                            f"TO {quoted_role} WITH GRANT OPTION"
                        )
                    )
                    await connection.execute(
                        text(
                            f"GRANT INSERT ON TABLE {qualified_table} "
                            f"TO {quoted_role}"
                        )
                    )
                else:
                    await connection.execute(
                        text(
                            f"GRANT {privileges} ON TABLE {qualified_table} "
                            f"TO {quoted_role}"
                        )
                    )
            grant_table_model = (
                ProviderUsage.__table__
                if grant_case in ("public-column", "public-table-insert")
                else TranscriptSegment.__table__
            )
            grant_table = _qualified_table(
                owner_engine, grant_table_model, effective_schema
            )
            grant_column_name = (
                "provider" if grant_case == "public-column" else "final_text"
            )
            grant_column = preparer.quote(grant_column_name)
            if grant_case == "column":
                await connection.execute(
                    text(
                        f"GRANT SELECT ({grant_column}) ON TABLE "
                        f"{grant_table} TO {quoted_role} WITH GRANT OPTION"
                    )
                )
            if grant_case in ("public-column", "public-column-duplicate"):
                public_column_select_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "JOIN pg_attribute AS column_info "
                        "ON column_info.attrelid = relation.oid "
                        "CROSS JOIN LATERAL aclexplode(column_info.attacl) "
                        "AS column_acl "
                        "WHERE namespace.nspname = :schema_name "
                        "AND relation.relname = :table_name "
                        "AND column_info.attname = :column_name "
                        "AND column_acl.grantee = 0 "
                        "AND column_acl.privilege_type = 'SELECT')"
                    ),
                    {
                        "schema_name": effective_schema,
                        "table_name": grant_table_model.name,
                        "column_name": grant_column_name,
                    },
                )
                if public_column_select_exists:
                    raise AssertionError(
                        "disposable test DB already grants PUBLIC column SELECT"
                    )
                await connection.execute(
                    text(
                        f"GRANT SELECT ({grant_column}) ON TABLE "
                        f"{grant_table} TO PUBLIC"
                    )
                )
                public_column_grant_added = True
            if grant_case in ("public-table-select", "public-table-insert"):
                public_table_privilege = (
                    "SELECT" if grant_case == "public-table-select" else "INSERT"
                )
                public_table_privilege_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_class AS relation "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = relation.relnamespace "
                        "CROSS JOIN LATERAL aclexplode(COALESCE("
                        "relation.relacl, acldefault('r', relation.relowner))) "
                        "AS relation_acl "
                        "WHERE namespace.nspname = :schema_name "
                        "AND relation.relname = :table_name "
                        "AND relation_acl.grantee = 0 "
                        "AND relation_acl.privilege_type = :privilege)"
                    ),
                    {
                        "schema_name": effective_schema,
                        "table_name": grant_table_model.name,
                        "privilege": public_table_privilege,
                    },
                )
                if public_table_privilege_exists:
                    raise AssertionError(
                        f"disposable test DB already grants PUBLIC "
                        f"{public_table_privilege} on {grant_table_model.name}"
                    )
                await connection.execute(
                    text(
                        f"GRANT {public_table_privilege} ON TABLE "
                        f"{grant_table} TO PUBLIC"
                    )
                )
                public_table_grant_added = True
            if wallet_update:
                await connection.execute(
                    text(
                        f"GRANT UPDATE ON TABLE "
                        f"{_qualified_table(owner_engine, WalletAccount.__table__, effective_schema)} "
                        f"TO {quoted_role}"
                    )
                )
        created = True
        consumer_url = make_url(settings.database_url).set(
            username=role, password=password
        ).render_as_string(hide_password=False)
        yield role, consumer_url
    finally:
        if (
            created
            or public_create_grantable is not None
            or public_column_grant_added
            or public_table_grant_added
        ):
            async with owner_engine.begin() as connection:
                if created:
                    await connection.execute(text(f"DROP OWNED BY {quoted_role}"))
                    await connection.execute(text(f"DROP ROLE {quoted_role}"))
                if public_column_grant_added:
                    await connection.execute(
                        text(
                            f"REVOKE SELECT ({grant_column}) ON TABLE "
                            f"{grant_table} FROM PUBLIC CASCADE"
                        )
                    )
                if public_table_grant_added:
                    await connection.execute(
                        text(
                            f"REVOKE {public_table_privilege} ON TABLE "
                            f"{grant_table} FROM PUBLIC CASCADE"
                        )
                    )
                if public_create_grantable is not None:
                    public_create_option = (
                        " WITH GRANT OPTION" if public_create_grantable else ""
                    )
                    await connection.execute(
                        text(
                            "GRANT CREATE ON SCHEMA public TO PUBLIC"
                            f"{public_create_option}"
                        )
                    )
        await owner_engine.dispose()


def _settings_with_consumer_url(
    settings: PlatformSettings, consumer_url: str | None
) -> PlatformSettings:
    values = settings.model_dump()
    values["event_consumer_database_url"] = consumer_url
    values["amqp_url"] = "amqp://guest:guest@127.0.0.1/"
    return PlatformSettings.model_validate(values)


@pytest.mark.asyncio
async def test_queue_transfer_requires_mandatory_publish_confirmation() -> None:
    class UnconfirmedPublisher:
        def __init__(self) -> None:
            self.arguments = None

        async def publish(self, body, *, exchange, routing_key, mandatory):
            self.arguments = body, exchange, routing_key, mandatory
            return 0

    publisher = UnconfirmedPublisher()
    transfer = QueueTransfer(publisher, "event-quarantine")
    with pytest.raises(PublishNotConfirmed):
        await transfer.transfer(b"original-body", reason="INVALID_EVENT")
    assert publisher.arguments == (
        b"original-body",
        "",
        "event-quarantine",
        True,
    )


# ---------------------------------------------------------------------------
# Requirement 2: Consumer Wallet Immutability
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_consumer_wallet_immutability_guard(
    initialized_pg: FreshPostgres,
) -> None:
    """The consumer path cannot mutate wallet tables via ORM or SQL statements.

    MECHANISM: Consumer Wallet Immutability Guard
    History consumer sessions set session.info["history_consumer"] = True.
    SQLAlchemy session-level event listeners (_reject_wallet_mutation on before_flush
    and _reject_wallet_orm_execute on do_orm_execute) intercept any ORM flush or DML
    statement (update, insert, delete, text) targeting wallet tables and raise
    WalletMutationForbidden (subclassing PermissionError / InsufficientPrivilege).
    """

    wallet_engine = initialized_pg.engine
    schema_map = wallet_engine.sync_engine.get_execution_options().get(
        "schema_translate_map", {}
    )
    wallet_schema = schema_map.get(
        WalletAccount.__table__.schema, WalletAccount.__table__.schema
    )
    if wallet_schema is None:
        async with wallet_engine.connect() as connection:
            wallet_schema = await connection.scalar(text("SELECT current_schema()"))
    qualified_wallet = _qualified_table(
        wallet_engine, WalletAccount.__table__, wallet_schema
    )
    async with initialized_pg.sessions() as session:
        # First, seed a user and wallet account in a normal (non-consumer) session.
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email="wallet-test@example.com",
            email_key="wallet-test@example.com",
            display_name="Wallet Test",
            password_hash="test-hash",
            email_verified_at=datetime.now(timezone.utc),
        )
        wallet = WalletAccount(
            user_id=user_id,
            posted_minor=1000,
            reserved_minor=0,
        )
        session.add(user)
        session.add(wallet)
        await session.commit()

    # Now verify that in a consumer session, all wallet mutations raise WalletMutationForbidden
    repo = HistoryRepository()

    # 1. ORM insert attempt under consumer session
    async with initialized_pg.sessions() as session:
        session.info["history_consumer"] = True
        another_user_id = uuid.uuid4()
        rogue_wallet = WalletAccount(
            user_id=another_user_id,
            posted_minor=5000,
            reserved_minor=0,
        )
        session.add(rogue_wallet)
        with pytest.raises(WalletMutationForbidden, match="InsufficientPrivilege"):
            await session.flush()
        await session.rollback()

    # 2. ORM update attempt under consumer session
    async with initialized_pg.sessions() as session:
        session.info["history_consumer"] = True
        existing_wallet = await session.get(WalletAccount, user_id)
        assert existing_wallet is not None
        existing_wallet.posted_minor = 9999
        with pytest.raises(WalletMutationForbidden, match="InsufficientPrivilege"):
            await session.flush()
        await session.rollback()

    # 3. Direct statement update(WalletAccount) under consumer session
    async with initialized_pg.sessions() as session:
        session.info["history_consumer"] = True
        with pytest.raises(WalletMutationForbidden, match="InsufficientPrivilege"):
            await session.execute(
                update(WalletAccount)
                .where(WalletAccount.user_id == user_id)
                .values(posted_minor=8888)
            )
        await session.rollback()

    # 4. Raw SQL UPDATE under consumer session
    async with initialized_pg.sessions() as session:
        session.info["history_consumer"] = True
        with pytest.raises(WalletMutationForbidden, match="InsufficientPrivilege"):
            await session.execute(
                text("UPDATE wallet_accounts SET posted_minor = 7777 WHERE user_id = :uid"),
                {"uid": user_id},
            )
        await session.rollback()

    # 5. Schema-qualified SQL cannot bypass the consumer's wallet guard.
    async with initialized_pg.sessions() as session:
        session.info["history_consumer"] = True
        with pytest.raises(WalletMutationForbidden, match="InsufficientPrivilege"):
            await session.execute(
                text(
                    f"UPDATE {qualified_wallet} "
                    "SET posted_minor = 6666 WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
        await session.rollback()

    # 6. Non-consumer session is unaffected (wallet code not weakened)
    async with initialized_pg.sessions() as session:
        normal_wallet = await session.get(WalletAccount, user_id)
        assert normal_wallet is not None
        assert normal_wallet.posted_minor == 1000
        normal_wallet.posted_minor = 2000
        await session.commit()

    async with initialized_pg.sessions() as session:
        updated_wallet = await session.get(WalletAccount, user_id)
        assert updated_wallet is not None
        assert updated_wallet.posted_minor == 2000

    # 7. Real consume_event does not touch wallet rows
    session_id = uuid.uuid4()
    body = _segment_event(session_id, 1, "legitimate transcript")
    async with initialized_pg.sessions() as session:
        async with session.begin():
            outcome = await repo.consume_event(session, body)
            assert outcome == "inserted"

    async with initialized_pg.sessions() as session:
        wallet_check = await session.get(WalletAccount, user_id)
        assert wallet_check is not None

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_uses_restricted_role_and_projects_event(
    initialized_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real consumer session projects events with a DB-enforced role."""
    session_id = uuid.uuid4()
    event_id = uuid.uuid4()
    body = _segment_event(session_id, 1, "restricted projection", event_id)
    observations: dict[str, object] = {}
    real_history_consumer = consumer_module.HistoryConsumer

    class ProbingRepository(HistoryRepository):
        async def ingest(self, session, event_body, **kwargs):
            observations["current_user"] = await session.scalar(
                text("SELECT current_user")
            )
            try:
                async with session.begin_nested():
                    connection = await session.connection()
                    wallet_engine = connection.engine
                    wallet_table = WalletAccount.__table__
                    schema_map = (
                        wallet_engine.sync_engine.get_execution_options().get(
                            "schema_translate_map", {}
                        )
                    )
                    wallet_schema = schema_map.get(
                        wallet_table.schema, wallet_table.schema
                    )
                    if wallet_schema is None:
                        wallet_schema = await session.scalar(
                            text("SELECT current_schema()")
                        )
                    await connection.exec_driver_sql(
                        f"UPDATE {_qualified_table(wallet_engine, wallet_table, wallet_schema)} "
                        "SET posted_minor = posted_minor WHERE false"
                    )
            except Exception as exc:
                original = getattr(exc, "orig", None)
                observations["wallet_update_error"] = (
                    isinstance(exc, DBAPIError),
                    getattr(original, "sqlstate", None),
                )
            else:
                observations["wallet_update_error"] = None
            return await super().ingest(session, event_body, **kwargs)

    class StubDelivery:
        def __init__(self, event_body: str) -> None:
            self.body = event_body
            self.acked = False

        async def ack(self) -> None:
            self.acked = True

    class StubPublisher:
        def __init__(self, _config) -> None:
            pass

        async def close(self) -> None:
            pass

    class StubAmqpConsumer:
        def __init__(self, _config) -> None:
            self.delivery = StubDelivery(body)

        async def consume(self, queue, on_message, *, prefetch, max_messages):
            assert queue == test_platform_settings.event_history_queue
            assert prefetch == 1
            assert max_messages == 1
            await on_message(self.delivery)
            observations["delivery_acked"] = self.delivery.acked
            return 1

        async def close(self) -> None:
            pass

    async def no_topology(_config) -> None:
        return None

    monkeypatch.setattr(consumer_module, "declare_topology", no_topology)
    monkeypatch.setattr(consumer_module, "AmqpPublisher", StubPublisher)
    monkeypatch.setattr(consumer_module, "AmqpConsumer", StubAmqpConsumer)
    monkeypatch.setattr(
        consumer_module,
        "HistoryConsumer",
        lambda session_factory, **kwargs: real_history_consumer(
            session_factory, repository=ProbingRepository(), **kwargs
        ),
    )

    async with _event_consumer_role(initialized_pg, test_platform_settings) as (
        _,
        consumer_url,
    ):
        settings = _settings_with_consumer_url(test_platform_settings, consumer_url)
        handled = await run_consumer(settings, once=True)

    assert handled == 1
    assert observations["wallet_update_error"] == (True, "42501")
    assert observations["delivery_acked"] is True
    async with initialized_pg.sessions() as session:
        assert await session.get(ProcessedEvent, event_id) is not None
        projection = await session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.event_id == event_id)
        )
        assert projection is not None
        assert projection.final_text == "restricted projection"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_fails_closed_without_consumer_role_config(
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP topology must not start without a consumer database role")

    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )
    settings = _settings_with_consumer_url(test_platform_settings, None)
    with pytest.raises(RuntimeError, match="PLATFORM_EVENT_CONSUMER_DATABASE_URL"):
        await run_consumer(settings, once=True)


@pytest.mark.parametrize("identity_field", ["server_address", "server_started_at"])
@pytest.mark.asyncio
async def test_run_consumer_rejects_same_endpoint_resolving_to_another_server(
    monkeypatch: pytest.MonkeyPatch,
    identity_field: Literal["server_address", "server_started_at"],
) -> None:
    test_platform_settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_test",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=base64.urlsafe_b64encode(b"M" * 32).decode("ascii"),
    )

    class FakeResult:
        def __init__(self, identity) -> None:
            self.identity = identity

        def mappings(self):
            return self

        def one_or_none(self):
            return self.identity

    class FakeConnection:
        def __init__(self, identity) -> None:
            self.identity = identity

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def execute(self, _statement):
            return FakeResult(self.identity)

    class FakeEngine:
        def __init__(self, url, identity) -> None:
            self.url = make_url(url)
            self.identity = identity
            self.disposed = False
            self.sync_engine = self

        def get_execution_options(self):
            return {}

        def connect(self):
            return FakeConnection(self.identity)

        async def dispose(self):
            self.disposed = True

    platform_url = test_platform_settings.database_url
    parsed_platform_url = make_url(platform_url)
    consumer_url = parsed_platform_url.set(
        username=f"event_consumer_{uuid.uuid4().hex}", password="restricted"
    ).render_as_string(hide_password=False)
    settings = _settings_with_consumer_url(
        test_platform_settings, consumer_url
    )
    server_started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    platform_identity = {
        "database_name": parsed_platform_url.database,
        "server_address": "192.0.2.10",
        "server_port": (
            parsed_platform_url.port
            if parsed_platform_url.port is not None
            else 5432
        ),
        "server_started_at": server_started_at,
    }
    consumer_identity = {
        **platform_identity,
        identity_field: {
            "server_address": "192.0.2.11",
            "server_started_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
        }[identity_field],
    }
    engines = []

    def fake_create_engine(_settings, *, database_url=None):
        url = platform_url if database_url is None else database_url
        identity = platform_identity if database_url is None else consumer_identity
        engine = FakeEngine(url, identity)
        engines.append(engine)
        return engine

    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP must not start against a different PostgreSQL server")

    monkeypatch.setattr(consumer_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )

    with pytest.raises(EventConsumerRoleError, match="different PostgreSQL server"):
        await run_consumer(settings, once=True)

    assert len(engines) == 2
    assert all(engine.disposed for engine in engines)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_rejects_loopback_endpoint_alias_before_amqp(
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP topology must not start for a different database endpoint")

    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )
    # Bypass settings validation to exercise the runtime endpoint guard itself.
    monkeypatch.setattr(
        consumer_module,
        "validate_event_consumer_database_url",
        lambda _platform_url, consumer_url: consumer_url,
    )
    platform_url = make_url(test_platform_settings.database_url)
    alternate_host = "::1" if platform_url.host != "::1" else "127.0.0.1"
    consumer_url = platform_url.set(
        username="event_consumer_test", host=alternate_host
    ).render_as_string(hide_password=False)
    settings = test_platform_settings.model_copy(
        update={
            "event_consumer_database_url": consumer_url,
            "amqp_url": "amqp://guest:guest@127.0.0.1/",
        }
    )

    with pytest.raises(RuntimeError, match="platform database endpoint"):
        await run_consumer(settings, once=True)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_rejects_wallet_privileges_before_amqp(
    initialized_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP topology must not start for an overprivileged role")

    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )
    async with _event_consumer_role(
        initialized_pg, test_platform_settings, wallet_update=True
    ) as (_, consumer_url):
        settings = _settings_with_consumer_url(
            test_platform_settings, consumer_url
        )
        with pytest.raises(EventConsumerRoleError, match="wallet"):
            await run_consumer(settings, once=True)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_rejects_partial_column_privileges_before_amqp(
    initialized_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP topology must not start for a partial column grant")

    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )
    async with _event_consumer_role(
        initialized_pg, test_platform_settings, partial_event_select=True
    ) as (_, consumer_url):
        settings = _settings_with_consumer_url(
            test_platform_settings, consumer_url
        )
        with pytest.raises(EventConsumerRoleError, match="column-only"):
            await run_consumer(settings, once=True)


@pytest.mark.parametrize(
    "grant_case",
    [
        "database",
        "schema",
        "table",
        "column",
        "public-column",
        "public-column-duplicate",
        "public-table-select",
        "public-table-insert",
    ],
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_consumer_rejects_excess_or_partial_grants_before_amqp(
    initialized_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
    grant_case: Literal[
        "database",
        "schema",
        "table",
        "column",
        "public-column",
        "public-column-duplicate",
        "public-table-select",
        "public-table-insert",
    ],
) -> None:
    async def topology_must_not_start(_config) -> None:
        pytest.fail("AMQP topology must not start for an excess or partial grant")

    monkeypatch.setattr(
        consumer_module, "declare_topology", topology_must_not_start
    )
    async with _event_consumer_role(
        initialized_pg,
        test_platform_settings,
        grant_case=grant_case,
    ) as (_, consumer_url):
        settings = _settings_with_consumer_url(
            test_platform_settings, consumer_url
        )
        if grant_case == "public-column":
            expected_error = "column-only"
        elif grant_case == "public-column-duplicate" or grant_case.startswith(
            "public-table-"
        ):
            expected_error = "PUBLIC"
        else:
            expected_error = "GRANT OPTION"
        with pytest.raises(EventConsumerRoleError, match=expected_error):
            await run_consumer(settings, once=True)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_consumer_database_url_requires_distinct_login_on_same_database(
    test_platform_settings: PlatformSettings,
) -> None:
    values = test_platform_settings.model_dump()
    values["event_consumer_database_url"] = test_platform_settings.database_url
    with pytest.raises(ValidationError, match="different database role"):
        PlatformSettings.model_validate(values)

    values["event_consumer_database_url"] = (
        make_url(test_platform_settings.database_url)
        .set(username="event_consumer_test", database="unrelated_platform_db")
        .render_as_string(hide_password=False)
    )
    with pytest.raises(ValidationError, match="same PostgreSQL database"):
        PlatformSettings.model_validate(values)
    platform_url = make_url(test_platform_settings.database_url)
    alternate_host = "::1" if platform_url.host != "::1" else "127.0.0.1"
    values["event_consumer_database_url"] = platform_url.set(
        username="event_consumer_test", host=alternate_host
    ).render_as_string(hide_password=False)
    with pytest.raises(ValidationError, match="same PostgreSQL database"):
        PlatformSettings.model_validate(values)

    platform_port = platform_url.port if platform_url.port is not None else 5432
    alternate_port = platform_port + 1 if platform_port < 65_535 else platform_port - 1
    values["event_consumer_database_url"] = platform_url.set(
        username="event_consumer_test", port=alternate_port
    ).render_as_string(hide_password=False)
    with pytest.raises(ValidationError, match="same PostgreSQL database"):
        PlatformSettings.model_validate(values)

    database_less_consumer_url = URL.create(
        drivername=platform_url.drivername,
        username="event_consumer_test",
        password=platform_url.password,
        host=platform_url.host,
        port=platform_url.port,
        query=platform_url.query,
    ).render_as_string(hide_password=False)
    assert make_url(database_less_consumer_url).database is None
    values["event_consumer_database_url"] = database_less_consumer_url
    with pytest.raises(ValidationError, match="explicitly name"):
        PlatformSettings.model_validate(values)

    values["event_consumer_database_url"] = (
        make_url(test_platform_settings.database_url)
        .set(username="event_consumer_test", query={"host": "alternate.example"})
        .render_as_string(hide_password=False)
    )
    with pytest.raises(ValidationError, match="connection query parameters"):
        PlatformSettings.model_validate(values)


# ---------------------------------------------------------------------------
# Requirement 4: Consume Outcomes
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_consume_event_exact_outcomes_inserted_duplicate_quarantine(
    initialized_pg: FreshPostgres,
) -> None:
    """consume_event returns exactly inserted | duplicate | quarantine."""
    repo = HistoryRepository()
    session_id = uuid.uuid4()
    event_id = uuid.uuid4()

    # 1. First event -> inserted
    body1 = _segment_event(session_id, 1, "first text", event_id=event_id)
    async with initialized_pg.sessions() as session:
        async with session.begin():
            res1 = await repo.consume_event(session, body1)
            assert res1.status == "inserted"
            assert res1 == "inserted"

    # Verify projection was committed
    async with initialized_pg.sessions() as session:
        stored = await session.get(ProcessedEvent, event_id)
        assert stored is not None
        assert stored.sequence == 1
        segment = await session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.event_id == event_id)
        )
        assert segment is not None
        assert segment.final_text == "first text"

    # 2. Identical replay of same body -> duplicate
    async with initialized_pg.sessions() as session:
        async with session.begin():
            res2 = await repo.consume_event(session, body1)
            assert res2.status == "duplicate"
            assert res2 == "duplicate"

    # Verify no duplicate projection row was inserted
    async with initialized_pg.sessions() as session:
        segments = (
            await session.scalars(
                select(TranscriptSegment).where(TranscriptSegment.event_id == event_id)
            )
        ).all()
        assert len(segments) == 1

    # 3. Same identity with different payload_hash -> quarantine, NEVER applied
    body_conflict = _segment_event(session_id, 1, "tampered different text", event_id=event_id)
    async with initialized_pg.sessions() as session:
        async with session.begin():
            res3 = await repo.consume_event(session, body_conflict)
            assert res3.status == "quarantine"
            assert res3 == "quarantine"
            assert res3.reason == "EVENT_ID_CONFLICT"

    # Verify the conflicting payload was NEVER applied and original text is preserved
    async with initialized_pg.sessions() as session:
        segment = await session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.event_id == event_id)
        )
        assert segment is not None
        assert segment.final_text == "first text"  # Original kept, not tampered text

        quarantine_row = await session.scalar(
            select(EventQuarantine).where(
                EventQuarantine.event_id == event_id,
                EventQuarantine.reason == "EVENT_ID_CONFLICT",
            )
        )
        assert quarantine_row is not None
        assert "tampered different text" in quarantine_row.body

    # 4. Same session sequence with different event_id -> quarantine
    different_event_id = uuid.uuid4()
    body_seq_conflict = _segment_event(
        session_id, 1, "conflicting sequence event", event_id=different_event_id
    )
    async with initialized_pg.sessions() as session:
        async with session.begin():
            res4 = await repo.consume_event(session, body_seq_conflict)
            assert res4.status == "quarantine"
            assert res4 == "quarantine"
            assert res4.reason == "SEQUENCE_CONFLICT"

    # 5. Invalid wire body -> quarantine (recorded in quarantine table)
    async with initialized_pg.sessions() as session:
        async with session.begin():
            res5 = await repo.consume_event(session, '{"not_a_valid_event": true}')
            assert res5.status == "quarantine"
            assert res5 == "quarantine"
            assert res5.reason == "INVALID_EVENT"

    async with initialized_pg.sessions() as session:
        invalid_row = await session.scalar(
            select(EventQuarantine).where(
                EventQuarantine.reason == "INVALID_EVENT"
            )
        )
        assert invalid_row is not None
        assert invalid_row.reason == "INVALID_EVENT"
        assert invalid_row.body == '{"not_a_valid_event": true}'


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_transcript_pagination_and_revision_invalidation(
    initialized_pg: FreshPostgres,
) -> None:
    """Transcript pages carry revision; late segments advance revision and fail stale cursors."""
    repo = HistoryRepository()
    session_id = uuid.uuid4()

    # Ingest 3 segments
    for seq in (1, 2, 3):
        body = _segment_event(session_id, seq, f"line {seq}")
        async with initialized_pg.sessions() as session:
            async with session.begin():
                await repo.consume_event(session, body)

    async with initialized_pg.sessions() as session:
        page = await repo.transcript_page(session, session_id, limit=2)
        assert len(page.segments) == 2
        assert page.next_cursor is not None
        assert page.has_gaps is False
        saved_cursor = page.next_cursor
        initial_revision = page.transcript_revision

    # Fetch next page using valid cursor
    async with initialized_pg.sessions() as session:
        page2 = await repo.transcript_page(session, session_id, cursor=saved_cursor, limit=2)
        assert len(page2.segments) == 1
        assert page2.segments[0].sequence == 3
        assert page2.next_cursor is None

    # Ingest segment 4: this advances transcript revision
    body4 = _segment_event(session_id, 4, "line 4")
    async with initialized_pg.sessions() as session:
        async with session.begin():
            await repo.consume_event(session, body4)

    # Now the previous cursor (issued at initial_revision) fails with TranscriptChanged
    async with initialized_pg.sessions() as session:
        with pytest.raises(TranscriptChanged):
            await repo.transcript_page(session, session_id, cursor=saved_cursor)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_late_transcript_segment_is_ordered_and_invalidates_cursor(
    initialized_pg: FreshPostgres,
) -> None:
    repo = HistoryRepository()
    session_id = uuid.uuid4()
    for sequence in (3, 1):
        async with initialized_pg.sessions() as session:
            async with session.begin():
                await repo.consume_event(
                    session, _segment_event(session_id, sequence, f"line {sequence}")
                )
    async with initialized_pg.sessions() as session:
        first_page = await repo.transcript_page(session, session_id, limit=1)
        assert [segment.sequence for segment in first_page.segments] == [1]
        assert first_page.has_gaps is True
        cursor = first_page.next_cursor
        assert cursor is not None
    async with initialized_pg.sessions() as session:
        async with session.begin():
            await repo.consume_event(session, _segment_event(session_id, 2, "line 2"))
    async with initialized_pg.sessions() as session:
        with pytest.raises(TranscriptChanged):
            await repo.transcript_page(session, session_id, cursor=cursor)
        page = await repo.transcript_page(session, session_id, limit=3)
        assert [segment.sequence for segment in page.segments] == [1, 2, 3]
        assert page.has_gaps is False
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_non_transcript_event_fills_session_sequence_gap(
    initialized_pg: FreshPostgres,
) -> None:
    repo = HistoryRepository()
    session_id = uuid.uuid4()

    for sequence in (3, 1):
        async with initialized_pg.sessions() as session:
            async with session.begin():
                await repo.consume_event(
                    session, _segment_event(session_id, sequence, f"line {sequence}")
                )

    async with initialized_pg.sessions() as session:
        page = await repo.transcript_page(session, session_id, limit=1)
        assert [segment.sequence for segment in page.segments] == [1]
        assert page.has_gaps is True
        cursor = page.next_cursor
        assert cursor is not None
        old_revision = page.transcript_revision

    usage = build_event(
        session_id=session_id,
        sequence=2,
        event_type="usage.provider",
        payload={
            "provider": "local",
            "model": "stt",
            "kind": "audio_ms",
            "quantity": 100,
            "unit": "ms",
        },
    )
    async with initialized_pg.sessions() as session:
        async with session.begin():
            await repo.consume_event(session, usage.to_json())

    async with initialized_pg.sessions() as session:
        page = await repo.transcript_page(session, session_id)
        assert page.has_gaps is False
        assert page.transcript_revision > old_revision
        assert [segment.sequence for segment in page.segments] == [1, 3]
        with pytest.raises(TranscriptChanged):
            await repo.transcript_page(session, session_id, cursor=cursor)



@pytest.mark.asyncio
async def test_database_outage_keeps_delivery_unacked_until_retry(monkeypatch) -> None:
    from types import SimpleNamespace

    attempts = 0
    sleep_intervals: list[float] = []

    class UnavailableSession:
        async def __aenter__(self):
            raise OSError("database unavailable")

        async def __aexit__(self, *_args):
            return False

    class AvailableSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

    class FlakyFactory:
        def __call__(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return UnavailableSession()
            return AvailableSession()

    class Repository:
        async def ingest(self, _session, envelope, *, received_at):
            return SimpleNamespace(
                status="inserted", event_id=envelope.event_id, reason=None
            )

    retry_started = asyncio.Event()
    retry_allowed = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        sleep_intervals.append(delay)
        retry_started.set()
        await retry_allowed.wait()

    monkeypatch.setattr("platform_api.events.consumer._sleep", controlled_sleep)
    consumer = HistoryConsumer(
        session_factory=FlakyFactory(),
        quarantine_transfer=object(),
        repository=Repository(),
        retry_backoff_seconds=1.0,
    )
    acked = False

    async def ack() -> None:
        nonlocal acked
        acked = True

    task = asyncio.create_task(
        consumer.handle(
            _segment_event(uuid.uuid4(), 1, "keep me").encode(),
            ack=ack,
        )
    )
    await asyncio.wait_for(retry_started.wait(), timeout=1)
    assert attempts == 1
    assert acked is False
    assert task.done() is False

    retry_allowed.set()
    result = await task
    assert result.status == "inserted"
    assert attempts == 2
    assert sleep_intervals == [1.0]
    assert acked is True

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_quarantine_destination_failure_retries_without_nacking_source(
    initialized_pg: FreshPostgres,
    monkeypatch,
) -> None:
    session_id, event_id = uuid.uuid4(), uuid.uuid4()
    repo = HistoryRepository()
    async with initialized_pg.sessions() as session:
        async with session.begin():
            await repo.consume_event(
                session, _segment_event(session_id, 1, "original", event_id=event_id)
            )

    class FlakyQuarantine:
        def __init__(self) -> None:
            self.attempts = 0

        async def transfer(self, body: bytes, *, reason: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("quarantine destination unavailable")

    transfer = FlakyQuarantine()
    retry_started = asyncio.Event()
    retry_allowed = asyncio.Event()
    sleep_intervals: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        sleep_intervals.append(delay)
        retry_started.set()
        await retry_allowed.wait()

    monkeypatch.setattr("platform_api.events.consumer._sleep", controlled_sleep)
    consumer = HistoryConsumer(
        session_factory=initialized_pg.sessions,
        quarantine_transfer=transfer,
        repository=repo,
        retry_backoff_seconds=0.5,
    )
    acked = False

    async def ack() -> None:
        nonlocal acked
        acked = True

    task = asyncio.create_task(
        consumer.handle(
            _segment_event(session_id, 1, "conflicting", event_id=event_id).encode(),
            ack=ack,
        )
    )
    try:
        await asyncio.wait_for(retry_started.wait(), timeout=10)
    except TimeoutError:
        if task.done():
            await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    assert transfer.attempts == 1
    assert acked is False
    assert task.done() is False

    retry_allowed.set()
    result = await task
    assert result.status == "quarantined"
    assert transfer.attempts == 2
    assert sleep_intervals == [0.5]
    assert acked is True


@pytest.mark.asyncio
async def test_retry_worker_keeps_delivery_until_confirmed_republish(monkeypatch) -> None:
    from platform_api.events.consumer import RetryWorker

    envelope = _segment_event(uuid.uuid4(), 1, "retry later")

    class Delivery:
        def __init__(self) -> None:
            self.body = envelope.encode()
            self.acknowledged = False
            self.nacked = False

        async def ack(self) -> None:
            self.acknowledged = True

        async def nack(self, *, requeue: bool = True) -> None:
            self.nacked = True

    class Source:
        def __init__(self) -> None:
            self.delivery = Delivery()

        async def get(self, _queue: str):
            return self.delivery

    class FlakyPublisher:
        def __init__(self) -> None:
            self.attempts = 0

        async def publish(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("history destination unavailable")
            return 1

    source = Source()
    publisher = FlakyPublisher()
    retry_started = asyncio.Event()
    retry_allowed = asyncio.Event()
    sleep_intervals: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        sleep_intervals.append(delay)
        if delay == 0.5:
            retry_started.set()
            await retry_allowed.wait()

    monkeypatch.setattr("platform_api.events.consumer._sleep", controlled_sleep)
    worker = RetryWorker(
        amqp_consumer=source,
        history_publisher=publisher,
        retry_queue="retry",
        history_exchange="events",
        delay_seconds=0.25,
        failure_backoff_seconds=0.5,
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(retry_started.wait(), timeout=1)
    assert publisher.attempts == 1
    assert source.delivery.acknowledged is False
    assert source.delivery.nacked is False
    assert task.done() is False

    retry_allowed.set()
    assert await task is True
    assert publisher.attempts == 2
    assert sleep_intervals == [0.25, 0.5]
    assert source.delivery.acknowledged is True
    assert source.delivery.nacked is False
