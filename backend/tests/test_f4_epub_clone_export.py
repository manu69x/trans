"""EPUB clone export test (2026-10-03).

"Con clone struttura" per EPUB (richiesta utente 2026-10-03, replicando il
flusso del PDF clone §31–40):

* AC1 -- export ``format=epub`` + ``clone_structure`` e' HTTP 200 e produce
  un EPUB valido con la copertina come immagine ``cover-image`` (meta
  ``name="cover"``), la pagina cover.xhtml e le immagini per capitolo;
* AC2 -- le immagini dei capitoli finiscono SOLO nei capitoli le cui pagine
  originali contengono l'immagine, con larghezza relativa al bbox
  (``width:%`` calcolato dal profilo);
* AC3 -- senza profilo tipografico il clone e' rifiutato con 409.
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_export",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

STORAGE_ROOT = os.environ.setdefault(
    "STORAGE_ROOT", "/tmp/trans-test-f4-export-clone")
os.environ.setdefault("LOCAL_ONLY", "1")

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea735c9a40000000049454e44ae"
    "426082"
)


def _ensure_test_db() -> None:
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
from backend.db import SessionLocal  # noqa: E402
from backend.models import (  # noqa: E402
    Document,
    DocumentPage,
    StructureNode,
    TranslationUnit,
)
from backend import typography as _typo  # noqa: E402


def _uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


async def _new_project(client: AsyncClient) -> str:
    r = await client.post(
        "/api/v1/projects",
        json={"title": "Clone Epub Book", "genre_profile": "saggio"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_book_with_profile(pid: str, tmp_root: str) -> None:
    """2 capitoli (pagine 2-3 e 4-5), cover a pag.1 + figura a pag.4."""
    ch1, ch2 = _uuid("cch1"), _uuid("cch2")
    with SessionLocal() as db:
        db.add(Document(
            id=_uuid(f"doc-{pid}"),
            project_id=pid,
            filename="original.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256="a" * 64,
            page_count=5,
            storage_key=f"{pid}/original.pdf",
        ))
        for pno in range(1, 6):
            db.add(DocumentPage(
                document_id=_uuid(f"doc-{pid}"),
                page_number=pno,
                extractor="pymupdf",
                confidence=1.0,
                char_count=0,
                suspect_chars=0,
                text_sha256="0" * 64,
                page_sha256="0" * 64,
                normalized_text="",
                page_payload={"width": 612.0, "height": 792.0,
                              "blocks": [], "normalized_text": ""},
            ))
        db.add(StructureNode(
            id=ch1, project_id=pid, kind="chapter",
            normalized_title="Capitolo uno", ordinal=1, status="confirmed",
            start_page=2, end_page=3,
        ))
        db.add(StructureNode(
            id=ch2, project_id=pid, kind="chapter",
            normalized_title="Capitolo due", ordinal=2, status="confirmed",
            start_page=4, end_page=5,
        ))
        for i in range(3):
            db.add(TranslationUnit(
                id=_uuid(f"cs1-{i}"), project_id=pid, chapter_id=ch1,
                ordinal=i + 1,
                source_text=f"Chapter one, line {i + 1}.",
                target_text=f"Capitolo uno, riga {i + 1}."
                if i else "<i>Capitolo uno</i>, riga 1.",
                status="approved", source_hash="x" * 64,
            ))
        for i in range(2):
            db.add(TranslationUnit(
                id=_uuid(f"cs2-{i}"), project_id=pid, chapter_id=ch2,
                ordinal=i + 1,
                source_text=f"Chapter two, line {i + 1}.",
                target_text=f"Capitolo due, riga {i + 1}.",
                status="approved", source_hash="x" * 64,
            ))
        db.commit()

    # Profilo tipografico + immagini nel provider di storage (get_storage_
    # provider() viene letto a ogni chiamata: la stessa STORAGE_ROOT vale
    # per il processo di test e per l'app ASGI in-process).
    images = [
        {"image_id": "p0001-00", "page": 1, "ext": "png",
         "bbox": [0.0, 0.0, 612.0, 792.0]},
        {"image_id": "p0004-00", "page": 4, "ext": "png",
         "bbox": [200.0, 300.0, 452.0, 500.0]},
    ]
    _page_texts = {
        1: [],
        2: ["chapter", "one", "line", "1",
            "chapter", "one", "line", "2"],
        3: ["chapter", "one", "line", "3"],
        4: ["chapter", "two", "line", "1"],
        5: ["chapter", "two", "line", "2"],
    }
    profile = {
        "source_document_id": _uuid(f"doc-{pid}"),
        "source_filename": "original.pdf",
        "original_page_count": 5,
        "page_width": 612.0,
        "page_height": 792.0,
        "body_font": "LiberationSerif",
        "body_size": 15.0,
        "body_leading": 18.0,
        "fonts": [],
        "dominant_sizes": [],
        "images": images,
        "image_count": len(images),
        "pages": [
            {"page": pno, "width": 612.0, "height": 792.0,
             "blocks": [], "images": [],
             "text": _page_texts[pno]}
            for pno in range(1, 6)
        ],
    }
    _typo.save_typography(pid, profile)
    _typo.put_image(pid, "p0001-00", "png", PNG_1PX, "image/png")
    _typo.put_image(pid, "p0004-00", "png", PNG_1PX, "image/png")


@pytest.fixture(autouse=True)
def _reset():
    from backend.db import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_ac1_epub_clone_has_cover_and_chapter_images(client):
    pid = await _new_project(client)
    _seed_book_with_profile(pid, "/tmp/trans-test-f4-export-clone")

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "epub", "clone_structure": True},
    )
    assert r.status_code == 200, r.text
    data = r.content
    assert len(data) > 1000

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        # 1) copertina: item immagine + meta + pagina cover
        opf = z.read("EPUB/content.opf").decode("utf-8")
        assert 'properties="cover-image"' in opf
        assert '<meta name="cover"' in opf
        assert "EPUB/cover.xhtml" in names
        # FIX 2026-10-03: niente pagina titolo nel clone (la cover e' la
        # prima pagina); la spine deve iniziare con la cover LINEARE
        # (linear="yes": con linear="no" i reader la saltano).
        assert "EPUB/title.xhtml" not in names
        first_ref_line = next(
            l for l in opf.splitlines() if "<itemref" in l)
        assert 'idref="cover"' in first_ref_line
        assert 'linear="no"' not in first_ref_line
        # l'immagine cover e' nel pacchetto (set_cover -> EPUB/cover.png)
        assert "EPUB/cover.png" in names
        # la copertina NON finisce anche nella pagina front-images
        assert not any("front-images" in n and "p0001-00" in z.read(n).decode(
            "utf-8", "ignore") for n in names)
        # 2) figura del capitolo 2 (pag.4) dentro chapter-2.xhtml
        ch2 = next(
            n for n in names
            if n.endswith(".xhtml") and "chapter-2" in n)
        ch2_html = z.read(ch2).decode("utf-8")
        assert "images/p0004-00" in ch2_html, "chapter-2 image missing"
        assert "width:41.2%" in ch2_html  # 252pt / 612pt
        assert "<em>Capitolo uno</em>" not in ch2_html
        # il capitolo 1 NON deve avere la figura di pag.4
        ch1 = next(
            n for n in names
            if n.endswith(".xhtml") and "chapter-1" in n)
        ch1_html = z.read(ch1).decode("utf-8")
        assert "images/p0004-00" not in ch1_html
        assert "<em>Capitolo uno</em>, riga 1." in ch1_html
        # 3) spine valida con oggetti (nessun idref duplicato/rotto)
        spine_ids = [
            l.split('idref="')[1].split('"')[0]
            for l in opf.splitlines() if "<itemref" in l
        ]
        assert len(spine_ids) == len(set(spine_ids)), spine_ids
        manifest_ids = [
            l.split('id="')[1].split('"')[0]
            for l in opf.splitlines() if "<item " in l
        ]
        missing = [i for i in spine_ids if i not in manifest_ids]
        assert not missing, f"spine refs missing from manifest: {missing}"


async def test_ac2_epub_clone_requires_profile(client):
    pid = await _new_project(client)
    from backend.models import StructureNode, TranslationUnit

    ch1 = _uuid("pch1")
    with SessionLocal() as db:
        db.add(StructureNode(
            id=ch1, project_id=pid, kind="chapter",
            normalized_title="Capitolo uno", ordinal=1, status="confirmed",
            start_page=1, end_page=1,
        ))
        db.add(TranslationUnit(
            id=_uuid("pu1"), project_id=pid, chapter_id=ch1, ordinal=1,
            source_text="Hello.", target_text="Ciao.",
            status="approved", source_hash="x" * 64,
        ))
        db.commit()

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "epub", "clone_structure": True},
    )
    assert r.status_code == 409, r.text
    assert "profilo tipografico" in r.text


async def test_ac3_epub_clone_opens_in_calibre(client):
    """Test oro (runbook §30): l'EPUB clonato si apre con ebook-convert."""
    pid = await _new_project(client)
    _seed_book_with_profile(pid, "/tmp/trans-test-f4-export-clone")

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "epub", "clone_structure": True},
    )
    assert r.status_code == 200, r.text
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="epub-clone-test") as tmp:
        src = Path(tmp) / "clone.epub"
        src.write_bytes(r.content)
        proc = subprocess.run(
            ["ebook-convert", str(src), str(Path(tmp) / "out.epub")],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
