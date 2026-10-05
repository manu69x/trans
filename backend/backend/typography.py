"""Profilo tipografico del libro originale (export "Con clone struttura").

Estrae durante il rileva-struttura (scheda Import/Struttura) le informazioni
grafiche del PDF originale: font (nomi/dimensioni/peso), allineamento dei
blocchi (centro/destra/sinistra), immagini con bbox e pagina, pagine per
capitolo, numero di pagine originali. Tutto viene salvato come JSON su MinIO
(``projects/<id>/typography.json``) e usato dai writer di export per
ricostruire l'aspetto grafico (2026-10-01, richiesta utente).

Le immagini estratte finiscono in ``projects/<id>/images/<n>.<ext>``.
"""
from __future__ import annotations

import io
import json
import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from .models import Document, DocumentPage

MINIO_BUCKET = "trans"
IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|bmp|tiff?|webp)$", re.I)


# --- storage helpers (usa il provider di progetto: §13.1 + MinIO o FS) ------
def _storage():
    from .storage import get_storage_provider

    return get_storage_provider()


def typography_key(project_id: str) -> str:
    return f"{project_id}/typography.json"


def save_typography(project_id: str, profile: dict[str, Any]) -> None:
    data = json.dumps(profile, ensure_ascii=False).encode("utf-8")
    _storage().put(typography_key(project_id), data)


def load_typography(project_id: str) -> dict[str, Any] | None:
    try:
        data = _storage().get(typography_key(project_id))
        return json.loads(data) if data else None
    except Exception:  # noqa: BLE001 - assente = nessun profilo
        return None


def image_key(project_id: str, image_id: str, ext: str) -> str:
    return f"{project_id}/images/{image_id}.{ext}"


def put_image(project_id: str, image_id: str, ext: str, data: bytes,
              content_type: str) -> None:
    _storage().put(image_key(project_id, image_id, ext), data)


def get_image(project_id: str, image_id: str, ext: str) -> bytes | None:
    try:
        return _storage().get(image_key(project_id, image_id, ext))
    except Exception:  # noqa: BLE001
        return None


# --- analisi -----------------------------------------------------------------
def _alignment(bbox: list[float] | None, page_width: float | None,
               margins: float) -> str:
    """Allineamento del blocco: centro / destra / sinistra (per bbox)."""
    if not bbox or not page_width:
        return "left"
    x0, _, x1, _ = bbox
    left_gap = x0 - margins
    right_gap = page_width - margins - x1
    if left_gap > 40 and abs(left_gap - right_gap) < 24:
        return "center"
    if right_gap < -12:
        return "right"
    return "left"


def _font_name(span: dict) -> str:
    return str(span.get("font") or "")


def extract_typography(db: Session, project_id: str) -> dict[str, Any]:
    """Analizza le pagine estratte e salva il profilo tipografico.

    Arricchisce i payload GIÀ presenti (pymupdf li ha estratti in L1) con
    font name/flags a livello di span, allineamento per blocco, immagini
    (bbox + estrazione binaria in MinIO) e mappa capitolo -> pagine.
    """
    document = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == document.id)
        .order_by(DocumentPage.page_number)
        .all()
    ) if document else []

    font_counter: Counter[str] = Counter()
    size_counter: Counter[float] = Counter()
    page_width: float | None = None
    page_height0: float | None = None
    margins: float = 72.0
    images: list[dict[str, Any]] = []
    page_meta: list[dict[str, Any]] = []

    # 1) arricchimento dai payload L1 (veloce, nessun I/O sul PDF)
    for row in pages:
        payload = row.page_payload or {}
        w_try = payload.get("width")
        if w_try and page_width is None:
            page_width = float(w_try)
        h_try = payload.get("height")
        if h_try and page_height0 is None:
            page_height0 = float(h_try)
        page_blocks: list[dict[str, Any]] = []
        for b_idx, block in enumerate(payload.get("blocks") or []):
            if block.get("kind") == "image":
                page_blocks.append({
                    "block_index": b_idx,
                    "kind": "image",
                    "bbox": block.get("bbox"),
                    "alignment": "center",
                })
                continue
            sizes = [float(s) for s in (block.get("font_sizes") or []) if s]
            dominant = max(set(sizes), key=sizes.count) if sizes else None
            if dominant:
                # peso = numero di CARATTERI del blocco (il corpo testo
                # domina sulle intestazioni: fix 2026-10-02, la moda dei
                # blocchi dava 15pt di un blocco titolo come corpo)
                n_chars = len(
                    " ".join(str(ln.get("text") or "")
                             for ln in (block.get("lines") or [])))
                size_counter[dominant] += max(1, n_chars)
            spans_font: list[str] = []
            italic = bold = underline = False
            # i nomi font non sono nei payload: li recuperiamo dal PDF
            # (2) dopo; qui l'allineamento dal bbox.
            align = _alignment(block.get("bbox"), page_width, margins)
            page_blocks.append({
                "block_index": b_idx,
                "kind": "text",
                "bbox": block.get("bbox"),
                "alignment": align,
                "dominant_size": dominant,
                "bold_ratio": block.get("bold_ratio"),
            })
        page_meta.append({
            "page": row.page_number,
            "width": payload.get("width"),
            "height": payload.get("height"),
            "blocks": page_blocks,
            # testo normalizzato della pagina: serve al match segmento->pagine
            "text": _norm_words(payload.get("normalized_text") or ""),
        })

    # 2) passata sul PDF originale: nomi font reali + estrazione immagini.
    import pymupdf  # lazy: dipende da L1 ma non dal modulo che la chiama

    doc_images_by_page: dict[int, list[dict]] = {}
    pdf_page_count = None
    pdf_width: float | None = None
    pdf_height: float | None = None
    leadings: list[float] = []
    if document and document.storage_key:
        try:
            pdf_bytes = _storage().get(document.storage_key)
            if pdf_bytes:
                pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
                pdf_page_count = pdf.page_count
                if pdf.page_count:
                    r0 = pdf[0].rect
                    pdf_width = round(float(r0.width), 1)
                    pdf_height = round(float(r0.height), 1)
                for pg in pdf:
                    pno = pg.number + 1
                    # leading: gap y medio tra righe consecutive (5..40pt:
                    # esclude interruzione paragrafo e distanze da titoli)
                    text_dict_l = pg.get_text("dict") or {}
                    ys_l: list[float] = []
                    for b_l in (text_dict_l.get("blocks") or []):
                        if b_l.get("type", 0) != 0:
                            continue
                        for l_l in (b_l.get("lines") or []):
                            ys_l.append(float((l_l.get("bbox") or [0, 0, 0, 0])[1]))
                    ys_l.sort()
                    for i_l in range(len(ys_l) - 1):
                        gap = ys_l[i_l + 1] - ys_l[i_l]
                        if 5 < gap < 40:
                            leadings.append(round(gap, 1))
                    # font usati dalle spans
                    text_dict = pg.get_text("dict") or {}
                    for block in (text_dict.get("blocks") or []):
                        if block.get("type", 0) != 0:
                            continue
                        for line in (block.get("lines") or []):
                            for span in (line.get("spans") or []):
                                fname = _font_name(span)
                                if fname:
                                    font_counter[fname] += len(
                                        span.get("text") or "")
                    # immagini
                    for img_index, info in enumerate(pg.get_images(full=True)):
                        xref = info[0]
                        try:
                            base = pdf.extract_image(xref)
                        except Exception:  # noqa: BLE001 - immagine illeggibile
                            continue
                        ext = base.get("ext", "png")
                        image_id = f"p{pno:04d}-{img_index:02d}"
                        put_image(project_id, image_id, ext, base["image"],
                                  f"image/{'jpeg' if ext in ('jpg','jpeg') else ext}")
                        rect = None
                        try:
                            rects = pg.get_image_rects(xref)
                            if rects:
                                rect = [round(v, 1) for v in rects[0]]
                        except Exception:  # noqa: BLE001
                            pass
                        images.append({
                            "image_id": image_id,
                            "page": pno,
                            "ext": ext,
                            "width": base.get("width"),
                            "height": base.get("height"),
                            "bbox": rect,
                            "bytes": len(base["image"]),
                        })
                        doc_images_by_page.setdefault(pno, []).append(
                            {"image_id": image_id, "bbox": rect})
        except Exception:  # noqa: BLE001 - PDF irraggiungibile: profilo parziale
            pass

    # merge immagini nel page_meta
    for pm in page_meta:
        pm["images"] = doc_images_by_page.get(pm["page"], [])

    dominant_fonts = [
        {"font": name, "chars": n}
        for name, n in font_counter.most_common(6)
    ]
    dominant_sizes = [
        {"size": s, "blocks": n} for s, n in size_counter.most_common(5)
    ]
    body_size = dominant_sizes[0]["size"] if dominant_sizes else 12.0

    profile = {
        "source_document_id": str(document.id) if document else None,
        "source_filename": document.filename if document else None,
        "original_page_count": pdf_page_count or len(pages),
        "page_width": page_width or pdf_width,
        "page_height": page_height0 or pdf_height,
        "fonts": dominant_fonts,
        "body_font": (dominant_fonts[0]["font"] if dominant_fonts else None),
        "body_size": body_size,
        "body_leading": round(
            sum(leadings) / len(leadings), 1) if leadings else None,
        "dominant_sizes": dominant_sizes,
        "images": images,
        "image_count": len(images),
        "pages": page_meta,
    }
    save_typography(project_id, profile)
    return profile


# --- mapping segmento -> pagine originali ------------------------------------
_WORD_RE = re.compile(r"\w+")


def _norm_words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def attach_segment_pages(db: Session, project_id: str,
                         profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Per ogni segmento approvato: pagine originali e stile del blocco.

    Mappa ogni segmento alle pagine originali in cui compare il suo testo
    inglese (match per parole normalizzate, sliding window per capitolo) e
    allinea lo stile (font size/allineamento/corsivo) del blocco sorgente.
    """
    from .models import StructureNode, TranslationUnit

    units = (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id)
        .order_by(TranslationUnit.chapter_id, TranslationUnit.ordinal)
        .all()
    )
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .all()
    )
    node_pages: dict[str, tuple[int, int]] = {
        str(n.id): (int(n.start_page), int(n.end_page))
        for n in nodes
        if n.start_page and n.end_page
    }

    pages_meta = profile.get("pages") or []
    page_texts: dict[int, list[str]] = {}
    for pm in pages_meta:
        words = pm.get("text")
        if words is None:  # profili vecchi: ricostruisce dai blocchi se possibile
            words = _norm_words(" ".join(
                str(b.get("text") or "") for b in pm.get("blocks", [])))
        page_texts[pm["page"]] = words

    out: list[dict[str, Any]] = []
    for u in units:
        rng = node_pages.get(str(u.chapter_id)) if u.chapter_id else None
        words = _norm_words(u.source_text or "")
        pages_hit: list[int] = []
        if words and rng:
            start, end = rng
            # prova con finestre decrescenti di parole iniziali (12->4):
            # i layout/ligature possono alterare la coda del testo.
            first = words[:12]
            for pno in range(start, end + 1):
                pwords = page_texts.get(pno) or []
                if not pwords:
                    continue
                for win in (12, 8, 6, 4):
                    fw = words[:win]
                    if not fw:
                        break
                    for i in range(0, max(1, len(pwords) - win + 1)):
                        if pwords[i:i + win] == fw:
                            pages_hit.append(pno)
                            break
                    if pages_hit and pages_hit[-1] == pno:
                        break
        # stile del primo blocco della prima pagina colpita
        style: dict[str, Any] = {}
        if pages_hit:
            pm = next((p for p in pages_meta if p["page"] == pages_hit[0]),
                      None)
            if pm and pm.get("blocks"):
                b0 = pm["blocks"][0]
                style = {
                    "alignment": b0.get("alignment") or "left",
                    "size": b0.get("dominant_size") or profile.get("body_size"),
                    "bold_ratio": b0.get("bold_ratio"),
                }
        out.append({
            "segment_id": str(u.id),
            "numero": u.numero,
            "status": u.status,
            "pages": pages_hit,
            "style": style,
        })
    return out
