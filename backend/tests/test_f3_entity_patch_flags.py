"""Regressione PATCH entità: i flag never_translate/allow_inflection devono
essere salvati (fix 2026-09-19: EntityPatch li scartava in silenzio)."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine, SessionLocal  # noqa: E402
from backend.models import Entity, Project  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
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


def _project_with_entity() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        p = Project(id=str(uuid.uuid4()), title="Patch flags",
                    genre_profile="saggio", source_language="en",
                    target_language="it", status="DRAFT",
                    copyright_confirmed=True, created_at=now, updated_at=now)
        db.add(p)
        db.flush()
        e = Entity(project_id=p.id, canonical_source="Warson",
                   canonical_target="Warson", entity_type="PERSON",
                   never_translate=False, allow_inflection=True)
        db.add(e)
        db.commit()
        return str(p.id), str(e.id)


async def test_patch_saves_boolean_flags(client):
    pid, eid = _project_with_entity()

    r = await client.patch(f"/api/v1/projects/{pid}/entities/{eid}",
                           json={"never_translate": True,
                                 "allow_inflection": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["never_translate"] is True
    assert body["allow_inflection"] is False

    # il valore letto dopo è quello salvato (non solo l'echo della risposta)
    from backend.models import Entity as E
    with SessionLocal() as db:
        e = db.get(E, eid)
        assert e.never_translate is True
        assert e.allow_inflection is False


async def test_patch_can_unset_flags_back(client):
    pid, eid = _project_with_entity()
    await client.patch(f"/api/v1/projects/{pid}/entities/{eid}",
                       json={"never_translate": True,
                             "allow_inflection": False})
    r = await client.patch(f"/api/v1/projects/{pid}/entities/{eid}",
                           json={"never_translate": False,
                                 "allow_inflection": True})
    assert r.status_code == 200, r.text
    assert r.json()["never_translate"] is False
    assert r.json()["allow_inflection"] is True
