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
from typing import Any, TYPE_CHECKING

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
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base, DEFAULT_PLATFORM_SCHEMA
if TYPE_CHECKING:
    from platform_api.billing.tariffs import InitialSessionTariff


# Unique 64-bit advisory lock ID for serializing platform schema operations
CURRENT_SCHEMA_VERSION = "platform-v3"
INTERMEDIATE_SCHEMA_VERSION = "platform-v2"
LEGACY_SCHEMA_VERSION = "platform-v1"
PROVIDER_SECRET_TABLE = "provider_secrets"
MODEL_ENTRY_TABLE = "provider_model_entries"
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


def compute_metadata_hash(
    metadata: MetaData | None = None,
    *,
    exclude_tables: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Compute deterministic SHA-256 hash of schema metadata.

    ``exclude_tables`` exists for compatibility checks against explicitly
    supported historical schemas before applying a narrow migration.
    """
    import platform_api.models  # Ensure all platform models are registered

    if metadata is None:
        metadata = Base.metadata

    excluded = set(exclude_tables)
    structure: dict[str, Any] = {}

    for table_name in sorted(metadata.tables.keys()):
        table = metadata.tables[table_name]
        if (
            table_name in excluded
            or table.name in excluded
            or table.fullname in excluded
        ):
            continue

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
    sequence_owners: dict[str, tuple[str, str, str] | None] = field(default_factory=dict)

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
            sequence_owners={},
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

    sequence_owners_res = await conn.execute(
        text(
            "SELECT seq.relname::text, owner_ns.nspname::text, "
            "owner.relname::text, owner_att.attname::text "
            "FROM pg_class seq "
            "JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace "
            "LEFT JOIN pg_depend dep "
            "  ON dep.classid = 'pg_class'::regclass "
            " AND dep.objid = seq.oid "
            " AND dep.refclassid = 'pg_class'::regclass "
            " AND dep.deptype::text IN ('a', 'i') "
            "LEFT JOIN pg_class owner ON owner.oid = dep.refobjid "
            "LEFT JOIN pg_namespace owner_ns ON owner_ns.oid = owner.relnamespace "
            "LEFT JOIN pg_attribute owner_att "
            "  ON owner_att.attrelid = owner.oid AND owner_att.attnum = dep.refobjsubid "
            "WHERE seq_ns.nspname = :schema AND seq.relkind::text = 'S'"
        ),
        {"schema": schema},
    )
    sequence_owners: dict[str, tuple[str, str, str] | None] = {}
    for r in sequence_owners_res.all():
        sequence_name = _normalize_pg_str(r[0])
        owner = (
            (_normalize_pg_str(r[1]), _normalize_pg_str(r[2]), _normalize_pg_str(r[3]))
            if all(value is not None for value in r[1:])
            else None
        )
        if sequence_name in sequence_owners and sequence_owners[sequence_name] != owner:
            sequence_owners[sequence_name] = None
        else:
            sequence_owners[sequence_name] = owner

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
        sequence_owners=sequence_owners,
    )


def _sequence_mismatches(
    inventory: DatabaseCatalogInventory,
    *,
    schema: str,
) -> tuple[set[str], set[tuple[str, str, str]]]:
    expected_owners = {
        (schema, table.name, column.name)
        for table in Base.metadata.tables.values()
        if (column := table.autoincrement_column) is not None
    }
    actual_by_owner: dict[tuple[str, str, str], set[str]] = {}
    unexpected: set[str] = set()

    for sequence_name in inventory.sequences:
        owner = inventory.sequence_owners.get(sequence_name)
        if owner not in expected_owners:
            unexpected.add(sequence_name)
            continue
        actual_by_owner.setdefault(owner, set()).add(sequence_name)

    for sequence_names in actual_by_owner.values():
        if len(sequence_names) > 1:
            unexpected.update(sequence_names)

    missing = expected_owners - actual_by_owner.keys()
    return unexpected, missing


async def initialize_schema(
    engine: AsyncEngine,
    *,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
    expected_version: str = CURRENT_SCHEMA_VERSION,
    metadata_hash: str | None = None,
    initial_tariff: InitialSessionTariff | None = None,
    allow_unseeded_restore: bool = False,
) -> None:
    """Initialize or verify the platform schema; fresh setup requires an explicit tariff seed.

    Serialized with PostgreSQL transaction-level advisory lock.
    - Non-PostgreSQL dialect: raises SchemaMismatch.
    - Missing or empty platform schema creates tables and records the schema only after an
      explicit tariff seed; only backup restore may set `allow_unseeded_restore=True` to import the archived tariff.
    - An exact platform-v1 inventory migrates to platform-v3 by adding provider_secrets
      and provider_model_entries; an exact platform-v2 inventory migrates by adding only
      provider_model_entries. Each migration preserves existing rows and records schema_info
      in the same transaction; unknown drift is refused.
    - Non-empty unknown platform schema (lacks schema_info or has unexpected objects): raises SchemaMismatch.
    - Matching schema_info with missing/extra tables, missing generated sequences, or unexpected objects: raises SchemaMismatch.
    - Identical recorded schema and tables: safe no-op; initial tariff values are ignored.
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

        if inventory.total_user_objects == 0:
            if initial_tariff is None and not allow_unseeded_restore:
                raise SchemaMismatch(
                    "SCHEMA_MISMATCH: Fresh platform schema initialization requires explicit "
                    "initial session rate, billing slice, and permit TTL values."
                )
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
            if initial_tariff is not None:
                from platform_api.billing.tariffs import seed_initial_active_tariff

                await seed_initial_active_tariff(conn, initial_tariff)
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
        expected_tables = {t.name for t in Base.metadata.tables.values()}
        migration_additions = {
            LEGACY_SCHEMA_VERSION: (
                {PROVIDER_SECRET_TABLE, MODEL_ENTRY_TABLE},
                (PROVIDER_SECRET_TABLE, MODEL_ENTRY_TABLE),
            ),
            INTERMEDIATE_SCHEMA_VERSION: (
                {MODEL_ENTRY_TABLE},
                (MODEL_ENTRY_TABLE,),
            ),
        }
        migration = (
            migration_additions.get(rec_version)
            if expected_version == CURRENT_SCHEMA_VERSION
            else None
        )
        if migration is not None:
            excluded_tables, tables_to_add = migration
            expected_current_hash = compute_metadata_hash()
            expected_previous_hash = compute_metadata_hash(
                exclude_tables=excluded_tables
            )
            expected_previous_tables = expected_tables - set(tables_to_add)
            tables_by_name = {
                table.name: table
                for table in Base.metadata.tables.values()
                if table.name in tables_to_add
            }
            if (
                set(tables_by_name) != set(tables_to_add)
                or metadata_hash != expected_current_hash
                or rec_hash != expected_previous_hash
            ):
                raise SchemaMismatch(
                    f"SCHEMA_MISMATCH: Cannot safely migrate schema '{schema}' from "
                    f"{rec_version}; recorded metadata does not match the supported "
                    "historical schema."
                )
            if inventory.tables != expected_previous_tables:
                raise SchemaMismatch(
                    f"SCHEMA_MISMATCH: Cannot safely migrate schema '{schema}'; "
                    f"expected historical tables {sorted(expected_previous_tables)}, "
                    f"found {sorted(inventory.tables)}."
                )
            if inventory.views or inventory.custom_types or inventory.user_functions:
                raise SchemaMismatch(
                    f"SCHEMA_MISMATCH: Cannot safely migrate schema '{schema}' with "
                    "unexpected views, types, or functions."
                )
            unexpected_sequences, missing_sequence_owners = _sequence_mismatches(
                inventory, schema=schema
            )
            if unexpected_sequences or missing_sequence_owners:
                raise SchemaMismatch(
                    f"SCHEMA_MISMATCH: Cannot safely migrate schema '{schema}' with "
                    "unexpected or missing generated sequences."
                )

            await conn.run_sync(
                lambda sync_conn: [
                    tables_by_name[name].create(sync_conn, checkfirst=False)
                    for name in tables_to_add
                ]
            )
            update_result = await conn.execute(
                update(SchemaInfo)
                .where(SchemaInfo.version == rec_version)
                .values(
                    version=expected_version,
                    metadata_hash=metadata_hash,
                    applied_at=datetime.now(timezone.utc),
                )
            )
            if update_result.rowcount != 1:
                raise SchemaMismatch(
                    f"SCHEMA_MISMATCH: Could not atomically record schema migration "
                    f"to {expected_version}."
                )
            inventory = await _inspect_catalog_inventory(conn, schema=schema)
            rec_version, rec_hash = expected_version, metadata_hash

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
        unexpected_sequences, missing_sequence_owners = _sequence_mismatches(
            inventory, schema=schema
        )
        if unexpected_sequences or missing_sequence_owners:
            raise SchemaMismatch(
                f"SCHEMA_MISMATCH: Platform schema '{schema}' sequences do not match expected metadata. "
                f"Unexpected sequences: {sorted(unexpected_sequences)}; "
                f"Missing generated sequences: {sorted(missing_sequence_owners)}."
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
    expected_version: str = CURRENT_SCHEMA_VERSION,
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

        unexpected_sequences, missing_sequence_owners = _sequence_mismatches(
            inventory, schema=schema
        )
        if (
            inventory.views
            or unexpected_sequences
            or missing_sequence_owners
            or inventory.custom_types
            or inventory.user_functions
        ):
            return False

        return True
