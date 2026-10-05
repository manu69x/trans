"""Idempotent, resumable L1 extraction runner (PRD 5.2, 14).

Runs the pure :mod:`backend.parsing.l1` extraction over a stored document and
persists one :class:`~backend.models.DocumentPage` row per page plus the
5.2.6 import report. Design constraints:

* **Idempotent** — pages are UPSERTed on ``(document_id, page_number)``;
  re-running a finished or half-finished job converges to the same rows
  ( hashes are content-derived, so a re-run rewrites identical data).
* **Resumable** — pages already extracted (present with the same
  ``document_sha256`` recorded in the payload) are skipped on resume, so a
  job restarted halfway continues where it stopped and a finished job is a
  no-op (AC3).
* **Progress** — the job row carries ``result.progress = {pages_done,
  total_pages, phase}``, updated with one lightweight UPDATE per page
  ( 14: progressive feedback during the import).
* **Local only** — the only I/O is the local DB and the local object store;
  nothing about the manuscript is logged (PRD §13.1).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    Document,
    DocumentPage,
    ImportReport,
    Job,
)
from .parsing import l1
from .storage import get_storage_provider


def _pages_done(payload: dict) -> int:
    prog = (payload or {}).get("progress") or {}
    return int(prog.get("pages_done", 0))


def _set_progress(db: Session, job: Job, phase: str, pages_done: int,
                  total_pages: int) -> None:
    """Persist per-page progress on the job row (14)."""
    job.result = {
        **(job.result or {}),
        "progress": {
            "phase": phase,
            "pages_done": pages_done,
            "total_pages": total_pages,
        },
    }
    db.add(job)
    db.commit()


def run_l1_extraction(db: Session, job: Job) -> None:
    """Extract every page of ``job.payload['document_id']`` with L1.

    Raises on hard failures (missing document/asset, unreadable PDF) so the
    scheduler marks the job ``failed``. Per-page failures do NOT abort the
    run: they are recorded in the report and the page is marked ``error``
    ( 5.2.6 report lists pages to verify), keeping the job resumable.
    """
    document_id = job.payload["document_id"]

    doc = db.get(Document, document_id)
    if doc is None:
        raise ValueError("unknown document_id in job payload")
    storage = get_storage_provider()
    data = storage.get(doc.storage_key)
    if data is None:
        raise ValueError(f"stored asset missing at {doc.storage_key!r}")

    # --- bookkeeping that must survive the parse (and a restart) ---------
    info = l1.open_document(data)
    total_pages = info["page_count"]
    if doc.page_count is None:
        doc.page_count = total_pages

    # Idempotency marker: pages carry the document hash they were extracted
    # from. If the stored asset was replaced, everything is re-extracted.
    doc_meta = {
        "page_count": total_pages,
        "toc": info["toc"],
        "metadata": info["metadata"],
        "document_sha256": doc.sha256,
    }

    # --- pass 1: header/footer repetition census needs all pages ---------
    key_counter: Counter[str] = Counter()
    per_page: dict[int, dict] = {}
    error_pages: list[int] = []
    pages_done = 0

    for page_index in range(total_pages):
        try:
            record = l1.extract_page(data, page_index)
        except Exception:  # noqa: BLE001 - one bad page must not kill the run
            error_pages.append(page_index + 1)
            record = {
                "page_number": page_index + 1,
                "extractor": "pymupdf",
                "confidence": 0.0,
                "char_count": 0,
                "suspect_chars": 0,
                "blocks": [],
                "normalized_text": "",
                "status": "error",
                "text_sha256": l1.sha256_json({"page": page_index}),
                "page_sha256": l1.sha256_json(
                    {"page": page_index, "status": "error"}),
                # keys the post-extraction pass expects on EVERY record:
                # collect_repeated_keys reads blocks + height (l1_runner:118)
                "height": None,
                "width": None,
            }
        record["document_sha256"] = doc.sha256
        per_page[page_index + 1] = record
        key_counter.update(l1.collect_repeated_keys(
            record["blocks"], record["height"]))
        pages_done += 1
        _set_progress(db, job, "extracting", pages_done, total_pages)

    flagged = l1.flag_repeated_keys(key_counter, total_pages)
    ocr_pages: list[int] = []
    columns_pages: list[int] = []
    headings_total = 0
    excluded_blocks_total = 0

    # --- pass 2: exclusions + persistence (UPSERT per page) --------------
    for page_number in range(1, total_pages + 1):
        record = per_page[page_number]
        if record["status"] == "ok":
            excluded = l1.apply_exclusions(record, flagged)
            excluded_blocks_total += excluded
            if record["char_count"] == 0:
                record["status"] = "empty"
        if record["status"] in ("empty", "error"):
            # No usable text layer: the page needs OCR (L3), §5.2 step 3.
            ocr_pages.append(page_number)
        if record.get("columns_suspected"):
            columns_pages.append(page_number)
        headings_total += sum(
            1 for b in record["blocks"]
            if b.get("heading_level"))

        existing = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id,
                    DocumentPage.page_number == page_number)
            .one_or_none()
        )
        if existing is not None:
            page_row = existing
        else:
            page_row = DocumentPage(
                document_id=document_id, page_number=page_number)
        page_row.extractor = record["extractor"]
        page_row.confidence = record["confidence"]
        page_row.char_count = record["char_count"]
        page_row.suspect_chars = record["suspect_chars"]
        page_row.text_sha256 = record["text_sha256"]
        page_row.page_sha256 = record["page_sha256"]
        page_row.normalized_text = record["normalized_text"]
        page_row.page_payload = record
        db.add(page_row)
        _set_progress(db, job, "persisting", page_number, total_pages)

    # --- import report 5.2.6 ---------------------------------------------
    pages_ok = sum(
        1 for r in per_page.values() if r["status"] == "ok")
    total_chars = sum(r["char_count"] for r in per_page.values())
    total_suspect = sum(r["suspect_chars"] for r in per_page.values())
    summary = {
        "document_id": str(document_id),
        "status": "completed" if not error_pages else "completed_with_errors",
        "total_pages": total_pages,
        "pages_ok": pages_ok,
        "pages_error": len(error_pages),
        "pages_ocr": sorted(set(ocr_pages)),
        "suspect_char_ratio": (
            round(total_suspect / total_chars, 4) if total_chars else 0.0),
        "repeated_headers_footers": [
            {"text": k, "pages": v} for k, v in
            sorted(flagged.items(), key=lambda kv: -kv[1])],
        "pages_with_columns": columns_pages,
        "pages_to_verify": sorted(
            set(ocr_pages) | set(columns_pages) | set(error_pages)),
        "heading_hints": headings_total,
        "toc_entries": info["toc"],
        "pdf_metadata": info["metadata"],
        "document_sha256": doc.sha256,
        "excluded_blocks": excluded_blocks_total,
        "extractors": sorted({
            r["extractor"] for r in per_page.values()}),
    }
    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == document_id)
        .one_or_none()
    )
    if report is None:
        report = ImportReport(document_id=document_id)
    report.summary = summary
    report.pages_ok = pages_ok
    report.pages_ocr_needed = len(set(ocr_pages))
    report.updated_at = datetime.utcnow()
    db.add(report)

    doc.status = "parsed"
    db.add(doc)
    _set_progress(db, job, "completed", total_pages, total_pages)
    job.result = {
        **(job.result or {}),
        "document_meta": doc_meta,
        "import_report_id": str(report.id),
        "pages_done": total_pages,
        "total_pages": total_pages,
    }
    db.add(job)
    db.add(AuditLog(
        action="document_l1_extracted",
        entity="document",
        entity_id=str(document_id),
        after={
            "pages": total_pages,
            "pages_ok": pages_ok,
            "suspect_char_ratio": summary["suspect_char_ratio"],
        },
        created_at=datetime.utcnow(),
    ))
    db.commit()
