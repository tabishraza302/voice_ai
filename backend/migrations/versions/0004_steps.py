"""Create ordered agent steps."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_steps"
down_revision = "0003_agents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(128), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("answer_type", sa.String(16), nullable=True),
        sa.Column("wording_mode", sa.String(16), nullable=True),
        sa.Column(
            "localized_phrases",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("extraction_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("clarification_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "validation_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "branches",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.UniqueConstraint("agent_id", "field_key", name="steps_agent_field_key_key"),
        sa.UniqueConstraint("agent_id", "sort_order", name="steps_agent_sort_order_key"),
        sa.CheckConstraint("sort_order >= 0", name="steps_sort_order_check"),
        sa.CheckConstraint("field_key IS NULL OR btrim(field_key) <> ''", name="steps_field_key_nonempty_check"),
        sa.CheckConstraint(
            "answer_type IS NULL OR answer_type IN ('boolean', 'text', 'number', 'currency', 'enum', 'message', 'outcome')",
            name="steps_answer_type_check",
        ),
        sa.CheckConstraint(
            "wording_mode IS NULL OR wording_mode IN ('exact', 'flexible')",
            name="steps_wording_mode_check",
        ),
    )


def downgrade():
    op.drop_table("steps")
