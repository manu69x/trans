#!/usr/bin/env python3
"""Generatore del corpus di test per il benchmark di import/OCR.

Crea 8 PDF rappresentativi:
  - 4 PDF NATIVI (layer testo reale tramite PyMuPDF)
      * native_01_literary_excerpt.pdf          : prosa letteraria EN classica
      * native_02_nonfiction_excerpt.pdf         : saggio non-fiction, EN
      * native_03_dialogue_heavy_novel.pdf       : ROMAZZO con molti dialoghi (EN)
      * native_04_essay_with_footnotes.pdf       : SAGGIO con note a pi' di pagina (EN)
  - 4 SCANNATI (solo immagine, ALCUN layer testo) simulati da pagine renderizzate

Tutti i testi sono INGLESE (sorgente EN->IT). I PDF "scansiti" non contengono
testo estrattabile: simulano il risultato di una scansione / OCR mancante.

Uso:  python3 generate_corpus.py
"""

import os
import io
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import fitz  # PyMuPDF

OUT = os.path.dirname(os.path.abspath(__file__))


def _rgb(*vals):
    """Converte componenti colore da 0-255 a 0-1 (richiesto da MuPDF)."""
    return tuple(v / 255.0 for v in vals)


# --------------------------------------------------------------------------- #
# Utilit'a tipografia / rendering
# --------------------------------------------------------------------------- #
def _raster_to_png(img, dpi=150):
    buf = io.BytesIO()
    if dpi != 72:
        scale = dpi / 72.0
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.NEAREST)
    img.save(buf, "PNG")
    return buf.getvalue()


def draw_text_raster(img, x, y, text, font, fill):
    img_draw = ImageDraw.Draw(img)
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    img_draw.text((x, y), text, font=font, fill=fill)


def new_page(doc):
    return doc.new_page(width=612, height=792)


# --------------------------------------------------------------------------- #
# TESTI SORGENTE (EN -> IT). Testo letterario / saggistico originale.
# Virgolette e apostrofi ASCII per massimale portabilita' del file.
# --------------------------------------------------------------------------- #
LITERARY = """Chapter One

The morning came grey and soft over the harbour of Saint Albans. Maren stood at
the window of the small kitchen and watched the boats lean against their moorings,
their hulls dripping with the tide that had left them long ago. Her father's coat
still hung on the peg by the door, though he had not worn it for ten years.

'You are thinking about him again,' said her mother, without turning from the
stove. The kettle began to murmur a low, patient tune.

'I am only listening,' Maren answered. 'The whole house is full of him. I can
almost hear his boots on the hallstone, pretending he has not been gone so long.

Her mother set two mugs on the table and sat at last, folding her hands around
the warmer one. 'Then let it remember. But do not forget that the tide always
comes back, even for the things we think are lost.'"""

FICTION = """The committee met every Thursday in a room that smelled faintly of old paper
and cold coffee. Dr. Okonkwo arrived first, as she always did, and spread her
notes across the long table with the patience of someone who had learned to expect
nothing in particular from the others.

'When do you think it will be ready?' asked the chairman, tapping his pen against
the edge of a spreadsheet that no one had read.

'When it is finished,' she replied. 'That is the only answer we have ever been
given about anything worth doing.'

Nobody laughed, though the words were nearly funny. Instead each person looked
out the window at the grey courtyard and imagined the year they had spent on the
project dissolving, quietly, into the ordinary hours between one meeting and the
next."""

DIALOGUE_NOVEL = """'You can't be serious,' Declan said, setting down his glass so carefully that the
condensation still trembled along its rim.

'More serious than anything I've ever been,' she answered, and for a moment the
music between their tables seemed to grow louder. 'Tell me you felt it too. The
way the whole room went quiet when the door opened.'

'I felt my cold beer go warm. That's what I felt.'

He was joking, but not really. She knew the difference the way you learn to know
the weather in a man's voice. Outside, the rain had started again, striking the
window in a rhythm that almost passed for applause.

'Then say it,' she said. 'Say the thing you keep walking around. Say it and we
can all go home.'

Declan opened his mouth, closed it, and started over from the beginning."""

ESAY_FOOTNOTES = """The question of local inference has grown less rhetorical and more practical in
the last several years, though few practitioners would describe it as simple.

There are three reasons an institution keeps its data on a single machine. The
first is confidentiality: a manuscript must never cross a public network. The
second is latency: a translator will not watch a spinner for twenty seconds.
The third is cost, which everyone mentions and almost no one budgets honestly.

Consider the tokenization problem. A model that counts words as we do will
regularly misjudge the length of a paragraph, sometimes by a factor of two.
This matters more than it appears, because budgeting is how we avoid the failure
mode in which the context overflows mid-sentence.

    Footnote: see also the discussion of context windows in chapter four, where
    the author argues that a planner must reserve output headroom rather than
    assume the model will stop politely.

The evidence, such as it is, points toward hybrid strategies. A small local
model can handle the mechanical work - segmentation, normalization, extraction -
while a larger model is reserved for the judgment that still resists automation.
This division of labour mirrors, in its own way, the older practice of the
translator's assistant reading the source aloud while the translator wrote."""


# Testi scansionati (solo immagine: non contengono testo nel PDF).
SCANNED_TEXTS = [
    "The old volume had seen better centuries. Its pages were the colour of weak "
    "tea and frayed at every corner. The printer's ink had faded to a dull brown, "
    "and where a fly had once landed the paper bore a small dark stain that hid two "
    "words. Still, the sentence survived: he returned at last to the place that "
    "had made him.",
    "A manuscript in a careless hand, the words running together where the pen had "
    "run dry and spreading apart where its owner thought carefully. In the margin, "
    "a single question in another ink: what does the author mean here? No answer was "
    "given, though someone had underlined the question three times.",
    "A page torn from a periodical of the previous century. The headlines shouted "
    "in a typeface that no longer exists anywhere else. Below them, an advertisement "
    "for a remedy that promised everything and explained nothing, as such remedies "
    "do.",
    "The final scanned page was damaged where the scanner lid had caught a loose "
    "corner. The bottom third dissolved into a grey wash, leaving only the ghost of "
    "a line of text, enough to suggest the sentence had ended with a name we will "
    "never be able to read.",
]


# --------------------------------------------------------------------------- #
# 1) PDF NATIVI (layer testo) -- PAGINATI
# --------------------------------------------------------------------------- #
# Margini interni (pt) su pagina Letter/US-Letter (612x792).
_MARG_L = 56
_MARG_R = 612 - 56          # ~556, larghezza testo disponibile
_MARG_T = 90              # primo testo della pagina
_MARG_B = 712             # fondo utile (792 - margine inferiore 80)
_LINE_H = 19              # passo verticale tra le righe del corpo
_FOOTNOTE_Y = 250         # nota a pi' di pagina: posizione fissa in testa ultima pagina


def _new_body_page(doc):
    """Prepara una nuova pagina e torna (page, cursore_y)."""
    page = new_page(doc)
    return page, _MARG_T


def _fits(page, y, text, fontsize):
    """Il testo entra nella larghezza disponibile a questa altezza?"""
    return fitz.Font("helv").text_length(text, fontsize=fontsize) <= (_MARG_R - _MARG_L)


def _draw_body(doc, body):
    """Disegna il corpo (già diviso in paragrafi) across multiple pagine.

    Il cursore y avanza di _LINE_H ogni riga; quando supera il fondo utile si
    passa a una nuova pagina (paginazione reale, nessun testo oltre il margine
    inferiore).
    """
    f_body = fitz.Font("helv")
    page, y = _new_body_page(doc)
    for para in body.strip().split("\n\n"):
        words = para.split()
        line, drawn = "", False
        for w in words:
            trial = f"{line} {w}".strip()
            if _fits(page, y, trial, 13):
                line = trial
            else:
                page.insert_text((_MARG_L, y), line, fontsize=13,
                                 color=_rgb(15, 15, 25))
                y += _LINE_H
                line, drawn = w, True
                if not line:
                    break
        if line and not drawn:
            page.insert_text((_MARG_L, y), line, fontsize=13,
                             color=_rgb(15, 15, 25))
            y += _LINE_H
        # Paginazione: se el cursore ha superato il fondo, nova pagina.
        if y > _MARG_B:
            page, y = _new_body_page(doc)
    return y


def _draw_footnote(doc, y):
    """Disegna la nota a pi' di pagina. La scrive sempre sulla PAGINA
    Corrente (l'ultima), perche' nella nuova API PyMuPDF inserire su una
    pagina creata in precedenza fallisce dopo che sono state aggiunte
    pagine successive. Ritorna y aggiornato."""
    page = doc[-1]
    if y + 40 > _MARG_B:  # senza posto: nova pagina
        page = new_page(doc)
        y = _FOOTNOTE_Y
    page.insert_text((_MARG_L, y), "Notes", fontsize=11, color=_rgb(60, 60, 70))
    y += 18
    page.insert_text((_MARG_L + 8, y), "See also the discussion of context windows "
             "in chapter four.", fontsize=9.5, color=_rgb(70, 70, 80))
    return y


def _make_native(path, title, body, footnote=None):
    doc = fitz.open()
    page, y = _new_body_page(doc)

    page.insert_text((_MARG_L, y), title, fontsize=24, color=_rgb(20, 20, 40))
    y += 34
    page.insert_text((_MARG_L, y), "EN source - machine-translatable excerpt",
                     fontsize=11, color=_rgb(110, 110, 120))

    y = _draw_body(doc, body)
    if footnote:
        y = _draw_footnote(doc, y)

    doc.save(path)
    n = doc.page_count
    doc.close()
    return n


# --------------------------------------------------------------------------- #
# 2) PDF SCANNATI (solo immagine, nessun layer testo)
# --------------------------------------------------------------------------- #
def _make_scanned(path, text, seed, damaged=False):
    rnd = random.Random(seed)

    page_w_px, page_h_px = 612, 792
    bg = (238, 232, 214)
    img = Image.new("RGB", (page_w_px, page_h_px), bg)

    # leggera grana / macchia "carta antica"
    for _ in range(40):
        x = rnd.randrange(page_w_px); y = rnd.randrange(page_h_px)
        r = rnd.randint(30, 120)
        shade = rnd.choice([225, 220, 230, 210])
        ImageDraw.Draw(img).ellipse([x - r, y - r, x + r, y + r],
                                     fill=(shade, shade - 6, shade - 18))

    # testo principale renderizzato su immagine (solo visivo)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    draw_text_raster(img, 70, 90, "SCANNED PAGE - no text layer", font, (40, 30, 20))
    y = 150
    for word in text.split():
        trial = f"{y} {word}"
        bbox = font.getbbox(trial)
        if (bbox[2] - bbox[0]) < 480:
            draw_text_raster(img, 70, y, word, font, (35, 28, 18))
            y += 28

    # timbro "SCANNED / OCR-UNVERIFIED" in diagonale, a simulare un foglio reale
    stamp = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.text((img.width // 2, img.height // 2), "SCANNED - NO TEXT LAYER",
            font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34),
            fill=(180, 40, 40, 90), angle=-25)
    img = Image.alpha_composite(img.convert("RGBA"), stamp).convert("RGB")

    if damaged:
        img = img.filter(ImageFilter.GaussianBlur(6))
        dmg = Image.new("RGB", img.size, (170, 168, 165))
        img.paste(dmg, (0, int(img.height * 0.66)))

    # piccolo rotto + rumore per simulare una vera scansione
    img = img.rotate(0.7, expand=False, fillcolor=(220, 214, 198))
    # grana sottile (salt-and-pepper leggero) usando rnd esistente
    px = img.load()
    W, H = img.size
    for _ in range((W * H) // 400):
        gx = rnd.randrange(W); gy = rnd.randrange(H)
        v = rnd.choice([60, 255])
        px[gx, gy] = (v, v, v)

    png = _raster_to_png(img, dpi=150)

    doc = fitz.open()
    page = new_page(doc)
    page.insert_image(fitz.Rect(0, 0, 612, 792), stream=png)
    doc.save(path)
    n = doc.page_count
    doc.close()
    return n


# --------------------------------------------------------------------------- #
def main():
    os.makedirs(OUT, exist_ok=True)

    specs = [
        ("native_01_literary_excerpt.pdf", LITERARY, None),
        ("native_02_nonfiction_excerpt.pdf", FICTION, None),
        ("native_03_dialogue_heavy_novel.pdf", DIALOGUE_NOVEL, None),
        ("native_04_essay_with_footnotes.pdf", ESAY_FOOTNOTES,
         "See also the discussion of context windows in chapter four."),
    ]
    for fname, body, foot in specs:
        n = _make_native(os.path.join(OUT, fname), "Title", body, foot)
        print(f"  NATIVE {fname:<38} pages={n}")

    # Il capitolo fantasy (benchmark NER LLM, task t_e87a4040) è generato
    # da generate_fantasy.py: testo dedicato con le categorie di dominio
    # §6.2.3 (maledizione, specie, artefatto, istituzione fittizia).

    scanned = [
        ("scanned_01_old_volume.pdf", SCANNED_TEXTS[0], 11, False),
        ("scanned_02_manuscript.pdf", SCANNED_TEXTS[1], 22, False),
        ("scanned_03_periodical.pdf", SCANNED_TEXTS[2], 33, False),
        ("scanned_04_damaged_page.pdf", SCANNED_TEXTS[3], 44, True),
    ]
    for fname, text, seed, dmg in scanned:
        n = _make_scanned(os.path.join(OUT, fname), text, seed, dmg)
        print(f"  SCANNED {fname:<37} pages={n}")


if __name__ == "__main__":
    main()
