"""Run the full QA pass for a project (PRD §10.2 / §10.3 / §12.4 / AC1-3).

:func:`run_qa_for_project` is the single entry point behind
``POST /api/v1/projects/{id}/qa/run`` (and the ``qa`` job type). It runs the
three levels in turn and persists their output:

1. **deterministic** (§10.2 / AC1) -- every segment is checked; each failure
   becomes a ``qa_issues`` row (category / severity / evidence / message);
2. **reference-free QE** (§10.3 / AC2) -- each segment gets a ``score`` that
   is written to ``translation_units.quality_score`` and used only to
   *prioritise* review (§10.3, never a gate);
3. **critic** (§10.3 / AC3) -- each segment gets structured, schema-validated
   errors, each a *suggestion* about what to fix (never a rewrite).

Issues are de-duplicated per ``(unit_id, category, evidence)`` so a segment
flagged by both the deterministic suite and the critic keeps one row per
category. Every mutation is audited (§13.1).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..audit import log_event
from ..models import (
    Entity,
    GlossaryTerm,
    Project,
    QaIssue,
    TranslationUnit,
)
from . import categories as C
from .critic import run_critic_deterministic
from .deterministic import run_deterministic
from .quality_estimation import estimate_segment_quality


def _constraints_for(project_id: str, db: Session) -> dict:
    """Approved glossary / entity constraints (§10.2) for the critic + suite."""
    forbidden: set[str] = set()
    must_keep: set[str] = set()
    glossary_in: list[dict] = []
    for t in db.query(GlossaryTerm).filter(
        GlossaryTerm.project_id == project_id,
        GlossaryTerm.status == "approved",
    ).all():
        for fb in (t.forbidden_targets or []):
            if fb:
                forbidden.add(fb)
        for a in (t.target_term,) + tuple(t.forbidden_targets or []):
            if a:
                must_keep.add(a)
        glossary_in.append({
            "canonical_target": t.target_term,
            "forbidden_targets": t.forbidden_targets or [],
            "must_keep": [t.target_term] if t.preferred else [],
        })
    # solo entità APPROVATE (coerente con translation.runner._constraints_for:
    # le proposte non vincolano — decisione 2026-09-19)
    for e in db.query(Entity).filter(
        Entity.project_id == project_id,
        Entity.status == "approved",
    ).all():
        for fb in (e.forbidden_targets or []):
            if fb:
                forbidden.add(fb)
        glossary_in.append({
            "canonical_target": e.canonical_target,
            "forbidden_targets": e.forbidden_targets or [],
            "must_keep": [e.canonical_target] if e.never_translate else [],
        })
    return {"forbidden_terms": forbidden, "must_keep": must_keep,
            "entities": [], "glossary": glossary_in}


def _persist_issues(db: Session, project_id: str,
                      units: dict[str, TranslationUnit],
                      issues: list[dict]) -> int:
    """Insert each issue, de-duplicated per (unit_id, category, evidence)."""
    seen: set[tuple[str, str, str]] = set()
    count = 0
    for i in issues:
        uid = i.get("segment_id")
        if not uid:
            continue
        key = (uid, i["category"], i["evidence"] or "")
        if key in seen:
            continue
        seen.add(key)
        row = QaIssue(
            id=_uid(),
            project_id=str(project_id),
            unit_id=uid,
            severity=i["severity"],
            kind=i.get("kind", C.KIND_QA),
            category=i.get("category"),
            message=i["message"],
            evidence=i.get("evidence"),
            suggestion=i.get("suggestion"),
            resolved=False,
            created_at=datetime.utcnow(),
        )
        db.add(row)
        count += 1
    return count


def _uid() -> str:
    import uuid
    return str(uuid.uuid4())


def run_qa_for_project(db: Session, project_id: str,
                       critic_backend: str = "deterministic",
                       unit_status: tuple[str, ...] | None = None,
                       chapter_id: str | None = None
                       ) -> dict:
    """Run all three QA levels for *project_id* and persist their output.

    ``chapter_id`` (optional) restricts the run to a single chapter, so the
    suite can be executed "su capitolo" (AC1). When omitted the run covers
    every unit matching *unit_status* in the project.

    The run is **idempotent**: re-running it on the same segments replaces the
    QA layer's previous output for those segments (AC1 "issue riproducibili")
    instead of accumulating duplicates. Human MQM annotations are preserved
    (see the delete step below).

    Returns a summary dict (counts + the per-segment QE scores). Raises
    :class:`RuntimeError` when the critic LLM backend is requested but the
    proxy is unreachable (callers fall back to the deterministic backend).
    """
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id")

    query = db.query(TranslationUnit).filter(
        TranslationUnit.project_id == project_id)
    if unit_status:
        query = query.filter(TranslationUnit.status.in_(unit_status))
    if chapter_id:
        query = query.filter(TranslationUnit.chapter_id == chapter_id)
    units = {str(u.id): u for u in query.all()}

    constraints = _constraints_for(project_id, db)
    glossary = constraints.pop("glossary", [])

    # --- 1. deterministic (§10.2 / AC1) -----------------------------------
    det_segs = [
        {"segment_id": str(u.id), "source_text": u.source_text or "",
         "target_text": u.target_text or ""}
        for u in units.values() if u.source_text and u.target_text
    ]
    det_issues = run_deterministic(det_segs, constraints)

    # --- 2. reference-free QE (§10.3 / AC2) -------------------------------
    qe_scores: dict[str, float] = {}
    for u in units.values():
        if not (u.source_text and u.target_text):
            continue
        q = estimate_segment_quality(u.source_text, u.target_text)
        qe_scores[str(u.id)] = q.score
        u.quality_score = q.score  # §10.3: stored, never a gate

    # --- 3. critic (§10.3 / AC3) ------------------------------------------
    # The deterministic backend is always available (local-only §13.1); the
    # LLM backend (Gateway) can be selected explicitly and falls back to the
    # deterministic one when the proxy is unreachable (§10.3 / §12.4).
    if critic_backend == "llm":
        from .critic import run_critic_llm as _critic
    else:
        _critic = run_critic_deterministic
    critic_issues: list[dict] = []
    for u in units.values():
        if not (u.source_text and u.target_text):
            continue
        try:
            res = _critic(u.source_text, u.target_text, glossary=glossary)
        except RuntimeError:  # proxy unreachable -> fall back, never fail
            res = run_critic_deterministic(u.source_text, u.target_text,
                                           glossary=glossary)
        if not res.validated:
            continue
        for e in res.errors:
            critic_issues.append({
                "segment_id": str(u.id),
                "category": e["category"],
                "severity": e["severity"],
                "kind": C.KIND_CRITIC,
                "evidence": e["evidence"],
                "message": e["suggestion"],
                "suggestion": e["suggestion"],
            })

    # --- persist everything ------------------------------------------------
    # Replacing the QA layer's previous output for the evaluated segments makes
    # a re-run idempotent (AC1 "issue riproducibili"). Machine output is the
    # output this function produces -- it always sets ``evidence``/
    # ``suggestion`` and never ``span``/``comment`` -- so deleting exactly the
    # rows that carry neither preserves human MQM annotations (§10.4: a span
    # selection plus a free-text comment) and other producers' issues.
    if units:
        unit_ids = list(units.keys())
        stale = db.query(QaIssue).filter(
            QaIssue.unit_id.in_(unit_ids),
            QaIssue.span.is_(None),
            QaIssue.comment.is_(None),
        ).all()
        for s in stale:
            db.delete(s)
    all_issues = det_issues + critic_issues
    n = _persist_issues(db, project_id, units, all_issues)
    db.commit()
    for u in units.values():
        db.add(u)
    db.commit()

    # --- audit + summary ---------------------------------------------------
    log_event("qa_run", "project", db=db,
              user_id=None, project_id=str(project_id),
              entity_id=str(project_id),
              after={"deterministic_issues": len(det_issues),
                     "critic_issues": len(critic_issues),
                     "issues_persisted": n,
                     "segments_evaluated": len(qe_scores)})
    db.commit()

    return {
        "project_id": str(project_id),
        "deterministic_issues": len(det_issues),
        "critic_issues": len(critic_issues),
        "issues_persisted": n,
        "segments_evaluated": len(qe_scores),
        "qe_scores": {k: round(v, 4) for k, v in qe_scores.items()},
    }
