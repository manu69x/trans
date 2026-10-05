"""OCR layer for scanned PDFs (PRD 5.2 step 4, ADR-002 L3/L4).

This module is the *pure* OCR layer: it never touches the database or the
network. It rasterises PDF pages, runs the local ``tesseract`` engine (the
installed fallback of the ``olmOCR`` L3 slot -- see ADR-002 section 4) and
returns a structured per-page record with:

* one row per OCR **line** with bounding box (in PDF points), mean word
  confidence and reading order (tesseract block/par/line numbers, kept in
  natural reading order, ``§5.2.4``);
* per-page confidence (mean line confidence) and per-page suspect flags;
* the normalised text assembled from the kept lines.

Two levels are exposed, mirroring the ADR-002 chain:

* **L3** (``run_page`` with ``level="L3"``): baseline OCR pass -- 200 dpi
  raster, ``--psm 3`` (automatic page segmentation, default reading order).
* **L4** (``run_page`` with ``level="L4"``): quality pass for difficult
  pages -- embedded-image extraction when present, 2x LANCZOS upscale,
  median de-speckle, autocontrast, ``--psm 11`` (sparse text) which keeps
  every text element of complex layouts.

Escalation policy (ADR-002 section 3, calibrated on the corpus in F1):
L3 runs first; when more than ``L4_ESCALATION_SUSPECT_RATIO`` of its lines
are low-confidence (or no line survives), the page is escalated to L4.  The
runner (:mod:`backend.ocr_runner`) owns that decision; this module only
executes a single level per call so each attempt stays recorded per page.

Privacy (PRD §13.1): nothing here logs, prints or transmits manuscript text.
All functions return data; the callers persist it to the local DB only.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

# --- thresholds (ADR-002 L3/L4, calibrated on docs/benchmarks/corpus) -------
MIN_WORD_CONF = 0.0          # tesseract reports conf < 0 for non-word rows
LINE_SUSPECT_CONF = 0.70     # line mean word-confidence below this => suspect
PAGE_SUSPECT_RATIO = 0.30    # >30% suspect lines => page flagged ocr_suspect
ESCALATE_LINES = 1           # a page with fewer kept lines than this escalates
L4_ESCALATION_SUSPECT_RATIO = 0.70

#: ``--psm 3`` = fully automatic page segmentation (L3 default reading order).
PSM_BY_LEVEL = {"L3": 3, "L4": 11}
DPI_BY_LEVEL = {"L3": 200, "L4": 300}

#: Environment for the tesseract subprocess. ``OMP_THREAD_LIMIT=1`` is
#: essential in WSL: without it a single scanned page can take >90 s of
#: contended OpenMP spin (measured), with it ~1 s.
_TESSERACT_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")
_TESSERACT_TIMEOUT_S = 120

_WS_RE = re.compile(r"[ \t\xa0]+")
_DIGITS_RE = re.compile(r"\d")


class OcrEngineError(RuntimeError):
    """Raised when the local OCR engine cannot be executed at all."""


def sha256_json(obj: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable object (for page hashes)."""
    canonical = json.dumps(obj, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# rasterisation / image preparation
# --------------------------------------------------------------------------
def _page_render_scale(doc: Any, page_index: int) -> float:
    """Scale factor of the embedded raster vs. the PDF page, if any.

    Scanned PDFs usually embed one image per page; its native resolution can
    be lower than a naive ``get_pixmap(dpi=...)`` render, which would then
    *upscale with nearest-neighbour artefacts* and degrade OCR. Rendering at
    least at the embedded resolution avoids that (measured on the F1 corpus:
    CER improves when the raster matches the source pixels).
    """
    try:
        images = doc[page_index].get_images(full=True)
        if not images:
            return 1.0
        info = doc.extract_image(images[0][0])
        return max(1.0, float(info.get("width", 0)) / doc[page_index].rect.width)
    except Exception:  # noqa: BLE001 - best effort, fall back to plain render
        return 1.0


def page_to_image(data: bytes, page_index: int, level: str) -> Any:
    """Render one page (0-based) to a PIL image prepared for OCR.

    L4 adds the quality pass: LANCZOS upscale to ~300 dpi, median
    de-speckle and autocontrast. The prefilter pipeline is deliberately
    tiny and dependency-light (Pillow only), and stays local.
    """
    import pymupdf
    from PIL import Image, ImageFilter, ImageOps

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(f"page index {page_index} out of range")
        embedded_scale = _page_render_scale(doc, page_index)
        dpi = max(DPI_BY_LEVEL.get(level, 200), int(72 * embedded_scale) + 1)
        pix = doc[page_index].get_pixmap(dpi=dpi)
        buf = io.BytesIO(pix.tobytes("png"))
        width, height = float(doc[page_index].rect.width), float(
            doc[page_index].rect.height)
    finally:
        doc.close()

    img = Image.open(buf).convert("L")
    if level == "L4":
        target = DPI_BY_LEVEL["L4"]
        if dpi < target:
            factor = target / dpi
            img = img.resize(
                (int(img.width * factor), int(img.height * factor)),
                Image.LANCZOS)
        img = img.filter(ImageFilter.MedianFilter(3))
        img = ImageOps.autocontrast(img)
    # Remember the raster resolution for bbox conversion (px -> pt).
    img.info["ocr_pdf_width"] = width
    img.info["ocr_pdf_height"] = height
    img.info["ocr_raster_dpi"] = round(
        img.width / (width / 72.0), 2) if width else dpi
    return img


def tesseract_available() -> bool:
    """True when the local tesseract binary can be executed."""
    return shutil.which("tesseract") is not None


def run_tesseract_tsv(img: Any, psm: int) -> str:
    """Run the local tesseract engine in TSV mode with *psm* segmentation.

    The output format is forced with ``-c tessedit_create_tsv=1`` because
    some tesseract 5.x builds (including the one in this environment) do not
    accept the ``--tsv`` command-line flag.
    """
    if shutil.which("tesseract") is None:
        raise OcrEngineError("tesseract binary not found on PATH")
    tmpdir = tempfile.mkdtemp(prefix="trans_ocr_")
    try:
        png = os.path.join(tmpdir, "page.png")
        img.save(png)
        proc = subprocess.run(
            [
                "tesseract", png, "stdout",
                "-c", "tessedit_create_tsv=1",
                "--psm", str(psm),
            ],
            capture_output=True, text=True,
            timeout=_TESSERACT_TIMEOUT_S, env=_TESSERACT_ENV,
        )
        if proc.returncode != 0:
            raise OcrEngineError(
                f"tesseract failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()[:200]}")
        return proc.stdout
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# TSV parsing
# --------------------------------------------------------------------------
def _parse_tsv_rows(tsv: str) -> list[dict]:
    """Extract level-5 word rows from a tesseract TSV dump."""
    rows: list[dict] = []
    lines = tsv.splitlines()
    if not lines:
        return rows
    header = lines[0].split("\t")
    try:
        idx = {name: i for i, name in enumerate(header)}
        i_level = idx["level"]
        i_conf = idx["conf"]
        i_text = idx["text"]
        required = ("block_num", "par_num", "line_num", "left", "top",
                    "width", "height")
        for name in required:
            _ = idx[name]
    except (KeyError, ValueError):
        # Older headerless builds: fall back to the documented fixed layout.
        i_level, i_conf, i_text = 0, 10, 11
        idx = {"block_num": 2, "par_num": 3, "line_num": 4, "left": 6,
               "top": 7, "width": 8, "height": 9}
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) <= max(i_conf, i_text):
            continue
        if parts[i_level] != "5":  # level 5 = word row
            continue
        try:
            conf = float(parts[i_conf])
        except ValueError:
            continue
        text = parts[i_text].strip()
        if not text or conf < MIN_WORD_CONF:
            continue
        rows.append({
            "block": int(parts[idx["block_num"]]),
            "par": int(parts[idx["par_num"]]),
            "line": int(parts[idx["line_num"]]),
            "left": int(parts[idx["left"]]),
            "top": int(parts[idx["top"]]),
            "width": int(parts[idx["width"]]),
            "height": int(parts[idx["height"]]),
            "conf": conf,
            "text": text,
        })
    return rows


def _group_lines(rows: list[dict]) -> list[dict]:
    """Group word rows into lines, preserving tesseract reading order."""
    lines: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        key = (row["block"], row["par"], row["line"])
        if key not in lines:
            lines[key] = {
                "words": [],
                "confs": [],
                "left": row["left"],
                "top": row["top"],
                "right": row["left"] + row["width"],
                "bottom": row["top"] + row["height"],
            }
            order.append(key)
        agg = lines[key]
        agg["words"].append(row["text"])
        agg["confs"].append(row["conf"])
        agg["left"] = min(agg["left"], row["left"])
        agg["top"] = min(agg["top"], row["top"])
        agg["right"] = max(agg["right"], row["left"] + row["width"])
        agg["bottom"] = max(agg["bottom"], row["top"] + row["height"])
    return [lines[key] for key in order]


def _assemble(lines_out: list[dict]) -> str:
    """Join the kept lines into a paragraph text (§5.2.5 normalised text).

    Lines whose predecessor ends with a hyphen are joined without a space
    and the hyphen is dropped (OCR line breaks split words more often than
    typography intends; unlike the L1 path the raw bbox per character is not
    available here to double-check).

    Tokens without any alphanumeric character (OCR debris: ``|``, ``©``,
    lone dashes...) are dropped from the normalised text; the raw lines in
    the payload keep everything (§5.2.5: normalised text AND raw extraction
    are both conserved).
    """
    parts: list[str] = []
    for line in lines_out:
        text = _WS_RE.sub(" ", line["text"]).strip()
        if not text:
            continue
        tokens = [t for t in text.split(" ") if any(c.isalnum() for c in t)]
        if not tokens:
            continue
        text = " ".join(tokens)
        if parts and parts[-1].endswith(("-", "\u2013", "\u2014")):
            parts[-1] = parts[-1][:-1] + text
        else:
            parts.append(text)
    return " ".join(parts)


def repeated_key(text: str) -> str:
    """Normalise a line for repetition matching (digits collapse to '#')."""
    return _WS_RE.sub(" ", _DIGITS_RE.sub("#", text)).strip()


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def ocr_page(data: bytes, page_index: int, level: str = "L3") -> dict:
    """OCR one page (0-based) at the given ADR-002 level and return a record.

    The record carries the raw structured payload (``lines`` with bbox in
    PDF points, per-line confidence, suspect flags, ``reading_order``),
    the page-level aggregates (``confidence``, ``mean_line_conf``,
    ``ocr_suspect``, ``suspect_line_ratio``) and content hashes.

    Raises :class:`OcrEngineError` when the engine itself is unusable.
    Per-page OCR garbage is NOT an error: it is recorded with low confidence
    and flagged suspect (§5.2.6 "pagine da verificare", §13 mitigation).
    """
    if level not in PSM_BY_LEVEL:
        raise ValueError(f"unknown OCR level {level!r} (expected L3|L4)")

    img = page_to_image(data, page_index, level)
    psm = PSM_BY_LEVEL[level]
    tsv = run_tesseract_tsv(img, psm)
    rows = _parse_tsv_rows(tsv)
    grouped = _group_lines(rows)

    pdf_w = float(img.info["ocr_pdf_width"])
    pdf_h = float(img.info["ocr_pdf_height"])
    raster_dpi = float(img.info["ocr_raster_dpi"])
    px_to_pt = 72.0 / raster_dpi if raster_dpi else 1.0

    lines_out: list[dict] = []
    for order_index, agg in enumerate(grouped):
        conf = sum(agg["confs"]) / len(agg["confs"]) / 100.0
        text = _WS_RE.sub(" ", " ".join(agg["words"])).strip()
        if not text:
            continue
        bbox_pt = [
            round(agg["left"] * px_to_pt, 2),
            round(agg["top"] * px_to_pt, 2),
            round(agg["right"] * px_to_pt, 2),
            round(agg["bottom"] * px_to_pt, 2),
        ]
        lines_out.append({
            "reading_order": order_index,
            "bbox": bbox_pt,
            "confidence": round(conf, 4),
            "suspect": conf < LINE_SUSPECT_CONF,
            "text": text,
        })

    kept = [ln for ln in lines_out if not ln["suspect"]]
    suspect_lines = len(lines_out) - len(kept)
    suspect_ratio = (
        round(suspect_lines / len(lines_out), 4) if lines_out else 1.0)
    mean_conf = (
        round(sum(ln["confidence"] for ln in lines_out) / len(lines_out), 4)
        if lines_out else 0.0)

    normalized = _assemble(kept)
    page_number = page_index + 1
    record: dict = {
        "page_number": page_number,
        "level": level,
        "extractor": "tesseract",
        "engine_label": "tesseract(olmocr-fallback)" if level == "L3"
        else "tesseract(paddleocr-fallback)",
        "psm": psm,
        "raster_dpi": raster_dpi,
        "width": round(pdf_w, 2),
        "height": round(pdf_h, 2),
        "line_count": len(lines_out),
        "suspect_lines": suspect_lines,
        "suspect_line_ratio": suspect_ratio,
        "mean_line_conf": mean_conf,
        "confidence": mean_conf,
        "char_count": len(normalized),
        "ocr_suspect": bool(
            suspect_ratio > PAGE_SUSPECT_RATIO or not kept),
        "lines": lines_out,
        "normalized_text": normalized,
    }
    record["text_sha256"] = hashlib.sha256(
        normalized.encode("utf-8")).hexdigest()
    record["page_sha256"] = sha256_json(record)
    record["status"] = "ok" if lines_out else "empty"
    return record
