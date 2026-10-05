"""Termbase (glossary) API routes (PRD §6.5, §7.1, §7.3, §12.3, §15.4).

Implements the termbase surface on top of the pure :mod:`backend.glossary`
layer and the ``glossary_terms`` / ``memory_snapshots`` tables:

* ``POST   /projects/{id}/glossary``            -- create a term (CRUD).
* ``GET    /projects/{id}/glossary``            -- list terms (filterable).
* ``GET    /projects/{id}/glossary/{term_id}``  -- fetch one term.
* ``PATCH  /projects/{id}/glossary/{term_id}``  -- update a term (§15.4:
  a change bumps ``version`` and records an audit entry).
* ``POST   /projects/{id}/glossary/{term_id}/retire`` -- soft-delete
  (status -> ``deprecated``); the row stays for audit.
* ``POST   /projects/{id}/glossary/import``     -- import CSV or TBX with a
  per-row error report (§12.3 / AC1).
* ``GET    /projects/{id}/glossary/export``     -- export CSV or TBX (§12.3).
* ``POST   /projects/{id}/glossary/snapshot``   -- freeze an immutable
  glossary snapshot (§7.2 / §15.4 / AC2).
* ``GET    /projects/{id}/glossary/snapshots``  -- list snapshots.
* ``POST   /projects/{id}/glossary/select``     -- §7.3 selection of the
  entities + terms to attach to one LLM block.

All mutations are audited (§13.1).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db_session
from .rbac import require_permission
from .glossary import (
    export_csv,
    export_tbx,
    import_csv,
    import_tbx,
    select_entities_for_block,
)
from .models import (
    AuditLog,
    GlossaryTerm,
    MemorySnapshot,
    Project,
    TranslationMemoryEntry,
)

router = APIRouter(prefix="/projects", tags=["glossary"])


# --- validation sets --------------------------------------------------------
_ENTITY_TYPES = {
    "PERSON", "ROLE", "CREATURE_SPECIES", "OBJECT_ARTIFACT",
    "LOCATION", "ORG_FACTION", "WORK_MEDIA", "EVENT",
    "CONCEPT_TERM", "TITLE_HONORIFIC",
}
_IT_GENDERS = {"masculine", "feminine", "common", "variable", "not_applicable"}
_NUMBERS = {"singular", "plural", "invariant", "collective", "unknown"}
_STATUSES = {"proposed", "verified", "approved", "deprecated", "archived"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _get_term(db: Session, project_id: str, term_id: str) -> GlossaryTerm:
    term = db.get(GlossaryTerm, term_id)
    if term is None or str(term.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="term not found")
    return term


# --- schemas ----------------------------------------------------------------
class TermCreate(BaseModel):
    source_term: str = Field(..., min_length=1)
    target_term: str | None = None
    term_type: str = Field(..., min_length=1)
    preferred: bool = True
    forbidden_targets: list[str] = Field(default_factory=list)
    grammatical_gender_it: str | None = None
    grammatical_number: str | None = None
    inflection_notes: str | None = None
    usage_notes: str | None = None
    status: str = "proposed"


class TermUpdate(BaseModel):
    target_term: str | None = None
    preferred: bool | None = None
    forbidden_targets: list[str] | None = None
    grammatical_gender_it: str | None = None
    grammatical_number: str | None = None
    inflection_notes: str | None = None
    usage_notes: str | None = None
    status: str | None = None
    source_term: str | None = None
    term_type: str | None = None


class SelectRequest(BaseModel):
    block_text: str
    preceding_texts: list[str] = Field(default_factory=list)
    coref_ids: list[str] = Field(default_factory=list)
    entities: list[dict] = Field(default_factory=list)
    terms: list[dict] = Field(default_factory=list)
    max_entities: int = Field(default=30, ge=0)
    max_terms: int = Field(default=20, ge=0)


# --- helpers ----------------------------------------------------------------
def _term_out(t: GlossaryTerm) -> dict:
    return {
        "id": str(t.id),
        "project_id": str(t.project_id),
        "source_term": t.source_term,
        "target_term": t.target_term,
        "term_type": t.term_type,
        "preferred": t.preferred,
        "forbidden_targets": list(t.forbidden_targets or []),
        "grammatical_gender_it": t.grammatical_gender_it,
        "grammatical_number": t.grammatical_number,
        "inflection_notes": t.inflection_notes,
        "usage_notes": t.usage_notes,
        "status": t.status,
        "version": t.version,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _audit(db: Session, project_id: str, action: str, entity: str,
           entity_id: str | None = None,
           before: dict | None = None, after: dict | None = None) -> None:
    db.add(AuditLog(
        project_id=project_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        created_at=datetime.utcnow(),
    ))
    db.commit()


# --- CRUD -------------------------------------------------------------------
@router.post("/{project_id}/glossary", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def create_term(
    project_id: str, payload: TermCreate, db: Session = Depends(get_db_session)
) -> dict:
    """Create one termbase entry (CRUD, §6.5)."""
    _get_project(db, project_id)
    if payload.term_type not in _ENTITY_TYPES:
        raise HTTPException(
            status_code=422, detail=f"invalid term_type {payload.term_type!r}")
    if payload.status not in _STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")
    if (payload.grammatical_gender_it is not None
            and payload.grammatical_gender_it not in _IT_GENDERS):
        raise HTTPException(status_code=422,
                            detail="invalid grammatical_gender_it")
    if (payload.grammatical_number is not None
            and payload.grammatical_number not in _NUMBERS):
        raise HTTPException(status_code=422, detail="invalid grammatical_number")

    term = GlossaryTerm(
        id=_uuid(),
        project_id=project_id,
        source_term=payload.source_term,
        target_term=payload.target_term,
        term_type=payload.term_type,
        preferred=payload.preferred,
        forbidden_targets=list(payload.forbidden_targets),
        grammatical_gender_it=payload.grammatical_gender_it,
        grammatical_number=payload.grammatical_number,
        inflection_notes=payload.inflection_notes,
        usage_notes=payload.usage_notes,
        status=payload.status,
        version=1,
        created_at=datetime.utcnow(),
    )
    db.add(term)
    db.commit()
    _audit(db, project_id, "term_created", "glossary_term", str(term.id),
           after=_term_out(term))
    return _term_out(term)


@router.get("/{project_id}/glossary")
async def list_terms(
    project_id: str,
    status: str | None = Query(default=None),
    term_type: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> list[dict]:
    """List the project's termbase entries (filterable by status/type)."""
    _get_project(db, project_id)
    query = db.query(GlossaryTerm).filter(GlossaryTerm.project_id == project_id)
    if status:
        query = query.filter(GlossaryTerm.status == status)
    if term_type:
        query = query.filter(GlossaryTerm.term_type == term_type)
    rows = query.order_by(GlossaryTerm.source_term).all()
    return [_term_out(t) for t in rows]


# --- export -----------------------------------------------------------------
@router.get("/{project_id}/glossary/export")
async def export_terms(
    project_id: str,
    fmt: str = Query(default="csv", description="csv|tbx"),
    db: Session = Depends(get_db_session),
) -> Response:
    """Export the termbase as CSV or TBX (§12.3)."""
    _get_project(db, project_id)
    rows = (
        db.query(GlossaryTerm)
        .filter(GlossaryTerm.project_id == project_id)
        .all()
    )
    payload = [_term_out(t) for t in rows]
    if fmt == "csv":
        content = export_csv(payload)
        media = "text/csv; charset=utf-8"
        ext = "csv"
    elif fmt == "tbx":
        content = export_tbx(payload)
        media = "application/xml; charset=utf-8"
        ext = "tbx"
    else:
        raise HTTPException(status_code=422, detail=f"unknown format {fmt!r}")

    _audit(db, project_id, "glossary_exported", "glossary",
           after={"format": fmt, "count": len(payload)})
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition":
                 f'attachment; filename="glossary.{ext}"'},
    )


@router.get("/{project_id}/glossary/snapshots")
async def list_snapshots(
    project_id: str, db: Session = Depends(get_db_session)
) -> list[dict]:
    """List the project's glossary snapshots (§15.4)."""
    _get_project(db, project_id)
    rows = (
        db.query(MemorySnapshot)
        .filter(MemorySnapshot.project_id == project_id,
                MemorySnapshot.snapshot_type == "glossary")
        .order_by(MemorySnapshot.created_at)
        .all()
    )
    return [
        {
            "snapshot_id": str(s.id),
            "snapshot_type": s.snapshot_type,
            "item_count": s.item_count,
            "description": s.description,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in rows
    ]


@router.get("/{project_id}/glossary/{term_id}")
async def get_term(
    project_id: str, term_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Fetch one termbase entry (CRUD)."""
    _get_project(db, project_id)
    term = _get_term(db, project_id, term_id)
    return _term_out(term)


@router.patch("/{project_id}/glossary/{term_id}", dependencies=[Depends(require_permission("manage_glossary"))])
async def update_term(
    project_id: str, term_id: str, payload: TermUpdate,
    db: Session = Depends(get_db_session),
) -> dict:
    """Update a term (CRUD, §15.4): a change bumps ``version`` and audits."""
    _get_project(db, project_id)
    term = _get_term(db, project_id, term_id)

    changes = payload.model_dump(exclude_none=True)
    if "status" in changes and changes["status"] not in _STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")
    if ("grammatical_gender_it" in changes
            and changes["grammatical_gender_it"] not in _IT_GENDERS):
        raise HTTPException(status_code=422,
                            detail="invalid grammatical_gender_it")
    if ('grammatical_number' in changes
            and changes['grammatical_number'] not in _NUMBERS):
        raise HTTPException(status_code=422, detail="invalid grammatical_number")

    before = _term_out(term)
    for key, value in changes.items():
        setattr(term, key, value)
    # §15.4: every edit bumps the version so the change is auditable.
    if changes:
        term.version += 1
    term.updated_at = datetime.utcnow()
    db.add(term)
    db.commit()
    after = _term_out(term)
    _audit(db, project_id, "term_updated", "glossary_term", str(term.id),
           before=before, after=after)
    return after


@router.post("/{project_id}/glossary/{term_id}/retire", dependencies=[Depends(require_permission("manage_glossary"))])
async def retire_term(
    project_id: str, term_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Soft-delete a term (status -> ``deprecated``); the row is kept (§15.4)."""
    _get_project(db, project_id)
    term = _get_term(db, project_id, term_id)
    before = _term_out(term)
    term.status = "deprecated"
    term.version += 1
    term.updated_at = datetime.utcnow()
    db.add(term)
    db.commit()
    after = _term_out(term)
    _audit(db, project_id, "term_retired", "glossary_term", str(term.id),
           before=before, after=after)
    return after


# --- import -----------------------------------------------------------------
@router.post("/{project_id}/glossary/import", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def import_terms(
    project_id: str,
    file: UploadFile,
    fmt: str = Query(default="auto", description="csv|tbx|auto"),
    db: Session = Depends(get_db_session),
) -> dict:
    """Import CSV or TBX with a per-row error report (§12.3 / AC1).

    ``fmt`` may be ``csv`` or ``tbx``; ``auto`` (default) infers the format
    from the filename. Invalid rows are skipped and reported; valid rows are
    inserted. The result is a partial import (AC1).
    """
    _get_project(db, project_id)
    data = await file.read()
    text = data.decode("utf-8", errors="replace")

    if fmt == "auto":
        fname = (file.filename or "").lower()
        if fname.endswith(".tbx") or fname.endswith(".xml"):
            fmt = "tbx"
        else:
            fmt = "csv"

    if fmt == "csv":
        terms, errors = import_csv(text)
    elif fmt == "tbx":
        terms, errors = import_tbx(text)
    else:
        raise HTTPException(status_code=422, detail=f"unknown format {fmt!r}")

    inserted: list[str] = []
    for t in terms:
        row = GlossaryTerm(
            id=_uuid(),
            project_id=project_id,
            source_term=t["source_term"],
            target_term=t["target_term"],
            term_type=t["term_type"],
            preferred=t["preferred"],
            forbidden_targets=t["forbidden_targets"],
            grammatical_gender_it=t["grammatical_gender_it"],
            grammatical_number=t["grammatical_number"],
            inflection_notes=t["inflection_notes"],
            usage_notes=t["usage_notes"],
            status=t["status"],
            version=1,
            created_at=datetime.utcnow(),
        )
        db.add(row)
        inserted.append(str(row.id))
    db.commit()
    _audit(
        db, project_id, "glossary_imported", "glossary",
        after={
            "format": fmt,
            "imported": len(inserted),
            "errors": len(errors),
            "rows": len(terms) + len(errors),
        },
    )
    return {
        "imported": len(inserted),
        "errors": errors,
        "error_count": len(errors),
        "total": len(terms) + len(errors),
    }


# --- snapshot (immutable) ---------------------------------------------------
@router.post("/{project_id}/glossary/snapshot", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def create_snapshot(
    project_id: str,
    description: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """Freeze an immutable glossary snapshot (§7.2 / §15.4 / AC2).

    The snapshot row is immutable: there is no API to modify or delete it,
    and ``item_count`` is frozen at creation time so later edits to the live
    glossary cannot change it. The returned ``snapshot_id`` is what a
    translation run references (§3.3 / §5.4).
    """
    _get_project(db, project_id)
    rows = (
        db.query(GlossaryTerm)
        .filter(GlossaryTerm.project_id == project_id)
        .all()
    )
    snap = MemorySnapshot(
        id=_uuid(),
        project_id=project_id,
        snapshot_type="glossary",
        description=description,
        item_count=len(rows),
        created_at=datetime.utcnow(),
    )
    db.add(snap)
    db.commit()
    _audit(db, project_id, "glossary_snapshot_created", "memory_snapshot",
           str(snap.id),
           after={"snapshot_type": "glossary", "item_count": len(rows)})
    return {
        "snapshot_id": str(snap.id),
        "snapshot_type": snap.snapshot_type,
        "item_count": snap.item_count,
        "created_at": snap.created_at.isoformat(),
    }


# --- §7.3 selection ---------------------------------------------------------
@router.post("/{project_id}/glossary/select", dependencies=[Depends(require_permission("manage_glossary"))])
async def select_terms(
    project_id: str, payload: SelectRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """§7.3 selection of entities + terms for one block."""
    _get_project(db, project_id)
    result = select_entities_for_block(
        block_text=payload.block_text,
        preceding_texts=payload.preceding_texts,
        coref_ids=payload.coref_ids,
        entities=payload.entities,
        terms=payload.terms,
        max_entities=payload.max_entities,
        max_terms=payload.max_terms,
    )
    _audit(db, project_id, "glossary_select", "glossary",
           after={"counts": result["counts"]})
    return result


# --- TM snapshot (F3 / PRD §7.2) -------------------------------------------
@router.post("/{project_id}/tm/snapshot", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def create_tm_snapshot(
    project_id: str,
    description: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """Freeze an immutable TM snapshot (F3 / PRD §7.2 / §16).

    Like the glossary snapshot, the row is immutable (no update/delete
    endpoint; ``item_count`` frozen) and carries a ``payload`` with the
    frozen TM entry ids so the translation planner (§10.1 step 3) can render
    the exact prompt from the snapshot.
    """
    _get_project(db, project_id)
    rows = (
        db.query(TranslationMemoryEntry)
        .filter(TranslationMemoryEntry.project_id == project_id)
        .all()
    )
    snap = MemorySnapshot(
        id=_uuid(),
        project_id=project_id,
        snapshot_type="tm",
        description=description,
        item_count=len(rows),
        payload={"tm_entry_ids": [str(r.id) for r in rows]},
        created_at=datetime.utcnow(),
    )
    db.add(snap)
    db.commit()
    _audit(db, project_id, "tm_snapshot_created", "memory_snapshot",
           str(snap.id),
           after={"snapshot_type": "tm", "item_count": len(rows)})
    return {
        "snapshot_id": str(snap.id),
        "snapshot_type": snap.snapshot_type,
        "item_count": snap.item_count,
        "created_at": snap.created_at.isoformat(),
    }
