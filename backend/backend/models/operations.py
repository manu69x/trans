"""LLM runs, memory snapshots, jobs and audit log (PRD §5.1, §7.2, §16)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LLMRun(Base):
    """One request/response against LLM Gateway (PRD §8)."""

    __tablename__ = "llm_runs"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)  # translate|classify|ner|qa
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # F2 / PRD §8.4: idempotency + resumable branch.
    run_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_llm_runs_project", "project_id"),
        Index("ix_llm_runs_model", "model_name"),
        Index("ix_llm_runs_status", "status"),
        Index("ix_llm_runs_run_ref", "run_ref"),
        Index("ix_llm_runs_branch", "branch"),
    )


class MemorySnapshot(Base):
    """Immutable frozen view of glossary/TM at a point in time (PRD §7.2)."""

    __tablename__ = "memory_snapshots"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_type: Mapped[str] = mapped_column(String(16), nullable=False)  # glossary|tm
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_count: Mapped[int] = mapped_column(nullable=False, default=0)
    # F3 / PRD §7.2, §16: the frozen item ids this snapshot references, so the
    # translation planner (§10.1 step 3) can render the exact prompt from the
    # immutable snapshot and the UI can show what was injected.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_snapshot_project", "project_id"),
        Index("ix_snapshot_type", "snapshot_type"),
    )


class Job(Base):
    """A queued/background work item (PRD §16)."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)  # import|parse|ocr|translate
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_jobs_project", "project_id"),
        Index("ix_jobs_status", "status"),
    )


class AuditLog(Base):
    """Append-only audit trail (PRD §13, §16)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    user_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(UUID(), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_audit_project", "project_id"),
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_created", "created_at"),
    )


class User(Base):
    """Local user account (PRD §2, §13)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="translator")
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_users_email", "email"),)


class TranslationUnitVersion(Base):
    """An immutable snapshot of a segment's target_text (PRD §11.2, §10.5).

    Each refinement of an editable segment appends a row: ``before`` is True
    for the state that was in place before the change (so the editor can
    diff and roll back) and False for the new state that became live. The
    live ``translation_units.target_text`` is always the most recent row.
    """

    __tablename__ = "translation_unit_versions"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("translation_units.id", ondelete="CASCADE"),
        nullable=False)
    target_text: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False,
                                         server_default="refine")
    diff_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True,
                                                   default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_unit_version_unit", "unit_id"),
        Index("ix_unit_version_project", "project_id"),
    )


class QaIssue(Base):
    """A QA issue on a segment, surfaced in the §11.2 "QA issues" panel.

    Severity follows the §10.4 scale ``minor`` / ``major`` / ``critical``;
    ``category`` is the MQM error category (§10.4); ``evidence`` quotes the
    offending span (§10.3) and ``suggestion`` describes what the revisor
    should do without rewriting (§10.3 / §10.5). ``span`` is the target span
    the revisor selected (§10.4/AC1) and ``comment`` the revisor's free-text
    note on it; with ``category``/``severity`` they back the human MQM
    annotation (§10.4/AC2). ``kind`` is one of ``ocr`` /
    ``entity`` / ``qa``. The "solo QA critici" filter (§11.2) selects the
    critical, unresolved rows.
    """

    __tablename__ = "qa_issues"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        UUID(), ForeignKey("translation_units.id", ondelete="CASCADE"),
        nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False,
                                           default="minor")
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    span: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_qa_issue_unit", "unit_id"),
        Index("ix_qa_issue_project", "project_id"),
        Index("ix_qa_issue_severity", "severity"),
        Index("ix_qa_issue_category", "category"),
    )


class Role(Base):
    """Named role with a set of permissions (PRD §2.1)."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(UUID(), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: [])
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, default=datetime.utcnow)
