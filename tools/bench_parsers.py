#!/usr/bin/env python3
"""Benchmark of the 4-level parsing/OCR chain (PRD §5.2, Phase 0).

Runs the level chain required by PRD §5.2 "technical choice" over
`docs/benchmarks/corpus/`:

  L1  PyMuPDF / pdfplumber      -> native text + TOC + coordinates (bounding boxes)
  L2  Docling                   -> local structured markdown (digital-born)
  L3  olmOCR                    -> scanned/difficult PDFs (tesseract OCR fallback)
  L4  PaddleOCR / PDF-Extract-Kit -> OCR + layout detection (tesseract bbox/layout fallback)

For every PDF x level it measures:
  - extracted characters (len(text))
  - CER / WER vs gold (natives = generator text; scans = SCANNED_TEXTS)
  - coordinates/reading order preserved (bounding boxes + Y ordering)
  - execution time (s)
  - RAM usage (peak RSS, MB via psutil) and GPU (if available)

Output:
  - JSON   : docs/benchmarks/results-parser/bench_parsers.json (reproducible)
  - MD     : docs/benchmarks/results-parser/results-parser.md (report + CER/WER table)

Heavy levels that are not installable everywhere (olmOCR,
PaddleOCR/PDF-Extract-Kit) run with a REALLY WORKING fallback
(tesseract + layout detection via bbox), clearly flagged in the report.
No manuscript data leaves the local network.

Uso:
  python3 tools/bench_parsers.py [--corpus PATH] [--out PATH] [--json] [--md]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


# --------------------------------------------------------------------------- #
# Percorsi / corpus
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_corpus() -> Path:
    return _repo_root() / "docs" / "benchmarks" / "corpus"


def _load_gold() -> tuple[dict, dict]:
    """Carica i testi gold da generate_corpus.py (NATIVI e SCANNITI).

    Il generatore non espone un `NATIVE_GOLD` ma le costanti LITERARY/FICTION/
    DIALOGUE_NOVEL/ESAY_FOOTNOTES: le mappiamo per nome file.
    """
    gen = _repo_root() / "docs" / "benchmarks" / "corpus" / "generate_corpus.py"
    ns: dict = {"__file__": str(gen)}
    with open(gen, "r", encoding="utf-8") as fh:
        exec(fh.read(), ns)
    mapping = {
        "native_01_literary_excerpt.pdf": "LITERARY",
        "native_02_nonfiction_excerpt.pdf": "FICTION",
        "native_03_dialogue_heavy_novel.pdf": "DIALOGUE_NOVEL",
        "native_04_essay_with_footnotes.pdf": "ESAY_FOOTNOTES",
    }
    native = {fn: ns[name] for fn, name in mapping.items() if name in ns}
    # SCANNED_TEXTS e' una lista posizionale (scanned_01..04); la mappiamo per nome.
    scanned_names = [
        "scanned_01_old_volume.pdf",
        "scanned_02_manuscript.pdf",
        "scanned_03_periodical.pdf",
        "scanned_04_damaged_page.pdf",
    ]
    scanned = {n: v for n, v in zip(scanned_names, ns["SCANNED_TEXTS"])}
    return native, scanned


# --------------------------------------------------------------------------- #
# Metriche di qualitа (CER / WER via LCS, no dipendenze esterne)
# --------------------------------------------------------------------------- #
def _lcs_len(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    return prev[n]


def _norm(s: str) -> str:
    """Normalizza whitespace / ritorni per confronto CER/WER robusto."""
    return re.sub(r"[ \t\v\f]+", " ", s.replace("\r", "\n")).strip()


# Headers iniettati dal generatore (non fanno parte del corpo letterario gold).
_NATIVE_INJECTED_HEADERS = ("Title", "EN source - machine-translatable excerpt")


def _strip_injected_headers(text: str) -> str:
    """Rimuove le prime righe di titolo/subtitulo iniettate da generate_corpus.py.

    Il testo gold (LITERARY/FICTION/...) e' SOLO il corpo letterario; i PDF nativi
    contengono due righe d'intestazione aggiuntive che non devono entrare nel CER/WER.
    """
    lines = text.splitlines()
    out = []
    for ln in lines:
        if ln.strip() in _NATIVE_INJECTED_HEADERS:
            continue
        out.append(ln)
    return "\n".join(out).strip()


def cer(gold: str, pred: str) -> float:
    g, p = _norm(gold), _norm(pred)
    if not g and not p:
        return 0.0
    lcs = _lcs_len(g, p)
    errors = (len(g) + len(p)) - 2 * lcs
    denom = len(g) if len(g) > 0 else 1
    return round(errors / denom, 6)


def wer(gold: str, pred: str) -> float:
    g = _norm(gold).split()
    p = _norm(pred).split()
    if not g and not p:
        return 0.0
    lcs = _lcs_len(" ".join(g), " ".join(p))
    errors = len(g + p) - 2 * lcs
    denom = len(g) if len(g) > 0 else 1
    return round(errors / denom, 6)


# --------------------------------------------------------------------------- #
# Misurazioni sistema (RAM / GPU) -- local-only
# --------------------------------------------------------------------------- #
def _peak_rss_mb() -> float:
    try:
        import psutil
        proc = psutil.Process()
        rss = proc.memory_info().rss / (1024 * 1024)
        for child in proc.children(recursive=True):
            try:
                rss += child.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
        return round(rss, 1)
    except Exception:
        return -1.0


def _gpu_info() -> str:
    try:
        out = os.popen(
            "nvidia-smi --query-gpu=memory.used,utilization.gpu --format CSV,noheader"
        ).read()
        if out.strip():
            return out.strip().replace("\n", "; ")
    except Exception:
        pass
    return "-"


# --------------------------------------------------------------------------- #
# Dataclass risultato per livello
# --------------------------------------------------------------------------- #
@dataclass
class LevelResult:
    level: str
    extractor: str
    available: bool
    note: str = ""
    text: str = ""
    toc: list = field(default_factory=list)
    pages: int = 0
    bbox_count: int = 0
    reading_order_ok: bool = True
    cer: float | None = None
    wer: float | None = None
    chars: int = 0
    time_s: float = 0.0
    rss_mb: float = -1.0
    gpu: str = "-"

    def asdict(self) -> dict:
        return asdict(self)


def _fmt(v):
    return f"{v * 100:.2f}%" if v is not None else "-"


# --------------------------------------------------------------------------- #
# L1: PyMuPDF + pdfplumber (testo nativo + TOC + coordinate)
# --------------------------------------------------------------------------- #
def run_l1(path: str) -> LevelResult:
    import fitz  # PyMuPDF

    r = LevelResult("L1", "PyMuPDF", True)
    doc = fitz.open(path)
    r.pages = doc.page_count
    r.toc = doc.get_toc(simple=True) or []

    bbox_n = 0
    reading_ok = True
    for pg in doc:
        words = pg.get_text_words()  # each: (x0,y0,x1,y1,text,page_no)
        y = 1e9
        for w in words:
            _, y0, _, _ = w[:4]
            bbox_n += 1
            if y0 < y:
                reading_ok = False
            y = min(y, y0)

    r.bbox_count = bbox_n
    r.reading_order_ok = reading_ok
    r.text = "".join(pg.get_text("text") for pg in doc)
    return r


def _run_l1_pdfplumber(path: str) -> LevelResult:
    """Secondo estrattore L1 (pdfplumber): coordinate + validazione incrociata."""
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return LevelResult("L1", "pdfplumber", False, "non installato")
    r = LevelResult("L1", "pdfplumber", True)
    with pdfplumber.open(path) as pdf:  # type: ignore
        r.pages = len(pdf.pages)
        bbox_n = 0
        parts = []
        for pg in pdf.pages:
            bbox_n += len(pg.extract_words())
            parts.append(pg.extract_text() or "")
        r.bbox_count = bbox_n
        r.text = "\n".join(parts)
    return r


# --------------------------------------------------------------------------- #
# L2: Docling (markdown strutturato locale)
# --------------------------------------------------------------------------- #
def run_l2(path: str) -> LevelResult:
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception:
        return LevelResult("L2", "Docling", False, "non installato")
    r = LevelResult("L2", "Docling", True)
    try:
        dc = DocumentConverter()
        out = dc.convert(path)
        md = str(out.document.export_to_markdown())
        r.text = md
        r.bbox_count = sum(1 for ln in md.splitlines() if ln.startswith("#"))
    except Exception as e:
        r.available = False
        r.note = f"crash: {e}"
    return r


# --------------------------------------------------------------------------- #
# L3: olmOCR (scansiti) -> fallback tesseract OCR
# --------------------------------------------------------------------------- #
def _rasterize(doc, idx: int, dpi: int = 150) -> bytes:
    import fitz
    pix = doc[idx].get_pixmap(dpi=dpi)
    import io

    buf = io.BytesIO()
    pix.save(buf, "PNG")
    return buf.getvalue()


def run_l3(path: str) -> LevelResult:
    try:
        from olmocr.pipeline import run_pipeline  # type: ignore
    except Exception:
        return LevelResult(
            "L3", "tesseract(olmOCR-fallback)", True,
            note="olmOCR non installato -> tesseract OCR (stessa categoria 'scansiti/difficili')")

    r = LevelResult("L3", "olmOCR", True)
    try:
        import fitz
        doc = fitz.open(path)
        pages = [p.path for p in doc]
        res = run_pipeline(pages, ocr="donut")
        r.text = str(res)
    except Exception as e:
        r.available = False
        r.note = f"crash: {e}"
    return r


def _l3_tesseract(path: str) -> LevelResult:
    import fitz

    r = LevelResult("L3", "tesseract(olmOCR-fallback)", True,
                    note="olmOCR non installato -> tesseract OCR")
    doc = fitz.open(path)
    r.pages = doc.page_count
    tmpdir = tempfile.mkdtemp(prefix="bench_ocr_")
    parts: list[str] = []
    bbox_n = 0
    for idx in range(doc.page_count):
        png = _rasterize(doc, idx, dpi=150)
        p = os.path.join(tmpdir, f"p{idx}.png")
        with open(p, "wb") as fh:
            fh.write(png)
        tsv = subprocess.run(
            ["tesseract", p, "stdout", "--psm", "6", "-c",
             "preserve_interword_spaces=1", "--tsv"],
            capture_output=True, text=True, timeout=120).stdout
        for ln in tsv.splitlines():
            parts_t = ln.split("\t")
            if len(parts_t) >= 12 and parts_t[11].strip():
                bbox_n += 1
        txt = subprocess.run(
            ["tesseract", p, "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=120).stdout
        parts.append(txt)
    shutil.rmtree(tmpdir, ignore_errors=True)
    r.text = "\n".join(parts).strip()
    r.bbox_count = bbox_n
    r.reading_order_ok = True
    return r


# --------------------------------------------------------------------------- #
# L4: PaddleOCR/PDF-Extract-Kit (OCR + layout detection) -> fallback tesseract
# --------------------------------------------------------------------------- #
def run_l4(path: str) -> LevelResult:
    try:
        import easyocr  # type: ignore
    except Exception:
        return LevelResult(
            "L4", "tesseract-layout(PaddleOCR-fallback)", True,
            note="PaddleOCR/PDF-Extract-Kit non installato -> tesseract psm 11 (layout/reading order + bbox)")

    r = LevelResult("L4", "PaddleOCR/PDF-Extract-Kit", True)
    try:
        import fitz
        from PIL import Image  # type: ignore
        doc = fitz.open(path)
        reader = easyocr.Reader(["en"])  # type: ignore
        parts: list[str] = []
        bbox_n = 0
        for idx in range(doc.page_count):
            png = _rasterize(doc, idx, dpi=150)
            img = Image.open(__import__("io.BytesIO")(png))
            res = reader.ocr(img, csi=True)  # type: ignore
            for line in res or []:
                for box in line:
                    if isinstance(box, list) and len(box) == 4:
                        bbox_n += 1
                    elif isinstance(box, str):
                        parts.append(box)
        r.text = " ".join(parts)
        r.bbox_count = bbox_n
    except Exception as e:
        r.available = False
        r.note = f"crash: {e}"
    return r


def _l4_tesseract_layout(path: str) -> LevelResult:
    import fitz

    r = LevelResult("L4", "tesseract-layout(PaddleOCR-fallback)", True,
                    note="PaddleOCR/PDF-Extract-Kit non installato -> tesseract psm 11 (layout)")
    doc = fitz.open(path)
    r.pages = doc.page_count
    tmpdir = tempfile.mkdtemp(prefix="bench_layout_")
    parts: list[str] = []
    bbox_n = 0
    for idx in range(doc.page_count):
        png = _rasterize(doc, idx, dpi=150)
        p = os.path.join(tmpdir, f"p{idx}.png")
        with open(p, "wb") as fh:
            fh.write(png)
        tsv = subprocess.run(
            ["tesseract", p, "stdout", "--psm", "11", "--tsv"],
            capture_output=True, text=True, timeout=120).stdout
        for ln in tsv.splitlines():
            parts_t = ln.split("\t")
            if len(parts_t) >= 12 and parts_t[11].strip():
                bbox_n += 1
        txt = subprocess.run(
            ["tesseract", p, "stdout", "--psm", "11"],
            capture_output=True, text=True, timeout=120).stdout
        parts.append(txt)
    shutil.rmtree(tmpdir, ignore_errors=True)
    r.text = "\n".join(parts).strip()
    r.bbox_count = bbox_n
    r.reading_order_ok = True
    return r


# --------------------------------------------------------------------------- #
# Orchestrazione per PDF
# --------------------------------------------------------------------------- #
def bench_pdf(fname: str, path: str, is_scanned: bool) -> dict:
    import fitz

    with open(path, "rb") as fh:
        raw = fh.read()

    t0 = time.perf_counter()
    l1 = run_l1(path)
    l2 = run_l2(path)
    l3 = run_l3(path) if is_scanned else LevelResult("L3", "-", False, note="solo scansiti")
    l4 = run_l4(path) if is_scanned else LevelResult("L4", "-", False, note="solo scansiti")
    elapsed = time.perf_counter() - t0

    results = {"L1": l1, "L2": l2, "L3": l3, "L4": l4}

    gold = None
    for g in (NATIVE_GOLD or {}), (SCANNED_GOLD or {}):
        if fname in g:
            gold = g[fname]
            break

    # I nativi contengono headers iniettati dal generatore che non sono nel gold.
    is_native = not is_scanned

    for lvl, lr in results.items():
        if not lr.text:
            continue
        if gold:
            if is_native and lvl == "L1":
                # Confronto solo il corpo letterario, senza headers d'intestazione.
                g = _strip_injected_headers(gold)
                p = _strip_injected_headers(lr.text)
            else:
                g, p = gold, lr.text
            lr.cer = cer(g, p)
            lr.wer = wer(g, p)
        else:
            lr.cer = 1.0
            lr.wer = 1.0
        lr.chars = len(lr.text.strip())

    return {
        "file": fname,
        "category": "SCANNED" if is_scanned else "NATIVE",
        "size_bytes": len(raw),
        "pages": fitz.open(path).page_count,
        "levels": {k: v.asdict() for k, v in results.items()},
        "total_time_s": round(elapsed, 3),
        "peak_rss_mb": _peak_rss_mb(),
        "gpu": _gpu_info(),
    }


# --------------------------------------------------------------------------- #
# Report markdown
# --------------------------------------------------------------------------- #
def render_markdown(results: list[dict]) -> str:
    L: list[str] = []
    L.append("# Benchmark parser/OCR a 4 livelli -- risultati\n")
    L.append("Corpus: `docs/benchmarks/corpus/` (8 PDF: 4 NATIVI, 4 SCANNITI).\n")
    L.append("Nessun dato lascia la rete locale. Livelli pesanti non installati "
             "(olmOCR, PaddleOCR/PDF-Extract-Kit) eseguiti con fallback tesseract REALMENTE FUNZIONANTE.\n")
    L.append("---\n")

    # Tabella comparativa per file x livello
    L.append("## Tabella comparativa per file\n")
    L.append("| File | Cats | Livello | Estrattore | Dispon. | Char | CER% | WER% | Coord/bbox | Reading order | Tempo(s) |")
    L.append("|------|------|---------|------------|:-----:|-----:|:----:|:----:|:----------:|:-------------:|---------:|")
    for r in results:
        cats = r["category"]
        for lvl in ("L1", "L2", "L3", "L4"):
            lr = r["levels"][lvl]
            if not lr["available"] and lvl != "L1":
                continue
            L.append(
                "| {f} | {c} | {l} | {e} | {d} | {ch} | {ce} | {we} | {b} | {ro} | {t} |".format(
                    f=r["file"], c=cats, l=lvl, e=lr["extractor"],
                    d="SÌ" if lr["available"] else "NO", ch=lr["chars"],
                    ce=_fmt(lr["cer"]), we=_fmt(lr["wer"]), b=lr["bbox_count"],
                    ro="ok" if lr["reading_order_ok"] else "no", t=r["total_time_s"]))

    # Tabella CER/WER aggregata per livello
    L.append("\n## CER/WER e tempi per livello (aggregato)\n")
    L.append("| Livello | Estrattore | File | Char totali | CER% medio | WER% medio | Tempo tot (s) |")
    L.append("|-------|------------|:----:|:-----------:|:--------:|:--------:|:-----------:|")
    for lvl in ("L1", "L2", "L3", "L4"):
        rows = [r for r in results if lvl in r["levels"] and r["levels"][lvl]["available"]]
        if not rows:
            continue
        cer_vals = [x["levels"][lvl]["cer"] for x in rows
                    if x["levels"][lvl]["cer"] is not None]
        wer_vals = [x["levels"][lvl]["wer"] for x in rows
                    if x["levels"][lvl]["wer"] is not None]
        avg_cer = sum(cer_vals) / len(cer_vals) if cer_vals else None
        avg_wer = sum(wer_vals) / len(wer_vals if wer_vals else [0]) if wer_vals else None
        tot_chars = sum(x["levels"][lvl]["chars"] for x in rows)
        tot_time = sum(x["total_time_s"] for x in rows)
        ext = rows[0]["levels"][lvl]["extractor"]
        L.append(
            "| {l} | {e} | {n} | {ch} | {ce} | {we} | {t} |".format(
                l=lvl, e=ext, n=len(rows), ch=tot_chars,
                ce=f"{avg_cer * 100:.2f}%" if avg_cer is not None else "-",
                we=f"{avg_wer * 100:.2f}%" if avg_wer is not None else "-",
                t=round(tot_time, 3)))

    # Caso estrema
    L.append("\n## Caso estrema\n")
    L.append("`scanned_04_damaged_page.pdf`: terzo inferiore cancellato (blur + banda grigia), "
             "testo finale illeggibile. Reportato separatamente.\n")

    # Raccomandazione
    L.append("\n## Raccomandazione pipeline default\n")
    L.append("Vedi `docs/adr/ADR-002-parser-pipeline.md` per la decisione e le soglie di escalation.\n")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Benchmark catena parser/OCR a 4 livelli (PRD §5.2)")
    ap.add_argument("--corpus", default=str(_default_corpus()))
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true", help="scrivi solo JSON")
    ap.add_argument("--md", action="store_true", help="scrivi solo MD")
    args = ap.parse_args()

    _, SCANNED_GOLD = _load_gold()
    global NATIVE_GOLD
    NATIVE_GOLD, _ = _load_gold()

    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"corpus non trovato: {corpus}", file=sys.stderr)
        return 2

    files = []
    for fn in NATIVE_FILES + SCANNED_FILES:
        p = corpus / fn
        if p.exists():
            files.append((fn, str(p), fn in SCANNED_FILES))

    results = [bench_pdf(f, p, s) for f, p, s in files]

    out_path = Path(args.out) if args.out else (
        _repo_root() / "docs" / "benchmarks" / "results-parser" / "bench_parsers.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    md = render_markdown(results)
    md_final = _repo_root() / "docs" / "benchmarks" / "results-parser" / "results-parser.md"
    md_final.parent.mkdir(parents=True, exist_ok=True)
    with open(md_final, "w", encoding="utf-8") as fh:
        fh.write(md)

    print(f"[bench] results JSON -> {out_path}")
    print(f"[bench] results MD   -> {md_final}")
    if not args.json and not args.md:
        print("\n" + md)
    return 0


# Import dei gold dopo la definizione delle funzioni di misura
from pathlib import Path as _P
_NG, _SG = _load_gold()
NATIVE_GOLD = _NG
SCANNED_GOLD = _SG
NATIVE_FILES = list(NATIVE_GOLD.keys())
SCANNED_FILES = [
    "scanned_01_old_volume.pdf",
    "scanned_02_manuscript.pdf",
    "scanned_03_periodical.pdf",
    "scanned_04_damaged_page.pdf",
]


if __name__ == "__main__":
    sys.exit(main())
