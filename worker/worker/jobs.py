"""Example worker jobs.

Real jobs (PDF parsing with PyMuPDF/Docling, OCR with olmOCR/PaddleOCR,
BookNLP structure detection, quality estimation) are added in later phases.
Each job is idempotent and logs no manuscript payload (see PRD §13.1).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .celery import app


@dataclass(frozen=True)
class JobResult:
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail}


@app.task(name="trans.ping", bind=True)
def ping(self: object) -> dict:
    """Smoke-test job used by the worker healthcheck."""
    return JobResult("ok", "pong").to_dict()


@app.task(name="trans.hash", bind=True)
def sha256_text(self: object, text: str) -> dict:
    """Compute a SHA-256 digest (used for immutable versioning)."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return JobResult("ok", {"sha256": digest}).to_dict()
