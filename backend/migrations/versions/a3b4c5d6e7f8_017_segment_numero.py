"""numero progressivo unico per segmento

Revision ID: a3b4c5d6e7f8
Revises: a2b3c4d5e6f7
"""
from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "translation_units",
        sa.Column("numero", sa.Integer(), nullable=True),
    )
    # backfill in ordine di lettura (capitolo, poi ordinal nel capitolo)
    op.execute(
        """
        WITH ord AS (
            SELECT tu.id, row_number() OVER (
                ORDER BY sn.ordinal NULLS LAST, tu.ordinal NULLS LAST, tu.id
            ) AS rn
            FROM translation_units tu
            LEFT JOIN structure_nodes sn ON sn.id = tu.chapter_id
        )
        UPDATE translation_units tu SET numero = ord.rn
        FROM ord WHERE tu.id = ord.id
        """
    )
    op.create_unique_constraint(
        "ux_tu_project_numero", "translation_units", ["project_id", "numero"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "ux_tu_project_numero", "translation_units", type_="unique"
    )
    op.drop_column("translation_units", "numero")
