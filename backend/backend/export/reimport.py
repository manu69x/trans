"""Bilingual re-import: XLIFF 2.1 / CSV / TMX 1.4 back into the platform
(PRd §1.3.7, §15.4, §10.5, §14).

A CAT tool round-trip is: export (this subsystem) -> edit in the tool ->
re-import here. The import applies the tool's changes *without* breaking the
platform's invariants:

* **Versioning (§11.2 / §10.5)** -- every change to a segment's target first
  freezes the pre-change target on ``translation_unit_versions``
  (``before=True``) and records the new state (``before=False``); the live
  ``target_text`` is always the latest row, so no version is ever lost.
* **Approved immutability (§5.1)** -- an ``approved`` segment is never
  overwritten: an incoming target that differs from it is reported as a
  *conflict* and left untouched.
* **Approval via import** -- a unit whose imported status is ``approved``
  (XLIFF ``state="final"`` / ``trans:status=approved``) is approved: the
  target is frozen, the status flips, and the §7.2 TM entry is registered
  (deduped by ``segment_id``), mirroring the editor's approve hook.
* **No silent deletes** -- an empty incoming target is treated as
  "unchanged", never as "clear the translation".

The three :func:`reimport_*` entry points return a summary the UI / audit
log consume; a single ``audit_log`` row (``action='export_reimport'``) is
written for the batch, plus one ``segment_approved`` row per unit approved
(§13.1: approvals are audited events).

.. note::
   :func:`~.audit.log_event` commits the session, so each applied unit is
   committed immediately (the import is row-atomic: one failing unit cannot
   roll back the units already applied -- acceptable for a CAT round-trip,
   which the user re-runs after fixing the offending file).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .. import audit
from ..models import (
    Project,
    TranslationMemoryEntry,
    TranslationUnit,
    TranslationUnitVersion,
)
from .csv_export import read_csv
from .errors import ExportError
from .tmx_export import read_tmx
from .xliff_export import read_xliff, state_to_status


def _get_unit(db: Session, project_id: str, segment_id: str) -> TranslationUnit | None:
    if not segment_id:
        return None
    return db.get(TranslationUnit, segment_id)


def _freeze_version(
    db: Session,
    project_id: str,
    unit: TranslationUnit,
    before_text: str,
    after_text: str,
    action: str,
) -> None:
    """Freeze the pre-change state and record the new live state (§10.5)."""
    db.add(
        TranslationUnitVersion(
            id=str(uuid.uuid4()),
            project_id=project_id,
            unit_id=str(unit.id),
            target_text=before_text if before_text != after_text else after_text,
            before=True,
            action=action,
            diff_json={},
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        TranslationUnitVersion(
            id=str(uuid.uuid4()),
            project_id=project_id,
            unit_id=str(unit.id),
            target_text=after_text,
            before=False,
            action=action,
            diff_json={},
            created_at=datetime.utcnow(),
        )
    )


def _register_tm_entry(db: Session, project: Project, unit: TranslationUnit) -> str:
    """Register/update the §7.2 TM entry for an approved unit (idempotent)."""
    existing = (
        db.query(TranslationMemoryEntry)
        .filter(TranslationMemoryEntry.segment_id == str(unit.id))
        .one_or_none()
    )
    if existing is not None:
        existing.target_approved = unit.target_text or ""
        return str(existing.id)
    from ..translation import embedding  # local-only deterministic embedding

    entry = TranslationMemoryEntry(
        id=str(uuid.uuid4()),
        project_id=str(project.id),
        chapter_id=unit.chapter_id,
        segment_id=str(unit.id),
        source_normalized=unit.source_text or "",
        source_original=unit.source_text or "",
        target_approved=unit.target_text or "",
        genre_profile=project.genre_profile,
        terms_used=[],
        source_embedding=embedding.embed(unit.source_text or ""),
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    return str(entry.id)


def _apply_unit(
    db: Session,
    project: Project,
    *,
    segment_id: str,
    target: str | None,
    status: str | None,
) -> str:
    """Apply one imported unit. Returns the outcome code:

    ``updated`` | ``approved`` | ``unchanged`` | ``skipped_approved`` |
    ``not_found``
    """
    unit = _get_unit(db, str(project.id), segment_id)
    if unit is None:
        return "not_found"

    # §5.1: approved segments are immutable.
    if unit.status == "approved":
        return "skipped_approved"

    old_target = unit.target_text or ""
    old_status = unit.status
    new_target = target if (target is not None and target != "") else old_target
    new_status = status or old_status

    target_changed = new_target != old_target
    approving = new_status == "approved" and old_status != "approved"

    if not target_changed and not approving:
        return "unchanged"

    if target_changed:
        _freeze_version(db, str(project.id), unit, old_target, new_target, "reimport")
        unit.target_text = new_target

    if approving:
        unit.status = "approved"
        unit.updated_at = datetime.utcnow()
        _register_tm_entry(db, project, unit)
        audit.log_event(
            action="segment_approved",
            entity="segment",
            project_id=str(project.id),
            entity_id=str(unit.id),
            before={"status": "draft", "target_text": old_target},
            after={"status": "approved", "via": "reimport"},
            db=db,
        )
        return "approved"

    if new_status != old_status:
        unit.status = new_status
    unit.updated_at = datetime.utcnow()
    audit.log_event(
        action="segment_reimported",
        entity="segment",
        project_id=str(project.id),
        entity_id=str(unit.id),
        before={"status": old_status, "target_text": old_target},
        after={"status": unit.status, "target_text": unit.target_text},
        db=db,
    )
    return "updated"


def _summary_audit(db: Session, project: Project, summary: dict) -> None:
    audit.log_event(
        action="export_reimport",
        entity="project",
        project_id=str(project.id),
        after=summary,
        db=db,
    )


# ---------------------------------------------------------------------------
# XLIFF
# ---------------------------------------------------------------------------
def reimport_xliff(db: Session, project: Project, data: bytes | str) -> dict:
    """Apply an imported XLIFF 2.1 file. See module docstring for rules."""
    units = read_xliff(data)
    counts = {
        "updated": 0,
        "approved": 0,
        "unchanged": 0,
        "skipped_approved": 0,
        "not_found": 0,
    }
    conflicts: list[dict] = []
    for u in units:
        outcome = _apply_unit(
            db,
            project,
            segment_id=u.id,
            target=u.target,
            status=u.status,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "skipped_approved" and u.target is not None:
            existing = _get_unit(db, str(project.id), u.id)
            if existing is not None and u.target != (existing.target_text or ""):
                conflicts.append(
                    {
                        "segment_id": u.id,
                        "existing_target": existing.target_text,
                        "incoming_target": u.target,
                    }
                )

    summary = {
        "format": "xliff",
        "total_units": len(units),
        "counts": counts,
        "conflicts": conflicts,
    }
    _summary_audit(db, project, summary)
    return summary


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def reimport_csv(db: Session, project: Project, data: bytes | str) -> dict:
    """Apply an imported bilingual CSV. See module docstring for rules."""
    rows = read_csv(data)
    counts = {
        "updated": 0,
        "approved": 0,
        "unchanged": 0,
        "skipped_approved": 0,
        "not_found": 0,
    }
    for r in rows:
        outcome = _apply_unit(
            db,
            project,
            segment_id=r.segment_id,
            target=r.target,
            status=r.status,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    summary = {
        "format": "csv",
        "total_units": len(rows),
        "counts": counts,
    }
    _summary_audit(db, project, summary)
    return summary


# ---------------------------------------------------------------------------
# TMX (translation-memory round-trip)
# ---------------------------------------------------------------------------
def reimport_tmx(db: Session, project: Project, data: bytes | str) -> dict:
    """Apply an imported TMX 1.4 file.

    The TMX carries the approved TM. Each ``<tu>`` whose ``tuid`` matches a
    ``tm_entries`` id updates the entry's ``target_approved`` (the TM is
    living memory -- the §7.2 maintenance job re-checks consistency
    afterwards). If the entry's segment exists and is *not* approved, the
    edited target also lands on the segment with full versioning; approved
    segments stay untouched (the divergence is reported as a conflict).
    """
    units = read_tmx(data)
    counts = {
        "updated": 0,
        "unchanged": 0,
        "not_found": 0,
        "skipped_approved": 0,
    }
    conflicts: list[dict] = []
    for u in units:
        if not u.tuid:
            continue
        entry = db.get(TranslationMemoryEntry, u.tuid)
        if entry is None or str(entry.project_id) != str(project.id):
            counts["not_found"] += 1
            continue
        if u.target is not None and u.target != (entry.target_approved or ""):
            entry.target_approved = u.target
            counts["updated"] += 1
            if entry.segment_id:
                unit = _get_unit(db, str(project.id), str(entry.segment_id))
                if unit is None:
                    continue
                if unit.status == "approved":
                    counts["skipped_approved"] += 1
                    if u.target != (unit.target_text or ""):
                        conflicts.append(
                            {
                                "segment_id": str(unit.id),
                                "existing_target": unit.target_text,
                                "incoming_target": u.target,
                            }
                        )
                else:
                    _freeze_version(
                        db,
                        str(project.id),
                        unit,
                        unit.target_text or "",
                        u.target,
                        "reimport",
                    )
                    unit.target_text = u.target
                    unit.updated_at = datetime.utcnow()
        else:
            counts["unchanged"] += 1

    summary = {
        "format": "tmx",
        "total_units": len(units),
        "counts": counts,
        "conflicts": conflicts,
    }
    _summary_audit(db, project, summary)
    return summary


REIMPORTS = {
    "xliff": reimport_xliff,
    "csv": reimport_csv,
    "tmx": reimport_tmx,
}
