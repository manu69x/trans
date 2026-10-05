"""012 translation unit versions + QA issues (F3 / PRD §11.2, §10.5).

F3 (t_6c5c528a) -- the bilingual editor needs:

* an **immutable version history** for every segment: each time the human
  refines a ``machine_draft``/``untranslated`` target a new row is appended
  to ``translation_unit_versions`` so the editor can show a diff and the
  §10.5 "diff prima dell'applicazione / entrambe le versioni conservate"
  invariant holds.
* a **QA issues** table so the §11.2 "QA issues" panel and the "solo QA
  critici" filter can select segments deterministically.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'translation_unit_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(),
                  sa.ForeignKey('projects.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('unit_id', sa.UUID(),
                  sa.ForeignKey('translation_units.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('target_text', sa.Text(), nullable=False),
        sa.Column('before', sa.BOOLEAN(), nullable=False, default=False),
        sa.Column('action', sa.String(64), nullable=False,
                  server_default='refine'),
        sa.Column('diff_json', JSONB(), nullable=True, default=dict),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_unit_version_unit',
                    'translation_unit_versions', ['unit_id'])
    op.create_index('ix_unit_version_project',
                    'translation_unit_versions', ['project_id'])

    op.create_table(
        'qa_issues',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(),
                  sa.ForeignKey('projects.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('unit_id', sa.UUID(),
                  sa.ForeignKey('translation_units.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('severity', sa.String(16), nullable=False,
                  server_default='warn'),
        sa.Column('kind', sa.String(64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('resolved', sa.BOOLEAN(), nullable=False, default=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_qa_issue_unit', 'qa_issues', ['unit_id'])
    op.create_index('ix_qa_issue_project', 'qa_issues', ['project_id'])
    op.create_index('ix_qa_issue_severity', 'qa_issues', ['severity'])


def downgrade() -> None:
    op.drop_index('ix_qa_issue_severity', table_name='qa_issues')
    op.drop_index('ix_qa_issue_project', table_name='qa_issues')
    op.drop_index('ix_qa_issue_unit', table_name='qa_issues')
    op.drop_table('qa_issues')
    op.drop_index('ix_unit_version_project',
                  table_name='translation_unit_versions')
    op.drop_table('translation_unit_versions')
