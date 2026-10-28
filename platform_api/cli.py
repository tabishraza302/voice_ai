"""Command-line entrypoints for the platform control plane.

Entrypoints:
- init-db / init-schema: Initialize fresh PostgreSQL platform database
- check-schema: Verify current database schema matches platform metadata

All exceptions and configuration errors are sanitized to prevent credential leaks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from pydantic import ValidationError

from platform_api.config import PlatformSettings, get_settings
from platform_api.db.base import Base
from platform_api.db.schema import (
    SchemaMismatch,
    check_schema_readiness,
    compute_metadata_hash,
    initialize_schema,
)
from platform_api.db.session import create_engine
import platform_api.models  # Ensure all platform models are registered


def _format_safe_validation_error(exc: ValidationError) -> str:
    """Format ValidationError without leaking raw input values, passwords, or secrets."""
    safe_parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []))
        msg = err.get("msg", "invalid value")
        safe_parts.append(f"{loc}: {msg}")
    return "; ".join(safe_parts)


async def _init_db_command(settings: PlatformSettings) -> int:
    """Execute schema initialization with advisory transaction lock."""
    engine = create_engine(settings)
    try:
        metadata_hash = compute_metadata_hash(Base.metadata)
        print(f"Initializing platform schema (version={settings.schema_version}, hash={metadata_hash[:16]}...)...")
        await initialize_schema(
            engine,
            expected_version=settings.schema_version,
            metadata_hash=metadata_hash,
        )
        print("Platform schema initialization completed successfully.")
        return 0
    except SchemaMismatch as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Error: Database operation failed during schema initialization.", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


async def _check_schema_command(settings: PlatformSettings) -> int:
    """Verify if database schema matches expected platform metadata."""
    engine = create_engine(settings)
    try:
        metadata_hash = compute_metadata_hash(Base.metadata)
        ready = await check_schema_readiness(
            engine,
            expected_version=settings.schema_version,
            metadata_hash=metadata_hash,
        )
        if ready:
            print(f"Schema is ready (version={settings.schema_version}).")
            return 0
        else:
            print(f"Schema is not ready or mismatched (expected {settings.schema_version}).", file=sys.stderr)
            return 1
    except Exception:
        print("Error: Database operation failed during schema check.", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch commands."""
    parser = argparse.ArgumentParser(
        prog="platform_api.cli",
        description="Voice AI Platform CLI commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db command
    init_db_parser = subparsers.add_parser(
        "init-db",
        help="Initialize fresh platform database with transactional advisory lock",
    )
    init_db_parser.add_argument(
        "--expected-version",
        default=None,
        help="Optional expected schema version override",
    )

    # init-schema alias
    subparsers.add_parser(
        "init-schema",
        help="Alias for init-db",
    )

    # check-schema command
    subparsers.add_parser(
        "check-schema",
        help="Verify database schema matches current platform metadata",
    )

    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Configuration error: {_format_safe_validation_error(exc)}", file=sys.stderr)
        return 1
    except Exception:
        print("Configuration error: Invalid platform settings.", file=sys.stderr)
        return 1

    if args.command in ("init-db", "init-schema"):
        if getattr(args, "expected_version", None):
            settings = settings.model_copy(update={"schema_version": args.expected_version})
        return asyncio.run(_init_db_command(settings))

    elif args.command == "check-schema":
        return asyncio.run(_check_schema_command(settings))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
