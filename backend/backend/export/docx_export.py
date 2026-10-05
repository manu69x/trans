"""DOCX writer for a planned export (PRD §15.4 AC1, §11.1).

Builds a ``.docx`` with one chapter per book chapter (``Heading 1``), each
paragraph styled by its §5.4.2 kind, a scene-break ``— — —`` line, and a
running page number in the footer. Drafts (bozze, §13.1) are flagged with a
``[BOZZA]`` watermark when the plan requested one.
"""
from __future__ import annotations

import logging
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor

from .collector import Exporter
from .errors import ExportError
from .markup import render_inline

log = logging.getLogger(__name__)

# §5.4.2 paragraph kinds -> paragraph alignment.
_KIND_ALIGNMENT: dict[str, str] = {
    "dialogue": "left",
    "narration": "left",
    "scene_break": "center",
    "letter": "left",
    "epigraph": "center",
    "front_matter": "left",
    "back_matter": "left",
}


def _add_page_number(footer) -> None:
    """Insert a centered ``PAGE`` field into the footer."""
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    paragraph._p.append(fld)


def write_docx(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """Render the planned segments to ``<book-title>.docx`` in ``out_dir``.

    Returns the written path. Raises :class:`~.errors.ExportError` on a
    writer failure so the route can surface a clean 502.
    """
    doc = Document()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.docx"

    watermark = plan.get("watermark", False)
    current_chapter: str | None = None

    try:
        for seg in plan["selected"]:
            chapter = seg.chapter_title
            if chapter and chapter != current_chapter:
                doc.add_heading(chapter, level=1)
                current_chapter = chapter

            kind = seg.kind or "narration"
            text = seg.target_text or seg.source_text or ""
            if not text:
                continue

            para = doc.add_paragraph()
            if _KIND_ALIGNMENT.get(kind, "left") == "center":
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            # §15.4 AC1: keep italics from <i>/<em> markup.
            for is_italic, run_text in render_inline(text):
                run = para.add_run(run_text)
                if is_italic:
                    run.italic = True

            # §13.1: watermark any shipped draft.
            if seg.is_draft and watermark:
                run = para.add_run("  [BOZZA]")
                run.italic = True
                run.font.color.rgb = RGBColor(0x80, 0x00, 0x00)

        _add_page_number(doc.sections[0].footer)
        doc.save(out_path)
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"docx writer failed: {exc}") from exc

    log.info(
        "docx written book=%s segments=%d path=%s",
        exporter.title,
        len(plan["selected"]),
        out_path,
    )
    return out_path
