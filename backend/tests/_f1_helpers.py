"""Helpers for the httpx-based F1 tests.

Kept in a separate module so the test files stay focused on the acceptance
criteria they verify.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def session_scope():
    """Yield a DB session and ALWAYS close it on exit.

    The tests must never leak a session: ``SessionLocal().get(...)`` without a
    ``close()`` leaves the connection checked out with an open transaction
    (SQLA ``autocommit=False`` -> the plain SELECT already opened one), holding
    ACCESS SHARE locks on every referenced table. The next test's
    ``Base.metadata.drop_all`` then waits forever on ACCESS EXCLUSIVE and the
    suite hangs. The ORM instance returned by the query keeps a weak reference
    to its session, so CPython does not reclaim the session either -- the leak
    is deterministic, not reference-counting luck.
    """
    from backend.db import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
