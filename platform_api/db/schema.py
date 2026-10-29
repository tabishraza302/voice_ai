"""Platform database schema initialization and verification.

Implements fresh-only initialization serialized by a PostgreSQL transaction-level
advisory lock. Re-running with an identical schema record and matching tables is a safe no-op.
Non-PostgreSQL dialects, non-empty unknown databases (including schemas with views),
mismatched metadata, or missing/extra tables fail with SchemaMismatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

from platform_api.db.base import Base, DEFAULT_PLATFORM_SCHEMA

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
    schema_exists: bool
    tables: set[str]
    views: set[str]
    sequences: set[str]
    custom_types: set[str]
    user_functions: set[str]
    user_schemas: set[str] = field(default_factory=set)

    @property
    def total_user_objects(self) -> int:
        return (
            len(self.tables)
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


async def _inspect_catalog_inventory(
    conn: AsyncConnection,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
) -> DatabaseCatalogInventory:
    """Query PostgreSQL system catalogs for objects restricted to the specified platform schema."""
    # 1. Check if the schema exists
    schema_res = await conn.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
        {"schema": schema},
    )
    schema_exists = bool(schema_res.scalar())

    if not schema_exists:
        return DatabaseCatalogInventory(
            schema_exists=False,
            tables=set(),
            views=set(),
            sequences=set(),
            custom_types=set(),
            user_functions=set(),
            user_schemas=set(),
        )

    # 2. Relations within the target schema (tables, views, sequences, etc.)
    relations_res = await conn.execute(
        text(
            "SELECT c.relname::text, c.relkind::text "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema "
            "  AND c.relkind::text IN ('r', 'v', 'm', 'S', 'f', 'p')"
        ),
        {"schema": schema},
    )
    tables: set[str] = set()
    views: set[str] = set()
    sequences: set[str] = set()

    for r in relations_res.all():
        relname = _normalize_pg_str(r[0])
        relkind = _normalize_pg_str(r[1])
        if relkind in ("r", "p", "f"):
            tables.add(relname)
        elif relkind in ("v", "m"):
            views.add(relname)
        elif relkind == "S":
            sequences.add(relname)

    # 3. Custom types within the target schema
    types_res = await conn.execute(
        text(
            "SELECT t.typname::text "
            "FROM pg_type t "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "LEFT JOIN pg_class c ON c.oid = t.typrelid "
            "WHERE n.nspname = :schema "
            "  AND ("
            "    t.typtype::text IN ('e', 'd', 'r', 'm') "
            "    OR (t.typtype::text = 'c' AND c.relkind::text = 'c') "
            "    OR (t.typtype::text = 'b' AND t.typname::text NOT LIKE '_%')"
            "  )"
        ),
        {"schema": schema},
    )
    custom_types = {_normalize_pg_str(r[0]) for r in types_res.all()}

    # 4. User functions and procedures within the target schema
    procs_res = await conn.execute(
        text(
            "SELECT p.proname::text "
            "FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :schema"
        ),
        {"schema": schema},
    )
    user_functions = {_normalize_pg_str(r[0]) for r in procs_res.all()}

    return DatabaseCatalogInventory(
        schema_exists=True,
        tables=tables,
        views=views,
        sequences=sequences,
        custom_types=custom_types,
        user_functions=user_functions,
        user_schemas={schema},
    )

async def initialize_schema(
    engine: AsyncEngine,
    *,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
    expected_version: str = "platform-v1",
    metadata_hash: str | None = None,
) -> None:
    """Initialize fresh platform PostgreSQL schema or verify identical existing schema.

    Serialized with PostgreSQL transaction-level advisory lock.
    - Non-PostgreSQL dialect: raises SchemaMismatch.
    - Missing or empty platform schema: creates platform schema, tables, and inserts schema_info record.
    - Non-empty unknown platform schema (lacks schema_info or has unexpected objects): raises SchemaMismatch.
    - Matching schema_info but missing/extra tables or views: raises SchemaMismatch.
    - Identical recorded schema and tables: safe no-op.
    - Preserves safety against unknown objects within platform schema while tolerating
      legitimate local and Phoenix schemas.
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

        if schema != DEFAULT_PLATFORM_SCHEMA:
            conn = await conn.execution_options(
                schema_translate_map={DEFAULT_PLATFORM_SCHEMA: schema}
            )

        inventory = await _inspect_catalog_inventory(conn, schema=schema)

        # 1. Fresh platform schema case: schema missing or completely empty of all user objects
        if inventory.total_user_objects == 0:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                SchemaInfo.__table__.insert().values(
                    version=expected_version,
                    metadata_hash=metadata_hash,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            return

        # 2. Non-empty platform schema: schema_info must exist
        if "schema_info" not in inventory.tables:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Platform schema '{schema}' is non-empty (found {len(inventory.tables)} tables, "
                f"{len(inventory.views)} views, {len(inventory.sequences)} sequences, "
                f"{len(inventory.custom_types)} types, {len(inventory.user_functions)} functions) "
                "and lacks schema_info table. Refusing to initialize non-empty unknown schema."
            )

        # 3. Read recorded schema_info
        result = await conn.execute(
            select(SchemaInfo.version, SchemaInfo.metadata_hash)
        )
        rows = result.all()

        if not rows:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: schema_info table in schema '{schema}' is empty. Database is in an invalid state."
            )

        if len(rows) != 1:
            recorded_desc = ", ".join(f"(version={r[0]}, hash={r[1]})" for r in rows)
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Expected single schema_info row in schema '{schema}'; found: {recorded_desc}"
            )

        rec_version, rec_hash = rows[0]
        if rec_version != expected_version or rec_hash != metadata_hash:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Expected version={expected_version}, hash={metadata_hash}; "
                f"found recorded schema in '{schema}': version={rec_version}, hash={rec_hash}"
            )

        # 4. Check for missing or extra tables despite matching schema_info
        expected_tables = {t.name for t in Base.metadata.tables.values()}
        missing_tables = expected_tables - inventory.tables
        extra_tables = inventory.tables - expected_tables
        if missing_tables or extra_tables:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Platform schema '{schema}' tables do not match expected metadata despite matching schema_info. "
                f"Missing tables: {sorted(missing_tables)}; Extra tables: {sorted(extra_tables)}."
            )

        # 5. Check for unexpected views, custom types, sequences, or functions within platform schema
        if inventory.views:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected views found in platform schema '{schema}': {sorted(inventory.views)}. "
                "Refusing unknown nonempty schema with views."
            )
        if inventory.sequences:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected sequences found in platform schema '{schema}': {sorted(inventory.sequences)}."
            )
        if inventory.custom_types:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected custom types found in platform schema '{schema}': {sorted(inventory.custom_types)}."
            )
        if inventory.user_functions:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Unexpected user functions found in platform schema '{schema}': {sorted(inventory.user_functions)}."
            )

        # Identical schema and tables: safe no-op
        return


async def check_schema_readiness(
    engine: AsyncEngine,
    *,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
    expected_version: str = "platform-v1",
    metadata_hash: str | None = None,
) -> bool:
    """Read-only check if the platform schema has the expected schema version, hash, and tables."""
    if engine.dialect.name != "postgresql":
        return False

    import platform_api.models  # Ensure all platform models are registered

    if metadata_hash is None:
        metadata_hash = compute_metadata_hash(Base.metadata)

    async with engine.connect() as conn:
        if schema != DEFAULT_PLATFORM_SCHEMA:
            conn = await conn.execution_options(
                schema_translate_map={DEFAULT_PLATFORM_SCHEMA: schema}
            )

        inventory = await _inspect_catalog_inventory(conn, schema=schema)

        if not inventory.schema_exists or "schema_info" not in inventory.tables:
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

        expected_tables = {t.name for t in Base.metadata.tables.values()}
        if inventory.tables != expected_tables:
            return False

        if (
            inventory.views
            or inventory.sequences
            or inventory.custom_types
            or inventory.user_functions
        ):
            return False

        return True
