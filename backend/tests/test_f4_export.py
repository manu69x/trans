"""Export tests (PRD §15.4, §13.1, §11.1 / ).

* AC1 -- a DOCX and EPUB export preserve chapter headings and keep
  ``<i>/<em>`` italics from the target markup.
* AC2 -- a ``MemorySnapshot`` (``snapshot_type='export'``) and one
  ``audit_log`` row are written for every export.
* AC3 -- the ``/export/plan`` endpoint returns the effective plan (counts +
  manifest) and the ``/export/snapshots`` endpoint lists prior exports.
* §15.4 gating -- exporting with no approved segments and no explicit
  ``include_drafts+watermark`` returns 409.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

# Isolated test database, decided BEFORE backend.main is imported (same
# pattern as test_f4_qa.py -- the engine is built from DATABASE_URL at
# import time, so a setdefault inside a fixture would come too late).
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_export",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f4-export")
os.environ.setdefault("LOCAL_ONLY", "1")


def _ensure_test_db() -> None:
    """(Re)create ``trans_export`` with the ``vector`` extension before the
    ``backend.main`` import, mirroring the F4 ``trans_qa`` pattern."""
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_export' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_export"))
            conn.execute(text(
                "CREATE DATABASE trans_export TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_export",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


_ensure_test_db()

from backend.main import app  # noqa: E402
from backend.db import Base, engine, SessionLocal  # noqa: E402
from backend.models import (  # noqa: E402
    AuditLog,
    MemorySnapshot,
    StructureNode,
    TranslationUnit,
)


def _drop_and_recreate_test_db() -> None:
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_export' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_export"))
            conn.execute(text(
                "CREATE DATABASE trans_export TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_export",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    import subprocess
    engine.dispose()
    _drop_and_recreate_test_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True, cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL},
    )
    engine.dispose()
    yield
    engine.dispose()


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


def _uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def _node(pid: str, ordinal: int, kind: str, title: str, node_id: str) -> StructureNode:
    return StructureNode(
        id=node_id,
        project_id=pid,
        kind=kind,
        normalized_title=title,
        ordinal=ordinal,
        status="confirmed",
    )


def _unit(
    pid: str,
    chapter_id: str | None,
    ordinal: int,
    name: str,
    source: str,
    target: str | None,
    status: str,
    flags: dict | None = None,
) -> TranslationUnit:
    return TranslationUnit(
        id=_uuid(name),
        project_id=pid,
        chapter_id=chapter_id,
        ordinal=ordinal,
        source_text=source,
        target_text=target,
        status=status,
        source_hash="x" * 64,
        source_flags=flags or {},
    )


async def _new_project(client: AsyncClient, title: str = "Export Book") -> str:
    r = await client.post(
        "/api/v1/projects",
        json={"title": title, "genre_profile": "saggio"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_book(
    pid: str,
    *,
    ch1: str,
    ch2: str,
    statuses: list[str] | None = None,
) -> None:
    """Two chapters, three units each; unit1 of ch1 has <i> markup."""
    statuses = statuses or ["approved", "machine_draft", "approved"]
    with SessionLocal() as db:
        # The units' ``chapter_id`` IS the chapter node's id (as in chunking),
        # so the node ids must be ``ch1``/``ch2`` for ``build_exporter`` to map
        # each segment to its chapter.
        db.add(_node(pid, 1, "chapter", "Capitolo uno", ch1))
        db.add(_node(pid, 2, "chapter", "Capitolo due", ch2))
        for i in range(3):
            src = f"Chapter one, line {i+1}."
            tgt = (
                f'<i>Capitolo uno</i>, riga {i+1}.'
                if (i == 0)
                else f"Capitolo uno, riga {i+1}."
            )
            db.add(_unit(
                pid, ch1, i + 1, f"ch1-{i}", src, tgt, statuses[i]))
        for i in range(3):
            src = f"Chapter two, line {i+1}."
            tgt = f"Capitolo due, riga {i+1}."
            db.add(_unit(
                pid, ch2, i + 1, f"ch2-{i}", src, tgt, statuses[i]))
        db.commit()


# ---------------------------------------------------------------------------
# AC1 -- DOCX export preserves chapter headings and italics
# ---------------------------------------------------------------------------
async def test_ac1_docx_preserves_chapters_and_italics(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "docx"},
    )
    assert r.status_code == 200, r.text
    data = r.content
    assert len(data) > 0
    # The DOCX is a ZIP; the document.xml inside should contain both
    # chapter titles and the italic run.
    import zipfile
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "Capitolo uno" in doc_xml
    assert "Capitolo due" in doc_xml
    # The <i> markup was converted to a docx <w:i> run
    assert "<w:i" in doc_xml, "italic markup not preserved in DOCX"


# ---------------------------------------------------------------------------
# AC1 (EPUB) -- EPUB export preserves chapter headings and italics
# ---------------------------------------------------------------------------
async def test_ac1_epub_preserves_chapters_and_italics(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "epub"},
    )
    assert r.status_code == 200, r.text
    data = r.content
    assert len(data) > 0
    import zipfile
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        # The EPUB nests items under EPUB/ (e.g. EPUB/chapter-1.xhtml)
        chapter_files = [n for n in names if "chapter-" in n and n.endswith(".xhtml")]
        assert len(chapter_files) >= 2, f"expected 2 chapter files, got {chapter_files}"
        # Read the first chapter's XHTML and check for the <em> tag
        first = z.read(chapter_files[0]).decode("utf-8")
        assert "<em>" in first, "italic markup not preserved in EPUB"
        assert "Capitolo uno" in first


# ---------------------------------------------------------------------------
# AC2 -- MemorySnapshot + audit_log row are written
# ---------------------------------------------------------------------------
async def test_ac2_snapshot_and_audit_written(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "docx"},
    )
    assert r.status_code == 200, r.text
    snap_id = r.headers.get("X-Export-Snapshot-Id")
    assert snap_id, "snapshot id missing from response header"

    with SessionLocal() as db:
        snap = db.get(MemorySnapshot, snap_id)
        assert snap is not None, "MemorySnapshot not persisted"
        assert snap.snapshot_type == "export"
        assert str(snap.project_id) == pid
        assert snap.item_count == 4, f"expected 4 approved segments, got {snap.item_count}"

        audit_rows = (
            db.query(AuditLog)
            .filter(AuditLog.project_id == pid, AuditLog.action == "export")
            .all()
        )
        assert len(audit_rows) == 1, f"expected 1 audit row, got {len(audit_rows)}"
        assert audit_rows[0].entity == "project"


# ---------------------------------------------------------------------------
# AC3 -- /export/plan returns the effective plan
# ---------------------------------------------------------------------------
async def test_ac3_plan_endpoint_returns_counts(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export/plan",
        json={"format": "docx"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Only the 4 approved segments are selected (2 per chapter)
    assert body["selected_segments"] == 4, body
    assert body["counts"]["approved"] == 4
    assert body["counts"]["drafts"] == 2
    # The manifest is present
    assert body["manifest"]["project_id"] == pid
    assert body["manifest"]["format"] == "docx"
    # Chapter titles are in the response
    assert "Capitolo uno" in body["chapters"]
    assert "Capitolo due" in body["chapters"]


# ---------------------------------------------------------------------------
# §15.4 -- no approved segments, no watermark → 409
# ---------------------------------------------------------------------------
async def test_15_4_no_approved_no_watermark_blocked(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    # All units are machine_draft (none approved)
    await _seed_book(
        pid, ch1=ch1, ch2=ch2,
        statuses=["machine_draft", "machine_draft", "machine_draft"],
    )

    # No include_drafts → 409
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "docx"},
    )
    assert r.status_code == 409, r.text
    # include_drafts without watermark → 409
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "docx", "include_drafts": True},
    )
    assert r.status_code == 409, r.text
    # include_drafts WITH watermark → 200
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "docx", "include_drafts": True, "watermark": True},
    )
    assert r.status_code == 200, r.text
    # The [BOZZA] watermark is in the document
    import zipfile, io
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "BOZZA" in doc_xml, "watermark not in DOCX"


# ---------------------------------------------------------------------------
# AC1 (HTML) -- HTML export dispatches to the HTML writer and embeds the
# §15.4 manifest block (regression: the old route fell through to write_epub).
# ---------------------------------------------------------------------------
async def test_ac1_html_export_and_manifest_block(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "html"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html"), r.headers
    body = r.text
    # It is real HTML, not an EPUB zip.
    assert "<html" in body and "PK" not in body[:4]
    # Both chapter headings survive.
    assert "Capitolo uno" in body
    assert "Capitolo due" in body
    # Italics from <i>/<em> markup are preserved as <em>.
    assert "<em>Capitolo uno</em>" in body
    # The §15.4 manifest block is embedded in the document.
    assert "Manifest" in body
    assert "Segmenti:" in body


# ---------------------------------------------------------------------------
# AC3 -- the /export/plan manifest counts are verifiable against the DB
# (total/approved/drafts match the actual translation_units rows).
# ---------------------------------------------------------------------------
async def test_ac3_manifest_counts_match_db(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    # 2 approved + 2 machine_draft per chapter = 4 approved, 2 drafts, 6 total
    await _seed_book(
        pid, ch1=ch1, ch2=ch2,
        statuses=["approved", "machine_draft", "approved"],
    )

    r = await client.post(
        f"/api/v1/projects/{pid}/export/plan",
        json={"format": "docx"},
    )
    assert r.status_code == 200, r.text
    counts = r.json()["manifest"]["counts"]

    # Verify the manifest counts against the raw DB.
    with SessionLocal() as db:
        from sqlalchemy import func

        total = db.query(func.count(TranslationUnit.id)).filter(
            TranslationUnit.project_id == pid
        ).scalar()
        approved = db.query(func.count(TranslationUnit.id)).filter(
            TranslationUnit.project_id == pid,
            TranslationUnit.status == "approved",
        ).scalar()

    assert counts["total_segments"] == total == 6
    assert counts["approved"] == approved == 4
    assert counts["drafts"] == total - approved == 2


# ---------------------------------------------------------------------------
# AC3 -- /export/snapshots lists prior exports
# ---------------------------------------------------------------------------
async def test_ac3_snapshots_endpoint(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(pid, ch1=ch1, ch2=ch2)

    # Do two exports
    for _ in range(2):
        r = await client.post(
            f"/api/v1/projects/{pid}/export",
            json={"format": "docx"},
        )
        assert r.status_code == 200, r.text

    r = await client.get(f"/api/v1/projects/{pid}/export/snapshots")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["snapshots"]) == 2
    assert body["snapshots"][0]["format"] == "docx"
    # Most recent first
    assert body["snapshots"][0]["created_at"] >= body["snapshots"][1]["created_at"]


# ---------------------------------------------------------------------------
# Unknown project → 404
# ---------------------------------------------------------------------------
async def test_unknown_project_404(client):
    r = await client.post(
        f"/api/v1/projects/{_uuid('nope')}/export",
        json={"format": "docx"},
    )
    assert r.status_code == 404, r.text

    r = await client.post(
        f"/api/v1/projects/{_uuid('nope')}/export/plan",
        json={"format": "docx"},
    )
    assert r.status_code == 404, r.text
