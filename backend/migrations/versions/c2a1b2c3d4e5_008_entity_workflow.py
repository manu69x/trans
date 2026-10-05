"""008 entity workflow fields + versioning (F2, task t_9a827344)

Adds the §6.6 workflow-UI columns to ``entities`` (forbidden targets,
priority, never-translate, allow-inflection, edit version) and the two
new tables that back §15.2 versioning (§13.1) and §15.4 selective
invalidation:

* ``entity_versions``       -- immutable snapshot of an entity after each edit
* ``entity_invalidations``  -- segments flagged as potentially inconsistent
  after an approved entity was changed.

Revision ID: c2a1b2c3d4e5
Revises: c1f0a1b2c3d4
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import DateTime as SADateTime

# revision identifiers, used by Alembic.
revision: str = 'c2a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'c1f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('entities', sa.Column('forbidden_targets', sa.JSON(), nullable=True))
    op.add_column('entities', sa.Column('priority', sa.String(length=24), nullable=True))
    op.add_column('entities', sa.Column('never_translate', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('entities', sa.Column('allow_inflection', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('entities', sa.Column('version', sa.Integer(), nullable=False, server_default=sa.literal(1)))

    op.create_table('entity_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('entity_id', sa.UUID(), sa.ForeignKey('entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('snapshot', JSONB(), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('created_at', SADateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_entity_version_entity', 'entity_versions', ['entity_id'])
    op.create_index('ix_entity_version_entity_version', 'entity_versions', ['entity_id', 'version'])

    op.create_table('entity_invalidations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('entity_id', sa.UUID(), sa.ForeignKey('entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('segment_id', sa.UUID(), sa.ForeignKey('translation_units.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', SADateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_entity_invalidation_segment', 'entity_invalidations', ['segment_id'])


def downgrade() -> None:
    op.drop_index('ix_entity_invalidation_segment', table_name='entity_invalidations')
    op.drop_table('entity_invalidations')
    op.drop_index('ix_entity_version_entity_version', table_name='entity_versions')
    op.drop_index('ix_entity_version_entity', table_name='entity_versions')
    op.drop_table('entity_versions')
    op.drop_column('entities', 'version')
    op.drop_column('entities', 'allow_inflection')
    op.drop_column('entities', 'never_translate')
    op.drop_column('entities', 'priority')
    op.drop_column('entities', 'forbidden_targets')
