"""Entity API routes (PRD §6.2, §6.6, §15.2).

Implements the entity-review surface on top of the NLP runner and the pure
:mod:`backend.parsing.entities` layer:

* ``POST /projects/{id}/entities/extract`` — queue the BookNLP+NER job for
  the whole project or one chapter (``node_id``).
* ``GET  /projects/{id}/entities`` — list entities with their §6.4 fields,
  aliases, mention counts and first evidence (filterable by status/type/
  gender/number/chapter).
* ``GET  /projects/{id}/entities/{entity_id}` — full detail: every mention
  with quote + page + chapter (§15.2: ogni entità mostra le sue evidenze).
* ``PATCH /projects/{id}/entities/{entity_id}` — set the §6.4 fields the
  user owns (referential gender, Italian grammatical gender, number,
  translation policy, canonical target) and promote the status
  (proposed → verified → approved, §6.2.5); also the §6.6 fields
  (forbidden targets, priority, never-translate, allow-inflection).
* ``POST /projects/{id}/entities/aliases`` — add/remove an alias (§6.5).
* ``POST /projects/{id}/entities/{id}/merge`` — merge another entity into
  this one (aliases + evidence + mentions fold in; the merged row is
  marked ``merged``), the §15.2 "un'entità unica dopo merge" contract.
* ``POST /projects/{id}/entities/{id}/split`` — split off an alias as a
  new entity (the §6.6 "split manuale di alias") with a new canonical
  form.
* ``GET  /projects/{id}/entities/{id}/versions — the immutable edit
  history (§15.4).
* ``GET  /projects/{id}/entities/chapter/{node_id}` — entities introduced
  in one chapter and the delta vs the previous chapter (§6.6).
* ``POST /projects/{id}/entities/approve — bulk-approve a list of ids
  (single user action, audited).
* ``GET  /projects/{id}/entities/export?fmt=csv|tbx — export the
  entity list as CSV or TBX (§12.3 / AC3 round-trip).

All mutations are audited (§13.1). Manuscript quotes stay local: the API
serves them from the DB like every other payload.
"""
from __future__ import annotations

import re
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
from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import get_db_session
from .rbac import require_permission
from .entity_io import export_csv, export_tbx, import_csv, import_tbx
from .models import (
    AuditLog,
    Document,
    DocumentPage,
    Entity,
    EntityAlias,
    EntityEvidence,
    EntityInvalidation,
    EntityVersion,
    Project,
)
from .scheduler import get_scheduler_from

router = APIRouter(prefix="/projects", tags=["entities"])

_GENDERS = {"male", "female", "nonbinary", "mixed", "unknown", "not_applicable"}
_IT_GENDERS = {"masculine", "feminine", "common", "variable", "not_applicable"}
_NUMBERS = {"singular", "plural", "invariant", "collective", "unknown"}
_POLICIES = {"keep_source", "translate", "transliterate", "contextual",
             "undecided"}
_ENTITY_TYPES = {
    "PERSON", "ROLE", "CREATURE_SPECIES", "OBJECT_ARTIFACT", "LOCATION",
    "ORG_FACTION", "WORK_MEDIA", "EVENT", "CONCEPT_TERM", "TITLE_HONORIFIC",
}


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _get_entity(db: Session, project_id: str, entity_id: str) -> Entity:
    entity = db.get(Entity, entity_id)
    if entity is None or str(entity.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="entity not found")
    return entity


def _evidence_ocr_suspect(
    db: Session,
    project_id: str,
    ev: EntityEvidence,
    seg: "TranslationUnit | None",
) -> bool:
    """Whether the page/segment behind an evidence is flagged OCR-suspect.

    ``ocr_suspect`` is the §5.2/§13 flag carried by ``DocumentPage`` (per
    document+page) and propagated to ``TranslationUnit.source_flags`` by the
    OCR runner (see ``ocr_runner._propagate_segment_flags``). The side panel
    surfaces it as a badge on each evidence (§6.6 / §11.2 "OCR sospetto").

    Resolution order: the attached segment's ``source_flags["ocr_suspect"]``
    (most precise — set by the OCR runner) wins; otherwise the page-level
    ``DocumentPage.ocr_suspect`` of any of the project's documents that has a
    row for that page number. Missing page/segment → ``False``.
    """
    if seg is not None:
        flags = seg.source_flags or {}
        if flags.get("ocr_suspect"):
            return True
    if ev.page_number is None:
        return False
    page_rows = (
        db.query(DocumentPage, Document)
        .join(Document, DocumentPage.document_id == Document.id)
        .filter(
            Document.project_id == project_id,
            DocumentPage.page_number == ev.page_number,
        )
        .all()
    )
    return any(bool(p.ocr_suspect) for p, _ in page_rows)


def _entity_summary(db: Session, entity: Entity) -> dict:
    evidence = (
        db.query(EntityEvidence)
        .filter(EntityEvidence.entity_id == entity.id)
        .order_by(EntityEvidence.page_number)
        .first()
    )
    aliases = [
        a.source_alias
        for a in db.query(EntityAlias)
        .filter(EntityAlias.entity_id == entity.id)
        .all()
    ]
    mention_count = (
        db.query(EntityEvidence)
        .filter(EntityEvidence.entity_id == entity.id)
        .count()
    )
    return {
        "id": str(entity.id),
        "project_id": str(entity.project_id),
        "canonical_source": entity.canonical_source,
        "canonical_target": entity.canonical_target,
        "entity_type": entity.entity_type,
        "status": entity.status,
        "referential_gender": entity.referential_gender,
        "referential_gender_evidence": entity.referential_gender_evidence,
        "italian_grammatical_gender": entity.italian_grammatical_gender,
        "grammatical_number": entity.grammatical_number,
        "translation_policy": entity.translation_policy,
        "definition": entity.definition,
        "notes": entity.notes,
        "confidence": float(entity.confidence) if entity.confidence is not None else None,
        "aliases": sorted(set(aliases)),
        "forbidden_targets": list(entity.forbidden_targets or []),
        "priority": entity.priority,
        "never_translate": entity.never_translate,
        "allow_inflection": entity.allow_inflection,
        "version": entity.version,
        "mention_count": mention_count,
        "first_evidence": {
            "page_number": evidence.page_number if evidence else None,
            "quote_text": evidence.quote_text if evidence else None,
            "evidence_type": evidence.evidence_type if evidence else None,
            "extractor": evidence.extractor if evidence else None,
        }
        if evidence else None,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }


# --- extraction job (§6.2) --------------------------------------------------
@router.post("/{project_id}/entities/extract", status_code=202, dependencies=[Depends(require_permission("manage_glossary"))])
async def extract_entities(
    project_id: str,
    node_id: str | None = Query(default=None),
    reuse_output_key: str | None = Query(
        default=None,
        description="Chiave object-storage di un output BookNLP grezzo "
        "(.../manifest.json) da riusare: salta i modelli (recupero/reprocess).",
    ),
    db: Session = Depends(get_db_session),
) -> dict:
    """Queue the BookNLP+NER extraction (whole project or one chapter)."""
    _get_project(db, project_id)
    payload: dict = {"project_id": str(project_id)}
    if node_id:
        payload["node_id"] = str(node_id)
    if reuse_output_key:
        payload["reuse_output_key"] = reuse_output_key
    scheduler = get_scheduler_from(db)
    job = scheduler.register(project_id, "extract_entities", payload)
    scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }


# --- LLM classification of ambiguous candidates (§6.2.3) --------------------
@router.post("/{project_id}/entities/llm-classify", status_code=202, dependencies=[Depends(require_permission("manage_glossary"))])
async def llm_classify_entities(
    project_id: str,
    node_id: str | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> dict:
    """Queue the structured LLM classification job (§6.2.3, §8.2).

    Only the §6.2.3 candidates are sent to the analysis model: domain
    categories (species, artefacts, curses, fictional institutions) and
    low-confidence proposals. The base URL passes the §13.1 local-only
    gate here so a misconfigured (non-local) Gateway fails fast at the
    API boundary — no manuscript payload is queued for an outbound call.
    """
    from .config import LLM_GATEWAY_BASE_URL, assert_local_url

    _get_project(db, project_id)
    try:
        assert_local_url(LLM_GATEWAY_BASE_URL)  # §13.1: fail fast, nothing leaves
    except ValueError as exc:
        # 451 Unavailable For Legal Reasons — the configured Gateway
        # endpoint violates the local-only policy (§13.1).
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    payload: dict = {"project_id": str(project_id)}
    if node_id:
        payload["node_id"] = str(node_id)
    scheduler = get_scheduler_from(db)
    job = scheduler.register(project_id, "llm_classify_entities", payload)
    scheduler.enqueue(job)
    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
    }


# --- listing/detail (§6.6) --------------------------------------------------
# NOTE: the static /import and /export routes MUST be registered before the
# dynamic /entities/{entity_id} route (below) or "import"/"export"/"approve"
# would be captured as an entity_id and fail the UUID cast.
@router.post("/{project_id}/entities/import", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def import_entities(
    project_id: str,
    file: UploadFile,
    fmt: str = Query(default="auto", description="csv|tbx|auto"),
    db: Session = Depends(get_db_session),
) -> dict:
    """Import entities from CSV or TBX (§12.3).

    ``fmt`` may be ``csv`` or ``tbx``; ``auto`` (default) infers the
    format from the filename. Invalid rows are skipped and reported; valid
    rows are inserted. The result is a partial import (AC1).
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
        entities, errors = import_csv(text)
    elif fmt == "tbx":
        entities, errors = import_tbx(text)
    else:
        raise HTTPException(status_code=422, detail=f"unknown format {fmt!r}")

    inserted: list[str] = []
    for e in entities:
        row = Entity(
            id=str(uuid.uuid4()),
            project_id=project_id,
            canonical_source=e["canonical_source"],
            canonical_target=e["canonical_target"],
            entity_type=e["entity_type"],
            referential_gender=e["referential_gender"] or "unknown",
            referential_gender_evidence=e["referential_gender_evidence"],
            italian_grammatical_gender=e["italian_grammatical_gender"]
            or "not_applicable",
            grammatical_number=e["grammatical_number"] or "unknown",
            translation_policy=e["translation_policy"] or "undecided",
            status=e["status"],
            confidence=e["confidence"],
            version=1,
            never_translate=e["never_translate"],
            allow_inflection=e["allow_inflection"],
            created_at=datetime.utcnow(),
        )
        db.add(row)
        inserted.append(str(row.id))
        for alias in e["aliases"]:
            db.add(EntityAlias(
                id=str(uuid.uuid4()),
                entity_id=row.id,
                source_alias=alias,
                target_alias=None,
                alias_type="imported",
            ))
    db.commit()
    db.add(AuditLog(
        project_id=project_id,
        action="entities_imported",
        entity="entity",
        before={
            "format": fmt,
            "rows": len(entities) + len(errors),
        },
        after={
            "format": fmt,
            "imported": len(inserted),
            "errors": len(errors),
        },
        created_at=datetime.utcnow(),
    ))
    return {
        "imported": len(inserted),
        "errors": errors,
        "error_count": len(errors),
        "total": len(entities) + len(errors),
    }


@router.get("/{project_id}/entities/export")
async def export_entities(
    project_id: str,
    fmt: str = Query(default="csv", description="csv|tbx"),
    db: Session = Depends(get_db_session),
) -> Response:
    """Export the entity list as CSV or TBX (§12.3 / AC3 round-trip)."""
    _get_project(db, project_id)
    rows = (
        db.query(Entity)
        .filter(Entity.project_id == project_id)
        .order_by(Entity.canonical_source)
        .all()
    )
    payload = [_entity_summary(db, e) for e in rows]
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
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition":
                 f'attachment; filename="entities.{ext}"'},
    )


# --- bulk approve (§6.2.5) --------------------------------------------------
class ApproveRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/{project_id}/entities/approve", dependencies=[Depends(require_permission("approve_terminology"))])
async def approve_entities(
    project_id: str,
    payload: ApproveRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Bulk-approve a list of entity ids (single user action, audited)."""
    _get_project(db, project_id)
    approved: list[str] = []
    for eid in payload.ids:
        # ignore ids that aren't valid UUIDs / belong to this project
        try:
            uuid.UUID(eid)
        except (ValueError, AttributeError):
            continue
        e = db.get(Entity, eid)
        if e is None or str(e.project_id) != str(project_id):
            continue
        if e.status not in ("proposed", "verified"):
            continue
        before = _entity_summary(db, e)
        e.status = "approved"
        e.updated_at = datetime.utcnow()
        e.version += 1
        db.add(e)
        db.add(AuditLog(
            project_id=project_id,
            action="entity_approved",
            entity="entity",
            entity_id=str(e.id),
            before={k: before.get(k) for k in ("status", "version")},
            after={"status": "approved", "version": e.version},
            created_at=datetime.utcnow(),
        ))
        approved.append(str(e.id))
    db.commit()
    return {"approved": approved, "count": len(approved)}


# --- bulk status change (proposta/approvazione massiva su tutte le pagine) ---
class BulkStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    status: str


@router.post("/{project_id}/entities/bulk-status", dependencies=[Depends(require_permission("approve_terminology"))])
async def bulk_status_entities(
    project_id: str,
    payload: BulkStatusRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Cambia lo stato di molte entità in UNA richiesta (§6.2.5).

    Fornito per le azioni massive su tutte le pagine: N PATCH parallele dal
    browser superano i timeout di fetch ("Failed to fetch") per lista grandi
    (migliaia di righe). Qui tutto avviene in una transazione, con audit log
    e version snapshot per ogni entità modificata.
    """
    if payload.status not in {"proposed", "verified", "approved"}:
        raise HTTPException(status_code=422, detail="invalid status")
    _get_project(db, project_id)
    changed: list[str] = []
    now = datetime.utcnow()
    for eid in payload.ids:
        try:
            uuid.UUID(eid)
        except (ValueError, AttributeError):
            continue
        e = db.get(Entity, eid)
        if e is None or str(e.project_id) != str(project_id):
            continue
        if e.status == payload.status:
            continue
        before = _entity_summary(db, e)
        e.status = payload.status
        e.updated_at = now
        e.version += 1
        db.add(e)
        db.add(AuditLog(
            project_id=project_id,
            action=f"entity_bulk_{payload.status}",
            entity="entity",
            entity_id=str(e.id),
            before={"status": before.get("status"), "version": before.get("version")},
            after={"status": payload.status, "version": e.version},
            created_at=now,
        ))
        # snapshot immutabile §15.4
        snapshot = _entity_summary(db, e)
        db.add(EntityVersion(
            id=str(uuid.uuid4()),
            entity_id=str(e.id),
            version=e.version,
            action=f"bulk_{payload.status}",
            snapshot=snapshot,
            created_at=now,
        ))
        changed.append(str(e.id))
    db.commit()
    return {"updated": changed, "count": len(changed)}


# --- bulk "Traduci Nomi" (traduzione del nome EN -> forma IT) ---------------
class TranslateNamesRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/{project_id}/entities/translate-names", dependencies=[Depends(require_permission("manage_glossary"))])
async def translate_entity_names(
    project_id: str,
    payload: TranslateNamesRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Traduce ``canonical_source`` (EN) -> ``canonical_target`` (IT).

    Usa il modello di traduzione del progetto (§8.1, via Gateway — ADR-001),
    a blocchi di ~20 nomi con commit **per blocco** (ogni blocco è una
    transazione separata: nessuna sessione DB aperta a lungo).

    Le chiamate LLM sono LENTE (~5-12 s ciascuna): il cliente deve invocare
    l'endpoint a FETTE (chunks di ~100 id) in loop invece di passare tutte
    le 2965 righe in una richiesta — una singola richiesta HTTP con ~150
    chiamate LLM (~30-45 min) supera i timeout di proxy/browser e la sessione
    DB muore ("server closed the connection unexpectedly").

    Se il modello segnala che il nome NON è traducibile (la forma inglese si
    usa identicamente in italiano), ``canonical_target`` resta **vuoto**.
    """
    from .gateway_http import GatewayInvalidJSON, GatewayUnavailable, GatewayClient
    from .config import DEFAULT_TRANSLATION_MODEL

    project = _get_project(db, project_id)

    targets: list[Entity] = []
    for eid in payload.ids:
        try:
            uuid.UUID(eid)
        except (ValueError, AttributeError):
            continue
        e = db.get(Entity, eid)
        if e is None or str(e.project_id) != str(project_id):
            continue
        if e.status in ("deprecated", "merged"):
            continue
        targets.append(e)
    if not targets:
        return {"translated": 0, "untranslatable": 0, "failed": 0,
                "details": [], "processed": 0}

    model = (
        getattr(project, "translation_model_id", None)
        or DEFAULT_TRANSLATION_MODEL
    )
    client = GatewayClient()

    BATCH = 20
    translated = untranslatable = failed = 0
    details: list[dict] = []

    def _blocks(items: list[Entity], size: int = BATCH):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    for index, batch in enumerate(_blocks(targets)):
        numbered = {str(i + 1): e for i, e in enumerate(batch)}
        listing = "\n".join(
            f"{key}. {e.canonical_source}" for key, e in numbered.items()
        )
        # NOTA (misurato): translategemma traduce in italiano SOLO quando
        # l'istruzione "Translate to Italian" è nel USER message — con un
        # system message in italiano produce un adattamento inglese.
        system_prompt = "Respond ONLY with valid JSON."
        user_prompt = (
            "Translate to Italian (literary EN->IT), keeping the rendering "
            "used in Italian editions (transliteration/adaptation when it "
            "exists). For each numbered item return the Italian rendering; "
            'use "it": null only if the text is identical in Italian '
            "(acronyms, numbers). JSON shape: "
            '{"items": [{"n": <number>, "it": <string|null>}]}.\n'
            f"Nomi da tradurre:\n{listing}"
        )
        try:
            result = client.chat_json(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
            )
        except (GatewayUnavailable, GatewayInvalidJSON) as exc:
            failed += len(batch)
            details.append({
                "batch": index, "error": str(exc)[:200],
                "ids": [str(e.id) for e in batch],
            })
            db.rollback()  # niente transazione sporca
            continue

        items = result.get("items") if isinstance(result, dict) else None
        by_n: dict[str, str | None] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    key = str(int(item.get("n")))
                except (TypeError, ValueError):
                    continue
                value = item.get("it")
                by_n[key] = value if isinstance(value, str) else None

        now = datetime.utcnow()
        for key, e in numbered.items():
            if key not in by_n:
                failed += 1
                details.append({
                    "entity_id": str(e.id),
                    "error": "missing in model response",
                })
                continue
            it_form = (by_n[key] or "").strip()
            before = {"canonical_target": e.canonical_target}
            e.canonical_target = it_form or None
            e.updated_at = now
            e.version += 1
            db.add(e)
            db.add(AuditLog(
                project_id=project_id,
                action="entity_name_translated",
                entity="entity",
                entity_id=str(e.id),
                before=before,
                after={"canonical_target": it_form or None},
                created_at=now,
            ))
            if it_form:
                translated += 1
            else:
                untranslatable += 1
        db.commit()  # COMMIT PER BLOCCO: sessione mai a lungo aperta
    return {
        "translated": translated,
        "untranslatable": untranslatable,
        "failed": failed,
        "details": details[:50],
        "processed": len(targets),
    }


@router.get("/{project_id}/entities")
async def list_entities(
    project_id: str,
    status: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    referential_gender: str | None = Query(default=None),
    grammatical_number: str | None = Query(default=None),
    chapter_id: str | None = Query(default=None),
    alias: str | None = Query(default=None, description="upper|lower"),
    translation: str | None = Query(
        default=None,
        description="identical|shares-word|same-as-en-and-alias",
    ),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db_session),
) -> dict:
    """The project's entities with their §6.4 fields (filterable).

    Filters: status, entity_type, referential_gender, grammatical_number
    and chapter (§6.6: tabella filtrabile per stato/tipo/genere/numero/
    capitolo). The chapter filter matches entities that have any evidence
    in that chapter.

    Results are paginated (§6.6 / AC2): ``page`` (1-based) and
    ``per_page`` (default 50, max 500). The response carries ``total``,
    ``page`` and ``per_page`` so the UI can render pagination controls.
    """
    _get_project(db, project_id)
    query = db.query(Entity).filter(Entity.project_id == project_id)
    if status:
        query = query.filter(Entity.status == status)
    if entity_type:
        query = query.filter(Entity.entity_type == entity_type)
    if referential_gender:
        query = query.filter(Entity.referential_gender == referential_gender)
    if grammatical_number:
        query = query.filter(Entity.grammatical_number == grammatical_number)
    if chapter_id:
        # an entity is "in" a chapter if any of its evidence points there.
        evidence = (
            db.query(EntityEvidence.entity_id)
            .filter(EntityEvidence.chapter_id == chapter_id)
            .all()
        )
        ids = {str(e[0]) for e in evidence}
        query = query.filter(Entity.id.in_(sorted(ids)))

    # Alias + Traduzione: applicati SERVERSIDE su TUTTE le righe PRIMA della
    # paginazione (a differenza del passato, quando erano client-side sulla
    # sola pagina corrente: le pagine restavano 60 e il filtro toglieva righe
    # solo dalla pagina visibile). Ora `total` riflette i filtri e la
    # paginazione è corretta.
    if alias or translation:
        # candidati: gli ID compatibili coi filtri base, con i soli campi che
        # servono a valutare alias/traduzione (dati minimi, no _entity_summary)
        cand = query.with_entities(
            Entity.id,
            Entity.canonical_source,
            Entity.canonical_target,
        ).all()
        cand_ids = [str(c[0]) for c in cand]
        alias_rows = (
            db.query(EntityAlias.entity_id, EntityAlias.source_alias)
            .filter(EntityAlias.entity_id.in_(cand_ids or ["00000000-0000-0000-0000-000000000000"]))
            .all()
        )
        aliases_by_entity: dict[str, list[str]] = {}
        for eid, src in alias_rows:
            aliases_by_entity.setdefault(str(eid), []).append(src)

        def _single_word(a: str) -> bool:
            return len(a.strip().split()) == 1

        def _has_upper(a: str) -> bool:
            return bool(re.match(r"^[A-Z]", a.strip()))

        def _pick_alias(aliases: list[str], src: str) -> str | None:
            name_lower = (src or "").strip().lower()
            single = [a for a in aliases if _single_word(a)]
            in_name = [a for a in single if a.strip().lower() in name_lower]
            upper = [a for a in in_name if _has_upper(a)]
            pool = upper if upper else in_name
            return pool[0] if pool else None

        def _words(s: str) -> set[str]:
            return {t for t in re.split(r"[^\w]+", (s or "").lower()) if t}

        matching: list[str] = []
        for cid, csrc, ctgt in cand:
            eid = str(cid)
            aliases = aliases_by_entity.get(eid, [])
            # --- filtro Alias (upper/lower) ---
            if alias:
                single = [a for a in aliases if _single_word(a)]
                has_upper_any = any(_has_upper(a) for a in single)
                if alias == "upper":
                    if not has_upper_any:
                        continue
                elif alias == "lower":
                    has_single = len(single) > 0
                    if not (has_single and not has_upper_any):
                        continue
                else:
                    raise HTTPException(
                        status_code=422, detail="invalid alias filter")
            # --- filtro Traduzione ---
            if translation:
                en = (csrc or "").strip().lower()
                it = (ctgt or "").strip().lower() if ctgt else None
                en_w = _words(en)
                if translation == "identical":
                    if not it or it != en:
                        continue
                elif translation == "shares-word":
                    if not it or not (en_w & _words(it)):
                        continue
                elif translation == "same-as-en-and-alias":
                    if not it or it != en:
                        continue
                    shown = _pick_alias(aliases, csrc)
                    if not shown or shown.strip().lower() != it:
                        continue
                else:
                    raise HTTPException(
                        status_code=422, detail="invalid translation filter")
            matching.append(eid)
        query = query.filter(
            Entity.id.in_(matching or ["00000000-0000-0000-0000-000000000000"])
        )

    total = query.count()
    # Ordinamento server-side (§6.6): agisce su TUTTE le righe, non solo
    # sulla pagina corrente. Sort supportati: alias, name, name_it, type,
    # status, mentions.
    from sqlalchemy import case, func, literal_column

    order = "asc" if (order or "asc") == "asc" else "desc"
    dir_ = 1 if order == "asc" else -1
    sort_col = sort or "name"

    # "alias" = primo alias monoparola dell'entità, preferendo quello con
    # iniziale maiuscola (stessa semantica della colonna UI).
    if sort_col == "alias":
        sub = (
            db.query(
                EntityAlias.entity_id.label("entity_id"),
                func.min(case(
                    # iniziale maiuscola (regex Postgres ~ '^[A-Z]')
                    (EntityAlias.source_alias.op("~")(r"^[A-Z]"),
                     EntityAlias.source_alias),
                    else_=None,
                )).label("alias_upper"),
                func.min(EntityAlias.source_alias).label("alias_any"),
            )
            # solo alias monoparola: nessuno spazio
            .filter(EntityAlias.source_alias.notlike("% %"))
            .group_by(EntityAlias.entity_id)
            .subquery()
        )
        query = query.outerjoin(sub, Entity.id == sub.c.entity_id)
        pick = func.coalesce(sub.c.alias_upper, sub.c.alias_any)
        rows = (
            query.order_by(
                pick.is_(None),  # senza alias in fondo
                pick.asc() if order == "asc" else pick.desc(),
                Entity.canonical_source.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    elif sort_col == "name_it":
        rows = (
            query.order_by(
                Entity.canonical_target.is_(None) if dir_ == 1 else Entity.canonical_target.isnot(None),
                Entity.canonical_target.asc() if dir_ == 1 else Entity.canonical_target.desc(),
                Entity.canonical_source.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    elif sort_col == "type":
        rows = (
            query.order_by(
                Entity.entity_type.asc() if dir_ == 1 else Entity.entity_type.desc(),
                Entity.canonical_source.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    elif sort_col == "status":
        rows = (
            query.order_by(
                Entity.status.asc() if dir_ == 1 else Entity.status.desc(),
                Entity.canonical_source.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    elif sort_col == "mentions":
        mcount = (
            db.query(
                EntityEvidence.entity_id.label("entity_id"),
                func.count(EntityEvidence.id).label("cnt"),
            )
            .group_by(EntityEvidence.entity_id)
            .subquery()
        )
        query = query.outerjoin(mcount, Entity.id == mcount.c.entity_id)
        cnt = func.coalesce(mcount.c.cnt, 0)
        rows = (
            query.order_by(
                cnt.desc() if dir_ == -1 else cnt.asc(),
                Entity.canonical_source.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    else:  # "name" (default)
        rows = (
            query.order_by(
                Entity.canonical_source.asc() if dir_ == 1 else Entity.canonical_source.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "entities": [_entity_summary(db, e) for e in rows],
    }


@router.get("/{project_id}/entities/{entity_id}")
async def get_entity(
    project_id: str,
    entity_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Full entity detail with every mention/evidence (§15.2)."""
    from .models import TranslationUnit

    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)
    summary = _entity_summary(db, entity)
    evidence_rows = (
        db.query(EntityEvidence)
        .filter(EntityEvidence.entity_id == entity.id)
        .order_by(EntityEvidence.page_number, EntityEvidence.id)
        .all()
    )
    summary["evidence"] = [
        {
            "id": str(ev.id),
            "page_number": ev.page_number,
            "chapter_id": str(ev.chapter_id) if ev.chapter_id else None,
            "quote_text": ev.quote_text,
            "evidence_type": ev.evidence_type,
            "confidence": float(ev.confidence) if ev.confidence is not None else None,
            "extractor": ev.extractor,
            "segment_id": str(ev.source_segment_id) if ev.source_segment_id else None,
            "ocr_suspect": _evidence_ocr_suspect(
                db, project_id, ev,
                db.get(TranslationUnit, ev.source_segment_id)
                if ev.source_segment_id else None,
            ),
        }
        for ev in evidence_rows
    ]
    return summary


# --- evidence with ±2 paragraphs of context (§6.6 / §12.3) ------------------
@router.get("/{project_id}/entities/{entity_id}/evidence")
async def entity_evidence(
    project_id: str,
    entity_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Each mention with ±2 paragraphs of context (§6.6 / §12.3).

    For every evidence of the entity we resolve the segment it is attached
    to (``source_segment_id`` -> ``translation_units.id``) and return the
    source text of that segment plus the source text of the two segments
    before and after it (in reading order within the same chapter), so the
    reviewer sees the mention inside its paragraph (§6.6: side panel con
    tutte le menzioni ed estratti di ±2 paragrafi).
    """
    from .models import TranslationUnit

    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)
    rows = (
        db.query(EntityEvidence)
        .filter(EntityEvidence.entity_id == entity.id)
        .order_by(EntityEvidence.page_number, EntityEvidence.id)
        .all()
    )
    out = []
    for ev in rows:
        context = None
        if ev.source_segment_id:
            seg = db.get(TranslationUnit, ev.source_segment_id)
            if seg is not None and seg.chapter_id == ev.chapter_id:
                neighbors = (
                    db.query(TranslationUnit)
                    .filter(
                        TranslationUnit.project_id == entity.project_id,
                        TranslationUnit.chapter_id == seg.chapter_id,
                    )
                    .order_by(TranslationUnit.ordinal)
                    .all()
                )
                idx = next(
                    (i for i, n in enumerate(neighbors)
                     if n.id == seg.id), None)
                if idx is not None:
                    lo = max(0, idx - 2)
                    hi = min(len(neighbors), idx + 3)
                    context = [
                        {"ordinal": n.ordinal, "source_text": n.source_text}
                        for n in neighbors[lo:hi]
                    ]
        out.append({
            "id": str(ev.id),
            "page_number": ev.page_number,
            "chapter_id": str(ev.chapter_id) if ev.chapter_id else None,
            "quote_text": ev.quote_text,
            "evidence_type": ev.evidence_type,
            "confidence": float(ev.confidence) if ev.confidence is not None else None,
            "extractor": ev.extractor,
            "segment_id": str(ev.source_segment_id) if ev.source_segment_id else None,
            "ocr_suspect": _evidence_ocr_suspect(
                db, project_id, ev,
                db.get(TranslationUnit, ev.source_segment_id)
                if ev.source_segment_id else None,
            ),
            "context": context,
        })
    return {
        "entity_id": str(entity.id),
        "canonical_source": entity.canonical_source,
        "mention_count": len(out),
        "evidence": out,
    }


# --- user review (§6.4, §6.6, §15.2) ----------------------------------------
class EntityPatch(BaseModel):
    canonical_target: str | None = None
    entity_type: str | None = None
    referential_gender: str | None = Field(default=None)
    italian_grammatical_gender: str | None = Field(default=None)
    grammatical_number: str | None = Field(default=None)
    translation_policy: str | None = Field(default=None)
    # fix 2026-09-19: i due flag booleani non erano nel modello PATCH ->
    # Pydantic li scartava in silenzio e i checkbox dell'UI non salvavano.
    never_translate: bool | None = None
    allow_inflection: bool | None = None
    definition: str | None = None
    notes: str | None = None
    status: str | None = None


@router.patch("/{project_id}/entities/{entity_id}", dependencies=[Depends(require_permission("manage_glossary"))])
async def patch_entity(
    project_id: str,
    entity_id: str,
    payload: EntityPatch,
    db: Session = Depends(get_db_session),
) -> dict:
    """User review: set the §6.4 fields and/or promote the status.

    The user owns these fields (§15.2: “l'utente può impostare genere
    referenziale, genere grammaticale italiano, numero e policy di
    traduzione”); the runner never overwrites a value the user set on a
    non-proposed entity.
    """
    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)

    changes = payload.model_dump(exclude_none=True)
    if "entity_type" in changes:
        if changes["entity_type"] not in _ENTITY_TYPES:
            raise HTTPException(status_code=422, detail="invalid entity_type")
    if "referential_gender" in changes and changes["referential_gender"] not in _GENDERS:
        raise HTTPException(status_code=422, detail="invalid referential_gender")
    if ("italian_grammatical_gender" in changes
            and changes["italian_grammatical_gender"] not in _IT_GENDERS):
        raise HTTPException(status_code=422, detail="invalid italian_grammatical_gender")
    if "grammatical_number" in changes and changes["grammatical_number"] not in _NUMBERS:
        raise HTTPException(status_code=422, detail="invalid grammatical_number")
    if "translation_policy" in changes and changes["translation_policy"] not in _POLICIES:
        raise HTTPException(status_code=422, detail="invalid translation_policy")
    if "status" in changes:
        allowed = {"proposed", "verified", "approved", "deprecated", "merged"}
        if changes["status"] not in allowed:
            raise HTTPException(status_code=422, detail="invalid status")

    before = {
        k: getattr(entity, k)
        for k in changes
    }
    for key, value in changes.items():
        setattr(entity, key, value)
    entity.updated_at = datetime.utcnow()
    entity.version += 1
    db.add(entity)
    db.add(AuditLog(
        action="entity_updated",
        entity="entity",
        entity_id=str(entity.id),
        before={k: (str(v) if v is not None else None) for k, v in before.items()},
        after={k: (str(v) if v is not None else None) for k, v in changes.items()},
        created_at=datetime.utcnow(),
    ))
    # §15.4: record an immutable snapshot of the post-edit state.
    snapshot = _entity_summary(db, entity)
    db.add(EntityVersion(
        id=str(uuid.uuid4()),
        entity_id=str(entity.id),
        version=entity.version,
        snapshot=snapshot,
        action="patch",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return _entity_summary(db, entity)


# --- aliases (§6.5) ---------------------------------------------------------
class AliasAction(BaseModel):
    source_alias: str = Field(..., min_length=1)
    target_alias: str | None = None
    alias_type: str = Field(default="synonym")


@router.post("/{project_id}/entities/{entity_id}/aliases", status_code=201, dependencies=[Depends(require_permission("manage_glossary"))])
async def add_entity_alias(
    project_id: str,
    entity_id: str,
    payload: AliasAction,
    db: Session = Depends(get_db_session),
) -> dict:
    """Add an alias to an entity (§6.5). Returns the updated summary."""
    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)
    existing = (
        db.query(EntityAlias)
        .filter(EntityAlias.entity_id == entity.id,
                EntityAlias.source_alias == payload.source_alias)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"alias '{payload.source_alias}' already exists")
    alias = EntityAlias(
        id=str(uuid.uuid4()),
        entity_id=entity.id,
        source_alias=payload.source_alias,
        target_alias=payload.target_alias,
        alias_type=payload.alias_type,
    )
    db.add(alias)
    db.commit()
    return _entity_summary(db, db.get(Entity, entity.id))


@router.delete("/{project_id}/entities/{entity_id}/aliases/{alias_id}", dependencies=[Depends(require_permission("manage_glossary"))])
async def remove_entity_alias(
    project_id: str,
    entity_id: str,
    alias_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Remove an alias from an entity (§6.5)."""
    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)
    alias = (
        db.query(EntityAlias)
        .filter(EntityAlias.id == alias_id,
                EntityAlias.entity_id == entity.id)
        .one_or_none()
    )
    if alias is None:
        raise HTTPException(status_code=404, detail="alias not found")
    db.delete(alias)
    db.commit()
    return _entity_summary(db, entity)


# --- merge (§15.2) ----------------------------------------------------------
class MergeTarget(BaseModel):
    target_id: str = Field(..., min_length=1)


@router.post("/{project_id}/entities/{entity_id}/merge", dependencies=[Depends(require_permission("manage_glossary"))])
async def merge_entity(
    project_id: str,
    entity_id: str,
    payload: MergeTarget,
    db: Session = Depends(get_db_session),
) -> dict:
    """Merge another entity into this one (§6.6 / §15.2).

    The target's aliases and evidence fold into the survivor; the target is
    marked ``merged`` (not deleted) so its history stays auditable. The
    survivor's canonical set is the union of both, so a UI that re-reads
    the list sees a single entity immediately (AC1).
    """
    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)
    target = _get_entity(db, project_id, payload.target_id)
    if target.id == entity.id:
        raise HTTPException(
            status_code=400, detail="an entity cannot be merged into itself")

    # fold aliases (skip ones already present on the survivor)
    kept_aliases = {
        a.source_alias.lower() for a in entity.aliases
    }
    for a in target.aliases:
        if a.source_alias.lower() in kept_aliases:
            continue
        new_alias = EntityAlias(
            id=str(uuid.uuid4()),
            entity_id=entity.id,
            source_alias=a.source_alias,
            target_alias=a.target_alias,
            alias_type=a.alias_type,
        )
        db.add(new_alias)
        kept_aliases.add(a.source_alias.lower())

    # fold evidence (re-point chapter_id/source_segment to the survivor)
    for ev in target.evidence:
        new_ev = EntityEvidence(
            id=str(uuid.uuid4()),
            entity_id=entity.id,
            chapter_id=ev.chapter_id,
            source_segment_id=ev.source_segment_id,
            page_number=ev.page_number,
            quote_text=ev.quote_text,
            evidence_type=ev.evidence_type,
            confidence=ev.confidence,
            extractor=ev.extractor,
        )
        db.add(new_ev)

    # remember the merged-away canonical source as an alias
    db.add(EntityAlias(
        id=str(uuid.uuid4()),
        entity_id=entity.id,
        source_alias=target.canonical_source,
        target_alias=target.canonical_target,
        alias_type="merged_from",
    ))

    # mark the target merged; invalidate segments that used it
    entity.version += 1
    entity.updated_at = datetime.utcnow()
    target.status = "merged"
    target.updated_at = datetime.utcnow()

    _invalidate_for_entity(db, entity.id, target.canonical_source)

    db.add(AuditLog(
        project_id=project_id,
        action="entity_merged",
        entity="entity",
        entity_id=str(entity.id),
        before={"merged_target": str(target.id)},
        after={"merged_into": str(entity.id)},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return _entity_summary(db, entity)


# --- split (§6.6) -----------------------------------------------------------
class SplitEntity(BaseModel):
    source_alias: str = Field(..., min_length=1)
    canonical_source: str = Field(..., min_length=1)
    canonical_target: str | None = None
    entity_type: str = Field(default="CONCEPT_TERM")


@router.post("/{project_id}/entities/{entity_id}/split", dependencies=[Depends(require_permission("manage_glossary"))])
async def split_entity(
    project_id: str,
    entity_id: str,
    payload: SplitEntity,
    db: Session = Depends(get_db_session),
) -> dict:
    """Split an alias off as a new entity (§6.6 "split manuale di alias").

    The alias (or an explicit ``canonical_source``) becomes the canonical
    source of a new entity; the source entity keeps its alias list minus
    the split-off one. The new entity starts ``proposed`` for review.
    """
    _get_project(db, project_id)
    entity = _get_entity(db, project_id, entity_id)

    # find the alias to split off
    alias = None
    if payload.source_alias:
        alias = next(
            (a for a in entity.aliases
             if a.source_alias == payload.source_alias), None)
    if alias is None and payload.canonical_source != entity.canonical_source:
        # allow splitting a brand-new canonical form
        pass

    new_entity = Entity(
        id=str(uuid.uuid4()),
        project_id=project_id,
        canonical_source=payload.canonical_source,
        canonical_target=payload.canonical_target,
        entity_type=payload.entity_type,
        referential_gender="unknown",
        italian_grammatical_gender="not_applicable",
        grammatical_number="unknown",
        translation_policy="undecided",
        status="proposed",
        version=1,
        never_translate=False,
        allow_inflection=True,
    )
    db.add(new_entity)

    if alias is not None:
        # remove the alias from the source entity; it becomes the canonical
        # source of the new (proposed) entity. No evidence is moved: the new
        # entity starts empty and must be reviewed (§6.6).
        db.delete(alias)
    else:
        # it's a new canonical form: record it on the new entity
        db.add(EntityAlias(
            id=str(uuid.uuid4()),
            entity_id=new_entity.id,
            source_alias=payload.canonical_source,
            target_alias=payload.canonical_target,
            alias_type="canonical",
        ))

    new_entity.updated_at = datetime.utcnow()
    entity.updated_at = datetime.utcnow()
    entity.version += 1
    db.add(entity)

    _invalidate_for_entity(db, entity.id, payload.canonical_source)

    db.add(AuditLog(
        project_id=project_id,
        action="entity_split",
        entity="entity",
        entity_id=str(entity.id),
        before={"split_from": str(entity.id)},
        after={"split_into": str(new_entity.id)},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return _entity_summary(db, new_entity)


# --- versions (§15.4) -------------------------------------------------------
@router.get("/{project_id}/entities/{entity_id}/versions")
async def list_entity_versions(
    project_id: str,
    entity_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """The entity's immutable edit history (§15.4)."""
    _get_project(db, project_id)
    _get_entity(db, project_id, entity_id)
    rows = (
        db.query(EntityVersion)
        .filter(EntityVersion.entity_id == entity_id)
        .order_by(EntityVersion.version)
        .all()
    )
    return {
        "entity_id": str(entity_id),
        "versions": [
            {
                "version": v.version,
                "action": v.action,
                "snapshot": v.snapshot,
                "created_at": v.created_at.isoformat()
                if v.created_at else None,
            }
            for v in rows
        ],
    }


# --- chapter intro / delta (§6.6) -------------------------------------------
@router.get("/{project_id}/entities/chapter/{node_id}")
async def chapter_entities(
    project_id: str,
    node_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Entities introduced in a chapter and the delta vs the previous one.

    "Introduced" = has at least one evidence whose ``chapter_id`` is this
    chapter. The delta is the set difference vs the previous chapter's
    introduced set (§6.6: "entità introdotte per capitolo e delta").
    """
    _get_project(db, project_id)
    # chapters in reading order
    chapters = (
        db.query(Entity)
        .filter(Entity.project_id == project_id)
        .all()
    )
    # map chapter_id -> set of entity ids introduced there
    per_chapter: dict[str, set[str]] = {}
    for e in chapters:
        for ev in e.evidence:
            if ev.chapter_id:
                per_chapter.setdefault(str(ev.chapter_id), set()).add(str(e.id))

    # order chapters by the earliest page of any evidence they hold
    def _first_page(cid: str) -> int:
        rows = (
            db.query(EntityEvidence.page_number)
            .filter(EntityEvidence.chapter_id == cid)
            .all()
        )
        pages = [r[0] for r in rows if r[0] is not None]
        return min(pages) if pages else 10**9

    ordered = sorted(per_chapter.keys(), key=_first_page)
    idx = ordered.index(str(node_id)) if str(node_id) in ordered else -1

    introduced = per_chapter.get(str(node_id), set())
    previous = (
        per_chapter.get(ordered[idx - 1], set()) if idx > 0 else set()
    )
    delta = introduced - previous  # newly introduced this chapter
    returned = previous - introduced  # no longer introduced (for reference)

    def _out(eids: set[str]) -> list[dict]:
        out = []
        for eid in eids:
            e = db.get(Entity, eid)
            if e is not None:
                out.append(_entity_summary(db, e))
        return out

    return {
        "chapter_id": str(node_id),
        "introduced_count": len(introduced),
        "new_count": len(delta),
        "introduced": _out(introduced),
        "delta": _out(delta),
        "returned": _out(returned),
    }



# --- helpers ----------------------------------------------------------------
def _invalidate_for_entity(
    db: Session, entity_id: str, note: str | None
) -> None:
    """Flag approved segments that referenced this entity (§15.4).

    An approved edit to an entity (merge/split/approve) can make approved
    translations inconsistent; we flag them for QA rather than rewriting
    them (§15.4: "senza sovrascriverli").
    """
    from .models import TranslationUnit

    entity = db.get(Entity, entity_id)
    if entity is None:
        return
    # segments whose source text mentions the (surviving) canonical source
    needle = entity.canonical_source
    if not needle:
        return
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == entity.project_id,
                TranslationUnit.status == "approved")
        .all()
    )
    for unit in units:
        if needle.lower() in (unit.source_text or "").lower():
            db.add(EntityInvalidation(
                id=str(uuid.uuid4()),
                entity_id=str(entity.id),
                segment_id=str(unit.id),
                reason=note,
            ))

