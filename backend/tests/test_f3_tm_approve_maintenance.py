"""Tests for F3 · TM: approve hook (AC1), retrieval (AC2), maintenance (AC3).

PRD §7.2 / §7.3 / §15.4 · 

* AC1 -- approving a segment creates a TM entry **with a source embedding**
  (end-to-end through the approve endpoint).
* AC2 -- the **semantic** retrieval (pgvector) finds a relevant match for a
  *rephrased* source (not an exact string match).
* AC3 -- the **maintenance** report flags contradictions (and, in the same
  dataset) duplicates / misaligned tags / length discrepancies / obsolete
  entries)) in the test dataset.
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

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f3-tm")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine, SessionLocal  # noqa: E402
from backend.models import TranslationMemoryEntry, TranslationUnit  # noqa: E402


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
        "title": "TM Book", "genre_profile": genre})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _unit(project_id: str, chapter_id: str, ordinal: int,
          source: str, target: str, status: str) -> TranslationUnit:
    return TranslationUnit(
        id=str(uuid.uuid4()),
        project_id=project_id,
        chapter_id=chapter_id,
        ordinal=ordinal,
        source_text=source,
        target_text=target,
        status=status,
        source_hash=source,
        source_flags={},
    )


# ---------------------------------------------------------------------------
# AC1 -- approve a segment creates a TM entry with an embedding
# ---------------------------------------------------------------------------
async def test_ac1_approve_creates_tm_entry_with_embedding(client):
    pid = await _new_project(client)
    chapter = str(uuid.uuid4())
    # seed a segment (untranslated) then approve it via the endpoint.
    with SessionLocal() as s:
        s.add(_unit(pid, chapter, 1, "Good morning, John.",
                    "Buongiorno, John.", "untranslated"))
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
    tm_id = body["tm_entry_id"]

    # the TM entry exists, is project-scoped and CARRIES an embedding.
    with SessionLocal() as s:
        entry = s.get(TranslationMemoryEntry, tm_id)
        assert entry is not None
        assert str(entry.project_id) == pid
        assert entry.segment_id is not None
        assert str(entry.segment_id) == str(seg_id)
        assert entry.source_embedding is not None  # <-- the AC1 requirement
        assert entry.target_approved == "Buongiorno, John."
        assert entry.reviewer == "revisore-1"
        assert float(entry.qa_score) == 4.5
        # the segment is now approved (immutable afterwards)
        seg = s.get(TranslationUnit, seg_id)
        assert seg.status == "approved"

    # approving again is idempotent: no second TM entry.
    r2 = await client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/approve",
        json={})
    assert r2.status_code == 201, r2.text
    assert r2.json()["created"] is False
    with SessionLocal() as s:
        assert s.query(TranslationMemoryEntry).count() == 1


# ---------------------------------------------------------------------------
# AC2 -- semantic retrieval finds a relevant match for a rephrased source
# ---------------------------------------------------------------------------
async def test_ac2_semantic_retrieval_rephrased(client):
    from backend.translation import embedding

    pid = await _new_project(client)
    # store an approved segment; later a *rephrased* source must still match.
    await client.post(f"/api/v1/projects/{pid}/tm", json={
        "source_normalized": "The cat slept on the warm sofa all day.",
        "source_original": "The cat slept on the warm sofa all day.",
        "target_approved": "Il dormito tutto il giorno sul caldo divano."})

    r = await client.post(f"/api/v1/projects/{pid}/tm/snapshot")
    assert r.status_code == 201, r.text
    snap = r.json()["snapshot_id"]

    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "block_source": "The cat was napping on the warm sofa all day long.",
        "segments": [{"segment_id": "s1",
                      "source_text": "The cat was napping on the warm sofa all day long."}],
        "semantic_threshold": 0.3,  # low so the paraphrase still qualifies
        "snapshot_ids": {"tm": snap}})
    assert r.status_code == 200, r.text
    plan = r.json()
    hits = plan["tm_matches"]
    assert len(hits) == 1, hits
    # matched by the SEMANTIC stage (not exact string match).
    assert hits[0]["method"] == "semantic"
    assert hits[0]["target_approved"] == "Il dormito tutto il giorno sul caldo divano."


# ---------------------------------------------------------------------------
# AC3 -- maintenance report flags contradictions (and more) in the dataset
# ---------------------------------------------------------------------------
async def test_ac3_maintenance_flags_contradictions(client):
    from backend.tm_maintenance import report_tm
    from backend.translation import embedding

    pid = await _new_project(client)
    chapter = str(uuid.uuid4())

    def _tm(source: str, target: str, seg_id=None):
        return TranslationMemoryEntry(
            id=str(uuid.uuid4()),
            project_id=pid,
            chapter_id=chapter,
            segment_id=seg_id,
            source_normalized=source,
            source_original=source,
            target_approved=target,
            terms_used=[],
            source_embedding=embedding.embed(source),
        )

    with SessionLocal() as s:
        # contradiction: same source, two different targets.
        seg_a = _unit(pid, chapter, 1, "Hello there, John.",
                      "Ciao, John.", "approved").id
        s.add(_tm("Hello there, John.", "Ciao, John.", seg_a))
        s.add(_tm("Hello there, John.", "Salve, John.", None))
        # duplicate: the same normalised source stored twice.
        s.add(_tm("Goodbye, world.", "Addio, mondo.", None))
        s.add(_tm("Goodbye, world.", "Arrivederci, mondo.", None))
        # misaligned tag: source has a tag the target is missing.
        s.add(_tm("See [[PERSON:John]] tomorrow.",
                  "Domani ti vedo.", None))
        # length discrepancy: absurdly long target.
        s.add(_tm("Hi.", "a" * 5000, None))
        # obsolete: entry whose segment no longer exists.
        s.add(_tm("Obsolete phrase.", "Frase obsoleta.",
                  str(uuid.uuid4())))
        s.commit()

    with SessionLocal() as s:
        rep = report_tm(s, pid)

    # AC3 core: the contradiction is flagged.
    assert len(rep["contradictions"]) >= 1
    c0 = rep["contradictions"][0]
    assert c0["entry_id_a"] != c0["entry_id_b"]
    assert c0["target_a"] != c0["target_b"]
    # every requested check is present in the report.
    assert rep["duplicates"]
    assert rep["misaligned_tags"]
    assert rep["length_discrepancies"]
    assert rep["obsolete"]
    # reuse rate is a fraction in [0, 1].
    assert 0.0 <= rep["reuse_rate"] <= 1.0
    # the report is also reachable via the dashboard endpoint.
    r = await client.get(f"/api/v1/projects/{pid}/tm/maintenance")
    assert r.status_code == 200, r.text
    assert r.json()["entry_count"] == 7
    assert len(r.json()["contradictions"]) >= 1
