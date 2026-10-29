"""Fresh-schema PostgreSQL backup and restore operations.

Archives use PostgreSQL's binary COPY format so every database value is retained
without a lossy Python type conversion. They contain private production data and
must be stored only in the deployment's protected backup location.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid
import zipfile

import asyncpg
from sqlalchemy.engine import make_url

from platform_api.db.base import Base, DEFAULT_PLATFORM_SCHEMA
from platform_api.db.schema import initialize_schema
from platform_api.db.session import create_engine
from platform_api.ops.privacy import sanitize_operator_reason

BACKUP_FORMAT = "voice-ai-platform-backup-v1"
MANIFEST_NAME = "manifest.json"
SCHEMA_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class BackupSummary:
    output_path: Path
    schema: str
    row_counts: dict[str, int]
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class RestoreSummary:
    schema: str
    row_counts: dict[str, int]
    restored_at: datetime


def _tables():
    return tuple(table for table in Base.metadata.sorted_tables if table.name != "schema_info")


def _schema_name(value: str) -> str:
    if not isinstance(value, str) or not SCHEMA_NAME_PATTERN.fullmatch(value):
        raise ValueError("Invalid platform schema name")
    return value


def _dsn(database_url: str) -> str:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError("Invalid PostgreSQL connection settings") from exc
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("Backup and restore require a named PostgreSQL database")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)

def validate_backup_database_url(database_url: str, backup_url: str) -> str:
    """Require a separate read role targeting the same database as the application."""
    return _validate_database_pair(database_url, backup_url, must_match=True)


def validate_restore_target_database_url(database_url: str, target_url: str) -> str:
    """Require a separate restore role and a database distinct from the source."""
    return _validate_database_pair(database_url, target_url, must_match=False)


def _database_endpoint(url: Any) -> tuple[str, int, str]:
    return (url.host or "<unix-socket>").lower(), url.port or 5432, url.database or ""


def _validate_database_pair(database_url: str, other_url: str, *, must_match: bool) -> str:
    if not other_url or not other_url.strip():
        variable = "PLATFORM_BACKUP_DATABASE_URL" if must_match else "PLATFORM_RESTORE_DATABASE_URL"
        raise ValueError(f"{variable} is required")
    try:
        source = make_url(database_url)
        other = make_url(other_url.strip())
    except Exception as exc:
        raise ValueError("Invalid PostgreSQL connection settings") from exc
    for label, url in (("DATABASE_URL", source), ("operations database URL", other)):
        if url.get_backend_name() != "postgresql" or url.get_driver_name() != "asyncpg":
            raise ValueError(f"{label} must use PostgreSQL with asyncpg")
        if not url.username or not url.database:
            raise ValueError(f"{label} must name a role and database")
        if any(
            key.lower() in {"db", "database", "dbname", "host", "hostaddr", "port", "user", "username"}
            for key in url.query
        ):
            raise ValueError(f"{label} must specify its endpoint outside connection query parameters")
    if source.username == other.username:
        raise ValueError("Backup and restore operations require a separate database role")
    same_database = _database_endpoint(source) == _database_endpoint(other)
    if same_database != must_match:
        relation = "the DATABASE_URL database" if must_match else "a database other than DATABASE_URL"
        raise ValueError(f"Operations database URL must target {relation}")
    return other_url.strip()


def _qualified(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def _copy_count(status: str) -> int:
    try:
        operation, count = status.split(" ", 1)
        if operation != "COPY":
            raise ValueError
        return int(count)
    except (AttributeError, ValueError) as exc:
        raise ValueError("PostgreSQL returned an invalid COPY result") from exc


def _validate_max_bytes(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("A positive maximum archive size is required")
    return max_bytes




class _ArchiveEntryWriter:
    def __init__(self, archive_entry: Any, *, max_bytes: int, total: list[int]):
        self._entry = archive_entry
        self._max_bytes = max_bytes
        self._total = total

    def write(self, data: bytes) -> int:
        next_size = self._total[0] + len(data)
        if next_size > self._max_bytes:
            raise ValueError("Backup exceeded its configured size limit")
        written = self._entry.write(data)
        self._total[0] += written
        return written


async def create_backup(
    database_url: str,
    *,
    output_path: Path,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
    schema_version: str,
    metadata_hash: str,
    max_bytes: int,
) -> BackupSummary:
    """Write a consistent, size-bounded backup of the initialized platform schema."""
    schema = _schema_name(schema)
    limit = _validate_max_bytes(max_bytes)
    if not isinstance(metadata_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata_hash):
        raise ValueError("Invalid platform metadata hash")
    if not schema_version or len(schema_version) > 64:
        raise ValueError("Invalid platform schema version")
    tables = _tables()
    created_at = datetime.now(timezone.utc)
    dsn = _dsn(database_url)
    connection = await asyncpg.connect(dsn=dsn)
    row_counts: dict[str, int] = {}
    total = [0]


    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            info = await connection.fetchrow(
                f"SELECT version, metadata_hash FROM {_qualified(schema, 'schema_info')}"
            )
            if info is None or info["version"] != schema_version or info["metadata_hash"] != metadata_hash:
                raise ValueError("Source platform schema does not match this application")

            # The callback must await the async COPY calls while the repeatable-read
            # snapshot is active; keep the archive lifecycle explicit here.
            output = Path(output_path)
            if not output.is_absolute():
                raise ValueError("Backup path must be absolute")
            if not output.parent.is_dir():
                raise ValueError("Backup directory must already exist")
            if output.exists():
                raise FileExistsError("Backup output already exists")
            fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
            temporary = Path(temp_name)
            linked = False
            try:
                os.fchmod(fd, 0o600)
                os.close(fd)
                with zipfile.ZipFile(
                    temporary, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True
                ) as archive:
                    for table in tables:
                        with archive.open(f"tables/{table.name}.bin", mode="w") as entry:
                            writer = _ArchiveEntryWriter(entry, max_bytes=limit, total=total)
                            status = await connection.copy_from_table(
                                table.name,
                                output=writer,
                                schema_name=schema,
                                format="binary",
                            )
                        row_counts[table.name] = _copy_count(status)
                    manifest = {
                        "format": BACKUP_FORMAT,
                        "schema": schema,
                        "schema_version": schema_version,
                        "metadata_hash": metadata_hash,
                        "created_at": created_at.isoformat(),
                        "table_names": [table.name for table in tables],
                        "row_counts": row_counts,
                    }
                    encoded_manifest = json.dumps(
                        manifest, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    if total[0] + len(encoded_manifest) > limit:
                        raise ValueError("Backup exceeded its configured size limit")
                    archive.writestr(MANIFEST_NAME, encoded_manifest)
                size = temporary.stat().st_size
                if size > limit:
                    raise ValueError("Backup exceeded its configured size limit")
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.link(temporary, output)
                linked = True
                temporary.unlink()
                directory_fd = os.open(output.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                if linked:
                    try:
                        output.unlink()
                    except FileNotFoundError:
                        pass
                raise
    finally:
        await connection.close()

    return BackupSummary(
        output_path=Path(output_path),
        schema=schema,
        row_counts=row_counts,
        size_bytes=size,
        created_at=created_at,
    )


def _read_manifest(archive: zipfile.ZipFile, *, schema: str, schema_version: str, metadata_hash: str, max_bytes: int) -> dict[str, Any]:
    names = archive.namelist()
    expected_tables = [table.name for table in _tables()]
    expected_entries = {MANIFEST_NAME, *(f"tables/{name}.bin" for name in expected_tables)}
    if len(names) != len(set(names)) or set(names) != expected_entries:
        raise ValueError("Backup archive has unexpected or missing entries")
    if sum(info.file_size for info in archive.infolist()) > max_bytes:
        raise ValueError("Backup exceeds its configured size limit")
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError("Backup manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != BACKUP_FORMAT
        or manifest.get("schema") != schema
        or manifest.get("schema_version") != schema_version
        or manifest.get("metadata_hash") != metadata_hash
        or manifest.get("table_names") != expected_tables
    ):
        raise ValueError("Backup schema or application version does not match this restore")
    row_counts = manifest.get("row_counts")
    if not isinstance(row_counts, dict) or set(row_counts) != set(expected_tables):
        raise ValueError("Backup row counts are invalid")
    for count in row_counts.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Backup row counts are invalid")
    return manifest


async def restore_backup(
    backup_path: Path,
    target_database_url: str,
    *,
    schema: str = DEFAULT_PLATFORM_SCHEMA,
    schema_version: str,
    metadata_hash: str,
    max_bytes: int,
    reason: str,
) -> RestoreSummary:
    """Restore into an empty platform schema; never merge into or overwrite data."""
    schema = _schema_name(schema)
    limit = _validate_max_bytes(max_bytes)
    safe_reason = sanitize_operator_reason(reason)
    if not isinstance(metadata_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata_hash):
        raise ValueError("Invalid platform metadata hash")
    archive_path = Path(backup_path)
    if not archive_path.is_absolute() or not archive_path.is_file():
        raise ValueError("Backup path must be an absolute regular file")
    if archive_path.stat().st_size > limit:
        raise ValueError("Backup exceeds its configured size limit")
    try:
        archive = zipfile.ZipFile(archive_path, mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Backup archive is invalid") from exc

    target_engine = create_engine(target_database_url, schema=schema)
    target_connection: asyncpg.Connection | None = None
    try:
        with archive:
            manifest = _read_manifest(
                archive,
                schema=schema,
                schema_version=schema_version,
                metadata_hash=metadata_hash,
                max_bytes=limit,
            )
            await initialize_schema(
                target_engine,
                schema=schema,
                expected_version=schema_version,
                metadata_hash=metadata_hash,
            )
            target_connection = await asyncpg.connect(dsn=_dsn(target_database_url))
            for table in _tables():
                count = await target_connection.fetchval(
                    f"SELECT count(*) FROM {_qualified(schema, table.name)}"
                )
                if count:
                    raise ValueError("Restore target platform schema must be empty")

            copied_counts: dict[str, int] = {}
            async with target_connection.transaction():
                for table in _tables():
                    with archive.open(f"tables/{table.name}.bin", mode="r") as source:
                        status = await target_connection.copy_to_table(
                            table.name,
                            source=source,
                            schema_name=schema,
                            format="binary",
                        )
                    copied = _copy_count(status)
                    if copied != manifest["row_counts"][table.name]:
                        raise ValueError("Restored row count does not match the backup manifest")
                    copied_counts[table.name] = copied

                for table in _tables():
                    for column in table.columns:
                        if column.identity is None and (
                            not column.primary_key or column.autoincrement is False
                        ):
                            continue
                        sequence = await target_connection.fetchval(
                            "SELECT pg_get_serial_sequence($1, $2)",
                            f"{schema}.{table.name}",
                            column.name,
                        )
                        if sequence is None:
                            continue
                        max_value = await target_connection.fetchval(
                            f"SELECT max(\"{column.name}\") FROM {_qualified(schema, table.name)}"
                        )
                        if max_value is None:
                            await target_connection.execute("SELECT setval($1, 1, false)", sequence)
                        else:
                            await target_connection.execute("SELECT setval($1, $2, true)", sequence, max_value)
                await target_connection.execute(
                    f"INSERT INTO {_qualified(schema, 'audit_logs')} "
                    "(id, actor_kind, actor_id, session_id, action, target_type, target_id, "
                    "reason, request_id, source_ip, result, change_summary, created_at) "
                    "VALUES ($1, 'operator', NULL, NULL, $2, 'platform_database', NULL, "
                    "$3, NULL, NULL, 'success', $4::jsonb, $5)",
                    uuid.uuid4(),
                    "ops.backup.restored",
                    safe_reason,
                    json.dumps({"row_counts": copied_counts}, sort_keys=True, separators=(",", ":")),
                    datetime.now(timezone.utc),
                )
    finally:
        if target_connection is not None:
            await target_connection.close()
        await target_engine.dispose()

    return RestoreSummary(
        schema=schema,
        row_counts=copied_counts,
        restored_at=datetime.now(timezone.utc),
    )
