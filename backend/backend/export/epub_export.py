"""EPUB writer for a planned export (PRD §15.4 AC1, §11.1).

Builds a ``.epub`` with one XHTML item per book chapter. Italics from
``<i>/<em>`` markup are preserved as ``<em>`` elements; a ``[BOZZA]`` span
watermarks any shipped draft (§13.1). The book title page is the first item.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from ebooklib import epub

from .collector import Exporter
from .errors import ExportError
from .markup import render_inline

log = logging.getLogger(__name__)

# §5.4.2 kinds that get a centred style class.
_CENTERED_KINDS = {"scene_break", "epigraph", "back_matter", "front_matter"}


def _render_paragraph(seg) -> str:
    """One target paragraph as an HTML ``<p>`` with preserved italics."""
    parts: list[str] = []
    for is_italic, run_text in render_inline(seg.target_text or seg.source_text or ""):
        esc = html.escape(run_text)
        if is_italic:
            parts.append(f"<em>{esc}</em>")
        else:
            parts.append(esc)
    body = "".join(parts)
    cls = " class='centred'" if (seg.kind or "") in _CENTERED_KINDS else ""
    out = f"<p{cls}>{body}"
    if seg.is_draft:
        out += "<span class='draft'>[BOZZA]</span>"
    return out + "</p>"


def _render_chapter(exporter: Exporter, segs: list, title: str, slug: str) -> epub.EpubHtml:
    item = epub.EpubHtml(title=title, file_name=f"chapter-{slug}.xhtml", lang=exporter.target_language)
    body = [f"<h1>{html.escape(title)}</h1>"]
    body.extend(_render_paragraph(s) for s in segs)
    item.content = (
        "<html><head>"
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        "</head><body>"
        + "".join(body)
        + "</body></html>"
    )
    return item


def write_epub(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """Render the planned segments to ``<book-title>.epub`` in ``out_dir``.

    Returns the written path. Raises :class:`~.errors.ExportError` on a
    writer failure so the route can surface a clean 502.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.epub"

    book = epub.EpubBook()
    book.set_identifier(exporter.project_id)
    book.set_title(exporter.title or "Untitled")
    book.set_language(exporter.target_language)
    book.add_author("Trans")

    # Group selected segments by chapter, preserving book order.
    chapters: list[tuple[str, list]] = []
    current: tuple[str, list] | None = None
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if current is None or current[0] != title:
            current = (title, [])
            chapters.append(current)
        current[1].append(seg)  # type: ignore[index]

    # Title page first, then chapters, with a toc.
    title_item = epub.EpubHtml(
        title="Title", file_name="title.xhtml", lang=exporter.target_language
    )
    title_item.content = (
        "<html><head>"
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        "</head><body>"
        f"<h1>{html.escape(exporter.title or 'Untitled')}</h1>"
        f"<p>{html.escape(exporter.source_language)} → {html.escape(exporter.target_language)}</p>"
        "</body></html>"
    )
    book.add_item(title_item)

    toc: list = [title_item]
    for slug, (title, segs) in enumerate(chapters, start=1):
        item = _render_chapter(exporter, segs, title, str(slug))
        book.add_item(item)
        toc.append(item)

    # EPUB 3 richiede il nav.xhtml (toc con property="nav") e l'EPUB 2
    # il toc.ncx: senza questi due item i lettori rifiutano il file.
    # SENZA book.spine settato ebooklib scrive una spine VUOTA (bug
    # riscontrato 2026-10-01: content.opf con <spine toc="ncx"/> e nessun
    # itemref -> nessun lettore riesce ad aprire il libro).
    nav = epub.EpubNav()
    ncx = epub.EpubNcx()
    book.add_item(nav)
    book.add_item(ncx)

    # CSS base: nero su bianco, serif, rientri (i lettori possono sempre
    # sovrascriverlo con i loro stili).
    style = epub.EpubItem(
        uid="css",
        file_name="style.css",
        media_type="text/css",
        content=(
            "body{font-family:'Georgia',serif;line-height:1.6;margin:5%}"
            "h1{text-align:center;font-size:1.6em;margin:1.5em 0}"
            "p{text-indent:1.5em;margin:0}"
            "p.centred{text-indent:0;text-align:center}"
            "em{font-style:italic}"
            ".draft{color:#800;font-size:.8em;letter-spacing:.05em}"
        ).encode("utf-8"),
    )
    book.add_item(style)

    # Spine: titolo + capitoli + nav (l'ordine è quello del libro).
    book.spine = ["nav", title_item] + toc[1:]

    book.toc = toc

    try:
        epub.write_epub(out_path, book, {})
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"epub writer failed: {exc}") from exc

    log.info(
        "epub written book=%s segments=%d path=%s",
        exporter.title,
        len(plan["selected"]),
        out_path,
    )
    return out_path
