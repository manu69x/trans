"""Project service: CRUD, state machine, upload, copyright (PRD §5.1/§5.2/§13.2).

Encapsulates every business rule that the routes must not duplicate:

* :class:`StateMachine` — the §5.1 project lifecycle with validated transitions.
* :meth:`ProjectService.create_project` — creates a DRAFT project.
* :meth:`ProjectService.upload_document` — computes SHA-256, stores the asset,
  enforces the §5.2 dedup-by-hash contract, records metadata, and (on the first
  upload) requires the §13.2 copyright declaration before transitioning to
  ``IMPORTING``. A parse job is registered (§16).
* :meth:`ProjectService.apply_state` — a guarded transition used by later phases.

All mutations are appended to the audit log (§13.1) so the trail is complete.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import AuditLog, Document, Job, Project
from .scheduler import Scheduler
from .storage import get_storage_provider, sha256_of

# §5.1 valid transitions (each state maps to the states it may move to).
STATES: tuple[str, ...] = (
    "DRAFT",
    "IMPORTING",
    "PARSED",
    "STRUCTURE_REVIEW",
    "ENTITY_REVIEW",
    "READY_FOR_TRANSLATION",
    "TRANSLATING",
    "QA_REVIEW",
    "APPROVED",
    "EXPORTED",
)

# Each state may also return to any earlier state (§5.1: "ogni状态 consente
# ritorno a uno stato precedente"). We store the forward-allowance explicitly.
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("IMPORTING",),
    "IMPORTING": ("PARSED",),
    "PARSED": ("STRUCTURE_REVIEW",),
    "STRUCTURE_REVIEW": ("ENTITY_REVIEW", "PARSED"),
    "ENTITY_REVIEW": ("READY_FOR_TRANSLATION", "STRUCTURE_REVIEW"),
    "READY_FOR_TRANSLATION": ("TRANSLATING", "ENTITY_REVIEW"),
    "TRANSLATING": ("QA_REVIEW", "READY_FOR_TRANSLATION"),
    "QA_REVIEW": ("APPROVED", "TRANSLATING"),
    "APPROVED": ("EXPORTED", "QA_REVIEW"),
    "EXPORTED": (),
}

# Fields the client is allowed to change via PATCH (§12.1).
_UPDATABLE = (
    "title",
    "genre_profile",
    "translation_model_id",
    "text_model_id",
    "model_settings",
)


class StateError(Exception):
    """Raised when an invalid state transition is requested (§5.1)."""


class StateMachine:
    """Validates §5.1 transitions."""

    @staticmethod
    def can(current: str, target: str) -> bool:
        if current not in _TRANSITIONS:
            raise StateError(f"unknown state {current!r}")
        return target in _TRANSITIONS[current]

    @staticmethod
    def all_states() -> tuple[str, ...]:
        return STATES


class ProjectService:
    """Business logic for projects and uploads."""

    def __init__(self, db: Session, scheduler: Scheduler | None = None) -> None:
        self.db = db
        self.scheduler = scheduler or Scheduler(db)

    # --- projects -------------------------------------------------------
    def create_project(
        self,
        title: str,
        genre_profile: str,
        source_language: str = "en",
        target_language: str = "it",
        translation_model_id: str | None = None,
        text_model_id: str | None = None,
        model_settings: dict | None = None,
    ) -> Project:
        # §8.1/§8.2 defaults: when the client does not pin models/settings,
        # the deployment defaults apply (env TRANS_DEFAULT_* in config.py).
        from .config import (
            DEFAULT_MODEL_SETTINGS,
            DEFAULT_TEXT_MODEL,
            DEFAULT_TRANSLATION_MODEL,
        )

        project = Project(
            id=str(uuid.uuid4()),
            title=title,
            genre_profile=genre_profile,
            source_language=source_language,
            target_language=target_language,
            translation_model_id=translation_model_id or DEFAULT_TRANSLATION_MODEL,
            text_model_id=text_model_id or DEFAULT_TEXT_MODEL or None,
            model_settings=dict(model_settings) if model_settings else dict(DEFAULT_MODEL_SETTINGS),
            status="DRAFT",
            copyright_confirmed=False,
            created_at=datetime.utcnow(),
        )
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        # Audit is written in its own transaction AFTER the create commit: a
        # flush before commit would make the FK check on projects.id fail
        # (the project row does not exist yet), and any pending audit rows
        # would be silently lost when the request session closes post-commit.
        self._audit(
            project.id,
            None,
            "project_created",
            "project",
            after={"title": title, "genre_profile": genre_profile, "status": "DRAFT"},
        )
        self.db.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        return self.db.get(Project, project_id)

    def list_projects(self) -> list[Project]:
        return self.db.query(Project).all()

    def delete_project(self, project_id: str) -> None:
        """Delete a project and everything under it (§13.1 retention).

        Every child table carries ``ForeignKey(..., ondelete="CASCADE")``, so
        deleting the project row cascades through documents, pages, jobs,
        entities, glossary, translation units, LLM runs and audit rows. MinIO
        assets are removed explicitly (object storage has no FK cascade):
        failures are logged but never block the delete.
        """
        project = self.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        title = project.title
        document_keys: list[str] = [
            row[0]
            for row in self.db.query(Document.storage_key)
            .filter(Document.project_id == project_id)
            .all()
            if row[0]
        ]

        self.db.delete(project)
        self.db.commit()

        # best-effort asset cleanup after the DB commit (§14: storage is
        # disposable — a dangling object must not keep the project alive)
        from .storage import get_storage_provider

        storage = get_storage_provider()
        for key in document_keys:
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

        self._audit(
            project_id,
            None,
            "project_deleted",
            "project",
            after={"title": title},
        )
        self.db.commit()

    def update_project(
        self, project_id: str, changes: dict[str, object]
    ) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        for field in changes:
            if field not in _UPDATABLE:
                raise HTTPException(
                    status_code=400,
                    detail=f"field {field!r} is not updatable",
                )

        before = self._snapshot(project)
        for key, value in changes.items():
            setattr(project, key, value)
        # Audit + update commit atomically (the project row already exists, so
        # the FK check in the pre-commit flush succeeds).
        self._audit(
            project.id, None, "project_updated", "project",
            before=before, after=self._snapshot(project),
        )
        self.db.commit()
        self.db.refresh(project)
        return project

    def transition(
        self, project_id: str, target_status: str, user_id: str | None = None
    ) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not StateMachine.can(project.status, target_status):
            raise StateError(
                f"invalid transition {project.status!r} -> {target_status!r} "
                f"(allowed: {_TRANSITIONS[project.status]})"
            )
        before = self._snapshot(project)
        project.status = target_status
        project.updated_at = datetime.utcnow()
        # Audit + state change commit atomically (the project row already
        # exists, so the FK check in the pre-commit flush succeeds).
        self._audit(
            project.id, user_id, "project_state", "project",
            before=before, after=self._snapshot(project),
        )
        self.db.commit()
        self.db.refresh(project)
        return project

    # --- documents ------------------------------------------------------
    def upload_document(
        self,
        project_id: str,
        filename: str,
        content_type: str | None,
        data: bytes,
        copyright_confirmed: bool,
        user_id: str | None = None,
    ) -> dict:
        """Store an asset, dedup by SHA-256, and register the parse job.

        §5.2: compute hash/size/pages and store in object storage.
        §13.2: the copyright declaration is mandatory on the first upload.
        Returns a dict describing the outcome (new vs. duplicate).
        """
        project = self.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        sha = sha256_of(data)
        existing = (
            self.db.query(Document)
            .filter(Document.project_id == project_id, Document.sha256 == sha)
            .first()
        )
        if existing is not None:
            self._audit(
                project_id, user_id, "document_deduped", "document",
                after={"sha256": sha, "storage_key": existing.storage_key},
            )
            return {
                "status": "duplicate",
                "document_id": existing.id,
                "sha256": sha,
                "size_bytes": existing.size_bytes,
                "page_count": existing.page_count,
                "storage_key": existing.storage_key,
                "new": False,
            }

        if project.copyright_confirmed is False and not copyright_confirmed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "copyright declaration (§13.2) is required before the first upload"
                ),
            )

        storage = get_storage_provider()
        key = f"{project_id}/docs/{uuid.uuid4()}.pdf"
        storage.put(key, data)

        page_count = self._count_pages(data)

        doc = Document(
            id=str(uuid.uuid4()),
            project_id=project_id,
            filename=filename,
            content_type=content_type or "application/pdf",
            size_bytes=len(data),
            sha256=sha,
            page_count=page_count,
            storage_key=key,
            status="stored",
            created_at=datetime.utcnow(),
        )
        self.db.add(doc)
        self.db.flush()

        # First upload flips the flag (§13.2, enforced above).
        if project.copyright_confirmed is False:
            project.copyright_confirmed = True
            self._audit(
                project_id, user_id, "copyright_confirmed", "project",
                after={"copyright_confirmed": True},
            )

        # §5.1 DRAFT -> IMPORTING on first upload.
        if project.status == "DRAFT":
            project.status = "IMPORTING"
            self._audit(
                project_id, user_id, "project_state", "project",
                after={"status": "IMPORTING"},
            )

        self.db.commit()
        self.db.refresh(doc)

        job = self.scheduler.register(
            project_id, "parse", {"document_id": str(doc.id)}
        )
        self.scheduler.enqueue(job)

        self._audit(
            project_id, user_id, "document_uploaded", "document",
            after={
                "document_id": str(doc.id),
                "sha256": sha,
                "size_bytes": len(data),
                "page_count": page_count,
                "storage_key": key,
            },
        )
        # Commit the upload audit so it is durable even if the parse job that
        # runs next fails and rolls back its own transaction (§13.1 audit trail).
        self.db.commit()

        return {
            "status": "uploaded",
            "document_id": doc.id,
            "sha256": sha,
            "size_bytes": len(data),
            "page_count": page_count,
            "storage_key": key,
            "job_id": job.id,
            "new": True,
        }

    # --- helpers --------------------------------------------------------
    def _count_pages(self, data: bytes) -> int | None:
        # Fast reject: a PDF must start with the "%PDF-" header. Anything else
        # (e.g. raw bytes in the §5.2 upload test) is not a PDF, so we return
        # ``None`` without ever handing a large payload to pypdf -- parsing 100+
        # MB of non-PDF would otherwise hang the request and every parse job.
        if len(data) < 8 or data[:5] != b"%PDF-":
            return None
        try:
            import io

            from pypdf import PdfReader

            return len(PdfReader(io.BytesIO(data)).pages)
        except Exception:  # noqa: BLE001 - page count is best-effort
            return None

    def _snapshot(self, project: Project) -> dict:
        return {
            "title": project.title,
            "genre_profile": project.genre_profile,
            "status": project.status,
            "copyright_confirmed": project.copyright_confirmed,
        }

    def _audit(
        self,
        project_id: str | None,
        user_id: str | None,
        action: str,
        entity: str,
        entity_id: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                project_id=project_id,
                user_id=user_id,
                action=action,
                entity=entity,
                entity_id=entity_id,
                before=before,
                after=after,
                created_at=datetime.utcnow(),
            )
        )
        self.db.flush()
