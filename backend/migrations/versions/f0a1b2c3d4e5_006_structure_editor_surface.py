"""006 structure editor surface (F1 viewer/editor, task t_52bc2f78)

Revision ID: f0a1b2c3d4e5
Revises: d9e5f1b2c3a4
Create Date: 2026-09-05 02:20:00.000000

No schema change is required: the editor operations (PRD 5.3 creare/
unire/dividere/spostare/rinominare, 15.1 boundary correction) work on
the existing ``structure_nodes`` + ``translation_units`` tables, and the
undo trail reuses the append-only ``audit_log`` (13.1).  This revision
formalises the editor's audit actions as a documented no-op upgrade so
environments provisioned only via Alembic and via ``create_all``
converge.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'd9e5f1b2c3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Intentional no-op (see module docstring)."""


def downgrade() -> None:
    """Intentional no-op (see module docstring)."""
