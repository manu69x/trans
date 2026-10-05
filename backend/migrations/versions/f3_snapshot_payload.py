"""010 add payload to memory_snapshots (F3 / PRD §7.2, §16)

F3: the translation planner (§10.1 step 3, §9.4) must build the prompt from
*immutable, referenced* snapshots. A snapshot row is frozen at creation but
the DB row must remember *what* it froze (the glossary items / TM entries) so
the run can render the exact prompt and the UI can show what was injected.

This adds a ``payload`` JSONB column to ``memory_snapshots`` that stores the
frozen item ids (and, for TM, the item ids that were retrieved for the block).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "f3_snapshot_payload"
down_revision: Union[str, Sequence[str], None] = "d3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'memory_snapshots',
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('memory_snapshots', 'payload')
