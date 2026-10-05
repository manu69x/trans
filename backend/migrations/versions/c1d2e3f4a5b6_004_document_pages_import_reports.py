"""004 document_pages + import_reports (F1 L1 extraction)

Revision ID: b8e4f0a1c2d3
Revises: a7f3c9d21e44
Create Date: 2026-09-03 20:30:00.000000

Per-page L1 extraction results (PRD 5.2: normalised text + raw extraction
with coordinates, per-page confidence, hashes) and the 5.2.6 import report.
The unique ``(document_id, page_number)`` constraint makes the extraction
idempotent: re-running a job re-UPSERTs the same rows instead of duplicating.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID

# revision identifiers, used by Alembic.
revision: str = 'b8e4f0a1c2d3'
down_revision: Union[str, Sequence[str], None] = 'a7f3c9d21e44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'document_pages',
        sa.Column('id', UUID(), nullable=False),
        sa.Column('document_id', UUID(), nullable=False),
        sa.Column('page_number', sa.Integer(), nullable=False),
        sa.Column('extractor', sa.String(length=24), nullable=False),
        sa.Column('confidence', sa.Numeric(6, 4), nullable=False),
        sa.Column('char_count', sa.Integer(), nullable=False),
        sa.Column('suspect_chars', sa.Integer(), nullable=False),
        sa.Column('text_sha256', sa.String(length=64), nullable=False),
        sa.Column('page_sha256', sa.String(length=64), nullable=False),
        sa.Column('normalized_text', sa.Text(), nullable=True),
        sa.Column('page_payload', JSON(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'page_number',
                            name='ux_document_page_number'),
    )
    op.create_index('ix_document_page_document', 'document_pages',
                    ['document_id'])

    op.create_table(
        'import_reports',
        sa.Column('id', UUID(), nullable=False),
        sa.Column('document_id', UUID(), nullable=False),
        sa.Column('summary', JSON(), nullable=False),
        sa.Column('pages_ok', sa.Integer(), nullable=False),
        sa.Column('pages_ocr_needed', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id',
                            name='ux_import_report_document'),
    )
    op.create_index('ix_import_report_document', 'import_reports',
                    ['document_id'])


def downgrade() -> None:
    op.drop_index('ix_import_report_document', table_name='import_reports')
    op.drop_table('import_reports')
    op.drop_index('ix_document_page_document', table_name='document_pages')
    op.drop_table('document_pages')
