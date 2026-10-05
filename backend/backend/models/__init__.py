"""SQLAlchemy declarative base and ORM models.

Importing this package registers every model on ``Base.metadata`` so that
Alembic autogenerate sees all tables.
"""
from __future__ import annotations

from .base import Base
from .document import Document
from .extracted_page import DocumentPage, ImportReport
from .entity import (
    Entity,
    EntityAlias,
    EntityEvidence,
    EntityInvalidation,
    EntityVersion,
)
from .glossary import GlossaryTerm
from .operations import (
    AuditLog,
    Job,
    LLMRun,
    MemorySnapshot,
    QaIssue,
    Role,
    TranslationUnitVersion,
    User,
)
from .project import Project, StructureNode
from .translation import TranslationMemoryEntry, TranslationUnit

__all__ = [
    "Base",
    "Project",
    "StructureNode",
    "Document",
    "DocumentPage",
    "ImportReport",
    "Entity",
    "EntityAlias",
    "EntityEvidence",
    "EntityInvalidation",
    "EntityVersion",
    "GlossaryTerm",
    "TranslationUnit",
    "TranslationMemoryEntry",
    "TranslationUnitVersion",
    "QaIssue",
    "LLMRun",
    "MemorySnapshot",
    "Job",
    "AuditLog",
    "User",
    "Role",
]
