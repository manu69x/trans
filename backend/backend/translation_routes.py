"""Translation planner API routes (F3 / PRD §5.4, §7.2-7.3, §9.3-9.4, §10.1).

Implements the block planner surface on top of the pure
:mod:`backend.translation` layer and the ``tm_entries`` / ``glossary_terms`` /
``entities`` / ``memory_snapshots`` tables:

* ``POST /projects/{id}/tm``                    -- register an approved TM
  entry (a segment becomes TM once approved).
* ``GET  /projects/{id}/tm``                    -- list TM entries.
* ``POST /projects/{id}/tm/snapshot``           -- freeze a TM snapshot.
* ``GET  /projects/{id}/tm/snapshots``          -- list TM snapshots.
* ``GET  /projects/{id}/tm/maintenance``        -- TM maintenance report.
* ``GET  /projects/{id}/tm/{snapshot_id}``      -- fetch a TM snapshot.
* ``POST /projects/{id}/translation/planner``   -- the planner: retrieve TM
  (§7.2), select entities (§7.3), build the block plan with a real token
  budget (§10.1 step 1) and the §9.4 prompt from immutable snapshots, and
  expose glossary/style conflicts (§9.3).
* ``POST /projects/{id}/translation/run``       -- enqueue the ``translate``
  job (§10.1 / §8.4 / §15.3).

Every mutation is audited (§13.1).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db_session
from .rbac import require_permission
from .models import (
    AuditLog,
    Entity,
    GlossaryTerm,
    MemorySnapshot,
    Project,
    TranslationMemoryEntry,
)
from .gateway import GatewayError, get_adapter  # noqa: F401
from .scheduler import get_scheduler_from
from .translation import embedding
from .translation import tm_retrieval  # noqa: F401
from .translation.planner import (
    BlockPlan,
    TokenBudget,
    build_block_plan,
)
from .parsing.chunking import MAX_BLOCK_TOTAL_TOKENS
from .translation.tm_retrieval import (
    DEFAULT_EXACT_THRESHOLD,
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    retrieve_tm,
)

router = APIRouter(prefix="/projects", tags=["translation"])


# --- validation sets --------------------------------------------------------
_IT_GENDERS = {"masculine", "feminine", "common", "variable", "not_applicable"}
_NUMBERS = {"singular", "plural", "invariant", "collective", "unknown"}
_STATUSES = {"proposed", "verified", "approved", "deprecated", "archived"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _get_project(db: "Session", project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _audit(db, project_id, action, entity, entity_id=None, before=None, after=None):
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


# --- schemas ----------------------------------------------------------------
class TmEntryCreate(BaseModel):
    chapter_id: str | None = None
    scene_id: str | None = None
    # F3 / PRD §7.2: the approved segment this entry came from (the approve
    # hook wires this automatically; the manual endpoint accepts it too).
    segment_id: str | None = None
    source_normalized: str = Field(..., min_length=1)
    source_original: str = Field(..., min_length=1)
    target_approved: str = Field(..., min_length=1)
    genre_profile: str | None = None
    pov: str | None = None
    reviewer: str | None = None
    qa_score: float | None = None
    terms_used: list[str] = Field(default_factory=list)
    source_embedding: list[float] | None = None


class PlannerRequest(BaseModel):
    """Body for ``POST /projects/{id}/translation/planner``.

    ``segments`` are the CAT segments that make up the block; ``block_source``
    is the concatenated source text used for budget/retrieval. ``chapter_id``
    scopes TM/glossary/entity retrieval. Retrieval thresholds are configurable
    (§7.2). ``style_guide`` (optional) feeds the §9.3 conflict detector; when
    omitted it is synthesised from the project's genre profile.
    """

    chapter_id: str | None = None
    segments: list[dict] = Field(default_factory=list)
    # vuoto = calcolato dal sorgente dei segmenti risolti (§10.1)
    block_source: str = Field(default="")
    previous_context: str = ""
    max_matches: int = Field(default=5, ge=1)
    exact_threshold: float = Field(default=DEFAULT_EXACT_THRESHOLD, ge=0.0, le=1.0)
    fuzzy_threshold: float = Field(default=DEFAULT_FUZZY_THRESHOLD, ge=0.0, le=1.0)
    semantic_threshold: float = Field(default=DEFAULT_SEMANTIC_THRESHOLD, ge=0.0, le=1.0)
    max_total_tokens: int = Field(
        default=MAX_BLOCK_TOTAL_TOKENS, ge=1024)
    style_guide: str | None = None
    snapshot_ids: dict = Field(default_factory=dict)


class TranslationRunRequest(BaseModel):
    """Body for ``POST /projects/{id}/translation/run``.

    ``segments`` are the CAT segments of the block; ``block_source`` is the
    concatenated source used for budget/retrieval. Every other field is
    optional and mirrors the ``translate`` job payload (§10.1 / §8.4):

    * ``idempotency_key`` / ``run_ref`` -- a deterministic key so a re-run of
      the same block resumes from the stored response (§8.4 / §15.3 AC3);
    * ``branch`` -- a comparable replay of a prior run (§8.4);
    * ``model`` -- an explicit model (else §8.1/§8.2 auto-resolve);
    * ``temperature`` / ``seed`` -- reproducibility (§8.4);
    * ``previous_context`` / ``snapshot_ids`` / ``max_total_tokens`` /
      ``max_output_tokens`` and the TM retrieval thresholds.
    """

    chapter_id: str | None = None
    segments: list[dict] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    # vuoto = calcolato dal sorgente dei segmenti risolti (§10.1)
    block_source: str = Field(default="")
    previous_context: str = ""
    idempotency_key: str | None = None
    run_ref: str | None = None
    branch: str | None = None
    model: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int = Field(
        default=MAX_BLOCK_TOTAL_TOKENS, ge=1024)
    style_guide: str | None = None
    snapshot_ids: dict = Field(default_factory=dict)
    exact_threshold: float = Field(default=DEFAULT_EXACT_THRESHOLD, ge=0.0, le=1.0)
    fuzzy_threshold: float = Field(default=DEFAULT_FUZZY_THRESHOLD, ge=0.0, le=1.0)
    semantic_threshold: float = Field(default=DEFAULT_SEMANTIC_THRESHOLD, ge=0.0, le=1.0)
    max_matches: int = Field(default=5, ge=1)


# --- TM entries -------------------------------------------------------------
@router.post("/{project_id}/tm", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def create_tm_entry(
    project_id: str, payload: TmEntryCreate, db: Session = Depends(get_db_session)
) -> dict:
    """Register an approved TM entry (a segment becomes TM once approved).

    The embedding is stored (§7.2: "embedding sorgente") so the pgvector
    semantic stage can run. When the caller does not provide one it is
    computed with the deterministic local embedding (offline, §7.2).
    """
    _get_project(db, project_id)
    emb = (
        [float(x) for x in payload.source_embedding]
        if payload.source_embedding is not None
        else embedding.embed(payload.source_original)
    )
    entry = TranslationMemoryEntry(
        id=_uuid(),
        project_id=project_id,
        chapter_id=payload.chapter_id,
        scene_id=payload.scene_id,
        segment_id=payload.segment_id,
        source_normalized=payload.source_normalized,
        source_original=payload.source_original,
        target_approved=payload.target_approved,
        genre_profile=payload.genre_profile,
        pov=payload.pov,
        reviewer=payload.reviewer,
        qa_score=payload.qa_score,
        terms_used=payload.terms_used,
        source_embedding=emb,
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    _audit(db, project_id, "tm_entry_created", "tm_entry", str(entry.id),
           after={"source_normalized": entry.source_normalized})
    return {
        "id": str(entry.id),
        "source_normalized": entry.source_normalized,
        "target_approved": entry.target_approved,
        "score": None,
        "method": None,
        "project_ok": True,
    }


@router.get("/{project_id}/tm")
async def list_tm_entries(
    project_id: str,
    chapter_id: str | None = Query(default=None),
    status: str | None = Query(default=None),  # noqa: ARG001 - reserved
    db: Session = Depends(get_db_session),
) -> list[dict]:
    """List the project's TM entries (optionally scoped to a chapter)."""
    _get_project(db, project_id)
    query = db.query(TranslationMemoryEntry).filter(TranslationMemoryEntry.project_id == project_id)
    if chapter_id:
        query = query.filter(TranslationMemoryEntry.chapter_id == chapter_id)
    rows = query.order_by(TranslationMemoryEntry.created_at).all()
    out = []
    for r in rows:
        d = {
            "id": str(r.id),
            "chapter_id": str(r.chapter_id) if r.chapter_id else None,
            "source_normalized": r.source_normalized,
            "source_original": r.source_original,
            "target_approved": r.target_approved,
            "genre_profile": r.genre_profile,
            "pov": r.pov,
            "reviewer": r.reviewer,
            "terms_used": list(r.terms_used or []),
            "has_embedding": r.source_embedding is not None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        # hide the raw embedding from the API surface; it is used internally.
        d["source_embedding"] = None
        out.append(d)
    return out


# --- TM snapshots -----------------------------------------------------------
@router.post("/{project_id}/tm/snapshot", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def create_tm_snapshot(
    project_id: str,
    description: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """Freeze an immutable TM snapshot (F3 / PRD §7.2 / §16)."""
    _get_project(db, project_id)
    rows = db.query(TranslationMemoryEntry).filter(TranslationMemoryEntry.project_id == project_id).all()
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
    _audit(db, project_id, "tm_snapshot_created", "memory_snapshot", str(snap.id),
           after={"snapshot_type": "tm", "item_count": len(rows)})
    return {
        "snapshot_id": str(snap.id),
        "snapshot_type": snap.snapshot_type,
        "item_count": snap.item_count,
        "created_at": snap.created_at.isoformat(),
    }


@router.get("/{project_id}/tm/snapshots")
async def list_tm_snapshots(
    project_id: str, db: Session = Depends(get_db_session)
) -> list[dict]:
    """List the project's TM snapshots (F3 / PRD §7.2)."""
    _get_project(db, project_id)
    rows = (
        db.query(MemorySnapshot)
        .filter(MemorySnapshot.project_id == project_id,
                MemorySnapshot.snapshot_type == "tm")
        .order_by(MemorySnapshot.created_at)
        .all()
    )
    return [
        {
            "snapshot_id": str(s.id),
            "snapshot_type": s.snapshot_type,
            "item_count": s.item_count,
            "description": s.description,
            "payload": s.payload or {},
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in rows
    ]


# --- TM maintenance report (§7.2 / dashboard, F3 / task t_694dfe91) --------
# Declared BEFORE /tm/{snapshot_id} so 'maintenance' is not swallowed by the
# dynamic route (FastAPI matches in order; 'maintenance' would otherwise be
# coerced to a UUID and fail with a DataError).
@router.get("/{project_id}/tm/maintenance")
async def tm_maintenance(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Run the TM maintenance checks (§7.2) and return the report.

    Flags duplicates, contradictory targets, misaligned tags/placeholders,
    length discrepancies and obsolete entries, and the TM reuse rate
    (§15.4 / dashboard). Runs synchronously so the dashboard can show a
    fresh report on demand; the same logic backs the ``tm_maintenance`` job.
    """
    _get_project(db, project_id)
    from .tm_maintenance import report_tm

    return report_tm(db, project_id)


@router.get("/{project_id}/tm/{snapshot_id}")
async def get_tm_snapshot(
    project_id: str, snapshot_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Fetch a TM snapshot and the TM entries it references (F3 / §7.2)."""
    _get_project(db, project_id)
    snap = db.get(MemorySnapshot, snapshot_id)
    if snap is None or str(snap.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="snapshot not found")
    entries = []
    ids = (snap.payload or {}).get("tm_entry_ids")
    if ids:
        for r in db.query(TranslationMemoryEntry).filter(TranslationMemoryEntry.id.in_(ids)):
            entries.append({
                "id": str(r.id),
                "source_normalized": r.source_normalized,
                "target_approved": r.target_approved,
                "genre_profile": r.genre_profile,
                "pov": r.pov,
                "score": None,
                "method": None,
                "project_ok": True,
            })
    return {
        "snapshot_id": str(snap.id),
        "snapshot_type": snap.snapshot_type,
        "item_count": snap.item_count,
        "description": snap.description,
        "payload": snap.payload or {},
        "entries": entries,
    }


# --- the planner ------------------------------------------------------------
def _default_style_guide(genre: str | None) -> str:
    """A minimal §9.2-derived style guide so the conflict detector has input.

    In the real product this comes from the project's approved style guide;
    here it is synthesised from the genre profile so §9.3 conflict exposure
    can be exercised end-to-end without an extra column.
    """
    _MATRIX = {
        "fantascientifica": "Worldbuilding: coerenza di nomi, tecnologie e razze. "
                             "Neologismi: mantenere il registro futurista.",
        "horror": "Mantieni tensione e ambiguità. Non spiegare troppo; "
                  " privilegia immagini sensoriali e frasi brevi.",
        "romanzarosa": "Voce intima, dialogo naturale, registro relazionale. "
                       "Ponere attenzione agli accordi di genere nel dialogo.",
        "saggio": "Accuratezza concettuale e terminologia disciplinare. "
                  "Conserva citazioni e riferimenti.",
    }
    key = (genre or "").lower()
    if key in _MATRIX:
        return _MATRIX[key]
    return "Traduci con fedeltà semantica e naturalezza letteraria italiana."


_OUTPUT_SCHEMA = """{
  "translations": [
    {
      "segment_id": "uuid",
      "target_text": "...",
      "used_entity_ids": ["uuid"],
      "term_violations": [],
      "flags": [
        {"type": "gender_ambiguous|term_ambiguous|ocr_suspect|source_ambiguous|other",
         "message": "..."}
      ]
    }
  ]
}"""


@router.post("/{project_id}/translation/planner", dependencies=[Depends(require_permission("start_translation"))])
async def translation_planner(
    project_id: str, payload: PlannerRequest, db: Session = Depends(get_db_session)
) -> dict:
    """Build a translation block plan (F3 / PRD §10.1 steps 1-3).

    1. real token budget (§10.1 step 1 / §5.4);
    2. TM retrieval (§7.2, project-scoped + threshold) and entity selection
       (§7.3);
    3. prompt assembly from immutable snapshots (§9.4 / §10.1 step 3) and
       conflict exposure (§9.3).

    Returns the :class:`BlockPlan` (prompt + budget + matches + conflicts).
    """
    project = _get_project(db, project_id)

    # --- TM candidates: project-scoped (§7.2 step 4, mandatory) ------------
    tm_query = db.query(TranslationMemoryEntry).filter(TranslationMemoryEntry.project_id == project_id)
    if payload.chapter_id:
        # §7.2 step 4: the chapter scope is OPTIONAL — keep entries of the
        # same chapter as well as chapter-agnostic (chapter_id NULL) entries.
        tm_query = tm_query.filter(
            (TranslationMemoryEntry.chapter_id == payload.chapter_id)
            | (TranslationMemoryEntry.chapter_id.is_(None))
        )
    tm_candidates = []
    for r in tm_query.all():
        c = {
            "id": str(r.id),
            "project_id": str(r.project_id),
            "chapter_id": str(r.chapter_id) if r.chapter_id else None,
            "source_normalized": r.source_normalized,
            "source_original": r.source_original,
            "target_approved": r.target_approved,
            "genre_profile": r.genre_profile,
            "pov": r.pov,
            "score": None,
            "method": None,
            "project_ok": True,
        }
        # pgvector stores the embedding as a list of floats.
        c["source_embedding"] = (
            [float(x) for x in r.source_embedding]
            if r.source_embedding is not None
            else None
        )
        tm_candidates.append(c)

    # --- TM retrieval (§7.2) ----------------------------------------------
    matches = retrieve_tm(
        block_source=payload.block_source,
        candidates=tm_candidates,
        project_id=project_id,
        exact_threshold=payload.exact_threshold,
        fuzzy_threshold=payload.fuzzy_threshold,
        semantic_threshold=payload.semantic_threshold,
        max_matches=payload.max_matches,
    )
    # §7.2 step 6: drop incompatible weak (semantic) hits from the prompt.
    block_genre = project.genre_profile
    usable_matches = []
    rejected_conflicts = []
    for m in matches:
        if tm_retrieval.is_incompatible_context(
            m, block_genre=block_genre, block_pov=None
        ):
            rejected_conflicts.append({
                "kind": "tm_incompatible_context",
                "source": m.get("source_original") or m.get("source_normalized"),
                "target": m.get("target_approved"),
                "reason": "TM match (semantic, debole) con genere incompatibile; "
                          "esposto, non usato in automatico (§7.2 step 6).",
            })
        else:
            usable_matches.append(m)

    # --- glossary (approved terms for the block) --------------------------
    gq = db.query(GlossaryTerm).filter(
        GlossaryTerm.project_id == project_id,
        GlossaryTerm.status == "approved",
    )
    glossary_entries = []
    for t in gq.all():
        glossary_entries.append({
            "id": str(t.id),
            "source": t.source_term,
            "target": t.target_term,
            "type": t.term_type,
            "forbidden_targets": list(t.forbidden_targets or []),
            "policy": "not_translate" if t.preferred else "translate",
            "italian_grammatical_gender": t.grammatical_gender_it,
            "italian_grammatical_number": t.grammatical_number,
            "notes": t.usage_notes,
            "priority": "high" if t.preferred else "normal",
        })

    # --- entities (§7.3, solo APPROVATE — coerenti col run) ---------------
    eq = db.query(Entity).filter(
        Entity.project_id == project_id,
        Entity.status == "approved",
    )
    entities = []
    for e in eq.all():
        aliases = [a.source_alias for a in (e.aliases or [])]
        entities.append({
            "id": str(e.id),
            "source": e.canonical_source,
            "target": e.canonical_target,
            "type": e.entity_type,
            "italian_grammatical_gender": e.italian_grammatical_gender,
            "grammatical_number": e.grammatical_number,
            "aliases": aliases,
            "policy": e.translation_policy,
            "allow_inflection": bool(e.allow_inflection),
            "notes": e.notes,
            "priority": "high" if e.priority == "block_batch" else "normal",
        })

    # --- style guide (§9.3 conflict input) --------------------------------
    style_guide = payload.style_guide or _default_style_guide(project.genre_profile)

    # --- assemble the block plan (§10.1 steps 1-3) ------------------------
    budget = TokenBudget(max_total=payload.max_total_tokens)
    plan: BlockPlan = build_block_plan(
        model_id=project.translation_model_id or "unselected",
        block_source=payload.block_source,
        segments=payload.segments,
        previous_context=payload.previous_context,
        budget=budget,
        tm_matches=usable_matches,
        glossary_entries=glossary_entries,
        entities=entities,
        style_guide=style_guide,
        snapshot_ids=payload.snapshot_ids,
        output_schema=_OUTPUT_SCHEMA,
    )
    # surface the rejected TM conflicts alongside the glossary/style ones.
    plan.conflicts.extend(rejected_conflicts)

    _audit(db, project_id, "translation_planner", "translation_block",
           after={
               "prompt_hash": plan.prompt_hash,
               "budget": plan.budget,
               "tm_matches": len(plan.tm_matches),
               "conflicts": len(plan.conflicts),
               "within_ceiling": plan.budget["within_ceiling"],
           })

    return plan.to_dict()


# --- run a translation block (§10.1 / §8.4 / §15.3) ------------------------
@router.post("/{project_id}/translation/run", status_code=202,
             dependencies=[Depends(require_permission("start_translation"))])
async def translation_run(
    project_id: str, payload: TranslationRunRequest,
    request: Request = None,
    db: Session = Depends(get_db_session),
) -> dict:
    """Run one translation block (§10.1 / §8.4 / §15.3).

    Enqueues the ``translate`` job with the supplied segments and options and
    returns the queued ``job_id``. The heavy work (constrained Gateway call,
    §10.1.5-9 validation, idempotent ``machine_draft`` save) runs on the
    worker thread; the §13.1 local-only gate fails fast here so a
    non-local Gateway never receives a manuscript payload.
    """
    from .routes import _enforce_rate

    _enforce_rate("translate", request)  # §13/§14: LLM runs are expensive
    _get_project(db, project_id)

    # §13.1: fail fast on a non-local Gateway BEFORE any payload validation,
    # so a misconfigured deployment surfaces as 451 regardless of the body.
    from backend.config import LLM_GATEWAY_BASE_URL, assert_local_url

    try:
        assert_local_url(LLM_GATEWAY_BASE_URL)
    except ValueError as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc

    if not payload.segments and not payload.segment_ids:
        raise HTTPException(
            status_code=422,
            detail="nessun segmento selezionato per la traduzione",
        )

    # Resolve segment_ids from the DB (client checkbox selection): the
    # runner needs full dicts (segment_id + source_text + ordinal ...),
    # the UI only knows the persisted IDs. Keeps §5.4 order.
    if payload.segment_ids:
        import uuid as _uuid

        from .models import TranslationUnit

        # Validate UUID format (DB raises DataError otherwise).
        bad = []
        for sid in payload.segment_ids:
            try:
                _uuid.UUID(sid)
            except (ValueError, AttributeError, TypeError):
                bad.append(sid)
        if bad:
            raise HTTPException(
                status_code=422,
                detail=f"identificativi segmento non validi: {', '.join(bad[:5])}",
            )
        units = (
            db.query(TranslationUnit)
            .filter(
                TranslationUnit.project_id == project_id,
                TranslationUnit.id.in_(payload.segment_ids),
            )
            .order_by(TranslationUnit.ordinal)
            .all()
        )
        found = {str(u.id) for u in units}
        missing = [sid for sid in payload.segment_ids if sid not in found]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"segmenti inesistenti: {', '.join(missing[:5])}",
            )
        payload.segments = [
            {
                "segment_id": str(u.id),
                "source_text": u.source_text,
                "chapter_id": str(u.chapter_id) if u.chapter_id else None,
                "ordinal": u.ordinal,
                "source_hash": u.source_hash,
            }
            for u in units
        ]
        if not payload.block_source:
            payload.block_source = " ".join(
                s["source_text"] or "" for s in payload.segments)

    scheduler = get_scheduler_from(db)
    job = scheduler.register(project_id, "translate",
                             {**payload.model_dump(), "project_id": project_id})
    scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }
