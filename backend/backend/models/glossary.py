"""Glossary terms (termbase) model (PRD §6.5, §7.1)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class GlossaryTerm(Base):
    """One termbase entry with versioning (PRD §6.5)."""

    __tablename__ = "glossary_terms"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_term: Mapped[str] = mapped_column(Text, nullable=False)
    target_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_type: Mapped[str] = mapped_column(String(48), nullable=False)
    preferred: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    forbidden_targets: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: [])
    grammatical_gender_it: Mapped[str | None] = mapped_column(String(24), nullable=True)
    grammatical_number: Mapped[str | None] = mapped_column(String(24), nullable=True)
    inflection_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_glossary_project", "project_id"),
        Index("ix_glossary_status", "status"),
    )
