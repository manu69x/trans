#!/usr/bin/env python3
"""Benchmark OCR over the corpus's scanned PDFs.

Measures CER/WER of the F1 OCR worker (backend.parsing.ocr, ADR-002
L3 -> L4 escalation chain) on docs/benchmarks/corpus/scanned_01..03.
``scanned_04_damaged_page.pdf`` (lower third destroyed) is the ADR-002
extreme case -> human review, not an OCR failure.

Per-page gold: the text the corpus generator actually DRAWS (reconstructed
by simulating the pagination loop of generate_corpus.py, which breaks words
one per line and stops at page end) -- comparing against the whole
SCANNED_TEXTS would understate the OCR by ~40% (words never printed).

Output (reproducible):
  docs/benchmarks/results-ocr/results-ocr.json
  docs/benchmarks/results-ocr/results-ocr.md

No data leaves the local network.

Uso:  python3 tools/bench_ocr.py [--corpus PATH] [--outdir PATH]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans")

from backend.parsing import ocr  # noqa: E402


# --------------------------------------------------------------------------- #
# gold "sulla pagina": replica il disegno effettivo del generatore
# --------------------------------------------------------------------------- #
def words_on_page(text: str, page_width_px: int = 612,
                  margin_x: int = 70, start_y: int = 150,
                  line_step: int = 28, bottom: int = 770,
                  font_size: int = 20) -> list[str]:
    """Le parole che generate_corpus._make_scanned disegna su UNA pagina."""
    from PIL import ImageFont

    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    words: list[str] = []
    y = start_y
    for word in text.split():
        if y > bottom:
            break  # il generatore smette: nessuna pagina 2
        bbox = font.getbbox(word)
        if (bbox[2] - bbox[0]) < (page_width_px - margin_x - 60):
            words.append(word)
            y += line_step
    return words


def load_generator() -> dict:
    gen_path = REPO / "docs" / "benchmarks" / "corpus" / "generate_corpus.py"
    spec = importlib.util.spec_from_file_location("gencorpus", gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = [
        "scanned_01_old_volume.pdf",
        "scanned_02_manuscript.pdf",
        "scanned_03_periodical.pdf",
        "scanned_04_damaged_page.pdf",
    ]
    return dict(zip(names, mod.SCANNED_TEXTS))


# --------------------------------------------------------------------------- #
# CER / WER (carattere/parola, allineamento LCS -- def. standard)
# --------------------------------------------------------------------------- #
def _lcs_chars(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if not m or not n:
        return 0
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            cur[j] = prev[j - 1] + 1 if ai == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    return prev[n]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\r", "\n")).strip()


def cer(gold: str, pred: str) -> float:
    g, p = _norm(gold), _norm(pred)
    if not g:
        return 0.0
    return ((len(g) + len(p)) - 2 * _lcs_chars(g, p)) / len(g)


def _lcs_words(a: list[str], b: list[str]) -> int:
    m, n = len(a), len(b)
    if not m or not n:
        return 0
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            cur[j] = prev[j - 1] + 1 if a[i - 1] == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    return prev[n]


def wer(gold: str, pred: str) -> float:
    g, p = _norm(gold).split(), _norm(pred).split()
    if not g:
        return 0.0
    return (len(g) + len(p) - 2 * _lcs_words(g, p)) / len(g)


# --------------------------------------------------------------------------- #
# benchmark per file: L3, L4, pipeline (con escalation come il runner)
# --------------------------------------------------------------------------- #
SUSPECT_DROP_RATIO = 0.70  # replica la soglia di escalation del runner

# Artefatti INIETTATI dal generatore sul raster, assenti dal gold (stesso
# approccio del bench F0: i titoli iniettati nei nativi erano esclusi).
_GENERATOR_ARTEFACT_RE = re.compile(
    r"(scan\s*ned|scanned)\s*page.{0,40}(text\s*layer)?", re.IGNORECASE)


def strip_injected_artefacts(text: str) -> str:
    """Rimuove il timbro/artefatto 'SCANNED PAGE - no text layer'."""
    cleaned = _GENERATOR_ARTEFACT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def bench_file(path: Path, gold_text: str) -> dict:
    data = path.read_bytes()
    gold = " ".join(words_on_page(gold_text))

    out = {"file": path.name, "gold_chars": len(gold),
           "gold_words": len(gold.split()), "levels": {}}

    for level in ("L3", "L4"):
        t0 = time.perf_counter()
        rec = ocr.ocr_page(data, 0, level=level)
        dt = time.perf_counter() - t0
        kept = [ln for ln in rec["lines"] if not ln["suspect"]]
        text = strip_injected_artefacts(
            " ".join(ln["text"] for ln in kept))
        out["levels"][level] = {
            "chars": len(_norm(text)),
            "lines_total": rec["line_count"],
            "lines_kept": len(kept),
            "mean_line_conf": rec["mean_line_conf"],
            "suspect": rec["ocr_suspect"],
            "cer": round(cer(gold, text), 4),
            "wer": round(wer(gold, text), 4),
            "time_s": round(dt, 2),
        }

    # pipeline = esito del runner: L3, poi L4 solo se L3 e' sospetto
    l3, l4 = out["levels"]["L3"], out["levels"]["L4"]
    escalate = l3["suspect"]
    final = l4 if escalate else l3
    out["pipeline"] = {
        "escalated_to_L4": escalate,
        "final_level": "L4" if escalate else "L3",
        "cer": final["cer"],
        "wer": final["wer"],
        "mean_line_conf": final["mean_line_conf"],
        "suspect_pages": 1 if final["suspect"] else 0,
    }
    return out


def render_markdown(results: list[dict], damaged: dict, out: list[str]) -> None:
    out.append("# Benchmark OCR F1 -- risultati (task t_dd7351bc)\n")
    out.append("Corpus: `docs/benchmarks/corpus/` (PDF scansioni, solo "
               "immagine, nessun layer testo).\n")
    out.append("Pipeline: worker OCR F1 (`backend.parsing.ocr`), catena "
               "ADR-002 **L3 -> L4** con escalation su pagine sospette; "
               "engine locale `tesseract` (fallback dichiarato di olmOCR/"
               "PaddleOCR, ADR-002 sez. 4). Nessun dato lascia la rete "
               "locale.\n")
    out.append("Gold per pagina: testo EFFETTIVAMENTE disegnato dal "
               "generatore del corpus (loop di paginazione simulato; le "
               "righe oltre il fondo pagina non esistono nel PDF).\n")
    out.append(f"Data: {datetime.now(timezone.utc).isoformat()}\n")
    out.append("---\n")
    out.append("## Risultati per file (pipeline completa L3->L4)\n")
    out.append("| File | Livello finale | Escalation | CER | WER | Conf. "
               "media righe | Pagine sospette | Tempo L3+L4 (s) |")
    out.append("|------|----------------|:----------:|:---:|:---:|"
               ":---------:|:---------------:|:---------------:|")
    for r in results:
        p = r["pipeline"]
        tot = round(r["levels"]["L3"]["time_s"] + r["levels"]["L4"]["time_s"], 2)
        out.append(
            "| {f} | {lvl} | {esc} | {cer:.1%} | {wer:.1%} | {conf:.3f} | "
            "{sus} | {t} |".format(
                f=r["file"], lvl=p["final_level"],
                esc="sì" if p["escalated_to_L4"] else "no",
                cer=p["cer"], wer=p["wer"], conf=p["mean_line_conf"],
                sus=p["suspect_pages"], t=tot))
    n = len(results)
    if n:
        avg_cer = sum(r["pipeline"]["cer"] for r in results) / n
        avg_wer = sum(r["pipeline"]["wer"] for r in results) / n
        esc_n = sum(1 for r in results if r["pipeline"]["escalated_to_L4"])
        susp = sum(r["pipeline"]["suspect_pages"] for r in results)
        out.append("")
        out.append(f"**Medie su {n} scansioni leggibili:** CER {avg_cer:.1%} · "
                   f"WER {avg_wer:.1%} · escalation L3→L4 su {esc_n}/{n} file "
                   f"· pagine sospette a fine pipeline: {susp}/{n}.\n")
    out.append("## Dettaglio per livello\n")
    out.append("| File | Livello | Righe tot | Righe mantenute | Conf. media | "
               "CER | WER | Tempo (s) |")
    out.append("|------|:-------:|:---------:|:---------------:|:-----------:|"
               ":---:|:---:|----------:|")
    for r in results:
        for lvl in ("L3", "L4"):
            d = r["levels"][lvl]
            out.append(
                "| {f} | {l} | {lt} | {lk} | {cf:.3f} | {cer:.1%} | "
                "{wer:.1%} | {t} |".format(
                    f=r["file"], l=lvl, lt=d["lines_total"],
                    lk=d["lines_kept"], cf=d["mean_line_conf"],
                    cer=d["cer"], wer=d["wer"], t=d["time_s"]))
    out.append("")
    out.append("## Caso estremo: `scanned_04_damaged_page.pdf`\n")
    out.append(f"- L3: righe OCR {damaged['L3']['lines_total']}, sospetto: "
               f"{damaged['L3']['suspect']}, conf. media "
               f"{damaged['L3']['mean_line_conf']:.3f}")
    out.append(f"- L4: righe OCR {damaged['L4']['lines_total']}, sospetto: "
               f"{damaged['L4']['suspect']}, conf. media "
               f"{damaged['L4']['mean_line_conf']:.3f}")
    out.append("- Esito: pagina segnata `ocr_suspect` -> coda revisione "
               "umana (ADR-002 sez. 3, ultima riga; PRD §13). Il testo del "
               "terzo inferiore e' fisicamente illeggibile nel PDF: nessun "
               "motore OCR puo' recuperarlo.\n")
    out.append("## Note di calibrazione (PRD §18.1: soglie da calibrare)\n")
    out.append("- Soglia riga sospetta: conf. media parola < 0.70; pagina "
               "sospetta: >30% righe sospette o nessuna riga mantenuta.")
    out.append("- Escalation L4: pagina L3 sospetta (misurato: su questo "
               "corpus L3/psm3 risulta sempre sospetto, L4/psm11 recupera "
               "le 3 pagine leggibili).")
    out.append("- psm 11 (testo sparso) e' decisivo su pagine con una "
               "parola per riga; i prefiltri L4 (LANCZOS ~300dpi, mediana, "
               "autocontrast) portano la conf. media da ~0.6 a ~0.9.")
    out.append("- WER/CER misurati con allineamento LCS (def. standard); "
               "il benchmark F0 usava una formula WER errata (LCS a "
               "caratteri su liste di parole), corretta qui.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(
        REPO / "docs" / "benchmarks" / "corpus"))
    ap.add_argument("--outdir", default=str(
        REPO / "docs" / "benchmarks" / "results-ocr"))
    args = ap.parse_args()

    corpus = Path(args.corpus)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    gold_map = load_generator()
    readable = ["scanned_01_old_volume.pdf", "scanned_02_manuscript.pdf",
                "scanned_03_periodical.pdf"]
    results = []
    for name in readable:
        print(f"bench {name} ...", flush=True)
        results.append(bench_file(corpus / name, gold_map[name]))
    damaged_text = gold_map["scanned_04_damaged_page.pdf"]
    damaged = bench_file(corpus / "scanned_04_damaged_page.pdf",
                         damaged_text)["levels"]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "tesseract (olmOCR/PaddleOCR fallback, ADR-002)",
        "corpus": [r["file"] for r in results],
        "results": results,
        "damaged_page": damaged,
    }
    (outdir / "results-ocr.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md: list[str] = []
    render_markdown(results, damaged, md)
    (outdir / "results-ocr.md").write_text("\n".join(md) + "\n",
                                           encoding="utf-8")
    print(f"written: {outdir / 'results-ocr.json'}")
    print(f"written: {outdir / 'results-ocr.md'}")
    for r in results:
        p = r["pipeline"]
        print(f"  {r['file']}: level={p['final_level']} "
              f"CER={p['cer']:.1%} WER={p['wer']:.1%}")


if __name__ == "__main__":
    main()
