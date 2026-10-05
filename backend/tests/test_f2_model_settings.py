"""httpx-based tests for F2: per-project model settings (PRD §8.2).

Covers acceptance criterion 3 of the corresponding kanban item:

1. ``PATCH /api/v1/projects/{id}` accepts and persists ``model_settings``
   (temperature / top_p / seed / reasoning / timeout / retry / max_output /
   prompt template) and the value round-trips through ``GET``.
2. the two model selectors (``translation_model_id`` / ``text_model_id``)
   persist as well.

The DB is the local Postgres given by DATABASE_URL / ALEMBIC_SQLALCHEMY_URI.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.db import Base, engine
from backend.main import app
from backend.models import Project


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema (incl. migration 009) is applied once per session."""
    os.environ.setdefault(
        "ALEMBIC_SQLALCHEMY_URI",
        os.getenv("DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans"),
    )
    import subprocess

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(BACKEND_DIR),
    )
    yield


@pytest.fixture(autouse=True)
def _reset():
    """Each test starts from an empty, well-defined DB."""
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    wait_for_workers()
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _new(client, title="Settings"):
    r = await client.post(
        "/api/v1/projects",
        json={"title": title, "genre_profile": "saga"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_model_settings_persist_and_roundtrip(client):
    pid = await _new(client)
    settings = {
        "translation": {
            "temperature": 0.2,
            "top_p": 0.95,
            "seed": 42,
            "reasoning": "off",
            "reasoning_budget": 1024,
            "timeout": 120,
            "retry": 3,
            "max_output": 2048,
            "prompt_template": "v2",
        },
        "text": {
            "temperature": 0.1,
            "top_p": 0.9,
            "reasoning": "on",
        },
    }
    r = await client.patch(f"/api/v1/projects/{pid}", json={
        "model_settings": settings,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_settings"] == settings

    # round-trips through GET
    got = await client.get(f"/api/v1/projects/{pid}")
    assert got.status_code == 200
    assert got.json()["model_settings"] == settings

    # persisted in the ORM
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    with _session() as db:
        p = db.get(Project, pid)
        assert p is not None
        assert p.model_settings == settings


@contextmanager
def _session():
    from backend.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


async def test_model_selectors_persist(client):
    pid = await _new(client)
    r = await client.patch(f"/api/v1/projects/{pid}", json={
        "translation_model_id": "llama-3.3-70b-instruct",
        "text_model_id": "qwen3-235b-instruct",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["translation_model_id"] == "llama-3.3-70b-instruct"
    assert body["text_model_id"] == "qwen3-235b-instruct"

    got = await client.get(f"/api/v1/projects/{pid}")
    assert got.json()["translation_model_id"] == "llama-3.3-70b-instruct"
    assert got.json()["text_model_id"] == "qwen3-235b-instruct"


async def test_default_model_settings_apply(client):
    """§8.1/§8.2: un progetto creato senza impostazioni esplicite eredita i
    default di deployment (env TRANS_DEFAULT_* / config.DEFAULT_MODEL_SETTINGS).
    Fix 2026-09-18: il test aspettava ``None``, il contratto precedente
    all'introduzione dei default di deployment nel service."""
    from backend.config import DEFAULT_MODEL_SETTINGS

    pid = await _new(client)
    got = await client.get(f"/api/v1/projects/{pid}")
    assert got.status_code == 200
    assert got.json()["model_settings"] == DEFAULT_MODEL_SETTINGS
