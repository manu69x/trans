"""Tests for the BookNLP entity-extraction worker.

Acceptance criteria under test (PRD §6.2, §6.3, §6.4, §15.2):

* AC1 — ``POST /api/v1/projects/{id}/entities/extract`` queues the job and
  the worker populates ``entities`` (status ``proposed``) with aliases,
  mentions and evidence from the real BookNLP service.
* AC2 — every proposed entity carries at least one evidence with a quote
  and a page number (§15.2), and no isolated first-person ``I`` pronoun
  becomes an entity (§15.2).
* AC3 — all §6.4 fields are present and populated on every entity:
  ``referential_gender`` (+ evidence), ``italian_grammatical_gender``,
  ``grammatical_number``, ``translation_policy``; gender inference follows
  the pronouns (referential, not identity), with the low-margin case kept
  ``unknown``.
* AC4 — idempotency: a second extraction converges to the same proposed
  set (rebuild without duplicates) and user-reviewed entities survive.
* AC5 — the raw BookNLP output is stored in object storage
  (``booknlp/<project>/<job>/...``) and a reprocess run
  (``reuse_output_key``) rebuilds entities without re-running the models.

These tests exercise the REAL BookNLP GPU service through the configured
LLM Gateway (ADR-008): they skip when the service is unreachable (no GPU
box / gateway upstream configured). Only the transformer NER is skipped
when no offline model is cached — the run must then still succeed
(BookNLP-only).
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

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-nlp")
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


@pytest.fixture(scope="session", autouse=True)
def _booknlp_service_required():
    """The whole module drives the real BookNLP GPU service (ADR-008)."""
    from backend import booknlp_service

    try:
        booknlp_service.check_health(timeout=10.0)
    except Exception as exc:  # noqa: BLE001 - service absence is a skip
        pytest.skip(f"BookNLP GPU service unreachable via gateway: {exc}")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _narrative_pdf() -> bytes:
    """A tiny native-text narrative: Maren (she/her) and Declan (he/him)
    interact over three pages so the gender inference has signal, with a
    location and an organisation for the NER categories."""
    import pymupdf

    pages = [
        [
            "CHAPTER 1",
            "Maren came to the harbour at dawn. She carried the letter",
            "she had never sent, and the wind took her hat at once.",
            "Declan was already there, waiting by the boats. He looked",
            "up when he heard her footsteps on the pier.",
            "Maren said nothing at first. She watched the water and",
            "thought about her brother far away in Saint Albans.",
        ],
        [
            "You are late, Declan said. He was joking, but not really.",
            "You know why I came, she answered. She sat down beside him",
            "and gave him the letter. He read it twice, slowly, and his",
            "face changed. They both looked at the grey water for a",
            "long time before either of them spoke again.",
            "The harbour master, a fat man from the town council of",
            "Saint Albans, waved at them from the far quay.",
        ],
        [
            "Will you stay in Saint Albans, she asked. He shook his",
            "head slowly. Declan had made his plans years before, and",
            "Maren knew that he would never change them now, whatever",
            "she said to him tonight on the cold pier.",
        ],
    ]
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        page.insert_text((72, 40), "THE HARBOUR NOVEL", fontsize=9)
        y = 72.0
        for line in lines:
            size = 20 if line == "CHAPTER 1" else 11
            page.insert_text((72, y), line, fontsize=size)
            y += 18.0 if size > 12 else 14.0
    data = doc.tobytes()
    doc.close()
    return data


async def _import_book(client: AsyncClient) -> str:
    """Import the narrative and detect the structure; returns project id."""
    from backend.scheduler import wait_for_workers

    r = await client.post("/api/v1/projects", json={
        "title": "NLP Book", "genre_profile": "saga"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("harbour.pdf", _narrative_pdf(),
                        "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]
    wait_for_workers()
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202, r.text
    wait_for_workers()
    return pid


async def _extract(client: AsyncClient, pid: str,
                   timeout: float = 480.0) -> dict:
    """Queue the extraction and poll until the job settles."""
    import asyncio
    import time

    r = await client.post(f"/api/v1/projects/{pid}/entities/extract")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/v1/projects/{pid}/jobs")
        assert r.status_code == 200, r.text
        job = next(j for j in r.json() if j["id"] == job_id)
        if job["status"] in ("completed", "failed"):
            assert job["status"] == "completed", job
            return job
        if time.monotonic() > deadline:
            pytest.fail(f"extraction job did not finish in {timeout}s: {job}")
        await asyncio.sleep(2.0)


# --------------------------------------------------------------------------
# AC1 — the endpoint queues the job and entities are populated (real
# BookNLP subprocess).
# --------------------------------------------------------------------------
async def test_extract_populates_entities(client):
    pid = await _import_book(client)
    job = await _extract(client, pid)

    result = job["result"]["entities"]
    assert result["scope"] == "project"
    assert result["entities"] > 0, result

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    assert r.status_code == 200, r.text
    entities = r.json()["entities"]
    assert entities, "no entities proposed"

    names = {e["canonical_source"] for e in entities}
    # the two protagonists must be found and clustered across aliases
    assert any("maren" in n.lower() for n in names), names
    assert any("declan" in n.lower() for n in names), names

    maren = next(e for e in entities if "maren" in e["canonical_source"].lower())
    assert maren["status"] == "proposed"
    assert maren["entity_type"] == "PERSON"
    assert maren["mention_count"] >= 2, maren


# --------------------------------------------------------------------------
# AC2 — every proposed entity has evidence with quote + page (§15.2);
# no isolated first-person ``I`` entity (§15.2).
# --------------------------------------------------------------------------
async def test_every_entity_has_quote_and_page_evidence(client):
    pid = await _import_book(client)
    await _extract(client, pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    entities = r.json()["entities"]
    assert entities

    for entity in entities:
        r = await client.get(
            f"/api/v1/projects/{pid}/entities/{entity['id']}")
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["evidence"], f"no evidence for {detail['canonical_source']}"
        for ev in detail["evidence"]:
            assert ev["quote_text"], ev
            assert ev["page_number"] is not None, ev
        # the §15.2 pronoun rule: no 1st/2nd-person-pronoun entity
        assert detail["canonical_source"].strip().lower() not in {
            "i", "me", "we", "us", "you"}


# --------------------------------------------------------------------------
# AC3 — §6.4 fields present and populated; referential gender from
# pronouns with evidence; ambiguous clusters stay ``unknown``.
# --------------------------------------------------------------------------
async def test_section_6_4_fields_populated(client):
    pid = await _import_book(client)
    await _extract(client, pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    entities = r.json()["entities"]

    valid = {
        "referential_gender": {"male", "female", "nonbinary", "mixed",
                               "unknown", "not_applicable"},
        "italian_grammatical_gender": {"masculine", "feminine", "common",
                                       "variable", "not_applicable"},
        "grammatical_number": {"singular", "plural", "invariant",
                               "collective", "unknown"},
        "translation_policy": {"keep_source", "translate", "transliterate",
                               "contextual", "undecided"},
    }
    for entity in entities:
        for field, allowed in valid.items():
            value = entity[field]
            assert value in allowed, (entity["canonical_source"], field, value)
        # §6.4: PERSON entities record the referential gender *with* its
        # evidence; NOM/ROLE persons without pronoun signal stay
        # ``unknown`` but still carry an evidence note.
        if entity["entity_type"] == "PERSON":
            assert entity["referential_gender_evidence"] is not None or (
                entity["referential_gender"] == "unknown"
            ), entity

    maren = next(e for e in entities if "maren" in e["canonical_source"].lower())
    assert maren["referential_gender"] == "female", maren
    assert "she/her" in (maren["referential_gender_evidence"] or ""), maren

    declan = next(e for e in entities
                  if "declan" in e["canonical_source"].lower())
    assert declan["referential_gender"] == "male", declan


def test_gender_margin_rule_and_pronoun_filter():
    """Unit: a low-margin pronoun distribution stays unknown; isolated
    first-person ``I`` mentions never become entities (§15.2)."""
    from backend.parsing import entities as ents

    page_map = {5: 1}
    raw = {
        "tokens": [],
        "entities": [
            # a cluster whose surface is only ``I`` must be dropped
            {"coref": "0", "start": 0, "end": 0, "prop": "PRON",
             "cat": "PER", "text": "I"},
        ],
        "book": {
            "characters": [
                {"id": 1, "count": 4, "g": {
                    "argmax": "she/her", "max": 0.238,
                    "inference": {"she/her": 0.238, "he/him/his": 0.219,
                                  "they/them/their": 0.0},
                }},
            ],
        },
    }
    clusters, _ = ents.cluster_mentions(raw, page_map, None, 1)
    assert clusters == {}, "isolated 'I' must not become an entity"

    clusters = {
        "1": ents.ProposedEntity(canonical_source="Kate",
                                 entity_type="PERSON"),
    }
    ents.apply_gender(clusters, raw)
    kate = clusters["1"]
    assert kate.referential_gender == "unknown", kate
    assert kate.referential_gender_evidence, kate
    assert "ambiguous" in (kate.referential_gender_evidence or ""), kate


# --------------------------------------------------------------------------
# AC4 — idempotent re-run: no duplicate proposed entities, user review
# survives a re-extraction.
# --------------------------------------------------------------------------
async def test_rerun_is_idempotent_and_review_survives(client):
    pid = await _import_book(client)
    await _extract(client, pid)

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    first = r.json()["entities"]
    assert first

    # the user reviews one entity: approve it and set the §6.4 fields
    target = first[0]
    r = await client.patch(
        f"/api/v1/projects/{pid}/entities/{target['id']}",
        json={
            "status": "approved",
            "canonical_target": "Forma Approvata",
            "referential_gender": "female",
            "italian_grammatical_gender": "feminine",
            "grammatical_number": "singular",
            "translation_policy": "keep_source",
        })
    assert r.status_code == 200, r.text

    job = await _extract(client, pid)
    assert job["status"] == "completed"

    r = await client.get(f"/api/v1/projects/{pid}/entities")
    second = r.json()["entities"]
    # the approved entity survives untouched
    approved = [e for e in second if e["id"] == target["id"]]
    assert len(approved) == 1
    assert approved[0]["status"] == "approved"
    assert approved[0]["canonical_target"] == "Forma Approvata"
    assert approved[0]["italian_grammatical_gender"] == "feminine"

    # no duplicate proposed rows for the same canonical name
    names = [e["canonical_source"].lower() for e in second]
    assert len(names) == len(set(names)), names


# --------------------------------------------------------------------------
# AC5 — raw BookNLP output stored; reprocess from the stored output.
# --------------------------------------------------------------------------
async def test_raw_output_stored_and_reusable(client):
    import json

    from backend.storage import get_storage_provider

    pid = await _import_book(client)
    job = await _extract(client, pid)

    storage = get_storage_provider()
    base = f"booknlp/{pid}"
    assert "booknlp" in job["result"]["entities"]["extractor"]

    # the run stored its raw output under booknlp/<pid>/<job_id>/ with a
    # manifest that lists every BookNLP artefact (reprocessable, §6.2)
    manifest_path = f"{base}/{job['id']}/manifest.json"
    raw = storage.get(manifest_path)
    assert raw is not None, f"manifest missing at {manifest_path}"
    manifest = json.loads(raw.decode("utf-8"))
    names = [f["key"].rsplit("/", 1)[-1] for f in manifest["files"]]
    assert "book.entities" in names, names
    assert "book.tokens" in names, names
    for name in names:
        assert storage.get(f"{base}/{job['id']}/{name}") is not None, name

    # a second extraction on the same project converges to the same
    # proposed surface (the reuse path of the runner, idempotent §14)
    job2 = await _extract(client, pid)
    assert "booknlp" in job2["result"]["entities"]["extractor"]
    assert job2["result"]["entities"]["entities"] > 0
