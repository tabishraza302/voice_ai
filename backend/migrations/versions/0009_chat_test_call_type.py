"""Allow chat_test call type and nullable signaling_expires_at.

Revision ID: 0009_chat_test_call_type
Revises: 0008_remove_unused_step_fields
Create Date: 2026-11-07 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_chat_test_call_type"
down_revision = "0008_remove_unused_step_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("sessions_call_type_check", "sessions", type_="check")
    op.create_check_constraint(
        "sessions_call_type_check",
        "sessions",
        "call_type IN ('test', 'public', 'chat_test')",
    )
    op.alter_column(
        "sessions",
        "signaling_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade():
    op.execute("DELETE FROM sessions WHERE call_type = 'chat_test' OR signaling_expires_at IS NULL")
    op.drop_constraint("sessions_call_type_check", "sessions", type_="check")
    op.create_check_constraint(
        "sessions_call_type_check",
        "sessions",
        "call_type IN ('test', 'public')",
    )
    op.alter_column(
        "sessions",
        "signaling_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
