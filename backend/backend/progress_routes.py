"""Progresso della traduzione (§11.1): indicatore globale in sidebar.

``GET /translation-progress`` -- stato dell'eventuale campagna di traduzione
in corso (finestra 24h, qualsiasi progetto): segmenti tradotti sul totale del
progetto, job per stato e stima dei tempi. L'ETA usa il ritmo osservato
(segmenti/minuto dal primo job della campagna) e si affina automaticamente a
ogni job completato, perché il calcolo riparte sempre dai dati correnti.

``POST /translation-progress/stop``    -- annulla i job translate in coda
(il job in esecuzione finisce il blocco corrente).
``POST /translation-progress/resume``  -- riaccoda la traduzione di tutti i
segmenti non tradotti, a blocchi entro il budget §5.4: i blocchi già
completati non vengono ricalcolati (resume §8.4 per run_ref).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .audit import log_event
from .db import get_db_session
from .models import AuditLog, Job, TranslationUnit
from .scheduler import get_scheduler_from
from .translation.planner import count_tokens

router = APIRouter(prefix="/translation-progress", tags=["translation"])

WINDOW_HOURS = 24
# Blocchi di segmentazione per il resume (§5.4: 20 segmenti come la UI,
# mai oltre il budget sorgente per blocco).
RESUME_MAX_SEGMENTS = 20
# blocchi allineati al nuovo budget 16k in / 16k out (2026-09-21)
RESUME_MAX_SOURCE_TOKENS = 16_384


@router.get("")
async def translation_progress(db: Session = Depends(get_db_session)) -> dict:
    """Progresso della traduzione per l'indicatore della sidebar."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)

    latest = (
        db.query(Job)
        .filter(Job.job_type == "translate", Job.created_at >= since)
        .order_by(Job.created_at.desc())
        .first()
    )
    if latest is None:
        return {"active": False}

    project_id = latest.project_id
    jobs = (
        db.query(Job)
        .filter(Job.job_type == "translate",
                Job.project_id == project_id,
                Job.created_at >= since)
        .all()
    )
    completed = [j for j in jobs if j.status == "completed"]
    failed = [j for j in jobs if j.status == "failed"]
    pending = [j for j in jobs if j.status in ("queued", "running")]

    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id)
        .all()
    )
    total_units = len(units)
    translated_units = sum(1 for u in units if (u.target_text or "").strip())

    # Stima dei tempi (metodo richiesto 2026-09-21): per ogni job completato
    # si memorizza sec/carattere (durata / caratteri sorgente del blocco);
    # l'ETA e la MEDIA dei sec/char per i caratteri rimanenti. Si ricalcola
    # a ogni terminazione di job, quindi affina progressivamente.
    per_job_rates: list[float] = []
    durations: list[float] = []
    chars_list: list[int] = []
    remaining_chars = 0
    for u in units:
        if (u.target_text or "").strip():
            continue
        remaining_chars += len(u.source_text or "")

    for j in completed:
        if not (j.started_at and j.completed_at
                and j.completed_at >= j.started_at):
            continue
        segs = (j.payload or {}).get("segments") or []
        chars = sum(len(s.get("source_text") or "") for s in segs)
        dur = (j.completed_at - j.started_at).total_seconds()
        if chars <= 0 or dur <= 0:
            continue
        per_job_rates.append(dur / chars)
        chars_list.append(chars)
        durations.append(dur)

    avg_sec_per_char = (
        sum(per_job_rates) / len(per_job_rates) if per_job_rates else None
    )
    avg_job_seconds = (
        round(sum(durations) / len(durations), 1) if durations else None
    )
    # ETA dal ritmo PESATO per dimensione (durata totale / caratteri
    # totali): la media semplice e' dominata dai job minuscoli.
    # La stima e' esposta SOLO con job effettivamente in coda/esecuzione:
    # a coda vuota non c'e' nessuna predizione da mostrare.
    dur_total = sum(durations)
    chars_total = sum(chars_list)
    weighted = dur_total / chars_total if chars_total else None
    eta_seconds = (
        int(remaining_chars * weighted)
        if weighted and remaining_chars and pending
        else None
    )

    return {
        "active": True,
        "project_id": str(project_id),
        "total_units": total_units,
        "translated_units": translated_units,
        "jobs_total": len(jobs),
        "jobs_completed": len(completed),
        "jobs_failed": len(failed),
        "jobs_pending": len(pending),
        "avg_job_seconds": avg_job_seconds,
        "eta_seconds": eta_seconds,
    }


def _active_project_id(db: Session) -> str | None:
    """Progetto con l'attività di traduzione più recente (finestra 24h)."""
    latest = (
        db.query(Job)
        .filter(Job.job_type == "translate", Job.created_at >= since_24h())
        .order_by(Job.created_at.desc())
        .first()
    )
    return str(latest.project_id) if latest else None


def since_24h() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)


@router.post("/stop")
async def stop_translation(db: Session = Depends(get_db_session)) -> dict:
    """Annulla i job translate in coda (il job in esecuzione finisce il
    blocco corrente, poi la fila si ferma)."""
    project_id = _active_project_id(db)
    if not project_id:
        return {"cancelled": 0, "running_left": 0}
    now = datetime.now(timezone.utc)
    queued = (
        db.query(Job)
        .filter(Job.job_type == "translate",
                Job.project_id == project_id,
                Job.created_at >= since_24h(),
                Job.status.in_(["queued", "running"]))
        .all()
    )
    cancelled = 0
    running_left = 0
    for j in queued:
        if j.status == "queued":
            j.status = "cancelled"
            j.error = "annullato dall'utente"
            j.completed_at = now
            db.add(AuditLog(
                action="translation_cancelled",
                entity="job",
                entity_id=str(j.id),
                after={"job_type": j.job_type},
                created_at=now,
            ))
            cancelled += 1
        else:
            # il job in esecuzione termina il blocco corrente
            running_left += 1
    if cancelled or running_left:
        db.commit()
    return {"cancelled": cancelled, "running_left": running_left}


@router.post("/resume")
async def resume_translation(db: Session = Depends(get_db_session)) -> dict:
    """Riaccoda la traduzione di tutti i segmenti non tradotti del progetto
    con attività recente, a blocchi entro il budget §5.4. I blocchi già
    completati non vengono ricalcolati (resume per run_ref, §8.4)."""
    project_id = _active_project_id(db)
    if not project_id:
        raise HTTPException(
            status_code=404,
            detail="nessuna attività di traduzione nelle ultime 24 ore",
        )

    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.status == "untranslated")
        .order_by(TranslationUnit.chapter_id, TranslationUnit.ordinal)
        .all()
    )
    if not units:
        return {"enqueued": 0, "segments": 0}

    scheduler = get_scheduler_from(db)
    by_chapter: dict[str, list[TranslationUnit]] = {}
    for u in units:
        by_chapter.setdefault(str(u.chapter_id), []).append(u)

    enqueued = 0
    segments = 0
    for _chapter_id, ch_units in by_chapter.items():
        block: list[TranslationUnit] = []
        block_tokens = 0
        for u in ch_units:
            t = count_tokens(u.source_text or "")
            if block and (len(block) >= RESUME_MAX_SEGMENTS
                          or block_tokens + t > RESUME_MAX_SOURCE_TOKENS):
                _enqueue_block(scheduler, project_id, block)
                enqueued += 1
                segments += len(block)
                block, block_tokens = [], 0
            block.append(u)
            block_tokens += t
        if block:
            _enqueue_block(scheduler, project_id, block)
            enqueued += 1
            segments += len(block)

    log_event(action="translation_resumed", entity="project",
              project_id=project_id,
              after={"blocks": enqueued, "segments": segments})
    return {"enqueued": enqueued, "segments": segments}


def _enqueue_block(scheduler, project_id: str,
                   block: list[TranslationUnit]) -> None:
    payload = {
        "project_id": project_id,
        # allineato al nuovo budget 16k in / 16k out (2026-09-21)
        "max_output_tokens": 16_384,
        "segments": [
            {"segment_id": str(u.id), "source_text": u.source_text or "",
             "chapter_id": str(u.chapter_id) if u.chapter_id else None,
             "ordinal": u.ordinal}
            for u in block
        ],
        "block_source": " ".join(u.source_text or "" for u in block),
    }
    job = scheduler.register(project_id, "translate", payload)
    scheduler.enqueue(job)
