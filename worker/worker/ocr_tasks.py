"""OCR jobs for the Celery worker pool (PRD 5.2 step 4, 14, ADR-002 L3/L4).

Mirrors :mod:`worker.l1_tasks`: the job row is loaded in the worker's own
DB session and the shared runner (``backend.ocr_runner``) does the work, so
queue workers and the in-process scheduler behave identically.

§14: OCR runs on a DEDICATED queue (``ocr``) so slow page recognition never
competes with API-critical jobs; route the worker accordingly::

    celery -A worker.celery:app worker -Q ocr,trans
"""
from __future__ import annotations

from datetime import datetime

from .celery import app

# Un documento intero può impiegare oltre un'ora (tesseract ~120s/pagina):
# senza limiti il task resta appeso per sempre e blocca il worker (§14).
OCR_SOFT_LIMIT = 3600
OCR_HARD_LIMIT = 3900


def _run_ocr_job(task, job_id: str) -> dict:
    from backend.db import SessionLocal
    from backend.models import Job
    from backend.ocr_runner import run_ocr_extraction

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "error", "detail": f"job {job_id} not found"}
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()
        run_ocr_extraction(db, job)
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        db.commit()
        return {"status": "ok", "detail": {"job_id": job_id}}
    except Exception as exc:  # noqa: BLE001 - retry with backoff (§14)
        db.rollback()
        # fix 2026-09-18: max_retries era dichiarato ma il retry non avveniva
        # mai (la sola raise segna il task come failed al primo tentativo).
        raise task.retry(exc=exc)
    finally:
        db.close()


@app.task(name="trans.ocr_document", bind=True, max_retries=3,
          default_retry_delay=10, queue="ocr",
          soft_time_limit=OCR_SOFT_LIMIT, time_limit=OCR_HARD_LIMIT)
def ocr_document(self, job_id: str) -> dict:
    """OCR every page of one queued job's document (idempotent, resumable)."""
    return _run_ocr_job(self, job_id)


@app.task(name="trans.ocr_pages", bind=True, max_retries=3,
          default_retry_delay=10, queue="ocr",
          soft_time_limit=OCR_SOFT_LIMIT, time_limit=OCR_HARD_LIMIT)
def ocr_pages(self, job_id: str) -> dict:
    """Selective re-OCR: the job payload carries ``page_numbers``."""
    return _run_ocr_job(self, job_id)
