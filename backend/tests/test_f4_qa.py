"""QA suite tests (PRD §10.2 / §10.3 / §12.4 / §18.2 / AC1-3 / task F4).

* AC1 -- the **deterministic** suite runs over a chapter and produces
  reproducible issues (each failure becomes a ``qa_issues`` row with an MQM
  category / severity / evidence / suggestion);
* AC2 -- **reference-free QE** produces a per-segment ``quality_score`` with
  no human reference (§10.3);
* AC3 -- the **critic** returns structured, schema-validated errors without
  rewriting the target (§10.3 / §10.5).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import jsonschema
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

# Isolated test database, decided and exported BEFORE backend.main is
# imported below: backend.db builds its engine from DATABASE_URL at import
# time, so a setdefault inside a fixture would come too late (the app would
# silently talk to a different database than the one the fixtures seed).
# A dedicated ``trans_qa`` DB (mirrors the F1 ``trans_test`` pattern) so the
# session fixture can drop/recreate it and Alembic can apply the full
# migration chain from scratch without colliding with the shared ``trans``
# DB's schema.
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_qa",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f4-qa")
os.environ.setdefault("LOCAL_ONLY", "1")

def _ensure_test_db() -> None:
    """(Re)create ``trans_qa`` with the ``vector`` extension BEFORE the
    ``backend.main`` import below.

    ``backend.main.create_app()`` calls ``Base.metadata.create_all`` at import
    time, and ``tm_entries.source_embedding`` is a ``VECTOR(768)`` column, so
    the database must already exist *and* carry the pgvector extension by then
    -- this mirrors the F1 suite, whose ``trans_test`` DB is created ahead of
    the import. The session ``_migrate`` fixture then applies the full
    Alembic migration chain (incl. the 003 append-only audit trigger).
    """
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_qa' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_qa"))
            # TEMPLATE template0: the default template1 can carry a stale
            # collation version (an OS/ICU version drift breaks CREATE
            # DATABASE against it); template0 has the minimal empty schema
            # so a fresh, clean database is created regardless.
            conn.execute(text(
                "CREATE DATABASE trans_qa TEMPLATE template0"))
    finally:
        admin.dispose()
    # The pgvector extension (normally created by alembic's _ensure_vector)
    # is needed by ``Base.metadata.create_all`` at import time, so create it
    # in trans_qa up front.
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_qa",
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
    QaIssue,
    TranslationUnit,
)
from backend.qa.critic import CRITIC_SCHEMA, run_critic_deterministic  # noqa: E402
from backend.qa.deterministic import run_deterministic  # noqa: E402
from backend.qa.quality_estimation import (  # noqa: E402
    calibrate,
    estimate_segment_quality,
)


def _drop_and_recreate_test_db() -> None:
    """Recreate ``trans_qa`` (empty, with the ``vector`` extension)."""
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_qa' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_qa"))
            conn.execute(text(
                "CREATE DATABASE trans_qa TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_qa",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Recreate the isolated DB, then apply the full migration chain.

    The ``backend.main`` import already ran ``create_all`` once (so the import
    succeeds), so this drops the DB again and rebuilds it from the Alembic
    chain (migrations 001 → head, incl. the 003 append-only audit trigger) on
    an empty database -- the same drop→alembic pattern the F1 suite uses.
    """
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
    """Each test starts from an empty, well-defined DB."""
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


async def _new_project(client: AsyncClient, title: str = "F4 QA Book") -> str:
    r = await client.post("/api/v1/projects", json={"title": title,
                                                    "genre_profile": "saggio"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _unit(project_id: str, name: str, ordinal: int, source: str,
          target: str, status: str = "machine_draft") -> TranslationUnit:
    return TranslationUnit(
        id=_uuid(name),
        project_id=project_id,
        chapter_id=_uuid("ch1"),
        ordinal=ordinal,
        source_text=source,
        target_text=target,
        status=status,
        source_hash="x" * 64,
        source_flags={},
    )


# ---------------------------------------------------------------------------
# AC1 -- deterministic suite produces reproducible issues (AC1)
# ---------------------------------------------------------------------------
async def test_ac1_deterministic_suite_produces_issues(client):
    from backend.db import SessionLocal

    pid = await _new_project(client)
    with SessionLocal() as db:
        db.add(_unit(pid, "good", 1, "The cat sat.", "Il gatto sedette."))
        db.add(_unit(pid, "bad", 2, "The cat sat on the warm mat.", "Gatto."))
        db.add(_unit(pid, "untrans", 3, "Good morning.", "Good morning."))
        db.commit()

    segs = [
        {"segment_id": _uuid("good"), "source_text": "The cat sat.",
         "target_text": "Il gatto sedette."},
        {"segment_id": _uuid("bad"), "source_text":
            "The cat sat on the warm mat.", "target_text": "Gatto."},
        {"segment_id": _uuid("untrans"), "source_text": "Good morning.",
         "target_text": "Good morning."},
    ]
    issues = run_deterministic(segs)
    cats = {i["category"] for i in issues}
    assert "untranslated" in cats, cats
    assert "omission" in cats, cats
    # reproducible: same input -> same categories
    again = run_deterministic(segs)
    assert {i["category"] for i in again} == cats

    # and the /qa/run endpoint persists them as qa_issues rows
    r = await client.post(f"/api/v1/projects/{pid}/qa/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segments_evaluated"] >= 3
    assert body["issues_persisted"] >= 2

    with SessionLocal() as db:
        rows = db.query(QaIssue).filter(QaIssue.project_id == pid).all()
        assert len(rows) >= 2
        # each issue carries the MQM shape the PRD requires (§10.4)
        for row in rows:
            assert row.category
            assert row.severity in ("minor", "major", "critical")
            assert row.unit_id  # linked to a segment (§10.4)


# ---------------------------------------------------------------------------
# AC2 -- reference-free QE produces a per-segment score (AC2)
# ---------------------------------------------------------------------------
async def test_ac2_qe_scores_without_reference(client):
    from backend.db import SessionLocal

    # a good translation scores higher than a broken one -- no reference used
    good = estimate_segment_quality(
        "The cat sat on the mat in 2024.",
        "Il gatto sedette sul tappeto nel 2024.",
        invariants=["2024"])
    broken = estimate_segment_quality(
        "The cat sat on the mat.", "Xyz")
    assert good.score > 0.6, good.score
    assert broken.score < good.score, (broken.score, good.score)

    # calibration against a human sample (§18.2)
    cal = calibrate([0.9, 0.8, 0.4], [0.95, 0.75, 0.3])
    assert cal["n"] == 3
    assert isinstance(cal["pearson"], float)

    # the /qa/run endpoint writes each score to translation_units (§10.3)
    pid = await _new_project(client)
    with SessionLocal() as db:
        db.add(_unit(pid, "a", 1, "The cat sat on the warm mat.",
                     "Il gatto sedette sul matito caldo."))
        db.add(_unit(pid, "b", 2, "The cat sat on the warm mat.", "Xyz"))
        db.commit()

    r = await client.post(f"/api/v1/projects/{pid}/qa/run", json={})
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        units = db.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).all()
        assert all(u.quality_score is not None for u in units), \
            "every evaluated segment carries a QE score (§10.3)"
        good_u = next(u for u in units if str(u.id) == _uuid("a"))
        bad_u = next(u for u in units if str(u.id) == _uuid("b"))
        assert good_u.quality_score > bad_u.quality_score


# ---------------------------------------------------------------------------
# AC3 -- critic returns structured, schema-validated errors (AC3)
# ---------------------------------------------------------------------------
async def test_ac3_critic_structured_validated(client):
    from backend.db import SessionLocal

    # forbidden term -> a structured error, not a rewrite
    res = run_critic_deterministic(
        "The cat sat on the mat.",
        "Il gattone sedette sul tappeto.",
        glossary=[{"canonical_target": "gatto",
                   "forbidden_targets": ["gattone"]}])
    assert res.validated, "critic output must validate against the schema"
    jsonschema.validate({"errors": res.errors}, CRITIC_SCHEMA)
    assert any(e["category"] == "forbidden_term"
               and e["severity"] == "critical" for e in res.errors)
    # each error is a SUGGEMENT about what to fix, never a rewrite (§10.5)
    for e in res.errors:
        assert "evidence" in e and "suggestion" in e
        assert "gattone" in e["evidence"]
        assert "Il gattone" != e["suggestion"]

    # un-translated segment -> critical error
    res2 = run_critic_deterministic(
        "The cat sat on the mat.", "The cat sat on the mat.")
    assert res2.validated
    assert any(e["category"] == "untranslated"
               and e["severity"] == "critical" for e in res2.errors)

    # the /qa/run endpoint persists critic issues too -- the critic reads the
    # project's *approved* glossary from the DB (PRD §10.2)
    from backend.models import GlossaryTerm

    pid = await _new_project(client)
    with SessionLocal() as db:
        db.add(_unit(pid, "c", 1, "The cat sat on the mat.",
                     "Il gattone sedette."))
        db.add(GlossaryTerm(
            id=str(uuid.uuid4()), project_id=pid,
            source_term="gatto", target_term="gatto",
            term_type="CONCEPT_TERM", preferred=True,
            forbidden_targets=["gattone"], status="approved"))
        db.commit()
    r = await client.post(f"/api/v1/projects/{pid}/qa/run", json={})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        rows = db.query(QaIssue).filter(QaIssue.project_id == pid).all()
        assert any(row.category == "forbidden_term"
                   for row in rows), [r.category for r in rows]


async def test_qa_run_404_unknown_project(client):
    r = await client.post("/api/v1/projects/" + str(_uuid("nope")) + "/qa/run",
                          json={})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# AC1 (F4) -- human MQM annotation on a span, saved and filterable
# ---------------------------------------------------------------------------
async def test_human_annotation_saved_and_filterable(client):
    pid = await _new_project(client)
    uid = _uuid("h1")
    with SessionLocal() as db:
        db.add(_unit(pid, "h1", 1, "The cat sat on the mat.",
                     "Il gattone sedette sul tappeto."))
        db.commit()

    # the revisor selects a span in the target, picks a category + severity
    # and adds a comment (PRD §10.4 / AC1).
    r = await client.post(f"/api/v1/projects/{pid}/qa/issues", json={
        "unit_id": uid,
        "kind": "human",
        "category": "wrong_term",
        "severity": "critical",
        "message": "gattone ≠ gatto (glossario)",
        "span": "gattone",
        "comment": "usare il termine del glossario",
    })
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["category"] == "wrong_term"
    assert created["severity"] == "critical"
    assert created["span"] == "gattone"
    assert created["comment"] == "usare il termine del glossario"

    # filterable: kind=human + category (PRD §11.1 "tipo")
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"kind": "human",
                                 "category": "wrong_term"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["issues"]) == 1
    assert body["issues"][0]["id"] == created["id"]

    # a different kind must be filtered out
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"kind": "qa"})
    assert r.json()["issues"] == []

    # §10.4 group filter: terminology group contains wrong_term
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"group": "terminology"})
    assert r.status_code == 200
    assert len(r.json()["issues"]) == 1
    # ... and the accuracy group does not
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"group": "accuracy"})
    assert r.json()["issues"] == []

    # unknown group → 404
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"group": "not_a_group"})
    assert r.status_code == 404

    # invalid category is rejected by the taxonomy (PRD §10.4)
    r = await client.post(f"/api/v1/projects/{pid}/qa/issues", json={
        "unit_id": uid, "kind": "human", "category": "bogus_category",
        "severity": "minor", "message": "x",
    })
    assert r.status_code == 422


async def test_human_annotation_resolve_toggle(client):
    pid = await _new_project(client)
    uid = _uuid("h2")
    with SessionLocal() as db:
        db.add(_unit(pid, "h2", 1, "Good morning.", "Good morning."))
        db.commit()
    r = await client.post(f"/api/v1/projects/{pid}/qa/issues", json={
        "unit_id": uid, "kind": "human", "category": "untranslated",
        "severity": "critical", "message": "non tradotto",
    })
    assert r.status_code == 201
    issue_id = r.json()["id"]

    r = await client.post(
        f"/api/v1/projects/{pid}/qa/issues/{issue_id}/resolve")
    assert r.status_code == 200
    assert r.json()["resolved"] is True
    # the resolved filter sees it
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"resolved": "1"})
    assert [i["id"] for i in r.json()["issues"]] == [issue_id]
    # unresolved filter no longer does
    r = await client.get(f"/api/v1/projects/{pid}/qa/issues",
                         params={"resolved": "0"})
    assert r.json()["issues"] == []
    # toggle back
    r = await client.post(
        f"/api/v1/projects/{pid}/qa/issues/{issue_id}/resolve")
    assert r.json()["resolved"] is False


# ---------------------------------------------------------------------------
# AC2 (F4) -- MQM statistics per 1,000 words, by category and severity
# ---------------------------------------------------------------------------
async def test_mqm_report_per_1000_words(client):
    pid = await _new_project(client)
    # 100 source words total (10 per unit, 10 units).
    for i in range(10):
        with SessionLocal() as db:
            db.add(_unit(pid, f"m{i}", i + 1,
                         "one two three four five six seven eight nine ten",
                         "uno due tre quattro cinque sei sette otto nove dieci"))
            db.commit()
    with SessionLocal() as db:
        # Key by the *name* used to seed the unit (via the deterministic
        # ``_uuid``) so the issues below can be linked to the right segment.
        # The units were seeded at ordinals 1..10 in order (m0 -> 1 ... m9 ->
        # 10).  Key by ordinal -- robust to how the UUID column normalises
        # its string form -- instead of trying to reconstruct each id string.
        units = db.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).all()
        by_name = {
            f"m{i}": next(u.id for u in units if u.ordinal == i + 1)
            for i in range(10)
        }
        # 3 issues: 2 critical (1 omission, 1 wrong_term), 1 minor (grammar)
        for name, sev, cat, k in [
            ("m0", "critical", "omission", "qa"),
            ("m1", "critical", "wrong_term", "human"),
            ("m2", "minor", "grammar", "human"),
        ]:
            db.add(QaIssue(
                id=str(uuid.uuid4()), project_id=pid,
                unit_id=by_name[name], severity=sev, kind=k, category=cat,
                message=f"msg-{name}"))
        db.commit()

    r = await client.get(f"/api/v1/projects/{pid}/qa/mqm")
    assert r.status_code == 200
    rep = r.json()
    assert rep["n_source_words"] == 100
    assert rep["total_issues"] == 3
    # 3 issues per 100 words -> 30 per 1,000 words
    assert rep["per_1000_all"] == 30.0
    assert rep["by_category"]["omission"] == 1
    assert rep["by_category"]["wrong_term"] == 1
    assert rep["by_category"]["grammar"] == 1
    assert rep["by_severity"]["critical"] == 2
    assert rep["by_severity"]["minor"] == 1
    # per 1,000 words per (category, severity)
    assert rep["by_category_severity"]["omission"]["critical"] == 10.0
    assert rep["by_category_severity"]["wrong_term"]["critical"] == 10.0
    assert rep["by_category_severity"]["grammar"]["minor"] == 10.0

    # the report respects the severity filter
    r = await client.get(f"/api/v1/projects/{pid}/qa/mqm",
                         params={"severity": "critical"})
    rep = r.json()
    assert rep["total_issues"] == 2
    assert rep["per_1000_all"] == 20.0
    # minor was filtered out, so it is either absent or zero
    assert rep["by_severity"].get("minor", 0) in (0, 0.0)
    # the report respects the category filter
    r = await client.get(f"/api/v1/projects/{pid}/qa/mqm",
                         params={"category": "grammar"})
    rep = r.json()
    assert rep["total_issues"] == 1
    assert rep["per_1000_all"] == 10.0


async def test_mqm_taxonomy_endpoint(client):
    pid = await _new_project(client)
    r = await client.get(f"/api/v1/projects/{pid}/qa/taxonomy")
    assert r.status_code == 200
    body = r.json()
    # the six §10.4 groups and their categories
    assert set(body["groups"]) == {
        "accuracy", "terminology", "italian", "style", "locale", "source"}
    assert "untranslated" in body["groups"]["accuracy"]
    assert "wrong_term" in body["groups"]["terminology"]
    assert "calco" in body["groups"]["style"]
    assert body["severities"] == ["minor", "major", "critical"]
    # every category listed belongs to exactly one group
    groups = set()
    for cats in body["groups"].values():
        groups.update(cats)
    assert groups == set(body["categories"])
    # unknown project → 404
    r = await client.get(f"/api/v1/projects/{_uuid('nope')}/qa/taxonomy")
    assert r.status_code == 404
