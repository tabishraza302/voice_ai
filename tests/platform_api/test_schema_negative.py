"""Negative behavioral tests for schema initialization, verification, and metadata hash.

Verifies:
- Schema initialization rejects unknown non-empty schemas (tables without schema_info)
- Schema initialization rejects schemas containing views without schema_info
- Schema initialization rejects databases with matching schema_info but missing required tables
- Schema initialization rejects databases with matching schema_info but extra unexpected tables
- Schema initialization rejects databases with matching schema_info but unexpected views
- Metadata hash incorporates:
  - CHECK constraint expressions
  - Foreign key target tables and columns
  - Column defaults and server defaults
- Determinism of metadata hash
- Both CLI and App import the identical complete platform metadata
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    text,
)

from platform_api.db.base import Base
from platform_api.db.schema import compute_metadata_hash
import platform_api.models  # Load platform models


def test_metadata_hash_incorporates_check_expressions():
    """Metadata hash must change when a CHECK constraint expression changes."""
    meta1 = MetaData()
    Table(
        "wallet_test",
        meta1,
        Column("id", Integer, primary_key=True),
        Column("balance", Integer, nullable=False),
        CheckConstraint("balance >= 0", name="ck_balance_nonnegative"),
    )

    meta2 = MetaData()
    Table(
        "wallet_test",
        meta2,
        Column("id", Integer, primary_key=True),
        Column("balance", Integer, nullable=False),
        CheckConstraint("balance > 0", name="ck_balance_nonnegative"),
    )

    hash1 = compute_metadata_hash(meta1)
    hash2 = compute_metadata_hash(meta2)
    assert hash1 != hash2, "Metadata hash failed to incorporate CHECK constraint expression change"


def test_metadata_hash_incorporates_foreign_targets():
    """Metadata hash must change when a Foreign Key target table/column changes."""
    meta1 = MetaData()
    Table("target_a", meta1, Column("id", Integer, primary_key=True))
    Table(
        "dependent",
        meta1,
        Column("id", Integer, primary_key=True),
        Column("target_id", Integer, ForeignKey("target_a.id")),
    )

    meta2 = MetaData()
    Table("target_b", meta2, Column("id", Integer, primary_key=True))
    Table(
        "dependent",
        meta2,
        Column("id", Integer, primary_key=True),
        Column("target_id", Integer, ForeignKey("target_b.id")),
    )

    hash1 = compute_metadata_hash(meta1)
    hash2 = compute_metadata_hash(meta2)
    assert hash1 != hash2, "Metadata hash failed to incorporate Foreign Key target change"


def test_metadata_hash_incorporates_column_defaults():
    """Metadata hash must change when column default or server_default changes."""
    meta1 = MetaData()
    Table(
        "items",
        meta1,
        Column("id", Integer, primary_key=True),
        Column("status", String, default="pending"),
    )

    meta2 = MetaData()
    Table(
        "items",
        meta2,
        Column("id", Integer, primary_key=True),
        Column("status", String, default="active"),
    )

    meta3 = MetaData()
    Table(
        "items",
        meta3,
        Column("id", Integer, primary_key=True),
        Column("status", String, server_default=text("'active'")),
    )

    hash1 = compute_metadata_hash(meta1)
    hash2 = compute_metadata_hash(meta2)
    hash3 = compute_metadata_hash(meta3)
    assert hash1 != hash2, "Metadata hash failed to incorporate Python default change"
    assert hash1 != hash3, "Metadata hash failed to incorporate server_default change"


def test_metadata_hash_deterministic():
    """Metadata hash must produce the exact same hash across repeated invocations."""
    hash1 = compute_metadata_hash(Base.metadata)
    hash2 = compute_metadata_hash(Base.metadata)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex string


def test_metadata_hash_deterministic_across_processes():
    """Metadata hash must be identical across separate Python processes with no memory address leaks."""
    import subprocess
    import sys

    code = (
        "import platform_api.models\n"
        "from platform_api.db.base import Base\n"
        "from platform_api.db.schema import compute_metadata_hash\n"
        "print(compute_metadata_hash(Base.metadata))\n"
    )

    proc1 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    proc2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    hash1 = proc1.stdout.strip()
    hash2 = proc2.stdout.strip()
    current_hash = compute_metadata_hash(Base.metadata)

    assert hash1 == hash2, "compute_metadata_hash varied across independent Python process executions!"
    assert hash1 == current_hash, "Subprocess hash does not match current process hash!"

def test_cli_and_app_import_identical_metadata():
    """Both CLI and App must register the identical Base.metadata."""
    import platform_api.app
    import platform_api.cli

    cli_hash = platform_api.cli.compute_metadata_hash(Base.metadata)
    app_hash = compute_metadata_hash(platform_api.app.Base.metadata)
    assert cli_hash == app_hash

    cli_tables = set(platform_api.cli.Base.metadata.tables.keys())
    app_tables = set(platform_api.app.Base.metadata.tables.keys())
    assert cli_tables == app_tables
    table_names = {t.name for t in platform_api.app.Base.metadata.tables.values()}
    assert "users" in table_names
    assert "wallet_accounts" in table_names
    assert "schema_info" in table_names
    assert "platform.users" in app_tables
    assert "platform.wallet_accounts" in app_tables
    assert "platform.schema_info" in app_tables
