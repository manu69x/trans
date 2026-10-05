"""009 model settings (F2, task t_29ec876e)

Adds the §8.2 advanced per-model settings persisted per project so the
frontend can remember temperature / top_p / seed / reasoning / timeout /
retry / max_output / prompt template across sessions.

Revision ID: d3b4c5d6e7f8
Revises: c2a1b2c3d4e5
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'c2a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'projects',
        sa.Column(
            'model_settings',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('projects', 'model_settings')
