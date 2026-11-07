"""Replace steps with goal-based fields and outcomes.

Revision ID: 0010_goal_based_fields
Revises: 0009_chat_test_call_type
Create Date: 2026-11-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import json
import uuid

revision = "0010_goal_based_fields"
down_revision = "0009_chat_test_call_type"
branch_labels = None
depends_on = None


def upgrade():
    # -- Create fields table --
    op.create_table(
        "fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, default=uuid.uuid4),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("guidance", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("required_when", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "field_key", name="fields_agent_field_key_key"),
        sa.CheckConstraint("field_key <> '' AND field_key = btrim(field_key)", name="fields_field_key_nonempty_check"),
        sa.CheckConstraint("sort_order >= 0", name="fields_sort_order_check"),
        sa.CheckConstraint("jsonb_typeof(options) = 'array'", name="fields_options_array_check"),
        sa.CheckConstraint("jsonb_typeof(required_when) = 'array'", name="fields_required_when_array_check"),
    )

    # -- Create outcomes table --
    op.create_table(
        "outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, default=uuid.uuid4),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("required_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "label", name="outcomes_agent_label_key"),
        sa.CheckConstraint("label <> '' AND label = btrim(label)", name="outcomes_label_nonempty_check"),
        sa.CheckConstraint("jsonb_typeof(conditions) = 'array'", name="outcomes_conditions_array_check"),
        sa.CheckConstraint("jsonb_typeof(required_fields) = 'array'", name="outcomes_required_fields_array_check"),
    )

    # -- Migrate steps → fields + outcomes --
    connection = op.get_bind()
    agents_with_steps = connection.execute(
        sa.text("SELECT DISTINCT agent_id FROM steps")
    )
    for (agent_id,) in agents_with_steps:
        steps = list(connection.execute(
            sa.text("SELECT id, field_key, title, objective, branches, sort_order FROM steps WHERE agent_id = :aid ORDER BY sort_order"),
            {"aid": agent_id},
        ))
        seen_outcomes = set()
        for step_row in steps:
            step_id, field_key, title, objective, branches_raw, sort_order = step_row
            if not field_key or not field_key.strip():
                continue
            branches = branches_raw if isinstance(branches_raw, list) else json.loads(branches_raw) if isinstance(branches_raw, str) else []

            # Determine options and required_when from branches of OTHER steps that target this step
            options = []
            for branch in branches:
                conditions = branch.get("conditions", [])
                for cond in conditions:
                    operand = cond.get("operand")
                    if cond.get("field_key") == field_key and operand is not None:
                        val = str(operand).strip()
                        if val and val not in options:
                            options.append(val)

            # First step is always required; others need required_when
            is_first = sort_order == 0 or step_row == steps[0]
            required_when = []

            # Check if any other step's branches point to this step
            if not is_first:
                for other_step in steps:
                    other_branches = other_step[4] if isinstance(other_step[4], list) else json.loads(other_step[4]) if isinstance(other_step[4], str) else []
                    for b in other_branches:
                        if str(b.get("target_step_id", "")) == str(step_id):
                            for cond in b.get("conditions", []):
                                required_when.append({
                                    "field_key": cond.get("field_key", ""),
                                    "operator": cond.get("operator", "eq"),
                                    "operand": cond.get("operand"),
                                })

            field_id = str(uuid.uuid4())
            connection.execute(
                sa.text("""
                    INSERT INTO fields (id, agent_id, field_key, description, guidance, options, required, required_when, sort_order)
                    VALUES (:id, :agent_id, :field_key, :description, :guidance, CAST(:options AS jsonb), :required, CAST(:required_when AS jsonb), :sort_order)
                """),
                {
                    "id": field_id,
                    "agent_id": agent_id,
                    "field_key": field_key,
                    "description": title or field_key,
                    "guidance": objective or "",
                    "options": json.dumps(options),
                    "required": is_first,
                    "required_when": json.dumps(required_when),
                    "sort_order": sort_order,
                },
            )

            # Extract outcomes from terminal branches
            for branch in branches:
                outcome_label = branch.get("outcome")
                if outcome_label and outcome_label not in seen_outcomes:
                    seen_outcomes.add(outcome_label)
                    outcome_conditions = branch.get("conditions", [])
                    outcome_conds = [
                        {"field_key": c.get("field_key", ""), "operator": c.get("operator", "eq"), "operand": c.get("operand")}
                        for c in outcome_conditions
                    ]
                    # Required fields: the current field_key
                    outcome_required = [field_key]
                    connection.execute(
                        sa.text("""
                            INSERT INTO outcomes (id, agent_id, label, conditions, required_fields)
                            VALUES (:id, :agent_id, :label, CAST(:conditions AS jsonb), CAST(:required_fields AS jsonb))
                        """),
                        {
                            "id": str(uuid.uuid4()),
                            "agent_id": agent_id,
                            "label": outcome_label,
                            "conditions": json.dumps(outcome_conds),
                            "required_fields": json.dumps(outcome_required),
                        },
                    )

    # -- Change sessions.outcome from Text to JSONB --
    # Convert existing non-null outcomes to JSON arrays
    connection.execute(
        sa.text("""
            UPDATE sessions
            SET outcome = to_jsonb(ARRAY[jsonb_build_object('label', outcome, 'completed_at', ended_at)])::text
            WHERE outcome IS NOT NULL
        """)
    )
    # The old text check calls btrim(), which does not exist for jsonb.
    op.drop_constraint("sessions_outcome_nonempty_check", "sessions", type_="check")
    op.alter_column("sessions", "outcome", type_=postgresql.JSONB(astext_type=sa.Text()),
                    postgresql_using="outcome::jsonb", nullable=True)
    op.create_check_constraint(
        "sessions_outcome_array_check",
        "sessions",
        "outcome IS NULL OR jsonb_typeof(outcome) = 'array'",
    )

    # -- Drop old steps table --
    op.drop_table("steps")


def downgrade():
    # -- Recreate steps table --
    op.create_table(
        "steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(length=128), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("objective", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("branches", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "field_key", name="steps_agent_field_key_key"),
        sa.UniqueConstraint("agent_id", "sort_order", name="steps_agent_sort_order_key"),
        sa.CheckConstraint("sort_order >= 0", name="steps_sort_order_check"),
        sa.CheckConstraint("field_key IS NULL OR btrim(field_key) <> ''", name="steps_field_key_nonempty_check"),
    )

    # -- Revert sessions.outcome to Text --
    op.drop_constraint("sessions_outcome_array_check", "sessions", type_="check")
    op.alter_column(
        "sessions",
        "outcome",
        type_=sa.Text(),
        postgresql_using="outcome->0->>'label'",
        nullable=True,
    )
    op.create_check_constraint(
        "sessions_outcome_nonempty_check",
        "sessions",
        "outcome IS NULL OR btrim(outcome) <> ''",
    )

    # -- Drop new tables --
    op.drop_table("outcomes")
    op.drop_table("fields")
