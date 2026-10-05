"""Structure editor operations (PRD §5.3, §11.1, §12.2, §15.1).

The pure + persistence layer behind the visual structure editor: the
§5.3 rule ("deve pertanto esistere sempre un editor visuale per creare,
unire, dividere, spostare e rinominare capitoli") implemented on top of
the :class:`~backend.models.StructureNode` rows the detection runner
proposes.  The HTTP surface lives in :mod:`backend.structure_routes`;
this module owns the invariants:

* **Continuous partition** — after any page-range operation (split,
  merge, boundary move, delete) the following nodes' ranges are rebuilt
  forward so the book stays a gapless partition of pages: a chapter
  whose predecessor now ends later starts right after it, and
  page-derived fields (``start_char``/``end_char``) are re-derived from
  the stored pages.  Chapter-level §5.4 re-segmentation therefore sees
  consistent ranges for every chapter, while *untouched* chapters keep
  their nodes (and their approved segments) exactly as they were (§15.1:
  "rigenera solo i segmenti dipendenti").
* **Immutable approvals (§5.1)** — segment operations move/reassign
  ``translation_units`` rows but never overwrite an approved target:
  merged units keep ``status``/``target_text``, a node whose segments
  contain approved translations cannot be deleted (HTTP-independent
  guard, surfaced as 409 by the route).
* **Undo (AC3)** — every edit operation persists an ``undo`` payload
  (complete before-state of the touched nodes + moved segment anchors)
  in an append-only :class:`~backend.models.AuditLog` row (§13.1: user
  actions are audited).  :func:`run_structure_undo` restores the most
  recent not-yet-undone operation — append-only, so undo itself is a new
  audit row that references the original one.
* **Sanitised logs (§13.1)** — audit payloads carry IDs, counts and
  hashes only, never manuscript text.
* **Idempotent rebuild (§14)** — page-derived fields are content-derived
  from the committed pages: rebuilding twice converges to the same
  values.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import Session

from .segment_numbering import next_numero_start
from .models import (
    AuditLog,
    Document,
    Project,
    StructureNode,
    TranslationUnit,
)
from .parsing import structure
from . import chunking_runner

# AuditLog actions that carry an undo payload (in insertion order).
EDIT_ACTIONS = (
    "structure_node_created",
    "structure_node_merged",
    "structure_node_split",
    "structure_nodes_moved",
    "structure_node_deleted",
    "structure_node_boundary_corrected",
    "structure_node_renamed",
)

# actions that change a node's page range and therefore require the
# selective re-segmentation afterwards (§15.1)
RANGE_ACTIONS = (
    "structure_node_created",
    "structure_node_merged",
    "structure_node_split",
    "structure_nodes_moved",
    "structure_node_boundary_corrected",
)

KINDS = ("front_matter", "part", "chapter", "scene", "back_matter",
         "footnote")


class EditorError(Exception):
    """Guard violation; the routes map these to 4xx responses."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# snapshots (undo payloads)
# --------------------------------------------------------------------------
def _node_snapshot(node: StructureNode) -> dict:
    return {
        "id": str(node.id),
        "project_id": str(node.project_id),
        "parent_id": str(node.parent_id) if node.parent_id else None,
        "kind": node.kind,
        "source_label": node.source_label,
        "normalized_title": node.normalized_title,
        "start_page": node.start_page,
        "end_page": node.end_page,
        "start_char": node.start_char,
        "end_char": node.end_char,
        "confidence": float(node.confidence) if node.confidence is not None
        else None,
        "detection_method": list(node.detection_method or []),
        "status": node.status,
        "ordinal": node.ordinal,
    }


def _restore_node(db: Session, snap: dict) -> StructureNode:
    """Restore one node row exactly to its snapshot (undo).

    Ordinals are written back as-is; the caller must have parked the whole
    project's ordinals first (see :func:`op_undo`) so restoring a snapshot
    ordinal can never collide with a row that still holds it.
    """
    node = db.get(StructureNode, snap["id"])
    if node is None:
        node = StructureNode(id=snap["id"], project_id=snap["project_id"])
    node.parent_id = snap["parent_id"]
    node.kind = snap["kind"]
    node.source_label = snap["source_label"]
    node.normalized_title = snap["normalized_title"]
    node.start_page = snap["start_page"]
    node.end_page = snap["end_page"]
    node.start_char = snap["start_char"]
    node.end_char = snap["end_char"]
    node.confidence = snap["confidence"]
    node.detection_method = list(snap["detection_method"] or [])
    node.status = snap["status"]
    node.ordinal = snap["ordinal"]
    db.add(node)
    return node


def record_edit(db: Session, project_id: str, action: str,
                before: list[dict], after: list[dict], undo: dict,
                summary: dict) -> AuditLog:
    """One append-only audit row per edit: before/after + undo payload.

    ``before``/``after`` describe the touched nodes for the audit UI (§13.1
    user actions); ``undo`` (nested under the same row's ``after``) carries
    the machine-restorable previous state.  Returns the row so callers can
    reference its id.
    """
    row = AuditLog(
        project_id=project_id,
        action=action,
        entity="structure",
        entity_id=str(project_id),
        before={"nodes": before},
        after={"nodes": after, "summary": summary, "undo": undo},
        created_at=datetime.utcnow(),
    )
    db.add(row)
    return row


# --------------------------------------------------------------------------
# derived fields
# --------------------------------------------------------------------------
def _page_text_offsets(db: Session, project_id: str) -> dict[int, tuple[int, int, str]]:
    """page_number -> (char_start, char_end, document_id) of every page.

    The offsets are content-derived: cumulative length of the stored
    normalised page texts in reading order (documents ordered by creation,
    pages by number) — the same convention the structure layer's
    ``start_char``/``end_char`` use.
    """
    offsets: dict[int, tuple[int, int, str]] = {}
    cursor = 0
    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    from .models import DocumentPage

    for document in documents:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        for row in rows:
            text = row.normalized_text or ""
            offsets[int(row.page_number)] = (cursor, cursor + len(text),
                                             str(document.id))
            cursor += len(text) + 1  # +1: the newline between pages
    return offsets


def _rebuild_derived(db: Session, project_id: str,
                     touched_ids: set[str]) -> dict:
    """Re-derive page ranges forward + char offsets for the touched nodes.

    * char offsets: recomputed for every *touched* node from the stored
      pages (idempotent: same pages -> same numbers).
    * forward ranges: every node AFTER a touched one starts at least one
      page after the previous node ends, so the book stays a continuous
      partition after a boundary move/split/merge/delete.
    """
    offsets = _page_text_offsets(db, project_id)
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    prev_end: int | None = None
    adjusted = 0
    for node in nodes:
        is_touched = str(node.id) in touched_ids
        if is_touched and node.kind != "scene":
            # re-derive the char offsets from the stored pages
            if node.start_page is not None and int(node.start_page) in offsets:
                node.start_char = offsets[int(node.start_page)][0]
            if node.end_page is not None and int(node.end_page) in offsets:
                node.end_char = offsets[int(node.end_page)][1]
            db.add(node)
        # forward continuity for chapter-level nodes (scenes may share the
        # page with their chapter)
        if node.kind != "scene" and prev_end is not None \
                and node.start_page is not None \
                and int(node.start_page) <= prev_end:
            node.start_page = prev_end + 1
            if node.end_page is not None and node.end_page < node.start_page:
                node.end_page = node.start_page
            if node.start_page in offsets:
                node.start_char = offsets[node.start_page][0]
            if node.end_page in offsets:
                node.end_char = offsets[node.end_page][1]
            db.add(node)
            adjusted += 1
        if node.kind != "scene" and node.end_page is not None:
            prev_end = int(node.end_page)
    return {"offsets_derived": len(touched_ids), "ranges_adjusted": adjusted}


def _compact_ordinals(db: Session, project_id: str) -> dict[str, int]:
    """Reassign ordinals 0..n-1 in reading order (book order).

    Scenes sort directly after the node that opens their page (same
    convention as the detection runner).  The reassignment NULLs every
    ordinal first (NULLs never collide under a unique constraint) so a
    fresh 0..n-1 sequence can always be written mid-transaction.  Returns
    the new ordinal per id.
    """
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .all()
    )
    nodes.sort(key=lambda n: (
        n.start_page is None,
        n.start_page if n.start_page is not None else 0,
        n.kind == "scene",
        n.ordinal if n.ordinal is not None else 0,
    ))
    for node in nodes:
        node.ordinal = None
    db.flush()
    mapping: dict[str, int] = {}
    for ordinal, node in enumerate(nodes):
        node.ordinal = ordinal
        db.add(node)
        mapping[str(node.id)] = ordinal
    db.flush()
    return mapping


def _guard_not_scene(node: StructureNode, op: str) -> None:
    if node.kind == "scene":
        raise EditorError(f"cannot {op} a scene: scenes are derived "
                          f"narrative units (§5.3)")


def _guard_no_approved(db: Session, node_id: str) -> int:
    approved = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.chapter_id == node_id,
                TranslationUnit.status == "approved")
        .count()
    )
    if approved:
        raise EditorError(
            f"node has {approved} approved segments: approval is immutable "
            f"(§5.1); reject or unapprove them first", status_code=409)
    return approved


# --------------------------------------------------------------------------
# operations (each: guards -> mutate -> rebuild -> audit -> return summary)
# --------------------------------------------------------------------------
def op_create(db: Session, project: Project, *, kind: str,
              source_label: str, normalized_title: str | None,
              start_page: int, end_page: int | None,
              parent_id: str | None) -> dict:
    """Insert a manually-drawn node (§5.3 'creare')."""
    if kind not in KINDS:
        raise EditorError(f"unknown kind {kind!r}")
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id)
        .all()
    )
    before = [_node_snapshot(n) for n in
              sorted(nodes, key=lambda n: n.ordinal or 0)]
    node = StructureNode(
        project_id=project.id,
        parent_id=parent_id,
        kind=kind,
        source_label=source_label,
        normalized_title=normalized_title
        or structure.normalize_title(source_label),
        start_page=start_page,
        end_page=end_page if end_page is not None else start_page,
        start_char=None,
        end_char=None,
        confidence=1.0,
        detection_method=["user"],
        status="user_confirmed",  # the user drew it: no proposal to confirm
        ordinal=None,  # assigned after insert: (project, ordinal) is UNIQUE
    )
    db.add(node)
    db.flush()
    node_id_str = str(node.id)
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(db, project.id, {node_id_str})
    after = [_node_snapshot(n) for n in
             db.query(StructureNode)
             .filter(StructureNode.project_id == project.id)
             .order_by(StructureNode.ordinal).all()]
    record_edit(
        db, str(project.id), "structure_node_created",
        before, after,
        undo={"nodes": before, "created_node_ids": [str(node.id)]},
        summary={"op": "create", "node_id": str(node.id),
                 "kind": kind, "start_page": start_page,
                 "ranges_adjusted": summary["ranges_adjusted"],
                 "ordinals": ordinals},
    )
    db.commit()
    return {"node": _node_snapshot(node) | {"ordinal": ordinals[str(node.id)]},
            "rebuild": summary}


def op_rename(db: Session, project: Project, node: StructureNode,
              source_label: str) -> dict:
    """§5.3 'rinominare' (title-only edit; no re-segmentation needed)."""
    before = [_node_snapshot(node)]
    undo_nodes = [dict(before[0])]
    node.source_label = source_label
    node.normalized_title = structure.normalize_title(source_label)
    db.add(node)
    summary = {"op": "rename", "node_id": str(node.id),
               "normalized_title": node.normalized_title}
    after = [_node_snapshot(node)]
    record_edit(db, str(project.id), "structure_node_renamed",
                before, after, undo={"nodes": undo_nodes}, summary=summary)
    db.commit()
    return summary


def op_set_boundary(db: Session, project: Project, node: StructureNode,
                    *, start_page: int | None, end_page: int | None) -> dict:
    """Correct a chapter's boundary (§15.1) — the node's range changes.

    Only the touched node's range is set here; the forward rebuild fixes
    the continuity for the following chapters.  The caller schedules the
    selective re-segmentation job afterwards (§15.1: only dependent
    segments regenerate).
    """
    before = [_node_snapshot(node)]
    if start_page is not None:
        node.start_page = start_page
    if end_page is not None:
        node.end_page = end_page
    if node.start_page is not None and node.end_page is not None \
            and node.end_page < node.start_page:
        node.end_page = node.start_page
    db.add(node)
    summary = _rebuild_derived(db, project.id, {str(node.id)})
    summary.update({"op": "boundary", "node_id": str(node.id),
                    "start_page": node.start_page, "end_page": node.end_page})
    after = [_node_snapshot(node)]
    record_edit(db, str(project.id), "structure_node_boundary_corrected",
                before, after, undo={"nodes": before}, summary=summary)
    db.commit()
    return summary


def op_split(db: Session, project: Project, node: StructureNode,
             split_page: int, label_prefix: str | None = None) -> dict:
    """§5.3 'dividere': split a chapter at a page boundary.

    The node keeps its start; the new node takes ``split_page``..end.
    Page-derived fields are re-derived for both halves.
    """
    _guard_not_scene(node, "split")
    if node.start_page is None or node.end_page is None:
        raise EditorError("node has no page range to split")
    if not node.start_page < split_page <= node.end_page:
        raise EditorError(
            f"split page must be inside the chapter "
            f"({node.start_page + 1}..{node.end_page})")
    before = [_node_snapshot(node)]
    old_end = int(node.end_page)
    old_label = node.source_label or node.normalized_title or "Chapter"
    node.end_page = split_page - 1
    db.add(node)
    new_node = StructureNode(
        project_id=project.id,
        parent_id=node.parent_id,
        kind=node.kind,
        source_label=f"{label_prefix or old_label} (b)",
        normalized_title=structure.normalize_title(
            f"{label_prefix or old_label} (b)"),
        start_page=split_page,
        end_page=old_end,
        start_char=None,
        end_char=None,
        confidence=1.0,
        detection_method=["user"],
        status="user_confirmed",
        ordinal=None,  # assigned by the compaction below (UNIQUE with project)
    )
    db.add(new_node)
    db.flush()
    new_node_id = str(new_node.id)
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(db, project.id,
                               {str(node.id), new_node_id})
    summary.update({"op": "split", "node_id": new_node_id,
                    "split_page": split_page,
                    "first_part_end_page": node.end_page,
                    "ranges_adjusted": summary["ranges_adjusted"]})
    after = [_node_snapshot(n) for n in
             db.query(StructureNode)
             .filter(StructureNode.project_id == project.id)
             .order_by(StructureNode.ordinal).all()]
    record_edit(db, str(project.id), "structure_node_split",
                before, after,
                undo={"nodes": before, "created_node_ids": [new_node_id]},
                summary={**summary, "ordinals": ordinals})
    db.commit()
    return {"new_node_id": new_node_id, "rebuild": summary}


def op_merge(db: Session, project: Project, node: StructureNode,
             target: StructureNode) -> dict:
    """§5.3 'unire': merge a chapter into the adjacent one.

    Only the immediately-preceding or immediately-following chapter-level
    node is a valid target; the absorbed node disappears, its segments
    move to the target with their approval state intact (§5.1) and a
    revision bump tells the translation adapter to re-read them.
    """
    _guard_not_scene(node, "merge")
    _guard_not_scene(target, "merge")
    if node.id == target.id:
        raise EditorError("cannot merge a node into itself")
    # only the page-adjacent chapter-level nodes are valid targets, so the
    # merge keeps the book a continuous partition of pages
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id,
                StructureNode.kind != "scene")
        .order_by(StructureNode.ordinal)
        .all()
    )
    index = next((i for i, n in enumerate(nodes) if n.id == node.id), None)
    valid_ids = set()
    if index is not None and index > 0:
        valid_ids.add(str(nodes[index - 1].id))
    if index is not None and index + 1 < len(nodes):
        valid_ids.add(str(nodes[index + 1].id))
    if str(target.id) not in valid_ids:
        raise EditorError(
            "merge target must be the immediately preceding or following "
            "chapter (§5.3)", status_code=409)
    before = [_node_snapshot(node), _node_snapshot(target)]
    # the segments move with the chapter
    existing = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.chapter_id == target.id)
        .count()
    )
    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.chapter_id == node.id)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    moved_ids: list[str] = []
    for offset, unit in enumerate(units):
        unit.chapter_id = target.id
        unit.ordinal = existing + offset + 1
        flags = dict(unit.source_flags or {})
        flags["structure_revision"] = flags.get("structure_revision", 0) + 1
        flags["merged_from_node"] = str(node.id)
        unit.source_flags = flags
        db.add(unit)
        moved_ids.append(str(unit.id))
    node_start = int(node.start_page) if node.start_page is not None else None
    node_end = int(node.end_page) if node.end_page is not None else None
    target.start_page = node_start if (
        node_start is not None
        and (target.start_page is None or node_start < target.start_page)
    ) else target.start_page
    target.end_page = max(
        int(target.end_page or 0), node_end or 0) or target.end_page
    db.add(target)
    absorbed_id = str(node.id)
    # scene children of the absorbed chapter follow their text into the
    # target (the FK is ON DELETE CASCADE — without the re-parent the
    # scenes would silently vanish with the absorbed node)
    scenes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id,
                StructureNode.parent_id == node.id)
        .all()
    )
    for scene in scenes:
        scene.parent_id = target.id
        db.add(scene)
    # snapshot the survivors BEFORE deleting: the ORM expunges the deleted
    # instance from the session and a later refresh of it would blow up
    after = [_node_snapshot(n) for n in
             db.query(StructureNode)
             .filter(StructureNode.project_id == project.id,
                     StructureNode.id != node.id)
             .order_by(StructureNode.ordinal).all()]
    db.delete(node)
    db.flush()
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(db, project.id, {absorbed_id})
    summary.update({"op": "merge", "node_id": absorbed_id,
                    "target_id": str(target.id), "segments_moved":
                    len(moved_ids), "ranges_adjusted":
                    summary["ranges_adjusted"]})
    record_edit(db, str(project.id), "structure_node_merged",
                before, after,
                undo={"nodes": before,
                      "units": [{"id": u, "chapter_id": absorbed_id}
                                for u in moved_ids]},
                summary={**summary, "ordinals": ordinals})
    db.commit()
    return {"absorbed_id": absorbed_id, "target_id": str(target.id),
            "segments_moved": len(moved_ids), "rebuild": summary}


def op_delete(db: Session, project: Project, node: StructureNode) -> dict:
    """Remove a node (proposal the user discards, or a manual node)."""
    _guard_not_scene(node, "delete")
    _guard_no_approved(db, node.id)
    before = [_node_snapshot(node)]
    removed_id = str(node.id)
    # scene children must not silently cascade away with the chapter: the
    # chapter that now surrounds their page (if any) adopts them
    next_chapter = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id,
                StructureNode.kind != "scene",
                StructureNode.id != node.id,
                StructureNode.start_page > node.start_page)
        .order_by(StructureNode.start_page)
        .first()
    )
    for scene in (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id,
                StructureNode.parent_id == node.id)
        .all()
    ):
        scene.parent_id = next_chapter.id if next_chapter else None
        db.add(scene)
    after = [_node_snapshot(n) for n in
             db.query(StructureNode)
             .filter(StructureNode.project_id == project.id,
                     StructureNode.id != node.id)
             .order_by(StructureNode.ordinal).all()]
    db.delete(node)
    db.flush()
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(db, project.id, {removed_id})
    summary.update({"op": "delete", "node_id": removed_id,
                    "ranges_adjusted": summary["ranges_adjusted"]})
    record_edit(db, str(project.id), "structure_node_deleted",
                before, after, undo={"nodes": before},
                summary={**summary, "ordinals": ordinals})
    db.commit()
    return {"removed_id": removed_id, "rebuild": summary}


def op_move(db: Session, project: Project, node: StructureNode,
            direction: str) -> dict:
    """§5.3 'spostare': swap the node's range with the neighbour's.

    Moving a chapter in a book means swapping its place with the adjacent
    chapter; the swap is undone exactly by restoring both snapshots.
    """
    _guard_not_scene(node, "move")
    if direction not in ("up", "down"):
        raise EditorError("direction must be 'up' or 'down'")
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id,
                StructureNode.kind != "scene")
        .order_by(StructureNode.ordinal)
        .all()
    )
    index = next((i for i, n in enumerate(nodes) if n.id == node.id), None)
    if index is None:
        raise EditorError("node not in the chapter list")
    neighbour_index = index - 1 if direction == "up" else index + 1
    if not 0 <= neighbour_index < len(nodes):
        raise EditorError(f"cannot move {direction}: no neighbour")
    neighbour = nodes[neighbour_index]
    before = [_node_snapshot(node), _node_snapshot(neighbour)]
    node.start_page, neighbour.start_page = \
        neighbour.start_page, node.start_page
    node.end_page, neighbour.end_page = neighbour.end_page, node.end_page
    node.start_char, neighbour.start_char = \
        neighbour.start_char, node.start_char
    node.end_char, neighbour.end_char = neighbour.end_char, node.end_char
    # swap the ordinals too: (project_id, ordinal) is UNIQUE, so park both
    # in scratch space, flush, then assign the swapped values
    old_node_ordinal, old_neighbour_ordinal = \
        node.ordinal, neighbour.ordinal
    node.ordinal, neighbour.ordinal = None, None
    db.add(node)
    db.add(neighbour)
    db.flush()
    node.ordinal, neighbour.ordinal = \
        old_neighbour_ordinal, old_node_ordinal
    db.add(node)
    db.add(neighbour)
    db.flush()
    # re-order first, then rebuild: the continuity pass walks in ordinal
    # order, so the swapped pair must already be in their new positions
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(
        db, project.id, {str(node.id), str(neighbour.id)})
    summary.update({"op": "move", "node_id": str(node.id),
                    "swapped_with": str(neighbour.id),
                    "direction": direction,
                    "ranges_adjusted": summary["ranges_adjusted"]})
    after = [_node_snapshot(n) for n in (node, neighbour)]
    record_edit(db, str(project.id), "structure_nodes_moved",
                before, after,
                undo={"nodes": before, "created_node_ids": [],
                      "ordinals": ordinals},
                summary={**summary, "ordinals": ordinals})
    db.commit()
    return {"swapped_with": str(neighbour.id), "rebuild": summary}


# --------------------------------------------------------------------------
# undo (AC3)
# --------------------------------------------------------------------------
def _latest_undoable_edit(db: Session, project_id: str) -> AuditLog | None:
    undone_ids = {
        (row.after or {}).get("original_log_id")
        for row in db.query(AuditLog)
        .filter(AuditLog.project_id == project_id,
                AuditLog.action == "structure_edit_undone")
        .all()
    }
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.project_id == project_id,
                AuditLog.action.in_(EDIT_ACTIONS),
                AuditLog.entity == "structure")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )
    for row in rows:
        if str(row.id) not in undone_ids:
            return row
    return None


def op_undo(db: Session, project: Project) -> dict:
    """Restore the previous state of the last edit operation (AC3)."""
    edit = _latest_undoable_edit(db, str(project.id))
    if edit is None:
        raise EditorError("nessuna modifica da annullare", status_code=404)
    undo = (edit.after or {}).get("undo") or {}
    nodes = undo.get("nodes") or []
    if not nodes:
        raise EditorError("the last edit carries no undo payload",
                          status_code=409)
    # park every ordinal (NULLs never collide under the unique constraint)
    # BEFORE writing the snapshots back: the unique (project_id, ordinal)
    # constraint would otherwise reject the swap (row B must take A's old
    # ordinal while A still holds it)
    all_nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project.id)
        .all()
    )
    for n in all_nodes:
        n.ordinal = None
    db.add_all(all_nodes)
    db.flush()
    restored_ids = []
    for snap in nodes:
        _restore_node(db, snap)
        restored_ids.append(snap["id"])
    # nodes the operation had created vanish again on undo
    for created_id in undo.get("created_node_ids") or []:
        created = db.get(StructureNode, created_id)
        if created is not None:
            db.delete(created)
    db.flush()  # session autoflush=False: drop the deleted row NOW, so the
    # compaction query below cannot return the deleted instance
    # segments moved by a merge go back to their original chapter
    for anchor in undo.get("units") or []:
        unit = db.get(TranslationUnit, anchor["id"])
        if unit is not None:
            unit.chapter_id = anchor["chapter_id"]
            flags = dict(unit.source_flags or {})
            flags["structure_revision"] = \
                flags.get("structure_revision", 0) + 1
            unit.source_flags = flags
            db.add(unit)
    ordinals = _compact_ordinals(db, project.id)
    summary = _rebuild_derived(db, project.id, set(restored_ids))
    db.add(AuditLog(
        project_id=str(project.id),
        action="structure_edit_undone",
        entity="structure",
        entity_id=str(project.id),
        after={"original_log_id": str(edit.id),
               "restored_nodes": len(restored_ids)},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {"undone_action": edit.action, "original_log_id": str(edit.id),
            "restored_nodes": len(restored_ids), "rebuild": summary,
            "ordinals": ordinals}


# --------------------------------------------------------------------------
# selective re-segmentation (§15.1 + §5.1 + §14)
# --------------------------------------------------------------------------
def _range_signature(node: StructureNode) -> tuple:
    return (node.start_page, node.end_page, node.start_char, node.end_char)


def _segment_chapter(db: Session, node: StructureNode,
                     sentences_per_segment: int = 1) -> dict:
    """Re-run the §5.4 segmentation of one chapter, immutably (§5.1).

    Mirrors :func:`backend.chunking_runner.run_chapter_segmentation` but
    takes the node directly and returns a summary instead of writing job
    state.  Approved translation units keep their text, status and id;
    everything else is replaced by the fresh segmentation.
    """
    from .parsing import chunking

    project_id = str(node.project_id)
    node_id = str(node.id)
    page_payloads = chunking_runner._project_page_payloads(db, project_id)
    if not page_payloads:
        raise EditorError(
            "no extracted pages for this project: run the import first "
            "(§5.2)", status_code=409)
    first_document = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    confirmed_keys = chunking_runner._confirmed_header_keys(
        db, page_payloads,
        first_document.id if first_document else None,
    )
    pages = chunking_runner._chapter_pages(page_payloads, node,
                                           confirmed_keys)
    paragraphs = chunking.build_chapter_paragraphs(pages, confirmed_keys)
    units = chunking.segment_chapter_text(paragraphs, sentences_per_segment)
    chunking.with_ids(units, project_id, node_id)

    existing = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id,
                TranslationUnit.chapter_id == node_id)
        .order_by(TranslationUnit.ordinal)
        .all()
    )
    by_ordinal = {u.ordinal: u for u in existing}
    written = kept_approved = 0
    next_n = next_numero_start(db, project_id)
    for unit in units:
        flags = {
            "segment_id": unit["segment_id"],
            "kind": unit["kind"],
            "page": unit.get("page"),
            "has_markup": unit.get("has_markup", False),
        }
        row = by_ordinal.get(unit["ordinal"])
        if row is not None and row.status == "approved":
            kept_approved += 1  # §5.1: le versioni approvate non si toccano
            continue
        if row is None:
            row = TranslationUnit(
                project_id=project_id,
                chapter_id=node_id,
                ordinal=unit["ordinal"],
                id=unit["segment_id"],
                numero=next_n,
            )
            next_n += 1
        else:
            flags["structure_revision"] = \
                flags.get("structure_revision", 0) + 1
        row.source_text = unit["text"]
        row.source_hash = unit["source_hash"]
        row.source_flags = flags
        if row.status == "approved":  # pragma: no cover - defensive
            continue
        if row.status not in ("untranslated", "draft"):
            row.status = "untranslated"
        db.add(row)
        written += 1
    dropped = 0
    for ordinal, row in by_ordinal.items():
        if ordinal > len(units) and row.status != "approved":
            db.delete(row)
            dropped += 1
    return {"segments": len(units), "segments_written": written,
            "segments_kept_approved": kept_approved,
            "segments_dropped": dropped}


def run_resegment(db: Session, job: Job) -> None:  # noqa: F821
    """``resegment_structure`` job: re-segment what the edit touched.

    Idempotent (§14): the segments a chapter produces from the same pages
    are stable (uuid5 IDs), and approved units are never rewritten.  Only
    the chapters whose derived range actually changed in the audited edit
    are re-segmented — every other chapter's segments stay untouched
    (§15.1).
    """
    from .models import Job  # local import: avoids a circular module import

    project_id = job.payload["project_id"]
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")
    node_id = job.payload.get("node_id")
    if node_id is not None:
        node = db.get(StructureNode, str(node_id))
        if node is None:
            raise ValueError("unknown node_id in job payload")
        targets = [node]
    else:
        # selective mode: the chapters the last range edit actually touched
        targets = _chapters_touched_by_last_edit(db, str(project_id))
    results = {}
    for node in targets:
        if node.kind == "scene":
            continue
        results[str(node.id)] = _segment_chapter(
            db, node,
            int(job.payload.get("sentences_per_segment") or 1))
    job.result = {
        **(job.result or {}),
        "resegment": {
            "chapters": results,
            "selected_node_id": str(node_id) if node_id else None,
        },
    }
    db.add(job)
    db.add(AuditLog(
        project_id=str(project_id),
        action="structure_resegmented",
        entity="structure",
        entity_id=str(project_id),
        after={"chapters": sorted(results),
               "segments": {k: v["segments"] for k, v in results.items()}},
        created_at=datetime.utcnow(),
    ))
    db.commit()


def _chapters_touched_by_last_edit(db: Session,
                                   project_id: str) -> list[StructureNode]:
    """Chapters whose *derived range* changed in the last range edit.

    Compares each chapter's current derived signature with the audit
    after-state; empty when the last edit was title-only or everything is
    unchanged (§15.1: only the dependent chapters regenerate).
    """
    edit = (
        db.query(AuditLog)
        .filter(AuditLog.project_id == project_id,
                AuditLog.action.in_(RANGE_ACTIONS),
                AuditLog.entity == "structure")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )
    if edit is None:
        return []
    after_nodes = {
        n["id"]: n for n in ((edit.after or {}).get("nodes") or [])}
    before_nodes = {
        n["id"]: n for n in ((edit.before or {}).get("nodes") or [])}
    changed: set[str] = set()
    for node_id, after in after_nodes.items():
        before = before_nodes.get(node_id)
        before_range = (before or {}).get("start_page")
        if before_range != after.get("start_page") or \
                (before or {}).get("end_page") != after.get("end_page"):
            changed.add(node_id)
        # a node the edit REMOVED (merge/delete): its page span moved into
        # a surviving node — re-segment the survivors whose range grew
    for node_id, before in before_nodes.items():
        if node_id not in after_nodes:
            changed.add(node_id)
    if not changed:
        return []
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    # the removed node's span was absorbed by its page-neighbours: those
    # neighbours' ranges changed too, so re-derive the touched set as
    # "every chapter whose range differs from its audit before-state"
    touched: list[StructureNode] = []
    for node in nodes:
        if node.kind == "scene":
            continue
        before = before_nodes.get(str(node.id))
        if before is not None and (
                before.get("start_page") != node.start_page
                or before.get("end_page") != node.end_page):
            touched.append(node)
        elif before is None and str(node.id) in changed:
            touched.append(node)
    return touched
