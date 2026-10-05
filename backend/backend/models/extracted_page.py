"""L1 parse results: one row per extracted page + the import report (5.2.6)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.types import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def sa_false():
    """SQL ``FALSE`` literal used as server default for boolean columns."""
    return false()


def _uuid() -> str:
    return str(uuid.uuid4())


class DocumentPage(Base):
    """Per-page L1 extraction result (PRD 5.2: coordinates + hashes).

    ``page_payload`` keeps the full structured record: raw extraction blocks
    with page/bbox coordinates, reading order, heading hints and zones; the
    normalised text lives beside it. ``page_sha256`` is the content hash of
    the payload itself (integrity + AC1). One row per (document, page).

    When the page has been OCR-ed (PRD 5.2 step 4 for scans, ADR-002 L3/L4)
    the OCR columns carry the result of the *last* OCR run for that page:
    ``ocr_suspect`` is the §5.2/§13 "OCR sospetto" flag consumed by QA
    (§10.2) and the UI filter (§11.2); ``ocr_level`` is the ADR-002 level
    that produced the text (L3, or L4 after escalation).
    """

    __tablename__ = "document_pages"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(nullable=False)  # 1-based
    extractor: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    char_count: Mapped[int] = mapped_column(nullable=False, default=0)
    suspect_chars: Mapped[int] = mapped_column(nullable=False, default=0)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    ocr_suspect: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False, server_default=sa_false()
    )
    ocr_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    ocr_mean_line_conf: Mapped[float | None] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("document_id", "page_number",
                         name="ux_document_page_number"),
        Index("ix_document_page_document", "document_id"),
    )


class ImportReport(Base):
    """Import report 5.2.6 for one document, updated incrementally."""

    __tablename__ = "import_reports"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {})
    pages_ok: Mapped[int] = mapped_column(nullable=False, default=0)
    pages_ocr_needed: Mapped[int] = mapped_column(nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("document_id", name="ux_import_report_document"),
        Index("ix_import_report_document", "document_id"),
    )
