"""Structure API routes (PRD §5.3, §15.1).

Implements the structure-review surface on top of the detection runner and
the pure :mod:`backend.parsing.structure` layer:

* ``POST /projects/{id}/structure/detect`` — queue the multi-signal
  detection job (§5.3 evidences 1-6).
* ``GET  /projects/{id}/structure`` — the proposed/confirmed nodes.
* ``PATCH /projects/{id}/structure/{node_id}`` — move/rename/re-kind a node
  (the §5.3 visual editor contract: creare, unire, dividere, spostare,
  rinominare) and set ``user_confirmed``.
* ``POST /projects/{id}/structure/{node_id}/confirm`` — confirm as-is.
* ``POST /projects/{id}/structure/{node_id}/regenerate`` — §15.1: after a
  boundary correction only the *dependent* segments are invalidated for
  regeneration; approved translations of untouched chapters stay approved.
* ``GET  /projects/{id}/documents/{document_id}/cleaning/preview`` — the
  header/footer candidates with their algorithmic confirmation status
  (§5.3: deletion only after confirmation on N pages, preview with
  rollback).
* ``POST .../cleaning/apply`` — user-confirmed deletion; stores a rollback
  snapshot and marks the pages' payload blocks ``excluded``.
* ``POST .../cleaning/rollback`` — restore the pre-cleaning text from the
  stored snapshot.

All mutations are audited (§13.1). Manuscript text never leaves the local
system (§13.1 local-only): the preview returns counts and the repeated
*key* strings (already sanitised: digits collapsed) — full page text stays
in the DB behind the existing page endpoint.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db_session
from .rbac import require_permission
from .segment_numbering import next_numero_start
from .models import (
    AuditLog,
    Document,
    DocumentPage,
    ImportReport,
    Job,
    Project,
    StructureNode,
    TranslationMemoryEntry,
    TranslationUnit,
)
from .parsing import chunking, structure
from . import chunking_runner
from .scheduler import get_scheduler_from
from .structure_editor import (
    EditorError,
    op_create,
    op_delete,
    op_merge,
    op_move,
    op_rename,
    op_set_boundary,
    op_split,
    op_undo,
)

router = APIRouter(prefix="/projects", tags=["structure"])


def _editor_error(exc: EditorError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# --- CAT segmentation (§5.4) ----------------------------------------------
def _unit_out(unit: TranslationUnit) -> dict:
    flags = dict(unit.source_flags or {})
    it = float(unit.is_italian) if unit.is_italian is not None else None
    en = float(unit.is_english) if unit.is_english is not None else None
    return {
        "segment_id": str(unit.id),
        "project_id": str(unit.project_id),
        "chapter_id": str(unit.chapter_id) if unit.chapter_id else None,
        "ordinal": unit.ordinal,
        "source_text": unit.source_text,
        "target_text": unit.target_text,
        "status": unit.status,
        "source_hash": unit.source_hash,
        "kind": flags.get("kind"),
        "page": flags.get("page"),
        "numero": unit.numero,
        "is_italian": it,
        "is_translated": (float(unit.is_translated)
                          if unit.is_translated is not None else None),
        "is_english": en,
        "qe_diff": (round(it - en, 4)
                     if it is not None and en is not None else None),
    }


@router.post("/{project_id}/structure/{node_id}/segment",
             status_code=202,
             dependencies=[Depends(require_permission("manage_structure"))])
async def segment_chapter(
    project_id: str, node_id: str,
    sentences_per_segment: int = Query(default=1, ge=1, le=9999),
    db: Session = Depends(get_db_session)
) -> dict:
    """Queue the §5.4 segmentation of one chapter (structure node)."""
    _get_project(db, project_id)
    _get_node(db, project_id, node_id)
    svc_scheduler = get_scheduler_from(db)
    job = svc_scheduler.register(
        project_id, "segment_chapter",
        {"project_id": str(project_id), "node_id": str(node_id),
         "sentences_per_segment": sentences_per_segment},
    )
    svc_scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }


# --- segmenti: overview per la scheda "Segmenti" (token, stato) ------------
@router.get("/{project_id}/segments/overview")
async def segments_overview(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Per-chapter token estimates for the Segments tab (§5.4 UI).

    One row per structure node (chapter/part/front_matter): segment count,
    total estimated source tokens and whether the chapter fits into ONE
    translation block (max_total 16.384 with context+output reserve). The
    per-segment estimates let the UI flag oversized segments (red) and
    offer a manual split.
    """
    from .parsing.chunking import (
        MAX_BLOCK_TOTAL_TOKENS,
        CONTEXT_RESERVE_TOKENS,
        OUTPUT_RESERVE_TOKENS,
    )
    from .translation.planner import count_tokens

    _get_project(db, project_id)
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    max_total = MAX_BLOCK_TOTAL_TOKENS
    usable = max_total - CONTEXT_RESERVE_TOKENS - OUTPUT_RESERVE_TOKENS

    # Testo estratto per la STIMA dei token dei capitoli non ancora
    # segmentati: l'utente deve vedere i token di TUTTI i capitoli subito,
    # senza eseguire prima la segmentazione (§5.4 UI).
    page_payloads = chunking_runner._project_page_payloads(db, project_id)
    first_document = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    confirmed_keys = chunking_runner._confirmed_header_keys(
        db, page_payloads,
        first_document.id if first_document else None,
    ) if page_payloads else None

    chapter_rows: list[dict] = []
    for n in nodes:
        if n.kind not in ("chapter", "part", "front_matter"):
            continue
        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == project_id,
                    TranslationUnit.chapter_id == str(n.id))
            .order_by(TranslationUnit.ordinal)
            .all()
        )
        seg_rows = []
        total_tokens = 0
        estimated = False
        paragraph_count = None
        if units:
            for u in units:
                t = count_tokens(u.source_text or "")
                total_tokens += t
                seg_rows.append({
                    "segment_id": str(u.id),
                    "ordinal": u.ordinal,
                    "numero": u.numero,
                    "status": u.status,
                    "tokens": t,
                    "chars": len(u.source_text or ""),
                    "over_limit": t > usable,
                    "text": (u.source_text or "")[:160],
                })
        # §5.4 UI: i paragrafi del capitolo contati dal testo sorgente
        # (proprietà del capitolo, indipendente dalla segmentazione); per i
        # capitoli non ancora segmentati qui si calcola anche la stima token.
        if confirmed_keys is not None and page_payloads:
            try:
                pages = chunking_runner._chapter_pages(
                    page_payloads, n, confirmed_keys)
                paragraphs = chunking.build_chapter_paragraphs(
                    pages, confirmed_keys)
                paragraph_count = len(paragraphs)
                if not units:
                    text = " ".join(p.get("text") or "" for p in paragraphs)
                    total_tokens = count_tokens(text)
                    estimated = True
            except Exception:  # noqa: BLE001 - stima best-effort
                if not units:
                    total_tokens = 0
                    estimated = True
        chapter_rows.append({
            "node_id": str(n.id),
            "kind": n.kind,
            "title": n.normalized_title or n.source_label,
            "status": n.status,
            "segment_count": len(seg_rows),
            "paragraph_count": paragraph_count,
            "total_tokens": total_tokens,
            "estimated": estimated,
            "fits_one_block": total_tokens <= usable,
            "over_limit": total_tokens > usable,
            "max_block_tokens": max_total,
            "usable_block_tokens": usable,
            "segments": seg_rows,
        })
    return {
        "project_id": project_id,
        "max_block_tokens": max_total,
        "usable_block_tokens": usable,
        "context_reserve_tokens": CONTEXT_RESERVE_TOKENS,
        "output_reserve_tokens": OUTPUT_RESERVE_TOKENS,
        "chapters": chapter_rows,
    }


# --- verifica corrispondenza originale <-> segmenti (scheda Segmenti) -------
@router.get("/{project_id}/segments/verify")
def verify_segment_correspondence(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Report di corrispondenza carattere-per-carattere fra il testo
    originale del libro e la sequenza dei segmenti (§5.4 UI).

    Sincrona (def): FastAPI la esegue in threadpool così la scansione di un
    libro intero non blocca l'event loop. Nessuna scrittura.
    """
    from .segment_verify import verify_project_segments

    _get_project(db, project_id)
    return verify_project_segments(db, project_id)


# --- rimozione segmentazione (scheda Segmenti) ------------------------------
class ClearSegmentationRequest(BaseModel):
    """Body for ``POST .../segments/clear``."""

    node_ids: list[str] = Field(default_factory=list)


@router.post("/{project_id}/segments/clear", status_code=200, dependencies=[Depends(require_permission("manage_structure"))])
async def clear_segmentation(
    project_id: str, payload: ClearSegmentationRequest,
    db: Session = Depends(get_db_session)
) -> dict:
    """Remove ALL segments (translation units) of the selected chapters.

    Scheda Segmenti "Rimuovi segmentazione": approved units are preserved
    (§5.1 immutability) unless the chapter has ONLY approved units, in
    which case the request is rejected for that chapter. Everything else
    (machine_draft / untranslated) is deleted, so the chapter shows as
    "non segmentato" again.
    """
    _get_project(db, project_id)
    if not payload.node_ids:
        raise HTTPException(status_code=422,
                            detail="nessun capitolo selezionato")
    deleted = kept_approved = 0
    skipped: list[str] = []
    for node_id in payload.node_ids:
        _get_node(db, project_id, node_id)
        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == project_id,
                    TranslationUnit.chapter_id == node_id)
            .all()
        )
        if not units:
            continue
        approved = [u for u in units if u.status == "approved"]
        if len(approved) == len(units):
            skipped.append(node_id)
            kept_approved += len(approved)
            continue
        for u in units:
            if u.status == "approved":
                kept_approved += 1
                continue
            db.delete(u)
            deleted += 1
    db.commit()
    return {
        "deleted": deleted,
        "kept_approved": kept_approved,
        "chapters_all_approved": skipped,
    }


# --- risegmentazione manuale di un segmento troppo lungo -------------------
class SegmentSplitRequest(BaseModel):
    """Body for ``POST .../segments/{segment_id}/split``."""

    parts: int = Field(default=2, ge=2, le=10)


@router.post("/{project_id}/segments/{segment_id}/split",
             status_code=202,
             dependencies=[Depends(require_permission("manage_structure"))])
async def split_segment(
    project_id: str, segment_id: str, payload: SegmentSplitRequest,
    db: Session = Depends(get_db_session)
) -> dict:
    """Manually split an oversized segment into ``parts`` halves (§5.4 UI).

    Sentence-aware: the split point is the sentence boundary closest to the
    middle of the text (anti-break protections do not apply here — the user
    explicitly asked for the cut). The following segments' ordinals shift
    downstream; approved units are never touched.
    """
    import uuid as _uuid

    from .parsing import chunking

    _get_project(db, project_id)
    try:
        _uuid.UUID(segment_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422,
                            detail="identificativo segmento non valido")
    unit = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.id == segment_id)
        .first()
    )
    if unit is None:
        raise HTTPException(status_code=404, detail="segmento inesistente")
    if unit.status == "approved":
        raise HTTPException(
            status_code=409,
            detail="segmento approvato: non può essere risegmentato (§5.1)")
    text = unit.source_text or ""
    if len(text) < 40:
        raise HTTPException(status_code=422,
                            detail="segmento troppo corto per essere diviso")

    sentences = chunking._regex_sentences(text)
    if len(sentences) < payload.parts:
        # not enough sentence boundaries: hard-split by characters
        size = len(text) // payload.parts
        sentences = [text[i:i + size]
                     for i in range(0, len(text), size)]
    while len(sentences) > payload.parts:
        # merge tail pieces so we produce exactly ``parts`` groups
        tail = sentences[-2:]
        sentences = sentences[:-2] + [" ".join(tail)]

    # renumber all subsequent ordinals to keep chapter-local 1..N order
    chapter_id = str(unit.chapter_id) if unit.chapter_id else None
    following = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.ordinal > unit.ordinal)
        .order_by(TranslationUnit.ordinal)
        .all()
    ) if chapter_id is None else (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == chapter_id,
                TranslationUnit.ordinal > unit.ordinal)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    base_ordinal = unit.ordinal
    new_flags = dict(unit.source_flags or {})
    new_flags["manual_split"] = True
    unit.source_text = " ".join(sentences[:-1])
    unit.source_hash = chunking.sha256_hex(unit.source_text)
    unit.source_flags = {**new_flags, "manual_split_of": str(unit.id)}
    unit.status = "untranslated" if unit.status != "machine_draft" \
        else "machine_draft"
    db.add(unit)
    tail_flags = dict(unit.source_flags or {})
    tail_row = TranslationUnit(
        project_id=project_id,
        chapter_id=unit.chapter_id,
        ordinal=base_ordinal + 1,
        numero=next_numero_start(db, project_id),
        id=chunking.segment_uid(project_id, str(unit.chapter_id),
                                base_ordinal + 1),
        source_text=sentences[-1],
        source_hash=chunking.sha256_hex(sentences[-1]),
        source_flags={**tail_flags, "manual_split": True},
        status="untranslated",
    )
    db.add(tail_row)
    shift = 1
    for row in following:
        row.ordinal = row.ordinal + shift
        db.add(row)
    db.commit()
    from backend.scheduler import get_scheduler_from  # noqa: F401
    return {
        "segment_id": str(unit.id),
        "new_segment_id": str(tail_row.id),
        "parts": payload.parts,
        "shifted_following": len(following),
    }


@router.get("/{project_id}/chapters/{node_id}/plan")
async def plan_chapter_blocks(
    project_id: str, node_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Dry-run planner: the chapter's §5.4 blocks with budgets + overlap.

    Works on the persisted segments (run ``segment`` first); calls no LLM
    and writes nothing — the same planner the translation adapter will use
    before dispatching a block to LLM Gateway.
    """
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == node_id)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    if not units:
        raise HTTPException(
            status_code=409,
            detail="no segments for this chapter: POST .../segment first")
    chunk_units = [
        {
            "segment_id": str(u.id),
            "ordinal": u.ordinal,
            "text": u.source_text,
        }
        for u in units
    ]
    tokenizer = chunking.get_tokenizer()
    blocks = chunking.plan_blocks(chunk_units, tokenizer)
    verification = chunking.verify_blocks(blocks)
    serialized = [
        chunking.serialize_block(chunk_units, block, tokenizer)
        for block in blocks
    ]
    return {
        "project_id": str(project_id),
        "chapter_id": str(node_id),
        "node_kind": node.kind,
        "segments": len(chunk_units),
        "tokenizer": getattr(tokenizer, "name", "unknown"),
        "verification": verification,
        "blocks": serialized,
    }


@router.get("/{project_id}/chapters/{node_id}/segments")
async def list_chapter_segments(
    project_id: str, node_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """The chapter's CAT segments (translation_units, PRD §5.4/§6.5)."""
    _get_project(db, project_id)
    _get_node(db, project_id, node_id)
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == node_id)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    return {
        "project_id": str(project_id),
        "chapter_id": str(node_id),
        "segments": [_unit_out(u) for u in units],
        "total": len(units),
        "untranslated": sum(1 for u in units if u.status == "untranslated"),
        "approved": sum(1 for u in units if u.status == "approved"),
    }


# --- segment approval (§5.1 / §7.2, F3 / task t_694dfe91) ------------------
class SegmentApprove(BaseModel):
    """Body for ``POST .../segments/{segment_id}/approve``.

    The reviewer approves one segment (setting ``status`` to ``approved``) and,
    as the §7.2 approve hook, the approved source/target is written to the
    short-term memory (``tm_entries``) with a source embedding so the pgvector
    retrieval (§7.2) can match it later. ``reviewer`` / ``qa_score`` are
    optional provenance fields carried onto the TM entry.
    """

    reviewer: str | None = None
    qa_score: float | None = None


@router.post("/{project_id}/segments/{segment_id}/approve", status_code=201, dependencies=[Depends(require_permission("approve_segments"))])
async def approve_segment(
    project_id: str,
    segment_id: str,
    payload: SegmentApprove,
    db: Session = Depends(get_db_session),
) -> dict:
    """Approve a segment and register it in the TM (§7.2 approve hook).

    Sets the segment to ``approved`` (§5.1: an approved segment is immutable
    afterwards) and, in the same transaction, stores a TM entry whose
    ``segment_id`` links back to the segment. The entry carries the normalised
    and original source, the approved target, the project's genre, the
    reviewer / QA score and the source embedding (§7.2).
    """
    _get_project(db, project_id)
    # §5.1: an already-approved segment is immutable -- reject the second
    # approval (it would create a duplicate TM entry).
    unit = (
        db.query(TranslationUnit)
        .filter(
            TranslationUnit.id == segment_id,
            TranslationUnit.project_id == project_id,
        )
        .one_or_none()
    )
    if unit is None:
        raise HTTPException(
            status_code=404, detail="segment not found"
        )
    if unit.status == "approved":
        # already in the TM; return it idempotently
        existing = db.query(TranslationMemoryEntry).filter(
            TranslationMemoryEntry.segment_id == segment_id
        ).one_or_none()
        if existing is not None:
            return {
                "segment_id": str(segment_id),
                "status": "approved",
                "tm_entry_id": str(existing.id),
                "created": False,
            }
        # approved but no TM entry yet (e.g. restored from an older row):
        # create it now so the invariant "approved => has a TM entry" holds.
    # --- mark the segment approved -----------------------------------------
    unit.status = "approved"
    unit.updated_at = datetime.utcnow()
    db.add(AuditLog(
        project_id=project_id,
        action="segment_approved",
        entity="segment",
        entity_id=str(segment_id),
        after={"status": "approved"},
        created_at=datetime.utcnow(),
    ))

    # --- §7.2 approve hook: write the TM entry -----------------------------
    from .translation import embedding

    project = db.get(Project, project_id)
    genre = project.genre_profile if project else None
    src = unit.source_text or ""
    entry = TranslationMemoryEntry(
        id=str(uuid.uuid4()),
        project_id=project_id,
        chapter_id=unit.chapter_id,
        segment_id=segment_id,
        source_normalized=src,
        source_original=src,
        target_approved=unit.target_text or "",
        genre_profile=genre,
        pov=None,
        reviewer=payload.reviewer,
        qa_score=payload.qa_score,
        terms_used=[],
        source_embedding=embedding.embed(src),
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    return {
        "segment_id": str(segment_id),
        "status": "approved",
        "tm_entry_id": str(entry.id),
        "created": True,
    }


# --- schemas --------------------------------------------------------------
class StructureNodeUpdate(BaseModel):
    kind: str | None = Field(
        default=None,
        pattern="^(front_matter|part|chapter|scene|back_matter|footnote)$")
    source_label: str | None = Field(default=None, min_length=1)
    normalized_title: str | None = None
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    status: str | None = Field(
        default=None, pattern="^(proposed|user_confirmed)$")


class CleaningApply(BaseModel):
    texts: list[str] = Field(..., min_length=1,
                             description="repeated keys to delete")


class RegenResult(BaseModel):
    node_id: str
    status: str
    segments_invalidated: int
    segments_untouched: int


# --- helpers --------------------------------------------------------------
def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _get_node(db: Session, project_id: str, node_id: str) -> StructureNode:
    node = (
        db.query(StructureNode)
        .filter(StructureNode.id == node_id,
                StructureNode.project_id == project_id)
        .one_or_none()
    )
    if node is None:
        raise HTTPException(status_code=404, detail="structure node not found")
    return node


def _get_document(db: Session, project_id: str,
                  document_id: str) -> Document:
    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


def _node_out(node: StructureNode) -> dict:
    return {
        "node_id": str(node.id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "kind": node.kind,
        "source_label": node.source_label,
        "normalized_title": node.normalized_title,
        "start_page": node.start_page,
        "end_page": node.end_page,
        "start_char": node.start_char,
        "end_char": node.end_char,
        "confidence": float(node.confidence) if node.confidence is not None
        else None,
        "detection_method": node.detection_method or [],
        "status": node.status,
        "ordinal": node.ordinal,
    }


# --- detection ------------------------------------------------------------
@router.post("/{project_id}/structure/detect",
             response_model=dict, status_code=202,
             dependencies=[Depends(require_permission("manage_structure"))])
async def detect_structure(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Queue the §5.3 multi-signal structure detection for the project."""
    _get_project(db, project_id)
    svc_scheduler = get_scheduler_from(db)
    job = svc_scheduler.register(
        project_id, "detect_structure", {"project_id": str(project_id)}
    )
    svc_scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }


@router.get("/{project_id}/structure")
async def get_structure(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """The project's structure nodes (§5.3 output schema), in book order."""
    _get_project(db, project_id)
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    return {
        "project_id": str(project_id),
        "nodes": [_node_out(n) for n in nodes],
        "proposed": sum(1 for n in nodes if n.status == "proposed"),
        "user_confirmed": sum(
            1 for n in nodes if n.status == "user_confirmed"),
    }


@router.patch("/{project_id}/structure/{node_id}", dependencies=[Depends(require_permission("manage_structure"))])
async def update_structure_node(
    project_id: str, node_id: str, payload: StructureNodeUpdate,
    db: Session = Depends(get_db_session),
) -> dict:
    """Edit a node (§5.3 visual editor) and/or confirm it.

    Title-only edits go through :func:`op_rename` (undoable); range edits
    go through :func:`op_set_boundary` (undoable + tracked by the
    selective ``resegment``).  ``status`` and ``kind`` stay direct field
    writes (confirmation/kind changes need no re-segmentation).
    """
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    project = db.get(Project, project_id)
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="no changes")
    range_change = "start_page" in changes or "end_page" in changes
    title_change = "source_label" in changes or \
        "normalized_title" in changes
    try:
        if title_change and not range_change:
            label = changes.get("source_label") or \
                changes.get("normalized_title")
            if label:
                op_rename(db, project, node, label)
                node = _get_node(db, project_id, node_id)
        if range_change:
            op_set_boundary(db, project, node,
                            start_page=changes.get("start_page"),
                            end_page=changes.get("end_page"))
            node = _get_node(db, project_id, node_id)
    except EditorError as exc:
        raise _editor_error(exc) from exc
    direct = {
        k: v for k, v in changes.items() if k in ("kind", "status")}
    if direct:
        for field, value in direct.items():
            setattr(node, field, value)
        db.add(AuditLog(
            project_id=project_id,
            action="structure_node_updated",
            entity="structure_node",
            entity_id=str(node.id),
            after=_node_out(node),
            created_at=datetime.utcnow(),
        ))
        db.commit()
    db.refresh(node)
    return _node_out(node)


@router.post("/{project_id}/structure/{node_id}/confirm", dependencies=[Depends(require_permission("manage_structure"))])
async def confirm_structure_node(
    project_id: str, node_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Confirm a proposed node as-is (§5.3 ``user_confirmed``)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    node.status = "user_confirmed"
    db.add(AuditLog(
        project_id=project_id,
        action="structure_node_confirmed",
        entity="structure_node",
        entity_id=str(node.id),
        after=_node_out(node),
        created_at=datetime.utcnow(),
    ))
    db.commit()
    db.refresh(node)
    return _node_out(node)


@router.post("/{project_id}/structure/{node_id}/regenerate",
             response_model=RegenResult,
             dependencies=[Depends(require_permission("manage_structure"))])
async def regenerate_node_segments(
    project_id: str, node_id: str, db: Session = Depends(get_db_session)
) -> RegenResult:
    """Selective regeneration after a boundary change (§15.1).

    Invalidates only the translation units whose ``chapter_id`` points at
    this node — and only while they are not approved — leaving every other
    chapter's segments (and approved translations) untouched.
    """
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    if node.kind == "scene":
        raise HTTPException(
            status_code=400,
            detail="scenes do not own segments; regenerate their chapter")
    node.status = "user_confirmed"
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == node.id)
        .all()
    )
    invalidated = untouched = 0
    for unit in units:
        if unit.status == "approved":
            untouched += 1  # §5.1: immutabilità delle versioni approvate
            continue
        flags = dict(unit.source_flags or {})
        flags["regen_pending"] = True
        flags["regen_node_id"] = str(node.id)
        unit.source_flags = flags
        unit.status = "untranslated"
        db.add(unit)
        invalidated += 1
    db.add(AuditLog(
        project_id=project_id,
        action="structure_segments_regenerated",
        entity="structure_node",
        entity_id=str(node.id),
        after={"invalidated": invalidated, "untouched_approved": untouched},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    db.refresh(node)
    return RegenResult(
        node_id=str(node.id),
        status=node.status,
        segments_invalidated=invalidated,
        segments_untouched=untouched,
    )


# --- editor operations (§5.3 visual editor, §11.1, §12.2) -----------------
class StructureNodeCreate(BaseModel):
    kind: str = Field(..., pattern="^(front_matter|part|chapter|scene|"
                                   "back_matter|footnote)$")
    source_label: str = Field(..., min_length=1)
    normalized_title: str | None = None
    start_page: int = Field(..., ge=1)
    end_page: int | None = Field(default=None, ge=1)
    parent_id: str | None = None


class StructureSplit(BaseModel):
    split_page: int = Field(..., ge=1)
    label_prefix: str | None = Field(default=None, min_length=1)


class StructureMerge(BaseModel):
    target_id: str = Field(..., min_length=1)


class StructureMove(BaseModel):
    direction: str = Field(..., pattern="^(up|down)$")


class StructureBoundary(BaseModel):
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)


@router.post("/{project_id}/structure/nodes", status_code=201, dependencies=[Depends(require_permission("manage_structure"))])
async def create_structure_node(
    project_id: str, payload: StructureNodeCreate,
    db: Session = Depends(get_db_session),
) -> dict:
    """§5.3 'creare': a manually drawn node (editor AC3)."""
    _get_project(db, project_id)
    try:
        project = db.get(Project, project_id)
        return op_create(
            db, project, kind=payload.kind,
            source_label=payload.source_label,
            normalized_title=payload.normalized_title,
            start_page=payload.start_page,
            end_page=payload.end_page, parent_id=payload.parent_id)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/{node_id}/rename", dependencies=[Depends(require_permission("manage_structure"))])
async def rename_structure_node(
    project_id: str, node_id: str, payload: StructureNodeUpdate,
    db: Session = Depends(get_db_session),
) -> dict:
    """§5.3 'rinominare' with undo (editor AC3)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    if not (payload.source_label or payload.normalized_title):
        raise HTTPException(status_code=400, detail="missing source_label")
    label = payload.source_label or payload.normalized_title or ""
    try:
        return op_rename(db, db.get(Project, project_id), node, label)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/{node_id}/split", dependencies=[Depends(require_permission("manage_structure"))])
async def split_structure_node(
    project_id: str, node_id: str, payload: StructureSplit,
    db: Session = Depends(get_db_session),
) -> dict:
    """§5.3 'dividere': split the chapter at a page boundary (AC3)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    try:
        return op_split(db, db.get(Project, project_id), node,
                        payload.split_page, payload.label_prefix)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/{node_id}/merge", dependencies=[Depends(require_permission("manage_structure"))])
async def merge_structure_node(
    project_id: str, node_id: str, payload: StructureMerge,
    db: Session = Depends(get_db_session),
) -> dict:
    """§5.3 'unire': absorb this node into the adjacent target (AC3)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    target = _get_node(db, project_id, payload.target_id)
    try:
        return op_merge(db, db.get(Project, project_id), node, target)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/{node_id}/move", dependencies=[Depends(require_permission("manage_structure"))])
async def move_structure_node(
    project_id: str, node_id: str, payload: StructureMove,
    db: Session = Depends(get_db_session),
) -> dict:
    """§5.3 'spostare': swap the chapter with its neighbour (AC3)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    try:
        return op_move(db, db.get(Project, project_id), node,
                       payload.direction)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.delete("/{project_id}/structure/{node_id}", dependencies=[Depends(require_permission("manage_structure"))])
async def delete_structure_node(
    project_id: str, node_id: str, db: Session = Depends(get_db_session),
) -> dict:
    """Discard a proposal / remove a manual node (undoable)."""
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    try:
        return op_delete(db, db.get(Project, project_id), node)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/{node_id}/boundary", dependencies=[Depends(require_permission("manage_structure"))])
async def set_structure_boundary(
    project_id: str, node_id: str, payload: StructureBoundary,
    db: Session = Depends(get_db_session),
) -> dict:
    """Correct a chapter's start/end page (§15.1, undoable).

    The boundary change alone does NOT touch any segment: schedule the
    selective ``resegment`` afterwards so only the chapters whose derived
    range changed are re-segmented.
    """
    _get_project(db, project_id)
    node = _get_node(db, project_id, node_id)
    if payload.start_page is None and payload.end_page is None:
        raise HTTPException(status_code=400,
                            detail="start_page or end_page required")
    try:
        return op_set_boundary(db, db.get(Project, project_id), node,
                               start_page=payload.start_page,
                               end_page=payload.end_page)
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/undo", dependencies=[Depends(require_permission("manage_structure"))])
async def undo_structure_edit(
    project_id: str, db: Session = Depends(get_db_session),
) -> dict:
    """Undo the last structure edit (AC3: operativi con undo)."""
    _get_project(db, project_id)
    try:
        return op_undo(db, db.get(Project, project_id))
    except EditorError as exc:
        raise _editor_error(exc) from exc


@router.post("/{project_id}/structure/resegment", status_code=202, dependencies=[Depends(require_permission("manage_structure"))])
async def resegment_structure(
    project_id: str, node_id: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """§12.2: regenerate ONLY the dependent segments (§15.1).

    Without ``node_id`` the job re-segments exactly the chapters whose
    derived page range changed in the last structure edit; with
    ``node_id`` it (re-)segments that one chapter.  Approved translations
    are never rewritten (§5.1).
    """
    _get_project(db, project_id)
    if node_id is not None:
        _get_node(db, project_id, node_id)
    svc_scheduler = get_scheduler_from(db)
    payload: dict = {"project_id": str(project_id)}
    if node_id is not None:
        payload["node_id"] = str(node_id)
    job = svc_scheduler.register(project_id, "resegment_structure", payload)
    svc_scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
        "node_id": str(node_id) if node_id else None,
    }


# --- cleaning (header/footer) ---------------------------------------------
def _cleaning_candidates(db: Session, document: Document) -> dict:
    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == document.id)
        .one_or_none()
    )
    repeated = list(
        (report.summary or {}).get("repeated_headers_footers") or []) \
        if report is not None else []
    total_pages = document.page_count or 0
    confirmed = structure.confirm_repeated_lines(repeated, total_pages)
    confirmed_keys = {c["text"] for c in confirmed}
    return {
        "candidates": [
            {
                "text": item.get("text"),
                "pages": item.get("pages"),
                "algorithmically_confirmed": item.get("text") in
                confirmed_keys,
            }
            for item in repeated
        ],
        "total_pages": total_pages,
    }


@router.get(
    "/{project_id}/documents/{document_id}/cleaning/preview")
async def cleaning_preview(
    project_id: str, document_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """§5.3 header/footer preview: candidates + confirmation + snapshot."""
    _get_project(db, project_id)
    doc = _get_document(db, project_id, document_id)
    candidates = _cleaning_candidates(db, doc)
    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
        .all()
    )
    applied_keys = set()
    for p in pages:
        applied_keys.update(
            (p.page_payload or {}).get("cleaning", {}).get("applied") or [])
    return {
        "document_id": str(document_id),
        "total_pages": candidates["total_pages"],
        "candidates": candidates["candidates"],
        "applied": sorted(applied_keys),
        "rollback_available": any(
            (p.page_payload or {}).get("cleaning", {}).get("snapshot")
            for p in pages),
    }


@router.post(
    "/{project_id}/documents/{document_id}/cleaning/apply",
    dependencies=[Depends(require_permission("manage_structure"))])
async def cleaning_apply(
    project_id: str, document_id: str, payload: CleaningApply,
    db: Session = Depends(get_db_session),
) -> dict:
    """Delete the user-confirmed headers/footers (§5.3 + §15.1).

    Every requested key must be algorithmically confirmed first (§5.3:
    "dopo conferma algoritmica su almeno N pagine"); the apply stores a
    rollback snapshot per page and re-derives the normalised text with the
    flagged blocks excluded.
    """
    _get_project(db, project_id)
    doc = _get_document(db, project_id, document_id)
    candidates = _cleaning_candidates(db, doc)
    confirmed_keys = {
        c["text"] for c in candidates["candidates"]
        if c["algorithmically_confirmed"]}
    unknown = sorted(set(payload.texts) - confirmed_keys)
    if unknown:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not algorithmically confirmed (§5.3)",
                "keys": unknown,
            })

    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
        .all()
    )
    wanted = set(payload.texts)
    touched_pages = 0
    excluded_blocks_total = 0
    for page_row in pages:
        page_payload = dict(page_row.page_payload or {})
        # snapshot BEFORE the first change on this page (rollback preview)
        cleaning_state = dict(page_payload.get("cleaning") or {})
        if not cleaning_state.get("snapshot"):
            pre_text = page_row.normalized_text or ""
            cleaning_state["snapshot"] = {
                "text": pre_text,
                "sha256": structure.sha256_json({"text": pre_text}),
            }
        excluded = 0
        if page_row.extractor == "pymupdf" and \
                page_payload.get("blocks") is not None:
            from .parsing import l1  # local import: reuse the L1 exclusion

            excluded = l1.apply_exclusions(
                page_payload, {k: 1 for k in wanted})
            excluded_blocks_total += excluded
        else:
            lines = (page_payload.get("ocr_record") or {}).get("lines") or []
            for ln in lines:
                if structure.repeated_line_key(ln.get("text", "")) in wanted:
                    if not ln.get("excluded"):
                        ln["excluded"] = True
                        excluded += 1
            if excluded:
                kept = [ln["text"] for ln in lines if not ln.get("excluded")]
                page_payload["ocr_record"] = {
                    **page_payload["ocr_record"],
                    "normalized_text": structure.dehyphenate(
                        " ".join(kept), mode="ocr"),
                }
                page_row.normalized_text = \
                    page_payload["ocr_record"]["normalized_text"]
        applied = set(cleaning_state.get("applied") or []) | wanted
        cleaning_state["applied"] = sorted(applied)
        page_payload["cleaning"] = cleaning_state
        page_row.page_payload = page_payload
        if excluded:
            from .parsing import l1 as _l1  # hash refresh for L1 pages

            page_row.text_sha256 = _l1.sha256_json(
                {"text": page_row.normalized_text})
            page_row.page_sha256 = _l1.sha256_json(page_payload)
        touched_pages += 1 if excluded else 0
    db.add(AuditLog(
        project_id=project_id,
        action="cleaning_applied",
        entity="document",
        entity_id=str(document_id),
        after={"keys": sorted(wanted), "pages": touched_pages,
               "excluded_blocks": excluded_blocks_total},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {
        "document_id": str(document_id),
        "applied": sorted(wanted),
        "pages_affected": touched_pages,
        "excluded_blocks": excluded_blocks_total,
        "rollback_available": True,
    }


@router.post(
    "/{project_id}/documents/{document_id}/cleaning/rollback",
    dependencies=[Depends(require_permission("manage_structure"))])
async def cleaning_rollback(
    project_id: str, document_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Restore the pre-cleaning text of every page from the snapshots."""
    _get_project(db, project_id)
    _get_document(db, project_id, document_id)
    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
        .all()
    )
    restored = 0
    for page_row in pages:
        page_payload = dict(page_row.page_payload or {})
        cleaning_state = dict(page_payload.get("cleaning") or {})
        snapshot = cleaning_state.get("snapshot")
        if not snapshot:
            continue
        page_row.normalized_text = structure.roll_back(
            page_row.normalized_text or "", snapshot)
        # un-exclude the blocks the cleaning pass had flagged
        if page_payload.get("blocks"):
            for block in page_payload["blocks"]:
                block.pop("excluded", None)
        ocr_record = page_payload.get("ocr_record")
        if ocr_record:
            for ln in ocr_record.get("lines") or []:
                ln.pop("excluded", None)
        cleaning_state["applied"] = []
        page_payload["cleaning"] = cleaning_state
        page_row.page_payload = page_payload
        from .parsing import l1 as _l1

        page_row.text_sha256 = _l1.sha256_json(
            {"text": page_row.normalized_text})
        page_row.page_sha256 = _l1.sha256_json(page_payload)
        restored += 1
    db.add(AuditLog(
        project_id=project_id,
        action="cleaning_rolled_back",
        entity="document",
        entity_id=str(document_id),
        after={"pages_restored": restored},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {"document_id": str(document_id), "pages_restored": restored}
