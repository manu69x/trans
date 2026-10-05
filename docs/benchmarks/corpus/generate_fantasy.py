#!/usr/bin/env python3
"""Generate the fantasy-chapter corpus PDF (native text layer).

The fantasy chapter is the benchmark text for the F2 LLM-structured NER
(task t_e87a4040, PRD §6.2.3): it is deliberately dense with domain
categories — the curse (the Withering), the species (the Wyrm, wyrmlings),
the artefact (the Black Key) and the fictional institution (the Order of
the Pale Hand) — plus two persons and two locations, so candidate
selection, classification and precision can all be measured.

Usage:  python3 generate_fantasy.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fantasy_text import FANTASY_CHAPTER  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    margin_l, margin_r = 56, 612 - 56
    y = 90.0
    line_h = 19.0
    font = fitz.Font("helv")

    def fits(text: str) -> bool:
        return font.text_length(text, fontsize=13) <= (margin_r - margin_l)

    # Title block
    page.insert_text((margin_l, y), "Chapter One", fontsize=24,
                     color=(20 / 255, 20 / 255, 40 / 255))
    y += 34
    page.insert_text((margin_l, y),
                     "EN source - fantasy chapter (F2 NER LLM benchmark)",
                     fontsize=11, color=(110 / 255, 110 / 255, 120 / 255))
    y += 30

    for para in FANTASY_CHAPTER.strip().split("\n\n"):
        if para.strip() == "Chapter One":
            continue
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if fits(trial):
                line = trial
            else:
                page.insert_text((margin_l, y), line, fontsize=13,
                                 color=(15 / 255, 15 / 255, 25 / 255))
                y += line_h
                line = word
        if line:
            page.insert_text((margin_l, y), line, fontsize=13,
                             color=(15 / 255, 15 / 255, 25 / 255))
            y += line_h + 6  # paragraph gap

    out_path = os.path.join(OUT, "native_05_fantasy_chapter.pdf")
    doc.save(out_path)
    n = doc.page_count
    doc.close()

    txt_path = os.path.join(OUT, "txt", "native_05_fantasy_chapter.txt")
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(FANTASY_CHAPTER)
    print(f"NATIVE {os.path.basename(out_path)} pages={n}")
    print(f"TXT    {os.path.relpath(txt_path, OUT)}")


if __name__ == "__main__":
    main()
