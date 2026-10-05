"""Project and structure-node models (PRD §5.3, §6.5)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    CHAR,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.types import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_language: Mapped[str] = mapped_column(CHAR(2), nullable=False, default="en")
    target_language: Mapped[str] = mapped_column(CHAR(2), nullable=False, default="it")
    genre_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    translation_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text_model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    copyright_confirmed: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False
    )
    # §8.2 advanced per-model settings (temperature/top_p/seed/reasoning/
    # timeout/retry/max_output/prompt template) persisted per project.
    model_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    structure_nodes: Mapped[list["StructureNode"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_languages", "source_language", "target_language"),
    )


class StructureNode(Base):
    """One node of the book's detected structure (PRD §5.3 output)."""

    __tablename__ = "structure_nodes"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        UUID(), ForeignKey("structure_nodes.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_page: Mapped[int | None] = mapped_column(nullable=True)
    end_page: Mapped[int | None] = mapped_column(nullable=True)
    start_char: Mapped[int | None] = mapped_column(nullable=True)
    end_char: Mapped[int | None] = mapped_column(nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    detection_method: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    ordinal: Mapped[int | None] = mapped_column(nullable=True)

    project: Mapped[Project] = relationship(back_populates="structure_nodes")

    __table_args__ = (
        UniqueConstraint("project_id", "ordinal", name="ux_structure_node_project_ordinal"),
        Index("ix_structure_node_project", "project_id"),
        Index("ix_structure_node_parent", "parent_id"),
        Index("ix_structure_node_kind", "kind"),
    )
