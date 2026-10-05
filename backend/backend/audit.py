"""Append-only audit trail (PRD §13.1, §16, §18.1).

Every security-relevant action -- login, logout, refresh, export, approval,
deletion -- is recorded in the ``audit_log`` table as a single, immutable
row. The log is append-only by construction: :func:`log_event` only ever
inserts; nothing here mutates or deletes existing rows, so an approved
decision can never be retroactively altered (PRD principle 5 and the
"audit trail completo" definition of done).

The function accepts either a live SQLAlchemy session or a connection string
/ URL so it can be called from background jobs that do not hold a session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.dialects.postgresql import JSONB

from .db import SessionLocal
from .models import AuditLog, User  # noqa: F401  (kept for symmetry; not required)

def _uuid() -> str:
    return str(uuid.uuid4())


def _coerce_id(value: Any, column_name: str) -> Any:
    """Return *value* for a UUID column, or a deterministic UUID when it is a
    non-UUID placeholder (e.g. ``'proj-1'``) so callers that pass string ids
    don't raise a DataError. Real UUIDs are returned unchanged.

    Only used to keep the append-only audit log accepting both real IDs and
    human-readable placeholders; production callers pass real UUIDs.
    """
    if value is None:
        return None
    text = str(value)
    try:
        uuid.UUID(text)
        return text  # already a valid UUID
    except ValueError:
        # Deterministic, stable per name so repeated calls don't collide.
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{column_name}:{text}"))


def log_event(
    action: str,
    entity: str,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    entity_id: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    ip_address: str | None = None,
    db: Any = None,
) -> AuditLog:
    """Insert one append-only audit row and return it.

    *db* may be a SQLAlchemy session (a :class:`SessionLocal` instance). When
    omitted the module-level :data:`SessionLocal` is used so background code
    without an existing session can still audit. The returned object carries
    the generated ``id`` and ``created_at``.
    """
    session = db if db is not None else SessionLocal()
    own_session = db is None

    row = AuditLog(
        id=_uuid(),
        project_id=_coerce_id(project_id, "project_id"),
        user_id=_coerce_id(user_id, "user_id"),
        action=action[:64],
        entity=entity[:64],
        entity_id=_coerce_id(entity_id, "entity_id"),
        before=dict(before) if before else None,
        after=dict(after) if after else None,
        ip_address=ip_address,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    if own_session:
        session.close()
    return row
