"""Pure CAT chunking layer (PRD §5.4) — no DB, no I/O.

Three responsibilities:

1. **First segmentation** (§5.4.2): the chapter's page-body lines become
   ``paragraph | dialogue | epigraph | letter | quotation | scene_break``
   paragraph segments.  Line joins are conservative (``structure.dehyphenate``
   preserves meaningful hyphens) and running headers/footers already
   algorithmically confirmed by the import are dropped.
2. **Second segmentation** (§5.4.3): sentences via the spaCy English parser,
   *without breaking*: dialogue with an external speech tag, a sentence with
   an unclosed parenthesis/quotation, lists/verse (kept whole), text with
   markup/placeholders, and multi-token proper names.
3. **LLM block planner** (§5.4.4-8): greedy packing of the chapter's segments
   into blocks of at most **16,384 TOTAL tokens** measured with the real
   tokenizer (tiktoken BPE; conservative estimate fallback), split as
   10-11k source + 2-2.5k glossary/TM reserve + 2-3k output reserve.  Each
   block exposes 1-2 preceding + 1 following segments as read-only context
   explicitly marked ``DO_NOT_TRANSLATE_CONTEXT``.  Segment IDs are stable
   (uuid5 of project+chapter+ordinal) and every segment carries a
   ``source_hash``.

Nothing here imports spaCy at module import time: the parser loads lazily
and the sentence splitter degrades to a regex fallback when the model is
absent (the fallback is deliberately dumber; ``en_core_web_sm`` is a hard
requirement for production, see requirements.txt).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Constants (PRD §5.4.4-5.4.6, rivisti 2026-09-21 su richiesta: input e
# output del modello entrambi a 16.384 token).
# ---------------------------------------------------------------------------
#: Hard cap for one LLM block = 16,384 source + 2,500 context + 16,384 output.
MAX_BLOCK_TOTAL_TOKENS = 16_384 + 2_500 + 16_384

#: Source budget inside one block: 16,384 source tokens.
SOURCE_BUDGET_MAX_TOKENS = 16_384

#: Reserved for glossary/TM/context payload (PRD: 2,000-2,500).
CONTEXT_RESERVE_TOKENS = 2_500

#: Reserved for output: 16,384 tokens.
OUTPUT_RESERVE_TOKENS = 16_384

#: Overlap: 1-2 preceding + 1 following segments, read-only (§5.4.6).
OVERLAP_PRECEDING = 2
OVERLAP_FOLLOWING = 1

#: Explicit label carried by every read-only overlap segment (§5.4.6).
DO_NOT_TRANSLATE_CONTEXT = "DO_NOT_TRANSLATE_CONTEXT"

#: Stable-ID namespace: uuid5(NAMESPACE_TRANS, "project:chapter:ordinal").
NAMESPACE_TRANS = uuid.uuid5(uuid.NAMESPACE_URL, "trans:segment:v1")


def segment_uid(project_id: str, chapter_id: str, ordinal: int) -> str:
    """Deterministic, stable segment ID (§5.4.7).

    Same project + chapter + ordinal always yields the same UUID, so a
    re-segmentation that produces the same segmentation keeps the segment
    identities (and the CAT references to them) intact.
    """
    return str(uuid.uuid5(NAMESPACE_TRANS, f"{project_id}:{chapter_id}:{ordinal}"))


def sha256_hex(text: str) -> str:
    """Content hash of a segment source (``translation_units.source_hash``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Real tokenizer (ADR-001 §3.4: the proxy exposes no tokenizer endpoint, so
# the adapter ships a local BPE tokenizer; the estimate is the fallback).
# ---------------------------------------------------------------------------
class _EstimatedTokenizer:
    """Conservative offline estimate (~4 char/token + punctuation split).

    Deliberately *over*-estimates English text a little so a block planned
    with the fallback never exceeds the real count of a BPE tokenizer.
    """

    name = "estimate-4char"

    _WORD_RE = re.compile(r"\s+|[^\s\w]+|\w+")

    def encode(self, text: str) -> list[int]:
        tokens: list[int] = []
        for chunk in self._WORD_RE.findall(text or ""):
            tokens.extend([0] * max(1, -(-len(chunk) // 3)))
        return tokens


class _TiktokenTokenizer:
    """Real BPE tokenizer (tiktoken, offline once the vocabulary is cached)."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken

        self._enc = tiktoken.get_encoding(encoding_name)
        self.name = f"tiktoken:{encoding_name}"

    def encode(self, text: str) -> list[int]:
        return self._enc.encode(text or "")


_TOKENIZER: Any | None = None
_TOKENIZER_FAILED = False


def get_tokenizer() -> Any:
    """Return the process-wide tokenizer (real BPE, estimate as fallback)."""
    global _TOKENIZER, _TOKENIZER_FAILED
    if _TOKENIZER is None and not _TOKENIZER_FAILED:
        try:
            _TOKENIZER = _TiktokenTokenizer()
        except Exception:  # noqa: BLE001 - offline box without the vocab
            _TOKENIZER_FAILED = True
    return _TOKENIZER if _TOKENIZER is not None else _EstimatedTokenizer()


def count_tokens(tokenizer: Any, text: str) -> int:
    """Token count of *text* with the given tokenizer."""
    return len(tokenizer.encode(text or ""))


# ---------------------------------------------------------------------------
# First segmentation (§5.4.2)
# ---------------------------------------------------------------------------
SCENE_BREAK_RE = re.compile(r"^[\*\#\•\.\s~\-—–]{3,}$")
EPIGRAPH_RE = re.compile(
    r"^[“\"']?(?:An old |Ancient |Old )?(?:saying|proverb|epigraph)\b",
    re.IGNORECASE,
)
LETTER_OPENING_RE = re.compile(
    r"^(?:Dear|My dear|Dearest|Darling|My own)\s+[A-Z]", re.MULTILINE
)
LETTER_CLOSING_RE = re.compile(
    r"(?:Yours|Sincerely|Farewell|Ever yours|With love)[,\s]",
)
PLACEHOLDER_RE = re.compile(r"\[\[[^\]]+\]\]|\{\{[^}]+\}\}|<[a-z/][^>]*>")
#: terminal punctuation (optionally followed by closing quotes/brackets)
_TERMINAL_TAIL = "\"'”’»\\)\\]\\}*"
TERMINAL_RE = re.compile(rf"[.!?…][{_TERMINAL_TAIL}]*$")
QUOTE_OPENERS = "“\"«‘’”"
DASH_OPENERS = "—–"

KINDS = (
    "paragraph",
    "dialogue",
    "epigraph",
    "letter",
    "quotation",
    "scene_break",
)


def classify_paragraph(text: str) -> str:
    """Kind of one first-level segment (§5.4.2 list)."""
    t = (text or "").strip()
    if not t:
        return "paragraph"
    if SCENE_BREAK_RE.match(t):
        return "scene_break"
    if EPIGRAPH_RE.match(t):
        return "epigraph"
    if LETTER_OPENING_RE.match(t) or LETTER_CLOSING_RE.search(t):
        return "letter"
    first = t[0]
    if first in QUOTE_OPENERS + "\"'":
        return "dialogue"
    if first in DASH_OPENERS:
        return "dialogue"
    if t.startswith("> "):
        return "quotation"
    return "paragraph"


def _has_markup(text: str) -> bool:
    return bool(PLACEHOLDER_RE.search(text))


#: A group of short lines set off by extra leading is verse or a list:
#: their line breaks are semantic and must survive (§5.4.3).  Erring on
#: the side of keeping the breaks is safe for CAT (the segment stays one
#: unit; internal newlines are preserved either way).
VERSE_MAX_LINE_LEN = 42


def _is_verse_group(group: list[str]) -> bool:
    if len(group) < 2:
        return False
    return all(len(line) <= VERSE_MAX_LINE_LEN for line in group)


_WS_RE = re.compile(r"[ \t\xa0]+")


def join_page_lines(lines: list[str], mode: str = "l1") -> str:
    """Join the wrapped lines of one page-block into a paragraph.

    Line-break hyphens are resolved conservatively (``structure.dehyphenate``:
    a meaningful hyphen is never merged silently).  Poetry/verse must not be
    passed here — verse blocks are kept line-per-segment by the caller.
    """
    from . import structure

    text = _WS_RE.sub(" ", " ".join(s.strip() for s in lines if s and s.strip()))
    return structure.dehyphenate(text.strip(), mode=mode)


def ends_terminal(text: str) -> bool:
    """True when *text* ends a sentence (paragraph continuation check)."""
    return bool(TERMINAL_RE.search((text or "").rstrip()))


def build_chapter_paragraphs(
    pages: list[dict],
    confirmed_keys: set[str] | None = None,
) -> list[dict]:
    """First-level segmentation of one chapter (§5.4.2).

    *pages* is a list of ``{"page_number": int, "mode": "l1"|"ocr",
    "lines": [{"text", "zone"}]}`` in reading order (the caller selects the
    chapter's page range and drops the confirmed header/footer keys).

    Returns paragraph records ``{"text", "kind", "page", "has_markup"}``.
    Page-spanning paragraphs are re-joined when the previous page clearly
    continues (no terminal punctuation) — never inside dialogue turns
    (an unfinished *spoken* turn is a stylistic fact, not a line-wrap).
    """
    confirmed_keys = confirmed_keys or set()
    from . import structure as _structure

    paras: list[dict] = []
    for page in pages:
        mode = page.get("mode") or "l1"
        # Group consecutive lines into visual paragraphs.  Zone is
        # deliberately NOT trusted here (a chapter's first dialogue line
        # can sit in the geometric top band — the t_6db59f2b lesson):
        # only the algorithmically confirmed header/footer keys are
        # dropped.  A new group opens on:
        #   * a vertical gap well above the median line height;
        #   * a line starting with an opening quote (English typography
        #     starts a dialogue turn at line start, §5.4.2 "dialoghi");
        #   * a scene-break line (its own group).
        body: list[tuple[str, float | None, float | None]] = []
        for line in page["lines"]:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            if _structure.repeated_line_key(text) in confirmed_keys:
                continue  # confirmed running header/footer (§5.3)
            bbox = line.get("bbox") or None
            y0 = y1 = None
            if bbox and len(bbox) == 4:
                try:
                    y0, y1 = float(bbox[1]), float(bbox[3])
                except (TypeError, ValueError):
                    y0 = y1 = None
            body.append((text, y0, y1))
        # leading census over the body lines only (header/footer gaps must
        # not inflate the estimate: their distance to the text block is
        # huge).  Q1 is robust to verse/paragraph gaps outnumbering the
        # normal line steps.
        distances = sorted(
            pair[1][1] - pair[0][1]
            for pair in zip(body, body[1:])
            if pair[0][1] is not None and pair[1][1] is not None
        )
        normal_leading = \
            distances[len(distances) // 4] if distances else 0.0

        groups: list[list[str]] = []
        current: list[str] = []
        prev_y0: float | None = None
        for text, y0, _y1 in body:
            new_group = False
            if current and y0 is not None and prev_y0 is not None and \
                    normal_leading > 0 and \
                    (y0 - prev_y0) > 1.45 * normal_leading:
                new_group = True  # extra leading: visual paragraph break
            # a line opening with a quote char starts a new dialogue turn:
            # print does not insert blank lines between turns.
            if current and text[0] in QUOTE_OPENERS + '"\'':
                new_group = True
            if new_group:
                groups.append(current)
                current = []
            if SCENE_BREAK_RE.match(text):
                if current:
                    groups.append(current)
                    current = []
                groups.append([text])
                current = []
                prev_y0 = y0
                continue
            current.append(text)
            prev_y0 = y0
        if current:
            groups.append(current)

        page_paras: list[dict] = []
        for group in groups:
            if len(group) == 1 and SCENE_BREAK_RE.match(group[0]):
                text = group[0]
            elif _is_verse_group(group):
                # verse/list lines: joined with hard breaks, never merged
                # into prose (§5.4.3: lists and poetry are not split)
                text = "\n".join(group)
            else:
                text = join_page_lines(group, mode=mode)
            page_paras.append({
                "text": text,
                "kind": classify_paragraph(text),
                "page": page.get("page_number"),
                "has_markup": _has_markup(text),
                "is_verse": _is_verse_group(group),
            })

        # cross-page continuation: previous page ended without terminal
        # punctuation and the new page opens with a continuation of the
        # same kind (never when the new page opens a new dialogue turn,
        # a scene break or a verse block).
        if paras and page_paras:
            last = paras[-1]
            first = page_paras[0]
            if (
                last["kind"] == first["kind"]
                and last["kind"] in ("paragraph", "letter")
                and not last.get("is_verse")
                and not first.get("is_verse")
                and not ends_terminal(last["text"])
                and not SCENE_BREAK_RE.match(first["text"])
                and not first["text"][:1].isupper()
            ):
                last["text"] = join_page_lines(
                    [last["text"], first["text"]], mode=mode)
                page_paras = page_paras[1:]
        paras.extend(page_paras)
    return [p for p in paras if p["text"].strip()]


# ---------------------------------------------------------------------------
# Second segmentation (§5.4.3): sentences, with anti-break rules
# ---------------------------------------------------------------------------
SPEECH_VERBS = {
    "say", "ask", "reply", "whisper", "shout", "murmur", "cry", "answer",
    "remark", "add", "begin", "continue", "think", "call", "declare",
    "mutter", "repeat", "observe", "sigh", "exclaim", "suggest", "offer",
    "explain", "interrupt", "demand", "admit", "announce", "order", "urge",
}
#: bracket pairs that must stay balanced inside one sentence (typographic
#: quotation marks are deliberately NOT here: quoted speech may span turns).
_OPEN_BRACKETS = "([{«"
_CLOSE_BRACKETS = ")]}»"
_NLP: Any | None = None
_NLP_FAILED = False


def get_nlp() -> Any | None:
    """Lazy spaCy ``en_core_web_sm`` loader (``None`` when unavailable)."""
    global _NLP, _NLP_FAILED
    if _NLP is None and not _NLP_FAILED:
        try:
            import spacy

            _NLP = spacy.load("en_core_web_sm")
        except Exception:  # noqa: BLE001 - model missing on this box
            _NLP_FAILED = True
    return _NLP


def _bracket_balance(text: str) -> int:
    """Net bracket depth of *text* (escaped markup ignored)."""
    depth = 0
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in _OPEN_BRACKETS:
            depth += 1
        elif ch in _CLOSE_BRACKETS:
            depth -= 1
    return depth


def _ends_soft(text: str) -> bool:
    """Sentence ending in a connective: the next 'sentence' is a fragment."""
    return bool(re.search(r"[,:;—–]$", text.rstrip()))


def _is_protected_boundary(doc: Any, sent_idx: int, sent_of: dict[int, int],
                           spans: list[Any]) -> bool:
    """True when the boundary between sentence *sent_idx* and the next must
    NOT stand (§5.4.3 anti-break rules)."""
    a = spans[sent_idx]
    b = spans[sent_idx + 1]
    # (a) unclosed parenthesis/quotation in the first part
    if _bracket_balance(a.text) > 0:
        return True
    # (b) odd number of hard quotes: the quoted utterance is still open
    if a.text.count('"') % 2 == 1:
        return True
    # (c) the first part ends in a connective (tag of parlato, '…,' ':' ecc.)
    if _ends_soft(a.text):
        return True
    # (d) speech tag outside its quote: verb in the next part governed by a
    # head in this one ('"…?" asked John' style attribution)
    for tok in b:
        if tok.lemma_ in SPEECH_VERBS and sent_of.get(tok.head.i, sent_idx + 1) == sent_idx:
            return True
    # (e) speech verb in this part whose head lives in the next one
    for tok in a:
        if tok.lemma_ in SPEECH_VERBS and sent_of.get(tok.head.i, sent_idx) != sent_idx:
            return True
    # (f) a multi-token proper name spans the boundary
    last = doc[a.end - 1]
    nxt = b[0]
    if last.ent_iob_ == "B" and nxt.ent_iob_ == "I":
        return True
    if last.ent_iob_ == "I" and nxt.ent_iob_ == "I":
        return True
    return False


_REGEX_SENT_RE = re.compile(r"(?<=[.!?…])\s+")


def _regex_sentences(text: str) -> list[str]:
    """Fallback splitter (no spaCy): punctuation-based, unprotective."""
    return [s for s in (p.strip() for p in _REGEX_SENT_RE.split(text)) if s]


def split_sentences(text: str, protect: bool = True,
                    nlp: Any | None = None) -> list[str]:
    """Second-level segmentation (§5.4.3).

    Sentences via the English parser; every soft boundary that would break
    a protected construct is dissolved by merging the two parts back
    together (the *text is preserved verbatim*, only segmentation changes).
    """
    if nlp is None:
        nlp = get_nlp()
    if nlp is None:
        return [text.strip()] if protect and _bracket_balance(text) else \
            _regex_sentences(text)
    doc = nlp(text)
    spans = list(doc.sents)
    if not spans:
        return [text.strip()] if text.strip() else []
    sent_of: dict[int, int] = {}
    for k, span in enumerate(spans):
        for tok in span:
            sent_of[tok.i] = k
    out: list[str] = []
    start = 0
    for i in range(len(spans)):
        last = i == len(spans) - 1
        if not last and protect and _is_protected_boundary(
                doc, i, sent_of, spans):
            continue  # boundary dissolved: this sentence merges into the next
        out.append(doc.text[spans[start].start_char:spans[i].end_char].strip())
        start = i + 1
    return [s for s in out if s]


def segment_paragraph(p: dict, sentences_per_segment: int = 1) -> list[dict]:
    """Second-level segmentation of ONE first-level segment (§5.4.2-3).

    ``paragraph``/``dialogue``/``letter`` are sentence-split with the
    anti-break protections; ``epigraph``/``quotation``/``scene_break`` and
    em-dash dialogue turns stay whole (lists/verse are never split).
    ``sentences_per_segment`` (>1) merges consecutive sentences into one
    segment up to that count (user preference, §5.4 UI): boundaries
    protected by the anti-break rules still dissolve the merge, so a
    protected construct is never split across segments.
    """
    kind = p["kind"]
    text = p["text"]
    base = {
        "kind": kind,
        "page": p.get("page"),
        "has_markup": p.get("has_markup", _has_markup(text)),
        "is_verse": p.get("is_verse", False),
    }
    splittable = kind in ("paragraph", "dialogue", "letter") and \
        not text.lstrip().startswith(tuple(DASH_OPENERS)) and \
        not base["is_verse"]
    if not splittable:
        return [{**base, "text": text}] if text.strip() else []
    parts = split_sentences(text) or [text]
    if sentences_per_segment > 1:
        merged: list[str] = []
        for i in range(0, len(parts), sentences_per_segment):
            merged.append(" ".join(parts[i:i + sentences_per_segment]))
        parts = merged
    return [{**base, "text": part} for part in parts if part.strip()]


def segment_chapter_text(paragraphs: list[dict],
                         sentences_per_segment: int = 1) -> list[dict]:
    """Full two-pass segmentation of one chapter (§5.4.2 + §5.4.3).

    Ordinals are 1-based and chapter-local, in reading order.  Output rows
    are ready for :func:`plan_blocks` and for ``translation_units``.
    """
    units: list[dict] = []
    for para in paragraphs:
        for part in segment_paragraph(para, sentences_per_segment):
            units.append(part)
    for i, unit in enumerate(units, start=1):
        unit["ordinal"] = i
    return units


def with_ids(units: list[dict], project_id: str, chapter_id: str) -> list[dict]:
    """Attach stable segment IDs + source hashes (§5.4.7)."""
    for unit in units:
        unit["segment_id"] = segment_uid(project_id, chapter_id,
                                         unit["ordinal"])
        unit["source_hash"] = sha256_hex(unit["text"])
    return units


# ---------------------------------------------------------------------------
# LLM block planner (§5.4.4-8)
# ---------------------------------------------------------------------------
def _planner_budgets(source_budget_max: int = SOURCE_BUDGET_MAX_TOKENS,
                     context_reserve: int = CONTEXT_RESERVE_TOKENS,
                     output_reserve: int = OUTPUT_RESERVE_TOKENS) -> dict:
    return {
        "total_limit": MAX_BLOCK_TOTAL_TOKENS,
        "source_budget_max": source_budget_max,
        "context_reserve": context_reserve,
        "output_reserve": output_reserve,
        "source_available": min(
            source_budget_max,
            MAX_BLOCK_TOTAL_TOKENS - context_reserve - output_reserve,
        ),
    }


def plan_blocks(units: list[dict], tokenizer: Any, *,
                source_budget_max: int = SOURCE_BUDGET_MAX_TOKENS,
                context_reserve: int = CONTEXT_RESERVE_TOKENS,
                output_reserve: int = OUTPUT_RESERVE_TOKENS) -> list[dict]:
    """Greedy block planner (§5.4.4-5).

    Packs segments in reading order while the SOURCE portion fits into
    ``16,384 - context_reserve - output_reserve`` tokens; every block
    therefore satisfies the TOTAL budget with the reserves set aside
    (glossario/TM 2-2.5k, output 2-3k).  A single segment larger than the
    available source budget gets a block of its own (flagged) instead of
    being split — breaking it would violate §5.4.3.
    """
    budgets = _planner_budgets(source_budget_max, context_reserve,
                               output_reserve)
    blocks: list[dict] = []
    current: list[dict] = []
    current_tokens = 0

    def _close() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        blocks.append({
            "block_index": len(blocks),
            "segment_ids": [u["segment_id"] for u in current],
            "token_totals": {
                "source": current_tokens,
                "context_reserve": budgets["context_reserve"],
                "output_reserve": budgets["output_reserve"],
                "total": current_tokens + budgets["context_reserve"]
                + budgets["output_reserve"],
            },
            "oversized_source": current_tokens > budgets["source_available"],
        })
        current = []
        current_tokens = 0

    for unit in units:
        tokens = count_tokens(tokenizer, unit["text"])
        if current and current_tokens + tokens > budgets["source_available"]:
            _close()
        current.append(unit)
        current_tokens += tokens
    _close()

    order = [u["segment_id"] for u in units]
    for block in blocks:
        seg_ids = block["segment_ids"]
        first = order.index(seg_ids[0])
        last = order.index(seg_ids[-1])
        preceding = order[max(0, first - OVERLAP_PRECEDING):first]
        following = order[last + 1:last + 1 + OVERLAP_FOLLOWING]
        block["overlap"] = {
            "marker": DO_NOT_TRANSLATE_CONTEXT,
            "preceding_segment_ids": preceding,
            "following_segment_ids": following,
        }
    return blocks


def iter_overlap_texts(units: list[dict], block: dict) -> Iterator[dict]:
    """The read-only overlap rows of *block* (§5.4.6)."""
    by_id = {u["segment_id"]: u for u in units}
    for key in ("preceding_segment_ids", "following_segment_ids"):
        for seg_id in block["overlap"][key]:
            unit = by_id[seg_id]
            yield {
                "segment_id": seg_id,
                "role": "preceding" if key == "preceding_segment_ids"
                else "following",
                "marker": DO_NOT_TRANSLATE_CONTEXT,
                "text": unit["text"],
            }


def serialize_block(units: list[dict], block: dict, tokenizer: Any) -> dict:
    """The prompt-ready payload of one block (§5.4.6-8).

    The overlap segments are marked ``DO_NOT_TRANSLATE_CONTEXT`` at payload
    level AND per item: they travel read-only, never to be translated.
    Segment rows keep their stable IDs in order so the response can be
    checked for missing/duplicated/reordered IDs (§5.4.8, done by the
    translation adapter in a later phase).
    """
    by_id = {u["segment_id"]: u for u in units}
    payload = {
        "block_index": block["block_index"],
        "instruction": (
            "Translate every segment listed in 'segments' from EN to IT. "
            f"Segments marked '{DO_NOT_TRANSLATE_CONTEXT}' are context "
            "only: return them unchanged. Return one translation per "
            "segment_id, in the same order, keeping placeholders intact."
        ),
        "context": {
            "marker": DO_NOT_TRANSLATE_CONTEXT,
            "items": list(iter_overlap_texts(units, block)),
        },
        "segments": [
            {"segment_id": seg_id, "source_text": by_id[seg_id]["text"]}
            for seg_id in block["segment_ids"]
        ],
        "token_budget": {**block["token_totals"], "limit":
                         MAX_BLOCK_TOTAL_TOKENS},
    }
    total = count_tokens(tokenizer, repr(payload))
    # `total` is informational (payload overhead); the *budgeted* total that
    # must satisfy §5.4.4 is the planner's, reserves included.
    payload["token_budget"]["total"] = block["token_totals"]["total"]
    payload["token_budget"]["payload_overhead_tokens"] = max(
        0, total - block["token_totals"]["total"])
    return payload


def verify_blocks(blocks: list[dict]) -> dict:
    """§5.4.4 verification: every block within the TOTAL token budget."""
    limit = MAX_BLOCK_TOTAL_TOKENS
    totals = [b["token_totals"]["total"] for b in blocks]
    return {
        "blocks": len(blocks),
        "limit": limit,
        "max_block_total": max(totals, default=0),
        "all_within_budget": all(t <= limit for t in totals),
        "oversized_blocks": [
            b["block_index"] for b in blocks
            if b["token_totals"]["total"] > limit
        ],
    }
