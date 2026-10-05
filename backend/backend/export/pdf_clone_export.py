"""PDF writer "Con clone struttura" v3 (2026-10-02) — impaginazione uniforme.

Sospende il per-segment style mapping (l'allineamento/font per segmento
estratto dall'originale era inaffidabile: "funziona malissimo"). Regole:

* **uniformità**: tutto il corpo del libro usa lo STESSO font (nome generico
  DejaVu, dimensione = corpo dell'originale) e lo stesso leading;
* **distribuzione uniforme sulla larghezza**: paragrafi giustificati;
* **rientro prima riga** per facilitare la lettura (tranne dopo un titolo e
  per i blocchi centrati come epigrafi e pause di scena);
* **mai testo sopra le immagini**: le immagini del capitolo sono stampate
  in un blocco DEDICATO in testa al capitolo e il flusso testo riparte
  SOTTO l'immagine (il cursore y avanza dopo il piazzamento);
* le pagine frontal (copertina ecc.) restano immagine a tutta pagina;
* rapporto pagine ~1:1 via distribuzione dei segmenti per pagina.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from fpdf import FPDF

from .collector import Exporter
from .errors import ExportError
from .markup import render_inline
from .. import typography as _typo

log = logging.getLogger(__name__)

_PT_TO_MM = 25.4 / 72.0

_CENTERED_KINDS = {"scene_break", "epigraph", "back_matter", "front_matter"}


class _ClonePDF(FPDF):
    book_title: str = "Trans"

    def footer(self) -> None:
        # Il clone per la verifica non ha MAI la numerazione (2026-10-02).
        return


def _visible_on_page(img: dict, page_h: float | None) -> bool:
    """False per le immagini il cui bbox e' (quasi) interamente fuori pagina
    (ombre del PDF originale: bbox con y negativo, es. pag.3)."""
    bbox = img.get("bbox")
    if not bbox or len(bbox) != 4 or not page_h:
        return True
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if y1 <= 2 or y0 >= page_h - 2:
        return False
    page_w = float(img.get("_page_width") or 1e9)
    if x1 <= 2 or x0 >= page_w - 2:
        return False
    visible_h = min(y1, page_h) - max(y0, 0)
    if visible_h <= 0.15 * (y1 - y0):
        return False
    return True


def _image_mm(img: dict) -> tuple[float, float]:
    """Dimensioni ORIGINALI in mm: bbox in pt se nota, altrimenti px@96dpi."""
    bbox = img.get("bbox")
    if bbox and len(bbox) == 4:
        w = (float(bbox[2]) - float(bbox[0])) * _PT_TO_MM
        h = (float(bbox[3]) - float(bbox[1])) * _PT_TO_MM
        return max(5.0, w), max(5.0, h)
    w_px = float(img.get("width") or 600)
    h_px = float(img.get("height") or 400)
    return w_px * 25.4 / 96.0, h_px * 25.4 / 96.0


def _place_image_block(pdf: FPDF, data: bytes, w_mm: float, h_mm: float,
                       *, max_h: float = 120.0) -> bool:
    """Stampa l'immagine DENTRO il flusso: centrata, proporzioni originali,
    ridotta solo se non entra; avanza il cursore y SOTTO l'immagine cosi'
    il testo successivo non la sovrappone mai."""
    avail_w = pdf.w - 2 * pdf.l_margin
    rest_h = pdf.h - pdf.b_margin - pdf.get_y()
    if rest_h < min(40.0, max_h):
        pdf.add_page()
    w, h = w_mm, h_mm
    if w > avail_w:
        h = h * avail_w / w
        w = avail_w
    if h > max_h:
        w = w * max_h / h
        h = max_h
    try:
        pdf.image(io.BytesIO(data), x=(pdf.w - w) / 2, w=w, h=h)
        pdf.set_y(pdf.get_y() + h + 3)
        return True
    except Exception:  # noqa: BLE001 - formato non supportato
        return False


def write_pdf_clone(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """Clone del libro tradotto: impaginazione uniforme e leggibile."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}_clone.pdf"

    profile: dict = plan.get("typography") or {}
    seg_pages: list[dict] = plan.get("segment_pages") or []
    style_by_id = {s["segment_id"]: s for s in seg_pages}
    ratio = max(0.5, min(2.0, float(plan.get("page_ratio") or 1.0)))

    orig_w = float(profile.get("page_width") or 419.5)
    orig_h = float(profile.get("page_height") or 595.3)
    page_w_mm = orig_w * _PT_TO_MM
    page_h_mm = orig_h * _PT_TO_MM

    # Font del corpo: dimensione fissa = corpo dell'originale.
    body_size = int(max(8, min(20, float(profile.get("body_size") or 12))))
    body_leading_pt = float(profile.get("body_leading") or 0) or body_size * 1.2
    body_cell_mm = body_leading_pt * _PT_TO_MM

    pdf = _ClonePDF(orientation="P", unit="mm",
                    format=(page_w_mm, page_h_mm))
    pdf.book_title = exporter.title or "Trans"
    pdf.show_page_numbers = False
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=16)
    fonts_dir = Path(__file__).resolve().parent / "fonts"
    try:
        pdf.add_font("DejaVu", "", str(fonts_dir / "DejaVuSerif.ttf"))
        pdf.add_font("DejaVu", "B", str(fonts_dir / "DejaVuSerif-Bold.ttf"))
    except Exception as exc:  # pragma: no cover
        raise ExportError(f"pdf fonts missing: {exc}") from exc

    # --- capitoli + immagini per capitolo ---------------------------------
    chapters: list[dict] = []
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if not chapters or chapters[-1]["title"] != title:
            info = style_by_id.get(seg.segment_id) or {}
            pages = info.get("pages") or []
            chapters.append({
                "title": title, "segs": [],
                "orig_pages": len(pages),
                "images": [],
            })
        ch = chapters[-1]
        ch["segs"].append(seg)
        info = style_by_id.get(seg.segment_id) or {}
        for pno in (info.get("pages") or []):
            for img in (profile.get("images") or []):
                if (img["page"] == pno
                        and _visible_on_page(img, orig_h)
                        and all(i["image_id"] != img["image_id"]
                                for i in ch["images"])):
                    ch["images"].append(img)

    for img in (profile.get("images") or []):
        img["_page_width"] = orig_w

    chapter_pages: set[int] = set()
    for s in seg_pages:
        chapter_pages.update(s.get("pages") or [])

    # --- pagine frontal: copertina ecc. (immagine sola, tutta pagina) -----
    front_images = sorted(
        (img for img in (profile.get("images") or [])
         if img["page"] not in chapter_pages
         and _visible_on_page(img, orig_h)),
        key=lambda i: (i["page"], i["image_id"]),
    )
    for img in front_images:
        data = _typo.get_image(str(exporter.project_id),
                               img["image_id"], img["ext"])
        if not data:
            continue
        pdf.add_page()
        w_mm, h_mm = _image_mm(img)
        bbox = img.get("bbox")
        if bbox and len(bbox) == 4:
            x = float(bbox[0]) * _PT_TO_MM
            y = float(bbox[1]) * _PT_TO_MM
            w = min(float(bbox[2] - bbox[0]) * _PT_TO_MM, pdf.w - x)
            h = min(float(bbox[3] - bbox[1]) * _PT_TO_MM, pdf.h - y)
            try:
                pdf.image(io.BytesIO(data), x=max(0.0, x), y=max(0.0, y),
                          w=max(2.0, w), h=max(2.0, h))
                continue
            except Exception:  # noqa: BLE001
                pass
        _place_image_block(pdf, data, w_mm, h_mm)

    # --- capitoli ---------------------------------------------------------
    for ch in chapters:
        pdf.add_page()
        n_segs = len(ch["segs"])
        total_chars = sum(len(s.target_text or s.source_text or "")
                          for s in ch["segs"])
        if ch["orig_pages"]:
            target_pages = max(1, round(ch["orig_pages"] / ratio))
        else:
            target_pages = max(1, round(total_chars / 1800.0))
        segs_per_page = max(1, -(-n_segs // target_pages))

        # titolo di capitolo
        with pdf.local_context():
            pdf.set_font("DejaVu", "B", int(body_size + 3))
            pdf.multi_cell(0, body_cell_mm * 1.3, ch["title"], align="C",
                           new_x="LMARGIN", new_y="NEXT")
            pdf.ln(5)

        # blocco immagini DEDICATO: il cursore y avanza sotto l'immagine,
        # quindi il testo del capitolo non potra' mai sovrapporvisi.
        for img in ch["images"][:4]:
            data = _typo.get_image(str(exporter.project_id),
                                   img["image_id"], img["ext"])
            if not data:
                continue
            w_mm, h_mm = _image_mm(img)
            _place_image_block(pdf, data, w_mm, h_mm)

        # corpo del capitolo: UNIFORME — giustificato, rientro prima riga
        # (text_columns.paragraph(first_line_indent) di fpdf2)
        new_paragraph = True  # la prima riga dopo il titolo non ha rientro
        for i, seg in enumerate(ch["segs"]):
            if i and i % segs_per_page == 0:
                pdf.add_page()
                new_paragraph = True
            centered = (seg.kind or "") in _CENTERED_KINDS
            if centered:
                for is_italic, run_text in render_inline(
                        seg.target_text or seg.source_text or ""):
                    with pdf.local_context():
                        pdf.set_font("DejaVu", "I" if is_italic else "",
                                     body_size)
                        pdf.multi_cell(0, body_cell_mm, run_text, align="C",
                                       new_x="LMARGIN", new_y="NEXT")
            else:
                indent = body_size * _PT_TO_MM if new_paragraph else 0.0
                pdf.set_font("DejaVu", "", body_size)
                with pdf.text_columns(text_align="J", ncols=1) as cols:
                    with cols.paragraph(
                            first_line_indent=indent,
                            line_height=body_leading_pt / body_size) as par:
                        for is_italic, run_text in render_inline(
                                seg.target_text or seg.source_text or ""):
                            if is_italic:
                                with pdf.local_context():
                                    pdf.set_font("DejaVu", "I", body_size)
                                    par.write(run_text)
                            else:
                                par.write(run_text)
            if seg.is_draft and plan.get("watermark"):
                with pdf.local_context():
                    pdf.set_font("DejaVu", "", max(6, body_size - 6))
                    pdf.set_text_color(160, 30, 30)
                    pdf.multi_cell(0, 4.5, "[BOZZA]", align="L",
                                   new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2.0)

    try:
        pdf.output(str(out_path))
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover
        raise ExportError(f"pdf clone writer failed: {exc}") from exc

    log.info(
        "pdf clone v3 written book=%s chapters=%d front_images=%d "
        "orig_pages=%s path=%s",
        exporter.title, len(chapters), len(front_images),
        profile.get("original_page_count"), out_path,
    )
    return out_path
