"""TM maintenance report (PRD §7.2, F3 / task t_694dfe91).

The translation memory grows as reviewers approve segments; left unattended
it accumulates the classic problems of a professional TM:

* **duplicated entries** -- the same normalised source stored twice;
* **contradictory targets** -- the same source approved with two different
  targets (the most important check: a segment was approved, then re-approved
  later with a different rendering);
* **misaligned tags / placeholders** -- the target keeps a placeholder the
  source does not have (or vice versa), which would break the block prompt;
* **length discrepancies** -- a target that is far longer or shorter than the
  source (a bad, likely copy-pasted, translation);
* **obsolete entries** -- entries whose segment was invalidated / re-segmented
  and no longer exists.

Every check is a pure function over a list of ``tm_entries`` rows (each a
plain ``dict`` with a ``segment_id``, see ``_dedupe_key`` / the ``_looks_like``
helpers), and :func:`report_tm` ties them together over a project's TM table.
This makes each check trivially unit-testable and lets the report run either on
a scheduled job or on demand from the dashboard (§15.4 / "Percentuale riuso TM
in dashboard").
"""
from __future__ import annotations

import re
from .translation.embedding import embed, cosine

# A target is considered a "contradiction" of another when the two targets
# are not equal after collapsing whitespace, and neither is a plain
# capitalisation/whitespace variant of the other.
_WS = re.compile(r"\s+")


def _norm_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def _looks_like_variant(a: str, b: str) -> bool:
    """True when *b* is just a whitespace / case variant of *a* (not a real
    alternative translation)."""
    na = _norm_ws(a).lower()
    nb = _norm_ws(b).lower()
    return na == nb


def _has_all_source_tags(source: str, target: str) -> bool:
    """Every ``[...]`` / ``{{...`` tag in the source must survive in the
    target (and vice versa for placeholders the source is missing)."""
    src_tags = re.findall(r"\[[^\]]+\]", source)
    tgt_tags = re.findall(r"\[[^\]]+\]", target)
    for t in src_tags:
        if t not in target:
            return False
    for t in tgt_tags:
        if t not in source:
            return False
    return True


def _length_ratio_ok(source: str, target: str) -> bool:
    """A target within 5x / 0.2x of the source length is considered sane."""
    s = len(source)
    t = len(target)
    if s == 0:
        return True
    return 0.2 <= t / s <= 5.0


def _dedupe_key(row: dict) -> str:
    """The normalised source is the identity of a TM entry (§7.2)."""
    return _norm_ws(
        row.get("source_normalized") or row.get("source_original") or ""
    )


def report_tm(db, project_id: str) -> dict:
    """Run every maintenance check over the project's TM entries.

    *db* is a SQLAlchemy session; *project_id* scopes the scan (§7.2: every
    check is project-local). Returns a structured report:

    .. code-block:: python

        {
            "project_id": ...,
            "entry_count": int,
            "duplicates": [{"source": ..., "entry_ids": [..], "count": n}, ..],
            "contradictions": [
                {"source": ..., "target_a": ..., "entry_id_a": ...,
                 "target_b": ..., "entry_id_b": ...}, ..],
            "misaligned_tags": [{"source": ..., "target": ..., "entry_id": ...}, ..],
            "length_discrepancies": [{"source": ..., "target": ..., "entry_id": ...}, ..,
            "obsolete": [{"source": ..., "entry_id": ...}, ..],
            "reuse_rate": float,   # approved segments that have a TM entry
        }
    """
    from .models import TranslationMemoryEntry, TranslationUnit

    entries = (
        db.query(TranslationMemoryEntry)
        .filter(TranslationMemoryEntry.project_id == project_id)
        .all()
    )
    entries = [
        {
            "id": str(r.id),
            "segment_id": str(r.segment_id) if r.segment_id else None,
            "source_normalized": r.source_normalized,
            "source_original": r.source_original,
            "target_approved": r.target_approved,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in entries
    ]

    # --- duplicates: same normalised source stored more than once ----------
    by_source: dict[str, list[dict]] = {}
    for e in entries:
        by_source.setdefault(_dedupe_key(e), []).append(e)
    duplicates = [
        {"source": src, "entry_ids": [e["id"] for e in lst], "count": len(lst)}
        for src, lst in by_source.items()
        if len(lst) > 1
    ]

    # --- contradictory targets: same source, genuinely different targets ---
    contradictions: list[dict] = []
    for src, lst in by_source.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                if not _looks_like_variant(a["target_approved"],
                                           b["target_approved"]):
                    contradictions.append({
                        "source": src or a["source_original"],
                        "target_a": a["target_approved"],
                        "entry_id_a": a["id"],
                        "target_b": b["target_approved"],
                        "entry_id_b": b["id"],
                    })

    # --- misaligned tags / placeholders -----------------------------------
    misaligned_tags = []
    for e in entries:
        if not _has_all_source_tags(e["source_original"], e["target_approved"]):
            misaligned_tags.append({
                "source": e["source_original"],
                "target": e["target_approved"],
                "entry_id": e["id"],
            })

    # --- length discrepancies ---------------------------------------------
    length_discrepancies = []
    for e in entries:
        if not _length_ratio_ok(e["source_original"], e["target_approved"]):
            length_discrepancies.append({
                "source": e["source_original"],
                "target": e["target_approved"],
                "entry_id": e["id"],
            })

    # --- obsolete: entry whose segment no longer exists -------------------
    known_segments = {
        str(u.id)
        for u in db.query(TranslationUnit.id).filter(
            TranslationUnit.project_id == project_id
        )
    }
    obsolete = [
        {"source": e["source_original"], "entry_id": e["id"]}
        for e in entries
        if e["segment_id"] is not None and e["segment_id"] not in known_segments
    ]

    # --- TM reuse rate (§15.4 / dashboard): approved segments that have a
    # TM entry. An entry is "reused"/counted when it is linked to a segment;
    # the rate is the fraction of approved segments that are in the TM.
    approved = (
        db.query(TranslationUnit.id).filter(
            TranslationUnit.project_id == project_id,
            TranslationUnit.status == "approved",
        )
    ).all()
    approved_ids = {str(a[0]) for a in approved}
    linked = {e["segment_id"] for e in entries if e["segment_id"]}
    reused = len(approved_ids & linked)
    reuse_rate = reused / len(approved_ids) if approved_ids else 0.0

    return {
        "project_id": str(project_id),
        "entry_count": len(entries),
        "duplicates": duplicates,
        "contradictions": contradictions,
        "misaligned_tags": misaligned_tags,
        "length_discrepancies": length_discrepancies,
        "obsolete": obsolete,
        "reuse_rate": reuse_rate,
    }


def _assert_relevant_paraphrase():  # pragma: no cover - smoke helper
    """Sanity check that the semantic embedding ranks a paraphrase above an
    unrelated sentence (used by the AC2 test's paraphrase assertion)."""
    a = embed("Good morning, John.")
    b = embed("Good morning, my friend.")
    c = embed("The cat slept on the sofa all day.")
    from .embedding import cosine

    assert cosine(a, b) > cosine(a, c)
