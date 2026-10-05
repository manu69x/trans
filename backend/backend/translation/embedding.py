"""Deterministic local sentence embedding for TM retrieval (PRD §7.2).

The platform is **local-only** (PRD §13.1): no manuscript text may leave the
local network, and inference goes only through LLM Gateway. The translation
memory semantic retrieval (§7.2 step 3, "pgvector semantico") therefore needs
an embedding function that never touches the internet. This module provides a
*fully deterministic, offline* embedding: it hashes each token into a sparse
768-dimensional vector (the same dimension as the ``source_embedding`` column
of :class:`~backend.models.translation.TMEntry`), so it plugs directly into
pgvector without any external model.

The scheme is a simple but effective bag-of-hashed-grams:

* each token is lower-cased and split into character n-grams of length 1-3;
* each n-gram is hashed (FNV-1a) into a bucket in ``[-EMBEDDing_DIM//2,
  EMBEDDING_DIM//2)`` with a signed contribution;
* the contributions are summed and the vector is L2-normalised.

Determinism is the whole point: the same text always maps to the same vector,
which is exactly what a TM needs (an approved segment must keep matching
itself). Cosine similarity over these vectors ranks near-duplicate and
paraphrased segments well enough for a first-pass retrieval that is then
refined by the exact/fuzzy layers and by the human reviewer.
"""
from __future__ import annotations

import math
import re

EMBEDDING_DIM = 768  # must match TMEntry.source_embedding Vector(768)

_GRAM_RE = re.compile(r"[a-zà-ÿ0-9']+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-zà-ÿ0-9]+", re.IGNORECASE)


def _fnv1a(text: str) -> int:
    """FNV-1a hash of *text* as a non-negative 64-bit integer."""
    h = 0x811c9dc5
    for b in text.encode("utf-8", "ignore"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFFFFFFFFFF
    return h


def _grams(token: str, max_len: int = 3) -> list[str]:
    tok = token.lower()
    grams = {tok}
    for n in (2, 3):
        if len(tok) >= n:
            grams.update(tok[i : i + n] for i in range(len(tok) - n + 1))
    if len(tok) < max_len:
        grams.add(tok)
    return sorted(grams)


def embed(text: str) -> list[float]:
    """Return the deterministic 768-dim embedding of *text*.

    The vector is L2-normalised; an empty/whitespace input maps to the
    all-zero vector (cosine similarity with it is 0 against everything).
    """
    vec = [0.0] * EMBEDDING_DIM
    if not text:
        return vec

    # Each n-gram contributes once; weight rare-ish multi-gram tokens a touch
    # higher so multi-word segments stay distinct from single-word noise.
    for token in _TOKEN_RE.findall(text):
        for gram in _grams(token):
            h = _fnv1a(gram)
            # map the hash into a symmetric bucket and a signed contribution
            bucket = h % (EMBEDDING_DIM // 2)
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[bucket] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 if either is empty)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a) or 0.0)
    nb = math.sqrt(sum(y * y for y in b) or 0.0)
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
