"""Deterministic QA suite (PRD §10.2 / AC1).

Wraps the pure :mod:`backend.translation.validators` with the project's
approved constraints and produces a list of *issues* in the common MQM shape
(``segment_id`` / ``category`` / ``severity`` / ``kind`` / ``evidence`` /
``message``) that the ``qa_issues`` table, the ``/qa/issues`` endpoint and the
CLI all consume.

Each deterministic control (§10.2) maps to an MQM category and a severity:

* hard validator error -> ``critical`` (the batch was rejected);
* soft validator error -> ``major`` (recorded, draft still saved);

plus a few explicit §10.2 checks (untranslated, short output) that are
``critical``/``major``. The result is reproducible: the same input always
yields the same issues (all functions are pure).
"""
from __future__ import annotations

from difflib import SequenceMatcher

from . import categories as C
from .categories import SEVERITY_RANK
from .quality_estimation import estimate_segment_quality


def _issue(segment_id, category, severity, kind, evidence, message) -> dict:
    return {
        "segment_id": segment_id,
        "category": category,
        "severity": severity,
        "kind": kind,
        "evidence": evidence,
        "message": message,
    }


def _category_for(kind: str) -> str:
    """Map a validator error ``kind`` to an MQM category (§10.4)."""
    return {
        "cardinality": C.OMISSION,
        "id_missing": C.OMISSION,
        "id_extra": C.ADDITION,
        "id_order": C.MISTRANSLATION,
        "placeholder": C.GRAMMAR,
        "numbers": C.WRONG_TERM,
        "internal_token": C.ADDITION,
        "forbidden_term": C.FORBIDDEN_TERM,
        "must_keep": C.WRONG_TERM,
        "entity": C.WRONG_TERM,
        "target_identical": C.UNTRANSLATED,
        "length": C.OMISSION,
        "merge": C.MISTRANSLATION,
        "duplicate": C.ADDITION,
    }.get(kind, C.WRONG_TERM)


def _map_errors(requested, translations, constraints) -> list[dict]:
    """Run the deterministic validators (§10.2) and map each error to an issue.

    ``requested`` / ``translations`` / ``constraints`` are exactly what
    :func:`backend.validators.validate_response` takes; the mapping below is
    the only place that turns a raw validator error into an MQM issue.

    The per-segment validators (placeholders / numbers / forbidden /
    must-keep / identical / length) report their errors with ``segment_id``
    ``None`` -- they receive only the two texts.  When the call covers a
    single segment (as :func:`run_deterministic` does), we attribute the
    error to that segment so the issue keeps its §10.4 segment link and the
    runner can persist it.
    """
    from backend.translation.validators import (
        has_hard_errors,
        validate_response,
    )
    del has_hard_errors  # imported for symmetry with the validator API

    errors = validate_response(requested=requested, response=translations,
                               constraints=constraints)
    single = (len(requested) == 1
              and requested[0].get("segment_id") is not None)
    out: list[dict] = []
    for e in errors:
        seg_id = e.get("segment_id")
        if seg_id is None and single:
            seg_id = requested[0]["segment_id"]
        severity = C.CRITICAL if e["severity"] == "hard" else C.MAJOR
        out.append(_issue(
            seg_id,
            _category_for(e["kind"]),
            severity,
            C.KIND_QA,
            None,
            e["message"])
        )
    return out


def run_deterministic(segments: list[dict], constraints: dict | None = None
                      ) -> list[dict]:
    """Run the deterministic suite over a set of segments (AC1).

    ``segments`` is the ordered list of ``{segment_id, source_text,
    target_text, ...}`` that were produced; ``constraints`` is the glossary /
    entity constraint dict (see :func:`backend.runners._constraints_for`).
    Returns every issue, sorted by severity (critical first) so the UI / CLI
    can surface the most urgent first.
    """
    constraints = constraints or {}
    out: list[dict] = []
    for seg in segments:
        seg_id = seg.get("segment_id")
        source = seg.get("source_text") or ""
        target = seg.get("target_text") or ""
        if not source or not target:
            continue
        # §10.2: untranslated / near-identical target (critical)
        if SequenceMatcher(None, source, target).ratio() >= 0.95:
            out.append(_issue(seg_id, C.UNTRANSLATED, C.CRITICAL,
                              C.KIND_QA, target[:160],
                              "Il target è identico alla sorgente.")
            )
            continue
        # §10.2: output far shorter than the source (omission)
        q = estimate_segment_quality(source, target)
        n_src = q.diagnostics.get("n_source_words", 1) or 1
        n_tgt = q.diagnostics.get("n_target_words", 1)
        if n_tgt < 0.35 * n_src:
            out.append(_issue(seg_id, C.OMISSION, C.MAJOR, C.KIND_QA,
                              target[:160],
                              "Il target è molto più corto della sorgente.")
            )
        # glossary / entity level checks the validators already cover
        for e in _map_errors(
                [{"segment_id": seg_id, "source_text": source}],
                {"translations": [{"segment_id": seg_id,
                                   "target_text": target}]},
                constraints):
            out.append(e)
    out.sort(key=lambda i: SEVERITY_RANK.get(i["severity"], 0),
             reverse=True)
    return out
