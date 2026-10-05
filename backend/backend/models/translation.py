"""Translation units and short-term/long-term memory models (PRD §5.4, §7.2)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP
from pgvector.sqlalchemy import Vector

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TranslationUnit(Base):
    """One CAT segment/verse with source, target and provenance (PRD §6.5).

    ``source_flags`` carries non-translational alerts about the SOURCE text
    so QA (§10.2) and the UI filter (§11.2 "solo OCR sospetto") can select
    segments without parsing free-form columns: ``{"ocr_suspect": true,
    "ocr_page": 3}`` is set by the OCR runner when a segment originates from
    a suspect OCR page.
    """

    __tablename__ = "translation_units"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="untranslated")
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_flags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    glossary_snapshot_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    tm_snapshot_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    model_run_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    #: Stable per-project ordinal of the segment, assigned at creation.
    numero: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # QE verification scores (PRD §10.2-bis), each 0..1 as produced by the
    # quality-estimation endpoint (see backend.qe_client).
    # is_english = p(yes) "Is this text written in English?" on the target:
    # a high value with a low is_italian flags a draft that stayed English.
    is_italian: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    is_translated: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    is_english: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("project_id", "chapter_id", "ordinal", name="ux_tu_project_chapter_ordinal"),
        Index("ux_tu_project_numero", "project_id", "numero", unique=True),
        Index("ix_tu_project", "project_id"),
        Index("ix_tu_status", "status"),
    )


class TranslationMemoryEntry(Base):
    """Approved source/target pair stored for retrieval (PRD §7.2)."""

    __tablename__ = "tm_entries"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    scene_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    # F3 / PRD §7.2: which approved segment this entry came from, so the
    # maintenance job (§7.2) can flag a contradictory target on the same
    # source and the dashboard can compute the TM reuse rate.
    #
    # Deliberately NOT a foreign key: an entry whose segment was invalidated
    # / re-segmented must remain in the TM so the maintenance job can flag it
    # as *obsolete* (§7.2). A real FK (CASCADE or SET NULL) would reject the
    # insert of an entry whose segment no longer exists, making obsolete-entry
    # detection impossible.
    segment_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    source_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    source_original: Mapped[str] = mapped_column(Text, nullable=False)
    target_approved: Mapped[str] = mapped_column(Text, nullable=False)
    genre_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pov: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qa_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    terms_used: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    source_embedding: Mapped[Vector | None] = mapped_column(Vector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_tm_project", "project_id"),
        Index("ix_tm_source_normalized", "source_normalized"),
        Index("ix_tm_segment", "segment_id"),
    )
