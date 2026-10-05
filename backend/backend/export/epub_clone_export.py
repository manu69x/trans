"""EPUB clone writer (2026-10-03) — l'equivalente EPUB del PDF "Con clone
struttura" (§31–40 del runbook).

Replica sul file elettronico quanto ``pdf_clone_export`` fa per il PDF:

* **copertina**: la prima immagine front (fuori dai capitoli, es. la cover di
  pagina 1) diventa la copertina dell'EPUB (``set_cover`` + meta
  ``cover`` + pagina cover dedicata) — i reader la mostrano in libreria;
* **altre immagini front** (pagine non coperte da capitoli): pagina
  front-matter con le figure in ordine pagina;
* **immagini dei capitoli**: blocco ``<figure>`` in testa al capitolo, con
  larghezza RELATIVA all'originale (bbox pt -> % della larghezza pagina) così
  le proporzioni rispetto al testo tradotto restano quelle del libro;
* **immagini invisibili scartate**: stesso filtro ``_visible_on_page`` del
  PDF clone (ombre con bbox fuori pagina, pag.3 di Alien Clay);
* il flusso del testo resta quello dell'EPUB editoriale (capitoli, corsivi
  ``<em>``, watermark ``[BOZZA]``): un EPUB non ha pagine fisse, quindi il
  rapporto 1:1 riguarda la *struttura e le immagini*, non l'impaginazione.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from ebooklib import epub

from .collector import Exporter
from .errors import ExportError
from .pdf_clone_export import _visible_on_page
from .. import typography as _typo

log = logging.getLogger(__name__)


def _media_type(ext: str) -> str:
    return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"


def _add_image_item(book: epub.EpubBook, image_id: str, ext: str,
                    data: bytes) -> None:
    book.add_item(epub.EpubItem(
        uid=f"img-{image_id}",
        file_name=f"images/{image_id}.{ext}",
        media_type=_media_type(ext),
        content=data,
    ))


def _width_pct(img: dict, orig_w: float | None) -> float | None:
    """Larghezza RELATIVA dell'immagine: bbox pt -> % della pagina originale.

    Senza bbox (o senza larghezza pagina) restituisce ``None``: l'HTML usera'
    solo il vincolo ``max-width:100%`` (l'immagine entra nel testo senza
    sforare, proporzioni originali preservate dall'attributo height=auto).
    """
    bbox = img.get("bbox")
    if not bbox or len(bbox) != 4 or not orig_w:
        return None
    w_pt = float(bbox[2]) - float(bbox[0])
    if w_pt <= 0:
        return None
    return max(10.0, min(100.0, w_pt / orig_w * 100.0))


def _figure(img: dict, orig_w: float | None) -> str:
    """Un blocco ``<figure>`` con la larghezza relativa dell'originale."""
    src = f"images/{img['image_id']}.{img['ext']}"
    alt = html.escape(f"Immagine originale (pagina {img.get('page')})")
    style = "max-width:100%;height:auto;"
    pct = _width_pct(img, orig_w)
    if pct is not None:
        style += f"width:{pct:.1f}%;"
    return (f"<figure class='figure'>"
            f"<img src='{src}' alt='{alt}' style='{style}'/>"
            f"</figure>")


def write_epub_clone(exporter: Exporter, plan: dict, out_dir: Path) -> Path:
    """EPUB con la struttura grafica dell'originale (cover + immagini)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}_clone.epub"

    profile: dict = plan.get("typography") or {}
    seg_pages: list[dict] = plan.get("segment_pages") or []
    style_by_id = {s["segment_id"]: s for s in seg_pages}
    orig_w = float(profile.get("page_width") or 0) or None
    orig_h = float(profile.get("page_height") or 0) or None

    # larghezza pagina per il filtro visibilita' (come fa il PDF clone)
    for img in (profile.get("images") or []):
        img["_page_width"] = orig_w or 0.0

    # --- capitoli + immagini per capitolo (stessa logica del PDF clone) ----
    chapters: list[dict] = []
    for seg in plan["selected"]:
        title = seg.chapter_title or "Untitled"
        if not chapters or chapters[-1]["title"] != title:
            info = style_by_id.get(seg.segment_id) or {}
            pages = info.get("pages") or []
            chapters.append({
                "title": title, "segs": [], "images": [],
            })
        ch = chapters[-1]
        ch["segs"].append(seg)
        info = style_by_id.get(seg.segment_id) or {}
        for pno in (info.get("pages") or []):
            for img in (profile.get("images") or []):
                if (img["page"] == pno
                        and _visible_on_page(img, orig_h or 0.0)
                        and all(i["image_id"] != img["image_id"]
                                for i in ch["images"])):
                    ch["images"].append(img)

    chapter_pages: set[int] = set()
    for s in seg_pages:
        chapter_pages.update(s.get("pages") or [])

    # --- immagini front: fuori dai capitoli, visibili, in ordine pagina ----
    front_images = sorted(
        (img for img in (profile.get("images") or [])
         if img["page"] not in chapter_pages
         and _visible_on_page(img, orig_h or 0.0)),
        key=lambda i: (i["page"], i["image_id"]),
    )

    book = epub.EpubBook()
    book.set_identifier(exporter.project_id)
    book.set_title(exporter.title or "Untitled")
    book.set_language(exporter.target_language)
    book.add_author("Trans")

    # CSS: regole figura/cover oltre quelle dell'EPUB editoriale.
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
            "figure.figure{margin:1em auto;text-align:center;"
            "page-break-inside:avoid;break-inside:avoid}"
            "figure.figure img{max-width:100%;height:auto}"
            "div.cover-page{text-align:center;margin:0;padding:0}"
            "div.cover-page img{max-width:100%;height:auto}"
        ).encode("utf-8"),
    )
    book.add_item(style)

    # --- copertina: la prima immagine front (pagina piu' bassa) ------------
    cover_img: dict | None = None
    for img in front_images:
        data = _typo.get_image(str(exporter.project_id),
                               img["image_id"], img["ext"])
        if data:
            cover_img = img
            cover_img["_data"] = data
            break

    cover_html: epub.EpubHtml | None = None
    if cover_img is not None:
        # set_cover: aggiunge l'item immagine (properties="cover-image"),
        # la pagina cover.xhtml e il meta name="cover" per i reader.
        book.set_cover(
            f"cover.{cover_img['ext']}",
            cover_img["_data"],
            create_page=True,
        )
        cover_html = book.get_item_with_id("cover")

    # --- altre immagini front: pagina front-matter con le figure -----------
    front_item: epub.EpubHtml | None = None
    extra_front = [
        img for img in front_images if img is not cover_img
    ]
    if extra_front:
        parts = []
        for img in extra_front:
            data = _typo.get_image(str(exporter.project_id),
                                   img["image_id"], img["ext"])
            if not data:
                continue
            _add_image_item(book, img["image_id"], img["ext"], data)
            parts.append(_figure(img, orig_w))
        if parts:
            item = epub.EpubHtml(
                title="Immagini", file_name="front-images.xhtml",
                lang=exporter.target_language,
            )
            item.content = (
                "<html><head>"
                '<link rel="stylesheet" type="text/css" href="style.css"/>'
                "</head><body>" + "".join(parts) + "</body></html>"
            )
            front_item = item
            book.add_item(item)
        else:
            extra_front = []

    # --- capitoli: h1 + figure dei capitoli + paragrafi tradotti -----------
    # NB: NIENTE pagina titolo ("Titolo / en → it"): nel clone la prima
    # pagina DEVE essere la copertina originale (richiesta utente 2026-10-03).
    toc: list = []
    chapter_items: list[epub.EpubHtml] = []
    for slug, ch in enumerate(chapters, start=1):
        safe_title = html.escape(ch["title"])
        body = [f"<h1>{safe_title}</h1>"]
        n_imgs = 0
        for img in ch["images"][:4]:  # come il PDF clone: max 4 per capitolo
            data = _typo.get_image(str(exporter.project_id),
                                   img["image_id"], img["ext"])
            if not data:
                continue
            _add_image_item(book, img["image_id"], img["ext"], data)
            body.append(_figure(img, orig_w))
            n_imgs += 1
        from .epub_export import _render_paragraph  # riuso del flusso testo

        body.extend(_render_paragraph(s) for s in ch["segs"])
        item = epub.EpubHtml(
            title=ch["title"], file_name=f"chapter-{slug}.xhtml",
            lang=exporter.target_language,
        )
        item.content = (
            "<html><head>"
            '<link rel="stylesheet" type="text/css" href="style.css"/>'
            "</head><body>" + "".join(body) + "</body></html>"
        )
        book.add_item(item)
        chapter_items.append(item)
        toc.append(item)
    if front_item is not None:
        toc.insert(1, front_item)

    nav = epub.EpubNav()
    ncx = epub.EpubNcx()
    book.add_item(nav)
    book.add_item(ncx)

    # Spine con GLI OGGETTI (non gli id stringa: l'id "cover" della pagina
    # cover autogenerata colpirebbe l'item sbagliato, visto nel test).
    # Ordine da libro: copertina -> immagini front -> capitoli; il nav in
    # coda (usabile, ma fuori dal flusso di lettura).
    # FIX 2026-10-03: cover_html.is_linear = True — ebooklib scrive la pagina
    # cover con linear="no" (fuori dal flusso): i reader la SALTANO e la
    # prima pagina visibile diventava la pagina titolo. Con linear="yes" la
    # copertina e' la prima pagina del libro, come richiesto.
    spine: list = []
    if cover_html is not None:
        cover_html.is_linear = True
        spine.append(cover_html)
    if front_item is not None:
        front_item.is_linear = True
        spine.append(front_item)
    spine.extend(chapter_items)
    spine.append(nav)
    book.spine = spine
    book.toc = toc

    try:
        epub.write_epub(out_path, book, {})
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"epub clone writer failed: {exc}") from exc

    log.info(
        "epub clone written book=%s chapters=%d cover=%s "
        "front_images=%d chapter_images=%d orig_pages=%s path=%s",
        exporter.title, len(chapters),
        bool(cover_img), len(extra_front),
        sum(len(ch["images"]) for ch in chapters),
        profile.get("original_page_count"), out_path,
    )
    return out_path
