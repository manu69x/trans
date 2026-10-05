"""Verifica di corrispondenza originale <-> segmenti (scheda Segmenti).

Confronta carattere per carattere il testo originale del libro (i paragrafi
ricostruiti dalle pagine estratte con lo STESSO layer usato dalla
segmentazione, :func:`backend.parsing.chunking.build_chapter_paragraphs`) con
la concatenazione, in ordine di ordinal, dei ``source_text`` dei segmenti
(``translation_units``).

La segmentazione non altera il testo: :func:`split_sentences` preserva il
testo verbatim e :func:`segment_paragraph` ricuce le frasi con un singolo
spazio. Per questo il confronto è fatto sul testo con il whitespace
normalizzato (spazi/nuova riga -> singolo spazio): segnala solo differenze
REALI di contenuto (caratteri mancanti, aggiunti, sostituiti, testo
riordinato) e non le differenze di formazione.

Output: un report per capitolo con esito ok/problemi, le regioni di
divergenza con snippet, i capitoli senza segmenti o senza testo, e le pagine
del libro non coperte da nessun capitolo (testo originale mai segmentato).

Nessuna scrittura: pura lettura, nessun dato lascia la macchina (§13.1).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from .models import Document, DocumentPage, StructureNode, TranslationUnit
from .parsing import chunking, structure

#: Massimo numero di regioni di divergenza riportate per capitolo.
MAX_DIFFS_PER_CHAPTER = 15
#: Lunghezza massima degli snippet di contesto nel report.
SNIPPET_CHARS = 140

_WS_MULTI_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalizza il whitespace per il confronto (spazi/nuova riga -> ' ').

    Il confronto "carattere per carattere" è fatto sul contenuto testuale:
    la segmentazione normalizza gli spazi fra frasi e fra righe, quindi il
    whitespace non è una differenza di contenuto.
    """
    return _WS_MULTI_RE.sub(" ", text or "").strip()


def _chapter_paragraph_text(pages: list[dict],
                            confirmed_keys: set[str]) -> str:
    """Testo originale del capitolo: paragrafi estratti, in ordine."""
    paragraphs = chunking.build_chapter_paragraphs(pages, confirmed_keys)
    return " ".join((p.get("text") or "").strip()
                    for p in paragraphs if (p.get("text") or "").strip())


def _diff_regions(original: str, segments: str) -> list[dict]:
    """Regioni di divergenza fra i due testi normalizzati (difflib opcodes).

    Ogni regione è classificata ``whitespace_only`` quando il testo coinvolto
    è fatto SOLO di spazi: sono gli spazi introdotti dalla ricucitura delle
    frasi (``split_sentences`` + ``" ".join``) quando una parte inizia con
    punteggiatura, es. ``suckers;`` -> ``suckers ;``. Non sono differenze di
    contenuto: il report le segnala come avvisi, non come problemi.
    """
    matcher = SequenceMatcher(a=original, b=segments, autojunk=False)
    regions: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        a = original[i1:i2]
        b = segments[j1:j2]
        regions.append({
            "type": {
                "delete": "mancante_nei_segmenti",
                "insert": "aggiunto_nei_segmenti",
                "replace": "diverso",
            }[tag],
            "position": i1,
            "original_len": i2 - i1,
            "segment_len": j2 - j1,
            "whitespace_only": not a.strip() and not b.strip(),
            "original_snippet": a[:SNIPPET_CHARS],
            "segment_snippet": b[:SNIPPET_CHARS],
        })
        if len(regions) >= MAX_DIFFS_PER_CHAPTER:
            break
    return regions


def verify_project_segments(db: Session, project_id: str) -> dict[str, Any]:
    """Report completo di corrispondenza originale <-> segmenti del progetto."""
    nodes = (
        db.query(StructureNode)
        .filter(StructureNode.project_id == project_id)
        .order_by(StructureNode.ordinal)
        .all()
    )
    chapters = [n for n in nodes if n.kind in ("chapter", "part", "front_matter")]

    # Pagine estratte del progetto (letture + OCR), in ordine di pagina.
    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    page_rows: dict[int, DocumentPage] = {}
    for document in documents:
        for row in (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
            .all()
        ):
            page_rows[int(row.page_number)] = row

    page_payloads: dict[int, dict] = {}
    for page_number, row in page_rows.items():
        payload = dict(row.page_payload or {})
        payload["_page_row_id"] = str(row.id)
        payload["_document_id"] = str(row.document_id)
        page_payloads[page_number] = payload

    first_document_id = documents[0].id if documents else None
    confirmed_keys = _confirmed_keys(db, page_payloads, first_document_id)

    covered_pages: set[int] = set()
    chapter_reports: list[dict] = []
    total_original_chars = 0
    total_segment_chars = 0
    total_segments = 0
    chapters_ok = 0
    chapters_issues = 0
    chapters_whitespace_only = 0

    for node in chapters:
        start = int(node.start_page) if node.start_page else None
        end = int(node.end_page) if node.end_page else None
        has_range = start is not None and end is not None

        pages: list[dict] = []
        for page_number in sorted(page_payloads):
            if has_range:
                assert start is not None and end is not None
                if page_number < start or page_number > end:
                    continue
                covered_pages.add(page_number)
            payload = page_payloads[page_number]
            pages.append({
                "page_number": page_number,
                "mode": "ocr" if payload.get("ocr_record") is not None else "l1",
                "lines": structure.page_lines(payload),
            })

        original_raw = _chapter_paragraph_text(pages, confirmed_keys)
        original = normalize_text(original_raw)

        units = (
            db.query(TranslationUnit)
            .filter(TranslationUnit.project_id == project_id,
                    TranslationUnit.chapter_id == str(node.id))
            .order_by(TranslationUnit.ordinal)
            .all()
        )
        segments_raw = " ".join((u.source_text or "").strip()
                               for u in units if (u.source_text or "").strip())
        segments = normalize_text(segments_raw)

        issues: list[dict] = []
        warnings: list[dict] = []
        title = node.normalized_title or node.source_label

        if not has_range:
            issues.append({
                "type": "senza_range_pagine",
                "detail": ("Il capitolo non ha un intervallo di pagine "
                           "assegnato: il testo originale non è delimitato."),
            })
        if not units and original:
            issues.append({
                "type": "senza_segmenti",
                "detail": (f"Testo originale presente ({len(original)} "
                           "caratteri) ma nessun segmento: capitolo non "
                           "segmentato."),
            })
        if units and not original:
            issues.append({
                "type": "senza_testo",
                "detail": (f"{len(units)} segmenti presenti ma nessun testo "
                           "originale estratto per l'intervallo di pagine "
                           "del capitolo."),
            })

        # Ordinali non sequenziali (1..N) = sequenza dei segmenti interrotta.
        ordinals = [u.ordinal for u in units]
        expected = list(range(1, len(ordinals) + 1))
        if ordinals and ordinals != expected:
            issues.append({
                "type": "ordinali_non_sequenziali",
                "detail": ("Gli ordinali dei segmenti non sono 1..N in "
                           f"ordine: {len(ordinals)} segmenti, primo "
                           f"{ordinals[0]}, ultimo {ordinals[-1]}."),
            })

        # Confronto carattere per carattere sul testo normalizzato.
        content_regions: list[dict] = []
        space_regions: list[dict] = []
        if original and segments and original != segments:
            for region in _diff_regions(original, segments):
                if region["whitespace_only"]:
                    space_regions.append(region)
                else:
                    content_regions.append(region)

        if original and segments:
            if content_regions:
                delta = len(segments) - len(original)
                issues.append({
                    "type": "testo_non_corrispondente",
                    "detail": (
                        f"Testo originale {len(original)} caratteri, segmenti "
                        f"{len(segments)} caratteri "
                        f"({delta:+d}); {len(content_regions)} "
                        f"{'regione' if len(content_regions) == 1 else 'regioni'} "
                        "di contenuto divergente."
                    ),
                    "regions": content_regions,
                })
            elif space_regions:
                warnings.append({
                    "type": "solo_spaziatura",
                    "detail": (
                        f"Contenuto identico carattere per carattere; "
                        f"{len(space_regions)} "
                        f"{'differenza' if len(space_regions) == 1 else 'differenze'} "
                        "di sola spaziatura introdotta dalla ricucitura delle "
                        "frasi (nessuna perdita di testo)."
                    ),
                    "regions": space_regions,
                })

        ok = not issues
        if ok:
            chapters_ok += 1
        else:
            chapters_issues += 1

        total_original_chars += len(original)
        total_segment_chars += len(segments)
        total_segments += len(units)
        if warnings:
            chapters_whitespace_only += 1

        chapter_reports.append({
            "node_id": str(node.id),
            "kind": node.kind,
            "title": title,
            "start_page": start,
            "end_page": end,
            "segment_count": len(units),
            "original_chars": len(original),
            "segment_chars": len(segments),
            "char_delta": len(segments) - len(original),
            "ok": ok,
            "issues": issues,
            "warnings": warnings,
        })

    # Pagine del libro non coperte da nessun capitolo: testo mai segmentato.
    uncovered: list[dict] = []
    for page_number in sorted(page_payloads):
        if page_number in covered_pages:
            continue
        row = page_rows[page_number]
        text = (row.normalized_text or "").strip()
        if not text:
            text = " ".join((ln.get("text") or "").strip()
                            for ln in structure.page_lines(page_payloads[page_number])
                            if (ln.get("text") or "").strip())
        if not text.strip():
            continue  # pagina vuota: nulla da segmentare
        uncovered.append({
            "page": page_number,
            "chars": len(text),
            "preview": text[:SNIPPET_CHARS],
        })

    all_ok = chapters_issues == 0 and not uncovered and bool(chapters)

    return {
        "project_id": str(project_id),
        "ok": all_ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "chapters": len(chapters),
            "chapters_ok": chapters_ok,
            "chapters_with_issues": chapters_issues,
            "segments_total": total_segments,
            "original_chars": total_original_chars,
            "segment_chars": total_segment_chars,
            "char_delta": total_segment_chars - total_original_chars,
            "uncovered_pages": len(uncovered),
            "uncovered_chars": sum(u["chars"] for u in uncovered),
            "chapters_whitespace_only": chapters_whitespace_only,
        },
        "chapters": chapter_reports,
        "uncovered_pages": uncovered,
    }


def _confirmed_keys(db: Session, page_payloads: dict[int, dict],
                    first_document_id: str | None) -> set[str]:
    """Chiavi header/footer confermate (§5.3), stesse usate dalla segmentazione."""
    from .chunking_runner import _confirmed_header_keys

    if not page_payloads:
        return set()
    return _confirmed_header_keys(db, page_payloads, first_document_id)
