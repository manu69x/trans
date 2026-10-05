"""HTML writer for a planned export (PRD §15.4, §11.1).

Builds a self-contained ``.html`` with one ``<h2>`` per chapter, each
paragraph styled by its §5.4.2 kind, italics from ``<i>/<em>`` markup,
and a ``[BOZZA]`` marker on shipped drafts (§13.1). The file is a single
HTML document (no separate items) so it can be previewed in any browser
or printed directly.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from .collector import Exporter
from .errors import ExportError
from .markup import render_inline

log = logging.getLogger(__name__)

_CENTERED_KINDS = {"scene_break", "epigraph"}


def _paragraph(seg) -> str:
    parts: list[str] = []
    for is_italic, txt in render_inline(seg.target_text or seg.source_text or ""):
        esc = html.escape(txt)
        parts.append(f"<em>{esc}</em>" if is_italic else esc)
    body = "".join(parts)
    if (seg.kind or "") in _CENTERED_KINDS:
        body = f'<div class="centred">{body}</div>'
    if seg.is_draft:
        body += '<span class="bozza">[BOZZA]</span>'
    return f"<p>{body}</p>"


def write_html(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """Render the planned segments to ``<book-title>.html`` in ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.html"

    body: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="it">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(exporter.title or 'Untitled')}</title>",
        "<style>",
        "body{font-family:Georgia,serif;max-width:42em;margin:2em auto;line-height:1.6}",
        "h1{text-align:center;font-size:1.8em;margin-top:2em}",
        "h2{margin-top:2em;border-bottom:1px solid #ccc;padding-bottom:.3em}",
        "p{text-indent:1.5em}",
        'div.centred,p.centred{text-indent:0;text-align:center}',
        "em{font-style:italic}",
        '.bozza{color:#800;font-size:.8em;letter-spacing:.05em}',
        ".manifest{font-size:.8em;color:#666;margin-top:3em;border-top:1px solid #ccc;padding-top:1em}",
        "</style>",
        "</head><body>",
        f"<h1>{html.escape(exporter.title or 'Untitled')}</h1>",
    ]

    # Group by chapter, preserving book order.
    chapters: list[tuple[str, list]] = []
    cur: tuple[str, list] | None = None
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if cur is None or cur[0] != title:
            cur = (title, [])
            chapters.append(cur)
        cur[1].append(seg)  # type: ignore[index]

    for title, segs in chapters:
        body.append(f"<h2>{html.escape(title)}</h2>")
        body.extend(_paragraph(s) for s in segs)

    # Manifest block (PRD §15.4: "ogni export conserva manifest").
    manifest = plan.get("manifest")
    if manifest:
        body.append("<div class='manifest'>")
        body.append("<h3>Manifest</h3>")
        body.append(f"<p>Progetto: {html.escape(manifest.get('title',''))} "
                    f"({manifest.get('project_id','')})</p>")
        body.append(f"<p>Versione: {html.escape(manifest.get('version',''))} · "
                    f"Generato: {html.escape(manifest.get('generated_at',''))}</p>")
        body.append(f"<p>Formato: {manifest.get('format','')} · "
                    f"Segmenti: {manifest.get('counts',{}).get('total_segments',0)} "
                    f"({manifest.get('counts',{}).get('approved',0)} approvati, "
                    f"{manifest.get('counts',{}).get('drafts',0)} bozze)</p>")
        if manifest.get("models"):
            body.append(f"<p>Modelli: {html.escape(', '.join(manifest['models']))}</p>")
        body.append("</div>")

    body.append("</body></html>")

    try:
        out_path.write_text("\n".join(body), encoding="utf-8")
    except Exception as exc:
        raise ExportError(f"html writer failed: {exc}") from exc

    log.info("html written book=%s segments=%d path=%s",
             exporter.title, len(plan["selected"]), out_path)
    return out_path
