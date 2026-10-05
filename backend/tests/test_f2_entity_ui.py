"""Tests for the entity UI surface.

Acceptance criteria under test:

* AC1 — ``merge``/``split`` from the UI are reflected immediately in the
  list (§15.2: "un personaggio con alias viene mostrato come un'entità
  unica dopo merge approvato").
* AC2 — the detail endpoint returns every mention/evidence with quote and
  page (§15.2: "ogni entità mostra le sue evidenze").
* AC3 — ``export`` CSV is downloadable and re-importable without loss
  (§12.3 / AC3 round-trip).

The suite is pure (no LLM, no BookNLP): it seeds the DB directly and
drives the FastAPI app.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-entity-ui")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine, SessionLocal  # noqa: E402
from backend.models import (  # noqa: E402
    EntityAlias,
    Entity,
    EntityEvidence,
    TranslationUnit,
)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    os.environ.setdefault(
        "ALEMBIC_SQLALCHEMY_URI",
        os.getenv("DATABASE_URL",
                  "postgresql://trans:trans@127.0.0.1:5432/trans"),
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


async def _new_project(client: AsyncClient) -> str:
    r = await client.post("/api/v1/projects", json={
        "title": "UI Book", "genre_profile": "fantasy"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_entities(pid: str) -> dict[str, str]:
    """Insert two PERSON entities + one location; returns id->canonical map."""
    from datetime import datetime

    now = datetime.utcnow()
    ch1 = "11111111-1111-1111-1111-111111111111"
    ch2 = "22222222-2222-2222-2222-222222222222"
    rows: list[Entity] = []
    ids: dict[str, str] = {}

    maren = Entity(
        id="33333333-3333-3333-3333-333333333333",
        project_id=pid, canonical_source="Maren", canonical_target=None,
        entity_type="PERSON", referential_gender="female",
        italian_grammatical_gender="feminine", grammatical_number="singular",
        translation_policy="translate", status="proposed", version=1,
        never_translate=False, allow_inflection=True, created_at=now,
        updated_at=now)
    ids["maren"] = maren.id
    rows.append(maren)

    mara = Entity(
        id="44444444-4444-4444-4444-444444444444",
        project_id=pid, canonical_source="Mara", canonical_target=None,
        entity_type="PERSON", referential_gender="female",
        italian_grammatical_gender="feminine", grammatical_number="singular",
        translation_policy="translate", status="proposed", version=1,
        never_translate=False, allow_inflection=True, created_at=now,
        updated_at=now)
    ids["mara"] = mara.id
    rows.append(mara)

    albans = Entity(
        id="55555555-5555-5555-5555-555555555555",
        project_id=pid, canonical_source="Saint Albans", canonical_target=None,
        entity_type="LOCATION", referential_gender="not_applicable",
        italian_grammatical_gender="not_applicable",
        grammatical_number="singular", translation_policy="keep_source",
        status="proposed", version=1, never_translate=False,
        allow_inflection=True, created_at=now, updated_at=now)
    ids["albans"] = albans.id
    rows.append(albans)

    with SessionLocal() as s:
        for e in rows:
            s.add(e)
        s.commit()
        # evidence: Maren + Mara in ch1, Albans in ch2
        s.add(EntityEvidence(
            id="66666666-0001-0001-0001-000100010001",
            entity_id=maren.id, chapter_id=ch1, page_number=3,
            quote_text="Maren opened the door.", evidence_type="booknlp",
            extractor="booknlp"))
        s.add(EntityEvidence(
            id="66666666-0002-0002-0002-000200020002",
            entity_id=maren.id, chapter_id=ch1, page_number=7,
            quote_text="She sat beside Maren.", evidence_type="booknlp",
            extractor="booknlp"))
        s.add(EntityEvidence(
            id="66666666-0003-0003-0003-000300030003",
            entity_id=mara.id, chapter_id=ch1, page_number=4,
            quote_text="Mara smiled.", evidence_type="booknlp",
            extractor="booknlp"))
        s.add(EntityEvidence(
            id="66666666-0004-0004-0004-000400040004",
            entity_id=albans.id, chapter_id=ch2, page_number=12,
            quote_text="They reached Saint Albans.", evidence_type="booknlp",
            extractor="booknlp"))
        s.commit()
    ids["ch1"] = ch1
    ids["ch2"] = ch2
    return ids


# --------------------------------------------------------------------------
# AC2 — detail returns every evidence with quote + page.
# --------------------------------------------------------------------------
async def test_ac2_detail_shows_all_evidence(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities/{ids['maren']}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert len(detail["evidence"]) >= 1
    ev = detail["evidence"][0]
    assert ev["quote_text"] == "Maren opened the door."
    assert ev["page_number"] == 3
    assert ev["chapter_id"] == ids["ch1"]


# --------------------------------------------------------------------------
# AC1 — merge is reflected immediately in the list; split too.
# --------------------------------------------------------------------------
async def test_ac1_merge_reflected_in_list(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    # Maren and Mara are two entities; merge Mara into Maren.
    r = await client.post(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/merge",
        json={"target_id": ids["mara"]})
    assert r.status_code == 200, r.text
    merged = r.json()
    # the survivor now knows about the merged-away canonical source
    assert "Mara" in merged["aliases"]

    # the list shows Maren once and Mara as merged (single canonical row)
    r = await client.get(f"/api/v1/projects/{pid}/entities")
    entities = r.json()["entities"]
    live = [e for e in entities if e["status"] != "merged"]
    assert any(e["canonical_source"] == "Maren" for e in live)
    assert all(e["canonical_source"] != "Mara" for e in live)
    merged_row = next(
        (e for e in entities if e["id"] == ids["mara"]), None)
    assert merged_row is not None
    assert merged_row["status"] == "merged"


async def test_ac1_split_alias_creates_new_entity(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    # add an alias to Maren, then split it off as a new entity.
    r = await client.post(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/aliases",
        json={"source_alias": "Maren Rose",
              "alias_type": "synonym"})
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/split",
        json={"source_alias": "Maren Rose", "canonical_source": "Maren Rose",
              "canonical_target": "Maren Rosa",
              "entity_type": "PERSON"})
    assert r.status_code == 200, r.text
    split = r.json()
    assert split["canonical_source"] == "Maren Rose"
    assert split["status"] == "proposed"

    # the new entity exists and Maren no longer owns the alias
    r = await client.get(f"/api/v1/projects/{pid}/entities/{ids['maren']}")
    maren = r.json()
    assert "Maren Rose" not in maren["aliases"]

    r = await client.get(f"/api/v1/projects/{pid}/entities/{split['id']}")
    new = r.json()
    assert new["canonical_source"] == "Maren Rose"
    assert new["canonical_target"] == "Maren Rosa"


# --------------------------------------------------------------------------
# AC3 — CSV export is downloadable and re-importable without loss.
# --------------------------------------------------------------------------
async def test_ac3_csv_export_and_roundtrip(client):
    pid = await _new_project(client)
    _seed_entities(pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities/export",
                         params={"fmt": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    csv_out = r.text
    assert "Maren" in csv_out
    assert "Saint Albans" in csv_out
    assert csv_out.startswith(
        "canonical_source,canonical_target,entity_type,referential_gender")

    # re-import the exported CSV into a fresh project -> rows survive
    pid2 = await _new_project(client)
    r = await client.post(
        f"/api/v1/projects/{pid2}/entities/import",
        files={"file": ("entities.csv", csv_out, "text/csv"),
               "fmt": "csv"})
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["imported"] >= 3, report
    assert report["error_count"] == 0, report

    r = await client.get(f"/api/v1/projects/{pid2}/entities")
    entities = r.json()["entities"]
    sources = {e["canonical_source"] for e in entities}
    assert "Maren" in sources
    assert "Saint Albans" in sources


# --------------------------------------------------------------------------
# bulk approve (§6.2.5)
# --------------------------------------------------------------------------
async def test_bulk_approve(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.post(f"/api/v1/projects/{pid}/entities/approve", json={
        "ids": [ids["maren"], ids["albans"], "does-not-exist"]})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    maren = next(e for e in r.json()["entities"]
                 if e["id"] == ids["maren"])
    assert maren["status"] == "approved"


# --------------------------------------------------------------------------
# filters (§6.6: tabella filtrabile per stato/tipo/genere/capitolo)
# --------------------------------------------------------------------------
async def test_filters(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities",
                         params={"entity_type": "PERSON"})
    persons = r.json()["entities"]
    assert all(e["entity_type"] == "PERSON" for e in persons)

    r = await client.get(f"/api/v1/projects/{pid}/entities",
                         params={"referential_gender": "female"})
    females = r.json()["entities"]
    assert all(e["referential_gender"] == "female" for e in females)

    r = await client.get(f"/api/v1/projects/{pid}/entities",
                         params={"chapter_id": ids["ch2"]})
    ch2 = r.json()["entities"]
    assert len(ch2) == 1
    assert ch2[0]["canonical_source"] == "Saint Albans"


# --------------------------------------------------------------------------
# chapter intro / delta (§6.6)
# --------------------------------------------------------------------------
async def test_chapter_intro_and_delta(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/chapter/{ids['ch2']}")
    assert r.status_code == 200, r.text
    data = r.json()
    introduced = {e["canonical_source"] for e in data["introduced"]}
    # Saint Albans is the only entity introduced in ch2.
    assert introduced == {"Saint Albans"}
    # Maren and Mara were introduced in ch1 (the previous chapter) and are
    # not introduced in ch2, so they land in the "returned" (delta) set.
    returned = {e["canonical_source"] for e in data["returned"]}
    assert "Maren" in returned
    assert "Mara" in returned
    # ...and they are NOT newly introduced in ch2.
    new = {e["canonical_source"] for e in data["delta"]}
    assert "Maren" not in new


# --------------------------------------------------------------------------
# versions (§15.4): a PATCH records an immutable snapshot.
# --------------------------------------------------------------------------
async def test_versions_recorded(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.patch(f"/api/v1/projects/{pid}/entities/{ids['maren']}",
                           json={"status": "approved",
                                 "canonical_target": "Maren IT"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/versions")
    versions = r.json()["versions"]
    assert len(versions) >= 1
    assert versions[-1]["action"] == "patch"
    assert versions[-1]["snapshot"]["status"] == "approved"


# --------------------------------------------------------------------------
# pagination (§6.6 / AC2): the list is paginated with total/page/per_page.
# --------------------------------------------------------------------------
async def test_pagination(client):
    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.get(
        f"/api/v1/projects/{pid}/entities",
        params={"page": 1, "per_page": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["per_page"] == 2
    # only 2 entities on page 1
    assert len(body["entities"]) == 2

    r = await client.get(
        f"/api/v1/projects/{pid}/entities",
        params={"page": 2, "per_page": 2})
    body = r.json()
    assert body["page"] == 2
    assert len(body["entities"]) == 1
    # the two pages are disjoint and cover all three entities
    p1 = {e["canonical_source"] for e in body["entities"]}
    r = await client.get(
        f"/api/v1/projects/{pid}/entities",
        params={"page": 1, "per_page": 2})
    p2 = {e["canonical_source"] for e in r.json()["entities"]}
    all_sources = p1 | p2
    assert len(all_sources) == 3


# --------------------------------------------------------------------------
# evidence endpoint (§12.3 / §6.6): each mention with ±2 paragraphs.
# --------------------------------------------------------------------------
async def test_evidence_endpoint(client):
    from datetime import datetime

    pid = await _new_project(client)
    ids = _seed_entities(pid)

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_source"] == "Maren"
    assert body["mention_count"] == 2
    assert len(body["evidence"]) == 2
    # each evidence still carries the raw quote and page
    assert body["evidence"][0]["quote_text"] == "Maren opened the door."
    assert body["evidence"][0]["page_number"] == 3
    # the evidence is attached to no segment in this seed, so context is None
    assert body["evidence"][0]["context"] is None


async def test_evidence_endpoint_with_context(client):
    import uuid as _uuid

    pid = await _new_project(client)
    ids = _seed_entities(pid)

    # build a chapter with 5 segments and attach Maren's evidence to seg 3
    ch = ids["ch1"]
    seg_ids = [str(_uuid.uuid4()) for _ in range(5)]
    seg3 = seg_ids[2]
    with SessionLocal() as s:
        for i, sid in enumerate(seg_ids, start=1):
            s.add(
                TranslationUnit(
                    id=sid,
                    project_id=pid, chapter_id=ch, ordinal=i,
                    source_text="segment about the story",
                    status="untranslated", source_hash=f"h{i}"))
        # re-point Maren's first evidence to segment 3
        s.add(EntityEvidence(
            id="77777777-0001-0001-0001-000100010001",
            entity_id=ids["maren"], chapter_id=ch, page_number=1,
            source_segment_id=seg3,
            quote_text="Maren opened the door.", evidence_type="booknlp",
            extractor="booknlp"))
        s.commit()

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    ev = body["evidence"][0]
    assert ev["segment_id"] == seg3
    ctx = ev["context"]
    assert ctx is not None
    ordinals = [c["ordinal"] for c in ctx]
    # ±2 paragraphs around segment 3 -> segments 1..5 (all 5 here)
    assert ordinals == [1, 2, 3, 4, 5]
    assert ctx[2]["source_text"] == "segment about the story"


# --------------------------------------------------------------------------
# OCR-suspect badge on evidence (§6.6 / §11.2 "OCR sospetto")
# --------------------------------------------------------------------------
def _seed_document_with_page(pid: str, document_id: str, page_number: int,
                             ocr_suspect: bool) -> None:
    from datetime import datetime
    from backend.models import Document, DocumentPage

    now = datetime.utcnow()
    with SessionLocal() as s:
        s.add(Document(
            id=document_id, project_id=pid, filename="book.pdf",
            content_type="application/pdf", size_bytes=1024,
            sha256="a" * 64, page_count=12, storage_key=f"docs/{document_id}",
            status="parsed", created_at=now))
        s.add(DocumentPage(
            id=str(_uuid4()), document_id=document_id, page_number=page_number,
            extractor="native" if not ocr_suspect else "ocr",
            confidence=0.99, char_count=100, suspect_chars=0,
            text_sha256="b" * 64, page_sha256="c" * 64,
            normalized_text="…", page_payload={},
            ocr_suspect=ocr_suspect, ocr_level=None,
            ocr_mean_line_conf=None, created_at=now))
        s.commit()


def _uuid4() -> str:
    import uuid as _uuid
    return str(_uuid.uuid4())


async def test_evidence_ocr_suspect_from_page(client):
    """Page-level flag: an evidence on an OCR-suspect page is badged."""
    from datetime import datetime

    pid = await _new_project(client)
    ids = _seed_entities(pid)
    doc_id = str(_uuid4())
    # Maren's first evidence is on page 3; flag that page as OCR-suspect.
    _seed_document_with_page(pid, doc_id, page_number=3, ocr_suspect=True)

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/evidence")
    assert r.status_code == 200, r.text
    by_page = {e["page_number"]: e for e in r.json()["evidence"]}
    assert by_page[3]["ocr_suspect"] is True
    # the other Maren evidence (page 7, not flagged) is not badged
    assert by_page[7]["ocr_suspect"] is False


async def test_evidence_ocr_suspect_from_segment_flags(client):
    """Segment-level flag wins: a segment flagged by the OCR runner is
    badged even when no DocumentPage row marks the page suspect."""
    import uuid as _uuid

    pid = await _new_project(client)
    ids = _seed_entities(pid)
    ch = ids["ch1"]
    seg_id = str(_uuid.uuid4())
    with SessionLocal() as s:
        s.add(TranslationUnit(
            id=seg_id, project_id=pid, chapter_id=ch, ordinal=1,
            source_text="Maren opened the door.",
            status="untranslated", source_hash="h1",
            source_flags={"ocr_suspect": True, "ocr_page": 3}))
        # point Maren's first evidence (page 3) at the flagged segment
        s.add(EntityEvidence(
            id="88888888-0001-0001-0001-000100010001",
            entity_id=ids["maren"], chapter_id=ch, page_number=3,
            source_segment_id=seg_id,
            quote_text="Maren opened the door.", evidence_type="booknlp",
            extractor="booknlp"))
        s.commit()

    r = await client.get(
        f"/api/v1/projects/{pid}/entities/{ids['maren']}/evidence")
    assert r.status_code == 200, r.text
    flagged = [e for e in r.json()["evidence"]
               if e["id"] == "88888888-0001-0001-0001-000100010001"]
    assert flagged and flagged[0]["ocr_suspect"] is True
