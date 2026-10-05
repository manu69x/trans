"""Shared pytest fixtures for the Trans backend test suite.

The per-test ``_reset`` fixtures in the F1 test modules rebuild the schema
with ``Base.metadata.drop_all/create_all``. That wipes every DB-level
artifact that ``create_all`` cannot know about -- notably the append-only
trigger on ``audit_log`` (migration 003, PRD §13.1). This conftest restores
those artifacts once at session end, so a pytest run never leaves the
database without the production enforcement (``verify_f1_e2e.py`` checks it
against the same DB).

ISOLAMENTO (fix 2026-09-19): TUTTI i test girano sul DB dedicato
``trans_test`` (creato e migrato qui sotto). Il default precedente era il
DB live ``trans``: un modulo senza override proprio (es. test_f2_*) eseguiva
drop_all SUL DATABASE REALE, cancellando utenti e progetti.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_test",
)
# Assegnazione INCONDIZIONATA prima di ogni import di backend.*: il modulo
# senza override proprio erede trans_test, mai il DB live.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ALEMBIC_SQLALCHEMY_URI", TEST_DATABASE_URL)

BACKEND_DIR = Path(__file__).resolve().parent.parent

# §13 rate-limit buckets that the functional tests would otherwise exhaust
# (each run/export is cheap in tests but numerous): raise the burst BEFORE
# backend modules are imported (limits are read at import time). The 429
# mechanism itself is covered by test_f4_hardening (auth/upload buckets) and
# test_api_authz_enforcement (translate bucket, tuned in-process).
os.environ.setdefault("TRANS_RATE_TRANSLATE_BURST", "1000")
os.environ.setdefault("TRANS_RATE_EXPORT_BURST", "1000")

# BookNLP gira SOLO sul servizio GPU (ADR-008, aggiornamento 2026-09-18).
# Il default di config.py è un placeholder redatto: i test da host usano la
# STESSA chiave che il compose passa al container, altrimenti il Gateway
# upstream risponde 401 e l'estrazione entità fallirebbe in tutti i test
# che esercitano il percorso reale (test_f2_entity_extraction / ner_llm).
os.environ.setdefault("LLM_GATEWAY_API_KEY", "sk-local-dev-change-me")


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """Create ``trans_test`` if missing and apply Alembic once per session."""
    from sqlalchemy import create_engine, text

    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'trans_test'")
            ).scalar()
            if not exists:
                conn.execute(text(
                    "CREATE DATABASE trans_test TEMPLATE template0 "
                    "LC_COLLATE 'C' LC_CTYPE 'C'"
                ))
    finally:
        admin.dispose()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL},
    )
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth: test the real §2.1 auth enforcement (no bypass override)",
    )


@pytest.fixture(autouse=True)
def _auth_bypass(request):
    """Override the §2.1 auth dependencies for functional tests.

    The domain suites exercise business logic, not authentication: without
    this fixture every call would 401 now that the routers require a token.
    ``tests/test_api_authz_enforcement.py`` opts out via the ``real_auth``
    marker to test the real 401/403/429 behavior.

    Both levels are overridden: the router-level ``get_current_user`` AND
    every per-route ``require_permission(...)`` closure (each call to
    ``require_permission`` returns a distinct closure, so they must be
    collected from the registered routes). ``/auth/*`` endpoints call
    ``require_admin`` inside the function body, which dependency overrides
    never touch -- the AC tests for those keep working.
    """
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    from backend.main import app
    from backend.rbac import get_current_user
    from backend.rate_limit import limiter

    def _fake_user() -> dict:
        return {"sub": "test-user", "role": "admin", "type": "access"}

    def _iter_routes(router):
        """Walk the route tree: newer FastAPI nests included routers
        (``_IncludedRouter.original_router``) instead of flattening."""
        for entry in router.routes:
            original = getattr(entry, "original_router", None)
            if original is not None:
                yield from _iter_routes(original)
            elif hasattr(entry, "routes"):
                yield from _iter_routes(entry)
            else:
                yield entry

    app.dependency_overrides[get_current_user] = _fake_user
    for route in _iter_routes(app.router):
        for dep in getattr(route, "dependencies", []) or []:
            call = getattr(dep, "dependency", None) or getattr(dep, "call", None)
            if call is not None and call is not get_current_user:
                app.dependency_overrides[call] = _fake_user
    # the token buckets are in-process and shared across the session: reset
    # them per test so a 429 in one suite never bleeds into the next
    limiter.reset()
    yield
    app.dependency_overrides.clear()
    limiter.reset()


@pytest.fixture(scope="session", autouse=True)
def _restore_db_level_schema_after_session():
    """Re-apply the audit-log immutability trigger after the last test."""
    yield
    from sqlalchemy import inspect, text

    from backend.db import engine

    engine.dispose()
    insp = inspect(engine)
    if not insp.has_table("audit_log"):
        # a schema-destroying run never happened; nothing to restore
        return
    sql = """
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
    with engine.begin() as conn:
        conn.execute(text(sql))
    engine.dispose()
