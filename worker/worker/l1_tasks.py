"""L1 extraction job for the Celery worker pool (PRD 5.2, ADR-002 L1).

Mirrors the backend's ``parse_l1`` scheduler handler: loads the job row in
its own DB session, runs the idempotent/resumable extraction and lets the
runner persist pages + report. Queue workers and the in-process scheduler
share the same code path (backend.l1_runner), so behaviour is identical.
"""
from __future__ import annotations

from datetime import datetime

from .celery import app


@app.task(name="trans.parse_l1", bind=True, max_retries=3,
          default_retry_delay=10)
def parse_l1(self, job_id: str) -> dict:
    """Run the L1 extraction for one queued job (by id).

    Idempotent: safe to retry after a crash or a worker restart (AC3) --
    already-extracted pages are re-UPSERTed with identical content hashes.
    """
    from backend.db import SessionLocal
    from backend.l1_runner import run_l1_extraction
    from backend.models import Job

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "error", "detail": f"job {job_id} not found"}
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()
        run_l1_extraction(db, job)
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        db.commit()
        return {"status": "ok", "detail": {"job_id": job_id}}
    except Exception as exc:  # noqa: BLE001 - retry with backoff (14)
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()
