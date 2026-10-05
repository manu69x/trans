"""Enforcement reale di auth/RBAC/rate-limit sull'API (PRD §2.1, §13/§14).

A differenza delle suite funzionali (che via conftest aggirano le dipendenze
di auth per collaudare la logica di business), questo modulo testa il
comportamento HTTP reale:

* 401 per chi non presenta un access token;
* 403 per un ruolo senza il permesso richiesto dalla rotta;
* 200 con il ruolo giusto;
* 429 sul bucket ``translate`` quando il budget LLM è esaurito (§13/§14).

Il marker ``real_auth`` disattiva il bypass di conftest.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.real_auth

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.main import app  # noqa: E402
from backend.db import SessionLocal, engine  # noqa: E402
from backend.models import User  # noqa: E402
from backend.rbac import get_current_user  # noqa: E402


@pytest.fixture(autouse=True)
def _no_overrides():
    """Nessun override: queste prove devono vedere l'enforcement vero."""
    from backend.rate_limit import limiter

    app.dependency_overrides.clear()
    limiter.reset()
    yield
    app.dependency_overrides.clear()
    limiter.reset()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _seed_user(email: str, role: str, password: str = "S3cret!") -> None:
    import uuid as _u

    from backend.security import hash_password

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(id=str(_u.uuid4()))
        user.email = email
        user.role = role
        user.is_active = True
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()


async def _login(client: AsyncClient, email: str,
                 password: str = "S3cret!") -> str:
    r = await client.post("/api/v1/auth/login",
                          json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_protected_endpoint_rejects_anonymous(client):
    r = await client.get("/api/v1/projects")
    assert r.status_code == 401
    body = r.json()
    assert "Authorization" in body["detail"] or "token" in body["detail"]


async def test_refresh_token_is_not_an_access_token(client):
    _seed_user("enforcement-admin@libro.example", "admin")
    r = await client.post("/api/v1/auth/login",
                          json={"email": "enforcement-admin@libro.example",
                                "password": "S3cret!"})
    refresh = r.json()["refresh_token"]
    # il refresh token non deve valere come access token sulle rotte protette
    r = await client.get("/api/v1/projects", headers=_auth(refresh))
    assert r.status_code == 401


async def test_qa_reader_cannot_create_project(client):
    _seed_user("enforcement-qa@libro.example", "qa_reader")
    token = await _login(client, "enforcement-qa@libro.example")
    r = await client.post(
        "/api/v1/projects",
        json={"title": "Nope", "genre_profile": "thriller"},
        headers=_auth(token),
    )
    assert r.status_code == 403
    assert "permissions" in r.json()["detail"]


async def test_project_manager_can_create_project(client):
    _seed_user("enforcement-pm@libro.example", "project_manager")
    token = await _login(client, "enforcement-pm@libro.example")
    r = await client.post(
        "/api/v1/projects",
        json={"title": "RBAC ok", "genre_profile": "thriller"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # ... ma il PM non può cancellare (permesso ``delete`` solo admin)
    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth(token))
    assert r.status_code == 403


async def test_translator_cannot_manage_users(client):
    _seed_user("enforcement-tr@libro.example", "translator")
    token = await _login(client, "enforcement-tr@libro.example")
    r = await client.get("/api/v1/auth/admin/users", headers=_auth(token))
    assert r.status_code == 403


async def test_translate_run_rate_limited(client, monkeypatch):
    """Il bucket §13 ``translate`` restituisce 429 quando è esaurito."""
    from backend.rate_limit import limiter

    _seed_user("enforcement-admin2@libro.example", "admin")
    token = await _login(client, "enforcement-admin2@libro.example")

    # budget minuscolo e in-process (gli altri bucket restano invariati)
    monkeypatch.setattr(limiter, "limits",
                        {**limiter.limits, "translate": (1.0, 0.0001)})
    limiter.reset()

    body = {"segments": [{"segment_id": "s1", "source_text": "Hello."}],
            "block_source": "Hello."}
    r1 = await client.post("/api/v1/projects/00000000-0000-0000-0000-000000000000/translation/run",
                           json=body, headers=_auth(token))
    assert r1.status_code in (202, 404), r1.text  # 404: progetto inesistente, ma il bucket è già stato consumato
    r2 = await client.post("/api/v1/projects/00000000-0000-0000-0000-000000000000/translation/run",
                           json=body, headers=_auth(token))
    assert r2.status_code == 429, r2.text
    assert "Retry-After" in r2.headers
