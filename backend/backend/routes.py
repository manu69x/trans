"""Public API routes for the Trans backend (V1).

Implements the §12.1 project/upload surface:

* ``GET``/``POST /api/projects`` -- list / create projects.
* ``GET``/``PATCH /api/projects/{id}`` -- read / update a project.
* ``POST /api/projects/{id}/documents`` -- upload a PDF to object storage,
  with §5.2 hash/size/page metadata, §5.2 dedup-by-hash and §13.2 copyright
  enforcement.
* ``GET /api/projects/{id}/jobs`` -- list jobs (§16).

All logic lives in :class:`~backend.service.ProjectService`; the routes are a
thin HTTP layer on top of it.
"""
from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from datetime import datetime
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Any
from sqlalchemy.orm import Session

from .db import get_db_session
from .rate_limit import RateLimitExceeded, limiter as rate_limiter
from .rbac import require_permission
from .scheduler import InProcessScheduler, Scheduler, get_scheduler_from
from .service import ProjectService, StateMachine
from .storage import get_storage_provider

router = APIRouter(prefix="/projects", tags=["projects"])


def _enforce_rate(bucket: str, request: Any = None) -> None:
    """Token-bucket guard (§13/§14). Raises 429 when the budget is spent.

    Identity is the client IP as seen by the API (X-Forwarded-For aware for
    the TLS-terminating proxy in the prod compose profile).
    """
    from fastapi import Request

    req = request
    ip = "unknown"
    if req is not None:
        xff = req.headers.get("x-forwarded-for")
        ip = (xff.split(",")[0].strip() if xff else None) or (
            req.client.host if req.client else "unknown")
    allowed, retry_after = rate_limiter.check(bucket, ip)
    if not allowed:
        raise RateLimitExceeded(retry_after)


def _rate_limit_exception_handler(app: Any) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(RateLimitExceeded)  # type: ignore[arg-type]
    async def _handler(request: Any, exc: RateLimitExceeded) -> Any:  # pragma: no cover
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )


# --- schemas --------------------------------------------------------------
class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1)
    genre_profile: str = Field(..., min_length=1)
    source_language: str = "en"
    target_language: str = "it"
    translation_model_id: str | None = None
    text_model_id: str | None = None
    model_settings: dict | None = None


class ProjectUpdate(BaseModel):
    title: str | None = None
    genre_profile: str | None = None
    translation_model_id: str | None = None
    text_model_id: str | None = None
    status: str | None = None
    model_settings: dict | None = None


class ProjectOut(BaseModel):
    id: Any
    title: str
    source_language: str
    target_language: str
    genre_profile: str
    status: str
    copyright_confirmed: bool
    created_at: datetime
    updated_at: datetime
    translation_model_id: str | None = None
    text_model_id: str | None = None
    model_settings: dict | None = None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: Any
    job_type: str
    status: str
    payload: dict | None
    result: dict | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


# --- projects -------------------------------------------------------------
@router.get("", response_model=list[ProjectOut])
async def list_projects(db: Session = Depends(get_db_session)) -> list[ProjectOut]:
    svc = ProjectService(db)
    return [ProjectOut.model_validate(p) for p in svc.list_projects()]


@router.post("", response_model=ProjectOut, status_code=201,
             dependencies=[Depends(require_permission("manage_projects"))])
async def create_project(
    payload: ProjectCreate, db: Session = Depends(get_db_session)
) -> ProjectOut:
    svc = ProjectService(db)
    project = svc.create_project(**payload.model_dump())
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: Session = Depends(get_db_session)) -> ProjectOut:
    svc = ProjectService(db)
    project = svc.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut,
              dependencies=[Depends(require_permission("manage_projects"))])
async def update_project(
    project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db_session)
) -> ProjectOut:
    svc = ProjectService(db)
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    project = svc.update_project(project_id, changes)
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=204,
               dependencies=[Depends(require_permission("delete"))])
async def delete_project(
    project_id: str,
    request: Any = None,
    db: Session = Depends(get_db_session),
) -> Response:
    """Delete the project and every child row (cascading) + MinIO assets."""
    _enforce_rate("project_delete", request)
    svc = ProjectService(db)
    svc.delete_project(project_id)
    return Response(status_code=204)


# --- documents ------------------------------------------------------------
@router.post("/{project_id}/documents", status_code=201,
             dependencies=[Depends(require_permission("manage_structure"))])
async def upload_document(
    project_id: str,
    request: Any = None,
    file: UploadFile = File(...),
    copyright_confirmed: bool = Form(...),
    db: Session = Depends(get_db_session),
) -> dict:
    _enforce_rate("upload", request)
    svc = ProjectService(db, scheduler=get_scheduler_from(db))
    data = await file.read()
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")

    result = svc.upload_document(
        project_id,
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        copyright_confirmed=copyright_confirmed,
    )
    return result


# --- L1 extraction (PRD 5.2 / ADR-002 L1) ---------------------------------
@router.post("/{project_id}/documents/{document_id}/parse_l1",
             response_model=JobOut, status_code=202,
             dependencies=[Depends(require_permission("manage_structure"))])
async def parse_document_l1(
    project_id: str,
    document_id: str,
    db: Session = Depends(get_db_session),
) -> JobOut:
    """Queue (or re-queue) the idempotent L1 extraction for a document.

    The job is resumable: re-triggering a finished or interrupted extraction
    converges to the same pages (UPSERT by page number) without duplicates.
    """
    from .models import Document

    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    svc = ProjectService(db, scheduler=get_scheduler_from(db))
    job = svc.scheduler.register(
        project_id, "parse_l1", {"document_id": str(doc.id)}
    )
    svc.scheduler.enqueue(job)
    return JobOut.model_validate(job)


@router.get("/{project_id}/documents")
async def list_documents(
    project_id: str, db: Session = Depends(get_db_session)
) -> list[dict]:
    """The project's uploaded documents (newest first), for the viewer UI.

    Backs the §11.1 Import-and-structure page: the user picks the document
    whose PDF/extracted-text pair the viewer shows.
    """
    from .models import Document

    svc = ProjectService(db)
    if svc.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    rows = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "content_type": d.content_type,
            "size_bytes": d.size_bytes,
            "sha256": d.sha256,
            "page_count": d.page_count,
            "status": d.status,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


@router.get(
    "/{project_id}/documents/{document_id}/file")
async def get_document_file(
    project_id: str, document_id: str, db: Session = Depends(get_db_session),
    exp: str | None = None, token: str | None = None,
    request: Any = None,
) -> Response:
    """Stream the stored PDF of a document (§11.1 viewer pane).

    §13.1 URL download firmati e temporanei: when the caller presents
    ``?exp=...&token=...`` the signature is verified (resource-bound,
    time-limited) *before* any byte leaves the store; an invalid or expired
    token answers 403. Without a token the route keeps working for
    authenticated in-app sessions (the viewer pane) -- the signed form is
    what export/download links use.

    The bytes never leave the local system (§13.1): the file is served from
    the local object store through the Next origin rewrite, exactly like
    every other API payload.
    """
    from . import signed_urls
    from .models import Document

    if token or exp:
        resource = f"projects/{project_id}/documents/{document_id}/file"
        if not signed_urls.verify_url(resource, token or "", exp or ""):
            raise HTTPException(status_code=403, detail="invalid or expired signature")
    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    data = get_storage_provider().get(doc.storage_key)
    if data is None:
        raise HTTPException(status_code=404, detail="stored file missing")
    return Response(
        content=data,
        media_type=doc.content_type or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc.filename}"'},
    )


@router.get("/{project_id}/documents/{document_id}/import-report")
async def get_import_report(
    project_id: str,
    document_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Return the 5.2.6 import report of a document (with job progress)."""
    from .models import Document, ImportReport, Job

    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == document_id)
        .one_or_none()
    )
    latest_job = (
        db.query(Job)
        .filter(Job.project_id == project_id,
                Job.job_type == "parse_l1",
                Job.payload["document_id"].astext == document_id)
        .order_by(Job.created_at.desc())
        .first()
    )
    return {
        "document_id": str(document_id),
        "document_status": doc.status,
        "report": report.summary if report is not None else None,
        "pages_ok": report.pages_ok if report is not None else 0,
        "pages_ocr_needed": (
            report.pages_ocr_needed if report is not None else 0),
        "report_updated_at": (
            report.updated_at.isoformat() if report is not None else None),
        "job_progress": (
            (latest_job.result or {}).get("progress")
            if latest_job is not None else None),
    }


# --- OCR (PRD 5.2 step 4, ADR-002 L3/L4, dedicated queue 14) ---------------
@router.post("/{project_id}/documents/{document_id}/ocr",
             response_model=JobOut, status_code=202,
             dependencies=[Depends(require_permission("manage_structure"))])
async def ocr_document(
    project_id: str,
    document_id: str,
    page_numbers: list[int] | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> JobOut:
    """Queue the OCR of a document's pages (or a selective re-OCR).

    Without ``page_numbers`` every page is (re-)OCR-ed; with the list only
    those pages are processed (§5.2: re-OCR selettivo per pagina). The job
    is idempotent/resumable and runs on the dedicated ``ocr`` queue (§14).
    """
    from .models import Document

    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    payload: dict = {"document_id": str(doc.id)}
    if page_numbers:
        payload["page_numbers"] = sorted({int(p) for p in page_numbers})
        if payload["page_numbers"][0] < 1:
            raise HTTPException(status_code=400,
                                detail="page_numbers are 1-based")
    svc = ProjectService(db, scheduler=get_scheduler_from(db))
    job = svc.scheduler.register(project_id, "ocr_document", payload)
    svc.scheduler.enqueue(job)
    return JobOut.model_validate(job)


@router.get("/{project_id}/documents/{document_id}/ocr-report")
async def get_ocr_report(
    project_id: str,
    document_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """Per-page OCR status: level, confidence and the suspect flag (§5.2)."""
    from .models import Document, DocumentPage, ImportReport

    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")

    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
        .all()
    )
    report = (
        db.query(ImportReport)
        .filter(ImportReport.document_id == document_id)
        .one_or_none()
    )
    return {
        "document_id": str(document_id),
        "document_status": doc.status,
        "pages": [
            {
                "page_number": p.page_number,
                "ocr_done": (p.page_payload or {}).get("ocr_record")
                is not None,
                "level": p.ocr_level,
                "confidence": float(p.confidence) if p.confidence is not None
                else None,
                "mean_line_conf": (float(p.ocr_mean_line_conf)
                                   if p.ocr_mean_line_conf is not None
                                   else None),
                "ocr_suspect": bool(p.ocr_suspect),
                "char_count": p.char_count,
            }
            for p in pages
        ],
        "report_ocr": (report.summary or {}).get("ocr")
        if report is not None else None,
    }


@router.get("/{project_id}/documents/{document_id}/pages/{page_number}")
async def get_document_page(
    project_id: str,
    document_id: str,
    page_number: int,
    db: Session = Depends(get_db_session),
) -> dict:
    """One page's structured record: OCR lines with bbox + confidence."""
    from .models import Document, DocumentPage

    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    page = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document_id,
                DocumentPage.page_number == page_number)
        .one_or_none()
    )
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    payload = page.page_payload or {}
    ocr_record = payload.get("ocr_record") or {}
    return {
        "document_id": str(document_id),
        "page_number": page.page_number,
        "level": page.ocr_level,
        "ocr_suspect": bool(page.ocr_suspect),
        "confidence": float(page.confidence) if page.confidence is not None
        else None,
        "normalized_text": page.normalized_text,
        "lines": ocr_record.get("lines", []),
        "attempts": ocr_record.get("attempts", []),
        "text_sha256": page.text_sha256,
        "page_sha256": page.page_sha256,
    }


# --- jobs -----------------------------------------------------------------
@router.get("/{project_id}/jobs", response_model=list[JobOut])
async def list_jobs(project_id: str, db: Session = Depends(get_db_session)) -> list[JobOut]:
    from .models import Job

    rows = (
        db.query(Job)
        .filter(Job.project_id == project_id)
        .order_by(Job.created_at.desc())
        .all()
    )
    return [JobOut.model_validate(j) for j in rows]


# --- state machine (for later phases; exposed here as a convenience) ------
@router.get("/states", tags=["meta"])
async def list_states() -> dict:
    return {"states": list(StateMachine.all_states())}


# --- signed download URLs (§13.1) ------------------------------------------
class SignedUrlRequest(BaseModel):
    document_id: str | None = None
    ttl_seconds: int = Field(default=300, ge=1, le=3600)


@router.post("/{project_id}/documents/{document_id}/signed_url")
async def create_signed_url(
    project_id: str, document_id: str | None = None,
    payload: SignedUrlRequest | None = None,
    db: Session = Depends(get_db_session),
) -> dict:
    """Issue a temporary, resource-bound download URL (§13.1).

    The token is an HMAC over ``resource:exp`` keyed outside the repo; the
    ``/file`` route verifies it before serving bytes.
    """
    from . import signed_urls
    from .models import Document

    document_id = document_id or (payload.document_id if payload else None)
    if not document_id:
        raise HTTPException(status_code=400, detail="missing document_id")
    doc = (
        db.query(Document)
        .filter(Document.id == document_id,
                Document.project_id == project_id)
        .one_or_none()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    ttl = payload.ttl_seconds if payload else 300
    resource = f"projects/{project_id}/documents/{document_id}/file"
    signed = signed_urls.sign_url(resource, ttl_seconds=ttl)
    return {
        "resource": resource,
        "exp": signed["exp"],
        "token": signed["token"],
        "url": f"/api/v1/projects/{project_id}/documents/{document_id}/file?{signed['url_path']}",
        "expires_in": ttl,
    }
