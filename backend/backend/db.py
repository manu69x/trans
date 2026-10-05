"""Database engine, session and ORM base for the Trans backend.

pgvector is exposed through the `sqlalchemy_vector` Dialect so that a
`Vector` column type is available on SQLAlchemy models.

THREAD ISOLATION
---------------
Requests run on the AnyIO worker pool and parse jobs run on their own
background threads, but every caller shares one pooled engine. Each
``SessionLocal`` call returns a brand-new :class:`Session` that pulls its own
connection from the pool, so concurrent requests and background jobs never
share a connection -- SQLAlchemy's connection pool hands each session a private
one and each session is only ever used on the thread that created it. This is
the canonical FastAPI + SQLAlchemy pattern and it sidesteps every cross-thread
edge case (no engine is ever disposed out from under an in-flight request).

The parse job scheduler passes only a ``job_id`` to its background worker
(see :meth:`backend.scheduler.InProcessScheduler.enqueue`); the worker loads the
:class:`~backend.models.Job` fresh from its own session. Passing the ``Job``
instance itself would be unsafe: it belongs to the request's session, and once
that session closes its attributes are expired, so touching them from another
thread reloads on a closed session.
"""
from __future__ import annotations

import os
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

try:  # SQLAlchemy >= 2.1 moved Connection into sqlalchemy.dbapi.
    from sqlalchemy.dbapi import Connection
except ImportError:  # pragma: no cover - older SQLAlchemy (2.0) exposes it here.
    from sqlalchemy.engine import Connection


class Base(DeclarativeBase):
    """Shared ORM metadata base for all models."""


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://trans:trans@db:5432/trans")


# A single shared engine (default pooling) is the canonical FastAPI +
# SQLAlchemy pattern. Each ``SessionLocal`` call returns a fresh ``Session``
# that pulls its own connection from the pool, so concurrent requests on
# different AnyIO worker threads never share a connection -- SQLAlchemy's
# connection pool makes each connection thread-local and hands each session a
# private one. Connections return to the pool after each use (they are not
# closed), so an in-flight request is never blocked by another thread closing
# a shared connection.
#
# TCP keepalive: behind Docker Desktop's port-forward an idle pooled
# connection can be silently dropped; worse, the tunnel can swallow an
# in-flight query with the TCP stream looking "established" — the client then
# waits forever for a reply the server never saw. pool_pre_ping and libpq
# keepalives cannot detect that. NullPool removes the class of problem:
# every checkout opens a fresh connection and closes it on return (a local
# connection setup costs ~1 ms, negligible here), so no long-lived socket
# ever sits idle in the pool waiting to rot.
engine = create_engine(
    _database_url(),
    pool_pre_ping=True,
    poolclass=NullPool,
    connect_args={
        "connect_timeout": 10,
        "keepalives": 1,       # libpq-side TCP keepalive (portable)
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    },
)


def SessionLocal() -> Session:
    """Return a fresh :class:`Session` bound to the shared engine.

    Each call creates a new session that owns its own pooled connection, so it
    never contends with another thread's session. This keeps every caller --
    routes, audit helpers, auth routes and the test body -- on a session that
    is safe to use from its own thread.
    """
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session.

    The generator pattern lets FastAPI's `Depends` close the session
    automatically at the end of each request. Each session owns its own
    pooled connection, so it never contends with another thread's connection.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def connect_to_db(conn: Connection) -> None:
    """Ensure pgvector is available on a fresh connection."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        if cur.fetchone() is None:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
