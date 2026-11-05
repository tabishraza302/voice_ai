"""Create owner-managed agents."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_agents"
down_revision = "0002_user_constraints"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("default_language", sa.String(35), nullable=True),
        sa.Column("stt_provider", sa.String(40), nullable=True),
        sa.Column("stt_model", sa.String(80), nullable=True),
        sa.Column("llm_provider", sa.String(40), nullable=True),
        sa.Column("llm_model", sa.String(80), nullable=True),
        sa.Column("tts_provider", sa.String(40), nullable=True),
        sa.Column("tts_model", sa.String(80), nullable=True),
        sa.Column("voice_id", sa.String(120), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("name <> '' AND name = btrim(name)", name="agents_name_check"),
        sa.CheckConstraint("slug <> '' AND slug = lower(btrim(slug))", name="agents_slug_normalized_check"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="agents_slug_format_check"),
        sa.CheckConstraint("status IN ('draft', 'published', 'suspended')", name="agents_status_check"),
        sa.CheckConstraint("jsonb_typeof(languages) = 'array'", name="agents_languages_array_check"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="agents_slug_key"),
    )
    op.create_index("ix_agents_owner_id", "agents", ["owner_id"], unique=False)


def downgrade():
    op.drop_index("ix_agents_owner_id", table_name="agents")
    op.drop_table("agents")
