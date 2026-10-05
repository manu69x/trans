"""Solo le entità APPROVATE fanno da vincoli di traduzione (2026-09-19).

Le proposte (status proposed/verified) non devono comparire tra i vincoli
§10.2 del runner di traduzione né tra i termini passati al critic QA.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.db import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    engine.dispose()


def _project_with_entities():
    from datetime import datetime, timezone

    from backend.db import SessionLocal
    from backend.models import Entity, Project

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        p = Project(
            id=str(uuid.uuid4()),
            title="Vincoli per stato",
            genre_profile="saggio",
            source_language="en",
            target_language="it",
            status="DRAFT",
            copyright_confirmed=True,
            created_at=now,
            updated_at=now,
        )
        db.add(p)
        db.flush()
        db.add(Entity(
            project_id=p.id,
            canonical_source="Warson",
            canonical_target="Warson",
            entity_type="PERSON",
            never_translate=True,
            status="approved",
        ))
        db.add(Entity(
            project_id=p.id,
            canonical_source="Baker Street",
            canonical_target="Baker Street",
            entity_type="PLACE",
            never_translate=True,
            status="proposed",
        ))
        db.commit()
        return str(p.id)


def test_constraints_come_solo_da_entita_approvate():
    pid = _project_with_entities()

    from backend.db import SessionLocal
    from backend.translation.runner import _constraints_for

    with SessionLocal() as db:
        c = _constraints_for(pid, db)
    sources = {e["canonical_source"] for e in c["entities"]}
    assert sources == {"Warson"}, \
        f"le proposte non devono vincolare: {sources}"
    assert "Baker Street" not in c["must_keep"]
    assert not any("Baker" in s for s in
                   {t.lower() for t in c["forbidden_terms"]})


def test_qa_constraints_come_solo_da_entita_approvate():
    pid = _project_with_entities()

    from backend.db import SessionLocal
    from backend.qa.runner import _constraints_for as qa_constraints

    with SessionLocal() as db:
        c = qa_constraints(pid, db)
    targets = {g["canonical_target"] for g in c["glossary"]}
    assert "Warson" in targets
    assert "Baker Street" not in targets
