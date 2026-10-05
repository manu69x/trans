"""Anteprima renderizzata del libro tradotto (scheda "Anteprima", 2026-10-01).

Riusa il §15.4 collector dell'export (stessa selezione approved/bozze,
stesso ordine capitoli -> segmenti) e lo serve PAGINATO PER CAPITOLO: la
UI sfoglia capitolo per capitolo (frecce/indice) invece di ricevere 808
segmenti in un colpo. Le opzioni specchiano l'export (include_drafts +
watermark), così l'anteprima mostra esattamente quello che partirà nel
file; gli excerpt sono troncati a 400 caratteri e il conteggio pagine del
PDF è una STIMA (3400 caratteri/pagina A5) dichiarata come tale.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from .export import NoApprovedSegments, WatermarkRequired
from .export.collector import Exporter, build_exporter
from .models import Project

#: Caratteri visibili per segmento nell'anteprima (testo ridotto, §13.1).
EXCERPT_CHARS = 400

#: Stima caratteri per pagina A5 con il layout del writer PDF (fpdf2, 10pt).
ESTIMATED_CHARS_PER_PAGE = 3400

#: Caratteri di TESTO ITALIANO per pagina nel viewer bilingue (colonna
#: sinistra): A5 tascabile a schermo. La paginazione segue il target IT; la
#: colonna EN mostra gli stessi segmenti (flussi indipendente).
BOOK_PAGE_CHARS = 1800

_TAG_RE = re.compile(r"<[^>]+>")


def _excerpt(text: str | None) -> str:
    t = _TAG_RE.sub("", text or "")
    if len(t) > EXCERPT_CHARS:
        return t[: EXCERPT_CHARS - 1].rstrip() + "…"
    return t


def _strip_markup_len(text: str | None) -> int:
    return len(_TAG_RE.sub("", text or ""))


def _excerpt_keep_all(text: str | None) -> str:
    """Testo completo senza markup (il viewer bilingue mostra tutto)."""
    return _TAG_RE.sub("", text or "")


def build_book_outline(exporter: Exporter, plan: dict) -> dict[str, Any]:
    """Indice del libro + stime globali, dal piano §15.4."""
    chapters: list[tuple[str, list]] = []
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if not chapters or chapters[-1][0] != title:
            chapters.append((title, []))
        chapters[-1][1].append(seg)

    items: list[dict[str, Any]] = []
    total_chars = 0
    for i, (title, segs) in enumerate(chapters):
        chars = sum(_strip_markup_len(s.target_text or s.source_text)
                    for s in segs)
        total_chars += chars
        items.append({
            "index": i,
            "title": title,
            "segments": len(segs),
            "chars": chars,
            "est_pages": max(1, round(chars / ESTIMATED_CHARS_PER_PAGE)),
        })
    return {
        "chapters": items,
        "total_chapters": len(chapters),
        "total_segments": len(plan["selected"]),
        "total_chars": total_chars,
        "est_pages": max(1, round(total_chars / ESTIMATED_CHARS_PER_PAGE)),
    }


def preview_chapter(
    db: Session,
    project_id: str,
    *,
    include_drafts: bool = False,
    watermark: bool = False,
    chapter_index: int,
) -> dict[str, Any]:
    """Un capitolo renderizzato in anteprima, con indice di navigazione.

    L'exporter è ricaricato a ogni chiamata (read-only, ~1s su 808 segmenti,
    sotto il timeout del rewrite Next ~30s).
    """
    project = db.get(Project, project_id)
    if project is None:
        raise KeyError("project not found")

    exporter: Exporter = build_exporter(db, project_id)
    try:
        plan = exporter.plan(
            include_drafts=include_drafts, watermark=watermark)
    except NoApprovedSegments as exc:
        return {"error": "no_approved_segments", "message": str(exc)}
    except WatermarkRequired as exc:
        return {"error": "watermark_required", "message": str(exc)}

    selected = plan["selected"]
    chapters: list[tuple[str, list]] = []
    for seg in selected:
        title = seg.chapter_title or "Untitled"
        if not chapters or chapters[-1][0] != title:
            chapters.append((title, []))
        chapters[-1][1].append(seg)

    total_chapters = len(chapters)
    if total_chapters == 0:
        return {"error": "empty",
                "message": "nessun segmento selezionato dall'export"}

    idx = max(0, min(chapter_index, total_chapters - 1))
    title, segs = chapters[idx]

    paragraphs = [
        {
            "segment_id": s.segment_id,
            "ordinal": s.ordinal,
            "kind": s.kind,
            "status": s.status,
            "is_draft": s.is_draft,
            "source_excerpt": _excerpt(s.source_text),
            "target_excerpt": _excerpt(s.target_text),
            "chars": _strip_markup_len(s.target_text or s.source_text),
        }
        for s in segs
    ]
    return {
        "project_id": str(project_id),
        "include_drafts": include_drafts,
        "watermark": watermark,
        "chapter_index": idx,
        "total_chapters": total_chapters,
        "chapter": {
            "title": title,
            "segments": len(segs),
            "est_pages": max(1, round(sum(p["chars"] for p in paragraphs)
                                     / ESTIMATED_CHARS_PER_PAGE)),
        },
        "paragraphs": paragraphs,
    }


def build_book_pages(
    db: Session,
    project_id: str,
    *,
    include_drafts: bool = False,
    watermark: bool = False,
    page_size: int = BOOK_PAGE_CHARS,
) -> dict[str, Any]:
    """Tutto il libro impaginato per il viewer bilingue (spread IT | EN).

    La paginazione segue la colonna ITALIANA (il testo che verrà esportato):
    i segmenti si accumulano finché la pagina raggiunge ``page_size``
    caratteri; un segmento non viene MAI spezzato (una pagina termina su un
    confine di segmento, come nel writer PDF). Ogni pagina porta l'intervallo
    ``segment_index`` (inizio inclusa, fine esclusa) nel flusso §15.4: la UI
    allinea la colonna inglese sullo stesso intervallo.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise KeyError("project not found")

    exporter: Exporter = build_exporter(db, project_id)
    try:
        plan = exporter.plan(
            include_drafts=include_drafts, watermark=watermark)
    except NoApprovedSegments as exc:
        return {"error": "no_approved_segments", "message": str(exc)}
    except WatermarkRequired as exc:
        return {"error": "watermark_required", "message": str(exc)}

    # Flusso lineare di segmenti con i confini di capitolo marcati.
    flow: list[dict[str, Any]] = []
    for seg in plan["selected"]:
        flow.append({
            "segment_id": seg.segment_id,
            "chapter_title": seg.chapter_title or "Untitled",
            "new_chapter": bool(flow and (flow[-1]["chapter_title"]
                                          != (seg.chapter_title or "Untitled"))),
            "is_first": len(flow) == 0,
            "kind": seg.kind,
            "is_draft": seg.is_draft,
            "target": _excerpt_keep_all(seg.target_text),
            "source": _excerpt_keep_all(seg.source_text),
            "chars": _strip_markup_len(seg.target_text or seg.source_text),
        })
    if flow:
        flow[0]["is_first"] = True

    pages: list[dict[str, Any]] = []
    cur: list[int] = []
    cur_chars = 0
    for i, item in enumerate(flow):
        opens_chapter = item["new_chapter"] or item["is_first"]
        # Capitolo nuovo => pagina nuova (come nel libro stampato), a meno
        # che la pagina corrente sia vuota.
        if cur and (opens_chapter or cur_chars >= page_size):
            pages.append({"segments": cur, "chars": cur_chars})
            cur = []
            cur_chars = 0
        cur.append(i)
        cur_chars += item["chars"]
    if cur:
        pages.append({"segments": cur, "chars": cur_chars})

    out_pages: list[dict[str, Any]] = []
    for n, pg in enumerate(pages):
        first = flow[pg["segments"][0]]
        out_pages.append({
            "number": n + 1,
            "chapter_title": first["chapter_title"],
            "chapter_start": bool(first["new_chapter"] or first["is_first"]),
            "segment_start": pg["segments"][0],
            "segment_end": pg["segments"][-1] + 1,
            "chars": pg["chars"],
        })

    return {
        "project_id": str(project_id),
        "project_title": project.title,
        "include_drafts": include_drafts,
        "watermark": watermark,
        "page_size_chars": page_size,
        "total_pages": len(out_pages),
        "total_segments": len(flow),
        "pages": out_pages,
        # Il flusso completo VA CON LE PAGINE: la UI ne estrae gli slice
        # [segment_start:segment_end] per ogni pagina. ~2.5MB JSON per
        # 808 segmenti: accettabile in locale (§13.1), una sola richiesta.
        "flow": flow,
    }
