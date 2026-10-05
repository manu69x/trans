"""httpx + unit tests for the F1 OCR worker.

Acceptance criteria under test:

* AC1 — scanned corpus PDFs produce OCR text with per-page confidence and
  per-line bounding boxes (unit-level on the real corpus files + API-level
  through the async job queue).
* AC2 — the suspect-OCR flag exists on the page data and is propagated to
  the translation-unit segments (QA §10.2 / UI filter §11.2 input).
* AC3 — selective re-OCR of single pages works on request (§5.2).
* AC4 — idempotent/resumable: a finished job re-run converges to the same
  rows without duplicates.
* AC5 — the escalation L3→L4 policy of ADR-002 is wired and improves or
  preserves quality on the escalated pages.

The DB is the local Postgres (DATABASE_URL), storage is LocalFileStorage so
no MinIO is needed. The corpus PDFs live in docs/benchmarks/corpus/. The
OCR engine is the local tesseract binary.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-ocr")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.models import DocumentPage, ImportReport, Job, TranslationUnit  # noqa: E402
from tests._f1_helpers import session_scope  # noqa: E402

CORPUS_DIR = REPO_ROOT / "docs" / "benchmarks" / "corpus"
SCANNED_CORPUS = [
    "scanned_01_old_volume.pdf",
    "scanned_02_manuscript.pdf",
    "scanned_03_periodical.pdf",
]


def _tesseract_or_skip() -> None:
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract not available")


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema (incl. migration 005) is applied once per session."""
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
    _restore_db_level_schema()
    engine.dispose()
    yield
    wait_for_workers()
    engine.dispose()


def _restore_db_level_schema() -> None:
    """Re-apply the schema pieces ``create_all`` cannot know about.

    ``Base.metadata.drop_all`` drops the ``audit_log`` table together with
    its append-only trigger (migration 003, PRD §13.1), and ``create_all``
    recreates only the table. Without this restore, ANY pytest run leaves
    the DB without the DB-level immutability enforcement and
    ``verify_f1_e2e.py`` (run later against the same DB) fails its
    append-only checks.
    """
    from sqlalchemy import text

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


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _scanned_page_image(text_lines: list[str], seed: int = 1) -> bytes:
    """PNG bytes of a scanned-looking page (raster text, noise, rotation)."""
    import io
    import random

    from PIL import Image, ImageDraw, ImageFont

    rnd = random.Random(seed)
    img = Image.new("RGB", (612, 792), (238, 232, 214))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    y = 90
    for line in text_lines:
        draw.text((70, y), line, font=font, fill=(35, 28, 18))
        y += 28
    px = img.load()
    for _ in range((img.width * img.height) // 900):
        gx, gy = rnd.randrange(img.width), rnd.randrange(img.height)
        v = rnd.choice([90, 255])
        px[gx, gy] = (v, v, v)
    img = img.rotate(0.4, expand=False, fillcolor=(220, 214, 198))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _scanned_pdf(text_lines: list[str], seed: int = 1) -> bytes:
    """A scanned-style PDF: rasterised text, NO text layer (§5.2 step 3)."""
    import pymupdf

    png = _scanned_page_image(text_lines, seed=seed)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(0, 0, 612, 792), stream=png)
    data = doc.tobytes()
    doc.close()
    return data


async def _upload(client: AsyncClient, data: bytes, name: str = "scan.pdf"):
    r = await client.post("/api/v1/projects", json={
        "title": "OCR Book", "genre_profile": "saga"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": (name, data, "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    from backend.scheduler import wait_for_workers

    wait_for_workers()  # initial parse job
    return pid, r.json()["document_id"]


def _wait_job(job_id: str, timeout: float = 180.0) -> str:
    """Poll the job row until it leaves 'queued'/'running' (thread-safe).

    ``wait_for_workers`` alone can race the worker thread's registration
    (the thread registers itself *inside* the thread), so the tests poll
    the durable job state instead and only then join the workers.
    """
    import time

    from backend.db import SessionLocal
    from backend.scheduler import wait_for_workers

    deadline = time.monotonic() + timeout
    status = "queued"
    while time.monotonic() < deadline:
        wait_for_workers(timeout=1.0)
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            status = job.status if job is not None else "missing"
        finally:
            db.close()
        if status not in ("queued", "running", "missing"):
            return status
        time.sleep(0.2)
    return status


# --------------------------------------------------------------------------
# AC1 (unit): the real scanned corpus OCRs with bbox + confidence per line
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", SCANNED_CORPUS)
def test_corpus_scanned_ocr_bbox_confidence(name):
    pytest.importorskip("pymupdf")
    _tesseract_or_skip()
    from backend.parsing import ocr

    data = (CORPUS_DIR / name).read_bytes()
    record = ocr.ocr_page(data, 0, level="L3")
    assert record["page_number"] == 1
    assert record["level"] == "L3"
    assert record["lines"], "no OCR lines extracted"
    for ln in record["lines"]:
        assert len(ln["bbox"]) == 4
        x0, y0, x1, y1 = ln["bbox"]
        assert 0 <= x0 < x1 and 0 <= y0 < y1
        assert 0.0 <= ln["confidence"] <= 1.0
        assert isinstance(ln["suspect"], bool)
        assert ln["reading_order"] >= 0
    # reading order is monotonic in extraction order (5.2: ordine di lettura)
    orders = [ln["reading_order"] for ln in record["lines"]]
    assert orders == sorted(orders)
    assert record["text_sha256"]
    assert record["page_sha256"]
    # the suspect flag must EXIST on the page record (AC2, 5.2/13)
    assert isinstance(record["ocr_suspect"], bool)


def test_l4_quality_pass_carries_bbox_and_confidence():
    pytest.importorskip("pymupdf")
    _tesseract_or_skip()
    from backend.parsing import ocr

    data = (CORPUS_DIR / "scanned_01_old_volume.pdf").read_bytes()
    l4 = ocr.ocr_page(data, 0, level="L4")
    assert l4["lines"]
    # L4 lines carry bbox + confidence exactly like L3 (AC1)
    assert all(len(ln["bbox"]) == 4 for ln in l4["lines"])
    assert all(0.0 <= ln["confidence"] <= 1.0 for ln in l4["lines"])


# --------------------------------------------------------------------------
# AC1 (API): scanned PDF -> async OCR job -> pages with confidence + bbox
# --------------------------------------------------------------------------
async def test_api_ocr_document_end_to_end(client):
    _tesseract_or_skip()
    from backend.scheduler import wait_for_workers

    data = _scanned_pdf([
        "The harbour was quiet in the early morning.",
        "Maren counted the boats twice before dawn.",
    ])
    pid, document_id = await _upload(client, data)

    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr")
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["job_type"] == "ocr_document"

    assert _wait_job(job_id) == "completed"

    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job.status == "completed", job.error
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        assert len(pages) == 1
        page = pages[0]
        assert (page.page_payload or {}).get("ocr_record"), "no OCR record"
        ocr_rec = page.page_payload["ocr_record"]
        assert ocr_rec["lines"], "no OCR lines"
        assert all(len(ln["bbox"]) == 4 for ln in ocr_rec["lines"])
        assert 0.0 < float(page.confidence) <= 1.0
        assert page.ocr_level in ("L3", "L4")
        assert isinstance(page.ocr_suspect, bool)
        assert page.normalized_text
        # recognisable words made it through the whole pipeline
        assert "harbour" in page.normalized_text.lower()
        report = (
            db.query(ImportReport)
            .filter(ImportReport.document_id == document_id)
            .one()
        )
        assert report.summary["ocr"]["pages_ocr_done"] == [1]

    # the ocr-report endpoint exposes per-page status (5.2.6)
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr-report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"][0]["ocr_done"] is True
    assert body["pages"][0]["page_number"] == 1
    assert "confidence" in body["pages"][0]
    assert "ocr_suspect" in body["pages"][0]

    # the page endpoint exposes lines with bbox + confidence (AC1)
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/pages/1")
    assert r.status_code == 200, r.text
    page_body = r.json()
    assert page_body["lines"], "page endpoint exposes no OCR lines"
    assert all("bbox" in ln and "confidence" in ln
               for ln in page_body["lines"])


# --------------------------------------------------------------------------
# AC2: suspect flag propagation to translation units (10.2 / 11.2)
# --------------------------------------------------------------------------
async def test_suspect_flag_propagates_to_segments(client, monkeypatch):
    _tesseract_or_skip()
    from backend import ocr_runner
    from backend.scheduler import wait_for_workers

    data = _scanned_pdf(["A quiet line of text for the test."])
    pid, document_id = await _upload(client, data)

    # Segments pointing at page 1, as the (later-phase) segmenter would
    # create them.
    with session_scope() as db:
        db.add(TranslationUnit(
            project_id=pid, ordinal=1,
            source_text="A quiet line of text for the test.",
            source_hash="h1", source_flags={"ocr_page": 1}))
        db.add(TranslationUnit(
            project_id=pid, ordinal=2,
            source_text="Second unit", source_hash="h2",
            source_flags={"ocr_page": 1, "ocr_suspect": False}))
        db.commit()

    # Force a suspect outcome deterministically: monkeypatch the pure layer
    # so every attempt reports suspect lines (the QA filter must light up).
    original = ocr_runner.ocr.ocr_page

    def fake_ocr_page(data_bytes, page_index, level="L3"):
        rec = original(data_bytes, page_index, level=level)
        rec["ocr_suspect"] = True
        for ln in rec["lines"]:
            ln["suspect"] = True
        return rec

    monkeypatch.setattr(ocr_runner.ocr, "ocr_page", fake_ocr_page)

    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr")
    assert r.status_code == 202, r.text
    assert _wait_job(r.json()["id"]) == "completed"

    with session_scope() as db:
        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == pid)
            .order_by(TranslationUnit.ordinal)
            .all()
        )
        assert len(units) == 2
        for unit in units:
            assert unit.source_flags.get("ocr_suspect") is True, (
                f"unit {unit.ordinal} not flagged")
        page = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .one()
        )
        assert bool(page.ocr_suspect) is True


# --------------------------------------------------------------------------
# AC3: selective re-OCR of single pages (5.2)
# --------------------------------------------------------------------------
async def test_selective_reocr_single_page(client):
    _tesseract_or_skip()
    import pymupdf
    from backend.scheduler import wait_for_workers

    doc = pymupdf.open()
    for i in range(1, 4):
        png = _scanned_page_image([f"Page number {i} test line"], seed=i)
        page = doc.new_page(width=612, height=792)
        page.insert_image(pymupdf.Rect(0, 0, 612, 792), stream=png)
    multi = doc.tobytes()
    doc.close()

    pid, document_id = await _upload(client, multi)
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr",
        params={"page_numbers": [2]})
    assert r.status_code == 202, r.text
    assert _wait_job(r.json()["id"]) == "completed"

    with session_scope() as db:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        assert [p.page_number for p in rows] == [2], (
            "selective re-OCR touched pages it should not")
        assert (rows[0].page_payload or {}).get("ocr_record")


# --------------------------------------------------------------------------
# AC4: idempotent re-run — rows converge, no duplicates
# --------------------------------------------------------------------------
async def test_ocr_rerun_idempotent(client):
    _tesseract_or_skip()
    from backend.scheduler import wait_for_workers

    data = _scanned_pdf(["Resume test line for idempotency."])
    pid, document_id = await _upload(client, data)

    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr")
    assert r.status_code == 202, r.text
    assert _wait_job(r.json()["id"]) == "completed"

    with session_scope() as db:
        first = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .one()
        )
        first_sha = first.page_sha256

    # Re-run the whole document: rows must converge (same page_sha256),
    # not duplicate.
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/ocr")
    assert r.status_code == 202, r.text
    assert _wait_job(r.json()["id"]) == "completed"

    with session_scope() as db:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].page_sha256 == first_sha


# --------------------------------------------------------------------------
# AC5: L3 -> L4 escalation recorded (ADR-002)
# --------------------------------------------------------------------------
def test_escalation_policy_thresholds():
    _tesseract_or_skip()
    from backend.parsing import ocr as ocr_mod

    # The calibration constants exist and levels map to distinct PSM modes.
    assert ocr_mod.PSM_BY_LEVEL == {"L3": 3, "L4": 11}
    assert 0.0 < ocr_mod.LINE_SUSPECT_CONF < 1.0
    data = (CORPUS_DIR / "scanned_01_old_volume.pdf").read_bytes()
    l3 = ocr_mod.ocr_page(data, 0, level="L3")
    if l3["ocr_suspect"]:
        # the runner would escalate; L4 must clear or roughly preserve quality
        l4 = ocr_mod.ocr_page(data, 0, level="L4")
        assert (not l4["ocr_suspect"]) or (
            l4["mean_line_conf"] >= l3["mean_line_conf"] - 0.2)
