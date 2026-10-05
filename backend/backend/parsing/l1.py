"""Layout-aware extraction of native PDFs (PRD 5.2/5.3, ADR-002 L1).

This module is the *pure* extraction layer: it never touches the database or
the network. It turns PDF bytes into a normalised-text + raw-block structure
with coordinates, per-page confidence, bookmarks (TOC) and font statistics
for heading detection, exactly as PRD 5.2 steps 3-5 and ADR-002 (Livello L1:
PyMuPDF con fallback pdfplumber) require.

Privacy (PRD §13.1): nothing here logs, prints or transmits manuscript text.
All functions return data; the callers persist it to the local DB only.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from typing import Any

# --- thresholds (ADR-002 L1 / §5.2) ---------------------------------------
_MIN_TEXT_CHARS = 1          # below this a page is considered text-less
_HEADING_RATIO_LEVELS = ((1.6, 1), (1.3, 2), (1.15, 3))  # size/body -> level
_COLUMN_SPLIT = 0.5          # page-width midline
_COLUMN_EDGE = 0.06          # tolerance around the midline for line centres
_ZONE_RATIO = 0.12           # top/bottom 12% of the page = header/footer zone
_MIN_REPEATED = 2            # a repeated line must appear on >= 2 pages


def sha256_json(obj: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable object (for page hashes)."""
    canonical = json.dumps(obj, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------
_WS_RE = re.compile(r"[ \t\xa0]+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _is_suspect_char(ch: str, superscript: bool) -> bool:
    """§5.2 \"caratteri dubbi\": replacement chars, private-use area,
    superscript spans (usually footnote markers / broken extraction)."""
    if ch == "\ufffd":
        return True
    if superscript:
        return True
    code = ord(ch)
    return 0xE000 <= code <= 0xF8FF  # private use area


def _line_text(span_chars: list[dict]) -> str:
    return "".join(c.get("c", "") for c in span_chars)


def _zone(bbox: list[float], page_height: float) -> str:
    cy = (bbox[1] + bbox[3]) / 2.0
    if cy < page_height * _ZONE_RATIO:
        return "top"
    if cy > page_height * (1.0 - _ZONE_RATIO):
        return "bottom"
    return "body"


def _upper_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _bold_ratio(spans: list[dict]) -> float:
    """Bold flag is bit 4 (2**4) of the PyMuPDF span flags."""
    if not spans:
        return 0.0
    return sum(1 for s in spans if s.get("flags", 0) & 16) / len(spans)


def _heading_level(sizes: list[float], body_mode: float | None,
                   bold_ratio: float, upper: float) -> int | None:
    """§5.3 heading evidence: font size above body text + weight/caps."""
    if body_mode is None or not sizes:
        return None
    size = max(sizes)
    ratio = size / body_mode
    if bold_ratio < 0.5 and upper < 0.5:
        return None
    for min_ratio, level in _HEADING_RATIO_LEVELS:
        if ratio >= min_ratio:
            return level
    return None


def _assemble_paragraph(lines: list[str]) -> str:
    """Join the wrapped lines of one block into a paragraph.

    §5.3 cleanup rules: em/en dashes, ellipses and quotes are preserved; a
    line ending with a hyphen/dash is joined WITHOUT a space and the hyphen
    is kept (conservative: never merges a semantically significant hyphen).
    """
    parts: list[str] = []
    for raw in lines:
        text = _collapse_ws(raw)
        if not text:
            continue
        if parts and parts[-1][-1:] in ("-", "\u2013", "\u2014"):
            parts[-1] = parts[-1] + text
        else:
            parts.append(text)
    return " ".join(parts)


def _columns_suspected(lines_bbox: list[list[float]], page_width: float) -> bool:
    """§5.2.6 \"colonne da verificare\": two symmetric text bands separated
    by a real gutter (no line crosses the middle)."""
    if len(lines_bbox) < 8:
        return False
    left: list[list[float]] = []
    right: list[list[float]] = []
    for bb in lines_bbox:
        cx = (bb[0] + bb[2]) / 2.0 / page_width
        if cx < _COLUMN_SPLIT - _COLUMN_EDGE:
            left.append(bb)
        elif cx > _COLUMN_SPLIT + _COLUMN_EDGE:
            right.append(bb)
        else:
            return False  # a line straddles the midline -> single column
    if len(left) < 3 or len(right) < 3:
        return False
    gutter_ok = max(bb[2] for bb in left) <= min(bb[0] for bb in right)
    return bool(gutter_ok)


# --------------------------------------------------------------------------
# PyMuPDF primary extraction
# --------------------------------------------------------------------------
def _blocks_from_pymupdf(page: Any) -> tuple[list[dict], int, int]:
    raw = page.get_text("rawdict")
    blocks_out: list[dict] = []
    suspect = 0
    total = 0
    for block in raw.get("blocks", []):
        bbox = [round(v, 2) for v in block.get("bbox", [0, 0, 0, 0])]
        if block.get("type", 0) != 0:
            blocks_out.append({
                "kind": "image", "bbox": bbox, "lines": [],
                "font_sizes": [], "bold_ratio": 0.0,
            })
            continue
        lines: list[dict] = []
        block_sizes: list[float] = []
        for line in block.get("lines", []):
            span_lines = []
            sup_any = False
            for span in line.get("spans", []):
                sup = bool(span.get("flags", 0) & 1)  # bit 0 = superscript
                sup_any = sup_any or sup
                size = round(float(span.get("size", 0.0)), 1)
                if size > 0:
                    block_sizes.append(size)
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    total += 1
                    if _is_suspect_char(c, sup):
                        suspect += 1
                span_lines.append(_line_text(span.get("chars", [])))
            text = _collapse_ws("".join(span_lines))
            if not text:
                continue
            lines.append({
                "bbox": [round(v, 2) for v in line.get("bbox", bbox)],
                "text": text,
                "dir": line.get("dir", [1.0, 0.0]),
            })
        if not lines:
            continue
        blocks_out.append({
            "kind": "text",
            "bbox": bbox,
            "lines": lines,
            "font_sizes": block_sizes,
            "bold_ratio": round(_bold_ratio(
                [sp for ln in block.get("lines", [])
                 for sp in ln.get("spans", [])]), 2),
        })
    return blocks_out, total, suspect


def _page_from_blocks(blocks: list[dict], body_mode: float | None,
                      page_width: float, page_height: float) -> dict:
    """Assign reading order, heading levels and exclusion flags to blocks."""
    reading_order = 0
    heading_blocks: list[int] = []
    text_lines_bbox: list[list[float]] = []
    for i, block in enumerate(blocks):
        block["excluded"] = False
        if block["kind"] != "text":
            block["reading_order"] = None
            continue
        block["reading_order"] = reading_order
        reading_order += 1
        for ln in block["lines"]:
            ln["zone"] = _zone(ln["bbox"], page_height)
            text_lines_bbox.append(ln["bbox"])
        level = _heading_level(
            block.get("font_sizes", []),
            body_mode,
            block.get("bold_ratio", 0.0),
            max((_upper_ratio(ln["text"]) for ln in block["lines"]),
                default=0.0),
        )
        block["heading_level"] = level
        if level is not None:
            heading_blocks.append(i)
    return {
        "heading_blocks": heading_blocks,
        "columns_suspected": _columns_suspected(
            text_lines_bbox, page_width),
    }


def _normalize(blocks: list[dict]) -> str:
    paragraphs: list[str] = []
    for block in blocks:
        if block["kind"] != "text" or block.get("excluded"):
            continue
        paragraphs.append(_assemble_paragraph(
            [ln["text"] for ln in block["lines"]]))
    return "\n\n".join(p for p in paragraphs if p)


def _extract_with_pdfplumber(data: bytes, page_index: int) -> list[dict]:
    """ADR-002 L1 fallback: pdfplumber when PyMuPDF yields no text."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[page_index]
        blocks: list[dict] = []
        for line in page.extract_text_lines(layout=False, strip=True,
                                            return_chars=False):
            text = _collapse_ws(line.get("text", ""))
            if not text:
                continue
            blocks.append({
                "kind": "text",
                "bbox": [round(line["x0"], 2), round(line["top"], 2),
                         round(line["x1"], 2), round(line["bottom"], 2)],
                "lines": [{"bbox": [round(line["x0"], 2),
                                    round(line["top"], 2),
                                    round(line["x1"], 2),
                                    round(line["bottom"], 2)],
                           "text": text, "dir": [1.0, 0.0]}],
                "font_sizes": [],
                "bold_ratio": 0.0,
            })
        return blocks


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def open_document(data: bytes) -> dict:
    """Open a PDF and return page count / TOC / metadata (§5.2 step 2).

    Raises :class:`ValueError` when *data* is not a readable PDF.
    """
    if len(data) < 8 or data[:5] != b"%PDF-":
        raise ValueError("not a PDF: missing %PDF- header")
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - corrupt PDF surface
        raise ValueError(f"could not open PDF: {exc}") from exc
    toc = [
        {"level": int(level), "title": str(title), "page": int(page_no)}
        for level, title, page_no in doc.get_toc(simple=True)
    ]
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    result = {
        "page_count": doc.page_count,
        "toc": toc,
        "metadata": meta,
        "encrypted": bool(doc.is_encrypted),
    }
    doc.close()
    return result


def extract_page(data: bytes, page_index: int) -> dict:
    """Extract one page (0-based) with coordinates, confidence and blocks.

    Returns a dict with the normalised text, the raw blocks (bbox + reading
    order + heading hints), the per-page confidence layer and hashes:
    * ``text_sha256`` — hash of the normalised text;
    * ``page_sha256`` — hash of the whole structured page record.
    """
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(f"page index {page_index} out of range")
        page = doc[page_index]
        width = float(page.rect.width)
        height = float(page.rect.height)
        blocks, total_chars, suspect = _blocks_from_pymupdf(page)
    finally:
        doc.close()

    extractor = "pymupdf"
    if total_chars < _MIN_TEXT_CHARS:
        # ADR-002 L1 fallback: pdfplumber sees different content encodings.
        blocks = _extract_with_pdfplumber(data, page_index)
        extractor = "pdfplumber"
        total_chars = sum(len(ln["text"])
                          for b in blocks for ln in b.get("lines", []))
        suspect = 0

    # body font size = most common size weighted by span occurrences
    size_counter: Counter[float] = Counter()
    for b in blocks:
        for s in b.get("font_sizes", []):
            size_counter[s] += 1
    body_mode: float | None = None
    if size_counter:
        body_mode = size_counter.most_common(1)[0][0]

    info = _page_from_blocks(blocks, body_mode, width, height)

    if total_chars > 0:
        confidence = round(1.0 - suspect / total_chars, 4)
    else:
        confidence = 0.0  # no text layer: page needs OCR (L3), §5.2 step 3

    normalized = _normalize(blocks)
    page_record = {
        "page_number": page_index + 1,
        "width": round(width, 2),
        "height": round(height, 2),
        "extractor": extractor,
        "confidence": confidence,
        "char_count": total_chars,
        "suspect_chars": suspect,
        "columns_suspected": info["columns_suspected"],
        "body_font_size": body_mode,
        "blocks": blocks,
        "normalized_text": normalized,
    }
    page_record["text_sha256"] = hashlib.sha256(
        normalized.encode("utf-8")).hexdigest()
    page_record["page_sha256"] = sha256_json(page_record)
    page_record["status"] = "ok" if total_chars > 0 else "empty"
    return page_record


# --------------------------------------------------------------------------
# repeated header/footer detection (§5.2.6 / §5.3)
# --------------------------------------------------------------------------
_DIGITS_RE = re.compile(r"\d")


def repeated_key(text: str) -> str:
    """Normalise a line for repetition matching (digits collapse to '#')."""
    return _collapse_ws(_DIGITS_RE.sub("#", text))


def collect_repeated_keys(page_blocks: list[dict], page_height: float) -> list[str]:
    """Keys of the header/footer-zone lines of one page (for the Counter)."""
    keys: list[str] = []
    for block in page_blocks:
        if block.get("kind") != "text":
            continue
        for ln in block.get("lines", []):
            key = repeated_key(ln.get("text", ""))
            if len(key) < 3:
                continue
            if _zone(ln["bbox"], page_height) in ("top", "bottom"):
                keys.append(key)
    return keys


def flag_repeated_keys(counter: Counter[str], total_pages: int) -> dict[str, int]:
    """Lines repeated on >= max(2, 40%) of pages are suspected headers."""
    threshold = max(_MIN_REPEATED, math.ceil(0.4 * total_pages))
    return {k: v for k, v in counter.items() if v >= threshold}


def apply_exclusions(page_record: dict, flagged: dict[str, int]) -> int:
    """Mark blocks whose lines are ALL suspected headers/footers excluded.

    §5.3: repetition alone only *flags*; the exclusion here feeds the import
    report and the UI preview — the user confirms before deletion (rollback
    preview is a later-phase concern). Returns the excluded block count.
    """
    if not flagged:
        return 0
    excluded = 0
    for block in page_record["blocks"]:
        if block.get("kind") != "text" or not block.get("lines"):
            continue
        keys = [repeated_key(ln.get("text", "")) for ln in block["lines"]]
        if keys and all(k in flagged for k in keys):
            block["excluded"] = True
            excluded += 1
    if excluded:
        page_record["normalized_text"] = _normalize(page_record["blocks"])
        page_record["text_sha256"] = hashlib.sha256(
            page_record["normalized_text"].encode("utf-8")).hexdigest()
        page_record["page_sha256"] = sha256_json(page_record)
    return excluded
