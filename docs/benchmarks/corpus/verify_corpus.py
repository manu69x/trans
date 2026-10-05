#!/usr/bin/env python3
"""Verifica del corpus: testo estrattabile vs pagina-only + dimensione."""
import os
import fitz

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)))

NATIVE = ["native_01_literary_excerpt.pdf", "native_02_nonfiction_excerpt.pdf",
          "native_03_dialogue_heavy_novel.pdf", "native_04_essay_with_footnotes.pdf"]
SCANNED = ["scanned_01_old_volume.pdf", "scanned_02_manuscript.pdf",
           "scanned_03_periodical.pdf", "scanned_04_damaged_page.pdf"]

print(f"corpus: {CORPUS}")
all_ok = True
for fn in NATIVE + SCANNED:
    p = os.path.join(CORPUS, fn)
    if not os.path.exists(p):
        print(f"  MANANTE {fn}")
        all_ok = False
        continue
    sz = os.path.getsize(p)
    doc = fitz.open(p)
    txt = ""
    for pg in doc:
        txt += pg.get_text()
    nchars = len(txt.strip())
    over50 = " >50MB" if sz > 50 * 1024 * 1024 else ""
    tag = "NATIVO (deve AVER testo)" if fn in NATIVE else "SCANSITO (deve AVERE 0 testo)"
    if fn in NATIVE and nchars < 10:
        all_ok = False
        print(f"  !!! {fn} MALE: manca testo")
    if fn in SCANNED and nchars > 0:
        all_ok = False
        print(f"  !!! {fn} MALE: ha un layer testo inatteso")
    print(f"  {fn:<38} {sz//1024:>5d}KB  chars={nchars:>5d}  [{tag}]{over50}")

print()
print("VERIFICA DIMENSIONI:", "TUTTI <= 50MB" if all_ok else "PROBLEMI TROVATI")
