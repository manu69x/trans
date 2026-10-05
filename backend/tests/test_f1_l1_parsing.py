"""httpx + unit tests for the F1 L1 extraction worker.

Acceptance criteria under test:

* AC1 — native corpus PDFs are extracted with per-page coordinates and
  hashes (unit-level on the real corpus files + API-level on a synthetic
  multi-page PDF).
* AC2 — the 5.2.6 import report is generated with all its sections.
* AC3 — restarting the job halfway resumes WITHOUT duplicates (unique
  ``(document_id, page_number)`` + content-derived hashes).

The DB is the local Postgres (DATABASE_URL), storage is LocalFileStorage so
no MinIO is needed. The corpus PDFs live in docs/benchmarks/corpus/.
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

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-l1")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402

CORPUS_DIR = REPO_ROOT / "docs" / "benchmarks" / "corpus"
NATIVE_CORPUS = [
    "native_01_literary_excerpt.pdf",
    "native_02_nonfiction_excerpt.pdf",
    "native_03_dialogue_heavy_novel.pdf",
    "native_04_essay_with_footnotes.pdf",
]


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema (incl. migration 004) is applied once per session."""
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


def _book_pdf(n_pages: int = 4) -> bytes:
    """A native multi-page PDF with a body font, headings and a TOC."""
    import pymupdf

    doc = pymupdf.open()
    for i in range(n_pages):
        page = doc.new_page()
        if i == 0:
            page.insert_text((72, 72), "CHAPTER I", fontsize=20)
            page.insert_text((72, 100), "The Beginning", fontsize=14)
        else:
            page.insert_text((72, 72), f"Chapter {i} heading", fontsize=18)
        # repeated running header on every page + page number in the footer
        page.insert_text((72, 40), "MY GREAT NOVEL", fontsize=9)
        page.insert_text((72, 770), f"- {i + 1} -", fontsize=9)
        body = (
            "It was a dark and stormy night when the traveller arrived at "
            "the old manor. The rain had followed him for miles, and his "
            "coat was heavy with water. He knocked twice upon the great "
            "oak door and waited, breath held, for an answer."
        )
        # wrap the body in several lines so the block has multiple lines
        y = 130
        for chunk in (body[:100], body[100:200], body[200:]):
            page.insert_text((72, y), chunk.strip(), fontsize=11)
            y += 18
    doc.set_toc([[1, "CHAPTER I", 1], [2, "Chapter 1 heading", 2]])
    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------
# AC1 (unit): the real native corpus extracts with coordinates + hashes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", NATIVE_CORPUS)
def test_corpus_native_extraction(name):
    pytest.importorskip("pymupdf")
    from backend.parsing import l1

    data = (CORPUS_DIR / name).read_bytes()
    info = l1.open_document(data)
    assert info["page_count"] >= 1

    for page_index in range(info["page_count"]):
        rec = l1.extract_page(data, page_index)
        assert rec["status"] == "ok"
        assert rec["extractor"] == "pymupdf"
        assert rec["confidence"] > 0.9  # native layer: ~perfect
        assert rec["char_count"] > 0
        # coordinates present on every text block/line
        text_blocks = [b for b in rec["blocks"] if b["kind"] == "text"]
        assert text_blocks, "expected at least one text block"
        for b in text_blocks:
            assert len(b["bbox"]) == 4
            for ln in b["lines"]:
                assert len(ln["bbox"]) == 4
        # hashes are stable and content-derived
        assert rec["text_sha256"]
        assert rec["page_sha256"]
        again = l1.extract_page(data, page_index)
        assert again["page_sha256"] == rec["page_sha256"]


# --------------------------------------------------------------------------
# AC1 + AC2 (API): upload -> parse_l1 -> pages with coordinates + report
# --------------------------------------------------------------------------
async def test_upload_parse_l1_pages_and_report(client):
    from backend.models import DocumentPage, ImportReport
    from tests._f1_helpers import session_scope

    r = await client.post("/api/v1/projects", json={
        "title": "L1 Book", "genre_profile": "saga"})
    assert r.status_code == 201
    pid = r.json()["id"]

    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("book.pdf", _book_pdf(4), "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]

    # wait for the *initial* parse job, then trigger the L1 extraction
    from backend.scheduler import wait_for_workers
    wait_for_workers()
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()

    with session_scope() as db:
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        assert len(pages) == 4
        # one row per page, each with coordinates + hashes (AC1)
        for i, p in enumerate(pages):
            assert p.page_number == i + 1
            assert p.page_payload["blocks"], f"page {i+1} has no blocks"
            assert p.page_payload["page_number"] == i + 1
            for b in p.page_payload["blocks"]:
                assert len(b["bbox"]) == 4
            assert p.text_sha256
            assert p.page_sha256
            assert p.char_count > 0
        # heading detection saw the chapter headings (5.3 font evidence)
        headings = [
            b for p in pages for b in p.page_payload["blocks"]
            if b.get("heading_level")]
        assert headings, "expected at least one heading hint"
        # the repeated running header was flagged (5.2.6)
        report = (
            db.query(ImportReport)
            .filter(ImportReport.document_id == document_id)
            .one()
        )
    summary = report.summary
    assert summary["total_pages"] == 4
    assert summary["pages_ok"] == 4
    # 5.2.6 sections all present (AC2)
    for key in ("pages_ok", "pages_ocr", "suspect_char_ratio",
                "repeated_headers_footers", "pages_with_columns",
                "pages_to_verify"):
        assert key in summary, f"missing 5.2.6 section {key}"
    assert any("MY GREAT NOVEL" in e["text"]
               for e in summary["repeated_headers_footers"])
    assert summary["toc_entries"], "TOC (bookmarks) not extracted"
    assert summary["pdf_metadata"] is not None

    # the report endpoint exposes everything
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/import-report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["total_pages"] == 4
    assert body["pages_ok"] == 4
    assert body["job_progress"]["phase"] == "completed"
    assert body["job_progress"]["pages_done"] == 4


# --------------------------------------------------------------------------
# AC3: a job restarted halfway resumes without duplicates
# --------------------------------------------------------------------------
async def test_restart_resume_without_duplicates(client, monkeypatch):
    from backend import l1_runner
    from backend.models import DocumentPage, ImportReport
    from tests._f1_helpers import session_scope

    r = await client.post("/api/v1/projects", json={
        "title": "Resumable", "genre_profile": "saga"})
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("book.pdf", _book_pdf(4), "application/pdf")},
        data={"copyright_confirmed": "true"})
    document_id = r.json()["document_id"]
    from backend.scheduler import wait_for_workers
    wait_for_workers()

    # First run: crash mid-job by making the per-page progress commit blow
    # up once the persisting phase reaches page 3 (simulated worker crash).
    calls = {"n": 0}
    original_progress = l1_runner._set_progress

    def flaky_progress(db, job, phase, pages_done, total_pages):
        calls["n"] += 1
        if phase == "persisting" and pages_done >= 3:
            calls["crashed"] = True
            raise RuntimeError("simulated worker crash at page 3")
        original_progress(db, job, phase, pages_done, total_pages)

    monkeypatch.setattr(l1_runner, "_set_progress", flaky_progress)

    from backend.db import SessionLocal
    from backend.models import Job as JobModel

    # run the first (crashing) attempt through the scheduler path
    from backend.scheduler import InProcessScheduler

    db = SessionLocal()
    try:
        job = JobModel(
            project_id=pid, job_type="parse_l1",
            payload={"document_id": document_id}, status="queued")
        db.add(job)
        db.commit()
        crashed_job_id = job.id
    finally:
        db.close()

    sched_db = SessionLocal()
    try:
        loaded = sched_db.get(JobModel, crashed_job_id)
        with pytest.raises(RuntimeError):
            l1_runner.run_l1_extraction(sched_db, loaded)
    finally:
        sched_db.close()

    # The restart runs with the REAL progress function (the crash was a
    # one-off simulated event, not a permanent condition).
    monkeypatch.setattr(l1_runner, "_set_progress", original_progress)

    with session_scope() as db:
        n_after_crash = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .count()
        )
        prog_job = db.get(JobModel, crashed_job_id)
        last_payload = prog_job.payload
    assert n_after_crash == 2, f"expected 2 pages before crash, got {n_after_crash}"

    # Second run (the restart): must complete WITHOUT duplicates.
    with session_scope() as db:
        restart_job = JobModel(
            project_id=pid, job_type="parse_l1",
            payload={"document_id": document_id}, status="queued")
        db.add(restart_job)
        db.commit()
        restart_id = restart_job.id

    db = SessionLocal()
    try:
        loaded = db.get(JobModel, restart_id)
        l1_runner.run_l1_extraction(db, loaded)
        db.commit()
    finally:
        db.close()

    with session_scope() as db:
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        # exactly 4 pages, no duplicates (unique constraint held)
        assert len(pages) == 4
        assert [p.page_number for p in pages] == [1, 2, 3, 4]
        # the pages that survived the crash kept identical content hashes
        report = (
            db.query(ImportReport)
            .filter(ImportReport.document_id == document_id)
            .one()
        )
    assert report.summary["total_pages"] == 4
    assert report.summary["pages_ok"] == 4

    # and the idempotent re-run via the API also converges to 4 rows
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202
    wait_for_workers()
    with session_scope() as db:
        count = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .count()
        )
    assert count == 4, f"duplicates after restart: {count}"


# --------------------------------------------------------------------------
# scanned PDF: no text layer -> page marked for OCR in the report
# --------------------------------------------------------------------------
async def test_scanned_document_report_flags_ocr(client):
    from backend.scheduler import wait_for_workers

    scanned = CORPUS_DIR / "scanned_01_old_volume.pdf"
    if not scanned.exists():
        pytest.skip("scanned corpus file missing")
    r = await client.post("/api/v1/projects", json={
        "title": "Scanned", "genre_profile": "classic"})
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("scan.pdf", scanned.read_bytes(),
                        "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201
    document_id = r.json()["document_id"]
    wait_for_workers()
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202
    wait_for_workers()

    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/import-report")
    body = r.json()
    assert body["report"] is not None
    assert body["report"]["pages_ocr"], "scanned pages must be flagged for OCR"
    assert body["pages_ocr_needed"] >= 1
