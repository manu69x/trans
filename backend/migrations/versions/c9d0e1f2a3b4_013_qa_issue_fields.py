"""013 qa_issues category / evidence / suggestion (F4 / PRD §10.3, §10.4, §12.4).

F4 (t_b51cb2d6) -- the QA layer (§10.3 / §10.4) needs each issue to carry the
MQM shape the PRD prescribes:

* ``category``   -- the MQM error category (§10.4: accuracy / terminology /
  italian / style / source), so the "solo QA critici" / per-category filters
  and the critic's structured output (§10.3) can be stored;
* ``evidence``   -- the offending span quoted from the target (§10.3:
  "evidenza");
* ``suggestion`` -- what a revisor should do, without a rewrite (§10.3 /
  §10.5: the critic returns a suggestion, not a rewritten text);
* ``severity``   -- widened from ``critical|warn`` to the §10.4 scale
  ``minor|major|critical``.

Revision ID: c9d0e1f2a3b4
Revises: b2c3d4e5f6a7
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # widen the severity enum to the §10.4 scale (minor/major/critical)
    op.execute("ALTER TABLE qa_issues ALTER COLUMN severity "
               "TYPE VARCHAR(16) USING coalesce(severity, 'minor'::text)")
    op.alter_column('qa_issues', 'severity',
                    server_default=sa.text("'minor'"),
                    nullable=False)
    # MQM category (§10.4) + the §10.3 evidence / suggestion fields.
    op.add_column('qa_issues', sa.Column('category', sa.String(48),
                                          nullable=True))
    op.add_column('qa_issues', sa.Column('evidence', sa.Text(),
                                          nullable=True))
    op.add_column('qa_issues', sa.Column('suggestion', sa.Text(),
                                          nullable=True))
    op.create_index('ix_qa_issue_category', 'qa_issues', ['category'])


def downgrade() -> None:
    op.drop_index('ix_qa_issue_category', table_name='qa_issues')
    op.drop_column('qa_issues', 'suggestion')
    op.drop_column('qa_issues', 'evidence')
    op.drop_column('qa_issues', 'category')
    op.execute("ALTER TABLE qa_issues ALTER COLUMN severity "
               "TYPE VARCHAR(16) USING coalesce(severity, 'warn'::text)")
