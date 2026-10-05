"""OCR runner: idempotent, resumable OCR of document pages (PRD 5.2, 14).

Runs the pure :mod:`backend.parsing.ocr` layer over a stored document and
UPSERTs one :class:`~backend.models.DocumentPage` row per OCR-ed page,
mirroring :mod:`backend.l1_runner` so queue workers (Celery, queue ``ocr``)
and the in-process scheduler share the same code path (§14: OCR runs in its
own queue, decoupled from the API process).

Pipeline per requested page (ADR-002 escalation):

1. **L3** — baseline OCR (tesseract ``--psm 3``, the fallback of the olmOCR
   slot). If the page comes back suspect (low line confidences or no kept
   line), and L4 has not already produced a better result,
2. **L4** — quality pass (embedded-resolution raster, LANCZOS upscale,
   median de-speckle, autocontrast, ``--psm 11``). Its result replaces the
   L3 attempt when it is *less suspect*; both attempts stay recorded in the
   page payload under ``attempts``.

Design constraints (identical to the L1 runner):

* **Idempotent** — pages are UPSERTed on ``(document_id, page_number)``;
  re-running a finished or half-finished job converges to the same rows
  (hashes are content-derived).
* **Resumable** — already-OCR-ed pages carrying the same ``document_sha256``
  are skipped on resume, so a restarted job continues where it stopped.
* **Re-OCR selettivo** — ``page_numbers=[...]`` restricts the run to
  specific pages (§5.2: re-OCR per pagina su richiesta); ``force=True``
  re-OCRs pages regardless of the resume marker.
* **Flag propagation** — a page flagged ``ocr_suspect`` sets
  ``{"ocr_suspect": true, "ocr_page": N}`` on the ``source_flags`` of every
  translation unit whose provenance points at that page (§10.2 QA / §11.2
  UI filter); units already approved are left untouched.
* **Progress** — ``job.result.progress`` is updated per page (§14).
* **Local only** — the only I/O is the local DB, the local object store and
  the local tesseract binary; nothing about the manuscript is logged.

Segment propagation detail: the F1 import does not create translation units
yet (that is the segmentation phase), so the propagation pass links by
``source_flags['ocr_page']`` when units exist — created later or by tests —
and re-runs cheaply on every OCR job, keeping flags convergent (§14
idempotency).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    Document,
    DocumentPage,
    ImportReport,
    Job,
    TranslationUnit,
)
from .parsing import ocr
from .storage import get_storage_provider


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


def _merge_import_report(db: Session, document_id: str,
                         ocr_pages: list[int], suspect_pages: list[int],
                         levels: dict[int, str]) -> None:
    """Merge the OCR outcome into the 5.2.6 import report (§5.2.6)."""
    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == document_id)
        .one_or_none()
    )
    if report is None:
        report = ImportReport(document_id=document_id, summary={})
    summary = dict(report.summary or {})
    summary["ocr"] = {
        "pages_ocr_done": sorted(set(ocr_pages)),
        "pages_suspect": sorted(set(suspect_pages)),
        "levels": {str(p): lvl for p, lvl in sorted(levels.items())},
        "engine": "tesseract (olmOCR/PaddleOCR fallback)",
        "updated_at": datetime.utcnow().isoformat(),
    }
    report.summary = summary
    report.pages_ocr_needed = len(set(ocr_pages))
    report.updated_at = datetime.utcnow()
    db.add(report)


def _propagate_segment_flags(db: Session, document_id: str,
                             suspect_pages: list[int]) -> int:
    """Set/clear ``source_flags['ocr_suspect']`` on translation units.

    Units are matched by ``source_flags['ocr_page']``; for each flagged page
    every unit of the project pointing at it gets ``ocr_suspect: true``
    (§10.2 QA input, §11.2 UI filter). Units whose page is no longer
    suspect get the flag cleared, so repeated runs converge.
    """
    doc = db.get(Document, document_id)
    if doc is None:
        raise ValueError("unknown document_id in job payload")
    suspect = {int(p) for p in suspect_pages}
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == doc.project_id)
        .all()
    )
    touched = 0
    for unit in units:
        flags = dict(unit.source_flags or {})
        page = flags.get("ocr_page")
        if page is None:
            continue
        wanted = int(page) in suspect
        if bool(flags.get("ocr_suspect")) != wanted:
            flags["ocr_suspect"] = wanted
            unit.source_flags = flags
            db.add(unit)
            touched += 1
    return touched


def run_ocr_extraction(db: Session, job: Job) -> None:
    """OCR the pages of ``job.payload['document_id']`` (optionally a subset).

    Payload keys: ``document_id`` (required), ``page_numbers`` (optional
    list of 1-based pages for selective re-OCR), ``force`` (bool, re-OCR
    pages that already carry an OCR result).

    Raises on hard failures (missing document/asset, unusable engine) so the
    scheduler marks the job ``failed``. Per-page engine failures do NOT
    abort the run: the page is recorded with ``status='error'`` and flagged
    suspect (§5.2.6 report), keeping the job resumable.
    """
    document_id = job.payload["document_id"]
    wanted = job.payload.get("page_numbers") or None
    force = bool(job.payload.get("force"))

    doc = db.get(Document, document_id)
    if doc is None:
        raise ValueError("unknown document_id in job payload")
    storage = get_storage_provider()
    data = storage.get(doc.storage_key)
    if data is None:
        raise ValueError(f"stored asset missing at {doc.storage_key!r}")
    if not ocr.tesseract_available():
        raise ValueError("tesseract binary not available on this host")

    total_pages = doc.page_count or 0
    if total_pages == 0:
        # No page count yet: open the PDF purely to count pages.
        import pymupdf

        pdf = pymupdf.open(stream=data, filetype="pdf")
        total_pages = pdf.page_count
        doc.page_count = total_pages
        pdf.close()

    targets = sorted(
        {int(p) for p in wanted} if wanted else set(range(1, total_pages + 1))
    )
    if targets and (targets[0] < 1 or targets[-1] > total_pages):
        raise ValueError(
            f"page_numbers out of range 1..{total_pages}: {targets}")

    pages_done = 0
    suspect_pages: list[int] = []
    done_levels: dict[int, str] = {}
    escalated: list[int] = []

    for page_number in targets:
        page_index = page_number - 1
        existing = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id,
                    DocumentPage.page_number == page_number)
            .one_or_none()
        )
        payload_marker = ((existing.page_payload or {}) if existing else {})
        if (existing is not None and not force
                and payload_marker.get("document_sha256") == doc.sha256
                and payload_marker.get("ocr_record") is not None):
            # Resume: this page already has an OCR result from this asset.
            pages_done += 1
            _set_progress(db, job, "skipping", pages_done, len(targets))
            continue

        attempts: list[dict] = []
        best: dict | None = None
        error: str | None = None
        for level in ("L3", "L4"):
            try:
                record = ocr.ocr_page(data, page_index, level=level)
            except Exception as exc:  # noqa: BLE001 - engine hiccup
                error = f"{type(exc).__name__}: {exc}"
                break  # engine-level failure: no point trying the next level
            attempts.append({
                "level": level,
                "line_count": record["line_count"],
                "mean_line_conf": record["mean_line_conf"],
                "suspect": record["ocr_suspect"],
            })
            if best is None or (
                best["ocr_suspect"] and not record["ocr_suspect"]
            ):
                best = record
            if level == "L3" and record["ocr_suspect"]:
                escalated.append(page_number)  # ADR-002 escalation to L4
            if not record["ocr_suspect"]:
                break  # good enough: stay at this level
        if best is None:
            # Engine unusable for this page: record an error page, flagged.
            record = {
                "page_number": page_number,
                "level": None,
                "extractor": "tesseract",
                "confidence": 0.0,
                "char_count": 0,
                "ocr_suspect": True,
                "line_count": 0,
                "suspect_lines": 0,
                "suspect_line_ratio": 1.0,
                "mean_line_conf": 0.0,
                "lines": [],
                "normalized_text": "",
                "status": "error",
                "error": error,
            }
            record["text_sha256"] = ocr.sha256_json(
                {"page": page_index, "status": "error"})
            record["page_sha256"] = ocr.sha256_json(
                {"page": page_index, "status": "error"})
            best = record

        ocr_payload = {
            "document_sha256": doc.sha256,
            "ocr_record": {
                "level": best["level"],
                "extractor": best["extractor"],
                "psm": best.get("psm"),
                "raster_dpi": best.get("raster_dpi"),
                "confidence": best["confidence"],
                "mean_line_conf": best.get("mean_line_conf", 0.0),
                "ocr_suspect": best["ocr_suspect"],
                "line_count": best["line_count"],
                "char_count": best["char_count"],
                "lines": best["lines"],
                "attempts": attempts,
                "status": best["status"],
            },
        }

        if existing is None:
            page_row = DocumentPage(
                document_id=document_id, page_number=page_number)
        else:
            page_row = existing
        ocr_rec = ocr_payload["ocr_record"]
        page_row.extractor = ocr_rec["extractor"]
        page_row.confidence = ocr_rec["confidence"]
        page_row.char_count = ocr_rec["char_count"]
        page_row.normalized_text = best["normalized_text"]
        page_row.ocr_suspect = bool(ocr_rec["ocr_suspect"])
        page_row.ocr_level = ocr_rec["level"]
        page_row.ocr_mean_line_conf = ocr_rec["mean_line_conf"]
        # page_payload keeps the L1 record (if any) beside the OCR record:
        # previous extraction results stay available and the payload hashes
        # are refreshed to describe what is being stored now.
        merged_payload = dict(payload_marker or {})
        merged_payload.update(ocr_payload)
        merged_payload["text_sha256"] = best["text_sha256"]
        page_row.page_payload = merged_payload
        page_row.text_sha256 = best["text_sha256"]
        page_row.page_sha256 = ocr.sha256_json(merged_payload)
        db.add(page_row)

        pages_done += 1
        if ocr_rec["ocr_suspect"]:
            suspect_pages.append(page_number)
        if ocr_rec["level"]:
            done_levels[page_number] = ocr_rec["level"]
        _set_progress(db, job, "ocr", pages_done, len(targets))

    _merge_import_report(db, document_id, targets, suspect_pages, done_levels)
    touched_units = _propagate_segment_flags(db, document_id, suspect_pages)

    if doc.status in ("stored", "parsed", "ocr_done", "ocr_suspect"):
        doc.status = "ocr_suspect" if suspect_pages else "ocr_done"
    db.add(doc)
    _set_progress(db, job, "completed", len(targets), len(targets))
    job.result = {
        **(job.result or {}),
        "ocr": {
            "document_id": str(document_id),
            "pages_requested": len(targets),
            "pages_done": pages_done,
            "pages_suspect": suspect_pages,
            "pages_escalated": sorted(set(escalated)),
            "levels": {str(p): lvl for p, lvl in sorted(done_levels.items())},
            "segments_flagged": touched_units,
        },
    }
    db.add(job)
    db.add(AuditLog(
        action="document_ocr_done",
        entity="document",
        entity_id=str(document_id),
        after={
            "pages": pages_done,
            "pages_suspect": suspect_pages,
            "levels": done_levels,
        },
        created_at=datetime.utcnow(),
    ))
    db.commit()
