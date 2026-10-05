"""Job scheduler (PRD §16, §5.2).

Long-running work (OCR/NLP/parsing) runs on a separate worker. Two backends
are provided behind :class:`Scheduler`:

* :class:`CeleryScheduler` — real async queue over Redis (production).
* :class:`InProcessScheduler` — runs each job in a background thread with its
  own DB session (tests / dev). Behaviour is identical; only the delivery
  differs. Running in a background thread mirrors the production model: the
  upload request returns while the parse is still running, so the project stays
  in ``IMPORTING`` (it only advances to ``PARSED`` once the job completes),
  exactly as §5.1/§5.2/§14 intend.

Registering a job always persists it to the ``jobs`` table so ``GET
/api/projects/{id}/jobs`` (§12.1) reflects queued work even before a worker
has picked it up. The parse handler reads the stored PDF, counts pages with
pypdf and marks the document ``parsed``; failures are recorded on the job row.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from .db import SessionLocal  # noqa: E402  (avoid circular import)
from .models import AuditLog, Document, Job, Project
from .storage import get_storage_provider


# Handlers keyed by job_type. Installed at import time by _register_handlers().
_HANDLERS: dict[str, Callable[[Session, Job], None]] = {}

# All InProcessScheduler worker threads currently running. The test suite joins
# these (see ``wait_for_workers``) so a background parse job never holds a DB
# connection open when the next test's teardown drops tables -- which would
# otherwise block on the lock that job still holds.
_WORKERS: set[threading.Thread] = set()
_WORKERS_LOCK = threading.Lock()


def wait_for_workers(timeout: float = 60.0) -> None:
    """Join every in-flight InProcessScheduler worker thread.

    Used by the test suite so a background parse job can't still hold a DB
    connection when the next test's teardown drops tables (which would block on
    the lock that job holds). Returns once no worker is alive or *timeout*
    seconds have elapsed.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while True:
        with _WORKERS_LOCK:
            pending = [t for t in _WORKERS if t.is_alive()]
        if not pending:
            return
        for t in pending:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return
            t.join(remaining)


class Scheduler:
    """Registers and runs jobs. Subclasses change only :meth:`enqueue`.

    A scheduler is created per request (it only needs the DB session) so each
    request closes its own transaction.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # --- public API -----------------------------------------------------
    def register(
        self,
        project_id: str,
        job_type: str,
        payload: dict | None = None,
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            project_id=project_id,
            job_type=job_type,
            status="queued",
            payload=payload,
            created_at=datetime.utcnow(),
        )
        self.db.add(job)
        self.db.commit()
        return job

    def run(self, job: Job, db: Session) -> None:
        """Execute the handler registered for *job.job_type*.

        *db* is the worker thread's own session (see
        :meth:`InProcessScheduler.enqueue`); it must never be a request/thread
        session shared with another thread.

        On success the job row is marked ``completed`` with ``completed_at``
        here -- the same final state the Celery task wrapper writes -- so the
        in-process path and the queue path converge on identical job state
        (§14: the API reads progress from the ``jobs`` table in both modes).
        Handlers own intermediate state only.
        """
        handler = _HANDLERS.get(job.job_type)
        if handler is None:
            job.status = "failed"
            job.error = f"no handler for job_type {job.job_type!r}"
            job.completed_at = datetime.utcnow()
            db.commit()
            return
        try:
            job.status = "running"
            job.started_at = datetime.utcnow()
            db.flush()
            handler(db, job)
            if job.status != "failed":
                job.status = "completed"
            job.completed_at = datetime.utcnow()
            db.add(job)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - record on the job row
            db.rollback()
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = datetime.utcnow()
            db.add(
                AuditLog(
                    action="job_failed",
                    entity="job",
                    entity_id=str(job.id),
                    after={"error": str(exc)},
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()

    def enqueue(self, job: Job) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


# Max concurrency for heavy jobs (§14). Without a cap, bulk-translating a
# whole book would launch one LLM thread per chapter all at once, saturating
# the gateway, host RAM and Postgres. Jobs beyond the limit stay "queued"
# until a slot frees up.
_JOB_CONCURRENCY: dict[str, int] = {
    # translation is strictly sequential (one job at a time, no parallelism)
    "translate": int(os.getenv("TRANS_MAX_CONCURRENT_TRANSLATE", "1")),
    "ocr_document": 2,
    "ocr_pages": 2,
    "extract_entities": 1,  # BookNLP è il job più pesante
    "llm_classify_entities": 2,
    "detect_structure": 2,
    "segment_chapter": 4,
    "resegment_structure": 4,
}
_JOB_SEMAPHORES = {
    k: threading.Semaphore(v) for k, v in _JOB_CONCURRENCY.items()
}


class InProcessScheduler(Scheduler):
    """Runs each job in a background thread with its own DB session.

    The upload request returns immediately (the project stays in ``IMPORTING``)
    while the parse runs asynchronously, mirroring the production Celery model
    and satisfying §14 ("OCR asincrono"). Each thread opens its own SQLAlchemy
    session so it never shares a transaction with the request that enqueued it.
    Heavy job types are throttled by a per-type semaphore (see
    ``_JOB_CONCURRENCY``): excess jobs stay ``queued`` until a slot frees.
    """

    def enqueue(self, job: Job) -> None:
        # Pass only the job id to the worker thread. The ``job`` object itself
        # belongs to the request's session; with ``NullPool`` that session's
        # connection closes on commit, leaving ``job``'s attributes expired so
        # accessing them (e.g. ``job.id`` from another thread) reloads on the
        # closed request session and raises. Loading the Job fresh in the
        # worker's own session avoids all cross-thread/session state.
        job_id = job.id
        # il semaforo si acquisisce PRIMA di aprire la sessione: i thread in
        # attesa non devono trattenere connessioni PostgreSQL (fix 2026-09-20:
        # 85 job in coda tenevano 85 connessioni e esaurivano il limite,
        # bloccando tutta la coda).
        sem = _JOB_SEMAPHORES.get(job.job_type)

        def _run(job_id: str, sem) -> None:
            # Track the WORKER thread (not the caller): join()ing the caller
            # from wait_for_workers would raise "cannot join current thread".
            worker = threading.current_thread()
            with _WORKERS_LOCK:
                _WORKERS.add(worker)
            if sem is not None:
                sem.acquire()
            logging.getLogger(__name__).warning(
                "job %s: slot acquisito, avvio sessione", job_id)
            # Each worker thread gets its OWN engine/session (all bound to the
            # same database). SQLAlchemy 2.x has strict thread affinity: a
            # Session created on one thread and used on another trips
            # "concurrent operations are not permitted". Each thread therefore
            # opens its own session so it never shares a transaction with the
            # request that enqueued it (mirrors the production Celery model).
            db = SessionLocal()
            try:
                # Re-load the job in this thread's session so the handler can
                # mutate it, and re-check the status after the semaphore wait:
                # annullato dall'utente nel frattempo -> non eseguire.
                loaded = db.get(Job, job_id)
                logging.getLogger(__name__).warning(
                    "job %s: caricato (status=%s)",
                    job_id, loaded.status if loaded else "inesistente")
                if loaded is not None:
                    db.refresh(loaded)
                    if loaded.status == "queued":
                        logging.getLogger(__name__).warning(
                            "job %s: esecuzione", job_id)
                        self.__class__(db).run(loaded, db)
            finally:
                db.close()
                if sem is not None:
                    sem.release()
                with _WORKERS_LOCK:
                    _WORKERS.discard(worker)

        threading.Thread(
            target=_run,
            args=(job_id, sem),
            daemon=True,
        ).start()


class CeleryScheduler(Scheduler):
    """Async scheduler backed by Celery + Redis.

    NON operativo (ADR-007): il dispatch richiederebbe task Celery per tutti
    i 12 job_type registrati e un'immagine worker con il package ``backend`` e
    le sue dipendenze; oggi nessuna delle due cose esiste e il vecchio import
    ``backend.tasks`` puntava a un modulo inesistente. Con ``USE_CELERY=1``
    l'errore è esplicito e immediato invece di scoppiare alla prima enqueue.
    """

    def enqueue(self, job: Job) -> None:
        raise RuntimeError(
            "USE_CELERY=1 ma il dispatch Celery non è implementato: il worker "
            "non copia tutti i job_type e l'immagine non contiene il package "
            "backend (vedi ADR-007). Usa lo scheduler in-process (default)."
        )


def recover_stale_jobs() -> int:
    """Recover jobs orphaned by a restart (§16 operational hygiene).

    The in-process worker threads die with the uvicorn process: a job left in
    ``running`` (thread killed mid-flight) or ``queued`` (thread never spawned)
    at boot has no worker anymore and would stay stuck forever. They are
    marked ``failed`` with an explicit reason and a single audit row records
    the recovery. Returns the number of recovered rows.
    """
    recovered = 0
    with SessionLocal() as db:
        stale = (
            db.query(Job)
            .filter(Job.status.in_(["queued", "running"]))
            .all()
        )
        for job in stale:
            job.status = "failed"
            job.error = "interrotto dal riavvio del servizio (recupero al boot)"
            job.completed_at = datetime.utcnow()
            db.add(
                AuditLog(
                    action="job_recovered",
                    entity="job",
                    entity_id=str(job.id),
                    after={"job_type": job.job_type,
                           "previous_status": "running"},
                    created_at=datetime.utcnow(),
                )
            )
            recovered += 1
        if recovered:
            db.commit()
    return recovered


def _parse_handler(db: Session, job: Job) -> None:
    """Parse the stored PDF, count pages and advance the project state.

    §5.2: after a successful parse the document is ``parsed`` and, if the
    project was still in ``IMPORTING``, it transitions to ``PARSED`` (§5.1).

    The DB session is closed *before* the (potentially slow) pypdf read so the
    worker never holds a Postgres transaction open while another thread runs
    DDL -- e.g. the test suite's ``_reset`` fixture calling ``drop_all`` would
    otherwise block on this backend's open transaction and hang. Page counting
    happens on the in-memory bytes, outside any transaction; results is written
    back on a fresh session afterwards.
    """
    job_id = job.id
    document_id = job.payload["document_id"]

    # --- Phase 1: load what we need, then release the connection ----------
    doc = db.get(Document, document_id)
    if doc is None:
        raise ValueError("unknown document_id in job payload")
    project_id = doc.project_id

    data = get_storage_provider().get(doc.storage_key)
    if data is None:
        raise ValueError(f"stored asset missing at {doc.storage_key!r}")

    db.close()  # free the Postgres connection before the slow parse.

    # --- Phase 2: count pages WITHOUT holding a DB transaction ------------
    # Fast reject: a PDF must start with the "%PDF-" header. Anything else
    # (e.g. raw bytes in the §5.2 upload test) is not a PDF, so we fail fast
    # instead of letting pypdf scan a large non-PDF payload (which would hang
    # the parse job and every wait_for_workers() that waits on it).
    if len(data) < 8 or data[:5] != b"%PDF-":
        raise ValueError("not a PDF: missing %PDF- header")
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - non-PDF / unreadable
        raise ValueError(f"could not read PDF: {exc}")

    # --- Phase 3: persist results on a fresh session ----------------------
    with SessionLocal() as db2:
        stored_job = db2.get(Job, job_id)
        doc = db2.get(Document, document_id)
        if doc is not None:
            doc.status = "parsed"
            doc.page_count = pages
        if stored_job is not None:
            stored_job.status = "completed"
            stored_job.result = {"page_count": pages}
            stored_job.completed_at = datetime.utcnow()
            db2.flush()
        project = db2.get(Project, project_id)
        if project is not None and project.status == "IMPORTING":
            project.status = "PARSED"
            project.updated_at = datetime.utcnow()
        db2.add(
            AuditLog(
                action="document_parsed",
                entity="document",
                entity_id=str(document_id),
                after={"page_count": pages},
                created_at=datetime.utcnow(),
            )
        )
        db2.commit()


def _parse_l1_handler(db: Session, job: Job) -> None:
    """L1 layout-aware extraction (PRD 5.2, ADR-002 L1).

    Delegates to :mod:`backend.l1_runner`, which is idempotent and resumable:
    pages are UPSERTed per ``(document_id, page_number)`` and the job's
    ``result.progress`` tracks per-page feedback (14). The heavy work runs
    with the request-style session closed, then results are committed inside
    the runner on the same worker session (like ``_parse_handler``).
    """
    from .l1_runner import run_l1_extraction

    run_l1_extraction(db, job)


def _ocr_document_handler(db: Session, job: Job) -> None:
    """OCR of scanned pages (PRD 5.2 step 4, ADR-002 L3/L4, queue 14).

    Delegates to :mod:`backend.ocr_runner` (same idempotent/resumable design
    as the L1 runner). The job payload carries ``document_id`` and, for a
    selective re-OCR, ``page_numbers`` (§5.2: re-OCR per pagina).
    """
    from .ocr_runner import run_ocr_extraction

    run_ocr_extraction(db, job)


def _detect_structure_handler(db: Session, job: Job) -> None:
    """Multi-signal structure detection (PRD 5.3, task t_6db59f2b).

    Delegates to :mod:`backend.structure_runner`: replaces the ``proposed``
    structure nodes, keeps ``user_confirmed`` ones, flags ambiguous nodes
    for the analysis model (5.3.6) and moves PARSED projects to
    STRUCTURE_REVIEW (5.1).
    """
    from .structure_runner import run_structure_detection

    run_structure_detection(db, job)


def _segment_chapter_handler(db: Session, job: Job) -> None:
    """CAT segmentation of one chapter (PRD 5.4, task t_b4986b66).

    Delegates to :mod:`backend.chunking_runner`: two-pass segmentation of
    the chapter's pages, stable segment IDs + source hashes persisted into
    ``translation_units``, LLM blocks planned within the 16,384-token total
    budget.  Never calls an LLM.
    """
    from .chunking_runner import run_chapter_segmentation

    run_chapter_segmentation(db, job)


def _resegment_structure_handler(db: Session, job: Job) -> None:
    """Selective re-segmentation after a structure edit (PRD 15.1, T15).

    Delegates to :mod:`backend.structure_editor`: only the chapters whose
    derived page range the edit actually changed are re-segmented (or the
    single ``node_id`` given in the payload); approved translations stay
    immutable (5.1).
    """
    from .structure_editor import run_resegment

    run_resegment(db, job)


def _extract_entities_handler(db: Session, job: Job) -> None:
    """Entity extraction via BookNLP + NER (PRD §6.2, task t_cfa55b05).

    Delegates to :mod:`backend.nlp_runner`: BookNLP runs in its own venv
    (subprocess, local small models), the raw output is stored in object
    storage for reprocessing, and proposed entities land with mentions,
    evidence, §6.4 fields. Idempotent: proposed rows are rebuilt,
    user-reviewed rows are only enriched.
    """
    from .nlp_runner import run_entity_extraction

    run_entity_extraction(db, job)


def _llm_classify_entities_handler(db: Session, job: Job) -> None:
    """LLM-structured classification of ambiguous candidates (§6.2.3).

    Delegates to :mod:`backend.llm_ner_runner`: §6.2.3 candidates only
    (domain categories / low deterministic confidence), one schema-
    constrained Gateway call per block (§8.2, §9.5), idempotency per
    block hash (§13.2), field-level merge without overwriting BookNLP
    proposals, evidence rows with provenance ``extractor='llm:<model>'``.
    """
    from .llm_ner_runner import run_llm_entity_classification

    run_llm_entity_classification(db, job)


def _translate_handler(db: Session, job: Job) -> None:
    """Translation batch: one constrained Gateway call, deterministic
    validation (§10.1.5-9) and idempotent ``machine_draft`` save (§10.1 /
    §8.4 / §15.3). Delegates to :mod:`backend.translation.runner`.
    """
    from .translation.runner import run_translation_batch

    run_translation_batch(db, job)


def _verify_translations_handler(db: Session, job: Job) -> None:
    """Verifica QE massiva (§10.2-bis, modello Open-QE su GPU0).

    Delegates to :mod:`backend.verify_handler`: due domande chiuse per
    segmento tradotto (lingua + coppia EN->IT) con le probabilita'
    memorizzate sui campi ``is_italian`` / ``is_translated``.
    """
    from .verify_handler import run_verify_translations

    run_verify_translations(db, job)


def _tm_maintenance_handler(db: Session, job: Job) -> None:
    """TM maintenance report (PRD §7.2, F3 / task t_694dfe91).

    Flags duplicates, contradictory targets, misaligned tags/placeholders,
    length discrepancies and obsolete entries, and the TM reuse rate
    (§15.4 / dashboard). Runs on the worker thread with its own session.
    """
    from .tm_maintenance import report_tm

    project_id = job.payload["project_id"]
    report = report_tm(db, project_id)
    stored = db.get(Job, job.id)
    if stored is not None:
        stored.status = "completed"
        stored.result = report
        stored.completed_at = datetime.utcnow()
        db.flush()
    db.add(
        AuditLog(
            action="tm_maintenance",
            entity="tm_report",
            after={
                "entry_count": report["entry_count"],
                "contradictions": len(report["contradictions"]),
                "duplicates": len(report["duplicates"]),
                "reuse_rate": report["reuse_rate"],
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()


def _register_handlers() -> None:
    _HANDLERS["parse"] = _parse_handler
    _HANDLERS["parse_l1"] = _parse_l1_handler
    _HANDLERS["ocr_document"] = _ocr_document_handler
    _HANDLERS["ocr_pages"] = _ocr_document_handler
    _HANDLERS["detect_structure"] = _detect_structure_handler
    _HANDLERS["segment_chapter"] = _segment_chapter_handler
    _HANDLERS["resegment_structure"] = _resegment_structure_handler
    _HANDLERS["extract_entities"] = _extract_entities_handler
    _HANDLERS["llm_classify_entities"] = _llm_classify_entities_handler
    _HANDLERS["translate"] = _translate_handler
    _HANDLERS["verify_translations"] = _verify_translations_handler
    _HANDLERS["tm_maintenance"] = _tm_maintenance_handler
    _HANDLERS["qa"] = _qa_handler


def _qa_handler(db: Session, job: Job) -> None:
    """Full QA pass (§10.2 / §10.3 / §12.4 / AC1-3) as a background job.

    Delegates to :func:`backend.qa.runner.run_qa_for_project`; the job row is
    marked completed with the summary on success (§16).
    """
    from .qa.runner import run_qa_for_project

    project_id = job.payload["project_id"]
    summary = run_qa_for_project(
        db, project_id,
        critic_backend=job.payload.get("critic_backend", "deterministic"),
        unit_status=tuple(job.payload.get("unit_status",
                                           ["machine_draft", "untranslated"])))
    stored = db.get(Job, job.id)
    if stored is not None:
        stored.status = "completed"
        stored.result = summary
        stored.completed_at = datetime.utcnow()
        db.flush()
    db.add(stored)
    db.commit()


_register_handlers()


def get_scheduler_from(db: Session) -> Scheduler:
    """Return the configured scheduler for *db (Celery in prod, inline tests).
    """
    if os.getenv("USE_CELERY", "0") == "1":
        return CeleryScheduler(db)
    return InProcessScheduler(db)
