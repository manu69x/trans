"""add verification score columns to translation_units

Revision ID: a2b3c4d5e6f7
Revises: 3f1a2b3c4d5e
Create Date: 2026-09-22

QE verification scores (PRD §10.2-bis), one row per segment, each a 0..1
probability produced by the quality-estimation endpoint:
* is_italian    -- "Is this text written in Italian?"
* is_translated -- "Is the second text the Italian translation of the first?"
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "3f1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "translation_units",
        sa.Column("is_italian", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "translation_units",
        sa.Column("is_translated", sa.Numeric(5, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("translation_units", "is_translated")
    op.drop_column("translation_units", "is_italian")
