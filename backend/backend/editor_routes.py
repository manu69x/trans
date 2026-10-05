"""Editor bilingue surface (PRD §11.2, §11.3, §10.5 / F3 task t_6c5c528a).

Implements the HTTP behind the 3-panel editor on top of ``translation_units``
and the new :class:`~backend.models.TranslationUnitVersion` /
:class:`~backend.models.QaIssue` tables:

* ``POST /projects/{id}/segments/{id}/approve`` -- approve (already in
  structure_routes, mirrored here for the editor's Ctrl+Enter);
* ``POST /projects/{id}/segments/{id}/reject`` -- reject a segment;
* ``POST /projects/{id}/segments/{id}/refine`` -- refine an editable target,
  recording the pre-change state on ``translation_unit_versions`` (§10.5:
  "diff prima dell'applicazione, entrambe le versioni conservate");
* ``GET  /projects/{id}/segments/{id}/versions`` -- the immutable version
  history (side-by-side) of one segment;
* ``GET  /projects/{id}/qa/issues`` -- the project's QA issues with filters
  (severity/kind/resolved) backing the "solo QA critici" filter (§11.2);
* ``POST /projects/{id}/qa/issues`` -- seed/derive QA issues from the
  deterministic validators (§10.1.5-9 / §10.2) and the OCR/entity flags;
* ``POST /projects/{id}/search`` -- find/replace with a scope
  (segment/chapter/project) and a preview of each affected target.

Every mutation is audited (§13.1).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from difflib import SequenceMatcher

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db_session
from .rbac import require_permission
from .models import (
    AuditLog,
    Project,
    QaIssue,
    TranslationUnit,
    TranslationUnitVersion,
)
from . import translation as _translation  # noqa: F401  (ensure pkg is imported)
from .qa import categories as mqm  # noqa: E402  (taxonomy §10.4)

router = APIRouter(prefix="/projects", tags=["editor"])


# --- helpers ----------------------------------------------------------------
def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _get_unit(db: Session, project_id: str, unit_id: str) -> TranslationUnit:
    unit = (
        db.query(TranslationUnit)
        .filter(
            TranslationUnit.id == unit_id,
            TranslationUnit.project_id == project_id,
        )
        .one_or_none()
    )
    if unit is None:
        raise HTTPException(status_code=404, detail="segment not found")
    return unit


def _audit(db, project_id, action, entity, entity_id=None, before=None,
           after=None):
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


def _qe_out(unit: TranslationUnit) -> dict:
    """Punteggi QE del segmento + DIFF = is_italian - is_english."""
    it = float(unit.is_italian) if unit.is_italian is not None else None
    en = float(unit.is_english) if unit.is_english is not None else None
    diff = round(it - en, 4) if it is not None and en is not None else None
    return {
        "is_italian": it,
        "is_translated": (float(unit.is_translated)
                          if unit.is_translated is not None else None),
        "is_english": en,
        "qe_diff": diff,
    }


def _unit_out(unit: TranslationUnit) -> dict:
    flags = dict(unit.source_flags or {})
    return {
        "segment_id": str(unit.id),
        "project_id": str(unit.project_id),
        "chapter_id": str(unit.chapter_id) if unit.chapter_id else None,
        "ordinal": unit.ordinal,
        "source_text": unit.source_text,
        "target_text": unit.target_text,
        "status": unit.status,
        "source_hash": unit.source_hash,
        "source_flags": flags,
        "has_markup": bool(flags.get("has_markup")),
        "page": flags.get("page"),
        "numero": unit.numero,
        **_qe_out(unit),
    }


# --- word-level diff (pure) -------------------------------------------------
def _word_diff(a: str, b: str) -> dict:
    """A minimal tokenised diff of (a -> b) as an ``added``/``removed`` list.

    Used by ``refine`` to build the ``diff_json`` the editor shows before
    applying (§10.5). The diff is a token stream: each item is
    ``{"op": "unchanged" | "removed" | "added", "text": ...}``.
    """
    if not a and not b:
        return {"ops": []}

    def _tokens(text: str) -> list[str]:
        # Split keeping whitespace as its own tokens so the reconstruction is
        # exact; fall back to characters when the text is empty of "words".
        if not text:
            return []
        parts = re.split(r"(\s+)", text)
        return [p for p in parts if p != ""]

    ta = _tokens(a)
    tb = _tokens(b)
    sm = SequenceMatcher(None, ta, tb)
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                ops.append({"op": "unchanged", "text": ta[k]})
        elif tag == "replace":
            for k in range(i1, i2):
                ops.append({"op": "removed", "text": ta[k]})
            for k in range(j1, j2):
                ops.append({"op": "added", "text": tb[k]})
        elif tag == "delete":
            for k in range(i1, i2):
                ops.append({"op": "removed", "text": ta[k]})
        elif tag == "insert":
            for k in range(j1, j2):
                ops.append({"op": "added", "text": tb[k]})
    return {"ops": ops}


# --- approve (mirror of structure_routes, the editor's Ctrl+Enter) ----------
class SegmentApprove(BaseModel):
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

    Mirrors :func:`backend.structure_routes.approve_segment` so the editor's
    Ctrl+Enter shortcut has a single, audited entry point (§11.2 / AC1).
    """
    _get_project(db, project_id)
    unit = _get_unit(db, project_id, segment_id)
    if unit.status == "approved":
        from .models import TranslationMemoryEntry
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
    from .translation import embedding
    from .models import TranslationMemoryEntry, Project as _P
    project = db.get(_P, project_id)
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
    unit.status = "approved"
    unit.updated_at = datetime.utcnow()
    db.add(entry)
    _audit(db, project_id, "segment_approved", "segment", str(segment_id),
           after={"status": "approved"})
    return {
        "segment_id": str(segment_id),
        "status": "approved",
        "tm_entry_id": str(entry.id),
        "created": True,
    }


# --- reject -----------------------------------------------------------------
class SegmentReject(BaseModel):
    reason: str | None = None


@router.post("/{project_id}/segments/{segment_id}/reject", status_code=200, dependencies=[Depends(require_permission("approve_segments"))])
async def reject_segment(
    project_id: str,
    segment_id: str,
    payload: SegmentReject,
    db: Session = Depends(get_db_session),
) -> dict:
    """Reject a segment: it returns to ``untranslated`` (or ``machine_draft``).

    A rejected segment keeps its ``machine_draft`` target so the editor can
    re-run translation; it is no longer ``approved`` and is not in the TM.
    """
    _get_project(db, project_id)
    unit = _get_unit(db, project_id, segment_id)
    if unit.status == "approved":
        raise HTTPException(
            status_code=409,
            detail="an approved segment cannot be rejected; "
                   "refine it instead (§5.1)")
    # Rejecting a draft keeps the draft target; rejecting an untranslated
    # segment keeps it untranslated.
    new_status = "machine_draft" if unit.status == "machine_draft" else "untranslated"
    unit.status = new_status
    unit.updated_at = datetime.utcnow()
    _audit(db, project_id, "segment_rejected", "segment", str(segment_id),
           before={"status": unit.status},
           after={"status": new_status, "reason": payload.reason})
    return {
        "segment_id": str(segment_id),
        "status": new_status,
        "reason": payload.reason,
    }


# --- verifica QE massiva (§10.2-bis, modello Open-QE su GPU0) -------------
@router.get("/verify-translations/status")
async def verify_translations_status() -> dict:
    """Stato del servizio QE (Open-QE su GPU0) per la scheda Prompt/Modelli."""
    from . import qe_client

    try:
        info = qe_client.health()
    except Exception as exc:  # noqa: BLE001
        info = {"reachable": False, "error": str(exc)[:160]}
    return {"base_url": qe_client._base_url(), "service": info}


@router.post("/{project_id}/verify-translations", status_code=202, dependencies=[Depends(require_permission("review_segments"))])
async def start_verify_translations(
    project_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_db_session),
) -> dict:
    """Start QE verification on the project's translated segments.

    With ``segment_ids`` in the body, verify ONLY those (the frontend button
    acts on the current selection, like the other bulk buttons); without,
    every segment that has a ``target_text``.

    For each segment with a ``target_text`` the QE service computes:
    * ``is_italian``    -- p(yes) to  "Is this text written in Italian?"
    * ``is_translated`` -- p(yes) to  "Is the following text the Italian
      translation of this text?"
    * ``is_english``    -- p(yes) to  "Is this text written in English?"

    Values are written to the ``translation_units`` fields and shown on the
    Translation page (§11.2).
    """
    from .scheduler import get_scheduler_from

    _get_project(db, project_id)
    segment_ids = (payload or {}).get("segment_ids") or []
    if not isinstance(segment_ids, list) or \
            not all(isinstance(s, str) for s in segment_ids):
        segment_ids = []
    scheduler = get_scheduler_from(db)
    job = scheduler.register(project_id, "verify_translations",
                             {"project_id": str(project_id),
                              "segment_ids": segment_ids})
    scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }


# --- refine (§10.5) ---------------------------------------------------------
class SegmentRefine(BaseModel):
    """Body for ``POST .../segments/{id}/refine``.

    ``target_text`` is the human's edited target. The pre-change target is
    frozen on ``translation_unit_versions`` (``before=True``); the new target
    becomes live (``before=False``). The diff (a token stream) is computed so
    the editor can render it before applying.
    """
    target_text: str = Field(..., min_length=0)
    reason: str | None = None


@router.post("/{project_id}/segments/{segment_id}/refine", status_code=200, dependencies=[Depends(require_permission("review_segments"))])
async def refine_segment(
    project_id: str,
    segment_id: str,
    payload: SegmentRefine,
    db: Session = Depends(get_db_session),
) -> dict:
    """Refine an editable segment, recording the pre-change state (§10.5).

    Only ``machine_draft`` / ``untranslated`` segments can be refined
    (§5.1: approved segments are immutable). The diff is shown before the
    change is applied, and both versions are conserved.
    """
    _get_project(db, project_id)
    unit = _get_unit(db, project_id, segment_id)
    if unit.status == "approved":
        raise HTTPException(
            status_code=409,
            detail="an approved segment is immutable; refine a draft "
                   "instead (§5.1)")
    if unit.status == "untranslated" and unit.target_text:
        # first edit of a still-untranslated segment: freeze the existing
        # target as a "before" version so the history is not empty.
        pass

    before_text = unit.target_text or ""
    new_text = payload.target_text
    diff = _word_diff(before_text, new_text)
    # freeze the pre-change state (before=True); the new state that became
    # live is recorded too (before=False) so the history is complete.
    db.add(TranslationUnitVersion(
        id=str(uuid.uuid4()),
        project_id=project_id,
        unit_id=segment_id,
        target_text=before_text if before_text != new_text else new_text,
        before=True,
        action="refine",
        diff_json=diff,
        created_at=datetime.utcnow(),
    ))
    db.add(TranslationUnitVersion(
        id=str(uuid.uuid4()),
        project_id=project_id,
        unit_id=segment_id,
        target_text=new_text,
        before=False,
        action="refine",
        diff_json=diff,
        created_at=datetime.utcnow(),
    ))

    unit.target_text = new_text
    unit.updated_at = datetime.utcnow()
    db.add(unit)
    _audit(db, project_id, "segment_refined", "segment", str(segment_id),
           before={"target_text": before_text},
           after={"target_text": new_text})
    return {
        "segment_id": str(segment_id),
        "status": unit.status,
        "target_text": new_text,
        "diff": diff,
        "before_text": before_text,
    }


# --- version history (side-by-side) -----------------------------------------
@router.get("/{project_id}/segments/{segment_id}/versions")
async def list_segment_versions(
    project_id: str,
    segment_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """The immutable version history of one segment (§11.2 / §10.5)."""
    _get_project(db, project_id)
    _get_unit(db, project_id, segment_id)
    rows = (
        db.query(TranslationUnitVersion)
        .filter(
            TranslationUnitVersion.unit_id == segment_id,
            TranslationUnitVersion.project_id == project_id,
        )
        .order_by(TranslationUnitVersion.created_at)
        .all()
    )
    versions = [
        {
            "id": str(v.id),
            "before": v.before,
            "target_text": v.target_text,
            "action": v.action,
            "diff": v.diff_json or {},
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in rows
    ]
    return {
        "segment_id": str(segment_id),
        "history": versions,
        "count": len(versions),
    }


# --- QA issues --------------------------------------------------------------
@router.get("/{project_id}/qa/issues")
async def list_qa_issues(
    project_id: str,
    severity: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    category: str | None = Query(default=None),
    group: str | None = Query(default=None),
    resolved: bool | None = Query(default=None),
    chapter_id: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """The project's QA issues, filtered (§11.1: severità/capitolo/tipo).

    ``group`` narrows the filter to the §10.4 category group
    (``accuracy`` / ``terminology`` / ``italian`` / ``style`` / ``locale`` /
    ``source``); ``kind`` ``human`` selects the human MQM annotations (§10.4)
    produced on the QA page (AC1).
    """
    _get_project(db, project_id)
    query = db.query(QaIssue).filter(
        QaIssue.project_id == project_id)
    if severity:
        query = query.filter(QaIssue.severity == severity)
    if kind:
        query = query.filter(QaIssue.kind == kind)
    if category:
        query = query.filter(QaIssue.category == category)
    if group:
        cats = mqm.CATEGORY_GROUPS.get(group)
        if cats is None:
            raise HTTPException(status_code=404,
                                detail="unknown category group")
        query = query.filter(QaIssue.category.in_(list(cats)))
    if resolved is not None:
        query = query.filter(QaIssue.resolved == resolved)
    if chapter_id:
        query = query.join(
            TranslationUnit, QaIssue.unit_id == TranslationUnit.id
        ).filter(TranslationUnit.chapter_id == chapter_id)
    rows = query.order_by(QaIssue.created_at).all()
    out = []
    for r in rows:
        out.append({
            "id": str(r.id),
            "segment_id": str(r.unit_id),
            "severity": r.severity,
            "kind": r.kind,
            "category": r.category,
            "message": r.message,
            "evidence": r.evidence,
            "suggestion": r.suggestion,
            "span": r.span,
            "comment": r.comment,
            "resolved": r.resolved,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {
        "project_id": str(project_id),
        "issues": out,
        "critical_unresolved": sum(
            1 for i in out
            if i["severity"] == "critical" and not i["resolved"]),
    }


class QaIssueCreate(BaseModel):
    """Body for ``POST /projects/{id}/qa/issues`` (human MQM annotation).

    ``severity`` follows the §10.4 scale ``minor`` / ``major`` / ``critical``
    and ``category`` the MQM taxonomy (§10.4): the revisor selects a span in
    the target, picks a category and a severity, and adds a free-text
    ``comment`` (AC1/AC2/AC3). ``unit_id`` links the issue to a segment
    (§15.4 / AC3).
    """

    unit_id: str
    severity: str = Field(default="minor",
                          pattern="^(minor|major|critical)$")
    kind: str = Field(
        default="human",
        pattern="^(human|qa|critic|qe|ocr|entity)$",
        description="``human`` = MQM annotation of the revisor (AC1).")
    category: str | None = Field(
        default=None,
        pattern="^" + "|".join(
            re.escape(c) for c in sorted(mqm.CATEGORIES)) + "$",
        description="MQM taxonomy category (§10.4).")
    message: str = Field(..., min_length=1)
    span: str | None = None
    comment: str | None = None


@router.post("/{project_id}/qa/issues", status_code=201, dependencies=[Depends(require_permission("annotate_qm"))])
async def create_qa_issue(
    project_id: str,
    payload: QaIssueCreate,
    db: Session = Depends(get_db_session),
) -> dict:
    """Create a QA issue on a segment (§11.2 / §10.4)."""
    _get_project(db, project_id)
    _get_unit(db, project_id, payload.unit_id)
    issue = QaIssue(
        id=str(uuid.uuid4()),
        project_id=project_id,
        unit_id=payload.unit_id,
        severity=payload.severity,
        kind=payload.kind,
        category=payload.category,
        message=payload.message,
        span=payload.span,
        comment=payload.comment,
        resolved=False,
        created_at=datetime.utcnow(),
    )
    db.add(issue)
    _audit(db, project_id, "qa_issue_created", "qa_issue", str(issue.id),
           after={"severity": issue.severity, "kind": issue.kind,
                  "category": issue.category})
    return {
        "id": str(issue.id),
        "segment_id": str(issue.unit_id),
        "severity": issue.severity,
        "kind": issue.kind,
        "category": issue.category,
        "message": issue.message,
        "span": issue.span,
        "comment": issue.comment,
        "resolved": False,
    }


@router.post("/{project_id}/qa/issues/{issue_id}/resolve", status_code=200, dependencies=[Depends(require_permission("annotate_qm"))])
async def resolve_qa_issue(
    project_id: str,
    issue_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Mark a QA issue resolved (or un-resolved, toggling)."""
    _get_project(db, project_id)
    issue = (
        db.query(QaIssue)
        .filter(QaIssue.id == issue_id, QaIssue.project_id == project_id)
        .one_or_none()
    )
    if issue is None:
        raise HTTPException(status_code=404, detail="QA issue not found")
    issue.resolved = not issue.resolved
    _audit(db, project_id, "qa_issue_resolved" if issue.resolved
           else "qa_issue_unresolved", "qa_issue", str(issue.id),
           after={"resolved": issue.resolved})
    return {"id": str(issue.id), "resolved": issue.resolved}


@router.get("/{project_id}/qa/taxonomy")
async def qa_taxonomy(
    project_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """The §10.4 MQM taxonomy (category groups, categories, severities).

    The QA page uses it to render the category/severity pickers (AC1) so the
    vocabulary stays single-sourced in :mod:`backend.qa.categories`.
    """
    _get_project(db, project_id)
    return {
        "groups": mqm.CATEGORY_GROUPS,
        "categories": mqm.CATEGORIES,
        "severities": list(mqm.SEVERITIES),
    }


# --- run the full QA pass (§10.2 / §10.3 / §12.4 / AC1-3) ------------------
class QaRunRequest(BaseModel):
    """Body for ``POST /projects/{id}/qa/run`` (§12.4).

    ``critic_backend`` selects the critic backend (``deterministic`` is the
    default; ``llm`` uses Gateway and falls back automatically when the proxy
    is unreachable). ``unit_status`` restricts the pass to these
    ``translation_units`` statuses (default: drafts that still need review).
    ``chapter_id`` (optional) restricts the pass to a single chapter, so the
    suite can be executed "su capitolo" (AC1).
    """

    critic_backend: str = Field(
        default="deterministic",
        pattern="^(deterministic|llm)$")
    unit_status: list[str] = Field(
        default_factory=lambda: ["machine_draft", "untranslated"])
    chapter_id: str | None = Field(
        default=None, description="Restrict the run to one chapter UUID.")


@router.post("/{project_id}/qa/run", status_code=200, dependencies=[Depends(require_permission("review_segments"))])
async def run_qa(
    project_id: str,
    payload: QaRunRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Run the three-level QA pass for a project (§10.2 / §10.3 / §12.4).

    Persists each issue on ``qa_issues`` (category / severity / evidence /
    suggestion), writes each segment's reference-free QE ``score`` to
    ``translation_units.quality_score`` (§10.3, a priority signal only) and
    audits the run (§13.1). Returns the per-level counts and the QE scores.
    """
    _get_project(db, project_id)
    from .qa.runner import run_qa_for_project
    try:
        summary = run_qa_for_project(
            db, project_id,
            critic_backend=payload.critic_backend,
            unit_status=tuple(payload.unit_status),
            chapter_id=payload.chapter_id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"QA unavailable: {exc}") from exc
    return {
        "project_id": str(project_id),
        **summary,
    }


# --- MQM report per 1,000 words (§18.2 / AC2) -------------------------------
@router.get("/{project_id}/qa/mqm", status_code=200)
async def mqm_report(
    project_id: str,
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    resolved: bool | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """MQM report: issue counts per category and severity, normalised to
    1,000 source words (§18.2 / AC2).

    Each issue is linked to a segment (``unit_id``); the denominator is the
    number of source words across the project's evaluated segments, so the
    "MQM errors per 1,000 words, per category and severity" editorial metric
    (§18.2 / §18.1) can be computed from the same ``qa_issues`` rows.
    """
    _get_project(db, project_id)
    query = db.query(QaIssue).filter(
        QaIssue.project_id == project_id)
    if category:
        query = query.filter(QaIssue.category == category)
    if severity:
        query = query.filter(QaIssue.severity == severity)
    if resolved is not None:
        query = query.filter(QaIssue.resolved == resolved)
    rows = query.all()

    # denominator: source words across the project's segments.
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id)
        .all()
    )

    def _words(text: str | None) -> int:
        if not text:
            return 0
        return len(text.split())

    n_source_words = sum(_words(u.source_text) for u in units) or 0

    # counts per (category, severity), keeping the raw totals too.
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_category_severity: dict[str, dict[str, int]] = {}
    for r in rows:
        cat = r.category or "unspecified"
        sev = r.severity or "minor"
        by_category[cat] = by_category.get(cat, 0) + 1
        by_severity[sev] = by_severity.get(sev, 0) + 1
        bucket = by_category_severity.setdefault(cat, {})
        bucket[sev] = bucket.get(sev, 0) + 1

    def _per_1000(count: int) -> float:
        if n_source_words == 0:
            return 0.0
        return round(count / n_source_words * 1000, 4)

    return {
        "project_id": str(project_id),
        "n_source_words": n_source_words,
        "total_issues": len(rows),
        "per_1000_all": _per_1000(len(rows)),
        "by_category": by_category,
        "by_severity": by_severity,
        "by_category_severity": {
            cat: {sev: _per_1000(cnt) for sev, cnt in sevs.items()}
            for cat, sevs in by_category_severity.items()
        },
    }


# --- find / replace with scope ----------------------------------------------
class SearchRequest(BaseModel):
    """Body for ``POST /projects/{id}/search``.

    ``scope`` is ``segment`` / ``chapter`` / ``project``. ``chapter_id`` must
    be supplied when ``scope`` is ``chapter``. ``replace_all`` previews (and
   , when ``apply`` is true, performs) every replacement.
    """
    query: str = Field(..., min_length=1)
    replace_with: str = ""
    scope: str = Field(default="project",
                       pattern="^(segment|chapter|project)$")
    chapter_id: str | None = None
    case_sensitive: bool = True
    apply: bool = False


def _match(haystack: str, needle: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return needle in haystack
    return needle.lower() in haystack.lower()


def _replace(haystack: str, needle: str, repl: str, case_sensitive: bool) -> str:
    if case_sensitive:
        return haystack.replace(needle, repl)
    import re
    return re.sub(
        re.escape(needle), repl, haystack,
        flags=re.IGNORECASE)


@router.post("/{project_id}/search", status_code=200)
async def search_replace(
    project_id: str,
    payload: SearchRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Find/replace with a scope and a preview of each affected target.

    With ``apply`` false the response is a preview (no writes); with
    ``apply`` true each affected ``target_text`` is rewritten and each change
    is audited (§13.1). Approvals are never rewritten (§5.1).
    """
    _get_project(db, project_id)
    q = db.query(TranslationUnit).filter(
        TranslationUnit.project_id == project_id)
    if payload.scope == "chapter":
        if not payload.chapter_id:
            raise HTTPException(
                status_code=400, detail="chapter_id is required for scope=chapter")
        q = q.filter(TranslationUnit.chapter_id == payload.chapter_id)
    units = q.order_by(TranslationUnit.ordinal).all()

    replaced: list[dict] = []
    readonly: list[dict] = []
    for u in units:
        # §5.1: gli approved NON vengono riscritti, ma la ricerca deve
        # TROVARLI (2026-10-01: con il progetto approvato in massa la
        # ricerca restituiva sempre 0 occorrenze). L'utente vede le
        # occorrenze con un avviso; il replace vero e' consentito solo
        # su machine_draft / untranslated.
        if u.status == "approved":
            target = u.target_text or ""
            if _match(target, payload.query, payload.case_sensitive):
                readonly.append({
                    "segment_id": str(u.id),
                    "ordinal": u.ordinal,
                    "old": target,
                    "new": target,
                })
            continue
        target = u.target_text or ""
        if not _match(target, payload.query, payload.case_sensitive):
            continue
        if not payload.apply:
            replaced.append({
                "segment_id": str(u.id),
                "ordinal": u.ordinal,
                "old": target,
                "new": _replace(target, payload.query, payload.replace_with,
                                payload.case_sensitive),
            })
            continue
        new_target = _replace(target, payload.query, payload.replace_with,
                              payload.case_sensitive)
        if new_target != target:
            u.target_text = new_target
            u.updated_at = datetime.utcnow()
            db.add(u)
            _audit(db, project_id, "search_replace", "segment", str(u.id),
                   before={"target_text": target},
                   after={"target_text": new_target})
        replaced.append({
            "segment_id": str(u.id),
            "ordinal": u.ordinal,
            "old": target,
            "new": new_target,
        })
    return {
        "project_id": str(project_id),
        "scope": payload.scope,
        "query": payload.query,
        "replace_with": payload.replace_with,
        "applied": payload.apply,
        "matches": len(replaced) + len(readonly),
        "replacements": replaced,
        # Occorrenze in segmenti approvati (§5.1: mai riscritti): la UI le
        # mostra come sola lettura con il motivo.
        "readonly_matches": len(readonly),
        "readonly_replacements": readonly,
    }


# --- segments list (for the editor's left pane + filters) -------------------
@router.get("/{project_id}/segments")
async def list_segments(
    project_id: str,
    chapter_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    only_untranslated: bool | None = Query(default=None),
    only_approved: bool | None = Query(default=None),
    only_critical_qa: bool | None = Query(default=None),
    only_ocr_suspect: bool | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """The chapter's / project's segments with the editor's filters (§11.2).

    * ``only_untranslated`` -- "solo bozze" (lock of approved
      segments is implicit: approved rows are returned but flagged).
    * ``only_approved`` -- "solo approvati".
    * ``only_critical_qa`` -- "solo QA critici".
    * ``only_ocr_suspect`` -- "solo OCR sospetto".
    """
    _get_project(db, project_id)
    query = db.query(TranslationUnit).filter(
        TranslationUnit.project_id == project_id)
    if chapter_id:
        query = query.filter(TranslationUnit.chapter_id == chapter_id)
    if status:
        query = query.filter(TranslationUnit.status == status)
    if only_untranslated:
        query = query.filter(TranslationUnit.status != "approved")
    if only_approved:
        query = query.filter(TranslationUnit.status == "approved")
    if only_critical_qa:
        # critical, unresolved QA issues.
        from sqlalchemy import select
        query = query.filter(
            TranslationUnit.id.in_(
                select(QaIssue.unit_id).filter(
                    QaIssue.project_id == project_id,
                    QaIssue.severity == "critical",
                    QaIssue.resolved == False,  # noqa: E712
                )
            )
        )
    if only_ocr_suspect:
        query = query.filter(
            db.func.coalesce(
                TranslationUnit.source_flags["ocr_suspect"].as_(bool), False)
            .is_(True)
        )
    units = query.order_by(TranslationUnit.ordinal).all()

    def _with_qa(unit: TranslationUnit) -> dict:
        d = _unit_out(unit)
        issues = (
            db.query(QaIssue)
            .filter(
                QaIssue.unit_id == unit.id,
                QaIssue.resolved == False,
            )
            .all()
        )
        d["qa_critical"] = sum(
            1 for i in issues if i.severity == "critical")
        d["has_qa"] = len(issues) > 0
        return d

    out = [_with_qa(u) for u in units]
    return {
        "project_id": str(project_id),
        "chapter_id": chapter_id,
        "segments": out,
        "total": len(out),
        "approved": sum(1 for s in out if s["status"] == "approved"),
        "untranslated": sum(1 for s in out if s["status"] == "untranslated"),
        "machine_draft": sum(1 for s in out if s["status"] == "machine_draft"),
    }

# --- bulk status segmenti (approvazione / riporto in bozza massivi) ---------
class SegmentBulkStatus(BaseModel):
    segment_ids: list[str] = Field(default_factory=list)
    status: str  # "approved" | "machine_draft"


@router.post("/{project_id}/segments/bulk-status", dependencies=[Depends(require_permission("approve_segments"))])
async def bulk_segment_status(
    project_id: str,
    payload: SegmentBulkStatus,
    db: Session = Depends(get_db_session),
) -> dict:
    """Approva o riporta in bozza molti segmenti in UNA transazione (§11.2).

    * ``approved`` -- approva e registra in TM (dedupe per segmento);
    * ``machine_draft`` -- riporta in bozza (rimuove l'eventuale voce TM).
    I segmenti senza testo tradotto vengono saltati. Audit per segmento.
    """
    from .models import TranslationMemoryEntry, Project as _P
    from .translation import embedding

    if payload.status not in ("approved", "machine_draft"):
        raise HTTPException(status_code=422, detail="invalid status")
    _get_project(db, project_id)
    now = datetime.utcnow()
    updated = 0
    skipped = 0
    for sid in payload.segment_ids:
        try:
            uuid.UUID(sid)
        except (ValueError, AttributeError):
            skipped += 1
            continue
        unit = db.get(TranslationUnit, sid)
        if unit is None or str(unit.project_id) != project_id:
            skipped += 1
            continue
        if unit.status == payload.status:
            skipped += 1
            continue
        if payload.status == "machine_draft" and not (
            unit.target_text or ""
        ).strip():
            skipped += 1
            continue

        before_status = unit.status
        unit.status = payload.status
        unit.updated_at = now
        db.add(unit)

        if payload.status == "approved":
            existing = db.query(TranslationMemoryEntry).filter(
                TranslationMemoryEntry.segment_id == sid).one_or_none()
            if existing is None:
                project = db.get(_P, project_id)
                src = (unit.source_text or "")[:2000]
                db.add(TranslationMemoryEntry(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    chapter_id=unit.chapter_id,
                    segment_id=sid,
                    source_normalized=src,
                    source_original=src,
                    target_approved=unit.target_text or "",
                    genre_profile=project.genre_profile if project else None,
                    pov=None,
                    reviewer=None,
                    qa_score=None,
                    terms_used=[],
                    source_embedding=embedding.embed(src),
                    created_at=now,
                ))
        else:
            db.query(TranslationMemoryEntry).filter(
                TranslationMemoryEntry.segment_id == sid).delete()

        _audit(db, project_id, f"segment_bulk_{payload.status}",
               "segment", sid,
               before={"status": before_status},
               after={"status": payload.status})
        updated += 1
    db.commit()
    return {
        "updated": updated,
        "skipped": skipped,
        "status": payload.status,
    }
