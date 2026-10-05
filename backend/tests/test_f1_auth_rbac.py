"""Acceptance-criteria tests for F1: auth base + RBAC (PRD §2.1, §13.1).

Covers the three acceptance criteria of the corresponding kanban item:

1. login / logout / refresh work end to end with real tokens;
2. an admin-only endpoint returns 403 to every non-admin role (the five-role
   matrix of §2.1 is enforced by the ``require_admin`` dependency);
3. the append-only audit log records login and export events (§13.1).

Storage is LocalFileStorage (or local_only) so no MinIO is required. Each run
uses an isolated `trans_test` database that is dropped and recreated once per
session, so tests never see rows left behind by a previous run or by another
test suite sharing the Postgres instance. The DB is created ahead of time with
`createdb trans_test`; the schema is applied by Alembic (migration 001).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import subprocess

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine, text

# Isolated test database. Decided and exported BEFORE backend.main is imported
# below: backend.db builds its engine from DATABASE_URL at import time, so a
# setdefault inside a fixture would come too late (the app would silently talk
# to a different database than the one the fixtures seed).
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ALEMBIC_SQLALCHEMY_URI", TEST_DATABASE_URL)

# Make the backend package importable from the repo root.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, SessionLocal, engine  # noqa: E402
from backend.models import (  # noqa: E402
    AuditLog,
    Job,
    LLMRun,
    MemorySnapshot,
    Role,
    User,
)
from tests._f1_helpers import session_scope  # noqa: E402


def _drop_and_recreate_test_db() -> None:
    """Recreate ``trans_test`` over a plain TCP admin connection.

    Uses SQLAlchemy instead of ``psql`` so no peer-auth OS role is needed, and
    AUTOCOMMIT because CREATE/DROP DATABASE cannot run inside a transaction.
    """
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_test' AND pid <> pg_backend_pid()"
            ))
            conn.execute(text("DROP DATABASE IF EXISTS trans_test"))
            # TEMPLATE template0 + C collation: template1 carries a glibc
            # collation version that drifts on OS upgrade (WSL), which makes
            # plain CREATE DATABASE fail with InternalError_. template0 has no
            # collation-locked objects; C collation is deterministic and matches
            # what backup.restore_backup creates (same trick as the F4-QA
            # conftest). Alembic's env then ensures the pgvector extension.
            conn.execute(text(
                "CREATE DATABASE trans_test TEMPLATE template0 "
                "LC_COLLATE 'C' LC_CTYPE 'C'"
            ))
    finally:
        admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema is applied once per session (incl. migration 001).

    The database is dropped and recreated at the start of each test run so
    no rows from a previous run or another suite are visible. Alembic's env
    ensures the pgvector extension afterwards.
    """
    _drop_and_recreate_test_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL},
    )
    yield


@pytest.fixture(autouse=True)
def _reset():
    """Each test starts from an empty, well-defined DB.

    The ORM models register on the single shared metadata (``backend.db.Base``,
    re-exported by ``backend.models.base``). Sessions are always used via
    ``session_scope`` (see ``tests._f1_helpers``) so no "idle in transaction"
    connection lingers holding ACCESS SHARE locks that would block the
    DROP TABLE below.
    """
    # Drop pooled connections first: any open transaction on them (a session a
    # previous test forgot to close) would deadlock the DROP TABLE.
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _seed_user(db, email, role, password):
    """Create a user with a hashed password and return the plaintext."""
    from backend.security import hash_password
    from backend.models import User

    import uuid as _u
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(id=str(_u.uuid4()))
    user.email = email
    user.role = role
    user.is_active = True
    user.password_hash = hash_password(password)
    db.add(user)
    db.commit()
    return user


async def _login(client, email, password="S3cret!"):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["access_token"], body["refresh_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# AC1: login / logout / refresh
# --------------------------------------------------------------------------
async def test_login_refresh_logout_flow(client):
    from backend.models import User

    with SessionLocal() as db:
        _seed_user(db, "alice@libro.example", "admin", "S3cret!")

    access, refresh = await _login(client, "alice@libro.example")
    assert access and refresh

    # /auth/me resolves the identity from the access token.
    r = await client.get("/api/v1/auth/me", headers=_auth(access))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
    assert "manage_users" in r.json()["permissions"]

    # /auth/refresh exchanges the refresh token for a new access token.
    r = await client.post("/api/v1/auth/refresh", headers=_auth(refresh))
    assert r.status_code == 200, r.text
    new_access = r.json()["access_token"]
    assert new_access != access  # fresh signing each time

    # The refreshed token is itself valid for /auth/me.
    r = await client.get("/api/v1/auth/me", headers=_auth(new_access))
    assert r.status_code == 200

    # /auth/logout returns 204/205 and does not crash.
    r = await client.post("/api/v1/auth/logout", headers=_auth(access))
    assert r.status_code in (204, 205, 200)


async def test_login_wrong_password_is_401(client):
    from backend.models import User

    with SessionLocal() as db:
        _seed_user(db, "bob@libro.example", "translator", "right-password")

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "bob@libro.example", "password": "wrong"},
    )
    assert r.status_code == 401, r.text
    # Same (non-enumerating) error for an unknown email.
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@libro.example", "password": "whatever"},
    )
    assert r.status_code == 401


async def test_refresh_without_token_is_401(client):
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------
# AC2: admin-only endpoint -> 403 for every non-admin role (RBAC matrix)
# --------------------------------------------------------------------------
async def test_admin_only_endpoint_enforces_rbac(client):
    """Each of the five §2.1 roles logs in; only admin may hit /auth/admin/users."""
    from backend.rbac import ROLE_PERMISSIONS

    roles = ("admin", "project_manager", "translator", "revisor", "qa_reader")
    assert set(roles) == set(ROLE_PERMISSIONS), "role matrix changed"

    results = {}
    for role in roles:
        with SessionLocal() as db:
            _seed_user(db, f"{role}@libro.example", role, "S3cret!")
        access, _ = await _login(client, f"{role}@libro.example")
        r = await client.get("/api/v1/auth/admin/users", headers=_auth(access))
        results[role] = r.status_code

    # admin -> 200 (has the manage_users permission)
    assert results["admin"] == 200, results
    # every other role -> 403 (no manage_users)
    for role in roles:
        if role != "admin":
            assert results[role] == 403, f"{role} should be 403, got {results[role]}"


async def test_admin_role_holds_all_permissions(client):
    from backend.rbac import ALL_PERMISSIONS

    with SessionLocal() as db:
        _seed_user(db, "boss@libro.example", "admin", "S3cret!")

    access, _ = await _login(client, "boss@libro.example")
    r = await client.get("/api/v1/auth/me", headers=_auth(access))
    perms = set(r.json()["permissions"])
    assert perms <= set(ALL_PERMISSIONS)
    assert "manage_users" in perms and "all_audit" in perms


# --------------------------------------------------------------------------
# AC3: audit_log records login and export (append-only)
# --------------------------------------------------------------------------
async def test_login_is_recorded_in_audit_log(client):
    from backend.models import AuditLog, User

    with SessionLocal() as db:
        _seed_user(db, "carol@libro.example", "translator", "S3cret!")

    await _login(client, "carol@libro.example")

    with session_scope() as db:
        actions = [a.action for a in db.query(AuditLog).all()]
    assert "login" in actions, f"no login audit row; got {actions}"

    # The row is tied to the authenticated user.
    with session_scope() as db:
        log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "login", AuditLog.entity == "auth")
            .first()
        )
        user = db.get(User, log.user_id) if log else None
        email = user.email if user else None
    assert log is not None
    assert email == "carol@libro.example"


async def test_export_action_is_recorded_in_audit_log():
    """log_event (the audit mechanism behind export/approvals/deletions) writes
    an immutable row. There is no export endpoint in F1 (that is F4); this
    proves the append-only audit capability the AC requires is functional."""
    from backend.audit import log_event
    from backend.models import AuditLog

    log_event(
        action="export",
        entity="project",
        project_id="proj-1",
        user_id="user-1",
        after={"format": "docx"},
    )

    with session_scope() as db:
        rows = db.query(AuditLog).all()
        actions = [a.action for a in rows]
        assert "export" in actions, f"no export audit row; got {actions}"

        export_row = next(a for a in rows if a.action == "export")
        assert export_row.entity == "project"
        assert export_row.after["format"] == "docx"
        # Append-only: only ever inserts, never mutates an existing row.
        assert db.query(AuditLog).count() == 1


async def test_audit_log_is_append_only(client):
    """The audit table must never be mutated after insert (PRD principle 5)."""
    from backend.models import AuditLog

    with SessionLocal() as db:
        _seed_user(db, "dan@libro.example", "revisor", "S3cret!")
        await _login(client, "dan@libro.example")

    with session_scope() as db:
        n_before = db.query(AuditLog).count()
    assert n_before >= 1

    # No path in F1 mutates audit rows; a second login only adds.
    await _login(client, "dan@libro.example")
    with session_scope() as db:
        n_after = db.query(AuditLog).count()
    assert n_after == n_before + 1  # one new row per login, none deleted
