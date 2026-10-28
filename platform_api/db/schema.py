"""Platform database schema initialization and verification.

Implements fresh-only initialization serialized by a PostgreSQL transaction-level
advisory lock. Re-running with an identical schema record and matching tables is a safe no-op.
Non-PostgreSQL dialects, non-empty unknown databases (including schemas with views),
mismatched metadata, or missing/extra tables fail with SchemaMismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    inspect,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base

# Unique 64-bit advisory lock ID for serializing platform schema operations
PLATFORM_SCHEMA_ADVISORY_LOCK_ID = 8100984920448107368


class SchemaMismatch(Exception):
    """Raised when existing database schema does not match expected platform metadata."""

    pass


class SchemaInfo(Base):
    """Tracks applied schema version and canonical metadata hash in the platform database."""

    __tablename__ = "schema_info"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

def _canonicalize_default_value(arg: Any) -> str | None:
    """Canonicalize column defaults deterministically, removing all memory addresses."""
    if arg is None:
        return None
    if callable(arg):
        mod = getattr(arg, "__module__", "") or ""
        qual = getattr(arg, "__qualname__", getattr(arg, "__name__", "callable"))
        if "<lambda>" in qual:
            code = getattr(arg, "__code__", None)
            if code:
                consts_repr = tuple(str(c) for c in code.co_consts if not callable(c))
                return f"lambda:{mod}:{code.co_code.hex()}:{code.co_names}:{consts_repr}"
            return f"lambda:{mod}"
        return f"callable:{mod}:{qual}"
    if hasattr(arg, "text"):
        return str(arg.text)
    return str(arg)


def compute_metadata_hash(metadata: MetaData | None = None) -> str:
    """Compute deterministic SHA-256 hash of all tables, columns, constraints, and defaults."""
    import platform_api.models  # Ensure all platform models are registered

    if metadata is None:
        metadata = Base.metadata

    structure: dict[str, Any] = {}

    for table_name in sorted(metadata.tables.keys()):
        table = metadata.tables[table_name]

        columns = []
        for col in sorted(table.columns, key=lambda c: c.name):
            col_dict: dict[str, Any] = {
                "name": col.name,
                "type": str(col.type),
                "nullable": col.nullable,
                "primary_key": col.primary_key,
                "unique": getattr(col, "unique", False),
            }
            if col.default is not None:
                col_dict["default"] = _canonicalize_default_value(
                    getattr(col.default, "arg", col.default)
                )
            else:
                col_dict["default"] = None

            if col.server_default is not None:
                col_dict["server_default"] = _canonicalize_default_value(
                    getattr(col.server_default, "arg", col.server_default)
                )
            else:
                col_dict["server_default"] = None

            if getattr(col, "onupdate", None) is not None:
                col_dict["onupdate"] = _canonicalize_default_value(
                    getattr(col.onupdate, "arg", col.onupdate)
                )
            else:
                col_dict["onupdate"] = None
            columns.append(col_dict)

        constraints = []
        for c in sorted(table.constraints, key=lambda c: getattr(c, "name", "") or str(c)):
            c_dict: dict[str, Any] = {
                "name": getattr(c, "name", None),
                "type": c.__class__.__name__,
            }
            if isinstance(c, CheckConstraint):
                c_dict["sqltext"] = str(c.sqltext)
            elif isinstance(c, ForeignKeyConstraint):
                c_dict["columns"] = sorted([col.name for col in c.columns])
                c_dict["elements"] = sorted(
                    [str(getattr(fk, "target_fullname", fk.column)) for fk in c.elements]
                )
                c_dict["ondelete"] = c.ondelete
                c_dict["onupdate"] = c.onupdate
            elif isinstance(c, UniqueConstraint):
                c_dict["columns"] = sorted([col.name for col in c.columns])
            elif isinstance(c, PrimaryKeyConstraint):
                c_dict["columns"] = sorted([col.name for col in c.columns])
            constraints.append(c_dict)

        constraints.sort(key=lambda x: (x.get("name") or "", x.get("type") or "", str(x)))

        indexes = []
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            indexes.append({
                "name": idx.name,
                "unique": idx.unique,
                "columns": [c.name for c in idx.columns],
            })

        structure[table_name] = {
            "columns": columns,
            "constraints": constraints,
            "indexes": indexes,
        }

    canonical_json = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatabaseCatalogInventory:
    user_schemas: set[str]
    tables: set[str]
    views: set[str]
    sequences: set[str]
    custom_types: set[str]
    user_functions: set[str]

    @property
    def total_user_objects(self) -> int:
        return (
            len(self.user_schemas)
            + len(self.tables)
            + len(self.views)
            + len(self.sequences)
            + len(self.custom_types)
            + len(self.user_functions)
        )


def _normalize_pg_str(val: Any) -> str:
    """Normalize asyncpg/postgres char or bytes value to clean unicode string."""
    if isinstance(val, bytes):
        return val.decode("ascii", errors="replace")
    return str(val) if val is not None else ""


async def _inspect_catalog_inventory(conn: AsyncConnection) -> DatabaseCatalogInventory:
    """Query PostgreSQL system catalogs for non-system user objects and user-created schemas."""
    # 1. User-created schemas (excluding standard public and system catalogs)
    user_schemas_res = await conn.execute(
        text(
            "SELECT nspname::text FROM pg_namespace "
            "WHERE nspname::text NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast') "
            "  AND nspname::text NOT LIKE 'pg_temp%' "
            "  AND nspname::text NOT LIKE 'pg_toast_temp%'"
        )
    )
    user_schemas = {_normalize_pg_str(r[0]) for r in user_schemas_res.all()}

    # 2. Relations (tables, views, materialized views, sequences, foreign/partitioned tables)
    relations_res = await conn.execute(
        text(
            "SELECT c.relname::text, c.relkind::text, n.nspname::text "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname::text NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "  AND n.nspname::text NOT LIKE 'pg_temp%' "
            "  AND n.nspname::text NOT LIKE 'pg_toast_temp%' "
            "  AND c.relkind::text IN ('r', 'v', 'm', 'S', 'f', 'p')"
        )
    )
    tables: set[str] = set()
    views: set[str] = set()
    sequences: set[str] = set()

    for r in relations_res.all():
        relname = _normalize_pg_str(r[0])
        relkind = _normalize_pg_str(r[1])
        nspname = _normalize_pg_str(r[2])
        name = f"{nspname}.{relname}" if nspname != "public" else relname
        if relkind in ("r", "p", "f"):
            tables.add(name)
        elif relkind in ("v", "m"):
            views.add(name)
        elif relkind == "S":
            sequences.add(name)

    # 3. Custom types (enums, domains, ranges, custom composites)
    types_res = await conn.execute(
        text(
            "SELECT t.typname::text, t.typtype::text, n.nspname::text "
            "FROM pg_type t "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "LEFT JOIN pg_class c ON c.oid = t.typrelid "
            "WHERE n.nspname::text NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "  AND n.nspname::text NOT LIKE 'pg_temp%' "
            "  AND n.nspname::text NOT LIKE 'pg_toast_temp%' "
            "  AND ("
            "    t.typtype::text IN ('e', 'd', 'r', 'm') "
            "    OR (t.typtype::text = 'c' AND c.relkind::text = 'c') "
            "    OR (t.typtype::text = 'b' AND t.typname::text NOT LIKE '_%')"
            "  )"
        )
    )
    custom_types = {
        f"{_normalize_pg_str(r[2])}.{_normalize_pg_str(r[0])}"
        if _normalize_pg_str(r[2]) != "public"
        else _normalize_pg_str(r[0])
        for r in types_res.all()
    }

    # 4. User functions and procedures
    procs_res = await conn.execute(
        text(
            "SELECT p.proname::text, n.nspname::text "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname::text NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "  AND n.nspname::text NOT LIKE 'pg_temp%' "
            "  AND n.nspname::text NOT LIKE 'pg_toast_temp%'"
        )
    )
    user_functions = {
        f"{_normalize_pg_str(r[1])}.{_normalize_pg_str(r[0])}"
        if _normalize_pg_str(r[1]) != "public"
        else _normalize_pg_str(r[0])
        for r in procs_res.all()
    }

    return DatabaseCatalogInventory(
        user_schemas=user_schemas,
        tables=tables,
        views=views,
        sequences=sequences,
        custom_types=custom_types,
        user_functions=user_functions,
    )


async def initialize_schema(
    engine: AsyncEngine,
    *,
    expected_version: str = "platform-v1",
    metadata_hash: str | None = None,
) -> None:
    """Initialize fresh platform PostgreSQL database or verify identical existing schema.

    Serialized with PostgreSQL transaction-level advisory lock.
    - Non-PostgreSQL dialect: raises SchemaMismatch.
    - Empty database (0 tables and 0 views): creates platform metadata and inserts schema_info record.
    - Non-empty unknown database (lacks schema_info or has views): raises SchemaMismatch.
    - Matching schema_info but missing/extra tables or views: raises SchemaMismatch.
    - Identical recorded schema and tables: safe no-op.
    """
    if engine.dialect.name != "postgresql":
        raise SchemaMismatch(
            f"SCHEMA_MISMATCH: Platform database requires PostgreSQL; got {engine.dialect.name}."
        )

    import platform_api.models  # Ensure all platform models are registered

    if metadata_hash is None:
        metadata_hash = compute_metadata_hash(Base.metadata)

    async with engine.begin() as conn:
        # Acquire transaction-level advisory lock on PostgreSQL
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": PLATFORM_SCHEMA_ADVISORY_LOCK_ID},
        )

        inventory = await _inspect_catalog_inventory(conn)

        # 1. Fresh database case: completely empty of all user objects and schemas
        if inventory.total_user_objects == 0:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                SchemaInfo.__table__.insert().values(
                    version=expected_version,
                    metadata_hash=metadata_hash,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            return

        # 2. Non-empty database: schema_info must exist
        if "schema_info" not in inventory.tables:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Database is non-empty (found {len(inventory.tables)} tables, "
                f"{len(inventory.views)} views, {len(inventory.sequences)} sequences, "
                f"{len(inventory.custom_types)} types, {len(inventory.user_functions)} functions, "
                f"{len(inventory.user_schemas)} schemas) and lacks schema_info table. "
                "Refusing to initialize non-empty unknown database."
            )

        # 3. Read recorded schema_info
        result = await conn.execute(
            select(SchemaInfo.version, SchemaInfo.metadata_hash)
        )
        rows = result.all()

        if not rows:
            raise SchemaMismatch(
                "SCHEMA_MISMATCH: schema_info table is empty. Database is in an invalid state."
            )

        if len(rows) != 1:
            recorded_desc = ", ".join(f"(version={r[0]}, hash={r[1]})" for r in rows)
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Expected single schema_info row; found: {recorded_desc}"
            )

        rec_version, rec_hash = rows[0]
        if rec_version != expected_version or rec_hash != metadata_hash:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Expected version={expected_version}, hash={metadata_hash}; "
                f"found recorded schema: version={rec_version}, hash={rec_hash}"
            )

        # 4. Check for missing or extra tables despite matching schema_info
        expected_tables = set(Base.metadata.tables.keys())
        missing_tables = expected_tables - inventory.tables
        extra_tables = inventory.tables - expected_tables
        if missing_tables or extra_tables:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Database tables do not match expected metadata despite matching schema_info. "
                f"Missing tables: {sorted(missing_tables)}; Extra tables: {sorted(extra_tables)}."
            )

        # 5. Check for unexpected views, schemas, or custom types
        if inventory.views:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected views found in database: {sorted(inventory.views)}. "
                "Refusing unknown nonempty schema with views."
            )
        if inventory.user_schemas:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected user schemas found in database: {sorted(inventory.user_schemas)}."
            )
        if inventory.custom_types:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected custom types found in database: {sorted(inventory.custom_types)}."
            )

        # Identical schema and tables: safe no-op
        return


async def check_schema_readiness(
    engine: AsyncEngine,
    *,
    expected_version: str = "platform-v1",
    metadata_hash: str | None = None,
) -> bool:
    """Read-only check if the platform database has the expected schema version, hash, and tables."""
    if engine.dialect.name != "postgresql":
        return False

    import platform_api.models  # Ensure all platform models are registered

    if metadata_hash is None:
        metadata_hash = compute_metadata_hash(Base.metadata)

    async with engine.connect() as conn:
        inventory = await _inspect_catalog_inventory(conn)

        if "schema_info" not in inventory.tables:
            return False

        result = await conn.execute(
            select(SchemaInfo.version, SchemaInfo.metadata_hash)
        )
        rows = result.all()
        if len(rows) != 1:
            return False

        rec_version, rec_hash = rows[0]
        if rec_version != expected_version or rec_hash != metadata_hash:
            return False

        expected_tables = set(Base.metadata.tables.keys())
        if inventory.tables != expected_tables:
            return False

        if inventory.views or inventory.user_schemas or inventory.custom_types:
            return False

        return True
