"""TM retrieval (PRD §7.2).

Three-stage funnel: exact (normalised source) -> fuzzy (lexical Jaccard) ->
semantic (pgvector cosine). Every stage applies the mandatory project filter
and only returns scores at/above the configurable threshold. A weak (semantic)
hit whose genre/pov disagrees with the block is flagged as an incompatible
context so the planner can drop it and surface the conflict (§7.2 step 6).

Pure / DB-free: the routes supply the (project-scoped) candidate rows and
persist the retrieved matches.
"""
from __future__ import annotations

import re

from .embedding import cosine, embed

DEFAULT_EXACT_THRESHOLD = 1.0
DEFAULT_FUZZY_THRESHOLD = 0.65
DEFAULT_SEMANTIC_THRESHOLD = 0.6
DEFAULT_MAX_MATCHES = 5


def _norm(source: str) -> str:
    """Normalise a source for exact/fuzzy comparison (PRD §7.2 / §5.5)."""
    s = (source or "").lower()
    s = re.sub(r"\[\[.*?\]\]", " ", s)
    s = re.sub(r"[^a-z0-9\sà-ÿ]", " ", s)
    return " ".join(s.split())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zà-ÿ0-9]+", _norm(text).lower()))


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _in_project(candidate: dict, project_id) -> bool:
    return project_id is None or str(candidate.get("project_id")) == str(project_id)


def _hit(row: dict, score: float, method: str, *, project_ok: bool) -> dict:
    out = dict(row)
    out["score"] = round(score, 4)
    out["method"] = method
    out["project_ok"] = project_ok
    return out


def retrieve_tm(
    *,
    block_source: str,
    candidates: list[dict],
    project_id: str | None = None,
    exact_threshold: float = DEFAULT_EXACT_THRESHOLD,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    max_matches: int = DEFAULT_MAX_MATCHES,
    embed_source: bool = True,
) -> list[dict]:
    """Return TM matches for *block_source* ordered by confidence.

    ``candidates`` are already project-scoped rows; each returned item carries
    the original fields plus ``score`` (0..1), ``method`` and ``project_ok``.
    Each stage's threshold is configurable (§7.2 step 5: "soglia configurabile").
    """
    block_norm = _norm(block_source)
    block_emb = embed(block_source) if embed_source else None

    scored: list[dict] = []
    for c in candidates:
        src = c.get("source_normalized") or c.get("source_original") or ""
        project_ok = _in_project(c, project_id)
        if block_norm and _norm(src) == block_norm:
            if exact_threshold <= 1.0:
                scored.append(_hit(c, 1.0, "exact", project_ok=project_ok))
            continue
        jac = _jaccard(src, block_source)
        if jac >= fuzzy_threshold:
            score = 0.5 + 0.5 * jac
            scored.append(_hit(c, score, "fuzzy", project_ok=project_ok))
            continue
        if block_emb is not None:
            cemb = c.get("source_embedding") or embed(src)
            sim = cosine(block_emb, cemb)
            if sim >= semantic_threshold:
                scored.append(_hit(c, sim, "semantic", project_ok=project_ok))

    _method_rank = {"exact": 0, "fuzzy": 1, "semantic": 2}
    scored.sort(key=lambda h: (_method_rank.get(h["method"], 9), -h["score"]))
    return scored[:max_matches]


def is_incompatible_context(
    tm_match: dict, *, block_genre: str | None, block_pov: str | None
) -> bool:
    """§7.2 step 6: would a TM match impose an incompatible narrative context?

    §7.2 step 6 ("non far copiare meccanicamente una TM se il contesto
    narrativo è incompatibile") applies to **every** hit — an exact/fuzzy/
    semantic match whose stored genre/pov disagrees with the block must not be
    copied mechanically, regardless of its score.
    """
    if block_genre is None and block_pov is None:
        return False
    if tm_match.get("genre_profile") and block_genre is not None \
            and str(tm_match["genre_profile"]) != str(block_genre):
        return True
    if tm_match.get("pov") and block_pov is not None \
            and str(tm_match["pov"]) != str(block_pov):
        return True
    return False
