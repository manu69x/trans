"""Structure detection runner: builds §5.3 structure_nodes for a project.

Consumes the pages the import already extracted (L1 + OCR runners) and
persists the detected structure as :class:`~backend.models.StructureNode`
rows, mirroring :mod:`backend.l1_runner` / :mod:`backend.ocr_runner`:

* **Idempotent** — re-running the job converges to the same proposal:
  proposed nodes are replaced in bulk, ``user_confirmed`` nodes are updated
  in place (same ``ordinal``) so the user's review work survives a re-detect
  (§14 idempotency), and page-derived fields are content-derived.
* **Resumable** — every step reads committed rows only; the job can be
  re-queued at any time (it is a single idempotent pass).
* **Progress** — ``job.result.progress`` is updated per phase (§14).
* **Local only** — the optional LLM verification (evidence 6) calls the
  *analysis* model through the local LLM Gateway only; when no model is
  configured the run stays fully offline and ambiguous nodes are flagged
  for review instead (PRD §5.3.6: the model never segments, it only
  verifies).

The ambiguous-node verification is *pluggable*: the runner ships without an
LLM dependency and the F1 tests exercise the offline path; wiring the
LLM Gateway analysis model is a later-phase concern (the pure layer's
``text_model`` hook is the integration point).
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
)
from .parsing import structure


def _set_progress(db: Session, job: Job, phase: str, done: int,
                  total: int) -> None:
    job.result = {
        **(job.result or {}),
        "progress": {"phase": phase, "pages_done": done,
                     "total_pages": total},
    }
    db.add(job)
    db.commit()


def _iter_page_payloads(db: Session, document_ids: list[str]) -> dict[int, dict]:
    payloads: dict[int, dict] = {}
    for document_id in document_ids:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        for row in rows:
            payload = dict(row.page_payload or {})
            payload["_page_row_id"] = str(row.id)
            payload["_char_count"] = row.char_count
            payload["_document_id"] = str(document_id)
            payloads[int(row.page_number)] = payload
    return payloads


def run_structure_detection(db: Session, job: Job) -> None:
    """Detect the structure of ``job.payload['project_id']``.

    Payload keys: ``project_id`` (required). Raises on missing project or
    when the project has no extracted pages yet (detect must run after the
    import, §5.3 after §5.2).
    """
    project_id = job.payload["project_id"]
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")

    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    page_payloads = _iter_page_payloads(db, [d.id for d in documents])
    if not page_payloads:
        raise ValueError(
            "no extracted pages for this project: run the import first (§5.2)"
        )

    toc: list[dict] = []
    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == documents[0].id)
        .one_or_none()
    ) if documents else None
    if report is not None:
        toc = list((report.summary or {}).get("toc_entries") or [])

    _set_progress(db, job, "analysing", 0, len(page_payloads))
    result = structure.build_nodes(page_payloads, toc)

    # ---- persist: replace proposals, keep confirmed nodes ----------------
    existing = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    confirmed = {n.ordinal: n for n in existing if n.status == "user_confirmed"}

    nodes = list(result["nodes"]) + list(result.get("scene_nodes") or [])
    # scenes keep their place after the chapters in reading order
    nodes.sort(key=lambda n: (n["start_page"], 0 if n["kind"] != "scene" else 1))
    for i, node in enumerate(nodes):
        node["ordinal"] = i

    by_ordinal = {n["ordinal"]: n for n in nodes}
    for ordinal, node in sorted(by_ordinal.items()):
        row = confirmed.get(ordinal)
        if row is not None:
            # keep the user's status; refresh the derived fields in place
            row.kind = node["kind"]
            row.source_label = node["source_label"]
            row.normalized_title = node["normalized_title"]
            row.start_page = node["start_page"]
            row.end_page = node["end_page"]
            row.start_char = node["start_char"]
            row.end_char = node["end_char"]
            row.confidence = node["confidence"]
            row.detection_method = node["detection_method"]
            db.add(row)
            continue
        stale = (
            db.query(StructureNode)
            .filter(StructureNode.project_id == project_id,
                    StructureNode.ordinal == ordinal)
            .one_or_none()
        )
        if stale is not None:
            db.delete(stale)
            db.flush()
        db.add(StructureNode(
            id=node["node_id"],
            project_id=project_id,
            parent_id=(result.get("scene_parents") or {}).get(node["node_id"]),
            kind=node["kind"],
            source_label=node["source_label"],
            normalized_title=node["normalized_title"],
            start_page=node["start_page"],
            end_page=node["end_page"],
            start_char=node["start_char"],
            end_char=node["end_char"],
            confidence=node["confidence"],
            detection_method=node["detection_method"],
            status="proposed",
            ordinal=ordinal,
        ))
    # proposals that vanished from the new detection are dropped
    for row in existing:
        if row.status != "user_confirmed" and \
                row.ordinal not in by_ordinal:
            db.delete(row)

    ambiguous = [
        {"node_id": a["node_id"], "label": a["label"], "page": a["page"]}
        for a in result.get("ambiguous") or []
    ]
    if project.status == "PARSED":
        project.status = "STRUCTURE_REVIEW"
    db.add(project)

    # Profilo tipografico per l'export "Con clone struttura" (2026-10-01):
    # font, allineamenti, immagini, pagine originali. Best-effort: un
    # fallimento NON deve invalidare la struttura rilevata.
    typography_summary: dict = {}
    try:
        from . import typography as _typo

        profile = _typo.extract_typography(db, str(project_id))
        typography_summary = {
            "original_page_count": profile.get("original_page_count"),
            "body_font": profile.get("body_font"),
            "body_size": profile.get("body_size"),
            "image_count": profile.get("image_count"),
            "fonts": [f["font"] for f in (profile.get("fonts") or [])[:3]],
        }
    except Exception as exc:  # noqa: BLE001
        typography_summary = {"error": str(exc)[:200]}

    _set_progress(db, job, "completed", len(page_payloads),
                  len(page_payloads))
    job.result = {
        **(job.result or {}),
        "typography": typography_summary,
        "structure": {
            "project_id": str(project_id),
            "nodes_proposed": len(nodes),
            "confirmed_kept": len(confirmed),
            "ambiguous": ambiguous,
            "toc_pages": result.get("toc_pages") or [],
            "pdf_toc_verified": result.get("pdf_toc_verified") or [],
        },
    }
    db.add(job)
    db.add(AuditLog(
        action="structure_detected",
        entity="project",
        entity_id=str(project_id),
        after={
            "nodes": len(nodes),
            "ambiguous": len(ambiguous),
            "methods": sorted({
                m for n in nodes for m in n["detection_method"]}),
        },
        created_at=datetime.utcnow(),
    ))
    db.commit()
