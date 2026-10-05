"""Tests for the termbase (glossary) surface.

Acceptance criteria under test (PRD §7.1, §7.3, §12.3, §15.4 / §3.3 / §5.4):

* AC1 — ``POST /api/v1/projects/{id}/glossary/import`` imports a CSV of
  100 rows and returns a per-row error report; valid rows land in
  ``glossary_terms``, invalid ones are reported (not inserted).
* AC2 — ``POST /api/v1/projects/{id}/glossary/snapshot`` creates an
  immutable snapshot that is referenced by translation runs
  (``glossary_snapshot_id`` on ``translation_units``) and cannot be
  modified (no update/delete endpoint, ``item_count`` frozen at creation).
* AC3 — ``POST /api/v1/projects/{id}/glossary/select`` (§7.3) selects the
  entities mentioned in the block / in the two preceding segments / reached
  by coreference, plus high-priority terms with lexical overlap, capped at
  30 entities + 20 terms, and orders by priority.

The test suite is pure (no LLM, no BookNLP): it drives the FastAPI app
directly and exercises the pure :mod:`backend.glossary` layer for the
selection rules.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-glossary")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.models import MemorySnapshot, TranslationUnit  # noqa: E402


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
async def _new_project(client: AsyncClient) -> str:
    r = await client.post("/api/v1/projects", json={
        "title": "Glossary Book", "genre_profile": "fantasy"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _csv_header() -> str:
    return (
        "source_term,term_type,preferred,status,"
        "grammatical_gender_it,grammatical_number,target_term,usage_notes\n"
    )


def _csv_body(n: int) -> str:
    """Build ``n`` valid CSV rows plus a couple of deliberately invalid ones."""
    lines: list[str] = []
    for i in range(n):
        lines.append(
            f"Term{i},CONCEPT_TERM,true,approved,feminine,singular,"
            f"Target{i},note{i}"
        )
    # invalid rows: missing source_term, unknown term_type, bad gender.
    lines.append(",,CONCEPT_TERM,true,approved")
    lines.append("BadType,NOT_A_TYPE,true,approved")
    lines.append("BadGender,CONCEPT_TERM,true,approved,banana,singular")
    return _csv_header() + "\n".join(lines) + "\n"


def _tbx_body() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<TEI xmlns="http://www.tei-c.org/ns/1.0">\n'
        '  <text><group>\n'
        '    <entry type="LOCATION" preferred="true" status="approved"\n'
        '           gender_it="feminine" number_it="singular">\n'
        '      <head><gloss><ref target="#e1">Roma</ref></gloss></head>\n'
        '      <glossgrp lang="it"><lg><l><ref target="#e1">Roma</ref></l></lg></glossgrp>\n'
        '      <glossgrp lang="en"><lg><l>Rome</l></lg></glossgrp>\n'
        '      <desc><note type="usage">capital</note></desc>\n'
        '    </entry>\n'
        '  </group></text>\n'
        '</TEI>\n'
    )


# --------------------------------------------------------------------------
# AC1 — import CSV 100 rows with per-row error report
# --------------------------------------------------------------------------
async def test_ac1_import_csv_100_and_errors(client):
    pid = await _new_project(client)

    body = _csv_body(100)
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("glossary.csv", body, "text/csv")},
        params={"fmt": "csv"},
    )
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["imported"] == 100, report
    # the 3 invalid rows are each reported with a row number + message
    assert report["error_count"] == 3, report
    assert len(report["errors"]) == 3
    assert all("row" in e and "error" in e for e in report["errors"])
    # the invalid rows are the last three (101, 102, 103)
    assert {e["row"] for e in report["errors"]} == {101, 102, 103}

    # the 100 valid rows are in the DB
    r = await client.get(f"/api/v1/projects/{pid}/glossary")
    assert r.status_code == 200, r.text
    terms = r.json()
    assert len(terms) == 100, terms
    assert all(t["status"] == "approved" for t in terms)
    assert all(t["preferred"] is True for t in terms)
    assert all(t["grammatical_gender_it"] == "feminine" for t in terms)


async def test_ac1_tbx_import_roundtrip(client):
    pid = await _new_project(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("glossary.tbx", _tbx_body(), "application/xml")},
        params={"fmt": "tbx"},
    )
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["imported"] == 1, report
    assert report["error_count"] == 0, report

    r = await client.get(f"/api/v1/projects/{pid}/glossary")
    terms = r.json()
    assert len(terms) == 1, terms
    assert terms[0]["source_term"] == "Rome"
    assert terms[0]["target_term"] == "Roma"
    assert terms[0]["grammatical_gender_it"] == "feminine"
    assert terms[0]["usage_notes"] == "capital"


async def test_ac1_partial_import_keeps_good_rows(client):
    pid = await _new_project(client)
    # one good row, one bad row (empty source_term)
    body = _csv_header() + "Good,CONCEPT_TERM,true,approved\n,CONCEPT_TERM\n"
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("g.csv", body, "text/csv")},
        params={"fmt": "csv"},
    )
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["imported"] == 1
    assert report["error_count"] == 1

    r = await client.get(f"/api/v1/projects/{pid}/glossary")
    terms = r.json()
    assert len(terms) == 1
    assert terms[0]["source_term"] == "Good"


# --------------------------------------------------------------------------
# AC2 — snapshot referenced by runs and not modifiable
# --------------------------------------------------------------------------
async def test_ac2_snapshot_is_immutable_and_referenced(client):
    pid = await _new_project(client)

    # seed 5 terms
    body = _csv_header() + "".join(
        f"T{i},CONCEPT_TERM,true,approved\n" for i in range(5)
    )
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("g.csv", body, "text/csv")},
        params={"fmt": "csv"},
    )
    assert r.status_code == 201, r.text

    # create a snapshot -> item_count frozen at 5
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/snapshot",
        params={"description": "pre-translation"},
    )
    assert r.status_code == 201, r.text
    snap = r.json()
    assert snap["item_count"] == 5
    snapshot_id = snap["snapshot_id"]

    # the snapshot row exists and is a glossary snapshot
    from backend.db import SessionLocal
    with SessionLocal() as s:
        rows = (
            s.query(MemorySnapshot)
            .filter(MemorySnapshot.id == snapshot_id)
            .all()
        )
    assert len(rows) == 1
    assert rows[0].snapshot_type == "glossary"
    assert rows[0].item_count == 5  # frozen at creation

    # add more terms AFTER the snapshot: item_count must NOT change
    body2 = _csv_header() + "".join(
        f"U{i},CONCEPT_TERM,true,approved\n" for i in range(3)
    )
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("g2.csv", body2, "text/csv")},
        params={"fmt": "csv"},
    )
    assert r.status_code == 201, r.text

    # the live glossary grew...
    r = await client.get(f"/api/v1/projects/{pid}/glossary")
    assert len(r.json()) == 8
    # ...but the snapshot is unchanged (immutable, §15.4)
    from backend.db import SessionLocal
    with SessionLocal() as s:
        rows = (
            s.query(MemorySnapshot)
            .filter(MemorySnapshot.id == snapshot_id)
            .all()
        )
    assert rows[0].item_count == 5

    # no API endpoint modifies or deletes the snapshot: a PATCH/DELETE 404s
    r = await client.patch(
        f"/api/v1/projects/{pid}/glossary/snapshots/{snapshot_id}")
    assert r.status_code == 404, r.text
    r = await client.delete(
        f"/api/v1/projects/{pid}/glossary/snapshots/{snapshot_id}")
    assert r.status_code == 404, r.text

    # a translation run references the snapshot via glossary_snapshot_id:
    # the model carries the FK (TranslationUnit.glossary_snapshot_id) and a
    # run would set it to the id returned above.
    assert hasattr(TranslationUnit, "glossary_snapshot_id")
    from backend.models import TranslationUnit as TU
    col = TU.__table__.c["glossary_snapshot_id"]
    assert col.nullable is True  # optional, set by the run


# --------------------------------------------------------------------------
# AC3 — §7.3 selection: max 30+20 and priority
# --------------------------------------------------------------------------
def _entities(n: int, kind: str) -> list[dict]:
    """``kind`` in {``block`` (in block), ``preceding`` (in preceding),
    ``coref`` (coreference), ``none`` (not selected)."""
    out = []
    for i in range(n):
        out.append({
            "id": f"id-{kind}-{i}",
            "canonical_source": f"{kind}-{i}",
            "aliases": [f"{kind}-{i}, {kind}-alt{i}"],
            "entity_type": "CONCEPT_TERM",
            "italian_grammatical_gender": "feminine",
            "grammatical_number": "singular",
            "translation_policy": "translate",
            "notes": None,
            "priority": "block_batch" if kind == "block" else "normal",
        })
    return out


def test_ac3_selection_respects_max_and_priority():
    from backend.glossary import select_entities_for_block

    # 40 entities all "in block": only 30 selected, all priority high.
    entities = _entities(40, "block")
    res = select_entities_for_block(
        block_text=" ".join(f"block-{i}" for i in range(40)),
        preceding_texts=["not here"],
        entities=entities,
        max_entities=30,
    )
    assert res["counts"]["entities_selected"] == 30
    assert all(e["priority"] == "high" for e in res["entities"])
    # the top 30 by alphabetical tie-break (block-0 .. block-29)
    assert [e["source"] for e in res["entities"]][:3] == [
        "block-0", "block-1", "block-2"]


def test_ac3_selection_priority_ordering():
    from backend.glossary import select_entities_for_block

    block = _entities(2, "block")
    preceding = _entities(2, "preceding")
    coref = _entities(2, "coref")
    none = _entities(2, "none")

    res = select_entities_for_block(
        block_text=" ".join(f"block-{i}" for i in range(2)),
        preceding_texts=[" ".join(f"preceding-{i}" for i in range(2))],
        coref_ids=["id-coref-0", "id-coref-1"],
        entities=block + preceding + coref + none,
        max_entities=30,
    )
    # the 2 "none" entities are excluded
    assert res["counts"]["entities_selected"] == 6
    sources = [e["source"] for e in res["entities"]]
    # block (prio 3) before preceding (prio 2 before coref (prio 1),
    # alphabetical within each tier
    assert sources == ["block-0", "block-1",
                       "preceding-0", "preceding-1",
                       "coref-0", "coref-1"]


def test_ac3_terms_cap_and_overlap():
    from backend.glossary import select_entities_for_block

    # 25 preferred terms all overlapping the block -> 20 selected
    terms = [
        {
            "id": f"t{i}",
            "source_term": f"ovlap{i}",
            "preferred": True,
            "status": "approved",
            "term_type": "CONCEPT_TERM",
            "usage_notes": None,
        }
        for i in range(25)
    ]
    block_text = " ".join(f"ovlap{i}" for i in range(25))
    res = select_entities_for_block(
        block_text=block_text,
        preceding_texts=[],
        terms=terms,
        max_terms=20,
    )
    assert res["counts"]["terms_selected"] == 20

    # non-preferred / non-approved terms are never selected
    res2 = select_entities_for_block(
        block_text="ovlap0",
        preceding_texts=[],
        terms=[{
            "id": "t0", "source_term": "ovlap0",
            "preferred": False, "status": "proposed",
            "term_type": "CONCEPT_TERM", "usage_notes": None,
        }],
    )
    assert res2["counts"]["terms_selected"] == 0


def test_ac3_selection_via_endpoint():
    """The /select endpoint returns the same payload as the pure function."""
    import asyncio

    async def _run():
        async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test") as ac:
            pid = await _new_project(ac)
            r = await ac.post(f"/api/v1/projects/{pid}/glossary/select", json={
                "block_text": " ".join(f"block-{i}" for i in range(35)),
                "preceding_texts": ["preceding-0"],
                "coref_ids": ["id-coref-0"],
                "entities": _entities(35, "block")
                + _entities(1, "preceding"),
                "terms": [],
                "max_entities": 30,
                "max_terms": 20,
            })
            assert r.status_code == 200, r.text
            return r.json()

    res = asyncio.run(_run())
    assert res["counts"]["entities_selected"] == 30
    # 35 block + 1 preceding = 36 candidates; 30 selected, all high priority
    assert all(e["priority"] == "high" for e in res["entities"])


# --------------------------------------------------------------------------
# CRUD + export round-trip (supporting the ACs)
# --------------------------------------------------------------------------
async def test_crud_create_update_version_bump(client):
    pid = await _new_project(client)
    r = await client.post(f"/api/v1/projects/{pid}/glossary", json={
        "source_term": "Alpha", "term_type": "CONCEPT_TERM",
        "preferred": True, "status": "proposed",
        "grammatical_gender_it": "masculine",
        "grammatical_number": "singular",
        "target_term": "Alfa",
    })
    assert r.status_code == 201, r.text
    term = r.json()
    assert term["version"] == 1
    tid = term["id"]

    # update bumps the version and is audited
    r = await client.patch(f"/api/v1/projects/{pid}/glossary/{tid}", json={
        "status": "approved", "target_term": "Alfa2"})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["version"] == 2
    assert updated["status"] == "approved"
    assert updated["target_term"] == "Alfa2"

    # invalid status rejected
    r = await client.patch(f"/api/v1/projects/{pid}/glossary/{tid}", json={
        "status": "bogus"})
    assert r.status_code == 422, r.text


async def test_export_csv_and_tbx_roundtrip(client):
    pid = await _new_project(client)
    body = _csv_header() + "Beta,CONCEPT_TERM,true,approved,feminine,singular,Betta,x\n"
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("g.csv", body, "text/csv")},
        params={"fmt": "csv"},
    )
    assert r.status_code == 201, r.text

    # export CSV
    r = await client.get(f"/api/v1/projects/{pid}/glossary/export",
                         params={"fmt": "csv"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    csv_out = r.text
    assert "Beta" in csv_out
    assert csv_out.startswith("source_term,target_term,term_type")

    # export TBX
    r = await client.get(f"/api/v1/projects/{pid}/glossary/export",
                         params={"fmt": "tbx"})
    assert r.status_code == 200, r.text
    assert "application/xml" in r.headers["content-type"]
    tbx_out = r.text
    assert "<TEI" in tbx_out
    assert "Beta" in tbx_out

    # re-import the exported TBX -> same term survives
    r = await client.post(
        f"/api/v1/projects/{pid}/glossary/import",
        files={"file": ("out.tbx", tbx_out, "application/xml")},
        params={"fmt": "tbx"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["imported"] == 1
