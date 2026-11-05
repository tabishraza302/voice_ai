"""Add persisted voice call sessions, messages, and answers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_sessions"
down_revision = "0006_admin_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("call_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'created'"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("signaling_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("call_type IN ('test', 'public')", name="sessions_call_type_check"),
        sa.CheckConstraint(
            "status IN ('created', 'active', 'completed', 'failed', 'cancelled', 'expired')",
            name="sessions_status_check",
        ),
        sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="sessions_snapshot_object_check"),
        sa.CheckConstraint("outcome IS NULL OR btrim(outcome) <> ''", name="sessions_outcome_nonempty_check"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_agent_status", "sessions", ["agent_id", "status"])
    op.create_index("ix_sessions_owner_created", "sessions", ["owner_id", "created_at"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="messages_sequence_positive_check"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence", name="messages_session_sequence_key"),
    )

    op.create_table(
        "answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(length=128), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "accepted",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column(
            "validation_result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("field_key <> '' AND field_key = btrim(field_key)", name="answers_field_key_check"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_answers_session_created", "answers", ["session_id", "created_at"])


def downgrade():
    op.drop_index("ix_answers_session_created", table_name="answers")
    op.drop_table("answers")
    op.drop_table("messages")
    op.drop_index("ix_sessions_owner_created", table_name="sessions")
    op.drop_index("ix_sessions_agent_status", table_name="sessions")
    op.drop_table("sessions")
