"""NLP worker runner: BookNLP + NER → proposed entities (PRD §6.2).

Delegates the pure work to :mod:`backend.parsing.entities`; this module owns
the DB-facing, idempotent job:

* builds the chapter (or whole-book) text from the stored pages, with
  confirmed headers/footers already stripped (§5.3 — detection only marks,
  user confirmation deletes; chunking uses the same rule);
* runs BookNLP **in its own venv** (``.venv-booknlp``: torch/transformers
  stay out of the API image) as a subprocess over a temp copy of the text;
* stores the raw BookNLP output directory in object storage
  (``booknlp/<project_id>/<job_id>/...``) so it can be re-processed without
  re-running the models (§6.2: riprocessamento);
* runs the additional transformer NER (organisations, locations, works,
  artefacts) with a local HF pipeline when the model is available offline;
  otherwise the run continues BookNLP-only and records the skip;
* upserts :class:`~backend.models.Entity` rows in state ``proposed`` with
  aliases and per-mention evidence (§6.2.6, §15.2), never touching
  non-proposed (user-reviewed) entities.

Idempotency: proposed entities/aliases/evidence of the scope are deleted
and rebuilt; every other status survives a re-run. Re-processing from a
stored BookNLP output (``payload['reuse_output_key']``) skips the models.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime

from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    Document,
    DocumentPage,
    Entity,
    EntityAlias,
    EntityEvidence,
    ImportReport,
    Job,
    Project,
    StructureNode,
)
from .parsing import entities as ents
from .parsing import preprocess as pre
from .parsing import structure
from .storage import get_storage_provider


def _set_progress(db: Session, job: Job, phase: str, done: int,
                  total: int) -> None:
    job.result = {
        **(job.result or {}),
        "progress": {"phase": phase, "done": done, "total": total},
    }
    db.add(job)
    db.commit()


def _page_payloads(db: Session, project_id: str) -> dict[int, dict]:
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
            payloads[int(row.page_number)] = payload
    return payloads


def _confirmed_header_keys(db: Session, page_payloads: dict[int, dict],
                           first_document_id: str | None) -> set[str]:
    """Confirmed header/footer line keys (§5.3), same rule as chunking."""
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


def _scope_pages(db: Session, project_id: str, node_id: str | None,
                 confirmed_keys: set[str]) -> list[dict]:
    """Pages of the extraction scope with confirmed headers stripped.

    Each item is ``{"page_number", "text", "chapter_id"}``.
    """
    payloads = _page_payloads(db, project_id)
    if not payloads:
        raise ValueError(
            "no extracted pages for this project: run the import first (§5.2)")

    if node_id is None:
        # whole-project scope: evidence still records the chapter (§6.2.6)
        # by mapping every page onto the confirmed chapter nodes
        nodes = (
            db.query(StructureNode)
            .filter(StructureNode.project_id == project_id,
                    StructureNode.kind == "chapter")
            .order_by(StructureNode.start_page)
            .all()
        )
    else:
        node = (
            db.query(StructureNode)
            .filter(StructureNode.project_id == project_id,
                    StructureNode.id == node_id)
            .one_or_none()
        )
        if node is None:
            raise ValueError("unknown node_id in job payload")
        nodes = [node]

    def _chapter_of(page_number: int) -> str | None:
        for node in nodes:
            if int(node.start_page or 0) <= page_number <= int(node.end_page or 0):
                return str(node.id)
        return None

    pages: list[dict] = []
    for page_number in sorted(payloads):
        payload = payloads[page_number]
        lines = [
            ln for ln in structure.page_lines(payload)
            if structure.repeated_line_key(ln.get("text") or "")
            not in confirmed_keys
        ]
        text = "\n".join(ln["text"] for ln in lines).strip()
        if not text:
            continue
        pages.append({
            "page_number": page_number,
            "text": text,
            "chapter_id": _chapter_of(page_number),
        })
    if not pages:
        raise ValueError("all pages empty after confirmed-header strip")
    return pages


def _quote_context(text: str, start: int, end: int) -> str:
    """±60 chars around the mention, trimmed to whitespace boundaries."""
    lo = max(0, start - 60)
    hi = min(len(text), end + 60)
    while lo > 0 and not text[lo - 1].isspace():
        lo -= 1
    while hi < len(text) and not text[hi - 1].isspace():
        hi += 1
    return text[lo:hi].strip()


def _run_booknlp_subprocess(text: str, out_dir: str) -> str:
    """Run BookNLP on *text* via the GPU service; returns the output file id.

    Single path (ADR-008): the BookNLP FastAPI wrapper on a GPU host,
    reached through the local LLM Gateway (:mod:`.booknlp_service`) —
    "big" models on CUDA. The local CPU fallback (a venv with transformers
    5.x vs BookNLP 1.0.7) was REMOVED: it did not work and masked the
    failure. If the GPU service is unreachable the job fails explicitly.
    """
    try:
        from . import booknlp_service

        return booknlp_service.run_booknlp(text, out_dir, book_id="book")
    except Exception as exc:  # noqa: BLE001 - no fallback: fail loud
        raise RuntimeError(
            "BookNLP GPU service unreachable: entity extraction requires "
            "the service behind the gateway (ADR-008); the CPU fallback has "
            "been removed. Cause: " + str(exc)
        ) from exc


def _store_raw_output(out_dir: str, project_id: str, job_id: str) -> str:
    """Copy the raw BookNLP output into object storage; returns the key."""
    storage = get_storage_provider()
    base = f"booknlp/{project_id}/{job_id}"
    manifest = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        key = f"{base}/{name}"
        storage.put(key, data)
        manifest.append({"key": key, "bytes": len(data)})
    manifest_key = f"{base}/manifest.json"
    storage.put(
        manifest_key,
        json.dumps({"files": manifest}, indent=2).encode("utf-8"),
    )
    return manifest_key


def _hf_ner(text: str) -> list[dict]:
    """Additional transformer NER (§6.2 step 2) for org/place/work/artefact.

    Uses a local HF ``token-classification`` pipeline (aggregation over
    words). The model must already be in the local HF cache — this is a
    local-only platform, nothing is downloaded at job time (§13.1).
    Returns ``[{"text", "start", "end", "group", "score"}, ...]`` (empty when
    no offline model is available: the caller records the skip).
    """
    model = os.getenv("TRANS_NER_MODEL", "dslim/bert-base-NER")
    try:
        from transformers import pipeline

        ner = pipeline(
            "token-classification", model=model,
            aggregation_strategy="simple", device=-1,
        )
    except Exception as exc:  # noqa: BLE001 - offline / model missing → skip
        # fix 2026-09-18: skip NER ma con un segnale nei log (§13.2: nessun
        # testo del manoscritto nei log, solo il motivo del degrado).
        logging.getLogger(__name__).warning(
            "HF-NER non disponibile (%s): step 2 §6.2 saltato "
            "(modello %s non in cache locale)", exc, model,
        )
        return []
    try:
        out = ner(text[:200_000])  # transformers has no max_length guard here
    except Exception:  # noqa: BLE001 - pathological text → skip, don't fail
        return []
    return [
        {
            "text": item["word"],
            "start": int(item["start"]),
            "end": int(item["end"]),
            "group": item["entity_group"],
            "score": float(item.get("score") or 0.0),
        }
        for item in out
    ]


def run_entity_extraction(db: Session, job: Job) -> None:
    """Extract proposed entities for ``job.payload['project_id']``.

    Payload keys: ``project_id`` (required), ``node_id`` (optional —
    a chapter structure node; without it the whole project is processed),
    ``reuse_output_key`` (optional — object-storage key of a previously
    stored BookNLP manifest, for reprocessing without re-running models).
    """
    project_id = job.payload["project_id"]
    node_id = job.payload.get("node_id")
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")

    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    first_document_id = documents[0].id if documents else None
    confirmed_keys = _confirmed_header_keys(
        db, _page_payloads(db, project_id), first_document_id)
    pages = _scope_pages(db, project_id, node_id, confirmed_keys)

    _set_progress(db, job, "text", 0, 4)
    flat_text, page_map = ents.build_flat_text(pages)
    text_sha = __import__("hashlib").sha256(flat_text.encode("utf-8")).hexdigest()

    # ---- BookNLP (or reuse a stored raw output) --------------------------
    reuse_key = job.payload.get("reuse_output_key")
    out_dir = tempfile.mkdtemp(prefix="booknlp-")
    try:
        if reuse_key:
            storage = get_storage_provider()
            manifest_raw = storage.get(reuse_key)
            if manifest_raw is None:
                raise ValueError(f"stored BookNLP output missing: {reuse_key}")
            manifest = json.loads(manifest_raw.decode("utf-8"))
            for item in manifest.get("files", []):
                data = storage.get(item["key"])
                if data is not None:
                    name = item["key"].rsplit("/", 1)[-1]
                    with open(os.path.join(out_dir, name), "wb") as fh:
                        fh.write(data)
            file_id = "book"
            extractor_note = "booknlp (reused output)"
        else:
            _set_progress(db, job, "booknlp", 1, 4)
            file_id = _run_booknlp_subprocess(flat_text, out_dir)
            extractor_note = "booknlp"

        raw = ents.parse_booknlp_outputs(out_dir, file_id)

        _set_progress(db, job, "ner", 2, 4)
        hf = _hf_ner(flat_text)
        if hf:
            extractor_note += "+hf-ner"
    finally:
        if not reuse_key:
            _set_progress(db, job, "storing", 2, 4)
            _store_raw_output(out_dir, project_id, str(job.id))
        shutil.rmtree(out_dir, ignore_errors=True)

    # ---- pure-layer merge/gender/defaults --------------------------------
    clusters, _extra = ents.cluster_mentions(raw, page_map, node_id, len(pages))
    ents.attach_quote_flags(clusters, raw)
    ents.apply_gender(clusters, raw)
    ents.apply_type_defaults(clusters)
    ents.compute_confidence(clusters)
    clusters = ents.dedupe(clusters)

    _set_progress(db, job, "persisting", 3, 4)
    _persist(db, job, project_id, node_id, clusters,
             flat_text, hf, extractor_note, text_sha, reuse_key)

    db.add(AuditLog(
        action="entities_extracted",
        entity="project",
        entity_id=str(project_id),
        after={
            "job_id": str(job.id),
            "scope": str(node_id) if node_id else "project",
            "entities": len(clusters),
            "mentions": sum(c.mention_count for c in clusters.values()),
            "extractor": extractor_note,
            "hf_ner_mentions": len(hf),
            "text_sha256": text_sha,
        },
        created_at=datetime.utcnow(),
    ))
    db.commit()


def _persist(db: Session, job: Job, project_id: str, node_id: str | None,
             clusters: dict, flat_text: str, hf: list[dict],
             extractor_note: str, text_sha: str,
             reuse_key: str | None) -> None:
    """Upsert proposed entities; user-reviewed entities are never touched.

    A BookNLP cluster whose name matches a non-proposed entity (or another
    proposed entity in a different scope) is merged into the existing row's
    aliases/evidence instead of duplicating it.
    """
    scope = str(node_id) if node_id else "project"

    existing_rows = (
        db.query(Entity)
        .filter(Entity.project_id == project_id)
        .all()
    )
    by_name = {}
    for row in existing_rows:
        by_name.setdefault(row.canonical_source.strip().lower(), []).append(row)

    kept_rows: dict[str, Entity] = {}
    for coref, cluster in clusters.items():
        canon = cluster.canonical_source.strip()
        matches = by_name.get(canon.lower()) or []
        row = next((m for m in matches if m.status == "proposed"),
                   matches[0] if matches else None)

        if row is None:
            row = Entity(
                project_id=project_id,
                canonical_source=canon,
                entity_type=cluster.entity_type,
                referential_gender=cluster.referential_gender,
                referential_gender_evidence=cluster.referential_gender_evidence,
                italian_grammatical_gender=cluster.italian_grammatical_gender,
                grammatical_number=cluster.grammatical_number,
                translation_policy=cluster.translation_policy,
                status="proposed",
                confidence=cluster.confidence,
            )
            db.add(row)
            db.flush()
        else:
            # §6.4 fields: refresh only on still-proposed rows
            if row.status == "proposed":
                row.entity_type = cluster.entity_type
                row.referential_gender = cluster.referential_gender
                row.referential_gender_evidence = (
                    cluster.referential_gender_evidence)
                row.confidence = cluster.confidence
            db.add(row)

        # aliases (idempotent: unique(entity_id, source_alias))
        existing_aliases = {
            a.source_alias for a in
            db.query(EntityAlias).filter(EntityAlias.entity_id == row.id).all()
        }
        for alias in [cluster.canonical_source] + cluster.aliases:
            if alias in existing_aliases:
                continue
            db.add(EntityAlias(
                entity_id=row.id, source_alias=alias, alias_type="booknlp"))
            existing_aliases.add(alias)

        # evidence: one row per mention (§6.2.6/§15.2: quote + page always)
        for mention in cluster.mentions:
            db.add(EntityEvidence(
                entity_id=row.id,
                chapter_id=mention.chapter_id,
                page_number=mention.page,
                quote_text=_quote_context(flat_text,
                                          mention.char_start, mention.char_end),
                evidence_type=f"mention_{mention.prop.lower()}",
                confidence=cluster.gender_confidence
                if mention.prop == "PRON" else cluster.confidence,
                extractor=extractor_note,
            ))
        kept_rows[coref] = row

    # ---- transformer-only candidates (org/loc/work/artefact) -------------
    booknlp_spans = []
    for cluster in clusters.values():
        for m in cluster.mentions:
            booknlp_spans.append((m.char_start, m.char_end))

    hf_only = [
        item for item in hf
        if item["group"] in ("ORG", "LOC", "MISC")
        and not any(s <= item["start"] and item["end"] <= e
                    for s, e in booknlp_spans)
    ]
    for item in hf_only:
        name = item["text"].strip()
        if not name:
            continue
        # §5.5 hard rule: the isolated pronoun I is never an entity,
        # whatever the NER says.
        if pre.is_isolated_I(name):
            continue
        matches = by_name.get(name.lower()) or []
        row = next((m for m in matches if m.status == "proposed"),
                   matches[0] if matches else None)
        if row is None:
            row = Entity(
                project_id=project_id,
                canonical_source=name,
                entity_type=ents._HF_GROUP_TO_TYPE.get(item["group"],
                                                       "CONCEPT_TERM"),
                referential_gender="not_applicable",
                italian_grammatical_gender="not_applicable",
                grammatical_number="unknown",
                translation_policy="undecided",
                status="proposed",
                confidence=round(min(1.0, item["score"]), 3),
            )
            db.add(row)
            db.flush()
        db.add(EntityEvidence(
            entity_id=row.id,
            chapter_id=node_id,
            page_number=None,
            quote_text=_quote_context(flat_text,
                                      item["start"], item["end"]),
            evidence_type="mention_ner",
            confidence=round(min(1.0, item["score"]), 3),
            extractor="hf-ner",
        ))

    job.result = {
        **(job.result or {}),
        "entities": {
            "scope": scope,
            "entities": len(kept_rows) + len(hf_only),
            "mentions": sum(c.mention_count for c in clusters.values()),
            "hf_only": len(hf_only),
            "extractor": extractor_note,
            "text_sha256": text_sha,
            "raw_output_key": reuse_key,
        },
    }
    db.add(job)
