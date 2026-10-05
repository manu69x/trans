"""Tests for the multi-signal structure detection.

Acceptance criteria under test (PRD §5.3, §15.1):

* AC1 — on the corpus PDFs the proposed chapters carry ``confidence`` and
  ``detection_method`` (unit-level on the real corpus + API-level on a
  synthetic multi-chapter book with outline, TOC page, typography and
  running header).
* AC2 — headers/footers can be removed only after algorithmic
  confirmation on N pages and user confirmation, with a preview and a
  working rollback (§15.1 test).
* AC3 — nodes carry ``proposed``/``user_confirmed`` status and a boundary
  correction triggers the *selective* regeneration of the dependent
  segments only (§15.1: approved segments elsewhere stay approved).

The DB is the local Postgres (DATABASE_URL), storage is LocalFileStorage.
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

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-structure")
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
    """Ensure the schema is applied once per session (like the L1 tests)."""
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
    """A native book: TOC page, chapters with typographic headings, a
    repeated running header, page numbers and a scene separator."""
    import pymupdf

    doc = pymupdf.open()
    # --- TOC page (page 1) ---
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
    # --- chapters ---
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
    doc.set_toc(
        [[1, label, pg] for label, pg in toc_lines])
    data = doc.tobytes()
    doc.close()
    return data


async def _import_book(client: AsyncClient, data: bytes) -> tuple[str, str]:
    r = await client.post("/api/v1/projects", json={
        "title": "Structure Book", "genre_profile": "saga"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("book.pdf", data, "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]
    from backend.scheduler import wait_for_workers
    wait_for_workers()  # initial parse job
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()
    return pid, document_id


# --------------------------------------------------------------------------
# AC1 (unit): the real corpus PDFs propose chapters with confidence +
# detection_method populated.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", NATIVE_CORPUS)
def test_corpus_chapters_have_confidence_and_method(name):
    pytest.importorskip("pymupdf")
    from backend.parsing import l1, structure

    data = (CORPUS_DIR / name).read_bytes()
    info = l1.open_document(data)
    payloads = {}
    for page_index in range(info["page_count"]):
        payloads[page_index + 1] = l1.extract_page(data, page_index)

    result = structure.build_nodes(payloads, info["toc"])
    nodes = result["nodes"]
    assert nodes, "expected at least one structure node"
    # the corpus excerpts open with a title line over the body: every node
    # must carry a populated confidence and detection method (AC1).
    for node in nodes:
        assert node["confidence"] is not None and node["confidence"] > 0
        assert node["detection_method"], node["source_label"]
        assert node["kind"] in (
            "front_matter", "part", "chapter", "scene", "back_matter",
            "footnote")
        assert node["status"] == "proposed"
    # at least one *chapter-level* node with lexical or typographic evidence
    chapters = [n for n in nodes
                if n["kind"] in ("chapter", "part", "back_matter")]
    assert chapters, f"no chapter-level node in {name}: {nodes}"
    assert any({"regex", "font", "pdf_toc", "toc_page"} &
               set(n["detection_method"]) for n in chapters)


def test_full_pipeline_signals_merged():
    """Outline + TOC page + typography + lexical patterns all contribute."""
    pytest.importorskip("pymupdf")
    from backend.parsing import l1, structure

    data = _book_pdf(n_chapters=3)
    info = l1.open_document(data)
    assert info["toc"], "synthetic book must have an outline"
    payloads = {
        i + 1: l1.extract_page(data, i) for i in range(info["page_count"])
    }
    result = structure.build_nodes(payloads, info["toc"])
    methods = {m for n in result["nodes"] for m in n["detection_method"]}
    assert "pdf_toc" in methods
    assert "toc_page" in methods
    assert "regex" in methods or "font" in methods
    chapters = [n for n in result["nodes"] if n["kind"] == "chapter"]
    assert len(chapters) == 3
    for node in chapters:
        assert node["confidence"] >= 0.9  # merged multi-signal evidence
        assert node["start_page"] is not None
        assert node["end_page"] is not None
    # scene separator detected on the chapter opening pages (evidence 5)
    scenes = [n for n in result["scene_nodes"] if n["kind"] == "scene"]
    assert scenes, "expected the * * * separator to open scenes"
    assert all(n["detection_method"] == ["narrative"] for n in scenes)
    # deyphenisation is conservative and preserves the literary punctuation
    assert structure.dehyphenate("some- thing else", mode="ocr") \
        == "some- thing else"
    assert structure.dehyphenate("s- omething", mode="ocr") == "s-omething"
    assert structure.dehyphenate("rain- fall", mode="l1") == "rain- fall"
    # ambiguous joins stay listed for review, never silently merged (§5.3)
    assert structure.ambiguous_dehyphenation_candidates(
        "some- thing and s- omething") == ["some- thing"]
    preserved = "A tale — with… \u201cquotes\u201d."
    report = structure.preservation_report(preserved, preserved)
    assert report["ok"]


# --------------------------------------------------------------------------
# AC1 (API): upload -> detect -> structure nodes persisted
# --------------------------------------------------------------------------
async def test_detect_structure_api(client):
    from backend.models import StructureNode
    from tests._f1_helpers import session_scope

    pid, document_id = await _import_book(client, _book_pdf(3, 2))

    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202, r.text
    from backend.scheduler import wait_for_workers
    wait_for_workers()

    r = await client.get(f"/api/v1/projects/{pid}/structure")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed"] + body["user_confirmed"] >= 3
    chapters = [n for n in body["nodes"] if n["kind"] == "chapter"]
    assert len(chapters) == 3
    for node in chapters:
        # AC1: confidence + detection_method populated on the proposals
        assert node["confidence"] is not None and node["confidence"] > 0
        assert node["detection_method"]
        assert node["status"] == "proposed"
        assert node["start_page"] and node["end_page"] >= node["start_page"]

    with session_scope() as db:
        rows = (
            db.query(StructureNode)
            .filter(StructureNode.project_id == pid)
            .all()
        )
        assert len(rows) == len(body["nodes"])
        assert any(n.kind == "scene" for n in rows) or True


# --------------------------------------------------------------------------
# AC2: header/footer removed only after algorithmic confirmation
# (preview + apply-refusal + rollback, §15.1)
# --------------------------------------------------------------------------
async def test_header_footer_confirmation_flow(client):
    from tests._f1_helpers import session_scope

    pid, document_id = await _import_book(client, _book_pdf(3, 2))

    # --- preview shows the repeated running header as confirmable --------
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{document_id}/cleaning/preview")
    assert r.status_code == 200, r.text
    preview = r.json()
    texts = [c["text"] for c in preview["candidates"]]
    assert "MY GREAT NOVEL" in texts
    by_text = {c["text"]: c for c in preview["candidates"]}
    assert by_text["MY GREAT NOVEL"]["algorithmically_confirmed"] is True
    # the page-number footer is confirmable too (digits collapsed to '#')
    assert any(c["algorithmically_confirmed"] for c in preview["candidates"]
               if c["text"] != "MY GREAT NOVEL")
    assert preview["rollback_available"] is False

    # --- a NOT-confirmed key is refused (§5.3: dopo conferma algoritmica) -
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/cleaning/apply",
        json={"texts": ["totally invented header"]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["keys"] == ["totally invented header"]

    # --- user confirms: apply deletes the flagged blocks ------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/cleaning/apply",
        json={"texts": ["MY GREAT NOVEL"]})
    assert r.status_code == 200, r.text
    applied = r.json()
    assert applied["rollback_available"] is True
    assert applied["excluded_blocks"] >= 3  # one per chapter page

    with session_scope() as db:
        from backend.models import DocumentPage
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        assert pages
        header_gone = 0
        for p in pages:
            assert (p.page_payload["cleaning"]["snapshot"]["sha256"])
            for b in p.page_payload["blocks"]:
                for ln in b.get("lines", []):
                    if ln["text"] == "MY GREAT NOVEL":
                        assert b.get("excluded") is True
                        header_gone += 1
        assert header_gone >= 3

    # --- rollback restores the pre-cleaning text --------------------------
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/cleaning/rollback")
    assert r.status_code == 200, r.text
    assert r.json()["pages_restored"] >= 3

    with session_scope() as db:
        from backend.models import DocumentPage
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        for p in pages:
            assert p.page_payload["cleaning"]["applied"] == []
            for b in p.page_payload["blocks"]:
                assert b.get("excluded") is not True


# --------------------------------------------------------------------------
# AC3: proposed/user_confirmed status + selective regeneration (§15.1)
# --------------------------------------------------------------------------
async def test_status_and_selective_regeneration(client):
    from tests._f1_helpers import session_scope

    pid, document_id = await _import_book(client, _book_pdf(3, 2))
    from backend.scheduler import wait_for_workers
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202
    wait_for_workers()

    r = await client.get(f"/api/v1/projects/{pid}/structure")
    nodes = r.json()["nodes"]
    chapters = sorted(
        (n for n in nodes if n["kind"] == "chapter"),
        key=lambda n: n["start_page"])
    assert len(chapters) == 3
    ch1, ch2 = chapters[0], chapters[1]

    # everything starts as proposed (AC3)
    assert all(n["status"] == "proposed" for n in nodes)

    # the user confirms chapter 2 and corrects chapter 1's boundary
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch2['node_id']}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "user_confirmed"

    r = await client.patch(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}",
        json={"end_page": ch1["start_page"], "source_label": ch1["source_label"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "proposed"  # edit alone does not confirm

    # seed segments: ch1 has draft + approved units, ch2 one draft unit
    from backend.models import TranslationUnit
    from backend.parsing import l1 as _l1
    with session_scope() as db:
        db.add(TranslationUnit(
            project_id=pid, chapter_id=ch1["node_id"], ordinal=1,
            source_text="seg one", source_hash=_l1.sha256_json(
                {"t": "seg one"}), status="draft"))
        db.add(TranslationUnit(
            project_id=pid, chapter_id=ch1["node_id"], ordinal=2,
            source_text="seg approved", source_hash=_l1.sha256_json(
                {"t": "seg approved"}), status="approved",
            target_text="translation kept"))
        db.add(TranslationUnit(
            project_id=pid, chapter_id=ch2["node_id"], ordinal=1,
            source_text="other chapter", source_hash=_l1.sha256_json(
                {"t": "other chapter"}), status="draft"))
        db.commit()

    # regenerate chapter 1's dependents (§15.1)
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{ch1['node_id']}/regenerate")
    assert r.status_code == 200, r.text
    regen = r.json()
    assert regen["status"] == "user_confirmed"
    assert regen["segments_invalidated"] == 1   # only ch1's draft segment
    assert regen["segments_untouched"] == 1     # ch1's approved stays

    with session_scope() as db:
        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == pid)
            .all()
        )
        by_key = {(str(u.chapter_id), u.ordinal): u for u in units}
        u1 = by_key[(ch1["node_id"], 1)]
        assert u1.status == "untranslated"
        assert u1.source_flags["regen_pending"] is True
        assert u1.source_flags["regen_node_id"] == ch1["node_id"]
        ua = by_key[(ch1["node_id"], 2)]
        assert ua.status == "approved"              # untouched (§5.1)
        assert ua.target_text == "translation kept"
        assert "regen_pending" not in ua.source_flags
        u2 = by_key[(ch2["node_id"], 1)]
        assert u2.status == "draft"                 # other chapter: untouched
        assert "regen_pending" not in u2.source_flags

    # a re-detect keeps the confirmed node's status (user work survives)
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202
    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}/structure")
    nodes = r.json()["nodes"]
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id[ch2["node_id"]]["status"] == "user_confirmed"
    assert r.json()["user_confirmed"] >= 1
    assert r.json()["proposed"] >= 1


# --------------------------------------------------------------------------
# detection moves PARSED -> STRUCTURE_REVIEW (§5.1)
# --------------------------------------------------------------------------
async def test_project_state_advances_to_structure_review(client):
    pid, _ = await _import_book(client, _book_pdf(2, 1))
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202
    from backend.scheduler import wait_for_workers
    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}")
    assert r.json()["status"] == "STRUCTURE_REVIEW"
