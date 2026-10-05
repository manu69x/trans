"""PDF writer for a planned export (PRD §15.4, §11.1).

Builds a print-ready ``.pdf`` with one section per chapter, the same
paragraph styling of the HTML writer (centred kinds, italics from
``<i>/<em>`` markup) and a ``[BOZZA]`` watermark on shipped drafts (§13.1).
Unicode text (à è é « » …) is served by the bundled DejaVu fonts: the
fpdf2 core fonts are Latin-1 only and would mangle Italian typography.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fpdf import FPDF

from .collector import Exporter
from .errors import ExportError
from .markup import render_inline

log = logging.getLogger(__name__)

_CENTERED_KINDS = {"scene_break", "epigraph", "back_matter", "front_matter"}

_FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class _BookPDF(FPDF):
    """A5-book-like page with an optional running footer.

    Il piedipagina (titolo · numero pagina) si disattiva con
    ``show_page_numbers = False`` (flag `page_numbers` dell'export,
    2026-10-01): fpdf2 chiama ``footer()`` a ogni add_page, quindi il
    flag viene letto dinamicamente dal piano tramite la closure ``pdf.plan``.
    """

    show_page_numbers: bool = True
    book_title: str = "Trans"

    def footer(self) -> None:
        if not self.show_page_numbers:
            return
        self.set_y(-15)
        with self.local_context():
            self.set_font("DejaVu", "", 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 10, f"{self.book_title}  ·  {self.page_no()}", align="C")


def _runs(text: str) -> list[tuple[bool, str]]:
    """The (italic, text) runs of a paragraph, markup-aware."""
    return list(render_inline(text or ""))


def write_pdf(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """Render the planned segments to ``<book-title>.pdf`` in ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.pdf"

    pdf = _BookPDF(orientation="P", unit="mm", format="A5")
    pdf.book_title = exporter.title or "Trans"
    # Flag `page_numbers` (default True): footer con titolo + numero pagina.
    pdf.show_page_numbers = bool(plan.get("page_numbers", True))
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=20)

    regular = _FONTS_DIR / "DejaVuSerif.ttf"
    bold = _FONTS_DIR / "DejaVuSerif-Bold.ttf"
    try:
        pdf.add_font("DejaVu", "", str(regular))
        pdf.add_font("DejaVu", "B", str(bold))
    except Exception as exc:  # pragma: no cover - font packaging failure
        raise ExportError(f"pdf fonts missing: {exc}") from exc

    # --- title page ---
    pdf.add_page()
    pdf.set_y(70)
    with pdf.local_context():
        pdf.set_font("DejaVu", "B", 26)
        pdf.cell(0, 14, exporter.title or "Untitled", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font("DejaVu", "", 11)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(0, 8, f"{exporter.source_language} → {exporter.target_language}",
                 align="C", new_x="LMARGIN", new_y="NEXT")

    # --- chapters: group selected segments by chapter title ---
    chapters: list[tuple[str, list]] = []
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if not chapters or chapters[-1][0] != title:
            chapters.append((title, []))
        chapters[-1][1].append(seg)

    for title, segs in chapters:
        pdf.add_page()
        with pdf.local_context():
            pdf.set_font("DejaVu", "B", 17)
            pdf.multi_cell(0, 10, title, align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(6)
        for seg in segs:
            centered = (seg.kind or "") in _CENTERED_KINDS
            if centered:
                pdf.ln(3)
            for is_italic, run_text in _runs(seg.target_text or seg.source_text or ""):
                style = "I" if is_italic else ""
                with pdf.local_context():
                    try:
                        pdf.set_font("DejaVu", style, 10)
                    except Exception:
                        pdf.set_font("DejaVu", "", 10)
                    pdf.multi_cell(
                        0, 5.6, run_text,
                        align="C" if centered else "J",
                        new_x="LMARGIN", new_y="NEXT",
                    )
            if seg.is_draft:
                with pdf.local_context():
                    pdf.set_font("DejaVu", "", 8)
                    pdf.set_text_color(160, 30, 30)
                    pdf.multi_cell(0, 4.5, "[BOZZA]", align="L",
                                   new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2.6)

    try:
        pdf.output(str(out_path))
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"pdf writer failed: {exc}") from exc

    log.info(
        "pdf written book=%s segments=%d path=%s",
        exporter.title,
        len(plan["selected"]),
        out_path,
    )
    return out_path
