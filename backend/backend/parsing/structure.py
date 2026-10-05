"""Multi-signal structure detection (PRD §5.3) — pure layer.

This module never touches the database or the network (same contract as
:mod:`backend.parsing.l1` and :mod:`backend.parsing.ocr`). It consumes the
per-page records the import produced (L1 ``page_payload`` for native pages,
``ocr_record`` for scanned ones) and proposes the book's structure as a list
of §5.3 nodes.

Evidence sources, consulted in the §5.3 reliability order:

1. ``pdf_toc``      — PDF outline/bookmarks, verified against the page text.
2. ``toc_page``     — a textual table of contents recognised in the first
                      pages (title + leader dots + page number), matched on
                      later pages.
3. ``font``         — typographic headings (size above body text, bold
                      weight, uppercase ratio, roman/arabic numbering),
                      reusing the L1 ``heading_level`` hints.
4. ``regex``        — configurable lexical patterns (CHAPTER, PART,
                      PROLOGUE, EPILOGUE, INTERLUDE, ...).
5. ``narrative``    — scene separators (asterisk/dinkus lines) and dateline
                      openings, kept as ``scene`` nodes.
6. ``llm_verify``   — ambiguous nodes only (confidence < 0.8): here, local
                      and offline, the verdict is *withheld* (confidence
                      capped and the node marked for review); the actual
                      LLM call belongs to the runner, which can use the
                      analysis model exposed by the LLM Gateway.

Cleanup rules (§5.3 "Regole di pulizia"):

* repeated headers/footers are only *flagged* here; deletion happens after
  the user confirms (the runner exposes a preview + rollback API);
* dehyphenisation keeps a semantically significant hyphen (hard hyphens in
  compound words are preserved; L1 records already keep the hyphen, OCR
  records dropped it, so only the OCR path is re-joined conservatively);
* em dashes, ellipses and quotes are never altered;
* front/back matter (title page, copyright, TOC, acknowledgements) becomes
  its own ``front_matter``/``back_matter`` node so it is never translated
  by default (§5.3: no automatic translation without explicit selection).

Privacy (PRD §13.1): nothing here logs, prints or transmits manuscript
text. All functions return data; callers persist locally only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# §5.3 configuration (defaults; overridable per project later)
# ---------------------------------------------------------------------------
#: Lexical patterns (§5.3 evidence 4) — §15.1: "proposta quando i bookmark
#: mancano". Order matters only for readability; each pattern is tried on
#: every heading line.
LEXICAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(chapter|ch\.)\s+([0-9]+|[ivxlcdm]+)\b",
        r"^part\s+([0-9]+|[ivxlcdm]+|[a-z]+)\b",
        r"^book\s+([0-9]+|[ivxlcdm]+|[a-z]+)\b",
        r"^(prologue|epilogue|interlude|foreword|preface|afterword)\b",
    )
)

#: Content words of a TOC line (evidence 4 reinforces evidence 3).
_STRUCTURE_WORDS = frozenset({
    "chapter", "part", "book", "prologue", "epilogue", "interlude",
    "foreword", "preface", "afterword",
})

#: Scene separators (§5.3 evidence 5): asterisk rows / dinkus.
_SEPARATORS = frozenset({"* * *", "***", "* *", "*\u00a0*\u00a0*",
                         "# # #", "###", "\u2042", "\u2620"})
_SEPARATOR_RE = re.compile(r"^([*#\u2022\u00b7]\s*){2,}$")

#: Dateline / place-time opening (§5.3 evidence 5), e.g. "LONDON, 1893."
_DATELINE_RE = re.compile(
    r"^[A-Z][A-Za-z .'\-]{1,40},\s*(?:the\s+)?"
    r"(?:\d{1,2}\s+)?[A-Z][a-z]+\s+\d{1,4}\.?$"
)

#: front/back matter keywords for the first/last pages (§5.3 cleanup rules).
_FRONT_WORDS = frozenset({
    "copyright", "isbn", "acknowledg", "table of contents", "contents",
    "frontispiece", "dedication", "colophon", "imprint", "edition",
})
_BACK_WORDS = frozenset({
    "acknowledg", "about the author", "epigraph", "glossary", "colophon",
    "appendix", "afterword", "notes",
})

#: LLM verification gate (§5.3 evidence 6): nodes below this confidence are
#: "ambiguous" and go to the analysis model before user review.
AMBIGUOUS_CONFIDENCE = 0.8

#: Minimum pages a repeated line must appear on before header/footer
#: deletion is even confirmable (§5.3: "conferma algoritmica su almeno
#: N pagine" — the API refuses to delete below this).
MIN_REPEAT_PAGES = 2

ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\xa0]+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def new_node_id() -> str:
    """A §5.3 ``node_id`` (uuid4 string)."""
    return str(uuid.uuid4())


def sha256_json(obj: Any) -> str:
    """Stable SHA-256 of a JSON-serialisable object (rollback snapshots)."""
    canonical = json.dumps(obj, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# page-record accessors (L1 and OCR payloads unified)
# ---------------------------------------------------------------------------
def page_lines(page_payload: dict) -> list[dict]:
    """Unified line list of a stored page record.

    L1 pages: the raw text blocks' lines (``zone`` already computed by L1).
    OCR pages: the ``ocr_record`` lines (zone computed from the bbox by the
    caller-supplied ``page_height``). Each returned line is
    ``{"text", "bbox", "zone"}``.
    """
    lines: list[dict] = []
    if page_payload.get("ocr_record") is not None:
        rec = page_payload["ocr_record"]
        height = float(page_payload.get("height") or 792.0)
        for ln in rec.get("lines", []):
            bbox = ln.get("bbox") or [0, 0, 0, 0]
            cy = (float(bbox[1]) + float(bbox[3])) / 2.0
            if height and cy < height * 0.12:
                zone = "top"
            elif height and cy > height * 0.88:
                zone = "bottom"
            else:
                zone = "body"
            lines.append({"text": ln.get("text", ""), "bbox": bbox,
                          "zone": zone})
        return lines
    for block in page_payload.get("blocks", []):
        if block.get("kind") != "text":
            continue
        for ln in block.get("lines", []):
            lines.append({
                "text": ln.get("text", ""),
                "bbox": ln.get("bbox"),
                "zone": ln.get("zone", "body"),
            })
    return lines


def page_body_lines(page_payload: dict) -> list[dict]:
    """Lines of the page body (header/footer zones removed)."""
    return [ln for ln in page_lines(page_payload) if ln["zone"] == "body"]


def repeated_line_key(text: str) -> str:
    """Normalise a line for repetition matching (digits collapse to '#').

    Same normalisation as the L1/OCR import pass, so the keys stored in the
    import report match the keys computed here.
    """
    return _collapse_ws(re.sub(r"\d", "#", text))


def _cleaned_page_lines(page_payload: dict,
                        confirmed_keys: set[str]) -> list[dict]:
    """All non-header lines of a page for *structure* detection.

    Unlike :func:`page_body_lines` this keeps lines that merely sit high or
    low on the page (a chapter title at 12% of the page height is a title,
    not a running header): only the lines whose repetition key was
    algorithmically confirmed as header/footer are dropped.
    """
    return [
        ln for ln in page_lines(page_payload)
        if repeated_line_key(ln["text"]) not in confirmed_keys
    ]


# ---------------------------------------------------------------------------
# evidence 1 + 2: TOC (pdf outline already extracted by L1.open_document)
# ---------------------------------------------------------------------------
def verify_pdf_toc(toc: list[dict], page_payloads: dict[int, dict],
                   confirmed_keys: set[str] | None = None) -> list[dict]:
    """Confirm each bookmark against the text of its target page (§5.3.1).

    A bookmark is *verified* when its title (or its numbered/lexical core)
    actually appears among the target page's lines. Unverified entries
    still propose a node — the outline order is usually right — but carry a
    lower confidence and keep ``pdf_toc`` as their only detection method.
    """
    verified: list[dict] = []
    for entry in toc or []:
        page_no = int(entry.get("page") or 0)
        title = _collapse_ws(str(entry.get("title") or ""))
        if page_no < 1 or not title:
            continue
        payload = page_payloads.get(page_no)
        lines = (_cleaned_page_lines(payload, confirmed_keys or set())
                 if payload else [])
        found = False
        for ln in lines:
            text = ln["text"]
            if text.casefold() == title.casefold():
                found = True
                break
            if title.casefold() in text.casefold() and _looks_structural(text):
                found = True
                break
        verified.append({
            "level": int(entry.get("level") or 1),
            "title": title,
            "page": page_no,
            "verified": found,
        })
    return verified


def detect_toc_pages(page_payloads: dict[int, dict],
                     first_n: int = 6) -> list[dict]:
    """Recognise a textual TOC in the first pages (§5.3 evidence 2).

    A page is a TOC page when >= 3 of its body lines end with a leader-dot /
    whitespace run followed by a page number (``CHAPTER III ..... 21``).
    Returns the parsed entries ``{"title", "page", "toc_page"}``.
    """
    entry_re = re.compile(
        r"^(?P<title>.+?)[\s.\u2025\u2026_]{2,}(?P<page>\d{1,4})$"
    )
    entries: list[dict] = []
    for page_no in sorted(page_payloads):
        if page_no > first_n:
            break
        hits = 0
        for ln in page_body_lines(page_payloads.get(page_no) or {}):
            m = entry_re.match(ln["text"])
            if not m:
                continue
            title = _collapse_ws(m.group("title").rstrip(". "))
            if not title or not _looks_structural(title):
                continue
            hits += 1
            entries.append({
                "title": title,
                "page": int(m.group("page")),
                "toc_page": page_no,
            })
        if hits >= 3:
            continue
        # drop the entries of pages that were not TOC pages after all
        entries = [e for e in entries if e["toc_page"] != page_no]
    return entries


# ---------------------------------------------------------------------------
# evidence 3-5: per-line heading evidence
# ---------------------------------------------------------------------------
def _looks_structural(text: str) -> bool:
    tokens = [t.strip(".,:;!?\"'()").casefold()
              for t in _collapse_ws(text).split()]
    return any(t in _STRUCTURE_WORDS for t in tokens)


def _upper_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _roman(text: str) -> bool:
    tokens = [t for t in _collapse_ws(text).split()]
    return any(ROMAN_RE.match(t.strip(".,:;()")) or "" for t in tokens
               if ROMAN_RE.match(t.strip(".,:;()")))


def typographic_score(line_text: str, font_size: float | None,
                      body_font_size: float | None,
                      bold_ratio: float = 0.0) -> float:
    """Evidence 3: heading typography -> [0, 1] strength (§5.3.3).

    Signals: size above body text (a big display line is a heading even
    when it is long — titles are often full sentences), bold weight,
    uppercase, short isolated line, roman/arabic chapter numbering,
    structural wording.
    """
    text = _collapse_ws(line_text)
    if not text:
        return 0.0
    score = 0.0
    ratio = 0.0
    if font_size and body_font_size:
        ratio = font_size / body_font_size
        if ratio >= 1.15:
            score += 0.35
        if ratio >= 1.5:
            score += 0.1
    if bold_ratio >= 0.5:
        score += 0.15
    upper = _upper_ratio(text)
    if upper >= 0.8:
        score += 0.15
    elif upper >= 0.5:
        score += 0.08
    if len(text) <= 60:
        score += 0.1
    if ROMAN_RE.match(text.split()[0].strip(".,:;()")) or _roman(text):
        score += 0.1
    if _looks_structural(text):
        score += 0.1
    return min(score, 1.0)


def lexical_match(line_text: str) -> float:
    """Evidence 4: configurable lexical patterns -> [0, 1] strength."""
    text = _collapse_ws(line_text)
    for pattern in LEXICAL_PATTERNS:
        if pattern.match(text):
            return 0.95
    first = text.split(":", 1)[0].strip() if ":" in text else text
    if _looks_structural(first) and len(text) <= 60:
        return 0.72
    return 0.0


def narrative_signals(lines: list[str]) -> dict:
    """Evidence 5: scene separators / datelines on one page.

    Returns ``{"separators": [line, ...], "datelines": [line, ...]}``.
    """
    separators: list[str] = []
    datelines: list[str] = []
    for raw in lines:
        text = _collapse_ws(raw)
        if not text:
            continue
        if text in _SEPARATORS or _SEPARATOR_RE.match(text):
            separators.append(text)
        elif _DATELINE_RE.match(text):
            datelines.append(text)
    return {"separators": separators, "datelines": datelines}


def normalize_title(source_label: str) -> str:
    """``CHAPTER III`` -> ``Chapter III`` (§5.3 ``normalized_title``)."""
    label = _collapse_ws(source_label)
    if not label:
        return label
    head, _, rest = label.partition(" ")
    special = {
        "ch.": "Ch.",
        "ch": "Ch",
    }
    return " ".join(
        [special.get(head.casefold(), head.capitalize()), rest]
        if rest else [special.get(head.casefold(), head.capitalize())]
    )


# ---------------------------------------------------------------------------
# node assembly
# ---------------------------------------------------------------------------
def _kind_for(title: str) -> str:
    """Map a chapter-ish label to a §5.3 node kind.

    ``PROLOGUE`` opens the narrative like a chapter (the front/back-matter
    kinds are reserved for the §5.3 cleanup pages — title, copyright, TOC,
    acknowledgements — which are not translated by default).
    """
    low = _collapse_ws(title).casefold()
    if low.startswith(("epilogue", "afterword")):
        return "back_matter"
    if low.startswith(("part", "book")):
        return "part"
    return "chapter"


def make_node(*, kind: str, source_label: str, start_page: int,
              end_page: int | None, start_char: int,
              end_char: int | None, confidence: float,
              detection_method: list[str], ordinal: int,
              status: str = "proposed") -> dict:
    """One §5.3 output node (schema of the PRD JSON block)."""
    node = {
        "node_id": new_node_id(),
        "parent_id": None,
        "kind": kind,
        "source_label": source_label,
        "normalized_title": normalize_title(source_label),
        "start_page": start_page,
        "end_page": end_page if (end_page is None or end_page >= start_page)
        else start_page,
        "start_char": start_char,
        "end_char": end_char,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "detection_method": detection_method,
        "status": status,
        "ordinal": ordinal,
    }
    return node


def build_nodes(page_payloads: dict[int, dict], toc: list[dict],
                text_model: Any = None) -> dict:
    """Detect the whole-book structure (§5.3 pipeline, evidence 1 -> 6).

    ``page_payloads`` maps 1-based page numbers to the stored page records.
    ``toc`` is the PDF outline (``l1.open_document()['toc']``). ``text_model``
    is an optional callable ``verdict(label, excerpt) -> (bool, confidence)``
    used ONLY for ambiguous nodes (evidence 6); when it is ``None`` the
    ambiguous nodes keep ``needs_review`` and a capped confidence, so the
    same pipeline works fully offline and the LLM pass can be added by the
    runner without changing this layer.

    Returns ``{"nodes": [...], "ambiguous": [...], "toc_pages": [...],
    "pdf_toc_verified": [...]}`` with nodes sorted by reading order.
    """
    page_numbers = sorted(page_payloads)
    if not page_numbers:
        return {"nodes": [], "ambiguous": [], "toc_pages": [],
                "pdf_toc_verified": []}
    total_pages = page_numbers[-1]

    # ---- global body font size (mode across pages, L1 pages only) --------
    sizes: dict[float, int] = {}
    for payload in page_payloads.values():
        for block in payload.get("blocks", []) or []:
            for s in block.get("font_sizes", []) or []:
                sizes[s] = sizes.get(s, 0) + 1
    body_font = max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else None

    # ---- header/footer repetition census (§5.3) --------------------------
    # Only *algorithmically confirmed* repeated lines are excluded from the
    # structure view: a chapter title that happens to sit high on the page
    # is NOT a running header. A key is confirmed only when ALL guards hold:
    #   (a) it appears in the header/footer zone of at least
    #       max(2, 40%) of the pages (PRD: "conferma su N pagine");
    #   (b) its pages SPAN at least 60% of the book AND cover at least 70%
    #       of that span: running headers run CONTINUOUSLY through the
    #       volume, chapter headings repeat with big gaps;
    #   (c) it is not set in display type: a line whose block font is >20%
    #       above the body font is a heading, never a running header.
    key_pages: dict[str, set[int]] = {}
    key_display: dict[str, bool] = {}
    for page_no in page_numbers:
        payload = page_payloads.get(page_no) or {}
        display_sizes: dict[str, bool] = {}
        for block in payload.get("blocks", []) or []:
            if block.get("kind") != "text":
                continue
            bsize = max(block.get("font_sizes") or [0.0]) or 0.0
            big = bool(body_font and bsize > body_font * 1.2)
            for lnb in block.get("lines", []):
                display_sizes[lnb.get("text", "")] = big
        seen: set[str] = set()
        for ln in page_lines(payload):
            if ln["zone"] not in ("top", "bottom"):
                continue
            key = repeated_line_key(ln["text"])
            if len(key) < 3:
                continue
            seen.add(key)
            if display_sizes.get(ln["text"], False):
                key_display[key] = True
        for key in seen:
            key_pages.setdefault(key, set()).add(page_no)
    repeat_threshold = max(2, math.ceil(0.4 * len(page_numbers)))
    total_span = max(page_numbers) - min(page_numbers) + 1
    confirmed_keys: set[str] = set()
    for key, pages in key_pages.items():
        if len(pages) < repeat_threshold or key_display.get(key):
            continue
        span = max(pages) - min(pages) + 1
        if span < 0.6 * total_span:
            continue
        if len(pages) / span < 0.7:  # gappy repetition = chapter headings
            continue
        confirmed_keys.add(key)

    # ---- evidence 1: PDF outline, verified -------------------------------
    toc_verified = verify_pdf_toc(toc, page_payloads, confirmed_keys)
    toc_pages_hit = {e["page"] for e in toc_verified}

    # ---- evidence 2: textual TOC in the first pages ----------------------
    textual_toc = detect_toc_pages(page_payloads)
    # The TOC page(s) themselves are recognised precisely so that their
    # lines are NOT taken as chapter starts (only the outline speaks for
    # them); the front-matter node covers those pages.
    toc_page_numbers = {e["toc_page"] for e in textual_toc}

    # ---- evidence 3-5: line-level pass -----------------------------------
    candidates: list[dict] = []
    scenes: list[dict] = []
    last_page = None
    for page_no in page_numbers:
        if page_no in toc_page_numbers:
            last_page = page_no
            continue  # the TOC page lists the chapters, it does not open them
        payload = page_payloads.get(page_no) or {}
        view = _cleaned_page_lines(payload, confirmed_keys)
        lines_text = [ln["text"] for ln in view]
        signals = narrative_signals(lines_text)
        for sep in signals["separators"]:
            scenes.append({"page": page_no, "label": sep, "kind": "scene"})
        # a dateline right after a separator (or first body line) opens a scene
        for dl in signals["datelines"]:
            idx = lines_text.index(dl) if dl in lines_text else -1
            if idx <= 1:
                scenes.append({"page": page_no, "label": dl, "kind": "scene"})

        blocks = [b for b in payload.get("blocks", []) or []
                  if b.get("kind") == "text"]
        for ln in view:
            text = ln["text"]
            lex = lexical_match(text)
            typo = 0.0
            bold = 0.0
            fsize: float | None = None
            if blocks:
                for b in blocks:
                    joined = " ".join(l["text"] for l in b.get("lines", []))
                    if text and text in joined:
                        fsize = (max(b.get("font_sizes") or [0.0]) or None)
                        bold = float(b.get("bold_ratio") or 0.0)
                        break
            if fsize is None:
                # OCR pages (and L1 fallbacks without font stats): typography
                # cannot be measured; only lexical/narrative evidence applies.
                fsize = None
            typo = typographic_score(text, fsize, body_font, bold)
            if lex <= 0.0 and typo < 0.45:
                continue
            if page_no in toc_pages_hit and lex <= 0.0 and typo < 0.7:
                continue  # the outline already speaks for this page
            candidates.append({
                "page": page_no, "label": text, "lexical": lex,
                "typographic": typo,
            })
        last_page = page_no

    # ---- merge the evidence into one ordered node list --------------------
    anchors: list[dict] = []

    def _merge(target: dict, conf: float, methods: list[str]) -> None:
        target["confidence"] = max(target["confidence"], conf)
        for m in methods:
            if m not in target["detection_method"]:
                target["detection_method"].append(m)

    def _push(page: int, label: str, conf: float, methods: list[str],
              kind: str | None = None) -> None:
        key = label.casefold()
        # merge the same textual anchor seen on the same or adjacent page
        # (PDF outline vs printed title: the bookmark is sometimes one page
        # off the typography, e.g. titles drawn high on the page)
        for a in anchors:
            if a["label"].casefold() == key and abs(a["page"] - page) <= 1:
                _merge(a, conf, methods)
                return
        anchors.append({
            "page": page, "label": label, "confidence": conf,
            "detection_method": list(methods), "kind": kind,
        })

    for entry in toc_verified:
        _push(entry["page"], entry["title"],
              0.97 if entry["verified"] else 0.88, ["pdf_toc"])
    by_title = {e["title"].casefold(): e for e in textual_toc}
    page_seen: dict[int, str] = {}
    # an outline entry whose title matches a *textual* TOC entry (evidence 2
    # reinforcing evidence 1) or a printed heading on the target page
    for anchor in anchors:
        if by_title.get(anchor["label"].casefold()):
            anchor["detection_method"] = sorted(set(
                anchor["detection_method"] + ["toc_page"]))
    for cand in candidates:
        methods: list[str] = []
        conf = 0.0
        if cand["lexical"] > 0.0:
            methods.append("regex")
            conf = max(conf, cand["lexical"])
        if cand["typographic"] >= 0.45:
            methods.append("font")
            conf = max(conf, min(0.9, cand["typographic"]))
        if conf <= 0.0:
            continue
        if by_title.get(cand["label"].casefold()):
            methods.append("toc_page")
            conf = max(conf, 0.9)
        first = page_seen.get(cand["page"])
        if first is not None:
            # several heading-ish lines on one page: ONE chapter start per
            # page (the first heading line in reading order opens it; the
            # others — subtitles, sub-headings — stay inside the node)
            _push(cand["page"], first, conf, [])
            continue
        page_seen[cand["page"]] = cand["label"]
        _push(cand["page"], cand["label"], conf, methods)
    anchors.sort(key=lambda a: (a["page"],))

    # ---- front matter (title/copyright/TOC pages) ------------------------
    front_end = anchors[0]["page"] - 1 if anchors else 0
    nodes: list[dict] = []
    if front_end >= 1:
        nodes.append(make_node(
            kind="front_matter", source_label="Front matter",
            start_page=1, end_page=front_end, start_char=0, end_char=None,
            confidence=0.6, detection_method=["heuristic"], ordinal=0))

    ordinal = 1
    scene_open: list[dict] = []
    for anchor in anchors:
        kind = anchor["kind"] or _kind_for(anchor["label"])
        node = make_node(
            kind=kind, source_label=anchor["label"],
            start_page=anchor["page"],
            end_page=None,  # closed below by the next anchor
            start_char=None, end_char=None,
            confidence=anchor["confidence"],
            detection_method=anchor["detection_method"], ordinal=ordinal)
        nodes.append(node)
        ordinal += 1

    # close the ranges (a node ends where the next one starts)
    for i, node in enumerate(nodes[1:], start=1):
        if node["kind"] in ("front_matter", "back_matter") and \
                nodes[i - 1]["kind"] in ("front_matter", "back_matter"):
            continue
        prev = nodes[i - 1]
        prev["end_page"] = node["start_page"] - 1 if \
            node["start_page"] > prev["start_page"] else node["start_page"]
    if nodes and nodes[-1]["end_page"] is None:
        nodes[-1]["end_page"] = total_pages
    # front matter closes where the first real anchor starts
    if len(nodes) >= 2 and nodes[0]["kind"] == "front_matter":
        nodes[0]["end_page"] = nodes[1]["start_page"] - 1

    # ---- optional part grouping (§5.3 parent_id) -------------------------
    # When >= 4 chapters share the same progressive label prefix the book is
    # usually split in volumes ("Book One / Chapter 1..."). The parts become
    # parent nodes of their chapters. With < 4 chapters the structure stays
    # flat (part labels would be pure noise).
    chapters = [n for n in nodes if n["kind"] == "chapter"]

    def _part_key(node: dict, prefix: str) -> str:
        rest = node["source_label"][len(prefix):].strip()
        token = rest.split(" ", 1)[0] if rest else ""
        return token.casefold()

    if len(chapters) >= 4:
        labels = [n["source_label"] for n in chapters]

        def _longest_shared_prefix(texts: list[str]) -> str:
            if not texts:
                return ""
            shared = texts[0]
            for t in texts[1:]:
                while not t.startswith(shared):
                    shared = shared[:-1]
                    if not shared:
                        return ""
                shared = shared.rstrip()
            return shared if shared.casefold() != texts[0].casefold() else ""

        shared = _longest_shared_prefix(labels)
        if shared:
            groups: dict[str, list[dict]] = {}
            for n in chapters:
                groups.setdefault(_part_key(n, shared), []).append(n)
            if len(groups) >= 2:
                insert_at = nodes.index(chapters[0])
                part_nodes: list[dict] = []
                ordered: list[dict] = []
                part_ordinal = 1
                for key in sorted(
                        groups, key=lambda k: groups[k][0]["start_page"]):
                    members = groups[key]
                    part = make_node(
                        kind="part", source_label=f"Part {part_ordinal}",
                        start_page=members[0]["start_page"],
                        end_page=members[-1]["end_page"],
                        start_char=None, end_char=None,
                        confidence=min(0.85, 0.6 + 0.05 * len(members)),
                        detection_method=["heuristic"], ordinal=0)
                    part_nodes.append(part)
                    for n in members:
                        n["parent_id"] = part["node_id"]
                        ordered.append(n)
                    part_ordinal += 1
                nodes[:] = (nodes[:insert_at] + part_nodes + ordered +
                            nodes[insert_at + len(chapters):])
                for i, n in enumerate(sorted(nodes, key=lambda x: (
                        x["start_page"], 0 if x["kind"] != "scene" else 1))):
                    n["ordinal"] = i
                ordinal = len(nodes)

    # ---- evidence 5 nodes: scenes become children of their chapter -------
    chapter_of_page: list[tuple[int, str]] = [
        (n["start_page"], n["node_id"]) for n in nodes
        if n["kind"] in ("chapter", "part")]
    for scene in scenes:
        parent = None
        for start_page, node_id in reversed(chapter_of_page):
            if scene["page"] >= start_page:
                parent = node_id
                break
        scene_open.append({
            "node": make_node(
                kind="scene", source_label=scene["label"],
                start_page=scene["page"], end_page=scene["page"],
                start_char=None, end_char=None, confidence=0.55,
                detection_method=["narrative"], ordinal=ordinal),
            "parent": parent,
        })
        ordinal += 1

    # ---- evidence 6: ambiguous nodes -> LLM (runner-supplied) ------------
    ambiguous: list[dict] = []
    for node in nodes:
        if node["confidence"] >= AMBIGUOUS_CONFIDENCE or \
                node["kind"] in ("front_matter", "back_matter"):
            continue
        payload = page_payloads.get(node["start_page"]) or {}
        excerpt_lines = [ln["text"]
                         for ln in page_body_lines(payload)[:12]]
        ambiguous.append({
            "node_id": node["node_id"],
            "label": node["source_label"],
            "page": node["start_page"],
            "excerpt": "\n".join(excerpt_lines)[:1200],
        })
    if text_model is not None:
        for amb in ambiguous:
            try:
                keep, conf = text_model(amb["label"], amb["excerpt"])
            except Exception:  # noqa: BLE001 - LLM outage must not kill us
                continue
            for node in nodes:
                if node["node_id"] == amb["node_id"]:
                    if keep:
                        node["confidence"] = round(max(node["confidence"],
                                                       conf), 4)
                        node["detection_method"] = sorted(set(
                            node["detection_method"] + ["llm_verify"]))
                    else:
                        nodes.remove(node)
                    break
        ambiguous = [a for a in ambiguous
                     if any(n["node_id"] == a["node_id"] for n in nodes)]
    else:
        # offline mode: cap the confidence of unverified nodes and leave
        # them flagged so the runner/UI can send them to the analysis model.
        for node in nodes:
            if node["confidence"] < AMBIGUOUS_CONFIDENCE and \
                    node["kind"] not in ("front_matter", "back_matter"):
                node["confidence"] = round(min(node["confidence"], 0.75), 4)

    return {
        "nodes": nodes,
        "scene_nodes": [s["node"] for s in scene_open],
        "scene_parents": {s["node"]["node_id"]: s["parent"]
                          for s in scene_open},
        "ambiguous": ambiguous,
        "toc_pages": textual_toc,
        "pdf_toc_verified": toc_verified,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# §5.3 cleanup: header/footer confirmation, dehyphenisation, preservation
# ---------------------------------------------------------------------------
def confirm_repeated_lines(repeated: list[dict], total_pages: int,
                           min_pages: int = MIN_REPEAT_PAGES) -> list[dict]:
    """Algorithmic confirmation of header/footer candidates (§5.3).

    ``repeated`` is the import report's ``repeated_headers_footers`` list
    (``{"text", "pages"}``). A candidate is confirmable when it appears on
    at least *min_pages* pages (PRD: "almeno N pagine") — the *deletion*
    still requires the explicit user confirmation (§15.1); this function
    only says which candidates the algorithm stands behind.
    """
    threshold = max(min_pages, int(0.4 * total_pages) or min_pages)
    confirmed = []
    for item in repeated or []:
        pages = int(item.get("pages") or 0)
        text = _collapse_ws(str(item.get("text") or ""))
        if not text:
            continue
        digits_only = bool(re.fullmatch(r"[#\s\-–—.,()\[\]]+", text))
        if digits_only and pages >= min_pages:
            confirmed.append({"text": text, "pages": pages})
        elif pages >= threshold:
            confirmed.append({"text": text, "pages": pages})
    return confirmed


def dehyphenate(text: str, mode: str = "ocr") -> str:
    """Join line-break hyphens conservatively (§5.3 cleanup rules).

    * ``mode="ocr"`` — OCR lines: the hyphen at the join was already removed
      by the OCR layer; remaining ``word- word`` joins get the hyphen
      re-attached WITHOUT space only when the left side is not a complete
      word-like token (heuristic: single letter or <= 2 chars), otherwise a
      space is kept (conservative: never merges a possibly-significant
      hyphen blindly).
    * ``mode="l1"`` — L1 records already kept the hyphen and joined without
      a space; the text is returned unchanged except whitespace cleanup.

    Em dashes (—) and ellipses (…) are NEVER touched in either mode.
    """
    text = _collapse_ws(text)
    if mode == "l1":
        return text
    # conservative OCR repair: "s- omething" -> "s-omething" only when the
    # left fragment cannot stand alone (1-2 letters); keep everything else.
    def _join(m: re.Match[str]) -> str:
        left = m.group(1)
        if len(left) <= 2:
            return f"{left}-{m.group(2)}"
        return f"{left}- {m.group(2)}"

    return re.sub(r"(\S+)-\s+(\S+)", _join, text)


def ambiguous_dehyphenation_candidates(text: str) -> list[str]:
    """Line-join spots left UNDECIDED by :func:`dehyphenate` (review queue).

    §5.3: "deyphenizzazione con review casi ambigui". When a ``word- word``
    join keeps both the hyphen and the space (the left fragment could be a
    complete word, so merging might destroy a significant hyphen) the spot
    is returned here so the review UI can list it; the text itself is never
    altered silently.
    """
    text = _collapse_ws(text)
    out: list[str] = []
    for m in re.finditer(r"(\S+)-\s+(\S+)", text):
        if len(m.group(1)) > 2:
            out.append(f"{m.group(1)}- {m.group(2)}")
    return out


PRESERVE_CHECKS: tuple[tuple[str, str], ...] = (
    ("em_dash", "\u2014"),
    ("en_dash", "\u2013"),
    ("ellipsis", "\u2026"),
    ("left_quote", "\u201c"),
    ("right_quote", "\u201d"),
)


def preservation_report(original: str, cleaned: str) -> dict:
    """§5.3: em dash / ellipsis / quotes must survive the cleaning step."""
    report: dict[str, object] = {"ok": True}
    for name, ch in PRESERVE_CHECKS:
        n_orig, n_clean = original.count(ch), cleaned.count(ch)
        if n_clean < n_orig:
            report["ok"] = False
        report[name] = {"original": n_orig, "cleaned": n_clean}
    return report


def roll_back(cleaned: str, snapshot: dict) -> str:
    """Restore a pre-cleaning text from a rollback snapshot (§5.3 preview).

    The snapshot is the ``{"text": ..., "sha256": ...}`` dict stored by the
    runner before applying deletions; the hash guarantees the snapshot is
    intact before it is trusted.
    """
    text = snapshot.get("text")
    if not isinstance(text, str):
        raise ValueError("rollback snapshot has no text")
    if snapshot.get("sha256") != sha256_json({"text": text}):
        raise ValueError("rollback snapshot hash mismatch")
    return text
