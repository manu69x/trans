"""Tests for F3 · bilingual editor (PRD §11.2, §11.3, §10.5.

AC:
* AC1 -- approve via the editor endpoint updates status and creates a TM entry
  (mirrors structure_routes.approve_segment; the editor's Ctrl+Enter).
* AC2 -- the refine endpoint shows a diff before applying and every version is
  conserved on translation_unit_versions; version history is queryable.
* AC3 -- the "solo non approvati"/lock and the "solo QA critici" filter work.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f3-editor")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine, SessionLocal  # noqa: E402
from backend.models import (  # noqa: E402
    TranslationUnit,
    TranslationUnitVersion,
)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
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


async def _new_project(client: AsyncClient, genre: str = "fantascientifica") -> str:
    r = await client.post("/api/v1/projects", json={
        "title": "Editor Book", "genre_profile": genre})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _unit(project_id: str, chapter_id: str, ordinal: int,
          source: str, target: str, status: str,
          flags: dict | None = None) -> TranslationUnit:
    return TranslationUnit(
        id=str(uuid.uuid4()),
        project_id=project_id,
        chapter_id=chapter_id,
        ordinal=ordinal,
        source_text=source,
        target_text=target,
        status=status,
        source_hash=source,
        source_flags=flags or {},
    )


# ---------------------------------------------------------------------------
# AC1 -- approve via the editor endpoint updates status and creates a TM entry
# ---------------------------------------------------------------------------
async def test_ac1_approve_via_editor_endpoint(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "Good morning, John.",
                    "Buongiorno, John.", "machine_draft"))
        s.commit()
        seg_id = s.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).one().id

    r = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/approve",
        json={"reviewer": "revisore-1", "qa_score": 4.5})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["created"] is True
    assert body["tm_entry_id"]
    with SessionLocal() as s:
        seg = s.query(TranslationUnit).filter(
            TranslationUnit.id == seg_id).one()
        assert seg.status == "approved"


# ---------------------------------------------------------------------------
# AC2 -- refine shows a diff before applying; version history is queryable
# ---------------------------------------------------------------------------
async def test_ac2_refine_records_diff_and_versions(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "The cat slept.",
                    "Il cat dormì", "machine_draft"))
        s.commit()
        seg_id = s.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).one().id

    r = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/refine",
        json={"target_text": "Il gatto dormì", "reason": "correggi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_text"] == "Il gatto dormì"
    # the diff is a token stream with added/removed/unchanged ops
    ops = body["diff"]["ops"]
    assert any(o["op"] == "added" for o in ops)
    assert any(o["op"] == "removed" for o in ops)
    # the segment is still a draft (refine does not approve)
    with SessionLocal() as s:
        seg = s.query(TranslationUnit).filter(
            TranslationUnit.id == seg_id).one()
        assert seg.target_text == "Il gatto dormì"

    # version history: the pre-change target is frozen (before=True)
    r = await client.get(
        f"/api/v1/projects/{pid}/segments/{seg_id}/versions")
    assert r.status_code == 200, r.text
    hist = r.json()["history"]
    assert len(hist) >= 1
    assert hist[-1]["before"] is False  # the new state
    assert hist[-1]["target_text"] == "Il gatto dormì"
    assert hist[0]["before"] is True
    assert hist[0]["target_text"] == "Il cat dormì"

    # a second refine freezes the previous state
    r = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/refine",
        json={"target_text": "Il gatto ha dormito"})
    assert r.status_code == 200, r.text
    r = await client.get(
        f"/api/v1/projects/{pid}/segments/{seg_id}/versions")
    hist = r.json()["history"]
    # one extra "before"-True row now
    assert sum(1 for h in hist if h["before"]) == 2


# ---------------------------------------------------------------------------
# AC2b -- an approved segment cannot be refined (immutability)
# ---------------------------------------------------------------------------
async def test_ac2b_approved_cannot_be_refined(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "Hello, John.",
                    "Ciao, John.", "approved"))
        s.commit()
        seg_id = s.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).one().id

    r = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/refine",
        json={"target_text": "Salve, John."})
    assert r.status_code == 409, r.text
    r = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/reject", json={})
    assert r.status_code == 409, r.text  # an approved segment cannot be rejected


# ---------------------------------------------------------------------------
# AC3 -- "solo non approvati"/lock and "solo QA critici" filters
# ---------------------------------------------------------------------------
async def test_ac3_filters_and_lock(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "One.", "Uno.", "machine_draft"))
        s.add(_unit(pid, chapter, 2, "Two.", "Due.", "approved"))
        s.add(_unit(pid, chapter, 3, "Three.", "Tre.", "untranslated"))
        s.commit()

    # "solo non approvati" -- the approved segment is excluded
    r = await client.get(
        f"/api/v1/projects/{pid}/segments?only_untranslated=1")
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert all(s["status"] != "approved" for s in segs)
    assert r.json()["approved"] == 0
    assert r.json()["untranslated"] == 1
    assert r.json()["machine_draft"] == 1

    # seed a critical QA issue on the machine_draft segment
    with SessionLocal() as s:
        seg1 = s.query(TranslationUnit).filter(
            TranslationUnit.ordinal == 1).one().id
    from backend.models import QaIssue
    s.add(QaIssue(
        id=str(uuid.uuid4()), project_id=pid, unit_id=seg1,
        severity="critical", kind="ocr", message="OCR sospetto",
        resolved=False))
    s.commit()

    r = await client.get(
        f"/api/v1/projects/{pid}/segments?only_critical_qa=1")
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["ordinal"] == 1
    assert segs[0]["qa_critical"] == 1

    # the QA issues endpoint lists the critical issue
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues")
    assert r.status_code == 200, r.text
    assert r.json()["critical_unresolved"] == 1
    assert r.json()["issues"][0]["kind"] == "ocr"


# ---------------------------------------------------------------------------
# find/replace with scope
# ---------------------------------------------------------------------------
async def test_find_replace_preview_and_apply(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "John went home.",
                    "John è tornato a casa.", "machine_draft"))
        s.add(_unit(pid, chapter, 2, "John was tired.",
                    "John era stanco.", "untranslated"))
        s.add(_unit(pid, chapter, 3, "John stayed.",
                    "John restò.", "approved"))  # must NOT be rewritten
        s.commit()

    # preview only: no writes; occurrences in approved segments are listed
    # as read-only (§5.1: approved text is never rewritten)
    r = await client.post(f"/api/v1/projects/{pid}/search", json={
        "query": "John", "replace_with": "Giovanni", "scope": "project",
        "apply": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["matches"] == 3          # 2 replaceable + 1 read-only
    assert len(body["replacements"]) == 2
    assert body["readonly_matches"] == 1
    assert body["readonly_replacements"][0]["old"] == \
        body["readonly_replacements"][0]["new"]
    assert all(b["old"] != b["new"] for b in body["replacements"])

    # apply: both drafts are rewritten, the approved one is untouched
    r = await client.post(f"/api/v1/projects/{pid}/search", json={
        "query": "John", "replace_with": "Giovanni", "scope": "project",
        "apply": True})
    assert r.status_code == 200, r.text
    assert r.json()["matches"] == 3
    assert r.json()["readonly_matches"] == 1
    with SessionLocal() as s:
        units = (s.query(TranslationUnit)
                 .filter(TranslationUnit.project_id == pid).all())
        statuses = {u.ordinal: u.status for u in units}
        for u in units:
            if u.status == "approved":
                assert u.target_text == "John restò."  # untouched
            else:
                assert "Giovanni" in (u.target_text or "")


# ---------------------------------------------------------------------------
# reject flow
# ---------------------------------------------------------------------------
async def test_reject_flow(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "Hello.", "Ciao.", "machine_draft"))
        s.commit()
        seg_id = s.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).one().id

    r = await client.post(f"/api/v1/projects/{pid}/segments/{seg_id}/reject",
                          json={"reason": "sbagliato"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "machine_draft"  # draft keeps its target
    with SessionLocal() as s:
        seg = s.query(TranslationUnit).filter(
            TranslationUnit.id == seg_id).one()
        assert seg.status == "machine_draft"
