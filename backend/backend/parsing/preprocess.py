"""Pure English preprocessing layer (PRD §5.5).

Turns a plain-text chapter/segment into the normalised form the translation
prompt sees, without destroying anything: the original text and the
normalised text coexist and every transformation is recorded in a reversible
map, so ``restore()`` reconstructs the source exactly.

What it does (§5.5):

* Saxon genitive: only a POS-classified possessive ``'s`` (``tag=POS`` whose
  head is a proper noun or an NP) is normalised to the internal control
  token ``[[POSS:John]] 's``; ``Mary's late`` (``is``), ``Mary's been``
  (``has``) and ``Let's`` (``us``) are left untouched (§5.5 table, §15.2).
* Pronoun ``I``: the exact isolated token ``I`` analysed as a personal
  pronoun (PRP) is never a proper name (§5.5 hard rule) — it is filtered
  out of termbase/entity candidates by :func:`is_isolated_I` /
  :func:`filter_I_candidates`, while ``Icarus``, ``Unit I`` and ``I-5`` stay
  valid when the parser says so.
* Protected normalisations: Unicode NFC, controlled quote/apostrophe
  normalisation, URL/email/number/date placeholders, NBSP and italic
  preservation (no whitespace is rewritten beyond the recorded chars).

The layer is pure: no DB access, no network, no model downloads. spaCy
(``en_core_web_sm``, ADR-001) is loaded lazily so importing the module in a
bare environment never fails (tests use ``importorskip``).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Control tokens (§5.5.3): internal, reversible, never visible in the final
# translation. T22's post-translation step removes them and validates that
# none remain (strip_control_tokens / validate_no_control_tokens below).
# ---------------------------------------------------------------------------
POSS_PREFIX = "[[POSS:"
POSS_SUFFIX = "]]"
#: The full normalised possessive construction, as in the §5.5 table row
#: ``Mary's coat`` → ``[[POSS:Mary]] 's coat`` (control token + space).
POSS_TEMPLATE = "[[POSS:{owner}]] 's"

_CONTROL_TOKEN_RE = re.compile(r"\[\[[A-Z_]+:[^\]]*\]\]")


# ---------------------------------------------------------------------------
# Transform map (§5.5.1: "Ogni token modificato riceve una mappa di
# trasformazione reversibile")
# ---------------------------------------------------------------------------
@dataclass
class Transform:
    """One reversible transformation applied to the source text."""

    kind: str  # "possessive" | "quote" | "placeholder" | "unicode"
    start: int  # offset in the ORIGINAL text
    end: int  # offset in the ORIGINAL text (exclusive)
    original: str
    replacement: str
    meta: dict = field(default_factory=dict)


@dataclass
class PreprocessResult:
    """Original + normalised text + the reversible transform map."""

    original: str
    normalized: str
    transforms: list[Transform] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-safe serialisation (persisted next to the segment)."""
        return {
            "original": self.original,
            "normalized": self.normalized,
            "transforms": [
                {
                    "kind": t.kind,
                    "start": t.start,
                    "end": t.end,
                    "original": t.original,
                    "replacement": t.replacement,
                    "meta": t.meta,
                }
                for t in self.transforms
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PreprocessResult":
        return cls(
            original=data["original"],
            normalized=data["normalized"],
            transforms=[
                Transform(
                    kind=t["kind"],
                    start=t["start"],
                    end=t["end"],
                    original=t["original"],
                    replacement=t["replacement"],
                    meta=t.get("meta") or {},
                )
                for t in data.get("transforms", [])
            ],
        )


# ---------------------------------------------------------------------------
# spaCy loading (lazy; ADR-001 model)
# ---------------------------------------------------------------------------
_nlp = None


def get_nlp():
    """Load ``en_core_web_sm`` once per process (lazy, local-only)."""
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load("en_core_web_sm")
    return _nlp


# ---------------------------------------------------------------------------
# Saxon genitive (§5.5.2)
# ---------------------------------------------------------------------------
def find_possessives(text: str) -> list[dict]:
    """Find possessive ``'s`` constructions via POS/dependency parse.

    Only ``tag=POS`` (the possessive particle) whose head is a proper noun
    (``PROPN``) or a noun (NP head, ``NOUN``) is possessive. When ``'s`` is
    tagged ``PRP`` — ``Mary's late`` (is), ``Mary's been`` (has),
    ``Let's`` (us) — it is not a possessive and stays untouched, per the
    §5.5 table.
    """
    doc = get_nlp()(text)
    out: list[dict] = []
    for tok in doc:
        if tok.tag_ != "POS":
            continue
        owner = tok.head
        # §5.5.2: normalise only when the possessor is a proper noun or an
        # NP head DEPENDENT on the possessed noun that follows. The
        # ``dep=poss`` + following-noun-head pair excludes the copula
        # reading ("Mary's late" = Mary is late), where 's still carries
        # tag=POS but Mary is the sentence subject, not a possessor.
        if owner.pos_ not in ("PROPN", "NOUN"):
            continue
        possessed = owner.head
        if owner.dep_ != "poss" or possessed.i <= owner.i:
            continue
        if possessed.pos_ not in ("NOUN", "PROPN"):
            continue
        # The owner is the whole NP prefix of the chunk ("Mr. Dashwood",
        # "the Captain"), not its head token alone: start at the chunk
        # start, end at the owner token (the chunk also contains "'s" +
        # possessed, which must stay outside the owner span).
        ostart, oend = owner.idx, owner.idx + len(owner.text)
        for chunk in doc.noun_chunks:
            if chunk.start <= owner.i < chunk.end:
                ostart = min(chunk.start_char, owner.idx)
                oend = owner.idx + len(owner.text)
                break
        out.append({
            "owner": text[ostart:oend],
            "owner_start": ostart,
            "owner_end": oend,
            "apos_start": tok.idx,
            "apos_end": tok.idx + len(tok.text),
        })
    return out


# ---------------------------------------------------------------------------
# Pronoun I (§5.5 hard rule)
# ---------------------------------------------------------------------------
def is_isolated_I(token_text: str, pos: str | None = None) -> bool:
    """True for the exact isolated ``I`` analysed as a personal pronoun.

    The hard rule (§5.5): the exact token ``I`` surrounded by
    punctuation/whitespace and parsed as ``PRP`` is never a proper name.
    Anything else — ``Icarus``, ``Unit I`` as a unit label, ``I-5`` — stays
    valid; when no POS is supplied the conservative surface test applies
    (exact single ``I``).
    """
    if token_text != "I":
        return False
    if pos is not None:
        return pos == "PRP"
    return True


def _candidate_surface(cand: dict | object) -> tuple[str, str | None]:
    """(text, pos) of a candidate, dict (NER item) or object (cluster)."""
    if isinstance(cand, dict):
        text = (cand.get("text") or cand.get("name") or "")
        return text.strip(), cand.get("pos")
    text = (getattr(cand, "canonical_source", None)
            or getattr(cand, "text", "") or "")
    return text.strip(), getattr(cand, "pos", None)


def filter_I_candidates(
    candidates: list[dict] | list,
) -> tuple[list, list]:
    """Split entity/termbase candidates into (kept, dropped-I).

    Drops every candidate whose surface is exactly the isolated pronoun
    ``I`` (§5.5 hard rule: never in termbase or entity lists); keeps
    everything else, including ``Unit I``/``Icarus``/``I-5``. Accepts dict
    items (``text``/``name`` + optional ``pos``) or objects with a
    ``canonical_source``/``text`` attribute (e.g. ``ProposedEntity``).
    """
    kept: list = []
    dropped: list = []
    for cand in candidates:
        text, pos = _candidate_surface(cand)
        if is_isolated_I(text, pos):
            dropped.append(cand)
        else:
            kept.append(cand)
    return kept, dropped


# ---------------------------------------------------------------------------
# Placeholders (§5.5.4): URLs, emails, numbers, dates
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 1805, 3.14, 1,000, 12:30, 12/02/1897 — not letters, so "Unit I" survives.
_NUM_RE = re.compile(
    r"(?<![\w.])(?:\d{1,2}:\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,3}"
    r"(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)(?![\w])"
)


def _replay_to_offset(original: str, transforms: list[Transform],
                      orig_off: int) -> int:
    """Length of ``original[:orig_off]`` after the recorded transforms.

    Every recorded transform lies entirely before ``orig_off`` (the caller
    only rewrites already-scanned regions), so the offset of the next
    match in the rewritten string is its original offset plus the net size
    change introduced by everything before it.
    """
    delta = 0
    for t in transforms:
        if t.end <= orig_off:
            delta += len(t.replacement) - len(t.original)
    return orig_off + delta


def build_placeholders(
    text: str,
    transforms: list[Transform] | None = None,
) -> tuple[str, list[Transform]]:
    """Replace URL/email/number/date literals with stable placeholders.

    §5.5.4: the placeholder must survive translation and be restored
    afterwards; the transform map records the exact original span so
    ``restore()`` is lossless. ``transforms`` (optional) carries the
    transforms already applied to ``text`` so recorded offsets stay in
    ORIGINAL-text coordinates.
    """
    done: list[Transform] = list(transforms or [])
    out: list[Transform] = []
    result = text
    # Ordered longest-literal first so URLs/emails beat bare numbers.
    for regex, kind in ((_URL_RE, "url"), (_EMAIL_RE, "email"),
                        (_NUM_RE, "number")):
        pos = 0
        while True:
            m = regex.search(result, pos)
            if m is None:
                break
            literal = m.group(0)
            # The scan always resumes after the replacement, so every
            # earlier transform is fully before this match.
            orig_start = m.start() - sum(
                len(t.replacement) - len(t.original)
                for t in done if t.end <= m.start()
            )
            idx = sum(1 for t in done + out if t.kind == kind) + 1
            ph = f"⟦{kind.upper()}{idx}⟧"
            out.append(Transform(
                kind=kind,
                start=orig_start,
                end=orig_start + len(literal),
                original=literal,
                replacement=ph,
                meta={},
            ))
            result = result[:m.start()] + ph + result[m.end():]
            pos = m.start() + len(ph)
    return result, out


# ---------------------------------------------------------------------------
# Controlled quote/apostrophe normalisation (§5.5.4)
# ---------------------------------------------------------------------------
_QUOTE_MAP = {
    "“": '"',
    "”": '"',
    "„": '"',
    "‘": "'",
    "’": "'",
}


def normalise_quotes(text: str) -> tuple[str, list[Transform]]:
    """Typographic quotes/apostrophes → ASCII, recorded and reversible.

    NBSP (\u00a0), guillemets and every other char pass through untouched
    (§5.5.4: preservation of protected whitespace and layout).
    """
    transforms: list[Transform] = []
    out_chars: list[str] = []
    for i, ch in enumerate(text):
        rep = _QUOTE_MAP.get(ch)
        if rep is not None:
            transforms.append(Transform(
                kind="quote", start=i, end=i + 1, original=ch,
                replacement=rep, meta={},
            ))
            out_chars.append(rep)
        else:
            out_chars.append(ch)
    return "".join(out_chars), transforms


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------
def preprocess(text: str, *, use_spacy: bool = True) -> PreprocessResult:
    """Normalise ``text`` per §5.5 and record every change reversibly.

    Order matters: NFC first (so the parser sees composed chars), then
    quotes (so the genitive parser sees ``'s``), then the possessive
    analysis, then placeholders (so URL/number literals are never part of
    a possessive span).
    """
    original = text

    # 1. Unicode NFC (§5.5.4) — recorded as one whole-run transform.
    nfc = unicodedata.normalize("NFC", text)
    unicode_transforms: list[Transform] = []
    if nfc != text:
        unicode_transforms.append(Transform(
            kind="unicode", start=0, end=len(text),
            original=text, replacement=nfc, meta={"form": "NFC"},
        ))
    text = nfc

    # 2. Controlled quote normalisation (typographic → ASCII).
    text, quote_transforms = normalise_quotes(text)
    qmap = {t.start: t.original for t in quote_transforms}

    # 3. Saxon genitive: parser-driven decision, character-exact splice.
    poss_transforms: list[Transform] = []
    if use_spacy:
        try:
            marks = find_possessives(text)
        except Exception as exc:  # noqa: BLE001 — spaCy/model missing
            # fix 2026-09-18: il degrado non deve essere silenzioso — senza
            # spaCy il genitivo sassone §5.5 non viene mai marcato e la
            # traduzione perde il vincolo, senza nessun segnale nei log.
            import logging

            logging.getLogger(__name__).warning(
                "spaCy/posessive finder unavailable (%s): genitivo sassone "
                "§5.5 NON marcato per questo blocco", exc,
            )
            marks = []
        if marks:
            text, poss_transforms, consumed_quotes = _apply_possessives(
                text, marks, qmap)
            # The possessive's recorded original already carries the
            # user's apostrophe: the quote transform inside those spans
            # must not survive (restore would corrupt it).
            quote_transforms = [t for t in quote_transforms
                                if t.start not in consumed_quotes]

    # 4. Placeholders for URL/email/number/date literals (offsets stay in
    #    ORIGINAL coordinates: build_placeholders rebases through the
    #    transforms recorded so far).
    text, ph_transforms = build_placeholders(
        text, transforms=[*unicode_transforms, *quote_transforms,
                          *poss_transforms])

    return PreprocessResult(
        original=original,
        normalized=text,
        transforms=[*unicode_transforms, *quote_transforms,
                    *poss_transforms, *ph_transforms],
    )


def _apply_possessives(
    text: str, marks: list[dict], qmap: dict[int, str] | None = None,
) -> tuple[str, list[Transform], set[int]]:
    """Rewrite each possessive ``X's`` to ``[[POSS:X]] 's`` (§5.5.3).

    Splices right-to-left so recorded offsets stay valid. ``qmap`` maps a
    position to the pre-normalisation char (curly apostrophe): the
    possessive's recorded ``original`` keeps it, so ``restore()`` returns
    the true source without re-applying the quote transform inside the
    span (the returned set lists the covered quote positions).
    """
    qmap = qmap or {}
    transforms: list[Transform] = []
    consumed: set[int] = set()
    for mark in sorted(marks, key=lambda m: m["apos_start"], reverse=True):
        owner = mark["owner"]
        start, apos, end = (mark["owner_start"], mark["apos_start"],
                            mark["apos_end"])
        # The span owner..apos_end is exactly "X's"; the apostrophe the
        # user wrote (straight or curly) comes from qmap when the quote
        # stage normalised it, so the recorded original is the true
        # source surface.
        apos_char = qmap.get(apos, text[apos])
        original = f"{text[start:apos]}{apos_char}{text[apos + 1:end]}"
        replacement = POSS_TEMPLATE.format(owner=owner)
        transforms.append(Transform(
            kind="possessive",
            start=start,
            end=end,
            original=original,
            replacement=replacement,
            meta={"owner": owner},
        ))
        consumed.add(apos)
        text = text[:start] + replacement + text[end:]
    return text, transforms, consumed


def restore(normalized: str, transforms: list[Transform] | list[dict]) -> str:
    """Reverse every transform: normalised → original (§5.5.1).

    Replay is positional (each transform's original-coordinate span is
    rebased into normalised coordinates), with by-value fallbacks for the
    unique tokens (placeholders, control tokens) when the text in between
    shifted. Lossless for pristine normalised text; best-effort keeps
    working when only placeholder/control tokens survive an LLM.
    """
    if transforms and isinstance(transforms[0], dict):
        transforms = [Transform(
            kind=t["kind"], start=t["start"], end=t["end"],
            original=t["original"], replacement=t["replacement"],
            meta=t.get("meta") or {},
        ) for t in transforms]

    unicode_ts = [t for t in transforms if t.kind == "unicode"]
    poss = [t for t in transforms if t.kind == "possessive"]
    rest = [t for t in transforms
            if t.kind not in ("possessive", "unicode")]

    out = normalized

    def _delta_before(t_start: int) -> int:
        # Length change introduced by every non-unicode transform that
        # ends before ``t_start`` (unicode replacements are the coordinate
        # base, never a shift).
        return sum(len(s.replacement) - len(s.original)
                   for s in transforms
                   if s.kind != "unicode" and s.end <= t_start)

    # Replay right-to-left in normalised coordinates.
    events = [(t.start + _delta_before(t.start), t) for t in rest + poss]
    for nstart, t in sorted(events, key=lambda e: e[0], reverse=True):
        nend = nstart + len(t.replacement)
        if out[nstart:nend] == t.replacement:
            out = out[:nstart] + t.original + out[nend:]
            continue
        # Position shifted (surrounding text changed): fall back to the
        # unique-token value match.
        if t.kind == "possessive":
            out = re.sub(
                rf"\[\[POSS:{re.escape(t.meta.get('owner', ''))}\]\]"
                r"\s?('|’)s",
                t.original.replace("\\", "\\\\"), out, count=1)
        elif t.replacement in out:
            out = out.replace(t.replacement, t.original, 1)

    # NFC: the recorded original is the whole pre-normalisation text.
    if unicode_ts:
        out = unicode_ts[0].original
    return out


# ---------------------------------------------------------------------------
# Post-translation hooks for T22 (§5.5.5)
# ---------------------------------------------------------------------------
def strip_control_tokens(text: str) -> tuple[str, list[str]]:
    """Remove ``[[POSS:...]]`` control tokens after translation (§5.5.5).

    Returns ``(clean_text, removed)``; ``removed`` carries the owner names
    so the QA step can cross-check the genitive rendering.
    """
    removed: list[str] = []

    def _grab(m: re.Match) -> str:
        removed.append(m.group(1))
        return ""

    cleaned = re.sub(r"\[\[POSS:([^\]]*)\]\]", _grab, text)
    return cleaned, removed


def validate_no_control_tokens(text: str) -> list[str]:
    """Return every residual ``[[...]]`` token (empty = valid, §5.5.5)."""
    return [m.group(0) for m in _CONTROL_TOKEN_RE.finditer(text)]
