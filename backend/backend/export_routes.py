"""Export routes (PRD §15.4, §13.1, §11.1, §11.2, §1.3.7).

Endpoints:

* ``POST /projects/{id}/export/plan`` -- validate the request and return
  the effective plan (what would be shipped) plus the §15.4 manifest.
* ``POST /projects/{id}/export`` -- build a ``.docx`` / ``.epub`` /
  ``.html`` (editorial book) or ``.xlf`` / ``.tmx`` / ``.csv`` (CAT
  bilingual), record a :class:`~.models.MemorySnapshot`
  (``snapshot_type='export'``) and an :func:`~.audit.log_event` row, then
  stream the file back. CAT files are validated against the official
  schemas (XLIFF 2.1 XSD / TMX 1.4 DTD) *before* streaming; a writer
  failure is 500, never a silently broken file.
* ``POST /projects/{id}/export/reimport`` -- CAT round-trip: upload the
  tool's edited ``.xlf`` / ``.csv`` / ``.tmx`` and apply the changes back
  into the segments (targets + status), freezing versions (§10.5) and
  never overwriting approved segments (§5.1).
* ``GET  /projects/{id}/export/snapshots`` -- list prior export snapshots.
"""
from __future__ import annotations

import pathlib
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import audit
from . import preview
from .db import get_db_session
from .rbac import require_permission
from .export import (
    REIMPORTS,
    ExportError,
    NoApprovedSegments,
    WatermarkRequired,
    build_exporter,
    build_manifest,
    read_csv,
    read_tmx,
    read_xliff,
    validate_tmx,
    validate_xliff,
    write_csv,
    write_docx,
    write_epub,
    write_html,
    write_pdf,
    write_tmx,
    write_xliff,
)
from .models import (
    Document,
    MemorySnapshot,
    Project,
    TranslationMemoryEntry,
)

router = APIRouter(prefix="/projects", tags=["export"])

_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "epub": "application/epub+zip",
    "html": "text/html",
    "pdf": "application/pdf",
    "xliff": "application/x-xliff+xml",
    "tmx": "application/x-tmx",
    "csv": "text/csv",
}

_WRITERS = {
    "docx": write_docx,
    "epub": write_epub,
    "html": write_html,
    "pdf": write_pdf,
    "xliff": write_xliff,
    "csv": write_csv,
}

# The formats the reimport endpoint accepts, mapped to filename hints.
_REIMPORT_FORMATS = {"xliff": "xliff", "csv": "csv", "tmx": "tmx"}


def _latest_snapshot_id(db: Session, project_id: str, snapshot_type: str) -> str | None:
    """The most recent memory snapshot of *snapshot_type* for the project, or
    ``None`` if the project has none (PRD §15.4: the manifest records the
    glossary/TM snapshots that were current at export time)."""
    row = (
        db.query(MemorySnapshot)
        .filter(
            MemorySnapshot.project_id == project_id,
            MemorySnapshot.snapshot_type == snapshot_type,
        )
        .order_by(MemorySnapshot.created_at.desc())
        .first()
    )
    return str(row.id) if row is not None else None


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


class ExportRequest(BaseModel):
    """Body for ``POST /projects/{id}/export[/plan]`` (§15.4)."""

    format: str = Field(
        default="docx",
        description="`docx`/`epub`/`html` (editorial book) or `xliff`/`tmx`/`csv` (CAT bilingual).",
    )
    include_drafts: bool = Field(
        default=False,
        description="Include bozze (drafts) in the export. Only meaningful with "
        "`watermark=True` (§15.4).",
    )
    watermark: bool = Field(
        default=False,
        description="Stamp a `[BOZZA]` watermark on every shipped draft "
        "(§13.1). Required when `include_drafts=True`.",
    )
    page_numbers: bool = Field(
        default=True,
        description="PDF only: running footer with book title + page number "
        "(2026-10-01, richiesta utente: flag per mettere/togliere la "
        "numerazione pagine).",
    )
    clone_structure: bool = Field(
        default=False,
        description="Clone the ORIGINAL book's look: fonts, alignments, "
        "images, original page count with a ~1:1 page ratio when possible "
        "(2026-10-01). Requires the typography profile from structure "
        "detection.",
    )
    page_ratio: float = Field(
        default=1.0,
        description="Clone mode: target ratio between translated pages and "
        "original pages (1.0 = 1:1 when possible).",
    )


def _resolve_plan(db: Session, project_id: str, payload: ExportRequest) -> tuple:
    """Build the exporter and its §15.4 plan, translating errors to HTTP 409."""
    try:
        exporter = build_exporter(db, project_id)
        plan = exporter.plan(
            include_drafts=payload.include_drafts,
            watermark=payload.watermark,
        )
    except (NoApprovedSegments, WatermarkRequired) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return exporter, plan


@router.post("/{project_id}/export/plan", status_code=200)
def plan_export(
    project_id: str,
    payload: ExportRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Validate the export request and return the effective plan (§15.4).

    An export may contain only approved segments, or include drafts only when
    an explicit watermark is requested. This endpoint does not build the file;
    it validates and reports what would be shipped plus the §15.4 manifest.
    """
    _get_project(db, project_id)
    exporter, plan = _resolve_plan(db, project_id, payload)
    manifest = build_manifest(
        exporter=exporter,
        plan=plan,
        fmt=payload.format,
        version=None,
        models=None,
        glossary_snapshot_id=_latest_snapshot_id(db, project_id, "glossary"),
        tm_snapshot_id=_latest_snapshot_id(db, project_id, "tm"),
    )
    plan["manifest"] = manifest.to_dict()
    return {
        "project_id": str(project_id),
        "format": payload.format,
        "include_drafts": plan["include_drafts"],
        "watermark": plan["watermark"],
        "selected_segments": len(plan["selected"]),
        "counts": plan["counts"],
        "chapters": exporter.chapter_titles(),
        "manifest": manifest.to_dict(),
    }


def _tm_entries_for_project(db: Session, project_id: str) -> list[dict]:
    """The project's TM rows as plain dicts (the §7.2 approved memory)."""
    rows = (
        db.query(TranslationMemoryEntry)
        .filter(TranslationMemoryEntry.project_id == project_id)
        .all()
    )
    return [
        {
            "id": str(r.id),
            "source_original": r.source_original,
            "source_normalized": r.source_normalized,
            "target_approved": r.target_approved,
        }
        for r in rows
    ]


# --- anteprima del libro (scheda "Anteprima", 2026-10-01) -------------------
@router.post("/{project_id}/preview/outline", status_code=200)
def preview_outline(
    project_id: str,
    payload: ExportRequest,
    db: Session = Depends(get_db_session),
) -> dict:
    """Indice del libro per la scheda Anteprima: capitoli, segmenti e stime.

    Stesse opzioni e stessa selezione dell'export (§15.4): l'anteprima
    mostra esattamente i segmenti che finirebbero nel file.
    """
    project = _get_project(db, project_id)
    exporter, plan = _resolve_plan(db, project_id, payload)
    outline = preview.build_book_outline(exporter, plan)
    outline["project_id"] = str(project.id)
    outline["project_title"] = project.title
    outline["include_drafts"] = payload.include_drafts
    outline["watermark"] = payload.watermark
    return outline


@router.post("/{project_id}/preview/chapter", status_code=200)
def preview_chapter_route(
    project_id: str,
    payload: ExportRequest,
    chapter_index: int = Query(default=0, ge=0, le=500),
    db: Session = Depends(get_db_session),
) -> dict:
    """Un capitolo del libro in anteprima (render paginato per capitolo)."""
    _get_project(db, project_id)
    return preview.preview_chapter(
        db, project_id,
        include_drafts=payload.include_drafts,
        watermark=payload.watermark,
        chapter_index=chapter_index,
    )


@router.post("/{project_id}/preview/book", status_code=200)
def preview_book(
    project_id: str,
    payload: ExportRequest,
    page_size: int = Query(default=1800, ge=600, le=6000),
    db: Session = Depends(get_db_session),
) -> dict:
    """Tutto il libro impaginato per il viewer bilingue IT | EN (2026-10-01).

    La paginazione segue il testo italiano (colonna sinistra dello spread);
    ``page_size`` sono i caratteri IT per pagina. Restituisce le pagine e il
    flusso completo dei segmenti: la UI affetta lo slice per pagina.
    """
    _get_project(db, project_id)
    return preview.build_book_pages(
        db, project_id,
        include_drafts=payload.include_drafts,
        watermark=payload.watermark,
        page_size=page_size,
    )


# --- profilo tipografico (export "Con clone struttura", 2026-10-01) ---------
@router.get("/{project_id}/typography")
def get_typography(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Profilo tipografico estratto (o `{available: false}` se mai analizzato)."""
    from . import typography as _typo

    _get_project(db, project_id)
    profile = _typo.load_typography(project_id)
    if not profile:
        return {"available": False}
    # pages è voluminoso: riepilogo + conteggi
    return {
        "available": True,
        "source_filename": profile.get("source_filename"),
        "original_page_count": profile.get("original_page_count"),
        "page_width": profile.get("page_width"),
        "page_height": profile.get("page_height"),
        "body_font": profile.get("body_font"),
        "body_size": profile.get("body_size"),
        "fonts": profile.get("fonts"),
        "dominant_sizes": profile.get("dominant_sizes"),
        "image_count": profile.get("image_count"),
        "images": (profile.get("images") or [])[:20],
        "pages_analysed": len(profile.get("pages") or []),
    }


@router.post("/{project_id}/typography/analyse", status_code=200)
def analyse_typography(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Rianalizza il PDF originale e aggiorna il profilo tipografico."""
    from . import typography as _typo

    _get_project(db, project_id)
    profile = _typo.extract_typography(db, project_id)
    return {
        "available": True,
        "original_page_count": profile.get("original_page_count"),
        "body_font": profile.get("body_font"),
        "body_size": profile.get("body_size"),
        "image_count": profile.get("image_count"),
        "pages_analysed": len(profile.get("pages") or []),
    }


@router.get("/{project_id}/typography/segments")
def typography_segments(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Segmenti con pagine originali e stile sorgente (per il clone export)."""
    from . import typography as _typo

    _get_project(db, project_id)
    profile = _typo.load_typography(project_id)
    if not profile:
        raise HTTPException(
            status_code=409,
            detail="profilo tipografico assente: esegui il rileva struttura "
                   "(o POST /typography/analyse) prima",
        )
    rows = _typo.attach_segment_pages(db, project_id, profile)
    return {"project_id": project_id, "segments": rows}


@router.get("/{project_id}/typography/original-page/{page_number}")
def original_page_image(
    project_id: str, page_number: int, db: Session = Depends(get_db_session)
) -> Response:
    """Una pagina del PDF originale come PNG (scheda Verifica Libro)."""
    import pymupdf

    from . import typography as _typo

    _get_project(db, project_id)
    document = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    if document is None or not document.storage_key:
        raise HTTPException(status_code=404, detail="nessun documento originale")
    pdf_bytes = _typo._storage().get(document.storage_key)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF originale non trovato")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    if page_number < 1 or page_number > doc.page_count:
        raise HTTPException(status_code=404, detail="pagina fuori intervallo")
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=110)
    png = pix.tobytes("png")
    return Response(content=png, media_type="image/png", headers={
        "Cache-Control": "max-age=3600",
    })


@router.get("/{project_id}/typography/clone-page/{page_number}")
def clone_page_image(
    project_id: str, page_number: int, db: Session = Depends(get_db_session)
) -> Response:
    """Una pagina del CLONE (rigenerato al volo e cachato) come PNG."""
    import pymupdf

    from . import typography as _typo

    _get_project(db, project_id)
    profile = _typo.load_typography(project_id)
    if not profile:
        raise HTTPException(status_code=409,
                            detail="profilo tipografico assente: esegui il rileva struttura")
    # cache del clone nella giornata: la rigenerazione costa ~45s
    cache_key = f"{project_id}/preview_clone.pdf"
    pdf_bytes = _typo._storage().get(cache_key)
    if not pdf_bytes:
        exporter, plan = _resolve_plan(
            db, project_id,
            ExportRequest(format="pdf", clone_structure=True,
                          page_ratio=1.0, include_drafts=True,
                          watermark=True),
        )
        plan["typography"] = profile
        plan["segment_pages"] = _typo.attach_segment_pages(db, project_id, profile)
        from .export import write_pdf_clone

        import tempfile, pathlib as _pl
        with tempfile.TemporaryDirectory(prefix="trans-clone-") as tmp:
            path = write_pdf_clone(exporter, plan, _pl.Path(tmp))
            pdf_bytes = path.read_bytes()
        _typo._storage().put(cache_key, pdf_bytes)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    if page_number < 1 or page_number > doc.page_count:
        raise HTTPException(status_code=404, detail="pagina fuori intervallo")
    pix = doc[page_number - 1].get_pixmap(dpi=110)
    return Response(content=pix.tobytes("png"), media_type="image/png",
                    headers={"Cache-Control": "max-age=600"})


@router.delete("/{project_id}/typography/clone-cache")
def clone_cache_delete(project_id: str) -> dict:
    """Invalida la cache del clone (dopo nuove traduzioni/approvazioni)."""
    from . import typography as _typo

    _typo._storage().delete(f"{project_id}/preview_clone.pdf")
    return {"cleared": True}


@router.get("/{project_id}/typography/info")
def typography_info_short(
    project_id: str, db: Session = Depends(get_db_session)
) -> dict:
    """Conteggi rapidi per la Verifica Libro (pagine originali)."""
    import pymupdf

    from . import typography as _typo

    _get_project(db, project_id)
    document = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .first()
    )
    n = 0
    if document and document.storage_key:
        pdf_bytes = _typo._storage().get(document.storage_key)
        if pdf_bytes:
            n = pymupdf.open(stream=pdf_bytes, filetype="pdf").page_count

    # Pagine del CLONE: genera/cacha se necessario cosi' la Verifica Libro
    # ha il conteggio vero invece di un massimo prudenziale (fix 2026-10-02:
    # prima il frontend metteva un hardcap 700 e le pagine oltre il totale
    # reale sembravano "duplicate" restando sull'ultima immagine valida).
    clone_pages = 0
    profile = _typo.load_typography(project_id)
    if profile:
        cache_key = f"{project_id}/preview_clone.pdf"
        pdf_bytes = _typo._storage().get(cache_key)
        if not pdf_bytes:
            exporter, plan = _resolve_plan(
                db, project_id,
                ExportRequest(format="pdf", clone_structure=True,
                              page_ratio=1.0, include_drafts=True,
                              watermark=True),
            )
            plan["typography"] = profile
            plan["segment_pages"] = _typo.attach_segment_pages(
                db, project_id, profile)
            from .export import write_pdf_clone

            import tempfile
            import pathlib as _pl

            with tempfile.TemporaryDirectory(prefix="trans-clone-") as tmp:
                path = write_pdf_clone(exporter, plan, _pl.Path(tmp))
                pdf_bytes = path.read_bytes()
            _typo._storage().put(cache_key, pdf_bytes)
        if pdf_bytes:
            clone_pages = pymupdf.open(stream=pdf_bytes,
                                       filetype="pdf").page_count

    return {"original_pages": n, "clone_pages": clone_pages}


def _build_cat_file(
    exporter, plan: dict, payload: ExportRequest, db: Session, tmp_dir: pathlib.Path
) -> tuple[pathlib.Path, str]:
    """Write the requested CAT file and return ``(path, media_type)``.

    XLIFF is checked against the OASIS 2.1 XSD and TMX against the LISA 1.4
    DTD *before* the bytes are handed to the caller (AC1/AC2): a writer
    regression fails the export with 500 instead of shipping a broken file.
    """
    if payload.format == "tmx":
        tm_entries = _tm_entries_for_project(db, str(exporter.project_id))
        if not tm_entries:
            raise HTTPException(
                status_code=409,
                detail="no approved TM entries yet: approve segments first "
                       "to export a TMX (§7.2)",
            )
        path = write_tmx(exporter, tm_entries, tmp_dir)
    elif payload.format == "pdf" and payload.clone_structure:
        from .export import write_pdf_clone

        path = write_pdf_clone(exporter, plan, tmp_dir)
    elif payload.format == "epub" and payload.clone_structure:
        from .export import write_epub_clone

        path = write_epub_clone(exporter, plan, tmp_dir)
    else:  # docx / epub / html / pdf / xliff / csv
        path = _WRITERS[payload.format](exporter, plan, tmp_dir)

    data = path.read_bytes()
    if payload.format == "xliff":
        errors = validate_xliff(data)
        if errors:
            raise HTTPException(
                status_code=500,
                detail={"error": "xliff_failed_schema_validation",
                        "errors": errors[:10]},
            )
    elif payload.format == "tmx":
        errors = validate_tmx(data)
        if errors:
            raise HTTPException(
                status_code=500,
                detail={"error": "tmx_failed_dtd_validation",
                        "errors": errors[:10]},
            )
    return path, _MEDIA_TYPES[payload.format]


@router.post("/{project_id}/export", status_code=200)
def run_export(
    project_id: str,
    payload: ExportRequest,
    request: Request = None,
    db: Session = Depends(get_db_session),
) -> Response:
    """Build the project export and stream it.

    ``docx`` / ``epub`` / ``html`` are the editorial book; ``xliff`` /
    ``tmx`` / ``csv`` are the CAT bilingual formats (§1.3.7). Records a
    :class:`~.models.MemorySnapshot` (``snapshot_type='export'``) and an
    :func:`~.audit.log_event` row (§15.4 / §13.1) before the file streams.
    """
    from .routes import _enforce_rate

    _enforce_rate("export", request)  # §13/§14
    project = _get_project(db, project_id)
    if payload.format not in _MEDIA_TYPES:
        raise HTTPException(
            status_code=400,
            detail="format must be one of: docx, epub, html, xliff, tmx, csv",
        )

    exporter, plan = _resolve_plan(db, project_id, payload)
    manifest = build_manifest(
        exporter=exporter,
        plan=plan,
        fmt=payload.format,
        version=None,
        models=None,
        glossary_snapshot_id=_latest_snapshot_id(db, project_id, "glossary"),
        tm_snapshot_id=_latest_snapshot_id(db, project_id, "tm"),
    )
    # Let the writers embed the manifest (PRD §15.4) and keep the plan's
    # effective watermark in sync with what will actually ship.
    plan["manifest"] = manifest.to_dict()
    # Opzioni di impaginazione (2026-10-01): il writer PDF legge da qui il
    # flag del piedipagina (titolo + numero pagina).
    plan["page_numbers"] = payload.page_numbers
    # Clone della struttura originale (2026-10-01): profilo tipografico +
    # rapporto pagine tradotte/pagine originali. Se richiesto ma il profilo
    # manca, l'export fallisce con 409 e la spiegazione.
    plan["clone_structure"] = payload.clone_structure
    plan["page_ratio"] = payload.page_ratio
    if payload.clone_structure:
        from . import typography as _typo

        profile = _typo.load_typography(project_id)
        if not profile:
            raise HTTPException(
                status_code=409,
                detail="clone_structure richiede il profilo tipografico: "
                       "esegui il rileva struttura nella scheda "
                       "Import/Struttura (o POST /typography/analyse)",
            )
        plan["typography"] = profile
        plan["segment_pages"] = _typo.attach_segment_pages(
            db, project_id, profile)

    snapshot = MemorySnapshot(
        project_id=project_id,
        snapshot_type="export",
        description=(
            f"export {payload.format} include_drafts={payload.include_drafts} "
            f"watermark={payload.watermark}"
        ),
        item_count=len(plan["selected"]),
        payload=manifest.to_dict(),
    )
    db.add(snapshot)
    audit.log_event(
        action="export",
        entity="project",
        project_id=project_id,
        after=manifest.to_dict(),
        db=db,
    )
    db.commit()

    with tempfile.TemporaryDirectory(prefix="trans-export-") as tmp:
        tmp_dir = pathlib.Path(tmp)
        path, media_type = _build_cat_file(exporter, plan, payload, db, tmp_dir)
        data = path.read_bytes()

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename={project.title.replace(' ', '_')}{'_clone' if payload.clone_structure else ''}.{payload.format}"
            ),
            "X-Export-Snapshot-Id": str(snapshot.id),
        },
    )


@router.post("/{project_id}/export/reimport", status_code=200,
             dependencies=[Depends(require_permission("review_segments"))])
async def reimport_export(
    project_id: str,
    file: UploadFile = File(...),
    fmt: str = Query(
        default="auto",
        description="`xliff` | `csv` | `tmx`, or `auto` to infer from the filename.",
    ),
    db: Session = Depends(get_db_session),
) -> dict:
    """CAT round-trip: apply the tool's edited file back into the segments.

    The uploaded file is the XLIFF 2.1 / CSV / TMX 1.4 that was exported and
    then edited in an external CAT tool. The import updates each segment's
    ``target_text`` and status **without losing versions**: the pre-change
    target is frozen on ``translation_unit_versions`` (§10.5) before the new
    value goes live, and approved segments are never overwritten (§5.1) --
    a divergence is reported in ``conflicts`` instead.
    """
    project = _get_project(db, project_id)

    if fmt == "auto":
        fname = (file.filename or "").lower()
        if fname.endswith(".xlf") or fname.endswith(".xliff"):
            fmt = "xliff"
        elif fname.endswith(".tmx"):
            fmt = "tmx"
        elif fname.endswith(".csv"):
            fmt = "csv"
        else:
            raise HTTPException(
                status_code=400,
                detail="cannot infer format from filename; pass fmt=xliff|csv|tmx",
            )
    if fmt not in _REIMPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"unknown format {fmt!r}")

    data = await file.read()
    if not data.strip():
        raise HTTPException(status_code=400, detail="empty file")

    # Fail fast with a precise error when the file is not well-formed in the
    # advertised format (before touching any segment).
    try:
        if fmt == "xliff":
            units = read_xliff(data)
            errors = validate_xliff(data)
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "xliff_failed_schema_validation",
                            "errors": errors[:10]},
                )
        elif fmt == "tmx":
            read_tmx(data)
            errors = validate_tmx(data)
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "tmx_failed_dtd_validation",
                            "errors": errors[:10]},
                )
        else:
            read_csv(data)
    except HTTPException:
        raise
    except ExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        summary = REIMPORTS[fmt](db, project, data)
    except ExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.commit()
    return summary


@router.get("/{project_id}/export/snapshots", status_code=200)
def list_export_snapshots(
    project_id: str,
    db: Session = Depends(get_db_session),
) -> dict:
    """List prior export snapshots for a project (§15.4 / §16)."""
    _get_project(db, project_id)
    rows = (
        db.query(MemorySnapshot)
        .filter(
            MemorySnapshot.project_id == project_id,
            MemorySnapshot.snapshot_type == "export",
        )
        .order_by(MemorySnapshot.created_at.desc())
        .all()
    )
    return {
        "project_id": str(project_id),
        "snapshots": [
            {
                "id": str(r.id),
                "format": (r.payload or {}).get("format"),
                "include_drafts": (r.payload or {}).get("include_drafts"),
                "watermark": (r.payload or {}).get("watermarked"),
                "item_count": r.item_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
