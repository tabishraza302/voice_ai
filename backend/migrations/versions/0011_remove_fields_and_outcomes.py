"""Remove fields, outcomes, and answers tables.

Revision ID: 0011_remove_fields_and_outcomes
Revises: 0010_goal_based_fields
Create Date: 2026-11-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_remove_fields_and_outcomes"
down_revision = "0010_goal_based_fields"
branch_labels = None
depends_on = None


def upgrade():
    # Drop answers table
    op.drop_table("answers")

    # Drop outcomes table
    op.drop_table("outcomes")

    # Drop fields table
    op.drop_table("fields")

    # Drop outcome column and constraint from sessions
    op.drop_constraint("sessions_outcome_array_check", "sessions", type_="check")
    op.drop_column("sessions", "outcome")


def downgrade():
    op.add_column("sessions", sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        "sessions_outcome_array_check",
        "sessions",
        "outcome IS NULL OR jsonb_typeof(outcome) = 'array'",
    )

    op.create_table(
        "fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_key", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("guidance", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("options", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("required_when", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("agent_id", "field_key", name="fields_agent_field_key_key"),
        sa.CheckConstraint("field_key <> '' AND field_key = btrim(field_key)", name="fields_field_key_nonempty_check"),
        sa.CheckConstraint("sort_order >= 0", name="fields_sort_order_check"),
        sa.CheckConstraint("jsonb_typeof(options) = 'array'", name="fields_options_array_check"),
        sa.CheckConstraint("jsonb_typeof(required_when) = 'array'", name="fields_required_when_array_check"),
    )

    op.create_table(
        "outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("required_fields", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.UniqueConstraint("agent_id", "label", name="outcomes_agent_label_key"),
        sa.CheckConstraint("label <> '' AND label = btrim(label)", name="outcomes_label_nonempty_check"),
        sa.CheckConstraint("jsonb_typeof(conditions) = 'array'", name="outcomes_conditions_array_check"),
        sa.CheckConstraint("jsonb_typeof(required_fields) = 'array'", name="outcomes_required_fields_array_check"),
    )

    op.create_table(
        "answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("validation_result", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("field_key <> '' AND field_key = btrim(field_key)", name="answers_field_key_check"),
    )
    op.create_index("ix_answers_session_created", "answers", ["session_id", "created_at"])
