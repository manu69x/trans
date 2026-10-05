"""011 link TM entries to the approved segment (F3 / PRD §7.2).

F3 (t_694dfe91): the approve hook (§7.2) must remember *which* approved
segment a TM entry came from, so the maintenance job (§7.2) can flag a
contradictory target on the same source and the dashboard can compute the
TM reuse rate. Adds a nullable ``segment_id`` FK to ``tm_entries``.

Revision ID: a1b2c3d4e5f6
Revises: f3_snapshot_payload
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f3_snapshot_payload'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A plain nullable UUID (NOT a foreign key): an entry whose segment was
    # invalidated / re-segmented must remain in the TM so the maintenance job
    # (§7.2) can flag it as *obsolete*. A real FK would reject the insert of
    # an entry whose segment no longer exists.
    op.add_column(
        'tm_entries',
        sa.Column(
            'segment_id',
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_index('ix_tm_segment', 'tm_entries', ['segment_id'])


def downgrade() -> None:
    op.drop_index('ix_tm_segment', table_name='tm_entries')
    op.drop_column('tm_entries', 'segment_id')
