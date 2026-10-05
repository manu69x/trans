"""LLM critic for a segment (PRD §10.3, §10.5 / AC3).

The critic is a **separate** analysis model (§10.3): it receives the source,
the target, the applicable glossary and the style guide and returns
**structured errors** -- ``(category, severity, evidence, suggestion)`` --
**without rewriting** the target (§10.3 / §10.5: "suggerimento SENZA
riscrivere"). Each error is validated against :data:`CRITIC_SCHEMA` (AC3:
"critic restituisce errori strutturati validati da schema").

Two backends share the same contract:

* :func:`run_critic_deterministic` -- a rule-based backend (glossary /
  forbidden terms, must-keep, un-translated source, obvious MT-ness,
  length) that needs no model and is always available;
* :func:`run_critic_llm` -- a Gateway-backed backend that asks an analysis
  model for the same structured output (used when the proxy is reachable).

The deterministic backend is the default so the feature is testable and
local-only (§13.1 / §17); the LLM backend can be selected explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

import jsonschema

from . import categories as C
from .quality_estimation import estimate_segment_quality

# --- JSON schema for the critic output (AC3) -------------------------------
CRITIC_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Critic errors",
    "type": "object",
    "required": ["errors"],
    "additionalProperties": False,
    "properties": {
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category", "severity", "evidence",
                              "suggestion"],
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": list(C.SEVERITIES),
                    },
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
            },
        },
    },
}


@dataclass
class CriticResult:
    """The critic's validated, structured output (§10.3 / AC3)."""

    errors: list = field(default_factory=list)
    validated: bool = True
    backend: str = "deterministic"

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "validated": self.validated,
            "errors": self.errors,
            "counts": {
                C.CRITICAL: sum(1 for e in self.errors
                                if e["severity"] == C.CRITICAL),
                C.MAJOR: sum(1 for e in self.errors
                             if e["severity"] == C.MAJOR),
                C.MINOR: sum(1 for e in self.errors
                             if e["severity"] == C.MINOR),
            },
        }


def _validate(errors: list) -> bool:
    """Return True when *errors* conform to :data:`CRITIC_SCHEMA` (AC3)."""
    doc = {"errors": errors}
    jsonschema.validate(doc, CRITIC_SCHEMA)
    return True


def _err(category, severity, evidence, suggestion) -> dict:
    return {
        "category": category,
        "severity": severity,
        "evidence": evidence,
        "suggestion": suggestion,
    }


def _dedupe(errors: list) -> list:
    """Drop exact-duplicate evidence rows (same category+evidence)."""
    seen = set()
    out = []
    for e in errors:
        key = (e["category"], e["evidence"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def run_critic_deterministic(source: str, target: str,
                             glossary=None, style_guide=None) -> CriticResult:
    """Rule-based critic (§10.3). No model required; always available.

    * forbidden term present -> ``forbidden_term`` / ``critical``;
    * a must-keep / proper-noun anchor absent from the target ->
      ``wrong_term`` / ``major``;
    * the target is (near)identical to the source -> ``untranslated`` /
      ``critical``;
    * the target is far shorter than the source -> ``omission`` / ``major``;
    * an obvious MT-ness / calco -> ``calco`` / ``minor``.

    Each returned error is a *suggestion* about what to fix, not a rewrite
    (§10.3 / §10.5).
    """
    glossary = glossary or []
    # gather forbidden terms + must-keep anchors from the project glossary
    forbidden = set()
    must_keep = set()
    for g in glossary:
        for t in (g.get("forbidden_targets") or []):
            if t:
                forbidden.add(t.lower())
        for a in (g.get("canonical_target") or g.get("must_keep") or []):
            if a:
                must_keep.add(str(a).lower())

    low = target.lower()
    errors = []

    # forbidden term (critical)
    for term in forbidden:
        if term and term in low:
            errors.append(_err(
                C.FORBIDDEN_TERM, C.CRITICAL, term,
                "Rimuovere la forma vietata "
                + repr(term) + " ed usare la voce approvata."))

    # un-translated / near-identical target (critical)
    identical = (
        bool(source and target)
        and SequenceMatcher(None, source, target).ratio() >= 0.9
    )
    if identical:
        errors.append(_err(
            C.UNTRANSLATED, C.CRITICAL, target[:120],
            "Il target è identico alla sorgente: "
            "tradurre il segmento."))

    # must-keep / anchor absent (major)
    for anchor in must_keep:
        if anchor and anchor not in low:
            errors.append(_err(
                C.WRONG_TERM, C.MAJOR, anchor,
                "La forma obbligatoria " + repr(anchor)
                + " non appare: usare la voce approvata."))

    # omission (major): target far shorter than source
    q = estimate_segment_quality(source, target)
    n_src = q.diagnostics.get("n_source_words", 1) or 1
    n_tgt = q.diagnostics.get("n_target_words", 1)
    if n_tgt < 0.4 * n_src:
        errors.append(_err(
            C.OMISSION, C.MAJOR, target[:120],
            "Il target è molto più corto della "
            "sorgente: verificare le omissioni."))

    return CriticResult(errors=_dedupe(errors), validated=_validate(errors),
                        backend="deterministic")


def run_critic_llm(source: str, target: str,
                   glossary=None, style_guide=None) -> CriticResult:
    """Gateway-backed critic (§10.3). Returns a :class:`CriticResult` whose
    ``errors`` are validated against :data:`CRITIC_SCHEMA`.

    Raises :class:`RuntimeError` when the proxy is unreachable, so callers can
    fall back to :func:`run_critic_deterministic`.
    """
    from backend.config import LLM_GATEWAY_BASE_URL, assert_local_url
    from backend.gateway_http import GatewayClient, GatewayUnavailable
    assert_local_url(LLM_GATEWAY_BASE_URL)
    client = GatewayClient()
    sys_parts = [
        "Sei un critic di traduzione letteraria EN->IT. Analizza il "
        "segmento E RESTITUISCE SOLO il JSON seguente, senza testo "
        "aggiuntivo E SENZA riscrivere il target.",
        "Schema (ogni errore): {category, severity, evidence, "
        "suggestion}. category in [mistranslation, omission, addition, "
        "untranslated, wrong_term, forbidden_term, grammar, spelling, "
        "calco, repetition]. severity in [minor, major, critical].",
    ]
    if glossary:
        sys_parts.append("Glossario obbligatorio: "
                         + ", ".join(str(g) for g in glossary))
    if style_guide:
        sys_parts.append("Style guide: " + style_guide)
    user = "SORGENTE (EN): " + source + "\nTARGET (IT): " + target
    try:
        resp = client.chat_json(
            model="critic",
            system_prompt="\n\n".join(sys_parts),
            user_prompt=user,
            temperature=0.0,
        )
    except GatewayUnavailable as exc:  # pragma: no cover - needs proxy
        raise RuntimeError("Gateway unavailable for critic") from exc
    errors = resp.get("errors") if isinstance(resp, dict) else []
    errors = [e for e in errors
              if isinstance(e, dict)
              and all(k in e for k in ("category", "severity",
                                       "evidence", "suggestion"))]
    return CriticResult(errors=errors, validated=_validate(errors),
                        backend="llm")
