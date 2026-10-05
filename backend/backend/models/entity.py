"""Entity, alias and evidence models (PRD §6.2–6.5)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
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
from sqlalchemy.types import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Entity(Base):
    """A resolved entity for a project, with gender/number/policy (PRD §6.4)."""

    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    canonical_source: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    referential_gender: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    referential_gender_evidence: Mapped[str | None] = mapped_column(String(255), nullable=True)
    italian_grammatical_gender: Mapped[str] = mapped_column(String(24), nullable=False, default="not_applicable")
    grammatical_number: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    translation_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="undecided")
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # §6.6 (workflow UI) fields owned by the human reviewer.
    forbidden_targets: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=lambda: [])
    priority: Mapped[str | None] = mapped_column(String(24), nullable=True)  # block_batch|warn|normal
    never_translate: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False)
    allow_inflection: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    aliases: Mapped[list["EntityAlias"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    evidence: Mapped[list["EntityEvidence"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_entities_project", "project_id"),
        Index("ix_entities_type", "entity_type"),
        Index("ix_entities_status", "status"),
    )


class EntityAlias(Base):
    """An alternative source/target form of an entity (PRD §6.5)."""

    __tablename__ = "entity_aliases"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    source_alias: Mapped[str] = mapped_column(Text, nullable=False)
    target_alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    alias_type: Mapped[str] = mapped_column(String(48), nullable=False)

    entity: Mapped[Entity] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("entity_id", "source_alias", name="ux_entity_alias_entity_source"),
        Index("ix_entity_alias_entity", "entity_id"),
    )


class EntityEvidence(Base):
    """A citation supporting an entity (PRD §6.5)."""

    __tablename__ = "entity_evidence"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    source_segment_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    page_number: Mapped[int | None] = mapped_column(nullable=True)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(48), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    extractor: Mapped[str] = mapped_column(String(128), nullable=False)

    entity: Mapped[Entity] = relationship(back_populates="evidence")

    __table_args__ = (
        Index("ix_entity_evidence_entity", "entity_id"),
        Index("ix_entity_evidence_page", "page_number"),
    )


class EntityVersion(Base):
    """An immutable snapshot of an entity's §6.4/§6.6 fields (PRD §15.2, §15.4).

    Every user edit to a non-proposed entity records the pre-change state here
    so the review history is auditable and a bad edit can be rolled back.
    """

    __tablename__ = "entity_versions"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Frozen view of the entity's fields at the moment of the edit; mirrors the
    # shape returned by ``_entity_summary`` so the UI can show a full diff.
    snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # patch|merge|split
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_entity_version_entity", "entity_id"),
        Index("ix_entity_version_entity_version", "entity_id", "version"),
    )


class EntityInvalidation(Base):
    """A segment flagged as potentially inconsistent after an approved edit
    (PRD §15.4: "La modifica di un termine indicato quale segmenti approvati
    potrebbero essere inconsistenti, senza sovrascriverli")."""

    __tablename__ = "entity_invalidations"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("translation_units.id", ondelete="CASCADE"),
        nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_entity_invalidation_segment", "segment_id"),
    )
