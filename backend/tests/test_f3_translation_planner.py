"""Tests for the F3 translation planner (PRD §5.4, §7.2-7.3, §9.3-9.4, §10.1).

Acceptance criteria under test:

* AC1 -- the planner assembles a prompt **within the 16K budget** (§10.1
  step 1 / §5.4), referencing immutable snapshots (``snapshot_ids``) and
  using a real tokenizer (tiktoken).
* AC2 -- TM retrieval applies the **mandatory project filter** and the
  **configurable threshold (§7.2**: only entries of the same project and
  score >= threshold are returned; exact > fuzzy > semantic ordering.
* AC3 -- a **conflict** between the style guide and the glossary is
  **exposed in the run metadata / UI (``plan.conflicts``), not decided in
  silence (§9.3).
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

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f3")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.models import MemorySnapshot, TranslationMemoryEntry  # noqa: E402
from backend.parsing.chunking import MAX_BLOCK_TOTAL_TOKENS  # noqa: E402


@pytest.fixture
def chapter_id() -> str:
    """A valid UUID chapter id (the schema requires UUID chapter_id)."""
    return str(uuid.uuid4())


@pytest.fixture
def chapter(chapter_id):
    """Alias: a real chapter id (UUID), as the schema requires."""
    return chapter_id


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


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def _new_project(client: AsyncClient, genre: str = "fantascientifica") -> str:
    r = await client.post("/api/v1/projects", json={"title": "F3 Book",
                                                    "genre_profile": genre})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_tm(client: AsyncClient, pid: str, src: str, tgt: str,
                  genre: str | None = None, pov: str | None = None,
                  chapter: str | None = None) -> str:
    r = await client.post(f"/api/v1/projects/{pid}/tm", json={
        "chapter_id": chapter,
        "source_normalized": src,
        "source_original": src,
        "target_approved": tgt,
        "genre_profile": genre,
        "pov": pov,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------
# AC1 -- prompt assembled within budget, referencing snapshots, real tokens
# --------------------------------------------------------------------------
async def test_ac1_prompt_within_budget_and_snapshots(client, chapter):
    pid = await _new_project(client)
    # seed a TM entry and an approved glossary term + entity
    await _add_tm(client, pid, "Good morning, John.", "Buongiorno, John.",
                  genre="fantascientifica", chapter=chapter)
    r = await client.post(f"/api/v1/projects/{pid}/glossary", json={
        "source_term": "John", "term_type": "PERSON", "preferred": True,
        "status": "approved", "grammatical_gender_it": "masculine",
        "grammatical_number": "singular", "target_term": "John"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.get(f"/api/v1/projects/{pid}/glossary/{tid}")
    _ = r.json()

    # create a TM snapshot (it must be referenced by the planner)
    r = await client.post(f"/api/v1/projects/{pid}/tm/snapshot")
    assert r.status_code == 201, r.text
    tm_snap = r.json()["snapshot_id"]
    r = await client.get(f"/api/v1/projects/{pid}/tm/snapshots")
    assert r.status_code == 200, r.text
    snaps = r.json()
    assert len(snaps) == 1
    assert snaps[0]["snapshot_type"] == "tm"
    assert snaps[0]["item_count"] == 1
    # the snapshot carries the frozen item ids (payload)
    assert snaps[0]["payload"]["tm_entry_ids"]

    seg = {"segment_id": "s1", "source_text": "Good morning, John."}
    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "chapter_id": chapter,
        "segments": [seg],
        "block_source": "Good morning, John.",
        "snapshot_ids": {"tm": tm_snap, "glossary": "snap-gloss"},
    })
    assert r.status_code == 200, r.text
    plan = r.json()

    # AC1: prompt assembled and within the block ceiling (35,264: 16k in + 16k out + 2.5k ctx)
    assert plan["budget"]["within_ceiling"] is True
    assert plan["budget"]["max_total"] == MAX_BLOCK_TOTAL_TOKENS
    assert plan["prompt"]  # non-empty
    # the prompt carries the snapshot id in the plan metadata (§16)
    assert plan["snapshot_ids"].get("tm") == tm_snap
    # fix 2026-09-18: the prompt embeds the REAL approved content, not an
    # unresolvable "[snapshot:...]" marker -- the model must see the terms
    assert "[snapshot:" not in plan["prompt"]
    assert "Buongiorno, John." in plan["prompt"]  # TM pair rendered
    assert "John" in plan["prompt"]  # approved glossary term rendered
    # the prompt is the fixed §9.4 skeleton
    assert "RUOLO" in plan["prompt"]
    assert "TESTO DA TRADURRE" in plan["prompt"]
    # the TM match is injected
    assert plan["tm_matches"][0]["target_approved"] == "Buongiorno, John."
    # the approved glossary term is injected
    assert any(g["source"] == "John" for g in plan["glossary_entries"])
    # the prompt hash is present (reproducibility, §16)
    assert plan["prompt_hash"]


async def test_ac1_budget_respects_ceiling(client):
    pid = await _new_project(client)
    # a huge block_source must still report within_ceiling=False
    # (>= 2x il tetto 35,264 per essere sicuri oltre la tolleranza)
    big = "The quick brown fox. " * 12000
    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "block_source": big,
        "segments": [{"segment_id": "s1", "source_text": big}],
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["budget"]["within_ceiling"] is False
    assert plan["budget"]["total_used"] <= MAX_BLOCK_TOTAL_TOKENS + 30_000  # source alone exceeds it


# --------------------------------------------------------------------------
# AC2 -- TM project filter + threshold
# --------------------------------------------------------------------------
async def test_ac2_tm_project_filter_and_threshold(client, chapter):
    p1 = await _new_project(client, "fantascientifica")
    p2 = await _new_project(client, "horror")

    # same project, exact match
    await _add_tm(client, p1, "Where is my sword?", "Dov'è la mia spada?",
                  genre="fantascientifica", chapter=chapter)
    # different project -- must be FILTERED OUT (§7.2 step 4, mandatory)
    await _add_tm(client, p2, "Where is my sword?", "Dov'è la mia spada?",
                  genre="horror")
    # same project but low score (below fuzzy threshold) -- excluded
    await _add_tm(client, p1, "Completely different phrase",
                  "Frase completamente diversa")

    r = await client.post(f"/api/v1/projects/{p1}/translation/planner", json={
        "chapter_id": chapter,
        "block_source": "Where is my sword?",
        "segments": [{"segment_id": "s1", "source_text": "Where is my sword?"}],
        "fuzzy_threshold": 0.5,
        "semantic_threshold": 0.6,
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    hits = plan["tm_matches"]
    assert len(hits) == 1, hits
    # the winning hit is from the SAME project and an EXACT match
    assert hits[0]["method"] == "exact"
    assert hits[0]["project_ok"] is True
    assert hits[0]["target_approved"] == "Dov'è la mia spada?"


async def test_ac2_fuzzy_and_semantic_ordering():
    import backend.translation.embedding as emb
    from backend.translation.tm_retrieval import retrieve_tm

    def row(src: str, tgt: str) -> dict:
        return {
            "project_id": "p",
            "source_normalized": src,
            "source_original": src,
            "target_approved": tgt,
            "source_embedding": emb.embed(src),
        }

    q = "Good morning there"
    hits = retrieve_tm(
        block_source=q,
        candidates=[
            row("Good morning", "Buongiorno"),          # exact-ish (subset)
            row("Good morning there", "Buongiorno"),     # exact
            row("Good morning, friend", "Buongiorno, amico"),  # semantic only
        ],
        project_id="p",
        fuzzy_threshold=0.5,
        semantic_threshold=0.6,
    )
    # exact first
    assert hits[0]["method"] == "exact"
    # all three pass the thresholds (all similar enough here)
    assert len(hits) == 3


# --------------------------------------------------------------------------
# AC3 -- conflict exposed, not decided in silence (§9.3)
# --------------------------------------------------------------------------
async def test_ac3_conflict_exposed(client, chapter):
    pid = await _new_project(client)
    # glossary says 'sword' -> 'spada'; style guide wants 'lama' -> conflict.
    r = await client.post(f"/api/v1/projects/{pid}/glossary", json={
        "source_term": "sword", "term_type": "OBJECT_ARTIFACT",
        "preferred": True, "status": "approved",
        "grammatical_gender_it": "feminine", "grammatical_number": "singular",
        "target_term": "spada",
        "forbidden_targets": ["lama"],
        "usage_notes": "the magic sword"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    # update the term so the style guide disagrees with the approved target
    r = await client.patch(f"/api/v1/projects/{pid}/glossary/{tid}", json={
        "target_term": "lama"})
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "chapter_id": chapter,
        "block_source": "Take the sword and fight.",
        "segments": [{"segment_id": "s1", "source_text": "Take the sword."}],
        "style_guide": "For the term 'sword' use 'lama', never 'spada'.",
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    # AC3: the conflict is EXPOSED in the run metadata
    assert len(plan["conflicts"]) >= 1, plan["conflicts"]
    kinds = {c["kind"] for c in plan["conflicts"]}
    assert "style_vs_forbidden" in kinds or "style_vs_target" in kinds
    # the conflict is surfaced with the term and both values
    assert any(c.get("term") == "sword" for c in plan["conflicts"])


async def test_ac3_no_conflict_when_no_style_guide(client, chapter):
    pid = await _new_project(client)
    r = await client.post(f"/api/v1/projects/{pid}/glossary", json={
        "source_term": "sword", "term_type": "OBJECT_ARTIFACT",
        "preferred": True, "status": "approved",
        "grammatical_gender_it": "feminine", "grammatical_number": "singular",
        "target_term": "spada"})
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "chapter_id": chapter,
        "block_source": "Take the sword.",
        "segments": [{"segment_id": "s1", "source_text": "Take the sword."}],
        "style_guide": None,
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    # no explicit style guide -> no conflicts
    assert plan["conflicts"] == []


# --------------------------------------------------------------------------
# TM snapshot immutability (§7.2 / §16), mirrored from glossary_router
# --------------------------------------------------------------------------
async def test_ac4_tm_snapshot_immutable(client):
    pid = await _new_project(client)
    await _add_tm(client, pid, "Hello there.", "Ciao.")
    r = await client.post(f"/api/v1/projects/{pid}/tm/snapshot")
    assert r.status_code == 201, r.text
    sid = r.json()["snapshot_id"]

    from backend.db import SessionLocal
    with SessionLocal() as s:
        row = s.get(MemorySnapshot, sid)
        assert row.snapshot_type == "tm"
        assert row.item_count == 1
        assert row.payload["tm_entry_ids"]

    # a later TM entry must NOT change the snapshot item_count (immutable)
    await _add_tm(client, pid, "Another phrase", "Altra frase")
    r = await client.get(f"/api/v1/projects/{pid}/tm/snapshots")
    snaps = r.json()
    assert snaps[0]["item_count"] == 1

    # no update/delete endpoint -> 404
    r = await client.patch(f"/api/v1/projects/{pid}/tm/snapshots/{sid}")
    assert r.status_code == 404, r.text
    r = await client.delete(f"/api/v1/projects/{pid}/tm/snapshots/{sid}")
    assert r.status_code == 404, r.text


async def test_ac4_rejected_tm_incompatible_context(client, chapter):
    pid = await _new_project(client, "fantascientifica")
    # a semantic hit whose genre disagrees with the block's genre.
    import backend.translation.embedding as emb
    from backend.db import SessionLocal
    from backend.models import TranslationMemoryEntry as TME
    from backend.models import Project as P

    with SessionLocal() as s:
        proj = s.get(P, pid)
        assert proj is not None
        e = TME(
            id=str(uuid.uuid4()),
            project_id=pid,
            source_normalized="A distant star",
            source_original="A distant star",
            target_approved="Una stella lontana",
            genre_profile="horror",  # DIFFERENT from the block genre
            pov=None,
            source_embedding=emb.embed("A distant star"),
        )
        s.add(e)
        s.commit()
    r = await client.post(f"/api/v1/projects/{pid}/translation/planner", json={
        "chapter_id": chapter,
        "block_source": "A distant star shines.",
        "segments": [{"segment_id": "s1", "source_text": "A distant star shines."}],
        "semantic_threshold": 0.4,  # low so it is a weak/semantic hit
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    # the incompatible weak hit is REJECTED and surfaced as a conflict
    assert any(c["kind"] == "tm_incompatible_context" for c in plan["conflicts"])
    # and it is NOT usable in the prompt
    assert all(m["target_approved"] != "Una stella lontana"
               for m in plan["tm_matches"])
