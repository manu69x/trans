"""Reference-free Quality Estimation for a segment (PRD §10.3, §18.2).

The PRD (§10.3) names COMETKiwi / XCOMET as candidates. Those need a
monolingual MT-quality model and weights that are **not** bundled, and the
platform is **local-only** (§13.1 / §17). This module therefore implements a
*local, dependency-free* QE that needs only :mod:`spacy` and :mod:`numpy` --
already part of the stack for chunking (§5.4) and TM retrieval (§7.2).

A reference-free QE scores a segment **without any human target**
[cite:279]; it reasons about the *source -> target* pair alone. To stay
honest and model-free we use signals that are **cross-lingually sound
without a translation model**:

* **invariant coverage** (§10.2) -- the fraction of source *invariants*
  (numbers, dates, URLs, and, when supplied, proper nouns / must-keep terms)
  that survive in the target. A dropped "2024", "100 kg" or a character name
  is a gross adequacy failure; this catches exactly that with no model;
* **fluency** -- fraction of target tokens a dependency parse accepts as
  well-formed;
* **length sanity** -- target/source word-count ratio, compared against the
  band a literary EN->IT translation is expected to fall in;
* **MT-ness** -- lexical repetition / low diversity, a classic automatic
  post-editing signal.

The composite ``score`` lives in ``[0, 1]`` (higher = better). It is a
**signal only (§10.3 / §17)**: it must never be the single gate for approval,
and it correlates with human MQM only imperfectly (COMET-22 ~0.69, XCOMET
~0.72 at system level [cite:273]). A real COMETKiwi/XCOMET model can be
dropped in behind :func:`estimate_segment_quality` without touching any
caller -- its public contract is ``(source, target, invariants?) -> QEScore``.

Calibration against human revisers (§18.2) lives in :func:`calibrate`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

try:  # spacy is a hard dependency of the stack (chunking / TM retrieval).
    import spacy
except Exception:  # pragma: no cover - spacy always present in this project
    spacy = None

# A single shared parser keeps the hot path cheap and lets the model stay
# warm.  The target language of this platform is Italian (EN -> IT), so the
# fluency signal parses the *target* with an Italian model; the English
# model is the fallback when the IT model is not installed.
_PARSER = None
_PARSER_NAME = "it_core_news_sm"
_FALLBACK_NAME = "en_core_web_sm"

# §10.2 / §10.3: tokens that carry no meaning and must not count toward the
# length ratio. Kept deliberately small -- we compare content words only.
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with", "as", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "has", "have", "had", "will", "would", "can", "could", "should", "i", "you", "he", "she", "it", "they", "we", "my", "your", "his", "her", "its", "your", "not", "no", "yes", "any", "some", "each", "every", "other", "what", "which", "who", "that", "this", "there", "here", "all", "both", "some", "more", "most", "other", "few"])

# expected target/source word-count ratio for a literary EN->IT rendering
# (Italian is slightly more verbose; a faithful translation stays in band).
_LENGTH_MIN = 0.55
_LENGTH_MAX = 1.95

# §10.2: tokens that must survive a translation verbatim (numbers, dates,
# URLs, codes) -- a model-free adequacy anchor.
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _get_parser():
    global _PARSER
    if _PARSER is None:
        if spacy is None:  # pragma: no cover
            raise RuntimeError("spacy is required for QE (not installed)")
        try:
            _PARSER = spacy.load(_PARSER_NAME)
        except OSError:  # IT model not installed -> English fallback
            _PARSER = spacy.load(_FALLBACK_NAME)
    return _PARSER


_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ]+|[0-9]+")


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS]


def _all_words(text: str) -> list[str]:
    """Every word, stopword included (honest length measurement)."""
    return _WORD_RE.findall(text or "")


def _invariants(text: str) -> list[str]:
    """Source invariants that must survive: dates, numbers, URLs (§10.2)."""
    out = []
    for pat in (_ISO_DATE_RE, _SLASH_DATE_RE, _URL_RE, _NUMBER_RE):
        out.extend(pat.findall(text or ""))
    return out


def _coverage(source: str, target: str, invariants) -> float:
    """Fraction of source invariants preserved in the target (§10.2).

    ``invariants`` is an optional iterable of extra anchors (proper nouns,
    must-keep terms) supplied by the project glossary. Missing invariants are
    a gross adequacy failure (a dropped number, date, URL or name). Returns
    ``1`` when there is nothing to check, ``0`` when all are missing.
    """
    inv = list(_invariants(source))
    if invariants:
        inv.extend(str(v) for v in invariants)
    if not inv:
        return 1.0
    low = target.lower()
    kept = 0
    for v in inv:
        if v and v.lower() in low:
            kept += 1
    return kept / len(inv)


def _fluency(tgt: str) -> float:
    """Fraction of target tokens that parse as well-formed (§10.3)."""
    if spacy is None:  # pragma: no cover
        return 0.5
    try:
        doc = _get_parser()(tgt)
    except Exception:  # pragma: no cover - pathological input
        return 0.5
    content = 0
    ok = 0
    for tok in doc:
        if tok.pos_ in ("PUNCT", "SPACE"):
            continue
        content += 1
        if tok.head != tok and tok.dep_ not in ("ROOT",):
            ok += 1
    if content == 0:
        return 0.5
    return ok / content


def _length_ratio(src: str, tgt: str) -> float:
    """Banded score for the target/source word-count ratio (§10.2).

    All words count (stopwords included): a target that drops half the
    sentence is shorter, whatever its function words are.
    """
    n_src = len(_all_words(src)) or 1
    n_tgt = len(_all_words(tgt))
    if n_src == 0:
        return 1.0
    r = n_tgt / n_src
    if _LENGTH_MIN <= r <= _LENGTH_MAX:
        return 1.0
    if r < _LENGTH_MIN:
        return max(0.0, 1.0 - (_LENGTH_MIN - r) / _LENGTH_MIN)
    return max(0.0, 1.0 - (r - _LENGTH_MAX) / _LENGTH_MAX)


def _mt_ness(tgt: str) -> float:
    """Repetition / low-diversity penalty (§10.3, post-editing signal).

    A target of two words or fewer carries no reliable diversity signal, so
    the score is neutral (0.5) instead of a free 1.0 -- otherwise a single
    garbage word would look "unrepetitive".
    """
    words = _WORD_RE.findall(tgt or "")
    if len(words) <= 2:
        return 0.5
    uniq = len(set(w.lower() for w in words)) / len(words)
    pairs = [tuple(w.lower() for w in words[i:i + 2])
             for i in range(len(words) - 1) if len(words) > 1]
    rep = (len(pairs) - len(set(pairs)) if pairs else 0) / max(1, len(pairs))
    return 0.5 * uniq + 0.5 * (1.0 - rep)


@dataclass
class QEScore:
    """A single segment's reference-free quality score (§10.3).

    ``score`` is the composite in ``[0, 1]``; ``signals`` holds each raw
    signal so the UI can show *why* a segment was flagged (§10.3 "spiegabile").
    """

    score: float
    coverage: float
    fluency: float
    length_ok: float
    mt_ness: float
    diagnostics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "signals": {
                "coverage": round(self.coverage, 4),
                "fluency": round(self.fluency, 4),
                "length_ok": round(self.length_ok, 4),
                "mt_ness": round(self.mt_ness, 4),
            },
            **self.diagnostics,
        }


def estimate_segment_quality(source: str, target: str,
                             invariants=None) -> QEScore:
    """Score one segment with **no** reference translation (§10.3).

    ``invariants`` is an optional iterable of source anchors (proper nouns,
    must-keep terms) that must survive; numbers/dates/URLs are always checked.
    Weights are chosen so coverage and fluency dominate (the two dimensions a
    reviser cares about most); the secondary signals nudge the score for
    length sanity and MT-ness. The result is a faithful, reproducible
    *signal* -- never a gate (§10.3 / §17).
    """
    coverage = _coverage(source, target, invariants)
    fluency = _fluency(target)
    length_ok = _length_ratio(source, target)
    mt_ness = _mt_ness(target)
    score = (0.45 * coverage + 0.30 * fluency
             + 0.15 * length_ok + 0.10 * mt_ness)
    score = float(min(1.0, max(0.0, score)) * 100) / 100.0
    return QEScore(
        score=score,
        coverage=round(coverage, 4),
        fluency=round(fluency, 4),
        length_ok=round(length_ok := _length_ratio(source, target), 4),
        mt_ness=round(mt_ness, 4),
        diagnostics={
            "n_source_words": len(_words(source)),
            "n_target_words": len(_words(target)),
        },
    )


def calibrate(scores: list[float], humans: list[float]) -> dict:
    """Calibrate QE vs human reviser (§18.2).

    ``scores`` are the automatic per-segment QE scores, ``humans`` the
    corresponding human MQM-derived scores (same order). Returns the Pearson
    correlation (QE vs human) and the mean absolute error -- the two numbers
    the PRD asks to track "calibrata per progetto". A correlation near ``1``
    means the signal is trustworthy as a *priority* signal (never a gate).
    """
    n = min(len(scores), len(humans))
    if n < 2:
        return {"pearson": None, "mae": None, "n": n}
    s = np.array(scores[:n], dtype=float)
    h = np.array(humans[:n], dtype=float)
    if np.std(s) == 0 or np.std(h) == 0:
        pearson = 0.0
    else:
        pearson = float(np.corrcoef(s, h)[0, 1])
    mae = float(np.mean(np.abs(s - h)) if n else 0.0)
    return {"pearson": round(pearson, 4), "mae": round(mae, 4), "n": n}
