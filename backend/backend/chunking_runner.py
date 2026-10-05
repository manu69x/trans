"""Chapter segmentation runner: PRD §5.4 translation_units + LLM blocks.

Consumes the pages the import extracted (L1 + OCR runners) and the §5.3
structure nodes, segments one chapter and persists the CAT segments as
:class:`~backend.models.TranslationUnit` rows.  Mirrors
:mod:`backend.structure_runner` / :mod:`backend.ocr_runner`:

* **Idempotent** — the same chapter text produces the same stable segment
  IDs (uuid5 of project+chapter+ordinal) and the same ``source_hash``;
  re-running the job converges to the same state (§14).
* **Immutable approvals** — segments a user already ``approved`` are never
  touched nor deleted (§5.1); only non-approved segments are replaced.
* **Local only** — everything runs in-process; no LLM is called here.  The
  planner measures tokens with the local tokenizer (ADR-001 §3.4) and the
  blocks it emits are the *budget* the translation adapter must respect.
* **Sanitised logs** — the job result and the audit log carry counts, IDs
  and hashes only, never manuscript text (§13.1).
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
    Project,
    StructureNode,
    TranslationUnit,
)
from .parsing import chunking, structure
from .segment_numbering import next_numero_start


def _set_progress(db: Session, job: Job, phase: str, done: int,
                  total: int) -> None:
    job.result = {
        **(job.result or {}),
        "progress": {"phase": phase, "steps_done": done, "steps_total": total},
    }
    db.add(job)
    db.commit()


def _project_page_payloads(db: Session, project_id: str) -> dict[int, dict]:
    """All extracted pages of the project, in reading order."""
    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    payloads: dict[int, dict] = {}
    for document in documents:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        for row in rows:
            payload = dict(row.page_payload or {})
            payload["_page_row_id"] = str(row.id)
            payload["_document_id"] = str(document.id)
            payloads[int(row.page_number)] = payload
    return payloads


def _confirmed_header_keys(db: Session, page_payloads: dict[int, dict],
                           first_document_id: str | None) -> set[str]:
    """Header/footer line keys that are algorithmically confirmed (§5.3).

    Prefers the import report's repetition census; falls back to a census
    computed over the stored pages (same ``repeated_line_key`` semantics).
    """
    repeated: list[dict] = []
    if first_document_id is not None:
        report = (
            db.query(ImportReport)
            .filter(ImportReport.document_id == first_document_id)
            .one_or_none()
        )
        repeated = list((report.summary or {})
                        .get("repeated_headers_footers") or [])
    if not repeated:
        counts: dict[str, int] = {}
        for payload in page_payloads.values():
            keys = {
                structure.repeated_line_key(ln.get("text") or "")
                for ln in structure.page_lines(payload)
            }
            for key in keys:
                if key:
                    counts[key] = counts.get(key, 0) + 1
        repeated = [{"text": k, "pages": v} for k, v in counts.items()]
    total_pages = len(page_payloads) or 1
    confirmed = structure.confirm_repeated_lines(repeated, total_pages)
    return {c["text"] for c in confirmed}


def _chapter_pages(page_payloads: dict[int, dict], node: StructureNode,
                   confirmed_keys: set[str]) -> list[dict]:
    """The chapter's page records ready for the chunking layer."""
    start = int(node.start_page) if node.start_page else None
    end = int(node.end_page) if node.end_page else None
    pages: list[dict] = []
    for page_number in sorted(page_payloads):
        if start is not None and page_number < start:
            continue
        if end is not None and page_number > end:
            continue
        payload = page_payloads[page_number]
        pages.append({
            "page_number": page_number,
            "mode": "ocr" if payload.get("ocr_record") is not None else "l1",
            "lines": structure.page_lines(payload),
        })
    return pages


def run_chapter_segmentation(db: Session, job: Job) -> None:
    """Segment ``job.payload['node_id']`` and plan its LLM blocks (§5.4).

    Payload keys: ``project_id`` and ``node_id`` (required); optional
    ``source_budget_max`` to override the planner's source budget (must
    still keep every block within the 16,384-token total).
    """
    project_id = job.payload["project_id"]
    node_id = job.payload["node_id"]
    source_budget_max = int(
        job.payload.get("source_budget_max") or chunking.SOURCE_BUDGET_MAX_TOKENS
    )

    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")
    node = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id,
                StructureNode.id == node_id)
        .one_or_none()
    )
    if node is None:
        raise ValueError("unknown node_id in job payload")
    if node.kind == "scene":
        raise ValueError(
            "scenes do not own segments: segment their chapter (§5.4)")

    page_payloads = _project_page_payloads(db, project_id)
    if not page_payloads:
        raise ValueError(
            "no extracted pages for this project: run the import first (§5.2)")
    first_document_id = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    confirmed_keys = _confirmed_header_keys(
        db, page_payloads,
        first_document_id.id if first_document_id else None,
    )

    _set_progress(db, job, "segmenting", 0, 3)
    pages = _chapter_pages(page_payloads, node, confirmed_keys)
    paragraphs = chunking.build_chapter_paragraphs(pages, confirmed_keys)
    units = chunking.segment_chapter_text(
        paragraphs, int(job.payload.get("sentences_per_segment") or 1))
    chunking.with_ids(units, str(project_id), str(node_id))

    _set_progress(db, job, "planning", 1, 3)
    tokenizer = chunking.get_tokenizer()
    blocks = chunking.plan_blocks(
        units, tokenizer, source_budget_max=source_budget_max)
    verification = chunking.verify_blocks(blocks)
    if not verification["all_within_budget"]:
        raise ValueError(
            "planner produced blocks over the total token budget: "
            f"{verification['oversized_blocks']}")

    _set_progress(db, job, "persisting", 2, 3)
    existing = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == node_id)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    by_ordinal = {u.ordinal: u for u in existing}

    written = kept_approved = 0
    next_n = next_numero_start(db, project_id)
    for unit in units:
        flags = {
            "segment_id": unit["segment_id"],
            "kind": unit["kind"],
            "page": unit.get("page"),
            "has_markup": unit.get("has_markup", False),
        }
        row = by_ordinal.get(unit["ordinal"])
        if row is not None and row.status == "approved":
            kept_approved += 1  # §5.1: le versioni approvate non si toccano
            continue
        if row is None:
            row = TranslationUnit(
                project_id=project_id,
                chapter_id=node_id,
                ordinal=unit["ordinal"],
                id=unit["segment_id"],
                numero=next_n,
            )
            next_n += 1
        row.source_text = unit["text"]
        row.source_hash = unit["source_hash"]
        row.source_flags = flags
        db.add(row)
        written += 1
    # segments that vanished from the new segmentation are dropped unless
    # a human approved them (§5.1)
    dropped = 0
    for ordinal, row in by_ordinal.items():
        if ordinal > len(units) and row.status != "approved":
            db.delete(row)
            dropped += 1

    if project.status == "STRUCTURE_REVIEW":
        project.status = "READY_FOR_TRANSLATION"
        db.add(project)

    db.add(AuditLog(
        project_id=project_id,
        action="chapter_segmented",
        entity="structure_node",
        entity_id=str(node_id),
        after={
            "segments": len(units),
            "segments_written": written,
            "segments_kept_approved": kept_approved,
            "segments_dropped": dropped,
            "blocks": len(blocks),
            "tokenizer": getattr(tokenizer, "name", "unknown"),
            "max_block_total_tokens": verification["max_block_total"],
            "within_budget": verification["all_within_budget"],
        },
        created_at=datetime.utcnow(),
    ))
    _set_progress(db, job, "completed", 3, 3)
    job.result = {
        **(job.result or {}),
        "chapter_id": str(node_id),
        "segments": len(units),
        "segments_written": written,
        "segments_kept_approved": kept_approved,
        "blocks": len(blocks),
        "verification": verification,
        "tokenizer": getattr(tokenizer, "name", "unknown"),
    }
    db.add(job)
    db.commit()
