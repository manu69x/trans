"""003 audit_log append-only trigger

Revision ID: a7f3c9d21e44
Revises: 330342b3afad
Create Date: 2026-09-03 19:10:00.000000

Enforces PRD §13.1 ("Audit log immutabile di accessi, export, approvazioni e
cancellazioni") at the database level: UPDATE and DELETE on ``audit_log`` are
rejected by a trigger, so the immutability holds even for raw SQL access and
not only for the application code paths (which never mutate rows anyway).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7f3c9d21e44'
down_revision: Union[str, Sequence[str], None] = '330342b3afad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
"""

_DOWNGRADE_SQL = """
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
DROP FUNCTION IF EXISTS audit_log_append_only();
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
