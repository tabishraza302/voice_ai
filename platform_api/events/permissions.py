"""Database-level permissions required by the history event consumer."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    ProviderUsage,
    TranscriptGap,
    TranscriptSegment,
    TranscriptState,
)


class EventConsumerRoleError(RuntimeError):
    """The configured database login is not restricted to event projections."""


def _database_endpoint(url) -> tuple[str, int, str | None]:
    host = (url.host or "<unix-socket>").lower()
    port = url.port if url.port is not None else 5432
    return host, port, url.database

_SERVER_IDENTITY_QUERY = text(
    """
    SELECT
        current_database() AS database_name,
        inet_server_addr()::text AS server_address,
        inet_server_port() AS server_port,
        pg_postmaster_start_time() AS server_started_at
    """
)


def _server_identity_tuple(identity: Mapping[str, object]) -> tuple[object, ...]:
    return (
        identity["database_name"],
        identity["server_address"],
        identity["server_port"],
        identity["server_started_at"],
    )

_EVENT_TABLE_PRIVILEGES = {
    ProcessedEvent.__tablename__: frozenset({"SELECT", "INSERT"}),
    TranscriptSegment.__tablename__: frozenset({"SELECT", "INSERT"}),
    ProviderUsage.__tablename__: frozenset({"INSERT"}),
    TranscriptState.__tablename__: frozenset({"SELECT", "INSERT", "UPDATE"}),
    TranscriptGap.__tablename__: frozenset({"SELECT", "INSERT"}),
    EventQuarantine.__tablename__: frozenset({"SELECT", "INSERT"}),
}
_TABLE_PRIVILEGES_QUERY = text(
    """
    SELECT
        namespace.nspname AS schema_name,
        relation.relname AS table_name,
        privilege.privilege_name,
        EXISTS (
            SELECT 1
            FROM aclexplode(
                COALESCE(relation.relacl, acldefault('r', relation.relowner))
            ) AS relation_acl
            WHERE relation_acl.grantee = 0
              AND relation_acl.privilege_type = privilege.privilege_name
        ) AS has_public_table_privilege,
        EXISTS (
            SELECT 1
            FROM pg_attribute AS column_info
            CROSS JOIN LATERAL aclexplode(column_info.attacl) AS column_acl
            WHERE column_info.attrelid = relation.oid
              AND column_info.attnum > 0
              AND NOT column_info.attisdropped
              AND column_acl.grantee = 0
        ) AS has_public_column_privilege,
        has_table_privilege(
            current_user, relation.oid, privilege.privilege_name
        ) AS has_privilege,
        EXISTS (
            SELECT 1
            FROM aclexplode(
                COALESCE(relation.relacl, acldefault('r', relation.relowner))
            ) AS relation_acl
            WHERE relation_acl.grantee IN (
                0,
                (SELECT oid FROM pg_roles WHERE rolname = current_user)
            )
              AND relation_acl.is_grantable
        ) AS has_grant_option,
        EXISTS (
            SELECT 1
            FROM pg_attribute AS column_info
            -- A NULL column ACL has no column grants; aclexplode returns no rows.
            CROSS JOIN LATERAL aclexplode(column_info.attacl) AS column_acl
            WHERE column_info.attrelid = relation.oid
              AND column_info.attnum > 0
              AND NOT column_info.attisdropped
              AND column_acl.grantee IN (
                  0,
                  (SELECT oid FROM pg_roles WHERE rolname = current_user)
              )
              AND column_acl.is_grantable
        ) AS has_column_grant_option,
        EXISTS (
            SELECT 1
            FROM pg_attribute AS column_info
            CROSS JOIN (
                VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS column_privilege(privilege_name)
            WHERE column_info.attrelid = relation.oid
              AND column_info.attnum > 0
              AND NOT column_info.attisdropped
              AND has_column_privilege(
                  current_user,
                  relation.oid,
                  column_info.attnum,
                  column_privilege.privilege_name
              )
              AND NOT has_table_privilege(
                  current_user, relation.oid, column_privilege.privilege_name
              )
        ) AS has_column_only_privilege
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN (
        VALUES
            ('SELECT'),
            ('INSERT'),
            ('UPDATE'),
            ('DELETE'),
            ('TRUNCATE'),
            ('REFERENCES'),
            ('TRIGGER')
    ) AS privilege(privilege_name)
    WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
    """
)
_TABLE_PRIVILEGES_QUERY_PG17 = text(
    _TABLE_PRIVILEGES_QUERY.text.replace(
        "('TRIGGER')", "('TRIGGER'), ('MAINTAIN')"
    )
)


async def verify_event_consumer_role(
    engine: AsyncEngine,
    *,
    platform_engine: AsyncEngine,
    expected_username: str,
    expected_database_url: str,
) -> None:
    """Fail closed unless the consumer shares the platform server and role scope."""
    platform_url = make_url(expected_database_url)
    if (
        not engine.url.database
        or not platform_url.database
        or _database_endpoint(engine.url) != _database_endpoint(platform_url)
        or _database_endpoint(platform_engine.url) != _database_endpoint(platform_url)
    ):
        raise EventConsumerRoleError(
            "history database connection URL does not match the platform database endpoint"
        )
    async with platform_engine.connect() as platform_connection:
        platform_server = (
            await platform_connection.execute(_SERVER_IDENTITY_QUERY)
        ).mappings().one_or_none()
    if (
        platform_server is None
        or platform_server["database_name"] != platform_url.database
    ):
        raise EventConsumerRoleError(
            "platform database connection reached an unexpected server or database"
        )
    schema_map = engine.sync_engine.get_execution_options().get(
        "schema_translate_map", {}
    )
    schema = schema_map.get(
        ProcessedEvent.__table__.schema, ProcessedEvent.__table__.schema
    )

    async with engine.connect() as connection:
        consumer_server = (
            await connection.execute(_SERVER_IDENTITY_QUERY)
        ).mappings().one_or_none()
        if (
            consumer_server is None
            or platform_server["server_started_at"] is None
            or consumer_server["server_started_at"] is None
            or _server_identity_tuple(consumer_server)
            != _server_identity_tuple(platform_server)
        ):
            raise EventConsumerRoleError(
                "history database connection reached a different PostgreSQL server instance or endpoint"
            )
        identity = (
            await connection.execute(
                text(
                    """
                    WITH RECURSIVE member_roles(role_oid) AS (
                        SELECT membership.roleid
                        FROM pg_auth_members AS membership
                        WHERE membership.member = (
                            SELECT oid FROM pg_roles WHERE rolname = current_user
                        )
                        UNION
                        SELECT membership.roleid
                        FROM pg_auth_members AS membership
                        JOIN member_roles ON membership.member = member_roles.role_oid
                    )
                    SELECT
                        current_user AS username,
                        current_database() AS database_name,
                        role.rolsuper,
                        role.rolcreatedb,
                        role.rolcreaterole,
                        role.rolreplication,
                        role.rolbypassrls,
                        role.rolinherit,
                        current_setting('server_version_num')::integer
                            AS server_version_num,
                        EXISTS (SELECT 1 FROM member_roles) AS has_role_memberships,
                        has_database_privilege(
                            current_user, current_database(), 'CREATE'
                        ) AS can_create_in_database,
                        EXISTS (
                            SELECT 1
                            FROM pg_database AS db_info
                            CROSS JOIN LATERAL aclexplode(
                                COALESCE(
                                    db_info.datacl,
                                    acldefault('d', db_info.datdba)
                                )
                            ) AS database_acl
                            WHERE db_info.datname = current_database()
                              AND database_acl.grantee IN (
                                  0,
                                  (SELECT oid FROM pg_roles WHERE rolname = current_user)
                              )
                              AND database_acl.is_grantable
                        ) AS has_database_grant_option
                    FROM pg_roles AS role
                    WHERE role.rolname = current_user
                    """
                )
            )
        ).mappings().one_or_none()
        if identity is None:
            raise EventConsumerRoleError("could not identify the history database role")
        if identity["username"] != expected_username:
            raise EventConsumerRoleError(
                "history database connection did not use the configured consumer role"
            )
        if identity["database_name"] != engine.url.database:
            raise EventConsumerRoleError(
                "history database connection reached an unexpected database"
            )
        if any(
            identity[key]
            for key in (
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
                "rolinherit",
                "has_role_memberships",
                "can_create_in_database",
                "has_database_grant_option",
            )
        ):
            raise EventConsumerRoleError(
                "history database role has elevated attributes, memberships, "
                "database CREATE, or database GRANT OPTION"
            )

        if schema is None:
            schema = await connection.scalar(text("SELECT current_schema()"))
        if not schema:
            raise EventConsumerRoleError(
                "history database connection has no effective PostgreSQL schema"
            )
        schema_state = (
            await connection.execute(
                text(
                    """
                    SELECT
                        EXISTS (
                            SELECT 1 FROM pg_namespace WHERE nspname = :schema_name
                        ) AS schema_exists,
                        has_schema_privilege(
                            current_user, :schema_name, 'USAGE'
                        ) AS can_use_schema,
                        has_schema_privilege(
                            current_user, :schema_name, 'CREATE'
                        ) AS can_create_in_schema,
                        EXISTS (
                            SELECT 1
                            FROM pg_namespace AS candidate
                            WHERE candidate.nspname <> 'information_schema'
                              AND candidate.nspname !~ '^pg_'
                              AND has_schema_privilege(
                                  current_user, candidate.oid, 'CREATE'
                              )
                        ) AS can_create_in_any_schema,
                        EXISTS (
                            SELECT 1
                            FROM pg_namespace AS candidate
                            CROSS JOIN LATERAL aclexplode(
                                COALESCE(
                                    candidate.nspacl,
                                    acldefault('n', candidate.nspowner)
                                )
                            ) AS schema_acl
                            WHERE candidate.nspname <> 'information_schema'
                              AND candidate.nspname !~ '^pg_'
                              AND schema_acl.grantee IN (
                                  0,
                                  (SELECT oid FROM pg_roles WHERE rolname = current_user)
                              )
                              AND schema_acl.is_grantable
                        ) AS has_schema_grant_option
                    """
                ),
                {"schema_name": schema},
            )
        ).mappings().one()
        if (
            not schema_state["schema_exists"]
            or not schema_state["can_use_schema"]
        ):
            raise EventConsumerRoleError(
                f"history database role lacks USAGE on schema {schema!r}"
            )
        if (
            schema_state["can_create_in_schema"]
            or schema_state["can_create_in_any_schema"]
            or schema_state["has_schema_grant_option"]
        ):
            raise EventConsumerRoleError(
                "history database role can CREATE in a schema or has schema GRANT OPTION"
            )

        privilege_query = (
            _TABLE_PRIVILEGES_QUERY_PG17
            if identity["server_version_num"] >= 170000
            else _TABLE_PRIVILEGES_QUERY
        )
        table_rows = (
            await connection.execute(privilege_query)
        ).mappings().all()
    actual: dict[tuple[str, str], set[str]] = {}
    grant_option_relations: set[tuple[str, str]] = set()
    column_only_relations: set[tuple[str, str]] = set()
    public_privilege_relations: set[tuple[str, str]] = set()
    for row in table_rows:
        key = (row["schema_name"], row["table_name"])
        actual.setdefault(key, set())
        if row["has_privilege"]:
            actual[key].add(row["privilege_name"])
        if row["has_grant_option"] or row["has_column_grant_option"]:
            grant_option_relations.add(key)
        if row["has_column_only_privilege"]:
            column_only_relations.add(key)
        if (
            row["has_public_table_privilege"]
            or row["has_public_column_privilege"]
        ):
            public_privilege_relations.add(key)

    if grant_option_relations:
        raise EventConsumerRoleError(
            "history database role has table or column GRANT OPTION on: "
            + ", ".join(
                f"{table_schema}.{table_name}"
                for table_schema, table_name in sorted(grant_option_relations)
            )
        )
    if column_only_relations:
        raise EventConsumerRoleError(
            "history database role has column-only privileges on: "
            + ", ".join(
                f"{table_schema}.{table_name}"
                for table_schema, table_name in sorted(column_only_relations)
            )
        )
    if public_privilege_relations:
        raise EventConsumerRoleError(
            "history database role has PUBLIC table or column privileges on: "
            + ", ".join(
                f"{table_schema}.{table_name}"
                for table_schema, table_name in sorted(public_privilege_relations)
            )
        )

    expected_schema_tables = {
        (schema, table_name): privileges
        for table_name, privileges in _EVENT_TABLE_PRIVILEGES.items()
    }
    present_event_tables = {
        table_name
        for (table_schema, table_name) in actual
        if table_schema == schema
    }
    missing_tables = set(_EVENT_TABLE_PRIVILEGES) - present_event_tables
    if missing_tables:
        raise EventConsumerRoleError(
            "history database schema is missing event tables: "
            + ", ".join(sorted(missing_tables))
        )

    for key, privileges in actual.items():
        expected = expected_schema_tables.get(key, frozenset())
        if privileges != expected:
            schema_name, table_name = key
            raise EventConsumerRoleError(
                f"history database role privilege mismatch on "
                f"{schema_name}.{table_name}: expected {sorted(expected)}, "
                f"found {sorted(privileges)}"
            )
