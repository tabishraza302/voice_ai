"""Add azure_stt_english_locale to agents.

Revision ID: 0012_azure_stt_locale
Revises: 0011_remove_fields_and_outcomes
Create Date: 2026-11-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_azure_stt_locale"
down_revision = "0011_remove_fields_and_outcomes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("azure_stt_english_locale", sa.String(35), nullable=True))


def downgrade():
    op.drop_column("agents", "azure_stt_english_locale")
