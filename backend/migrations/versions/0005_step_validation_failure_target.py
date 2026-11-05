"""Add an explicit target for validation failures."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_step_validation"
down_revision = "0004_steps"
branch_labels = None
depends_on = None


def upgrade():
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
        sa.Column(
            "validation_failure_target",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("steps", "validation_failure_target")
    op.drop_column("steps", "enum_values")
