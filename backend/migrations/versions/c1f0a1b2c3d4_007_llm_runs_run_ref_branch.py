"""007 add run_ref/branch to llm_runs (PRD §8.4, F2)

Adds idempotency (run_ref) and resumable-branch (branch) columns to
``llm_runs`` so that F2 can record repeatable runs and resume branches
without autoswitching (§8.4).

Revision ID: c1f0a1b2c3d4
Revises: f0a1b2c3d4e5
Create Date: 2026-09-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1f0a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = 'f0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_runs', sa.Column('run_ref', sa.String(length=64), nullable=True))
    op.add_column('llm_runs', sa.Column('branch', sa.String(length=64), nullable=True))
    op.create_index('ix_llm_runs_run_ref', 'llm_runs', ['run_ref'], unique=False)
    op.create_index('ix_llm_runs_branch', 'llm_runs', ['branch'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_llm_runs_branch', table_name='llm_runs')
    op.drop_index('ix_llm_runs_run_ref', table_name='llm_runs')
    op.drop_column('llm_runs', 'branch')
    op.drop_column('llm_runs', 'run_ref')
