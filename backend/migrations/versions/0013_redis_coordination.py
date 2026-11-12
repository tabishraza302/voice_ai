"""Add ownership columns and recovery index to sessions for Redis coordination.

Revision ID: 0013_redis_coordination
Revises: 0012_azure_stt_locale
Create Date: 2026-11-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_redis_coordination"
down_revision = "0012_azure_stt_locale"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("worker_id", sa.String(128), nullable=True))
    op.add_column("sessions", sa.Column("claim_generation", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_sessions_coordination_recovery",
        "sessions",
        ["status", "lease_expires_at", "id"],
        postgresql_where=sa.text("call_type IN ('test', 'public')"),
    )


def downgrade():
    op.drop_index("ix_sessions_coordination_recovery", table_name="sessions")
    op.drop_column("sessions", "lease_expires_at")
    op.drop_column("sessions", "claim_generation")
    op.drop_column("sessions", "worker_id")
