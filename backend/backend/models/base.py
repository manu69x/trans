"""Declarative base for all ORM models.

The single source of truth for the ORM ``Base`` lives in :mod:`backend.db`:
defining it here as well would create a *second* ``DeclarativeBase`` with a
separate ``metadata``, so tables registered by the model modules would not be
seen by :func:`backend.db.Base.metadata.create_all` / ``drop_all`` (used by
:func:`backend.main.create_app` and the test fixtures). Re-exporting ``db.Base``
keeps every model on one metadata object.
"""
from __future__ import annotations

from ..db import Base  # noqa: F401  (re-exported; keeps one shared metadata)

__all__ = ["Base"]
