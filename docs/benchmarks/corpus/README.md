# Benchmark corpus — import / OCR gold standard

A pool of **9 PDFs** used as the *gold standard* to measure parser and OCR
quality (Phase 0 · PRD §16, §18.1). Every file is described below with its
category (**NATIVE**, **SCANNED**, **SYNTHETIC**) and annotation notes.

> **Note on the "scanned" PDFs**
> Public sources did not easily provide PDFs that are *really* scanned,
> i.e. with no text layer at all. Those 4 files are therefore **SYNTHETIC**:
> they were generated with PyMuPDF/PIL by rendering text onto an image and
> embedding it as **image-only pages**, with no extractable text layer.
> They are labelled synthetic here and are ideal for exercising the whole
> OCR path.

---

## Legend

| Tag | Meaning |
|-----|-------------|
| **NATIVE** | PDF with a real text layer (generated via PyMuPDF `insert_text`). Extraction must return the text without OCR. |
| **SCANNED / SYNTHETIC** | PDF without a text layer (image only). Simulates a real scan: the import must switch to OCR. |

---

## The files

### Native PDFs (5) — extractable text

| File | Notes |
|------|-------|
| `native_01_literary_excerpt.pdf` | Classic-style EN literary prose (descriptive narration, one dialogue). ~640 chars. |
| `native_02_nonfiction_excerpt.pdf` | EN non-fiction essay (long sentences, argumentative structure). ~615 chars. |
| `native_03_dialogue_heavy_novel.pdf` | **Dialogue-heavy novel** — the hardest case for chunking/entities: short sentences alternating with quoted speech, contractions (`can't`, `I've`). ~670 chars. |
| `native_04_essay_with_footnotes.pdf` | **Essay with footnotes** — an indented footnote block and a reference to "chapter four". ~1210 chars. |
| `native_05_fantasy_chapter.pdf` | **Fantasy chapter** — dense with the PRD §6.2.3 domain categories: the curse *the Withering*, the species *the Wyrm* / *wyrmlings*, the artifact *the Black Key*, the fictional institution *the Order of the Pale Hand*, plus characters (Kestra, Dain, Sorrel) and places (Ravenwood Manor, Thornbury). Gold text in `txt/native_05_fantasy_chapter.txt`, generator `generate_fantasy.py`. |

All five contain extractable English text (EN source → IT target) via
`document.get_text()`.

### Scanned PDFs (4, SYNTHETIC) — image only, no text layer

| File | Notes |
|------|-------|
| `scanned_01_old_volume.pdf` | Page from an **old volume: stained paper**, grain and shadows. Three paragraphs of English text. |
| `scanned_02_manuscript.pdf` | **Manuscript with uncertain handwriting**: ink that bunches and thins; a margin note. |
| `scanned_03_periodical.pdf` | **Periodical page**: headline in dated typography, an advertisement. |
| `scanned_04_damaged_page.pdf` | **Damaged page**: the lower third wiped out by a defective scan (blur + grey band). The text breaks off on an unreadable name — the extreme case for OCR QA. |

Every scan is rendered at 150 DPI with paper grain, a diagonal
`SCANNED — NO TEXT LAYER` stamp, slight rotation (0.7°) and salt-and-pepper
noise. **None contains extractable text** (`get_text()` → ""): they force
the system to switch to OCR.

---

## Statistics

| Group | Files | Avg size | Extractable text |
|--------|------|------------------|--------------------|
| Native | 5 | ~3 KB | **Yes** (600–2000 chars) |
| Scanned (synthetic) | 4 | ~6 MB | **No** (0 chars) |

No file exceeds 50 MB.

---

## How to regenerate / re-annotate

```bash
# Regenerate all 9 PDFs (overwrites)
python3 generate_corpus.py

# Verify text vs page + sizes
python3 verify_corpus.py
```

- `generate_corpus.py` — deterministic script (fixed seeds) that produces
  the files.
- `generate_fantasy.py` / `_fantasy_text.py` — the synthetic fantasy chapter
  used by the NER benchmarks.
- `verify_corpus.py` — checks that natives have text and scans do not, and
  that no file exceeds the size limit.

## Annotation notes (gold standard)

For each of the "hard" cases (`native_03`, `native_04`, the scans) keep an
annotation set outside the corpus (not committed):

- `native_03`: the entity list (Declan; "she"/"her" — gender inferred from
  evidence), dialogue punctuation, contractions to normalise.
- `native_04`: footnote/body alignment; the "chapter four" reference treated
  as a **structural entity** (not translated blindly).
- scans: per-line/per-page gold transcriptions (to compute CER/WER).
