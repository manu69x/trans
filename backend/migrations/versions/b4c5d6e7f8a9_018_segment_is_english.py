"""is_english: QE p(yes) "Is this text written in English?" on the target

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
"""
from alembic import op
import sqlalchemy as sa

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "translation_units",
        sa.Column("is_english", sa.Numeric(5, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("translation_units", "is_english")
