"""Remove unused step fields and constraints.

Revision ID: 0008_remove_unused_step_fields
Revises: 0007_sessions
Create Date: 2025-05-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_remove_unused_step_fields"
down_revision = "0007_sessions"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    used_keys_by_agent = {}
    missing_steps = []
    for step_id, agent_id, field_key in connection.execute(
        sa.text("SELECT id, agent_id, field_key FROM steps")
    ):
        if field_key is None or not field_key.strip():
            missing_steps.append((step_id, agent_id))
        else:
            used_keys_by_agent.setdefault(agent_id, set()).add(field_key)

    backfills = []
    for step_id, agent_id in missing_steps:
        used_keys = used_keys_by_agent.setdefault(agent_id, set())
        base_field_key = f"legacy_{step_id}"
        field_key = base_field_key
        suffix = 1
        while field_key in used_keys:
            field_key = f"{base_field_key}_{suffix}"
            suffix += 1
        used_keys.add(field_key)
        backfills.append({"field_key": field_key, "step_id": step_id})

    if backfills:
        connection.execute(
            sa.text("UPDATE steps SET field_key = :field_key WHERE id = :step_id"),
            backfills,
        )
    op.drop_constraint("steps_answer_type_check", "steps", type_="check")
    op.drop_constraint("steps_wording_mode_check", "steps", type_="check")

    op.drop_column("steps", "validation_failure_target")
    op.drop_column("steps", "validation_rules")
    op.drop_column("steps", "clarification_instructions")
    op.drop_column("steps", "extraction_instructions")
    op.drop_column("steps", "localized_phrases")
    op.drop_column("steps", "wording_mode")
    op.drop_column("steps", "enum_values")
    op.drop_column("steps", "enabled")
    op.drop_column("steps", "required")
    op.drop_column("steps", "answer_type")


def downgrade():
    op.add_column(
        "steps",
        sa.Column("answer_type", sa.String(16), nullable=True),
    )
    op.add_column(
        "steps",
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "steps",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "steps",
        sa.Column(
            "enum_values",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "steps",
        sa.Column("wording_mode", sa.String(16), nullable=True),
    )
    op.add_column(
        "steps",
        sa.Column(
            "localized_phrases",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "steps",
        sa.Column("extraction_instructions", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "steps",
        sa.Column("clarification_instructions", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "steps",
        sa.Column(
            "validation_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "steps",
        sa.Column(
            "validation_failure_target",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "steps_answer_type_check",
        "steps",
        "answer_type IS NULL OR answer_type IN ('boolean', 'text', 'number', 'currency', 'enum', 'message', 'outcome')",
    )
    op.create_check_constraint(
        "steps_wording_mode_check",
        "steps",
        "wording_mode IS NULL OR wording_mode IN ('exact', 'flexible')",
    )
