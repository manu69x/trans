"""014 qa_issues span / comment (F4 / PRD §10.4, §18.2).

F4 (t_efb376a9) -- the human MQM annotation (§10.4) needs each issue to carry
the *annotatable span* and the revisor's free-text comment:

* ``span`` -- the target span the revisor selected (§10.4: "selezione span
  nel target");
* ``comment`` -- the revisor's free-text note on that span (§10.4).

They complement the MQM ``category`` / ``severity`` already stored and let the
QA page render "selected span → category → severity → comment" (§10.4) and the
"issue linked to segment + span + category + severity" (§15.4 / AC3).

Revision ID: 3f1a2b3c4d5e
Revises: c9d0e1f2a3b4
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f1a2b3c4d5e'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('qa_issues', sa.Column('span', sa.Text(), nullable=True))
    op.add_column('qa_issues', sa.Column('comment', sa.Text(), nullable=True))
    op.create_index('ix_qa_issue_kind', 'qa_issues', ['kind'])


def downgrade() -> None:
    op.drop_index('ix_qa_issue_kind', table_name='qa_issues')
    op.drop_column('qa_issues', 'comment')
    op.drop_column('qa_issues', 'span')
