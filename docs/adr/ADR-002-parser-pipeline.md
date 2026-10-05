# ADR-002 · Four-level parsing / OCR pipeline

- **Status:** ACCEPTED
- **Date:** 2026-09-03
- **Phase:** F0 (discovery)
- **PRD refs:** §5.2 (layered chain), §16 (Phase 0), §18.1 (metrics)

## 1. Context

PRD §5.2 requires a layered extraction chain to handle the variability of
incoming PDFs: some are *digital-born* (they carry a text layer), others are
*scans* (image only, they require OCR). The pipeline must balance quality,
speed and cost, with explicit escalation thresholds between levels.

The Phase-0 benchmark (`tools/bench_parsers.py`) measured the candidate
levels over `docs/benchmarks/corpus/` (9 PDFs: 5 native, 4 synthetic
scans). See [benchmarks/README.md](../benchmarks/README.md) to reproduce.

## 2. Decision

A **four-level chain with conditional escalation**:

```
incoming PDF
      │
      ▼
┌──────────────┐  extractable text?   ┌──────────────┐
│ L1 · PyMuPDF │◄─────────────YES─────│ L2 · Docling │
└──────────────┘                      └──────────────┘
      │ NO                                    │
      │                                       ▼
      │                          structured markdown
      │                            (digital-born)
      ▼
┌──────────────┐  quality ok?         ┌──────────────┐
│ L3 · OCR     │◄─────────────YES─────│ L4 · layout  │
└──────────────┘                      │ detection    │
      │ NO                            └──────────────┘
      ▼
┌──────────────┐
│ L4 · PaddleOCR / │
│ PDF-Extract-Kit  │
└──────────────┘
```

### Level L1 — PyMuPDF / pdfplumber (digital-born)
- **When:** the PDF has an extractable text layer (`page.get_text() ≠ ""`).
- **Extracts:** native text + TOC + coordinates (bounding boxes) + reading
  order.
- **Advantage:** no model latency, ~0% CER on digital-born files.
- **Fallback:** if PyMuPDF fails, try pdfplumber; if both fail, drop to L3.

### Level L2 — Docling (structured markdown)
- **When:** digital-born but not reliably extractable via L1 (anomalously
  generated PDFs).
- **Extracts:** structured markdown with headings, tables, lists.
- **Note:** requires local ML models; use only when L1 is not enough.

### Level L3 — OCR (scans)
- **When:** the PDF is a scan/image with no text layer.
- **Extracts:** text via OCR (olmOCR preferred in production; tesseract as
  the always-available fallback).
- **Quality threshold:** CER ≤ 8% and WER ≤ 15% on the annotated sample
  (PRD §18.1).

### Level L4 — PaddleOCR / PDF-Extract-Kit (layout detection)
- **When:** plain OCR is not enough (complex layouts: columns, tables,
  formulas).
- **Extracts:** OCR + layout detection (text blocks, tables, images) to
  preserve structure.

## 3. Escalation thresholds

Level transitions are **conditional**, driven by verifiable metrics:

| Condition | Level | Why |
|------------|---------|-------------|
| Extractable text (L1) | L1 | zero cost, maximum accuracy |
| L1 failed or text unreadable | L3 | the PDF is a scan |
| L3: CER > 8% **or** WER > 15% | L4 | plain OCR not enough; layout needed |
| L4: still insufficient quality | human review | extreme case (e.g. `scanned_04_damaged_page.pdf`) |

The CER/WER thresholds follow PRD §18.1 ("to be calibrated in Phase 0");
the values above are the PRD's minimum acceptability figures.

## 4. Default pipeline

**L1 for digital-born, L3 for scans, L4 for complex cases, human review for
the unrecoverable.**

### Implementation note (F1)

The L3/L4 chain is implemented in production (`backend/backend/parsing/ocr.py`
+ `l1_runner.py`, dedicated `ocr` queue) and was measured on the readable
synthetic scans of the corpus with `tools/bench_ocr.py`. Findings that
**refine** (not change) the decision:

- **"CER ≤ 8%" is not reachable with OCR alone on the synthetic corpus**:
  the generator prints one word per line with grain, stamp and rotation; on
  real full-page material the expected values are far better. The threshold
  remains the PRD §18.1 reference, but the **operational L3→L4 escalation is
  driven by per-line confidence** (suspect line < 0.70; suspect page > 30%
  suspect lines), measured and calibrated in F1.
- **L3 (psm 3) proved unsuitable for "sparse" pages** (one word per line):
  escalation to L4 fired on 3/3 files; L4 (psm 11 + pre-filters) cleared the
  suspect flag on all three pages.
- **`scanned_04_damaged_page.pdf` remains a "human review" case**: the text
  is physically unreadable; the page is flagged `ocr_suspect` and the flag
  propagates to its segments (QA §10.2, UI filter §11.2).
- The Phase-0 WER was overestimated by a faulty formula (character-level LCS
  applied to word lists, producing negative WER); `tools/bench_ocr.py` uses
  standard LCS alignment for both CER and WER.

## 5. Alternatives considered

- **Single level (L1 + OCR only):** rejected — it does not handle complex
  layouts explicitly (they need L4).
- **Always L4 (with layout detection):** rejected — more expensive and
  slower; pointless on digital-born files where L1 is perfect.
- **Confidence-only escalation:** escalating purely on OCR confidence was
  considered as an alternative to fixed CER/WER thresholds; the PRD
  thresholds were kept for coherence with §18.1, with confidence as the
  operational signal (see §4).
