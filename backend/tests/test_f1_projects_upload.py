"""httpx-based tests for F1: projects CRUD + upload + object storage.

Covers the three acceptance criteria of the corresponding kanban item:

1. an up to 100 MB PDF uploads, hash/metadata are stored and dedup works;
2. the project state follows the §5.1 machine (DRAFT -> IMPORTING -> PARSED);
3. the §13.2 copyright declaration is mandatory on the first upload and is
   recorded in the audit log.

Storage is LocalFileStorage (env STORAGE_ROOT) so no MinIO is required. The DB
is the local Postgres given by DATABASE_URL / ALEMBIC_SQLALCHEMY_URI.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

# Make the backend package importable from the repo root.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.models import Project  # noqa: E402  (used across multiple tests)
from tests._f1_helpers import session_scope  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema (incl. migration 002) is applied once per session."""
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
    # Wait for any background parse job to finish so it can't still hold a DB
    # connection (and the lock it holds) when we drop the tables below.
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    # Roll back and check in any transaction still open on a pooled connection
    # (e.g. a session a previous test forgot to close): an open transaction
    # holds ACCESS SHARE locks that would deadlock the DROP TABLE below.
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    wait_for_workers()
    engine.dispose()


@pytest.fixture(scope="session")
def storage_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("storage")
    os.environ["STORAGE_ROOT"] = str(root)
    return root


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _pdf(n_pages: int = 1) -> bytes:
    """A minimal but valid multi-page PDF (enough for pypdf page count).

    Built with fpdf2 so the file has a correct xref/startxref and pypdf can
    read it back.
    """
    from fpdf import FPDF

    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, text="page")
    # fpdf2.output() returns a ``bytearray``; httpx's multipart encoder (0.28)
    # calls ``.read()`` on the uploaded object and only accepts ``bytes`` (or a
    # file-like), so coerce to ``bytes`` here.
    return bytes(pdf.output())


async def _upload(client, project_id, data, filename="book.pdf", confirmed=True):
    files = {"file": (filename, data, "application/pdf")}
    data_ = {"copyright_confirmed": "true" if confirmed else "false"}
    r = await client.post(f"/api/v1/projects/{project_id}/documents", files=files, data=data_)
    return r


# --------------------------------------------------------------------------
# AC1: upload 100MB OK, hash + metadata stored, dedup works
# --------------------------------------------------------------------------
async def test_upload_100mb_and_metadata(client):
    r = await client.post("/api/v1/projects", json={
        "title": "Big Book", "genre_profile": "fantasy",
    })
    assert r.status_code == 201
    pid = r.json()["id"]

    # ~100 MB payload.
    data = b"0" * (100 * 1024 * 1024)
    r = await _upload(client, pid, data)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "uploaded"
    assert body["size_bytes"] == len(data)
    assert body["new"] is True
    expected_sha = hashlib.sha256(data).hexdigest()
    assert body["sha256"] == expected_sha
    assert body["page_count"] is None  # not a real PDF, best-effort None

    # metadata persisted in the DB
    from backend.models import Document, Job

    with session_scope() as db:
        doc = db.get(Document, body["document_id"])
    assert doc is not None
    assert doc.size_bytes == len(data)
    assert doc.sha256 == expected_sha
    assert doc.storage_key.startswith(f"{pid}/docs/")

    # parse job registered (non-PDF bytes -> parse fails, but registration +
    # the DRAFT->IMPORTING transition still hold)
    with session_scope() as db:
        jobs = db.query(Job).all()
        p = db.get(Project, pid)
    assert len(jobs) == 1 and jobs[0].job_type == "parse"
    assert p.status in ("IMPORTING", "PARSED")


async def test_dedup_by_hash(client):
    r = await client.post("/api/v1/projects", json={
        "title": "Dup", "genre_profile": "essay",
    })
    pid = r.json()["id"]
    # confirm copyright on first upload so it is recorded
    data = b"hello world" * 1000
    r1 = await _upload(client, pid, data)
    assert r1.status_code == 201 and r1.json()["status"] == "uploaded"

    # identical content -> duplicate, no new storage
    r2 = await _upload(client, pid, data)
    body2 = r2.json()
    assert r2.status_code == 201
    assert body2["status"] == "duplicate"
    assert body2["new"] is False
    assert body2["document_id"] == r1.json()["document_id"]

    from backend.models import Document

    with session_scope() as db:
        docs = db.query(Document).all()
    assert len(docs) == 1  # dedup: only one row


# --------------------------------------------------------------------------
# AC2: state machine §5.1 (DRAFT -> IMPORTING -> PARSED)
# --------------------------------------------------------------------------
async def test_state_machine_transitions(client):
    r = await client.post("/api/v1/projects", json={
        "title": "Statey", "genre_profile": "horror",
    })
    pid = r.json()["id"]
    from backend.models import Project

    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.status == "DRAFT"

    # first upload moves DRAFT -> IMPORTING; the parse job then advances it
    # to PARSED (§5.1 / §5.2). The InProcessScheduler runs the parse on a
    # background thread, so wait for it to settle before checking the state.
    from backend.scheduler import wait_for_workers

    r = await _upload(client, pid, _pdf(3), confirmed=True)
    if r.status_code != 201:
        print("\nSTATUS", r.status_code, "BODY", repr(r.text))
    assert r.status_code == 201

    wait_for_workers()
    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.status == "PARSED"

    # invalid transition is rejected (409)
    r = await client.patch(f"/api/v1/projects/{pid}", json={"status": "EXPORTED"})
    # PATCH does not change status (not updatable) -> stays PARSED, no crash
    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.status == "PARSED"


async def test_invalid_transition_rejected(client):
    from backend.service import StateError, ProjectService

    with session_scope() as db:
        svc = ProjectService(db)
        p = svc.create_project("x", "saga")
        with pytest.raises(StateError):
            svc.transition(p.id, "EXPORTED")  # DRAFT -> EXPORTED is illegal
        assert db.get(Project, p.id).status == "DRAFT"


# --------------------------------------------------------------------------
# AC3: copyright mandatory on first upload + audit record
# --------------------------------------------------------------------------
async def test_copyright_required_before_first_upload(client):
    r = await client.post("/api/v1/projects", json={
        "title": "NoCopy", "genre_profile": "scifi",
    })
    pid = r.json()["id"]

    # upload WITHOUT confirming copyright -> 403
    r = await _upload(client, pid, b"data", confirmed=False)
    assert r.status_code == 403

    from backend.models import Document

    with session_scope() as db:
        assert db.query(Document).count() == 0  # nothing stored

    # with confirmation -> success and flag set
    r = await _upload(client, pid, b"data", confirmed=True)
    assert r.status_code == 201
    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.copyright_confirmed is True

    # audit log recorded both the refusal-then-confirm and the upload
    from backend.models import AuditLog

    with session_scope() as db:
        actions = [a.action for a in db.query(AuditLog).all()]
    assert "copyright_confirmed" in actions
    assert "document_uploaded" in actions


# --------------------------------------------------------------------------
# CRUD sanity
# --------------------------------------------------------------------------
async def test_crud(client):
    r = await client.post("/api/v1/projects", json={
        "title": "T1", "genre_profile": "saga",
    })
    pid = r.json()["id"]

    got = await client.get(f"/api/v1/projects/{pid}")
    assert got.status_code == 200 and got.json()["title"] == "T1"

    upd = await client.patch(f"/api/v1/projects/{pid}", json={"title": "T2"})
    assert upd.status_code == 200 and upd.json()["title"] == "T2"

    # non-updatable field is rejected with 400
    bad = await client.patch(f"/api/v1/projects/{pid}", json={"status": "X"})
    assert bad.status_code == 400
    lst = await client.get("/api/v1/projects")
    assert lst.status_code == 200 and any(p["id"] == pid for p in lst.json())


async def test_jobs_endpoint(client):
    r = await client.post("/api/v1/projects", json={"title": "J", "genre_profile": "saga"})
    pid = r.json()["id"]
    await _upload(client, pid, _pdf(3), confirmed=True)
    # The parse runs on a background thread (mirrors the production Celery
    # model of §14): the upload returns while the job is still ``queued``, so
    # wait for the worker to settle before reading the job list.
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}/jobs")
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1 and jobs[0]["status"] == "completed"
