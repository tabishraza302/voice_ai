"""Enforce normalized user records."""
from alembic import op
import sqlalchemy as sa

revision = "0002_user_constraints"
down_revision = "0001_users"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        server_default=sa.text("'user'"),
    )
    op.create_check_constraint("users_email_nonempty_check", "users", "email <> ''")
    op.create_check_constraint("users_email_normalized_check", "users", "email = lower(btrim(email))")
    op.create_check_constraint("users_password_hash_nonempty_check", "users", "password_hash <> ''")


def downgrade():
    op.drop_constraint("users_password_hash_nonempty_check", "users", type_="check")
    op.drop_constraint("users_email_normalized_check", "users", type_="check")
    op.drop_constraint("users_email_nonempty_check", "users", type_="check")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'user'"),
        server_default=None,
    )
