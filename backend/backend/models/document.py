"""Document / asset records for imported files (PRD §5.2, §6.5).

A :class:`Document` is an immutable record of one uploaded asset: its SHA-256
hash, size, page count and where it lives in object storage. The unique
constraint on ``(project_id, sha256)`` implements the §5.2 dedup-by-hash rule:
uploading an identical file does not create a second record.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Document(Base):
    """An uploaded asset (PDF or other), with provenance and storage path.

    Mirrors the §5.2 import contract: every document records ``sha256``,
    ``size_bytes`` and ``page_count``; the unique ``(project_id, sha256``
    constraint enforces dedup.
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="stored")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="ux_document_project_sha256"),
        Index("ix_document_project", "project_id"),
        Index("ix_document_sha256", "sha256"),
    )
