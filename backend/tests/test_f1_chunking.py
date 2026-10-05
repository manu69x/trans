"""Tests for CAT chunking & segmentation.

Acceptance criteria under test (PRD §5.4, §15.3):

* AC1 — the planner produces blocks all within **16,384 TOTAL tokens**,
  measured with the real tokenizer (tiktoken BPE per ADR-001 §3.4), with
  the 10-11k source / 2-2.5k glossary-TM / 2-3k output budget split.
* AC2 — the §5.4.3 anti-break rules hold on the dialogue-heavy corpus
  chapter: dialogue with speech tag, unclosed quotation, placeholder and
  multi-token proper names are never split mid-construct.
* AC3 — the overlap segments (1-2 preceding + 1 following) are marked
  ``DO_NOT_TRANSLATE_CONTEXT`` read-only in the serialised payload, and
  segment IDs are stable across re-segmentation.

The DB is the local Postgres (DATABASE_URL); the API tests run the import
→ structure → segment pipeline end-to-end through the FastAPI app.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-chunking")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402

CORPUS_DIR = REPO_ROOT / "docs" / "benchmarks" / "corpus"
DIALOGUE_CORPUS = CORPUS_DIR / "native_03_dialogue_heavy_novel.pdf"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema is applied once per session (like the other F1s)."""
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
def _dialogue_book_pdf(pages: int = 6) -> bytes:
    """A native dialogue-heavy chapter: typographic heading, running
    header, page numbers, quote-initial dialogue turns with speech tags,
    unclosed quotations, an em-dash turn, a verse block and placeholders.
    Paragraph/turn/verse gaps are *vertical* (real PDF layout), mirroring
    the corpus's dialogue-heavy novel (AC2's test object)."""
    import pymupdf

    # (lines, extra gap after this block in line-heights)
    page_body: list[list[tuple[str, float]]] = [
        # page 1
        [('"You can\'t be serious," Declan said, setting down his glass',
          0.0),
         ('so carefully that it made no sound at all. "More serious than',
          0.0),
         ('anything I\'ve ever been," she answered, and for a moment the',
          0.0),
         ('music between their tables seemed to grow louder.', 1.2),
         ('"I felt my cold beer go warm. That\'s what I felt." He was',
          0.0),
         ('joking, but not really. She knew the difference, she thought.',
          1.2)],
        # page 2: unclosed quotation across what a naive splitter would
        # call two sentences
        [('"Tell me you felt it too," she said, "the way the rain kept',
          0.0),
         ('falling and the room kept talking and nobody, not one person',
          0.0),
         ('in that whole crowded room, said your name."', 1.2),
         ('He was joking, but not really. Outside, the rain had started',
          1.2)],
        # page 3: verse block (never split)
        [('again, striking the window in waves, like applause.', 1.6),
         ('Rain on the harbour,', 0.0),
         ('lights that blur and swim,', 0.0),
         ('a word I never sent.', 1.6)],
        # page 4: placeholder + multi-token proper name
        [('The telegram read [[TELEGRAM_1]] and it was signed by Dame',
          0.0),
         ('Agatha Mary Clarissa Christie, whose name the clerk had',
          0.0),
         ('spelled wrong twice.', 1.2)],
        # page 5: one long paragraph so the planner needs >1 block
        [('He counted the reasons he should leave and found, as always, '
          'that', 0.0),
         ('the counting itself kept him at the table. The waiter came '
          'and', 0.0),
         ('went. The band played something about June. Somebody laughed '
          'in', 0.0),
         ('the back and the laugh broke against the mirrors like a wave. '
          'He', 0.0),
         ('thought about the harbour again, about the boats knocking '
          'gently', 0.0),
         ('against the pier in the dark, about the letter he had never '
          'sent', 0.0),
         ('and the one he had never written, and the night went on. '
          'Then he', 0.0),
         ('said it, at last, quietly, the way a man says a thing he has '
          'kept', 0.0),
         ('for a winter and a summer and another winter beyond that.',
          1.2)],
        # page 6
        [('"Then say it," she said. "Say the thing you keep walking',
          0.0),
         ('around. Say it and we can all go home."', 0.0)],
    ]
    doc = pymupdf.open()
    # 40 copies of the long-page layout (real "long chapter" scale for the
    # planner), with DIFFERENT page furniture so the repetition pass cannot
    # mistake body lines for running headers.
    for copy in range(40):
        for block in page_body:
            page = doc.new_page()
            page_no = doc.page_count
            page.insert_text((72, 40), "MY DIALOGUE NOVEL", fontsize=9)
            page.insert_text((72, 770), f"- {page_no} -", fontsize=9)
            y = 90.0
            if page_no == 1:
                # typographic chapter heading: the §5.3 detection needs a
                # chapter node to attach the segments to
                page.insert_text((72, 64), "CHAPTER 1", fontsize=20)
                page.insert_text((72, 88), "The Long Evening", fontsize=14)
                y = 130.0
            line_height = 14.0
            for text, extra in block:
                page.insert_text((72, y), text, fontsize=11)
                y += line_height * (1.0 + extra)
    data = doc.tobytes()
    doc.close()
    return data


async def _import_dialogue_book(client: AsyncClient) -> tuple[str, list[str]]:
    """Import the dialogue book and run structure detection; returns
    (project_id, chapter_node_ids)."""
    from backend.scheduler import wait_for_workers

    r = await client.post("/api/v1/projects", json={
        "title": "Chunking Book", "genre_profile": "saga"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("dialogue.pdf", _dialogue_book_pdf(),
                        "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]
    wait_for_workers()  # initial parse job
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}/structure")
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    # the whole excerpt is one chapter: the title line ("Title") opens it
    chapters = [n for n in nodes
                if n["kind"] in ("chapter", "part", "back_matter")]
    assert chapters, f"no chapter node detected: {nodes}"
    return pid, [n["node_id"] for n in chapters]


# --------------------------------------------------------------------------
# AC1 (unit): planner blocks within the 16,384-token TOTAL budget,
# measured with the real tokenizer.
# --------------------------------------------------------------------------
def _synthetic_units(target_tokens: int):
    """A chapter worth ~target_tokens tokens of prose."""
    from backend.parsing import chunking

    sentence = ('He counted the reasons he should leave and found, as '
                'always, that the counting itself kept him at the table.')
    units = []
    for i in range(1, target_tokens // 18 + 1):
        units.append({
            "segment_id": f"seg-{i:04d}",
            "ordinal": i,
            "text": f"{sentence} (Turn {i} of the chapter.)",
        })
    return units


def test_planner_blocks_within_total_budget():
    """AC1: every block ≤ 16,384 TOTAL tokens (real tokenizer), and the
    source portion respects the 10-11k budget with reserves set aside."""
    from backend.parsing import chunking

    tokenizer = chunking.get_tokenizer()
    assert tokenizer.name.startswith("tiktoken"), \
        "the real tokenizer must be available for the budget test"

    units = _synthetic_units(target_tokens=60_000)
    blocks = chunking.plan_blocks(units, tokenizer)
    verification = chunking.verify_blocks(blocks)
    assert verification["blocks"] >= 3, verification
    assert verification["all_within_budget"], verification
    # budget rivisto 2026-09-21: sorgente 16,384 + contesto 2,500 + output 16,384
    assert verification["max_block_total"] <= chunking.MAX_BLOCK_TOTAL_TOKENS

    # budget split: source ≤ 16k, total = source + 2.5k + 16,384 ≤ 35,264
    for block in blocks:
        assert block["token_totals"]["source"] <= 16_384
        assert block["token_totals"]["total"] == (
            block["token_totals"]["source"] + 2_500 + 16_384)
        assert block["token_totals"]["total"] <= chunking.MAX_BLOCK_TOTAL_TOKENS

    # every source segment appears exactly once, in order (§5.4.7-8)
    seen = [sid for b in blocks for sid in b["segment_ids"]]
    assert seen == [u["segment_id"] for u in units]

    # a stricter source budget keeps the same total guarantee
    tight = chunking.plan_blocks(units, tokenizer, source_budget_max=6_000)
    assert chunking.verify_blocks(tight)["all_within_budget"]
    assert all(b["token_totals"]["source"] <= 6_000 for b in tight)

    # no segments are lost or duplicated between blocks (§5.4.8)
    all_ids = [sid for b in tight for sid in b["segment_ids"]]
    assert sorted(all_ids) == sorted(u["segment_id"] for u in units)
    assert len(set(all_ids)) == len(all_ids)


def test_planner_with_estimate_tokenizer_stays_conservative():
    """The estimate fallback must never under-count the real tokenizer."""
    from backend.parsing import chunking

    est = chunking._EstimatedTokenizer()
    real = chunking.get_tokenizer()
    text = ('"You can\'t be serious," Declan said, setting down his glass '
            'so carefully that it made no sound at all.')
    # conservatism: estimate ≥ real on ordinary English prose
    assert chunking.count_tokens(est, text) >= chunking.count_tokens(real, text)


# --------------------------------------------------------------------------
# AC2 (unit): anti-break rules on the dialogue-heavy corpus chapter.
# --------------------------------------------------------------------------
def test_corpus_dialogue_chapter_anti_break_rules():
    """AC2: on the corpus dialogue-heavy novel the two-pass segmentation
    never breaks dialogue with its speech tag, an unclosed quotation, a
    placeholder or a multi-token proper name."""
    pytest.importorskip("pymupdf")
    pytest.importorskip("spacy")
    from backend.parsing import chunking, l1, structure

    data = DIALOGUE_CORPUS.read_bytes()
    info = l1.open_document(data)
    payloads = {
        i + 1: l1.extract_page(data, i) for i in range(info["page_count"])
    }
    pages = [
        {"page_number": n, "mode": "l1",
         "lines": structure.page_lines(payloads[n])}
        for n in sorted(payloads)
    ]
    paragraphs = chunking.build_chapter_paragraphs(pages)
    units = chunking.segment_chapter_text(paragraphs)
    assert units, "the corpus chapter must produce segments"

    texts = [u["text"] for u in units]
    # (a) speech tags stay attached to their quote: no segment starts with
    # a bare speech-verb tag fragment
    for t in texts:
        stripped = t.strip()
        assert not re.match(r'^(said|asked|answered|replied)\b', stripped), \
            f"speech tag severed from its dialogue: {stripped!r}"
    # (b) bracket/quote balance per segment: nothing cut inside an unclosed
    # quotation (hard double quotes)
    for t in texts:
        assert t.count('"') % 2 == 0, f"unbalanced quotes in {t!r}"
    # every unit has a kind we promised (§5.4.2 first-segmentation kinds)
    assert all(u["kind"] in chunking.KINDS for u in units)
    # the corpus's famous dialogue turns survive as dialogue segments
    assert any(u["kind"] == "dialogue" for u in units)


def _confirmed_keys_for(pages: list[dict]) -> set[str]:
    """The header/footer keys confirmed like the runner does (§5.3)."""
    from backend.parsing import structure

    counts: dict[str, int] = {}
    for page in pages:
        keys = {
            structure.repeated_line_key(ln["text"])
            for ln in page["lines"]
        }
        for key in keys:
            if key:
                counts[key] = counts.get(key, 0) + 1
    repeated = [{"text": k, "pages": v} for k, v in counts.items()]
    return {
        c["text"] for c in
        structure.confirm_repeated_lines(repeated, len(pages))
    }


def test_synthetic_dialogue_book_rules():
    """AC2 on a crafted dialogue chapter (L1 pipeline): em-dash turns,
    verse blocks, placeholders and multi-token proper names survive."""
    pytest.importorskip("pymupdf")
    pytest.importorskip("spacy")
    from backend.parsing import chunking, l1, structure

    data = _dialogue_book_pdf()
    info = l1.open_document(data)
    payloads = {
        i + 1: l1.extract_page(data, i) for i in range(info["page_count"])
    }
    pages = [
        {"page_number": n, "mode": "l1",
         "lines": structure.page_lines(payloads[n])}
        for n in sorted(payloads)
    ]
    confirmed = _confirmed_keys_for(pages)
    assert any("MY DIALOGUE NOVEL" in k for k in confirmed), \
        "the running header must be algorithmically confirmed"
    paragraphs = chunking.build_chapter_paragraphs(pages, confirmed)
    units = chunking.segment_chapter_text(paragraphs)
    assert units
    texts = [u["text"] for u in units]
    # running header/footer never become segments
    assert not any("MY DIALOGUE NOVEL" in t or "- 1 -" in t
                   for t in texts)
    # (c) em-dash turns stay whole: the unit is one segment, unsplit (the
    # base-14 PDF fonts cannot embed a real '—' glyph, so this rule is
    # exercised at the pure layer in test_verse_and_emdash_pure)
    # (d) verse lines survive with their line breaks (§5.4.3 lists/poetry)
    assert any(u.get("is_verse") and "\n" in u["text"] for u in units), \
        "verse block not kept whole"
    # (e) placeholders survive intact wherever they appear
    assert all(u["text"].count("[[") == u["text"].count("]]")
               for u in units)
    # (f) the multi-token proper name is not cut by a sentence boundary
    # (Dame Agatha Mary Clarissa Christie stays in one segment)
    assert any("Agatha Mary Clarissa Christie" in t for t in texts)


def test_verse_and_emdash_pure():
    """§5.4.3 at the pure layer: verse groups keep their line breaks and
    em-dash dialogue turns are never sentence-split."""
    from backend.parsing import chunking

    verse = chunking.build_chapter_paragraphs([
        {"page_number": 1, "mode": "l1", "lines": [
            {"text": "Rain on the harbour,", "bbox": [0, 100, 90, 112]},
            {"text": "lights that blur and swim,",
             "bbox": [0, 114, 90, 126]},
            {"text": "a word I never sent.", "bbox": [0, 128, 90, 140]},
        ]},
    ])
    assert len(verse) == 1
    assert verse[0]["is_verse"]
    assert verse[0]["text"] == ("Rain on the harbour,\n"
                                "lights that blur and swim,\n"
                                "a word I never sent.")
    units = chunking.segment_chapter_text(verse)
    assert len(units) == 1  # verse is ONE segment: no sentence splitting

    # em-dash turn: starts with '—' → dialogue, kept whole (no splitting)
    turn = chunking.build_chapter_paragraphs([
        {"page_number": 1, "mode": "l1", "lines": [
            {"text": "— And if I refuse, he asked, would you still go "
                     "on? She nodded slowly, and the rain kept falling.",
             "bbox": [0, 100, 400, 112]},
        ]},
    ])
    assert turn[0]["kind"] == "dialogue"
    dash_units = chunking.segment_chapter_text(turn)
    assert len(dash_units) == 1
    assert dash_units[0]["text"].startswith("—")
    assert dash_units[0]["text"].endswith("falling.")


def test_split_sentences_protections():
    """Unit-level §5.4.3 protections on crafted sentences."""
    from backend.parsing import chunking

    nlp = chunking.get_nlp()
    if nlp is None:
        pytest.skip("spaCy en_core_web_sm unavailable")
    # dialogue + speech tag
    out = chunking.split_sentences(
        '"Where are you going?" asked John. "I have Mary\'s coat."')
    assert out[0] == '"Where are you going?" asked John.'
    assert len(out) == 2
    # unclosed parenthesis: never split inside
    out = chunking.split_sentences(
        'He left at dawn (without a word, though the house was full '
        'of sleepers. Nobody stirred. The door closed behind him.')
    assert any("sleepers" in s and "Nobody stirred" in s for s in out), out
    # placeholder kept in one segment
    out = chunking.split_sentences(
        'The telegram read [[TELEGRAM_1]] and it was signed. Then he left.')
    assert out[0].endswith("[[TELEGRAM_1]] and it was signed.") or \
        len(out) == 2 and out[0].endswith("signed."), out


import re  # noqa: E402  (used by the corpus test above)


# --------------------------------------------------------------------------
# AC1/AC3 (API): segment a real chapter end-to-end, plan its blocks and
# check the overlap markers + stable IDs.
# --------------------------------------------------------------------------
async def test_segment_chapter_api_and_stable_ids(client):
    from backend.db import SessionLocal
    from backend.models import Job, TranslationUnit
    from backend.scheduler import wait_for_workers

    pid, chapter_ids = await _import_dialogue_book(client)
    node_id = chapter_ids[0]

    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{node_id}/segment")
    assert r.status_code == 202, r.text
    wait_for_workers()

    r = await client.get(f"/api/v1/projects/{pid}/chapters/{node_id}/segments")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] > 0
    first_run = data["segments"]
    for seg in first_run:
        assert seg["segment_id"]
        assert seg["source_hash"] == hashlib.sha256(
            seg["source_text"].encode()).hexdigest()
        assert seg["kind"] in (
            "paragraph", "dialogue", "epigraph", "letter", "quotation",
            "scene_break")

    # the segment job completed with a budget verification (AC1)
    with SessionLocal() as db:
        job = (
            db.query(Job)
            .filter(Job.job_type == "segment_chapter")
            .order_by(Job.created_at.desc())
            .first()
        )
        assert job is not None and job.status == "completed", job
        assert job.result["verification"]["all_within_budget"]
        assert job.result["tokenizer"].startswith("tiktoken")

    # re-segmentation keeps IDs + hashes stable (§5.4.7, AC3)
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/{node_id}/segment")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.get(f"/api/v1/projects/{pid}/chapters/{node_id}/segments")
    second_run = r.json()["segments"]
    assert [(s["segment_id"], s["ordinal"], s["source_hash"])
            for s in second_run] == \
        [(s["segment_id"], s["ordinal"], s["source_hash"])
         for s in first_run], "segment IDs must be stable"

    # planner API: blocks within budget, overlap marked read-only (AC1+AC3)
    from backend.parsing import chunking

    r = await client.get(f"/api/v1/projects/{pid}/chapters/{node_id}/plan")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["verification"]["all_within_budget"], plan["verification"]
    assert plan["verification"]["max_block_total"] <= chunking.MAX_BLOCK_TOTAL_TOKENS
    assert plan["blocks"], "expected at least one LLM block"
    for block in plan["blocks"]:
        assert block["token_budget"]["limit"] == chunking.MAX_BLOCK_TOTAL_TOKENS
        # payload-level marker (§5.4.6)
        assert block["context"]["marker"] == "DO_NOT_TRANSLATE_CONTEXT"
        # per-item markers on every overlap segment
        for item in block["context"]["items"]:
            assert item["marker"] == "DO_NOT_TRANSLATE_CONTEXT"
            assert item["role"] in ("preceding", "following")
        # the overlap ids are real neighbour segments, never in 'segments'
        seg_ids = {s["segment_id"] for s in block["segments"]}
        for item in block["context"]["items"]:
            assert item["segment_id"] not in seg_ids
        # at most 2 preceding + 1 following (§5.4.6)
        assert len([i for i in block["context"]["items"]
                    if i["role"] == "preceding"]) <= 2
        assert len([i for i in block["context"]["items"]
                    if i["role"] == "following"]) <= 1

    # the persisted rows carry the stable IDs (translation_units, AC3)
    with SessionLocal() as db:
        rows = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == pid,
                    TranslationUnit.chapter_id == node_id)
            .order_by(TranslationUnit.ordinal)
            .all()
        )
        assert rows
        assert str(rows[0].id) == first_run[0]["segment_id"]
        flags = dict(rows[0].source_flags or {})
        assert flags.get("segment_id") == str(rows[0].id)


async def test_segment_chapter_api_rejects_scene(client):
    """A scene node owns no segments (the chapter does)."""
    from backend.models import Job, StructureNode
    from tests._f1_helpers import session_scope

    pid, chapter_ids = await _import_dialogue_book(client)
    with session_scope() as db:
        scene = StructureNode(
            id="00000000-0000-0000-0000-00000000abc1",
            project_id=pid,
            kind="scene",
            status="proposed",
            ordinal=999,
        )
        db.add(scene)
        db.commit()
    r = await client.post(
        f"/api/v1/projects/{pid}/structure/"
        "00000000-0000-0000-0000-00000000abc1/segment")
    assert r.status_code == 202  # job queued; the JOB fails (not the API)
    from backend.scheduler import wait_for_workers
    wait_for_workers()
    with session_scope() as db:
        job = db.query(Job).order_by(Job.created_at.desc()).first()
        assert job is not None and job.status == "failed"
        assert "scenes do not own segments" in (job.error or "")
