"""Load exportable segments from the database (PRD §15.4, §6.5, §7.2).

:class:`Exporter` is a thin, testable read layer: given a project id it loads
the book's structure and every :class:`ExportSegment` (one per translation
unit), in book order, and it enforces the §15.4 gating rules on :meth:`plan`.

A :class:`ExportSegment` is the format-agnostic view of one translation unit:

* ``segment_id`` / ``ordinal`` -- CAT identity and in-chapter order.
* ``chapter_id`` / ``chapter_title`` / ``chapter_ordinal`` -- the parent
  :class:`~backend.models.StructureNode` (the unit's ``chapter_id`` is a real
  ``StructureNode.id`` UUID, so chapter ordering uses the node's ``ordinal``).
* ``kind`` -- the §5.4.2 paragraph kind (``dialogue`` keeps its quotes,
  ``scene_break`` a break, ``epigraph``/``letter`` their styling).
* ``source_text`` / ``target_text`` -- EN source and IT target.
* ``status`` -- ``approved`` (ship as-is) or a draft (bozza, §15.4).
* ``has_markup`` -- the source carried markup that must survive.
* ``page`` -- the source page, for the docx footer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models import Project, StructureNode, TranslationUnit
from .errors import LoadError, NoApprovedSegments, WatermarkRequired

# Book-order sentinel: segments not tied to a chapter sort after all chapters.
_UNCATEGORIZED = 1 << 30


@dataclass
class ExportSegment:
    """One translation unit, ready to be rendered (PRD §6.5/§5.4)."""

    segment_id: str
    ordinal: int
    source_text: str
    target_text: str | None
    status: str
    kind: str | None = None
    page: int | None = None
    has_markup: bool = False
    chapter_id: str | None = None
    chapter_title: str | None = None
    chapter_ordinal: int = _UNCATEGORIZED
    # §15.4 / §13.1: set when the segment is a draft that will be shipped.
    is_draft: bool = False

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"


class Exporter:
    """Reads a project's structure + segments and plans an export.

    The DB session is consumed by :meth:`load`; after that the :class:`Exporter`
    holds plain data and is safe to use from any thread.
    """

    def __init__(
        self,
        *,
        project_id: str,
        title: str,
        source_language: str,
        target_language: str,
        genre_profile: str,
        status: str,
        created_at: datetime | None,
        translation_model_id: str | None,
        text_model_id: str | None,
        nodes: list[dict],
        segments: list[ExportSegment],
    ) -> None:
        self.project_id = project_id
        self.title = title
        self.source_language = source_language
        self.target_language = target_language
        self.genre_profile = genre_profile
        self.status = status
        self.created_at = created_at
        self.translation_model_id = translation_model_id
        self.text_model_id = text_model_id
        self.nodes = nodes
        self.segments = segments

    # --- introspection -------------------------------------------------
    def counts(self) -> dict:
        """Counts verifiable against the DB (PRD §15.4 manifest / AC3)."""
        total = len(self.segments)
        approved = sum(1 for s in self.segments if s.is_approved)
        drafts = total - approved
        with_target = sum(1 for s in self.segments if s.target_text)
        return {
            "total_segments": total,
            "approved": approved,
            "drafts": drafts,
            "with_target": with_target,
        }

    @property
    def ordered_segments(self) -> list[ExportSegment]:
        """Segments in book order: chapter ordinal, then segment ordinal."""
        return sorted(
            self.segments,
            key=lambda s: (s.chapter_ordinal, s.ordinal, s.segment_id),
        )

    def chapter_titles(self) -> list[str]:
        """The book's chapters in order (front/back-matter excluded)."""
        titles: list[str] = []
        for node in sorted(self.nodes, key=lambda n: n.get("ordinal") or 0):
            kind = node.get("kind")
            title = node.get("normalized_title")
            if kind == "chapter" and title:
                titles.append(title)
            elif kind == "part" and title:
                titles.append(title.upper())
        return titles

    # --- §15.4 gating --------------------------------------------------
    def plan(
        self,
        *,
        include_drafts: bool = False,
        watermark: bool = False,
    ) -> dict:
        """Validate the requested export and return the effective plan.

        Implements §15.4: export *only* approved segments, or include drafts
        *only* with an explicit watermark.

        * ``include_drafts=False`` and the project has **no** approved
          segment -> :class:`~.errors.NoApprovedSegments` (blocked).
        * ``include_drafts=True`` and any draft would be shipped without
          ``watermark=True`` -> :class:`~.errors.WatermarkRequired` (the
          watermark is forced, §13.1).
        * otherwise the selection is the approved segments, or all segments
          when drafts are included (watermarked).
        """
        counts = self.counts()
        has_approved = counts["approved"] > 0

        if include_drafts:
            selected = list(self.ordered_segments)
            ships_a_draft = any(not s.is_approved for s in selected)
            if ships_a_draft and not watermark:
                raise WatermarkRequired(
                    "bozze (drafts) can only be exported with an explicit "
                    "watermark (§15.4 / §13.1)"
                )
        else:
            if not has_approved:
                raise NoApprovedSegments(
                    "no approved segments: set include_drafts=True with "
                    "watermark=True to export the drafts (§15.4)"
                )
            selected = [s for s in self.ordered_segments if s.is_approved]

        for seg in selected:
            seg.is_draft = not seg.is_approved

        return {
            "include_drafts": include_drafts,
            "watermark": watermark,
            "selected": selected,
            "counts": counts,
        }


def _dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def build_exporter(db, project_id: str) -> Exporter:
    """Load and return an :class:`Exporter` for ``project_id``.

    Raises :class:`~.errors.LoadError` when the project is missing or has no
    segments (nothing to export).
    """
    project = db.get(Project, project_id)
    if project is None:
        raise LoadError(f"project {project_id} not found")

    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .all()
    )
    # Map chapter node id (UUID) -> (ordinal, normalized_title).
    node_by_id: dict[str, tuple[int, str | None]] = {}
    node_payloads: list[dict] = []
    for node in nodes:
        key = str(node.id)
        node_by_id[key] = (node.ordinal or _UNCATEGORIZED, node.normalized_title)
        node_payloads.append(
            {
                "id": key,
                "ordinal": node.ordinal,
                "kind": node.kind,
                "normalized_title": node.normalized_title,
                "source_label": node.source_label,
                "start_page": node.start_page,
            }
        )

    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id)
        .order_by(TranslationUnit.chapter_id, TranslationUnit.ordinal)
        .all()
    )
    if not units:
        raise LoadError(
            f"project {project_id} has no segments: segment a chapter first"
        )

    segments: list[ExportSegment] = []
    for u in units:
        flags = u.source_flags or {}
        cid = str(u.chapter_id) if u.chapter_id is not None else None
        chap_ordinal = _UNCATEGORIZED
        chap_title = None
        if cid is not None and cid in node_by_id:
            chap_ordinal, chap_title = node_by_id[cid]
        segments.append(
            ExportSegment(
                segment_id=str(u.id),
                ordinal=u.ordinal,
                source_text=u.source_text,
                target_text=u.target_text,
                status=u.status,
                kind=flags.get("kind"),
                page=flags.get("page"),
                has_markup=bool(flags.get("has_markup")),
                chapter_id=cid,
                chapter_title=chap_title,
                chapter_ordinal=chap_ordinal,
            )
        )

    return Exporter(
        project_id=project_id,
        title=project.title,
        source_language=project.source_language,
        target_language=project.target_language,
        genre_profile=project.genre_profile,
        status=project.status,
        created_at=_dt(project.created_at),
        translation_model_id=project.translation_model_id,
        text_model_id=project.text_model_id,
        nodes=node_payloads,
        segments=segments,
    )
