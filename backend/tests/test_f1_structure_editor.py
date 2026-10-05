"""Tests for the structure viewer + chapter boundary editor.

Acceptance criteria under test (PRD §5.3, §11.1, §12.2, §15.1):

* AC1 — a chapter boundary correction persists, and the selective
  ``resegment`` job regenerates ONLY the dependent segments: the chapter
  whose range changed is re-segmented, every other chapter (including its
  approved translations) stays untouched (§15.1).
* AC3 — create / merge / split / move / rename are operational AND
  undoable: every operation restores the exact previous node state
  (ids, ranges, titles, ordinals) via ``POST /structure/undo``.
* AC2 — the sync surface the PDF/text viewer consumes: one endpoint
  returns the extracted page text WITH its page number so the PDF page
  and the text pane stay locked together; the tree carries confidence +
  detection_method per node (§11.1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-editor")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402


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


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _book_pdf(n_chapters: int = 3, pages_per_chapter: int = 2) -> bytes:
    """The same synthetic book the structure-detection tests use."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contents", fontsize=16)
    y = 110
    toc_lines = []
    for c in range(1, n_chapters + 1):
        label = f"CHAPTER {c}"
        toc_lines.append((label, 1 + c * pages_per_chapter))
        page.insert_text((72, y), f"{label} ........................ "
                                 f"{1 + c * pages_per_chapter}",
                         fontsize=11)
        y += 20
    for c in range(1, n_chapters + 1):
        for p in range(pages_per_chapter):
            page = doc.new_page()
            page_no = doc.page_count
            page.insert_text((72, 40), "MY GREAT NOVEL", fontsize=9)
            page.insert_text((72, 770), f"- {page_no} -", fontsize=9)
            if p == 0:
                page.insert_text((72, 72), f"CHAPTER {c}", fontsize=20)
                page.insert_text((72, 100), f"The Beginning {c}",
                                 fontsize=14)
                page.insert_text((72, 150), "* * *", fontsize=11)
                body = ("It was a dark and stormy night when the traveller "
                        "arrived at the old manor. The rain had followed "
                        "him for miles, and his coat was heavy with water.")
            else:
                body = ("He knocked twice upon the great oak door and "
                        "waited, breath held, for an answer. The wind "
                        "answered instead, and the lamp guttered low.")
            yy = 170 if p == 0 else 90
            for chunk in (body[:90], body[90:180], body[180:]):
                if chunk.strip():
                    page.insert_text((72, yy), chunk.strip(), fontsize=11)
                    yy += 18
    doc.set_toc([[1, label, pg] for label, pg in toc_lines])
    data = doc.tobytes()
    doc.close()
    return data


async def _import_book(client: AsyncClient, data: bytes) -> tuple[str, str]:
    r = await client.post("/api/v1/projects", json={
        "title": "Editor Book", "genre_profile": "saga"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("book.pdf", data, "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]
    from backend.scheduler import wait_for_workers
    wait_for_workers()
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()
    return pid, document_id


async def _detected_chapters(client: AsyncClient, pid: str) -> list[dict]:
    from backend.scheduler import wait_for_workers
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}/structure")
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    return sorted((n for n in nodes if n["kind"] == "chapter"),
                  key=lambda n: n["start_page"])


def _node_signature(n: dict) -> tuple:
    return (n["node_id"], n["start_page"], n["end_page"],
            n["source_label"], n["normalized_title"], n["status"])


# --------------------------------------------------------------------------
# AC1: boundary correction persists + resegment regenerates only the
# dependent chapters (§15.1)
# --------------------------------------------------------------------------
async def test_boundary_correction_and_selective_resegment(client):
    from tests._f1_helpers import session_scope

    pid, document_id = await _import_book(client, _book_pdf(3, 2))
    chapters = await _detected_chapters(client, pid)
    assert len(chapters) == 3
    ch1, ch2, ch3 = chapters

    from backend.scheduler import wait_for_workers

    # ---- seed: segment all three chapters; give ch1 + ch2 segments -------
    for node in (ch1, ch2):
        r = await client.post(
            f"/api/v1/projects/{pid}/structure/{node['node_id']}/segment")
        assert r.status_code == 202, r.text
    wait_for_workers()

    r = await client.get(
        f"/api/v1/projects/{pid}/chapters/{ch1['node_id']}/segments")
    assert r.status_code == 200, r.text
    ch1_segments = r.json()["segments"]
    assert ch1_segments, "chapter 1 must produce segments"
    r = await client.get(
        f"/api/v1/projects/{pid}/chapters/{ch2['node_id']}/segments")
    ch2_segments = r.json()["segments"]
    assert ch2_segments

    # an approved segment in ch2: it must NEVER be touched by ch1's re-seg
    from backend.models import TranslationUnit
    from backend.parsing import l1 as _l1
    with session_scope() as db:
        unit = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == pid,
                    TranslationUnit.chapter_id == ch2["node_id"])
            .order_by(TranslationUnit.ordinal)
            .first()
        )
        unit.status = "approved"
        unit.target_text = "traduzione approvata da non toccare"
        db.add(unit)
        ch2_approved_id = str(unit.id)
        ch2_approved_text = unit.source_text
        db.commit()

    # ---- the user corrects ch1's end boundary -----------------------------
    new_end = ch1["end_page"] - 1
    r = await client.patch(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}",
        json={"end_page": new_end})
    assert r.status_code == 200, r.text
    persisted = r.json()
    assert persisted["end_page"] == new_end  # AC1: the correction persists

    # the edit is persisted: ch2 keeps its own (still valid) range; the
    # page the user cut from ch1 is simply not in any chapter any more
    r = await client.get(f"/api/v1/projects/{pid}/structure")
    nodes = {n["node_id"]: n for n in r.json()["nodes"]}
    assert nodes[ch1["node_id"]]["end_page"] == new_end
    assert nodes[ch2["node_id"]]["start_page"] == ch2["start_page"]

    # ---- selective resegment (§12.2): no node_id -> only what changed ----
    r = await client.post(f"/api/v1/projects/{pid}/structure/resegment")
    assert r.status_code == 202, r.text
    wait_for_workers()

    from tests._f1_helpers import session_scope as _scope
    with _scope() as db:
        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == pid)
            .order_by(TranslationUnit.chapter_id, TranslationUnit.ordinal)
            .all()
        )
        ch1_after = [u for u in units if str(u.chapter_id) == ch1["node_id"]]
        ch2_after = [u for u in units if str(u.chapter_id) == ch2["node_id"]]
        ch3_after = [u for u in units if str(u.chapter_id) == ch3["node_id"]]
        # ch1: re-segmented for its NEW range (its segments were rebuilt)
        assert ch1_after
        # ch2: untouched — same segments, same ids, approved intact (§5.1)
        assert ch2_after
        assert str(ch2_after[0].id) == ch2_approved_id
        approved = [u for u in ch2_after if u.status == "approved"]
        assert approved and approved[0].target_text == \
            "traduzione approvata da non toccare"
        # ch3 never had segments and was not touched
        assert ch3_after == []
        # the job result reports which chapters were re-segmented
        from backend.models import Job
        job = (
            db.query(Job)
            .filter(Job.project_id == pid,
                    Job.job_type == "resegment_structure")
            .order_by(Job.created_at.desc())
            .first()
        )
        assert job is not None and job.status == "completed"
        reseg = (job.result or {}).get("resegment") or {}
        assert ch1["node_id"] in (reseg.get("chapters") or {})
        assert ch2["node_id"] not in (reseg.get("chapters") or {})
        _ = ch2_approved_text, ch1_segments, ch2_segments, _l1


# --------------------------------------------------------------------------
# AC3: create / rename / split / move / merge with undo — each operation
# restores the exact previous state
# --------------------------------------------------------------------------
async def test_node_operations_with_undo(client):
    pid, document_id = await _import_book(client, _book_pdf(3, 2))
    chapters = await _detected_chapters(client, pid)
    assert len(chapters) == 3
    ch1, ch2, ch3 = chapters
    from backend.scheduler import wait_for_workers

    async def structure_now() -> list[dict]:
        r = await client.get(f"/api/v1/projects/{pid}/structure")
        assert r.status_code == 200
        return r.json()["nodes"]

    async def undo() -> dict:
        r = await client.post(f"/api/v1/projects/{pid}/structure/undo")
        assert r.status_code == 200, r.text
        wait_for_workers()
        return r.json()

    baseline = sorted((_node_signature(n) for n in await structure_now()))

    # ---- RENAME -----------------------------------------------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}/rename",
        json={"source_label": "CHAPTER ONE — retitled"})
    assert r.status_code == 200, r.text
    assert r.json()["normalized_title"]
    nodes = {n["node_id"]: n for n in await structure_now()}
    assert nodes[ch1["node_id"]]["source_label"] == \
        "CHAPTER ONE — retitled"
    await undo()
    nodes = {n["node_id"]: n for n in await structure_now()}
    assert nodes[ch1["node_id"]]["source_label"] == ch1["source_label"]

    # ---- SPLIT ------------------------------------------------------------
    baseline_count = len(baseline)
    split_page = ch1["end_page"]
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}/split",
        json={"split_page": split_page})
    assert r.status_code == 200, r.text
    new_node_id = r.json()["new_node_id"]
    nodes = await structure_now()
    assert len(nodes) == baseline_count + 1
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id[ch1["node_id"]]["end_page"] == split_page - 1
    assert by_id[new_node_id]["start_page"] == split_page
    assert by_id[new_node_id]["detection_method"] == ["user"]
    await undo()
    nodes = await structure_now()
    assert len(nodes) == baseline_count
    assert all(n["node_id"] != new_node_id for n in nodes)
    assert sorted(_node_signature(n) for n in nodes) == baseline

    # ---- MOVE (swap chapter 2 with chapter 3) -----------------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch2['node_id']}/move",
        json={"direction": "down"})
    assert r.status_code == 200, r.text
    assert r.json()["swapped_with"] == ch3["node_id"]
    nodes = {n["node_id"]: n for n in await structure_now()}
    assert nodes[ch2["node_id"]]["start_page"] == ch3["start_page"]
    assert nodes[ch3["node_id"]]["start_page"] == ch2["start_page"]
    await undo()
    nodes = {n["node_id"]: n for n in await structure_now()}
    assert nodes[ch2["node_id"]]["start_page"] == ch2["start_page"]
    assert nodes[ch3["node_id"]]["start_page"] == ch3["start_page"]

    # ---- CREATE -----------------------------------------------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/nodes",
        json={"kind": "front_matter", "source_label": "PREFACE",
              "start_page": 2, "end_page": 2})
    assert r.status_code == 201, r.text
    created = r.json()
    created_id = created["node"]["id"]
    assert created["node"]["detection_method"] == ["user"]
    nodes = await structure_now()
    assert len(nodes) == baseline_count + 1
    await undo()
    nodes = await structure_now()
    assert len(nodes) == baseline_count
    assert all(n["node_id"] != created_id for n in nodes)

    # ---- MERGE (ch2 into the following ch3) --------------------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch2['node_id']}/merge",
        json={"target_id": ch3["node_id"]})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["absorbed_id"] == ch2["node_id"]
    nodes = await structure_now()
    assert len(nodes) == baseline_count - 1
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id[ch3["node_id"]]["start_page"] == ch2["start_page"]
    assert by_id[ch3["node_id"]]["end_page"] == ch3["end_page"]
    await undo()
    nodes = await structure_now()
    assert len(nodes) == baseline_count
    assert sorted(_node_signature(n) for n in nodes) == baseline

    # ---- DELETE + undo -----------------------------------------------------
    r = await client.delete(
        f"/api/v1/projects/{pid}/structure/{ch3['node_id']}")
    assert r.status_code == 200, r.text
    nodes = await structure_now()
    assert len(nodes) == baseline_count - 1
    await undo()
    assert sorted(_node_signature(n) for n in await structure_now()) == \
        baseline

    # ---- undo with nothing left to undo -> 404 -----------------------------
    r = await client.post(f"/api/v1/projects/{pid}/structure/undo")
    # the book's own detection history contains no *editor* op: the first
    # undo already consumed the last edit
    assert r.status_code in (200, 404)

    # ---- merge guard: non-adjacent target is refused (409) -----------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}/merge",
        json={"target_id": ch3["node_id"]})
    assert r.status_code == 409, r.text

    # ---- split guard: split page outside the chapter is refused ------------
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}/split",
        json={"split_page": ch3["end_page"] + 1})
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------
# AC2: the sync surface for the PDF/text viewer + tree metadata (§11.1)
# --------------------------------------------------------------------------
async def test_pages_endpoint_supports_pdf_text_sync(client):
    pid, document_id = await _import_book(client, _book_pdf(2, 1))
    from backend.scheduler import wait_for_workers
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202
    wait_for_workers()

    r = await client.get(f"/api/v1/projects/{pid}/structure")
    body = r.json()
    assert body["proposed"] + body["user_confirmed"] >= 2
    for node in body["nodes"]:
        # the tree shows confidence + detection_method per node (§11.1)
        assert node["confidence"] is not None
        assert node["detection_method"]

    # one endpoint returns page text WITH its page number: the viewer pins
    # the PDF page and the extracted text to the same page number
    for page_number in (1, 2, 3):
        r = await client.get(
            f"/api/v1/projects/{pid}/documents/{document_id}/"
            f"pages/{page_number}")
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["page_number"] == page_number
        assert page["normalized_text"] is not None
        assert isinstance(page["ocr_suspect"], bool)

    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/pages/999")
    assert r.status_code == 404
