"""Controllo della coda job (§16): stop globale della coda.

``POST /queue/stop`` -- annulla TUTTI i job in coda (qualsiasi tipo:
segmentazione, traduzione, estrazione...). I job in esecuzione non sono
interrompibili: finiscono il lavoro corrente e vengono riportati nel
conteggio ``running_left``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .audit import log_event
from .db import get_db_session
from .models import Job

router = APIRouter(prefix="/queue", tags=["queue"])


@router.post("/stop")
async def stop_queue(db: Session = Depends(get_db_session)) -> dict:
    """Annulla ogni job in coda; i job in esecuzione terminano il lavoro."""
    now = datetime.now(timezone.utc)
    queued = db.query(Job).filter(Job.status == "queued").all()
    cancelled = 0
    for j in queued:
        j.status = "cancelled"
        j.error = "coda annullata dall'utente"
        j.completed_at = now
        db.add(j)
        log_event(
            db=db,
            action="queue_job_cancelled",
            entity="job",
            entity_id=str(j.id),
            project_id=str(j.project_id) if j.project_id else None,
            after={"job_type": j.job_type},
        )
        cancelled += 1
    running_left = (
        db.query(Job).filter(Job.status == "running").count()
    )
    if cancelled:
        db.commit()
    return {"cancelled": cancelled, "running_left": running_left}
