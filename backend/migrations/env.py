"""Alembic migration environment for the Trans backend.

Responsibilities:

* Resolve the target database from ``DATABASE_URL`` (or
  ``ALEMBIC_SQLALCHEMY_URI``) so the same config works both inside Docker
  and against a local Postgres.
* Ensure the ``vector`` extension is present before running migrations,
  keeping pgvector columns usable regardless of how the DB volume was
  initialised.
* Import every ORM model via ``backend.models`` so ``Base.metadata`` is
  fully populated for ``alembic upgrade`` and ``alembic revision --autogenerate``.
"""
from __future__ import annotations

import os

from sqlalchemy.engine import Connection, engine_from_config

from alembic import context

# Make the backend package importable when running from the repo root or
# from inside the backend directory.
sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from backend.models import Base  # noqa: E402  (after path is set up)

# --- Configuration ---------------------------------------------------------

config = context.config

# Prefer an explicit override, then the standard DATABASE_URL, then the
# default baked into alembic.ini.
SQLALCHEMY_URI = os.getenv("ALEMBIC_SQLALCHEMY_URI") or os.getenv(
    "DATABASE_URL", config.get_main_option("sqlalchemy.url")
)
config.set_main_option("sqlalchemy.url", SQLALCHEMY_URI)

target_metadata = Base.metadata


def _ensure_vector(conn: Connection) -> None:
    """Create the pgvector extension if it is not yet available.

    Uses the raw DBAPI connection so the DDL runs outside Alembic's
    wrapping transaction and commits immediately.
    """
    dbapi = conn.connection  # underlying psycopg2 connection
    with dbapi.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        if cur.fetchone() is None:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    dbapi.commit()




# --- Runners ---------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,  # safe DDL on Postgres (idempotent CREATE).
        autogenerate=True,
        compare_server_default=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (via a real connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}) or {},
        prefix="sqlalchemy.",
        poolclass=__import__("sqlalchemy").pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_vector(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_server_default=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
