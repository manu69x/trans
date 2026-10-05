"""005 ocr columns + source_flags (F1 OCR worker, task t_dd7351bc)

Revision ID: d9e5f1b2c3a4
Revises: b8e4f0a1c2d3
Create Date: 2026-09-04 21:05:00.000000

OCR columns on ``document_pages`` (PRD 5.2 step 4 for scans, ADR-002 L3/L4):
``ocr_suspect`` is the "OCR sospetto" flag consumed by QA (10.2) and the UI
filter (11.2); ``ocr_level`` records the level whose text won the page;
``ocr_mean_line_conf`` is the mean per-line OCR confidence. ``source_flags``
on ``translation_units`` propagates ``{"ocr_suspect": true, "ocr_page": N}``
to the segments originating from a suspect page (5.2 flag propagation).
Existing rows keep working: booleans default to FALSE, flags to ``{}``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'd9e5f1b2c3a4'
down_revision: Union[str, Sequence[str], None] = 'b8e4f0a1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'document_pages',
        sa.Column('ocr_suspect', sa.BOOLEAN(), nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(
        'document_pages',
        sa.Column('ocr_level', sa.String(length=8), nullable=True),
    )
    op.add_column(
        'document_pages',
        sa.Column('ocr_mean_line_conf', sa.Numeric(6, 4), nullable=True),
    )
    op.add_column(
        'translation_units',
        sa.Column('source_flags', JSONB(), nullable=False,
                  server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('translation_units', 'source_flags')
    op.drop_column('document_pages', 'ocr_mean_line_conf')
    op.drop_column('document_pages', 'ocr_level')
    op.drop_column('document_pages', 'ocr_suspect')
